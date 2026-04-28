"""Behavior of the /rebind_window command."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccmux.api import ClaudeInstance


@pytest.fixture(autouse=True)
def allow_all_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the @authorized gate for these tests."""
    monkeypatch.setattr("ccmux_telegram.util.is_user_allowed", lambda _uid: True)


@pytest.fixture
def update_with_topic() -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock(id=8559840605)
    update.message = MagicMock()
    update.message.message_thread_id = 2
    update.message.reply_text = AsyncMock()
    return update


@pytest.fixture
def context() -> MagicMock:
    return MagicMock()


@pytest.mark.asyncio
async def test_rebind_window_unbound(update_with_topic, context) -> None:
    from ccmux_telegram import command_basic

    update = update_with_topic
    with patch("ccmux_telegram.command_basic.get_topic", return_value=None):
        await command_basic.rebind_window_command(update, context)
    msg = update.message.reply_text.await_args.args[0]
    # safe_reply may escape markdown (rebind\_topic for MarkdownV2),
    # so accept either the bare or escaped form.
    assert "❌" in msg
    assert "rebind" in msg and "topic" in msg


@pytest.mark.asyncio
async def test_rebind_window_success(update_with_topic, context) -> None:
    from ccmux_telegram import command_basic

    update = update_with_topic
    fake_topic = MagicMock(session_name="outlook")
    fake_inst = ClaudeInstance(
        instance_id="outlook",
        window_id="@22",
        session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        cwd="/Users/wenruiwu",
    )
    fake_backend = MagicMock()
    fake_backend.reconcile_instance = AsyncMock(return_value=fake_inst)
    fake_backend.claude_instances = MagicMock()
    fake_backend.claude_instances.set_override = MagicMock()

    with patch("ccmux_telegram.command_basic.get_topic", return_value=fake_topic):
        with patch(
            "ccmux_telegram.command_basic._get_backend",
            return_value=fake_backend,
            create=True,
        ):
            await command_basic.rebind_window_command(update, context)

    fake_backend.claude_instances.set_override.assert_called_once_with(
        "outlook", fake_inst
    )
    msg = update.message.reply_text.await_args.args[0]
    assert "✅" in msg
    assert "@22" in msg


@pytest.mark.asyncio
async def test_rebind_window_no_live_claude(update_with_topic, context) -> None:
    from ccmux_telegram import command_basic

    update = update_with_topic
    fake_topic = MagicMock(session_name="outlook")
    fake_backend = MagicMock()
    fake_backend.reconcile_instance = AsyncMock(return_value=None)

    with patch("ccmux_telegram.command_basic.get_topic", return_value=fake_topic):
        with patch(
            "ccmux_telegram.command_basic._get_backend",
            return_value=fake_backend,
            create=True,
        ):
            await command_basic.rebind_window_command(update, context)

    msg = update.message.reply_text.await_args.args[0]
    assert "⚠️" in msg
    assert "rebind" in msg and "topic" in msg
    assert "/start" in msg
