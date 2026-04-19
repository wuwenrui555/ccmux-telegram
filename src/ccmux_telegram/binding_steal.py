"""Steal flow — force rebind a session already owned by another topic.

Callback handlers for the steal UI presented when the user picks a tmux
session that is currently bound to a different topic:

  - handle_steal_select: show the steal confirmation prompt
  - handle_steal_confirm: unbind previous owner and bind to current topic
  - handle_steal_cancel: return to the session picker
"""

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from .runtime import (
    get_topic_by_session_name,
    topics as _topics,
)
from .callback_data import (
    CB_TMUX_STEAL,
    CB_TMUX_STEAL_CANCEL,
    CB_TMUX_STEAL_CONFIRM,
)
from .picker import (
    STEAL_SESSION_NAME_KEY,
    TMUX_FILTER_MODE_KEY,
    TMUX_SESSIONS_KEY,
    clear_tmux_session_picker_state,
)
from .sender import safe_edit
from .binding_flow import _build_picker_for_filter, _proceed_with_session

logger = logging.getLogger(__name__)


async def handle_steal_select(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle click on a bound session in the picker — show steal confirmation."""
    query = update.callback_query
    assert query and query.data
    try:
        index = int(query.data[len(CB_TMUX_STEAL) :])
    except ValueError:
        await query.answer("Invalid data")
        return
    if context.user_data is None:
        await query.answer()
        return

    sessions: list[str] = context.user_data.get(TMUX_SESSIONS_KEY, [])
    if index < 0 or index >= len(sessions):
        await query.answer("Invalid selection", show_alert=True)
        return

    session_name = sessions[index]
    existing = get_topic_by_session_name(session_name)
    if not existing:
        # Race: session no longer bound — fall through to normal select
        clear_tmux_session_picker_state(context.user_data)
        await _proceed_with_session(query, context, query.from_user, session_name)
        return

    context.user_data[STEAL_SESSION_NAME_KEY] = session_name

    text = (
        f"⚠️ Session `{session_name}` is already bound to topic `{existing.thread_id}`\\.\n"
        f"Steal it and bind to this topic instead?"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Steal", callback_data=CB_TMUX_STEAL_CONFIRM),
                InlineKeyboardButton("❌ Cancel", callback_data=CB_TMUX_STEAL_CANCEL),
            ]
        ]
    )
    await safe_edit(query, text, reply_markup=kb)
    await query.answer()


async def handle_steal_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Steal confirmed — unbind old topic and bind session to the current topic."""
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    if context.user_data is None:
        await query.answer()
        return

    session_name = context.user_data.pop(STEAL_SESSION_NAME_KEY, None)
    if not session_name:
        await query.answer("No session selected", show_alert=True)
        return

    existing = get_topic_by_session_name(session_name)
    if existing:
        _topics.unbind(existing.user_id, existing.thread_id)
        logger.info(
            "Steal: unbound previous owner user=%d thread=%d from session=%s",
            existing.user_id,
            existing.thread_id,
            session_name,
        )

    clear_tmux_session_picker_state(context.user_data)
    await _proceed_with_session(query, context, user, session_name)


async def handle_steal_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Steal cancelled — go back to the session picker."""
    query = update.callback_query
    if query is None:
        return
    if context.user_data is None:
        await query.answer()
        return

    context.user_data.pop(STEAL_SESSION_NAME_KEY, None)
    filter_mode = context.user_data.get(TMUX_FILTER_MODE_KEY, "unbound")
    text, kb, session_list, _ = _build_picker_for_filter(filter_mode)
    context.user_data[TMUX_SESSIONS_KEY] = session_list
    await safe_edit(query, text, reply_markup=kb)
    await query.answer("Cancelled")
