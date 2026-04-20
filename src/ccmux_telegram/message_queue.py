"""Per-topic message queue facade — public API + shared state + queue getters.

Provides a queue-based message processing system that ensures:
  - Messages are sent in receive order (FIFO)
  - Status messages always follow content messages
  - Consecutive content messages can be merged for efficiency
  - Each topic (user_id, thread_id) has its own independent queue and worker

Rate limiting is handled globally by AIORateLimiter on the Application.
Flood control is tracked per chat_id (Telegram rate limits are per-chat).

Key components:
  - `MessageTask`: Dataclass representing a queued message task (with thread_id)
  - `get_or_create_queue`: Get or create queue and worker for a topic
  - `enqueue_content_message` / `enqueue_status_update`: Public producers
  - `shutdown_workers`: Stop all workers on bot shutdown
  - Module-level state dicts shared with `_queue_worker`, `_queue_content`,
    `_queue_status` — those internal modules import this module as `_mq` and
    access state via attribute lookup to avoid import-time cycles.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from telegram import Bot

from .markdown import convert_markdown

logger = logging.getLogger(__name__)


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


# Merge limit for content messages
MERGE_MAX_LENGTH = 3800  # Leave room for markdown conversion overhead


@dataclass
class MessageTask:
    """Message task for queue processing.

    `chat_id` is required — every task is routed to a specific Telegram chat,
    which is always resolvable from the owning TopicBinding.group_chat_id.
    """

    task_type: Literal["content", "status_update", "status_clear"]
    chat_id: int  # Telegram chat_id for sending (from TopicBinding.group_chat_id)
    text: str | None = None
    window_id: str | None = None
    # content type fields
    parts: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    content_type: str = "text"
    thread_id: int | None = None  # Telegram topic thread_id for targeted send
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images


# Per-topic message queues and worker tasks.
# Keyed by (user_id, thread_id) — each bound topic processes independently.
_message_queues: dict[tuple[int, int], asyncio.Queue[MessageTask]] = {}
_queue_workers: dict[tuple[int, int], asyncio.Task[None]] = {}
_queue_locks: dict[tuple[int, int], asyncio.Lock] = {}

# Map (tool_use_id, user_id, thread_id_or_0) -> telegram message_id
# for editing tool_use messages with results
_tool_msg_ids: dict[tuple[str, int, int], int] = {}

# Status message tracking: (user_id, thread_id_or_0) -> (message_id, window_id, last_text)
_status_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}

# Track the most recent content message_id the bot has sent into each bound
# topic. The watcher uses this to build deep-links that land the user near
# the latest activity rather than on the topic's creation message.
_last_content_msg_ids: dict[tuple[int, int], int] = {}


def record_last_content_msg(user_id: int, thread_id: int, message_id: int) -> None:
    """Update the last-content message_id for a (user, topic) pair."""
    _last_content_msg_ids[(user_id, thread_id)] = message_id


def get_last_content_msg(user_id: int, thread_id: int) -> int | None:
    """Return the most recent recorded content message_id, or None."""
    return _last_content_msg_ids.get((user_id, thread_id))


# Flood control: chat_id -> monotonic time when ban expires.
# Keyed by chat_id because Telegram rate limits are per-chat, not per-user.
_flood_until: dict[int, float] = {}

# Max seconds to wait for flood control before dropping tasks
FLOOD_CONTROL_MAX_WAIT = 10


def _send_kwargs(thread_id: int | None) -> dict[str, int]:
    """Build message_thread_id kwargs for bot.send_message()."""
    if thread_id is not None:
        return {"message_thread_id": thread_id}
    return {}


def get_message_queue(
    user_id: int, thread_id: int
) -> asyncio.Queue[MessageTask] | None:
    """Get the message queue for a specific topic (if exists)."""
    return _message_queues.get((user_id, thread_id))


def get_or_create_queue(
    bot: Bot, user_id: int, thread_id: int
) -> asyncio.Queue[MessageTask]:
    """Get or create message queue and worker for a specific topic."""
    # Imported lazily to avoid import cycle: _queue_worker imports this module.
    from ._queue_worker import _message_queue_worker

    key = (user_id, thread_id)
    if key not in _message_queues:
        _message_queues[key] = asyncio.Queue()
        _queue_locks[key] = asyncio.Lock()
        _queue_workers[key] = asyncio.create_task(
            _message_queue_worker(bot, user_id, thread_id)
        )
    return _message_queues[key]


async def enqueue_content_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    parts: list[str],
    chat_id: int,
    tool_use_id: str | None = None,
    content_type: str = "text",
    text: str | None = None,
    thread_id: int | None = None,
    image_data: list[tuple[str, bytes]] | None = None,
) -> None:
    """Enqueue a content message task."""
    logger.debug(
        "Enqueue content: user=%d, window_id=%s, content_type=%s",
        user_id,
        window_id,
        content_type,
    )
    queue = get_or_create_queue(bot, user_id, thread_id or 0)

    task = MessageTask(
        task_type="content",
        text=text,
        window_id=window_id,
        parts=parts,
        tool_use_id=tool_use_id,
        content_type=content_type,
        thread_id=thread_id,
        image_data=image_data,
        chat_id=chat_id,
    )
    queue.put_nowait(task)


async def enqueue_status_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    status_text: str | None,
    chat_id: int,
    thread_id: int | None = None,
) -> None:
    """Enqueue status update. Skipped if text unchanged or during flood control."""
    # Don't enqueue during flood control — they'd just be dropped
    flood_end = _flood_until.get(chat_id, 0)
    if flood_end > time.monotonic():
        return

    tid = thread_id or 0

    # Deduplicate: skip if text matches what's already displayed
    if status_text:
        skey = (user_id, tid)
        info = _status_msg_info.get(skey)
        if info and info[1] == window_id and info[2] == status_text:
            return

    queue = get_or_create_queue(bot, user_id, tid)

    if status_text:
        task = MessageTask(
            task_type="status_update",
            text=status_text,
            window_id=window_id,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        logger.debug(
            "Enqueue status_update: user=%d, window_id=%s, text=%r",
            user_id,
            window_id,
            status_text[:50],
        )
    else:
        task = MessageTask(
            task_type="status_clear", thread_id=thread_id, chat_id=chat_id
        )
        logger.debug(
            "Enqueue status_clear: user=%d, window_id=%s",
            user_id,
            window_id,
        )

    queue.put_nowait(task)


def clear_status_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear status message tracking for a user (and optionally a specific thread)."""
    skey = (user_id, thread_id or 0)
    _status_msg_info.pop(skey, None)


def clear_tool_msg_ids_for_topic(user_id: int, thread_id: int | None = None) -> None:
    """Clear tool message ID tracking for a specific topic.

    Removes all entries in `_tool_msg_ids` that match the given user and thread.
    """
    tid = thread_id or 0
    # Find and remove all matching keys
    keys_to_remove = [
        key for key in _tool_msg_ids if key[1] == user_id and key[2] == tid
    ]
    for key in keys_to_remove:
        _tool_msg_ids.pop(key, None)


async def shutdown_workers() -> None:
    """Stop all queue workers (called during bot shutdown)."""
    for _, worker in list(_queue_workers.items()):
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    _queue_workers.clear()
    _message_queues.clear()
    _queue_locks.clear()
    logger.info("Message queue workers stopped")
