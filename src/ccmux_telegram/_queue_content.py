"""Content-task processing for the per-topic message queue.

Internal to `message_queue`. Do not import from outside this family.

Handles:
  - `_process_content_task`: send/edit merged content, pair tool_use/tool_result
  - `_send_task_images`: deliver images attached to a task
  - `_convert_status_to_content`: edit a live status message into first content part
"""

from __future__ import annotations

import logging

from telegram import Bot
from telegram.error import RetryAfter

from . import message_queue as _mq
from ._queue_status import _check_and_send_status, _do_clear_status_message
from .sender import (
    NO_LINK_PREVIEW,
    PARSE_MODE,
    send_photo,
    send_with_fallback,
)

logger = logging.getLogger(__name__)


async def _send_task_images(bot: Bot, chat_id: int, task: _mq.MessageTask) -> None:
    """Send images attached to a task, if any."""
    if not task.image_data:
        return
    logger.info(
        "Sending %d image(s) in thread %s",
        len(task.image_data),
        task.thread_id,
    )
    await send_photo(
        bot,
        chat_id,
        task.image_data,
        **_mq._send_kwargs(task.thread_id),  # type: ignore[arg-type]
    )


async def _process_content_task(bot: Bot, user_id: int, task: _mq.MessageTask) -> None:
    """Process a content message task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = task.chat_id

    # 1. Handle tool_result editing (merged parts are edited together)
    if task.content_type == "tool_result" and task.tool_use_id:
        _tkey = (task.tool_use_id, user_id, tid)
        edit_msg_id = _mq._tool_msg_ids.pop(_tkey, None)
        if edit_msg_id is not None:
            # Clear status message first
            await _do_clear_status_message(bot, user_id, tid, chat_id=chat_id)
            # Join all parts for editing (merged content goes together)
            full_text = "\n\n".join(task.parts)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_msg_id,
                    text=_mq._ensure_formatted(full_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                await _send_task_images(bot, chat_id, task)
                await _check_and_send_status(
                    bot, user_id, wid, task.thread_id, chat_id=chat_id
                )
                return
            except RetryAfter:
                raise
            except Exception:
                try:
                    # Fallback: plain Markdown (backend already emits
                    # human-readable `> ` blockquotes, no post-processing
                    # needed).
                    plain_text = task.text or full_text
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=edit_msg_id,
                        text=plain_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    await _send_task_images(bot, chat_id, task)
                    await _check_and_send_status(
                        bot, user_id, wid, task.thread_id, chat_id=chat_id
                    )
                    return
                except RetryAfter:
                    raise
                except Exception:
                    logger.debug(f"Failed to edit tool msg {edit_msg_id}, sending new")
                    # Fall through to send as new message

    # 2. Send content messages, converting status message to first content part
    first_part = True
    last_msg_id: int | None = None
    for part in task.parts:
        sent = None

        # For first part, try to convert status message to content (edit instead of delete)
        if first_part:
            first_part = False
            converted_msg_id = await _convert_status_to_content(
                bot,
                user_id,
                tid,
                wid,
                part,
                chat_id=chat_id,
            )
            if converted_msg_id is not None:
                last_msg_id = converted_msg_id
                continue

        sent = await send_with_fallback(
            bot,
            chat_id,
            part,
            **_mq._send_kwargs(task.thread_id),  # type: ignore[arg-type]
        )

        if sent:
            last_msg_id = sent.message_id

    # 3. Record tool_use message ID for later editing
    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _mq._tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id

    # 4. Record last content message_id for watcher deep-links
    if last_msg_id is not None and task.thread_id is not None:
        _mq.record_last_content_msg(user_id, task.thread_id, last_msg_id)

    # 4. Send images if present (from tool_result with base64 image blocks)
    await _send_task_images(bot, chat_id, task)

    # 5. After content, check and send status
    await _check_and_send_status(bot, user_id, wid, task.thread_id, chat_id=chat_id)


async def _convert_status_to_content(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    content_text: str,
    chat_id: int,
) -> int | None:
    """Convert status message to content message by editing it.

    Returns the message_id if converted successfully, None otherwise.
    """
    skey = (user_id, thread_id_or_0)
    info = _mq._status_msg_info.pop(skey, None)
    if not info:
        return None

    msg_id, stored_wid, _ = info
    if stored_wid != window_id:
        # Different window, just delete the old status
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        return None

    # Edit status message to show content
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=_mq._ensure_formatted(content_text),
            parse_mode=PARSE_MODE,
            link_preview_options=NO_LINK_PREVIEW,
        )
        return msg_id
    except RetryAfter:
        raise
    except Exception:
        try:
            # Fallback to plain Markdown (backend emits human-readable
            # `> ` blockquotes; no post-processing needed).
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=content_text,
                link_preview_options=NO_LINK_PREVIEW,
            )
            return msg_id
        except RetryAfter:
            raise
        except Exception as e:
            logger.debug(f"Failed to convert status to content: {e}")
            # Message might be deleted or too old, caller will send new message
            return None
