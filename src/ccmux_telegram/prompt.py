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
import re

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
from .sender import NO_LINK_PREVIEW, PARSE_MODE
from .prompt_state import (
    PROMPT_TOOL_NAMES,
    get_interactive_msg_id,
    pop_interactive_state,
    set_interactive_mode,
    set_interactive_msg_id,
)

logger = logging.getLogger(__name__)


# Footer lines that Claude renders at the bottom of a blocking UI —
# styled as italic so the directional/action hint visually recedes
# below the actionable content.
_FOOTER_HINT_RE = re.compile(r"^\s*(Esc to |Enter to )")
# The caret that marks the currently-selected option; bold the full
# line so Telegram users see their next-press target at a glance.
_SELECTED_OPTION_RE = re.compile(r"^\s*❯\s+")


def _format_blocked_content(text: str) -> str:
    """Apply heuristic Markdown to raw pane content.

    Returns plain Markdown that :func:`_render_mdv2` will translate to
    Telegram MarkdownV2. Leading whitespace stays *outside* the markers
    because Markdown does not recognize ``** x**`` (a space right after
    the opening ``**``) as bold.

    Rules, line-by-line:

    - The first non-empty line is the tool title or the question
      (``Read file``, ``Bash command``, ``Enable auto mode?``, ``Do you
      want to proceed?``). Bold it.
    - Any subsequent line ending with ``?`` is also a question header
      (e.g. ``Do you want to proceed?`` inside a tool-preview block).
      Bold.
    - Lines starting with ``❯`` are the currently-selected option.
      Bold the whole line.
    - Lines starting with ``Esc to `` / ``Enter to `` are the footer
      hint bar. Render italic.

    Heuristics only — we do not parse structure. The rules are chosen to
    be safe when they miss (a plain-text line stays plain text).
    """
    lines = text.split("\n")
    styled: list[str] = []
    first_nonblank_done = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            styled.append(line)
            continue

        indent_len = len(line) - len(line.lstrip())
        indent = line[:indent_len]
        body = line[indent_len:].rstrip()

        if not first_nonblank_done:
            styled.append(f"{indent}**{body}**")
            first_nonblank_done = True
            continue

        if _SELECTED_OPTION_RE.match(line):
            styled.append(f"{indent}**{body}**")
            continue

        if _FOOTER_HINT_RE.match(line):
            styled.append(f"{indent}_{body}_")
            continue

        if stripped.endswith("?"):
            styled.append(f"{indent}**{body}**")
            continue

        styled.append(line)
    return "\n".join(styled)


def _render_from_tool_args(tool_name: str, args: dict) -> tuple[str, str] | None:
    """Render a prompt UI directly from a JSONL `tool_use.input` dict.

    Used as the fallback when `extract_interactive_content` cannot pull
    a UI from the captured pane (scroll race / fast TUI answer / pane
    flicker). The output `(ui_name, text)` tuple matches what the
    pane-capture path produces for the same prompt, so the downstream
    `_format_blocked_content` / `_render_mdv2` / keyboard build code
    sees an identical shape regardless of source.

    Returns None for any tool other than `AskUserQuestion` /
    `ExitPlanMode` so this stays a pure helper with no side effects.
    """
    if tool_name == "AskUserQuestion":
        questions = args.get("questions")
        if not isinstance(questions, list) or not questions:
            return None
        sections: list[str] = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            question_text = q.get("question", "")
            options = q.get("options", [])
            lines: list[str] = []
            if question_text:
                lines.append(question_text)
            if isinstance(options, list):
                for i, opt in enumerate(options, start=1):
                    if not isinstance(opt, dict):
                        continue
                    label = opt.get("label", "")
                    description = opt.get("description", "")
                    if description:
                        lines.append(f"  {i}. {label} — {description}")
                    elif label:
                        lines.append(f"  {i}. {label}")
            if lines:
                sections.append("\n".join(lines))
        if not sections:
            return None
        return "ask_user_question", "\n\n".join(sections)

    if tool_name == "ExitPlanMode":
        plan = args.get("plan")
        if not isinstance(plan, str) or not plan:
            return None
        return "exit_plan_mode", plan

    return None


# MarkdownV2 escaping — Telegram requires these characters to be
# backslash-prefixed when they appear as literal text inside a message:
#   _ * [ ] ( ) ~ ` > # + - = | { } . ! \
_MDV2_SPECIAL = set("_*[]()~`>#+-=|{}.!\\")


