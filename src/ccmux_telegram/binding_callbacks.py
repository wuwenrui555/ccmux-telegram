"""Telegram callback-query handlers for the binding picker UIs.

Split from `binding_flow.py` so the file that owns the binding state
machine (handle_unbound_topic, _create_session_and_bind, etc.) doesn't
also have to host every UI button handler that fires into it.

Each handler here is registered with PTB in `bot.py` against a callback
data prefix and dispatches into helpers in `binding_flow`.
"""

import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from ccmux.api import tmux_registry
from .runtime import topics as _topics
from .util import get_thread_id, get_tm_and_window, has_window_binding
from .callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_PERM_SKIP,
    CB_TMUX_SESSION_CANCEL,
    CB_TMUX_SESSION_NEW,
    CB_TMUX_SESSION_SELECT,
    CB_TMUX_WIN_BIND,
    CB_TMUX_WIN_CANCEL,
    CB_TMUX_WIN_NEW,
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
    TMUX_FILTER_MODE_KEY,
    TMUX_SESSIONS_KEY,
    TMUX_WINDOWS_KEY,
    build_directory_browser,
    clear_browse_state,
    clear_permission_picker_state,
    clear_tmux_session_picker_state,
    clear_tmux_window_picker_state,
)
from .sender import safe_edit, safe_send
from .binding_flow import (
    _build_picker_for_filter,
    _create_session_and_bind,
    _proceed_with_session,
)

logger = logging.getLogger(__name__)


