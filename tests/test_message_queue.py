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
    queue_mod._tool_msg_ids.clear()
    yield
    queue_mod._message_queues.clear()
    queue_mod._queue_workers.clear()
    queue_mod._queue_locks.clear()


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
