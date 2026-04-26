"""New-topic binding flow — pickers and helpers for first-time binding.

Handles the flow of binding a Telegram topic to a tmux window via the
picker UI (initial tmux session select, window select, permission choice,
directory browse):

  - Tmux session picker callbacks (select/new/cancel)
  - Tmux window picker callbacks (bind/new/cancel)
  - Permission picker callbacks (normal/skip)
  - Directory browser callbacks (select/up/confirm/cancel/page)
  - _show_permission_picker: Transition to permission selection
  - _create_session_and_bind: Create window and bind to topic
  - _proceed_with_session: Shared continuation (also used by binding_steal)

Topic lifecycle events (close/rename) live in `binding_lifecycle.py`.
Steal-specific callbacks live in `binding_steal.py`.
"""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ccmux.api import tmux_registry, sanitize_session_name
from .claude_trust import mark_dir_trusted
from .runtime import (
    iter_topics_joined,
    topics as _topics,
    windows as _windows,
)
from .util import (
    has_window_binding,
)
from .picker import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    PENDING_THREAD_ID_KEY,
    PENDING_THREAD_TEXT_KEY,
    SELECTED_PATH_KEY,
    SESSION_NAME_KEY,
    STATE_AWAITING_SESSION_NAME,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_PERMISSION,
    STATE_SELECTING_TMUX_SESSION,
    STATE_SELECTING_TMUX_WINDOW,
    TMUX_FILTER_MODE_KEY,
    TMUX_SESSIONS_KEY,
    TMUX_WINDOWS_KEY,
    build_directory_browser,
    build_permission_picker,
    build_tmux_session_picker,
    build_window_picker,
    clear_browse_state,
    clear_permission_picker_state,
    clear_tmux_session_picker_state,
    clear_tmux_window_picker_state,
)
from .sender import safe_edit, safe_reply, safe_send

logger = logging.getLogger(__name__)


async def _rename_topic_to_session(
    bot: Bot, chat_id: int, thread_id: int, session_name: str
) -> None:
    """Rename a forum topic to match its bound session name.

    Telegram's Bot API has no getter for a topic's current name, so we
    cannot pre-check whether an edit is needed. When the topic already
    has ``session_name``, the API returns ``BadRequest: Topic_not_modified``
    -- a no-op, not a failure. Silence that specific response; keep the
    warning for real failures (missing permissions, topic deleted, ...).
    """
    try:
        await bot.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            name=session_name,
        )
    except BadRequest as e:
        # Telegram spells this as ``Topic_not_modified`` (underscore) for
        # forum topics, but ``Message is not modified`` (space) for chat
        # messages. Normalize so either form hits the silent branch.
        msg_norm = str(e).lower().replace("_", " ")
        if "not modified" in msg_norm:
            return
        logger.warning(
            "Failed to rename topic to %r (chat=%s thread=%s): %s",
            session_name,
            chat_id,
            thread_id,
            e,
        )
    except Exception as e:
        logger.warning(
            "Failed to rename topic to %r (chat=%s thread=%s): %s",
            session_name,
            chat_id,
            thread_id,
            e,
        )


# State-to-clear-function mapping for stale picker cleanup
_STATE_CLEAR_MAP: dict[str, Callable[[dict | None], None]] = {
    STATE_SELECTING_TMUX_SESSION: clear_tmux_session_picker_state,
    STATE_SELECTING_TMUX_WINDOW: clear_tmux_window_picker_state,
    STATE_SELECTING_PERMISSION: clear_permission_picker_state,
    STATE_BROWSING_DIRECTORY: clear_browse_state,
}

# User-facing messages for each picker state
_STATE_MESSAGES: dict[str, str] = {
    STATE_SELECTING_TMUX_SESSION: "Please use the session picker above, or tap Cancel.",
    STATE_SELECTING_TMUX_WINDOW: "Please use the window picker above, or tap Cancel.",
    STATE_SELECTING_PERMISSION: "Please use the permission picker above, or tap Cancel.",
    STATE_BROWSING_DIRECTORY: "Please use the directory browser above, or tap Cancel.",
}


