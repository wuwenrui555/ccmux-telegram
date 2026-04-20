"""Tests for per-topic message queue behavior.

Covers:
  - Queue independence per topic: separate queues and workers per (user_id, thread_id)
  - Flood control keyed by chat_id: shared across topics in the same chat
  - Shutdown: cancels all per-topic workers and clears state
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccmux_telegram import message_queue as queue_mod
from ccmux_telegram.message_queue import (
    get_message_queue,
    get_or_create_queue,
    shutdown_workers,
)


@pytest.fixture(autouse=True)
def clear_queue_state():
    """Ensure queue module state is clean between tests."""
    queue_mod._message_queues.clear()
    queue_mod._queue_workers.clear()
    queue_mod._queue_locks.clear()
    queue_mod._flood_until.clear()
    queue_mod._status_msg_info.clear()
    queue_mod._status_last_enqueue.clear()
    queue_mod._tool_msg_ids.clear()
    yield
    queue_mod._message_queues.clear()
    queue_mod._queue_workers.clear()
    queue_mod._queue_locks.clear()
    queue_mod._status_last_enqueue.clear()


@pytest.mark.asyncio
async def test_queues_are_per_topic():
    """Two topics for the same user get separate queues and workers."""
    bot = MagicMock()
    q_a = get_or_create_queue(bot, user_id=1, thread_id=10)
    q_b = get_or_create_queue(bot, user_id=1, thread_id=11)

    assert q_a is not q_b
    assert (1, 10) in queue_mod._message_queues
    assert (1, 11) in queue_mod._message_queues
    assert (1, 10) in queue_mod._queue_workers
    assert (1, 11) in queue_mod._queue_workers

    # get_message_queue fetches by key
    assert get_message_queue(1, 10) is q_a
    assert get_message_queue(1, 11) is q_b
    assert get_message_queue(1, 999) is None

    await shutdown_workers()


@pytest.mark.asyncio
async def test_flood_control_keyed_by_chat_id():
    """enqueue_status_update skips when _flood_until[chat_id] is set."""
    from ccmux_telegram.message_queue import enqueue_status_update

    bot = MagicMock()
    bot.send_message = AsyncMock()

    # Simulate active flood ban for chat 555
    queue_mod._flood_until[555] = time.monotonic() + 60

    # Enqueue status for two different topics (thread 10 and 11) in chat 555
    await enqueue_status_update(
        bot,
        user_id=1,
        window_id="@1",
        status_text="working",
        thread_id=10,
        chat_id=555,
    )
    await enqueue_status_update(
        bot,
        user_id=1,
        window_id="@2",
        status_text="working",
        thread_id=11,
        chat_id=555,
    )

    # Neither topic should have created a queue entry (flood was active)
    assert (1, 10) not in queue_mod._message_queues
    assert (1, 11) not in queue_mod._message_queues

    # Different chat (666) is not blocked
    await enqueue_status_update(
        bot,
        user_id=1,
        window_id="@3",
        status_text="working",
        thread_id=12,
        chat_id=666,
    )
    assert (1, 12) in queue_mod._message_queues

    await shutdown_workers()


@pytest.mark.asyncio
async def test_status_throttle_drops_updates_within_interval(monkeypatch):
    """Second text update within STATUS_MIN_INTERVAL is dropped."""
    from ccmux_telegram.message_queue import enqueue_status_update

    monkeypatch.setattr(queue_mod, "STATUS_MIN_INTERVAL", 5.0)
    clock = [1000.0]
    monkeypatch.setattr(queue_mod.time, "monotonic", lambda: clock[0])

    bot = MagicMock()

    # First update: goes through
    await enqueue_status_update(
        bot,
        user_id=1,
        window_id="@1",
        status_text="Computing… (0s)",
        thread_id=10,
        chat_id=555,
    )
    q = get_message_queue(1, 10)
    assert q is not None and q.qsize() == 1

    # 2s later — still within 5s window → dropped
    clock[0] = 1002.0
    await enqueue_status_update(
        bot,
        user_id=1,
        window_id="@1",
        status_text="Computing… (2s)",
        thread_id=10,
        chat_id=555,
    )
    assert q.qsize() == 1

    # 6s after first — past interval → goes through
    clock[0] = 1006.0
    await enqueue_status_update(
        bot,
        user_id=1,
        window_id="@1",
        status_text="Computing… (6s)",
        thread_id=10,
        chat_id=555,
    )
    assert q.qsize() == 2

    await shutdown_workers()


@pytest.mark.asyncio
async def test_status_clear_bypasses_throttle_and_resets_cursor(monkeypatch):
    """Clears are never throttled, and reset the cursor so the next update lands immediately."""
    from ccmux_telegram.message_queue import enqueue_status_update

    monkeypatch.setattr(queue_mod, "STATUS_MIN_INTERVAL", 5.0)
    clock = [2000.0]
    monkeypatch.setattr(queue_mod.time, "monotonic", lambda: clock[0])

    bot = MagicMock()

    # First update records cursor at t=2000
    await enqueue_status_update(
        bot,
        user_id=1,
        window_id="@1",
        status_text="Computing… (0s)",
        thread_id=10,
        chat_id=555,
    )
    q = get_message_queue(1, 10)
    assert q is not None and q.qsize() == 1
    assert (1, 10) in queue_mod._status_last_enqueue

    # Clear arrives 1s later — throttle must NOT drop a clear
    clock[0] = 2001.0
    await enqueue_status_update(
        bot,
        user_id=1,
        window_id="@1",
        status_text=None,
        thread_id=10,
        chat_id=555,
    )
    assert q.qsize() == 2
    # Cursor reset so the next update isn't blocked by the previous burst
    assert (1, 10) not in queue_mod._status_last_enqueue

    # New update 1s after clear — should land immediately
    clock[0] = 2002.0
    await enqueue_status_update(
        bot,
        user_id=1,
        window_id="@1",
        status_text="Wandering…",
        thread_id=10,
        chat_id=555,
    )
    assert q.qsize() == 3

    await shutdown_workers()


@pytest.mark.asyncio
async def test_status_throttle_disabled_when_interval_zero(monkeypatch):
    """STATUS_MIN_INTERVAL <= 0 disables the throttle entirely."""
    from ccmux_telegram.message_queue import enqueue_status_update

    monkeypatch.setattr(queue_mod, "STATUS_MIN_INTERVAL", 0.0)
    clock = [3000.0]
    monkeypatch.setattr(queue_mod.time, "monotonic", lambda: clock[0])

    bot = MagicMock()
    for i in range(3):
        await enqueue_status_update(
            bot,
            user_id=1,
            window_id="@1",
            status_text=f"Computing… ({i}s)",
            thread_id=10,
            chat_id=555,
        )

    q = get_message_queue(1, 10)
    assert q is not None and q.qsize() == 3

    await shutdown_workers()


@pytest.mark.asyncio
async def test_shutdown_workers_cancels_all_topics():
    """shutdown_workers cancels all per-topic workers and clears state."""
    bot = MagicMock()
    get_or_create_queue(bot, user_id=1, thread_id=10)
    get_or_create_queue(bot, user_id=2, thread_id=20)
    get_or_create_queue(bot, user_id=2, thread_id=21)

    assert len(queue_mod._queue_workers) == 3

    await shutdown_workers()

    assert queue_mod._queue_workers == {}
    assert queue_mod._message_queues == {}
    assert queue_mod._queue_locks == {}