async def handle_filter_switch(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle filter tab click — re-render the session picker with the new filter."""
    query = update.callback_query
    assert query and query.data
    if context.user_data is None:
        await query.answer()
        return
    filter_mode = query.data.split(":")[-1]  # "all" / "unbound" / "bound"
    context.user_data[TMUX_FILTER_MODE_KEY] = filter_mode
    text, kb, session_list, _ = _build_picker_for_filter(filter_mode)
    context.user_data[TMUX_SESSIONS_KEY] = session_list
    await safe_edit(query, text, reply_markup=kb)
    await query.answer()


async def handle_tmux_session_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle tmux session picker callbacks (select/new/cancel)."""
    query = update.callback_query
    assert query and query.data
    user = update.effective_user
    assert user
    data = query.data

    if data.startswith(CB_TMUX_SESSION_SELECT):
        pending_tid = (
            context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
        )
        if pending_tid is not None and get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        try:
            idx = int(data[len(CB_TMUX_SESSION_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_sessions: list[str] = (
            context.user_data.get(TMUX_SESSIONS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_sessions):
            await query.answer("Session list changed, please retry", show_alert=True)
            return
        session_name = cached_sessions[idx]

        # Race check: verify session is still unbound
        bound_names = _topics.all_session_names()
        if session_name in bound_names:
            await query.answer(
                f"Session '{session_name}' was just bound by another topic",
                show_alert=True,
            )
            return

        clear_tmux_session_picker_state(context.user_data)
        await _proceed_with_session(query, context, user, session_name)

    elif data == CB_TMUX_SESSION_NEW:
        pending_tid = (
            context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
        )
        if pending_tid is not None and get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        clear_tmux_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_AWAITING_SESSION_NAME
            context.user_data[SELECTED_PATH_KEY] = str(Path.home())
        await safe_edit(query, "Enter a name for the new tmux session:")
        await query.answer()

    elif data == CB_TMUX_SESSION_CANCEL:
        pending_tid = (
            context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
        )
        if pending_tid is not None and get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        clear_tmux_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop(PENDING_THREAD_ID_KEY, None)
            context.user_data.pop(PENDING_THREAD_TEXT_KEY, None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")


async def handle_window_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle tmux window picker callbacks (bind/new/cancel)."""
    query = update.callback_query
    assert query and query.data
    user = update.effective_user
    assert user
    data = query.data

    if data.startswith(CB_TMUX_WIN_BIND):
        pending_tid = (
            context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
        )
        if pending_tid is not None and get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        try:
            idx = int(data[len(CB_TMUX_WIN_BIND) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_win_ids: list[str] = (
            context.user_data.get(TMUX_WINDOWS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_win_ids):
            await query.answer("Window list changed, please retry", show_alert=True)
            return
        selected_wid = cached_win_ids[idx]
        session_name = (
            context.user_data.get(SESSION_NAME_KEY, "") if context.user_data else ""
        )

        # Verify window still exists
        pair = await get_tm_and_window(selected_wid)
        if not pair:
            await query.answer(
                f"Window '{selected_wid}' no longer exists", show_alert=True
            )
            return
        _tm, w = pair

        thread_id = get_thread_id(update)
        if thread_id is None:
            await query.answer("Not in a topic", show_alert=True)
            return

        display = w.window_id
        clear_tmux_window_picker_state(context.user_data)
        tmux_registry.update_window_map(selected_wid, session_name)
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

        # Rename the topic to match the session name
        try:
            await context.bot.edit_forum_topic(
                chat_id=chat.id,
                message_thread_id=thread_id,
                name=session_name,
            )
        except Exception as e:
            logger.warning(
                "Failed to rename topic to %r (chat=%s thread=%s): %s",
                session_name,
                chat.id,
                thread_id,
                e,
            )

        await safe_edit(
            query, f"✅ Bound to window `{display}` in session `{session_name}`"
        )

        # Warn if session_map has no entry (Claude hook hasn't fired)
        if not has_window_binding(selected_wid, session_name):
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
            context.user_data.pop(SESSION_NAME_KEY, None)
        await query.answer("Bound")

    elif data == CB_TMUX_WIN_NEW:
        pending_tid = (
            context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
        )
        if pending_tid is not None and get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        # Preserve pending thread info and session_name, clear picker state
        clear_tmux_window_picker_state(context.user_data)
        start_path = str(Path.cwd())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_TMUX_WIN_CANCEL:
        pending_tid = (
            context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
        )
        if pending_tid is not None and get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        clear_tmux_window_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop(PENDING_THREAD_ID_KEY, None)
            context.user_data.pop(PENDING_THREAD_TEXT_KEY, None)
            context.user_data.pop(SESSION_NAME_KEY, None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")


async def handle_permission_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle permission picker callbacks (normal/skip)."""
    query = update.callback_query
    assert query and query.data
    user = update.effective_user
    assert user
    data = query.data

    # Ack the callback up front. The creation flow below can take several
    # seconds (hook timeout wait, state writes); without an early ack the
    # Telegram client treats the button as pending and can redeliver the
    # callback, producing a duplicate handler invocation.
    await query.answer()

    # Atomic claim on the session name. The first delivery gets "test",
    # subsequent stale deliveries get None and bail silently so they
    # cannot overwrite the first handler's success UI.
    session_name = (
        context.user_data.pop(SESSION_NAME_KEY, None) if context.user_data else None
    )
    if not session_name:
        return

    pending_tid = (
        context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
    )
    if pending_tid is None:
        pending_tid = get_thread_id(update)
    selected_path = (
        context.user_data.pop(SELECTED_PATH_KEY, str(Path.cwd()))
        if context.user_data
        else str(Path.cwd())
    )
    clear_permission_picker_state(context.user_data)

    skip_permissions = data == CB_PERM_SKIP

    await _create_session_and_bind(
        query,
        context,
        user,
        selected_path,
        session_name,
        pending_tid,
        skip_permissions=skip_permissions,
    )


async def handle_directory_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle directory browser callbacks (select/up/confirm/cancel/page)."""
    query = update.callback_query
    assert query and query.data
    user = update.effective_user
    assert user
    data = query.data

    if data.startswith(CB_DIR_SELECT):
        # Validate: callback must come from the same topic that started browsing
        pending_tid = (
            context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
        )
        if pending_tid is not None and get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = (
            context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_dirs):
            await query.answer(
                "Directory list changed, please refresh", show_alert=True
            )
            return
        subdir_name = cached_dirs[idx]

        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        new_path = (Path(current_path) / subdir_name).resolve()

        if not new_path.exists() or not new_path.is_dir():
            await query.answer("Directory not found", show_alert=True)
            return

        new_path_str = str(new_path)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(new_path_str)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_UP:
        pending_tid = (
            context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
        )
        if pending_tid is not None and get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        current = Path(current_path).resolve()
        parent = current.parent
        # No restriction - allow navigating anywhere

        parent_path = str(parent)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(parent_path)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data.startswith(CB_DIR_PAGE):
        pending_tid = (
            context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
        )
        if pending_tid is not None and get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await query.answer("Invalid data")
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = pg

        msg_text, keyboard, subdirs = build_directory_browser(current_path, pg)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_CONFIRM:
        default_path = str(Path.cwd())
        selected_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        # Check if this was initiated from a thread bind flow
        pending_thread_id: int | None = (
            context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
        )

        # Validate: confirm button must come from the same topic that started browsing
        confirm_thread_id = get_thread_id(update)
        if pending_thread_id is not None and confirm_thread_id != pending_thread_id:
            clear_browse_state(context.user_data)
            if context.user_data is not None:
                context.user_data.pop(PENDING_THREAD_ID_KEY, None)
                context.user_data.pop(PENDING_THREAD_TEXT_KEY, None)
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return

        clear_browse_state(context.user_data)

        # Ask for session name
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_AWAITING_SESSION_NAME
            context.user_data[SELECTED_PATH_KEY] = selected_path
        await safe_edit(query, "Enter a name for the new tmux session:")

    elif data == CB_DIR_CANCEL:
        pending_tid = (
            context.user_data.get(PENDING_THREAD_ID_KEY) if context.user_data else None
        )
        if pending_tid is not None and get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop(PENDING_THREAD_ID_KEY, None)
            context.user_data.pop(PENDING_THREAD_TEXT_KEY, None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")