async def handle_text_in_picker_state(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    thread_id: int | None,
) -> bool:
    """Handle text input when a picker UI is active.

    Returns True if the text was consumed (caller should return early).
    Returns False if no picker state is active or stale state was cleared.

    Handles:
      - SESSION_NAME input: processes the name, transitions to permission picker
      - Other picker states: replies with "use the picker" message
      - Stale state from a different thread: clears it and returns False
    """
    if not context.user_data:
        return False

    state = context.user_data.get(STATE_KEY)
    if state is None:
        return False

    pending_tid = context.user_data.get(PENDING_THREAD_ID_KEY)

    # Special case: session name input
    if state == STATE_AWAITING_SESSION_NAME:
        if pending_tid == thread_id:
            text = message.text or ""
            existing_names = tmux_registry.all_server_session_names()
            sname = sanitize_session_name(text, existing_names)
            selected_path = context.user_data.get(SELECTED_PATH_KEY, str(Path.cwd()))
            context.user_data.pop(STATE_KEY, None)

            text_msg, keyboard = build_permission_picker()
            context.user_data[STATE_KEY] = STATE_SELECTING_PERMISSION
            context.user_data[SESSION_NAME_KEY] = sname
            context.user_data[SELECTED_PATH_KEY] = selected_path
            if sname != text:
                await safe_reply(message, f"Session name sanitized to `{sname}`")
            await safe_reply(message, text_msg, reply_markup=keyboard)
            return True
        # Stale state from a different thread
        context.user_data.pop(STATE_KEY, None)
        context.user_data.pop(PENDING_THREAD_ID_KEY, None)
        context.user_data.pop(PENDING_THREAD_TEXT_KEY, None)
        return False

    # Generic picker states: ignore text for same thread, clear stale for different
    if pending_tid == thread_id:
        hint = _STATE_MESSAGES.get(state, "Please use the picker above, or tap Cancel.")
        await safe_reply(message, hint)
        return True

    # Stale picker state from a different thread — clear it
    clear_fn = _STATE_CLEAR_MAP.get(state)
    if clear_fn:
        clear_fn(context.user_data)
    else:
        context.user_data.pop(STATE_KEY, None)
    context.user_data.pop(PENDING_THREAD_ID_KEY, None)
    context.user_data.pop(PENDING_THREAD_TEXT_KEY, None)
    return False


def _build_picker_for_filter(
    filter_mode: str,
) -> tuple[str, InlineKeyboardMarkup, list[str], dict[str, int]]:
    """Build picker text, keyboard, session list, and bound_map for a filter tab.

    Args:
        filter_mode: "all", "unbound", or "bound".

    Returns:
        (text, keyboard, session_list, bound_map) where bound_map maps
        session_name -> thread_id for all currently bound sessions.
    """
    all_sessions = tmux_registry.all_server_session_names()
    bound_names = _topics.all_session_names()

    # Build bound_map: session_name -> thread_id (first binding found)
    bound_map: dict[str, int] = {}
    for b in iter_topics_joined():
        if b.session_name in all_sessions and b.session_name not in bound_map:
            bound_map[b.session_name] = b.thread_id

    if filter_mode == "all":
        sessions = sorted(all_sessions)
    elif filter_mode == "bound":
        sessions = sorted(s for s in all_sessions if s in bound_names)
    else:  # unbound (default)
        sessions = sorted(s for s in all_sessions if s not in bound_names)

    text, kb, session_list = build_tmux_session_picker(sessions, filter_mode, bound_map)
    return text, kb, session_list, bound_map


async def handle_unbound_topic(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    thread_id: int,
    text: str,
) -> None:
    """Handle first message in an unbound topic — show tmux session picker."""
    filter_mode = "unbound"
    msg_text, keyboard, session_list, _ = _build_picker_for_filter(filter_mode)
    logger.debug(
        "Session picker check: filter=%s sessions=%s",
        filter_mode,
        session_list,
    )

    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_SELECTING_TMUX_SESSION
        context.user_data[TMUX_SESSIONS_KEY] = session_list
        context.user_data[TMUX_FILTER_MODE_KEY] = filter_mode
        context.user_data[PENDING_THREAD_ID_KEY] = thread_id
        context.user_data[PENDING_THREAD_TEXT_KEY] = text
    logger.info(
        "Unbound topic: showing tmux session picker (%d sessions, user=%d, thread=%d)",
        len(session_list),
        user.id,
        thread_id,
    )
    assert update.message is not None
    await safe_reply(update.message, msg_text, reply_markup=keyboard)


async def _show_permission_picker(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    selected_path: str,
    session_name: str,
) -> None:
    """Show permission picker before creating a session.

    Stores selected_path and session_name in user_data, then transitions
    to STATE_SELECTING_PERMISSION.
    """
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_SELECTING_PERMISSION
        context.user_data[SELECTED_PATH_KEY] = selected_path
        context.user_data[SESSION_NAME_KEY] = session_name

    text, keyboard = build_permission_picker()
    await safe_edit(query, text, reply_markup=keyboard)
    await query.answer()


