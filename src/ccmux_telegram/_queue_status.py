"""Status-task processing for the per-topic message queue.

Internal to `message_queue`. Do not import from outside this family.

Handles:
  - `_process_status_update_task`: edit/send status messages, dedupe
  - `_do_send_status_message` / `_do_clear_status_message`: raw send + delete
  - `_check_and_send_status`: poll terminal after content delivery
"""

from __future__ import annotations

import logging

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import RetryAfter

from ccmux.api import parse_status_line, tmux_registry
from . import message_queue as _mq
from .sender import (
    NO_LINK_PREVIEW,
    PARSE_MODE,
    send_with_fallback,
)

logger = logging.getLogger(__name__)


async def _process_status_update_task(
    bot: Bot, user_id: int, task: _mq.MessageTask
) -> None:
    """Process a status update task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = task.chat_id
    skey = (user_id, tid)
    status_text = task.text or ""

    if not status_text:
        # No status text means clear status
        await _do_clear_status_message(bot, user_id, tid, chat_id=chat_id)
        return

    current_info = _mq._status_msg_info.get(skey)

    if current_info:
        msg_id, stored_wid, last_text = current_info

        if stored_wid != wid:
            # Window changed - delete old and send new
            await _do_clear_status_message(bot, user_id, tid, chat_id=chat_id)
            await _do_send_status_message(
                bot, user_id, tid, wid, status_text, chat_id=chat_id
            )
        elif status_text == last_text:
            # Same content, skip edit
            return
        else:
            # Same window, text changed - edit in place
            # Send typing indicator when Claude is working
            if "esc to interrupt" in status_text.lower():
                try:
                    await bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                except RetryAfter:
                    raise
                except Exception:
                    pass
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_mq._ensure_formatted(status_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                _mq._status_msg_info[skey] = (msg_id, wid, status_text)
            except RetryAfter:
                raise
            except Exception as e:
                # Telegram returns "Message is not modified" when the edit's
                # rendered content is byte-identical to what's already on
                # screen (e.g. when two poll ticks produce the same status
                # text). Treat as a successful no-op: cache the text so the
                # next tick's `status_text == last_text` shortcut catches it
                # and never call send/delete again. Falling through to the
                # plain-text retry or to `_do_send_status_message` would
                # either hit the same error or, worse, drop the old message
                # and post a duplicate.
                if "Message is not modified" in str(e):
                    _mq._status_msg_info[skey] = (msg_id, wid, status_text)
                    return
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=status_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    _mq._status_msg_info[skey] = (msg_id, wid, status_text)
                except RetryAfter:
                    raise
                except Exception as e2:
                    if "Message is not modified" in str(e2):
                        _mq._status_msg_info[skey] = (msg_id, wid, status_text)
                        return
                    logger.debug(f"Failed to edit status message: {e2}")
                    _mq._status_msg_info.pop(skey, None)
                    await _do_send_status_message(
                        bot, user_id, tid, wid, status_text, chat_id=chat_id
                    )
    else:
        # No existing status message, send new
        await _do_send_status_message(
            bot, user_id, tid, wid, status_text, chat_id=chat_id
        )


async def _do_send_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
    chat_id: int,
) -> None:
    """Send a new status message and track it (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    # Safety net: delete any orphaned status message before sending a new one.
    # This catches edge cases where tracking was cleared without deleting the message.
    old = _mq._status_msg_info.pop(skey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass
    # Send typing indicator when Claude is working
    if "esc to interrupt" in text.lower():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except RetryAfter:
            raise
        except Exception:
            pass
    sent = await send_with_fallback(
        bot,
        chat_id,
        text,
        **_mq._send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    if sent:
        _mq._status_msg_info[skey] = (sent.message_id, window_id, text)


async def _do_clear_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    chat_id: int,
) -> None:
    """Delete the status message for a user (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    info = _mq._status_msg_info.pop(skey, None)
    if info:
        msg_id = info[0]
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Failed to delete status message {msg_id}: {e}")


async def _check_and_send_status(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    chat_id: int,
) -> None:
    """Check terminal for status line and send status message if present."""
    # Skip if there are more messages pending in the queue
    queue = _mq._message_queues.get((user_id, thread_id or 0))
    if queue and not queue.empty():
        return
    tm = tmux_registry.get_by_window_id(window_id)
    if not tm:
        return
    w = await tm.find_window_by_id(window_id)
    if not w:
        return

    pane_text = await tm.capture_pane(w.window_id)
    if not pane_text:
        return

    tid = thread_id or 0
    status_line = parse_status_line(pane_text)
    if status_line:
        await _do_send_status_message(
            bot, user_id, tid, window_id, status_line, chat_id=chat_id
        )
