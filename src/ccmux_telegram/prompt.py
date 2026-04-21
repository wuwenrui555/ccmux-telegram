"""Interactive prompt UI rendering for Claude Code prompts.

Renders interactive terminal UIs (AskUserQuestion, ExitPlanMode, Permission
Prompt, RestoreCheckpoint) as Telegram inline keyboards + captured terminal
content. Prompt state (mode tracking, msg_id tracking) lives in
`prompt_state.py` so non-UI modules can coordinate without touching this
module.

Provides:
  - handle_interactive_ui: Capture terminal and send/edit prompt UI
  - clear_interactive_msg: Dismiss an active prompt (state + Telegram)
  - handle_interactive_callback: Callback router for arrow/Esc/Enter keys
"""

import asyncio
import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from .runtime import get_topic
from ccmux.api import BlockedUI, extract_interactive_content, tmux_registry
from .util import get_thread_id, get_tm_and_window
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from .sender import NO_LINK_PREVIEW
from .prompt_state import (
    get_interactive_msg_id,
    pop_interactive_state,
    set_interactive_mode,
    set_interactive_msg_id,
)

logger = logging.getLogger(__name__)


def _build_interactive_keyboard(
    window_id: str,
    ui_name: str = "",
) -> InlineKeyboardMarkup:
    """Build keyboard for interactive UI navigation.

    `ui_name` controls the layout: `restore_checkpoint` omits ←/→ keys
    since only vertical selection is needed. Accepts the BlockedUI
    value string (e.g. 'restore_checkpoint') or the legacy PascalCase
    name; both compared case-insensitively.
    """
    vertical_only = ui_name.lower() in {
        "restore_checkpoint",
        "restorecheckpoint",
    }

    rows: list[list[InlineKeyboardButton]] = []
    # Row 1: directional keys
    rows.append(
        [
            InlineKeyboardButton(
                "␣ Space", callback_data=f"{CB_ASK_SPACE}{window_id}"[:64]
            ),
            InlineKeyboardButton("↑", callback_data=f"{CB_ASK_UP}{window_id}"[:64]),
            InlineKeyboardButton(
                "⇥ Tab", callback_data=f"{CB_ASK_TAB}{window_id}"[:64]
            ),
        ]
    )
    if vertical_only:
        rows.append(
            [
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "←", callback_data=f"{CB_ASK_LEFT}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "→", callback_data=f"{CB_ASK_RIGHT}{window_id}"[:64]
                ),
            ]
        )
    # Row 2: action keys
    rows.append(
        [
            InlineKeyboardButton(
                "⎋ Esc", callback_data=f"{CB_ASK_ESC}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "🔄", callback_data=f"{CB_ASK_REFRESH}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "⏎ Enter", callback_data=f"{CB_ASK_ENTER}{window_id}"[:64]
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    chat_id: int | None = None,
    *,
    ui: "BlockedUI | None" = None,
    content: str | None = None,
) -> bool:
    """Render a blocking UI to Telegram.

    If `ui` + `content` are provided (e.g. by the `on_state` consumer),
    they are used directly. Otherwise the function falls back to
    capturing the pane itself via the tmux_registry (legacy callers,
    primarily the refresh callback).
    """
    if chat_id is None:
        return False

    # Fast path: caller already has the parsed UI.
    if ui is not None and content is not None:
        ui_name = ui.value
        text = content
    else:
        # Legacy path: capture pane and extract. Kept for callback
        # refresh handlers that do not carry a ClaudeState.
        tm = tmux_registry.get_by_window_id(window_id)
        if not tm:
            return False
        w = await tm.find_window_by_id(window_id)
        if not w:
            return False
        pane_text = await tm.capture_pane(w.window_id)
        if not pane_text:
            logger.debug("No pane text captured for window_id %s", window_id)
            return False
        extracted = extract_interactive_content(pane_text)
        if not extracted:
            logger.debug(
                "No interactive UI detected in window_id %s (last 3 lines: %s)",
                window_id,
                pane_text.strip().split("\n")[-3:],
            )
            return False
        ui_name = extracted.ui.value
        text = extracted.content

    # Build message with navigation keyboard.
    keyboard = _build_interactive_keyboard(window_id, ui_name=ui_name)

    # The extracted content already carries the tool-preview block from
    # the pane (Claude renders `<Tool name>\n<Tool call>\n\nDo you want
    # to proceed?` as a single region, which the parser's walkback
    # captures). No JSONL lookup needed — see drop-tool-context.
    thread_kwargs: dict[str, int] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    existing_msg_id = get_interactive_msg_id(user_id, thread_id)
    if existing_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_msg_id,
                text=text,
                reply_markup=keyboard,
                link_preview_options=NO_LINK_PREVIEW,
            )
            set_interactive_mode(user_id, window_id, thread_id)
            return True
        except BadRequest as e:
            # "message is not modified" means the payload is identical
            # to what's already on Telegram -- a no-op success, not a
            # failure. Don't pop state or re-send; that would spam.
            if "not modified" in str(e).lower():
                set_interactive_mode(user_id, window_id, thread_id)
                return True
            logger.debug(
                "Edit failed for interactive msg %s (%s), sending new",
                existing_msg_id,
                e,
            )
            pop_interactive_state(user_id, thread_id)
        except Exception:
            logger.debug(
                "Edit failed for interactive msg %s, sending new", existing_msg_id
            )
            pop_interactive_state(user_id, thread_id)

    logger.info(
        "Sending interactive UI to user %d for window_id %s", user_id, window_id
    )
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            link_preview_options=NO_LINK_PREVIEW,
            **thread_kwargs,  # type: ignore[arg-type]
        )
    except Exception as e:
        logger.error("Failed to send interactive UI: %s", e)
        return False
    if sent:
        set_interactive_msg_id(user_id, sent.message_id, thread_id)
        set_interactive_mode(user_id, window_id, thread_id)
        return True
    return False


