"""Topic lifecycle — close / rename handlers + shared state cleanup.

Covers Telegram forum-topic lifecycle events and the reusable state-cleanup
helper used whenever a topic goes away or changes:

  - topic_closed_handler: clear in-memory state on topic close (binding kept).
  - topic_edited_handler: sync a renamed topic to its tmux window.
  - clear_topic_state: reusable cleanup for status/tool/interactive state
    and user_data pending fields; called by unbind/close/stale-cleanup paths.
"""

import logging
from typing import Any

from telegram import Bot, Update
from telegram.ext import ContextTypes

from .runtime import get_topic
from .picker import PENDING_THREAD_ID_KEY, PENDING_THREAD_TEXT_KEY
from .util import authorized, get_thread_id, get_tm_and_window
from .message_queue import clear_status_msg_info, clear_tool_msg_ids_for_topic
from .prompt import clear_interactive_msg

logger = logging.getLogger(__name__)


async def clear_topic_state(
    user_id: int,
    thread_id: int,
    bot: Bot | None = None,
    user_data: dict[str, Any] | None = None,
    chat_id: int | None = None,
) -> None:
    """Clear all memory state associated with a topic.

    This should be called when:
      - A topic is closed or deleted
      - A thread binding becomes stale (window deleted externally)

    Cleans up:
      - _status_msg_info (status message tracking)
      - _tool_msg_ids (tool_use → message_id mapping)
      - _interactive_msgs and _interactive_mode (interactive UI state)
      - user_data pending state (_pending_thread_id, _pending_thread_text)
    """
    # Clear status message tracking
    clear_status_msg_info(user_id, thread_id)

    # Clear tool message ID tracking
    clear_tool_msg_ids_for_topic(user_id, thread_id)

    # Clear interactive UI state (also deletes message from chat)
    await clear_interactive_msg(user_id, bot, thread_id, chat_id=chat_id)

    # Clear pending thread state from user_data
    if user_data is not None:
        if user_data.get(PENDING_THREAD_ID_KEY) == thread_id:
            user_data.pop(PENDING_THREAD_ID_KEY, None)
            user_data.pop(PENDING_THREAD_TEXT_KEY, None)


@authorized()
async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure — clear in-memory state only; binding is preserved."""
    user = update.effective_user
    assert user

    thread_id = get_thread_id(update)
    if thread_id is None:
        return

    topic = get_topic(user.id, thread_id)
    if topic:
        logger.info(
            "Topic closed: clearing state for session '%s' (user=%d, thread=%d)",
            topic.session_name,
            user.id,
            thread_id,
        )
        # Clear all memory state for this topic; binding is intentionally kept
        await clear_topic_state(
            user.id,
            thread_id,
            context.bot,
            context.user_data,
            chat_id=topic.group_chat_id,
        )
    else:
        logger.debug(
            "Topic closed: no binding (user=%d, thread=%d)", user.id, thread_id
        )


@authorized()
async def topic_edited_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic rename — sync new name to tmux window and internal state."""
    user = update.effective_user
    assert user

    msg = update.message
    if not msg or not msg.forum_topic_edited:
        return

    new_name = msg.forum_topic_edited.name
    if new_name is None:
        # Icon-only change, no rename needed
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        return

    topic = get_topic(user.id, thread_id)
    if not topic or not topic.window_id:
        logger.debug(
            "Topic edited: no binding (user=%d, thread=%d)", user.id, thread_id
        )
        return

    wid = topic.window_id
    pair = await get_tm_and_window(wid)
    if pair:
        tm, _w = pair
        await tm.rename_window(wid, new_name)
    logger.info(
        "Topic renamed: '%s' -> tmux window '%s' (session='%s', user=%d, thread=%d)",
        new_name,
        wid,
        topic.session_name,
        user.id,
        thread_id,
    )