def _escape_mdv2_chunk(s: str) -> str:
    """Escape every MarkdownV2 special char inside a literal chunk."""
    return "".join("\\" + c if c in _MDV2_SPECIAL else c for c in s)


def _render_mdv2(text: str) -> str:
    """Translate our limited Markdown to Telegram MarkdownV2.

    Handles only ``**bold**`` and ``_italic_`` pairs; everything else is
    escaped as literal. We bypass the full ``convert_markdown`` pipeline
    because its mistletoe parser rewrites ordered-list indentation
    (``1. Yes`` / ``2. No``) and rejects ``** x**`` bold with a space
    after the opening marker — both common in Claude's blocking UIs.
    """
    out: list[str] = []
    out_lines: list[str] = []
    for line in text.split("\n"):
        i = 0
        line_out: list[str] = []
        while i < len(line):
            if line[i : i + 2] == "**":
                end = line.find("**", i + 2)
                if end != -1:
                    body = line[i + 2 : end]
                    line_out.append("*")
                    line_out.append(_escape_mdv2_chunk(body))
                    line_out.append("*")
                    i = end + 2
                    continue
            if line[i] == "_":
                end = line.find("_", i + 1)
                if end != -1:
                    body = line[i + 1 : end]
                    line_out.append("_")
                    line_out.append(_escape_mdv2_chunk(body))
                    line_out.append("_")
                    i = end + 1
                    continue
            c = line[i]
            line_out.append("\\" + c if c in _MDV2_SPECIAL else c)
            i += 1
        out_lines.append("".join(line_out))
    out.append("\n".join(out_lines))
    return "".join(out)


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
    tool_name: str | None = None,
    tool_use_args: dict | None = None,
) -> bool:
    """Render a blocking UI to Telegram.

    If `ui` + `content` are provided (e.g. by the `on_state` consumer),
    they are used directly. Otherwise the function tries pane capture
    first (the primary path), and on failure falls back to rendering
    `AskUserQuestion` / `ExitPlanMode` prompts from the JSONL
    `tool_use_args` dict (covers tmux scroll race, fast TUI answers,
    transient pane flicker). Returns False only if neither path
    produces a UI.
    """
    if chat_id is None:
        return False

    # Fast path: caller already has the parsed UI.
    if ui is not None and content is not None:
        ui_name: str | None = ui.value
        text: str | None = content
    else:
        ui_name = None
        text = None
        # Primary path: capture pane and extract.
        tm = tmux_registry.get_by_window_id(window_id)
        if tm:
            w = await tm.find_window_by_id(window_id)
            if w:
                pane_text = await tm.capture_pane(w.window_id)
                if pane_text:
                    extracted = extract_interactive_content(pane_text)
                    if extracted:
                        ui_name = extracted.ui.value
                        text = extracted.content
                    else:
                        logger.debug(
                            "No interactive UI detected in window_id %s "
                            "(last 3 lines: %s)",
                            window_id,
                            pane_text.strip().split("\n")[-3:],
                        )
                else:
                    logger.debug("No pane text captured for window_id %s", window_id)

        # Fallback: render directly from JSONL tool_use args. Only fires
        # when pane capture produced nothing AND the caller passed args
        # for one of the prompt-only tools.
        if ui_name is None and tool_name in PROMPT_TOOL_NAMES and tool_use_args:
            rendered = _render_from_tool_args(tool_name, tool_use_args)
            if rendered is not None:
                ui_name, text = rendered
                logger.info(
                    "handle_interactive_ui: pane capture empty for window_id %s, "
                    "rendered %s from tool_use args",
                    window_id,
                    tool_name,
                )

        if ui_name is None or text is None:
            return False

    # Build message with navigation keyboard.
    keyboard = _build_interactive_keyboard(window_id, ui_name=ui_name)

    # The extracted content already carries the tool-preview block from
    # the pane (Claude renders `<Tool name>\n<Tool call>\n\nDo you want
    # to proceed?` as a single region, which the parser's walkback
    # captures). No JSONL lookup needed — see drop-tool-context.
    rendered_text = _render_mdv2(_format_blocked_content(text))

    thread_kwargs: dict[str, int] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    existing_msg_id = get_interactive_msg_id(user_id, thread_id)
    if existing_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_msg_id,
                text=rendered_text,
                reply_markup=keyboard,
                parse_mode=PARSE_MODE,
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
            text=rendered_text,
            reply_markup=keyboard,
            parse_mode=PARSE_MODE,
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
