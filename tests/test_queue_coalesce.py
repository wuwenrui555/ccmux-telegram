"""Tests for ``_coalesce_status_updates``.

When a worker dequeues a ``status_update`` task, any newer
``status_update`` for the same window already queued behind it is
stale — Telegram's per-chat rate limit makes intermediate edits
useless. The coalescer keeps only the latest, dropping older ones,
and preserves all other task types in their original order.
"""

from __future__ import annotations

import asyncio

import pytest

from ccmux_telegram._queue_worker import _coalesce_status_updates
from ccmux_telegram.message_queue import MessageTask


def _status(window_id: str, text: str, *, chat_id: int = -100) -> MessageTask:
    return MessageTask(
        task_type="status_update",
        chat_id=chat_id,
        text=text,
        window_id=window_id,
    )


def _content(window_id: str, body: str, *, chat_id: int = -100) -> MessageTask:
    return MessageTask(
        task_type="content",
        chat_id=chat_id,
        window_id=window_id,
        parts=[body],
    )


def _status_clear(window_id: str, *, chat_id: int = -100) -> MessageTask:
    return MessageTask(
        task_type="status_clear",
        chat_id=chat_id,
        window_id=window_id,
    )


async def _fill(queue: asyncio.Queue, tasks: list[MessageTask]) -> None:
    for t in tasks:
        await queue.put(t)


@pytest.mark.asyncio
async def test_no_other_status_updates_returns_first_unchanged() -> None:
    queue: asyncio.Queue[MessageTask] = asyncio.Queue()
    await _fill(queue, [_content("@1", "hello"), _status_clear("@1")])
    first = _status("@1", "Working… 1s")
    lock = asyncio.Lock()

    latest, dropped = await _coalesce_status_updates(queue, first, lock)

    assert latest is first
    assert dropped == 0
    # Both non-status-update items preserved in order.
    leftover = []
    while not queue.empty():
        leftover.append(queue.get_nowait())
        queue.task_done()
    assert [t.task_type for t in leftover] == ["content", "status_clear"]


@pytest.mark.asyncio
async def test_drops_older_keeps_latest_for_same_window() -> None:
    queue: asyncio.Queue[MessageTask] = asyncio.Queue()
    await _fill(
        queue,
        [
            _status("@1", "Working… 2s"),
            _status("@1", "Working… 3s"),
            _status("@1", "Working… 4s"),
        ],
    )
    first = _status("@1", "Working… 1s")
    lock = asyncio.Lock()

    latest, dropped = await _coalesce_status_updates(queue, first, lock)

    assert dropped == 3
    # The latest queued status text wins.
    assert latest.text == "Working… 4s"
    # Queue is now empty (all three were taken and discarded; the
    # caller will process ``latest`` in place of ``first``).
    assert queue.empty()


@pytest.mark.asyncio
async def test_does_not_drop_other_window_status() -> None:
    queue: asyncio.Queue[MessageTask] = asyncio.Queue()
    await _fill(
        queue,
        [
            _status("@2", "Different window"),
            _status("@1", "Working… 5s"),  # this one DOES coalesce
        ],
    )
    first = _status("@1", "Working… 1s")
    lock = asyncio.Lock()

    latest, dropped = await _coalesce_status_updates(queue, first, lock)

    assert dropped == 1  # only @1's intermediate
    assert latest.window_id == "@1"
    assert latest.text == "Working… 5s"
    # @2's status preserved
    leftover = []
    while not queue.empty():
        leftover.append(queue.get_nowait())
        queue.task_done()
    assert len(leftover) == 1
    assert leftover[0].window_id == "@2"


@pytest.mark.asyncio
async def test_preserves_intervening_content_for_same_window() -> None:
    """A content task BETWEEN two same-window status updates must not be
    swallowed. The newer status_update is still kept (replaces first); the
    content task is preserved at its position."""
    queue: asyncio.Queue[MessageTask] = asyncio.Queue()
    await _fill(
        queue,
        [
            _content("@1", "real response"),
            _status("@1", "Working… 5s"),
        ],
    )
    first = _status("@1", "Working… 1s")
    lock = asyncio.Lock()

    latest, dropped = await _coalesce_status_updates(queue, first, lock)

    assert dropped == 1
    assert latest.text == "Working… 5s"
    leftover = []
    while not queue.empty():
        leftover.append(queue.get_nowait())
        queue.task_done()
    # Content task survived.
    assert [t.task_type for t in leftover] == ["content"]


@pytest.mark.asyncio
async def test_status_clear_not_treated_as_status_update() -> None:
    """status_clear is a different task type and must be preserved."""
    queue: asyncio.Queue[MessageTask] = asyncio.Queue()
    await _fill(queue, [_status_clear("@1")])
    first = _status("@1", "Working… 1s")
    lock = asyncio.Lock()

    latest, dropped = await _coalesce_status_updates(queue, first, lock)

    assert latest is first
    assert dropped == 0
    # status_clear preserved.
    leftover = []
    while not queue.empty():
        leftover.append(queue.get_nowait())
        queue.task_done()
    assert [t.task_type for t in leftover] == ["status_clear"]