async def _create_session_and_bind(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    selected_path: str,
    session_name: str,
    pending_thread_id: int | None,
    skip_permissions: bool = False,
) -> None:
    """Create a tmux session+window and bind it to a topic.

    Called from CB_PERM_NORMAL and CB_PERM_SKIP handlers after permission choice,
    or from CB_TMUX_WIN_NEW for new windows in existing sessions.
    """
    tm = tmux_registry.get_or_create(session_name)

    # Claude Code gates new directories behind a "Trust this folder?"
    # dialog. Answering that dialog is a prerequisite for SessionStart
    # hooks to fire; without the hook, window_bindings never gets the
    # new window and the user sees "has no window yet" on send. The
    # picker flow is explicit user consent for this directory, so
    # pre-mark it as trusted. Best-effort: if this fails, Claude will
    # show its own dialog as before.
    mark_dir_trusted(selected_path)

    # Check if session already exists (binding to existing session)
    existing_session = tm.get_session()
    if existing_session:
        # Create a new window in the existing session
        success, message, created_wname, created_wid = await tm.create_window(
            selected_path,
        )
    else:
        # Create a brand-new tmux session
        success, message, created_wname, created_wid = await tm.create_session(
            selected_path,
            skip_permissions=skip_permissions,
        )

    if success:
        tmux_registry.update_window_map(created_wid, session_name)
        logger.info(
            "Session created: %s window=%s (id=%s) at %s (user=%d, thread=%s)",
            session_name,
            created_wname,
            created_wid,
            selected_path,
            user.id,
            pending_thread_id,
        )
        # Wait for Claude Code SessionStart hook to populate window_bindings.
        hook_timeout = 5.0
        poll_interval = 0.5
        hook_ok = False

        for _ in range(int(hook_timeout / poll_interval)):
            await _windows.load()
            if _windows.contains(session_name):
                hook_ok = True
                break
            await asyncio.sleep(poll_interval)

        if not hook_ok:
            logger.warning(
                "Hook timed out for window %s (session '%s'); monitor may "
                "not route messages until hook fires",
                created_wid,
                session_name,
            )

        if pending_thread_id is not None:
            # Thread bind flow: bind thread to newly created session
            chat = query.message.chat if query.message else None
            if chat is None:
                await safe_edit(query, "❌ No chat context for binding.")
                return
            _topics.bind(
                user.id,
                pending_thread_id,
                session_name=session_name,
                group_chat_id=chat.id,
            )

            await _rename_topic_to_session(
                context.bot, chat.id, pending_thread_id, session_name
            )

            await safe_edit(
                query,
                f"✅ {message}\n\nCreated. Send messages here.",
            )

            # Clean up pending state (text is intentionally not forwarded)
            if context.user_data is not None:
                context.user_data.pop(PENDING_THREAD_TEXT_KEY, None)
                context.user_data.pop(PENDING_THREAD_ID_KEY, None)
        else:
            # Should not happen in topic-only mode, but handle gracefully
            await safe_edit(query, f"✅ {message}")
    else:
        await safe_edit(query, f"❌ {message}")
        if pending_thread_id is not None and context.user_data is not None:
            context.user_data.pop(PENDING_THREAD_ID_KEY, None)
            context.user_data.pop(PENDING_THREAD_TEXT_KEY, None)
    await query.answer("Created" if success else "Failed")


# --- Callback handlers ---


async def _proceed_with_session(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user: User,
    session_name: str,
) -> None:
    """Continue binding flow after a session has been selected (or stolen).

    Inspects the windows in `session_name` and either binds directly (single
    window), shows the window picker (multiple windows), or shows the
    directory browser (no windows yet).

    This is shared by normal session-select and steal-confirm flows.
    """
    tm = tmux_registry.get_or_create(session_name)
    windows = await tm.list_windows()

    if len(windows) == 1:
        # Single window — bind directly
        w = windows[0]
        raw_tid = (
            getattr(query.message, "message_thread_id", None) if query.message else None
        )
        thread_id = None if (raw_tid is None or raw_tid == 1) else raw_tid
        if thread_id is None:
            await query.answer("Not in a topic", show_alert=True)
            return

        tmux_registry.update_window_map(w.window_id, session_name)
        chat = query.message.chat if query.message else None
        if chat is None:
            await query.answer("No chat context for binding", show_alert=True)
            return
        _topics.bind(
            user.id,
            thread_id,
            session_name=session_name,
            group_chat_id=chat.id,
        )

        await _rename_topic_to_session(context.bot, chat.id, thread_id, session_name)

        await safe_edit(query, f"✅ Bound to `{session_name}` ({w.window_id})")

        # Warn if session_map has no entry (Claude hook hasn't fired)
        if not has_window_binding(session_name):
            await safe_send(
                context.bot,
                chat.id,
                "⚠️ This window is not yet registered in session\\_map\\. "
                "Messages from Claude won't be delivered until the hook fires\\.\n"
                "Run `/clear` in the Claude window to re\\-register\\.",
                message_thread_id=thread_id,
            )

        # Clean up pending state (text is intentionally not forwarded)
        if context.user_data is not None:
            context.user_data.pop(PENDING_THREAD_TEXT_KEY, None)
            context.user_data.pop(PENDING_THREAD_ID_KEY, None)
        await query.answer("Bound")

    elif len(windows) > 1:
        # Multiple windows — show window picker.
        # window_name isn't tracked; label with window_id which is the
        # canonical identity (e.g. `@5`).
        win_tuples = [(w.window_id, w.window_id, w.cwd) for w in windows]
        msg_text, keyboard, win_ids = build_window_picker(win_tuples, session_name)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_SELECTING_TMUX_WINDOW
            context.user_data[TMUX_WINDOWS_KEY] = win_ids
            context.user_data[SESSION_NAME_KEY] = session_name
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    else:
        # No windows (only __main__ placeholder) — offer to create new window
        if context.user_data is not None:
            context.user_data[SESSION_NAME_KEY] = session_name
        start_path = str(Path.cwd())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()
