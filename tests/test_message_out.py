"""Tests for forward_command_handler — command forwarding to Claude Code."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update(text: str, user_id: int = 1, thread_id: int = 42) -> MagicMock:
    """Build a minimal mock Update with message text in a forum topic."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.chat = MagicMock()
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = 100
    return update


def _make_context() -> MagicMock:
    """Build a minimal mock context."""
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


# Patch targets point to the module where the names are looked up at runtime
_MOD = "ccmux_telegram.message_out"


class TestForwardCommand:
    @pytest.mark.asyncio
    async def test_model_sends_command_to_tmux(self):
        """/model → send_to_window called with "/model"."""
        update = _make_update("/model")
        context = _make_context()

        with (
            patch("ccmux_telegram.util.is_user_allowed", return_value=True),
            patch(f"{_MOD}.get_thread_id", return_value=42),
            patch(f"{_MOD}.get_topic") as mock_get_topic,
            patch(f"{_MOD}._topics") as mock_topics,
            patch(f"{_MOD}.get_tm_and_window", new_callable=AsyncMock) as mock_gtw,
            patch(f"{_MOD}.get_default_backend") as mock_get_backend,
            patch(f"{_MOD}.safe_reply", new_callable=AsyncMock),
        ):
            mock_tm = MagicMock()
            mock_tm.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_tm.send_keys = AsyncMock(return_value=True)
            mock_gtw.return_value = (mock_tm, MagicMock())
            mock_binding = MagicMock()
            mock_binding.window_id = "@5"
            mock_binding.session_name = "project"
            mock_get_topic.return_value = mock_binding
            mock_topics.is_alive.return_value = True
            mock_backend = MagicMock()
            mock_backend.tmux = MagicMock()
            mock_backend.tmux.send_text = AsyncMock(return_value=(True, "ok"))
            mock_get_backend.return_value = mock_backend

            from ccmux_telegram.message_out import forward_command_handler

            await forward_command_handler(update, context)

            mock_backend.tmux.send_text.assert_called_once_with("@5", "/model")

    @pytest.mark.asyncio
    async def test_cost_sends_command_to_tmux(self):
        """/cost → send_to_window called with "/cost"."""
        update = _make_update("/cost")
        context = _make_context()

        with (
            patch("ccmux_telegram.util.is_user_allowed", return_value=True),
            patch(f"{_MOD}.get_thread_id", return_value=42),
            patch(f"{_MOD}.get_topic") as mock_get_topic,
            patch(f"{_MOD}._topics") as mock_topics,
            patch(f"{_MOD}.get_tm_and_window", new_callable=AsyncMock) as mock_gtw,
            patch(f"{_MOD}.get_default_backend") as mock_get_backend,
            patch(f"{_MOD}.safe_reply", new_callable=AsyncMock),
        ):
            mock_tm = MagicMock()
            mock_tm.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_tm.send_keys = AsyncMock(return_value=True)
            mock_gtw.return_value = (mock_tm, MagicMock())
            mock_binding = MagicMock()
            mock_binding.window_id = "@5"
            mock_binding.session_name = "project"
            mock_get_topic.return_value = mock_binding
            mock_topics.is_alive.return_value = True
            mock_backend = MagicMock()
            mock_backend.tmux = MagicMock()
            mock_backend.tmux.send_text = AsyncMock(return_value=(True, "ok"))
            mock_get_backend.return_value = mock_backend

            from ccmux_telegram.message_out import forward_command_handler

            await forward_command_handler(update, context)

            mock_backend.tmux.send_text.assert_called_once_with("@5", "/cost")

    @pytest.mark.asyncio
    async def test_clear_sends_command_to_tmux(self):
        """/clear → send_to_window called with "/clear"."""
        update = _make_update("/clear")
        context = _make_context()

        with (
            patch("ccmux_telegram.util.is_user_allowed", return_value=True),
            patch(f"{_MOD}.get_thread_id", return_value=42),
            patch(f"{_MOD}.get_topic") as mock_get_topic,
            patch(f"{_MOD}._topics") as mock_topics,
            patch(f"{_MOD}.get_tm_and_window", new_callable=AsyncMock) as mock_gtw,
            patch(f"{_MOD}.get_default_backend") as mock_get_backend,
            patch(f"{_MOD}.safe_reply", new_callable=AsyncMock),
        ):
            mock_tm = MagicMock()
            mock_tm.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_tm.send_keys = AsyncMock(return_value=True)
            mock_gtw.return_value = (mock_tm, MagicMock())
            mock_binding = MagicMock()
            mock_binding.window_id = "@5"
            mock_binding.session_name = "project"
            mock_get_topic.return_value = mock_binding
            mock_topics.is_alive.return_value = True
            mock_backend = MagicMock()
            mock_backend.tmux = MagicMock()
            mock_backend.tmux.send_text = AsyncMock(return_value=(True, "ok"))
            mock_get_backend.return_value = mock_backend

            from ccmux_telegram.message_out import forward_command_handler

            await forward_command_handler(update, context)

            mock_backend.tmux.send_text.assert_called_once_with("@5", "/clear")
