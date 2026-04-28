"""Periodic loop drives BindingHealth.observe and posts ✅."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccmux_telegram.binding_health import BindingHealth, Transition  # noqa: F401


@pytest.mark.asyncio
async def test_loop_posts_recovered_message() -> None:
    from ccmux_telegram.main import _binding_health_iteration

    bindings = [MagicMock(session_name="ccmux", group_chat_id=-100, thread_id=1160)]
    topics = MagicMock()
    topics.all = MagicMock(return_value=bindings)

    state_cache = MagicMock()
    state_cache.is_alive = MagicMock(return_value=True)

    health = BindingHealth()
    # Seed: last observation was False, so True will produce RECOVERED.
    health.observe("ccmux", False)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await _binding_health_iteration(topics, state_cache, health, bot)

    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "recovered" in text.lower()
    assert "ccmux" in text


@pytest.mark.asyncio
async def test_loop_does_not_post_on_stable() -> None:
    from ccmux_telegram.main import _binding_health_iteration

    bindings = [MagicMock(session_name="ccmux", group_chat_id=-100, thread_id=1160)]
    topics = MagicMock()
    topics.all = MagicMock(return_value=bindings)

    state_cache = MagicMock()
    state_cache.is_alive = MagicMock(return_value=True)
    health = BindingHealth()  # default prev=True → STABLE

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await _binding_health_iteration(topics, state_cache, health, bot)

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_does_not_post_on_lost() -> None:
    from ccmux_telegram.main import _binding_health_iteration

    bindings = [MagicMock(session_name="ccmux", group_chat_id=-100, thread_id=1160)]
    topics = MagicMock()
    topics.all = MagicMock(return_value=bindings)

    state_cache = MagicMock()
    state_cache.is_alive = MagicMock(return_value=False)
    # prev defaults True → False yields LOST
    health = BindingHealth()

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await _binding_health_iteration(topics, state_cache, health, bot)

    bot.send_message.assert_not_awaited()
