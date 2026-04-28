"""Worker loop + merge logic for the per-topic message queue.

Internal to `message_queue`. Do not import from outside this family.

Handles:
  - `_message_queue_worker`: per-topic background task dispatching content /
    status tasks, with flood-control and RetryAfter handling
  - `_inspect_queue` / `_can_merge_tasks` / `_merge_content_tasks`: non-
    destructive dequeue + merge of consecutive mergeable content tasks
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot
from telegram.error import RetryAfter

from . import message_queue as _mq
from ._queue_content import _process_content_task
from ._queue_status import _do_clear_status_message, _process_status_update_task

logger = logging.getLogger(__name__)


def _inspect_queue(
    queue: asyncio.Queue[_mq.MessageTask],
) -> list[_mq.MessageTask]:
    """Non-destructively inspect all items in queue.

    Drains the queue and returns all items. Caller must refill.
    """
    items: list[_mq.MessageTask] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            items.append(item)
        except asyncio.QueueEmpty:
            break
    return items


def _can_merge_tasks(base: _mq.MessageTask, candidate: _mq.MessageTask) -> bool:
    """Check if two content tasks can be merged."""
    if base.window_id != candidate.window_id:
        return False
    if candidate.task_type != "content":
        return False
    # tool_use/tool_result break merge chain
    # - tool_use: will be edited later by tool_result
    # - tool_result: edits previous message, merging would cause order issues
    if base.content_type in ("tool_use", "tool_result"):
        return False
    if candidate.content_type in ("tool_use", "tool_result"):
        return False
    return True


async def _coalesce_status_updates(
    queue: asyncio.Queue[_mq.MessageTask],
    first: _mq.MessageTask,
    lock: asyncio.Lock,
) -> tuple[_mq.MessageTask, int]:
    """Drop stale status_update tasks for the same window.

    Telegram's per-chat rate limit (~1/sec, hardcoded inside PTB's
    ``AIORateLimiter``) means status updates can pile up faster than
    the worker can deliver them. Each ``Working`` status text edit
    that lands behind the actual response delays the response by
    one rate-limited slot. With ``StateMonitor.fast_tick`` running
    parallel (v4.0.1), status updates produce at ~2/sec for a
    thinking Claude — a backlog the user feels as latency.

    On dequeue of a ``status_update`` for window X, scan ahead for
    other ``status_update`` tasks for the same window. Replace
    ``first`` with the LATEST one and drop the intermediates. Other
    task types (content, status_clear, status_update for other
    windows) are preserved in their original order.

    Returns ``(latest_task, dropped_count)`` where ``dropped_count``
    is how many older status_update tasks for ``first.window_id``
    were discarded.
    """
    latest = first
    dropped = 0

    async with lock:
        items = _inspect_queue(queue)
        keep: list[_mq.MessageTask] = []
        for task in items:
            if task.task_type == "status_update" and task.window_id == first.window_id:
                # Newer status for the same window: replace and drop
                # the previous "latest" (it was already accounted for
                # by being either ``first`` or a prior coalesced item).
                latest = task
                dropped += 1
                continue
            keep.append(task)

        for item in keep:
            queue.put_nowait(item)
            # put_nowait re-counts; balance with task_done since the
            # item was counted on its original enqueue.
            queue.task_done()

    return latest, dropped


async def _merge_content_tasks(
    queue: asyncio.Queue[_mq.MessageTask],
    first: _mq.MessageTask,
    lock: asyncio.Lock,
) -> tuple[_mq.MessageTask, int]:
    """Merge consecutive content tasks from queue.

    Returns: (merged_task, merge_count) where merge_count is the number of
    additional tasks merged (0 if no merging occurred).

    Note on queue counter management:
        When we put items back, we call task_done() to compensate for the
        internal counter increment caused by put_nowait(). This is necessary
        because the items were already counted when originally enqueued.
        Without this compensation, queue.join() would wait indefinitely.
    """
    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0

    async with lock:
        items = _inspect_queue(queue)
        remaining: list[_mq.MessageTask] = []

        for i, task in enumerate(items):
            if not _can_merge_tasks(first, task):
                # Can't merge, keep this and all remaining items
                remaining = items[i:]
                break

            # Check length before merging
            task_length = sum(len(p) for p in task.parts)
            if current_length + task_length > _mq.MERGE_MAX_LENGTH:
                # Too long, stop merging
                remaining = items[i:]
                break

            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1

        # Put remaining items back into the queue
        for item in remaining:
            queue.put_nowait(item)
            # Compensate: this item was already counted when first enqueued,
            # put_nowait adds a duplicate count that must be removed
            queue.task_done()

    if merge_count == 0:
        return first, 0

    return (
        _mq.MessageTask(
            task_type="content",
            window_id=first.window_id,
            parts=merged_parts,
            tool_use_id=first.tool_use_id,
            content_type=first.content_type,
            thread_id=first.thread_id,
            chat_id=first.chat_id,
        ),
        merge_count,
    )


async def _message_queue_worker(bot: Bot, user_id: int, thread_id: int) -> None:
    """Process message tasks for a topic sequentially."""
    key = (user_id, thread_id)
    queue = _mq._message_queues[key]
    lock = _mq._queue_locks[key]
    logger.info(
        "Message queue worker started for user %d, thread %d", user_id, thread_id
    )

    while True:
        try:
            task = await queue.get()
            try:
                # Flood control: drop status, wait for content
                chat_id = task.chat_id
                if chat_id is not None:
                    flood_end = _mq._flood_until.get(chat_id, 0)
                    if flood_end > 0:
                        remaining = flood_end - time.monotonic()
                        if remaining > 0:
                            if task.task_type != "content":
                                # Status is ephemeral — safe to drop
                                continue
                            # Content is actual Claude output — wait then send
                            logger.debug(
                                "Flood controlled: waiting %.0fs for content "
                                "(user %d, thread %d)",
                                remaining,
                                user_id,
                                thread_id,
                            )
                            await asyncio.sleep(remaining)
                        # Ban expired
                        _mq._flood_until.pop(chat_id, None)
                        logger.info(
                            "Flood control lifted for chat %d (user %d, thread %d)",
                            chat_id,
                            user_id,
                            thread_id,
                        )

                if task.task_type == "content":
                    # Try to merge consecutive content tasks
                    merged_task, merge_count = await _merge_content_tasks(
                        queue, task, lock
                    )
                    if merge_count > 0:
                        logger.debug(
                            "Merged %d tasks for user %d, thread %d",
                            merge_count,
                            user_id,
                            thread_id,
                        )
                        # Mark merged tasks as done
                        for _ in range(merge_count):
                            queue.task_done()
                    await _process_content_task(bot, user_id, merged_task)
                elif task.task_type == "status_update":
                    # Coalesce: drop older status_update tasks for the
                    # same window if a newer one is queued behind us.
                    latest_task, dropped = await _coalesce_status_updates(
                        queue, task, lock
                    )
                    if dropped > 0:
                        logger.debug(
                            "Dropped %d stale status_update(s) for window %s "
                            "(user %d, thread %d)",
                            dropped,
                            task.window_id,
                            user_id,
                            thread_id,
                        )
                        for _ in range(dropped):
                            queue.task_done()
                    await _process_status_update_task(bot, user_id, latest_task)
                elif task.task_type == "status_clear":
                    await _do_clear_status_message(
                        bot, user_id, task.thread_id or 0, chat_id=task.chat_id
                    )
            except RetryAfter as e:
                retry_secs = (
                    e.retry_after
                    if isinstance(e.retry_after, int)
                    else int(e.retry_after.total_seconds())
                )
                if retry_secs > _mq.FLOOD_CONTROL_MAX_WAIT:
                    if task.chat_id is not None:
                        _mq._flood_until[task.chat_id] = time.monotonic() + retry_secs
                    logger.warning(
                        "Flood control for chat %s: retry_after=%ds, "
                        "pausing queue until ban expires (user %d, thread %d)",
                        task.chat_id,
                        retry_secs,
                        user_id,
                        thread_id,
                    )
                else:
                    logger.warning(
                        "Flood control for user %d, thread %d: waiting %ds",
                        user_id,
                        thread_id,
                        retry_secs,
                    )
                    await asyncio.sleep(retry_secs)
            except Exception as e:
                # If Telegram says the thread no longer exists, the topic
                # was deleted: drop the binding and any queued tasks.
                from .auto_unbind import maybe_unbind

                chat_id_for_unbind = task.chat_id if task is not None else None
                if maybe_unbind(e, chat_id_for_unbind, thread_id):
                    drained = 0
                    while not queue.empty():
                        try:
                            queue.get_nowait()
                            queue.task_done()
                            drained += 1
                        except asyncio.QueueEmpty:
                            break
                    logger.info(
                        "Drained %d queued task(s) after auto-unbind "
                        "(user %d, thread %d)",
                        drained,
                        user_id,
                        thread_id,
                    )
                else:
                    logger.error(
                        "Error processing message task for user %d, thread %d: %s",
                        user_id,
                        thread_id,
                        e,
                    )
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            logger.info(
                "Message queue worker cancelled for user %d, thread %d",
                user_id,
                thread_id,
            )
            break
        except Exception as e:
            logger.error(
                "Unexpected error in queue worker for user %d, thread %d: %s",
                user_id,
                thread_id,
                e,
            )