async def clear_interactive_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
    chat_id: int | None = None,
) -> None:
    """Clear tracked interactive message, delete from chat, and exit interactive mode.

    When chat_id is None, state is cleared but the Telegram message (if any)
    is left orphaned. Callers that want a clean delete should pass chat_id.
    """
    msg_id = pop_interactive_state(user_id, thread_id)
    if bot and msg_id and chat_id is not None:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass  # Message may already be deleted or too old


async def handle_interactive_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle all interactive UI callback queries (arrows, esc, enter, space, tab, refresh)."""
    query = update.callback_query
    assert query and query.data
    user = update.effective_user
    assert user
    data = query.data

    # Resolve chat_id once for all interactive callbacks
    thread_id = get_thread_id(update)
    topic = get_topic(user.id, thread_id)
    cb_chat_id = topic.group_chat_id if topic else None

    if data.startswith(CB_ASK_UP):
        window_id = data[len(CB_ASK_UP) :]
        pair = await get_tm_and_window(window_id)
        if pair:
            tm, w = pair
            await tm.send_keys(w.window_id, "Up", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(
                context.bot, user.id, window_id, thread_id, chat_id=cb_chat_id
            )
        await query.answer()

    elif data.startswith(CB_ASK_DOWN):
        window_id = data[len(CB_ASK_DOWN) :]
        pair = await get_tm_and_window(window_id)
        if pair:
            tm, w = pair
            await tm.send_keys(w.window_id, "Down", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(
                context.bot, user.id, window_id, thread_id, chat_id=cb_chat_id
            )
        await query.answer()

    elif data.startswith(CB_ASK_LEFT):
        window_id = data[len(CB_ASK_LEFT) :]
        pair = await get_tm_and_window(window_id)
        if pair:
            tm, w = pair
            await tm.send_keys(w.window_id, "Left", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(
                context.bot, user.id, window_id, thread_id, chat_id=cb_chat_id
            )
        await query.answer()

    elif data.startswith(CB_ASK_RIGHT):
        window_id = data[len(CB_ASK_RIGHT) :]
        pair = await get_tm_and_window(window_id)
        if pair:
            tm, w = pair
            await tm.send_keys(w.window_id, "Right", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(
                context.bot, user.id, window_id, thread_id, chat_id=cb_chat_id
            )
        await query.answer()

    elif data.startswith(CB_ASK_ESC):
        window_id = data[len(CB_ASK_ESC) :]
        pair = await get_tm_and_window(window_id)
        if pair:
            tm, w = pair
            await tm.send_keys(w.window_id, "Escape", enter=False, literal=False)
            await clear_interactive_msg(
                user.id, context.bot, thread_id, chat_id=cb_chat_id
            )
        await query.answer("⎋ Esc")

    elif data.startswith(CB_ASK_ENTER):
        window_id = data[len(CB_ASK_ENTER) :]
        pair = await get_tm_and_window(window_id)
        if pair:
            tm, w = pair
            await tm.send_keys(w.window_id, "Enter", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(
                context.bot, user.id, window_id, thread_id, chat_id=cb_chat_id
            )
        await query.answer("⏎ Enter")

    elif data.startswith(CB_ASK_SPACE):
        window_id = data[len(CB_ASK_SPACE) :]
        pair = await get_tm_and_window(window_id)
        if pair:
            tm, w = pair
            await tm.send_keys(w.window_id, "Space", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(
                context.bot, user.id, window_id, thread_id, chat_id=cb_chat_id
            )
        await query.answer("␣ Space")

    elif data.startswith(CB_ASK_TAB):
        window_id = data[len(CB_ASK_TAB) :]
        pair = await get_tm_and_window(window_id)
        if pair:
            tm, w = pair
            await tm.send_keys(w.window_id, "Tab", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(
                context.bot, user.id, window_id, thread_id, chat_id=cb_chat_id
            )
        await query.answer("⇥ Tab")

    elif data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        await handle_interactive_ui(
            context.bot, user.id, window_id, thread_id, chat_id=cb_chat_id
        )
        await query.answer("🔄")
