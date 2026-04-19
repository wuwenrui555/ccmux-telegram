"""Tests for the status producer/consumer split.

Covers both sides:
  - StatusMonitor._observe: returns raw WindowStatus observations.
  - consume_statuses: translates WindowStatus into Telegram actions.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccmux_telegram.status_line import consume_statuses
from ccmux.status_monitor import StatusMonitor, WindowStatus
from ccmux.tmux_pane_parser import InteractiveUIContent


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccmux_telegram.prompt_state import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


def _make_binding(window_id: str = "@5", thread_id: int = 42, chat_id: int = 100):
    b = MagicMock()
    b.window_id = window_id
    b.user_id = 1
    b.thread_id = thread_id
    b.group_chat_id = chat_id
    return b


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusMonitorObserve:
    """StatusMonitor._observe produces correct WindowStatus from raw pane text."""

    @pytest.mark.asyncio
    async def test_settings_pane_produces_interactive_ui(
        self, sample_pane_settings: str
    ):
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        mock_registry = MagicMock()
        mock_tm = MagicMock()
        mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tm.capture_pane = AsyncMock(return_value=sample_pane_settings)
        mock_registry.get_by_window_id.return_value = mock_tm

        monitor = StatusMonitor(tmux_registry=mock_registry)
        status = await monitor._observe(_make_binding(window_id))

        assert status.window_exists is True
        assert status.pane_captured is True
        assert status.interactive_ui is not None
        assert "Select model" in status.interactive_ui.content

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self):
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        mock_registry = MagicMock()
        mock_tm = MagicMock()
        mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tm.capture_pane = AsyncMock(return_value=normal_pane)
        mock_registry.get_by_window_id.return_value = mock_tm

        monitor = StatusMonitor(tmux_registry=mock_registry)
        status = await monitor._observe(_make_binding(window_id))

        assert status.window_exists is True
        assert status.pane_captured is True
        assert status.interactive_ui is None


@pytest.mark.usefixtures("_clear_interactive_state")
class TestConsumeStatuses:
    """consume_statuses translates WindowStatus into bot API calls."""

    @pytest.mark.asyncio
    async def test_interactive_ui_triggers_handler(self, mock_bot: AsyncMock):
        status = WindowStatus(
            window_id="@5",
            window_exists=True,
            pane_captured=True,
            status_text=None,
            interactive_ui=InteractiveUIContent(
                content="Select model …", name="Settings"
            ),
        )
        topic = _make_binding("@5")

        with (
            patch(
                "ccmux_telegram.runtime.get_topic_by_window_id",
                return_value=topic,
            ),
            patch(
                "ccmux_telegram.status_line.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_handle_ui.return_value = True
            await consume_statuses(mock_bot, [status])

        mock_handle_ui.assert_called_once_with(mock_bot, 1, "@5", 42, chat_id=100)

    @pytest.mark.asyncio
    async def test_no_interactive_ui_no_handler(self, mock_bot: AsyncMock):
        status = WindowStatus(
            window_id="@5",
            window_exists=True,
            pane_captured=True,
            status_text=None,
            interactive_ui=None,
        )
        topic = _make_binding("@5")

        with (
            patch(
                "ccmux_telegram.runtime.get_topic_by_window_id",
                return_value=topic,
            ),
            patch(
                "ccmux_telegram.status_line.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccmux_telegram.status_line.enqueue_status_update",
                new_callable=AsyncMock,
            ),
        ):
            await consume_statuses(mock_bot, [status])

        mock_handle_ui.assert_not_called()

    @pytest.mark.asyncio
    async def test_settings_ui_end_to_end_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Full path: producer observes Settings pane, consumer sends keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        topic = _make_binding(window_id)

        mock_registry_poll = MagicMock()
        mock_tm_poll = MagicMock()
        mock_tm_poll.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tm_poll.capture_pane = AsyncMock(return_value=sample_pane_settings)
        mock_registry_poll.get_by_window_id.return_value = mock_tm_poll

        with (
            patch("ccmux_telegram.prompt.tmux_registry") as mock_registry_ui,
            patch("ccmux_telegram.prompt.get_topic"),
            patch("ccmux_telegram.runtime.get_topic_by_window_id", return_value=topic),
        ):
            mock_tm_ui = MagicMock()
            mock_tm_ui.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tm_ui.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_tm_ui.send_keys = AsyncMock(return_value=True)
            mock_registry_ui.get_by_window_id.return_value = mock_tm_ui

            monitor = StatusMonitor(tmux_registry=mock_registry_poll)
            status = await monitor._observe(_make_binding(window_id))
            await consume_statuses(mock_bot, [status])

        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 100
        assert call_kwargs["message_thread_id"] == 42
        assert call_kwargs["reply_markup"] is not None
        assert "Select model" in call_kwargs["text"]
