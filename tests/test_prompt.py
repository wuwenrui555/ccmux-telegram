"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ccmux.api import BlockedUI
from telegram.error import BadRequest

from ccmux_telegram.prompt import (
    _build_interactive_keyboard,
    _format_blocked_content,
    handle_interactive_ui,
)
from ccmux_telegram.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)


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


@pytest.mark.usefixtures("_clear_interactive_state")
class TestHandleInteractiveUI:
    @pytest.mark.asyncio
    async def test_handle_settings_ui_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """handle_interactive_ui captures Settings pane, sends message with keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccmux_telegram.prompt.tmux_registry") as mock_registry,
            patch("ccmux_telegram.prompt.get_topic"),
        ):
            mock_tm = MagicMock()
            mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tm.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_tm.send_keys = AsyncMock(return_value=True)
            mock_registry.get_by_window_id.return_value = mock_tm

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42, chat_id=100
            )

        assert result is True
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args
        assert call_kwargs.kwargs["chat_id"] == 100
        assert call_kwargs.kwargs["message_thread_id"] == 42
        assert call_kwargs.kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_edit_not_modified_is_treated_as_success(self, mock_bot: AsyncMock):
        """Telegram's 'message is not modified' is a no-op success, not
        a failure -- edge-triggered dispatch already prevents most
        duplicates, but keep this defensive path so a stray identical
        edit does not pop state and re-send.
        """
        from ccmux_telegram.prompt_state import set_interactive_msg_id

        window_id = "@5"
        user_id = 1
        thread_id = 42
        set_interactive_msg_id(user_id, 12345, thread_id)

        mock_bot.edit_message_text.side_effect = BadRequest(
            "Message is not modified: specified new message content"
        )

        result = await handle_interactive_ui(
            mock_bot,
            user_id=user_id,
            window_id=window_id,
            thread_id=thread_id,
            chat_id=100,
            ui=BlockedUI.PERMISSION_PROMPT,
            content="Do you want to proceed?",
        )

        assert result is True
        mock_bot.edit_message_text.assert_called_once()
        mock_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_edit_other_bad_request_falls_back_to_send(self, mock_bot: AsyncMock):
        """A BadRequest that is NOT 'not modified' should still pop state
        and fall back to a fresh send."""
        from ccmux_telegram.prompt_state import set_interactive_msg_id

        window_id = "@5"
        user_id = 1
        thread_id = 42
        set_interactive_msg_id(user_id, 12345, thread_id)

        mock_bot.edit_message_text.side_effect = BadRequest("Message to edit not found")

        result = await handle_interactive_ui(
            mock_bot,
            user_id=user_id,
            window_id=window_id,
            thread_id=thread_id,
            chat_id=100,
            ui=BlockedUI.PERMISSION_PROMPT,
            content="Do you want to proceed?",
        )

        assert result is True
        mock_bot.edit_message_text.assert_called_once()
        mock_bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_no_ui_returns_false(self, mock_bot: AsyncMock):
        """Returns False when no interactive UI detected in pane."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccmux_telegram.prompt.tmux_registry") as mock_registry,
            patch("ccmux_telegram.prompt.get_topic"),
        ):
            mock_tm = MagicMock()
            mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tm.capture_pane = AsyncMock(return_value="$ echo hello\nhello\n$\n")
            mock_tm.send_keys = AsyncMock(return_value=True)
            mock_registry.get_by_window_id.return_value = mock_tm

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42, chat_id=100
            )

        assert result is False
        mock_bot.send_message.assert_not_called()


class TestFormatBlockedContent:
    """Heuristic Markdown styling for extracted pane content.

    The rules are deliberately simple so wrong matches produce plain
    text, not malformed MarkdownV2. Test the four rules independently
    plus the realistic permission-prompt composition.
    """

    def test_first_nonblank_line_is_bold(self):
        result = _format_blocked_content("Read file\n\n  Read(/etc/hosts)\n")
        assert result.startswith("**Read file**")

    def test_question_line_is_bold(self):
        text = "Read file\n\n  Read(/foo)\n\n Do you want to proceed?\n"
        result = _format_blocked_content(text)
        assert "**Read file**" in result
        assert "** Do you want to proceed?**" in result

    def test_selected_option_is_bold(self):
        text = "Pick one\n\n ❯ 1. Yes, default\n   2. No, cancel\n Esc to cancel\n"
        result = _format_blocked_content(text)
        assert "** ❯ 1. Yes, default**" in result
        # Unselected option stays plain.
        assert "\n   2. No, cancel\n" in result

    def test_footer_is_italic(self):
        text = "Read file\n\n ❯ 1. Yes\n   2. No\n\n Esc to cancel · Tab to amend\n"
        result = _format_blocked_content(text)
        assert "_ Esc to cancel · Tab to amend_" in result

    def test_enter_to_footer_is_italic(self):
        text = "Enable auto mode?\n\n ❯ 1. Yes\n   2. No\n\n Enter to confirm · Esc to cancel\n"
        result = _format_blocked_content(text)
        assert "_ Enter to confirm · Esc to cancel_" in result

    def test_plain_lines_stay_plain(self):
        """Lines that are neither title, question, selected option, nor
        footer must not gain any markdown markers."""
        text = "Read file\n\n  Read(/etc/hosts)\n  — allow reading from etc/\n"
        result = _format_blocked_content(text)
        # The tool-call line and description line are not decorated.
        assert "  Read(/etc/hosts)\n  — allow reading from etc/" in result
        # But the title line is still bolded.
        assert "**Read file**" in result


class TestKeyboardLayoutForSettings:
    def test_settings_keyboard_includes_all_nav_keys(self):
        """Settings keyboard includes Tab, arrows (not vertical_only), Space, Esc, Enter."""
        keyboard = _build_interactive_keyboard("@5", ui_name="Settings")
        # Flatten all callback data values
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert any(CB_ASK_TAB in d for d in all_cb_data if d)
        assert any(CB_ASK_SPACE in d for d in all_cb_data if d)
        assert any(CB_ASK_UP in d for d in all_cb_data if d)
        assert any(CB_ASK_DOWN in d for d in all_cb_data if d)
        assert any(CB_ASK_LEFT in d for d in all_cb_data if d)
        assert any(CB_ASK_RIGHT in d for d in all_cb_data if d)
        assert any(CB_ASK_ESC in d for d in all_cb_data if d)
        assert any(CB_ASK_ENTER in d for d in all_cb_data if d)
