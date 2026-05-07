"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ccmux.api import BlockedUI
from telegram.error import BadRequest

from ccmux_telegram.prompt import (
    _build_interactive_keyboard,
    _format_blocked_content,
    _render_from_tool_args,
    _render_mdv2,
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


@pytest.mark.usefixtures("_clear_interactive_state")
class TestHandleInteractiveUIArgsFallback:
    """Pane-capture-empty + tool_use args = render via fallback.

    Validates that when `extract_interactive_content` fails (covers the
    tmux scroll race / fast TUI answer / pane flicker), the prompt is
    still delivered to Telegram by rendering directly from the JSONL
    `tool_use_args` payload.
    """

    @pytest.mark.asyncio
    async def test_args_fallback_rendered_when_pane_empty(self, mock_bot: AsyncMock):
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        # Pane text that doesn't match any UI pattern → extract returns None.
        with (
            patch("ccmux_telegram.prompt.tmux_registry") as mock_registry,
            patch(
                "ccmux_telegram.prompt.extract_interactive_content",
                return_value=None,
            ),
        ):
            mock_tm = MagicMock()
            mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tm.capture_pane = AsyncMock(return_value="(non-matching pane)\n")
            mock_registry.get_by_window_id.return_value = mock_tm

            auq_args = {
                "questions": [
                    {
                        "question": "Pick a path",
                        "options": [
                            {"label": "Yes", "description": "proceed"},
                            {"label": "No", "description": "cancel"},
                        ],
                    }
                ]
            }
            result = await handle_interactive_ui(
                mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                chat_id=100,
                tool_name="AskUserQuestion",
                tool_use_args=auq_args,
            )

        assert result is True
        mock_bot.send_message.assert_called_once()
        sent_text = mock_bot.send_message.call_args.kwargs["text"]
        # The question survives the MDV2 rendering pipeline.
        assert "Pick a path" in sent_text
        # And so do the option labels.
        assert "Yes" in sent_text
        assert "No" in sent_text

    @pytest.mark.asyncio
    async def test_args_fallback_skipped_for_non_prompt_tool(self, mock_bot: AsyncMock):
        """A non-prompt `tool_name` must not trigger the fallback path
        even if the caller passes args (defensive: backend already
        whitelists prompt tools, this is the second guard).
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccmux_telegram.prompt.tmux_registry") as mock_registry,
            patch(
                "ccmux_telegram.prompt.extract_interactive_content",
                return_value=None,
            ),
        ):
            mock_tm = MagicMock()
            mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tm.capture_pane = AsyncMock(return_value="(non-matching pane)\n")
            mock_registry.get_by_window_id.return_value = mock_tm

            result = await handle_interactive_ui(
                mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                chat_id=100,
                tool_name="Edit",
                tool_use_args={"file_path": "x.py"},
            )

        assert result is False
        mock_bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_fallback_when_args_missing(self, mock_bot: AsyncMock):
        """Pane fails AND no args → return False. The default path for
        callers that don't have a JSONL message (e.g. refresh callback).
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccmux_telegram.prompt.tmux_registry") as mock_registry,
            patch(
                "ccmux_telegram.prompt.extract_interactive_content",
                return_value=None,
            ),
        ):
            mock_tm = MagicMock()
            mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tm.capture_pane = AsyncMock(return_value="(non-matching pane)\n")
            mock_registry.get_by_window_id.return_value = mock_tm

            result = await handle_interactive_ui(
                mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                chat_id=100,
                # tool_name + tool_use_args omitted (None)
            )

        assert result is False
        mock_bot.send_message.assert_not_called()


class TestRenderFromToolArgs:
    """Pure-function tests for `_render_from_tool_args`."""

    def test_ask_user_question_basic(self):
        args = {
            "questions": [
                {
                    "question": "Continue?",
                    "options": [
                        {"label": "Yes", "description": "proceed"},
                        {"label": "No", "description": "cancel"},
                    ],
                }
            ]
        }
        result = _render_from_tool_args("AskUserQuestion", args)
        assert result is not None
        ui_name, text = result
        assert ui_name == "ask_user_question"
        assert "Continue?" in text
        assert "1. Yes" in text
        assert "2. No" in text
        assert "proceed" in text
        assert "cancel" in text

    def test_ask_user_question_options_without_description(self):
        args = {
            "questions": [
                {
                    "question": "Pick",
                    "options": [{"label": "A"}, {"label": "B"}],
                }
            ]
        }
        result = _render_from_tool_args("AskUserQuestion", args)
        assert result is not None
        _ui_name, text = result
        assert "1. A" in text
        assert "2. B" in text
        # No em-dash separator when description is empty.
        assert " — " not in text

    def test_ask_user_question_missing_questions_returns_none(self):
        assert _render_from_tool_args("AskUserQuestion", {}) is None
        assert _render_from_tool_args("AskUserQuestion", {"questions": []}) is None

    def test_exit_plan_mode_basic(self):
        args = {"plan": "Step 1: do X\nStep 2: do Y"}
        result = _render_from_tool_args("ExitPlanMode", args)
        assert result is not None
        ui_name, text = result
        assert ui_name == "exit_plan_mode"
        assert text == "Step 1: do X\nStep 2: do Y"

    def test_exit_plan_mode_missing_plan_returns_none(self):
        assert _render_from_tool_args("ExitPlanMode", {}) is None
        assert _render_from_tool_args("ExitPlanMode", {"plan": ""}) is None

    @pytest.mark.parametrize("tool_name", ["Read", "Bash", "Edit", "Write"])
    def test_non_prompt_tools_return_none(self, tool_name: str):
        # Even when args look plausible the helper must refuse — only
        # prompt tools are supported.
        assert _render_from_tool_args(tool_name, {"plan": "x"}) is None
        assert _render_from_tool_args(tool_name, {"questions": [{}]}) is None


class TestFormatBlockedContent:
    """Heuristic Markdown styling for extracted pane content.

    Leading whitespace stays outside the markers so ``** x**`` (with a
    space after the opening ``**``) is never produced — Markdown would
    not recognize that as bold.
    """

    def test_first_nonblank_line_is_bold(self):
        result = _format_blocked_content("Read file\n\n  Read(/etc/hosts)\n")
        assert result.startswith("**Read file**")

    def test_leading_indent_stays_outside_bold(self):
        """Bold markers must hug the text, never the whitespace."""
        result = _format_blocked_content(" Read file\n")
        # Preferred: " **Read file**" — indent survives, markers wrap text.
        assert result.startswith(" **Read file**")
        # Never the broken form where the opening ** has a trailing space.
        assert not result.startswith("** ")

    def test_question_line_is_bold(self):
        text = "Read file\n\n  Read(/foo)\n\n Do you want to proceed?\n"
        result = _format_blocked_content(text)
        assert "**Read file**" in result
        assert " **Do you want to proceed?**" in result

    def test_selected_option_is_bold(self):
        text = "Pick one\n\n ❯ 1. Yes, default\n   2. No, cancel\n Esc to cancel\n"
        result = _format_blocked_content(text)
        assert " **❯ 1. Yes, default**" in result
        # Unselected option stays plain.
        assert "\n   2. No, cancel\n" in result

    def test_footer_is_italic(self):
        text = "Read file\n\n ❯ 1. Yes\n   2. No\n\n Esc to cancel · Tab to amend\n"
        result = _format_blocked_content(text)
        assert " _Esc to cancel · Tab to amend_" in result

    def test_enter_to_footer_is_italic(self):
        text = "Enable auto mode?\n\n ❯ 1. Yes\n   2. No\n\n Enter to confirm · Esc to cancel\n"
        result = _format_blocked_content(text)
        assert " _Enter to confirm · Esc to cancel_" in result

    def test_plain_lines_stay_plain(self):
        """Lines that are neither title, question, selected option, nor
        footer must not gain any markdown markers."""
        text = "Read file\n\n  Read(/etc/hosts)\n  — allow reading from etc/\n"
        result = _format_blocked_content(text)
        # The tool-call line and description line are not decorated.
        assert "  Read(/etc/hosts)\n  — allow reading from etc/" in result
        # But the title line is still bolded.
        assert "**Read file**" in result


class TestRenderMdV2:
    """Minimal Markdown → MarkdownV2 translator.

    We bypass the full ``convert_markdown`` pipeline because its
    mistletoe parser (1) rewrites ``1. Yes`` / ``2. No`` numbered lines
    as an ordered list, collapsing indentation, and (2) does not accept
    ``** x**`` as bold. Those are routine shapes for Claude's blocking
    UIs, so we roll our own.
    """

    def test_double_asterisk_bold_to_single_asterisk(self):
        assert _render_mdv2("**Read file**") == "*Read file*"

    def test_underscore_italic_stays(self):
        assert _render_mdv2("_Esc to cancel_") == "_Esc to cancel_"

    def test_escapes_mdv2_special_outside_markers(self):
        # `(`, `)`, `.` all require backslash escape in MDV2 literals.
        assert _render_mdv2("Read(/etc/hosts)") == "Read\\(/etc/hosts\\)"
        assert _render_mdv2("1. Yes") == "1\\. Yes"

    def test_escapes_inside_bold_markers(self):
        # Literals inside bold still need escape — Telegram parses
        # `*1. Yes*` as bold containing an unescaped `.`, which rejects
        # the whole message.
        assert _render_mdv2("**1. Yes**") == "*1\\. Yes*"

    def test_preserves_leading_whitespace(self):
        # mistletoe collapses leading spaces for ordered-list lines; our
        # renderer must not.
        assert _render_mdv2("   2. No") == "   2\\. No"

    def test_unterminated_marker_is_literal(self):
        # A stray `**` with no closing pair should be escaped, not
        # consumed — prevents truncated bold from swallowing the rest
        # of the message.
        out = _render_mdv2("hello **world")
        assert out == "hello \\*\\*world"

    def test_full_permission_prompt_pipeline(self):
        """End-to-end: raw pane content → styled Markdown → MDV2."""
        raw = (
            " Read file\n"
            "\n"
            "  Read(/etc/hosts)\n"
            "\n"
            " Do you want to proceed?\n"
            " ❯ 1. Yes\n"
            "   2. No\n"
            "\n"
            " Esc to cancel · Tab to amend\n"
        )
        out = _render_mdv2(_format_blocked_content(raw))
        # Bold title / question / selected option.
        assert " *Read file*" in out
        assert " *Do you want to proceed?*" in out
        assert " *❯ 1\\. Yes*" in out
        # Italic footer.
        assert " _Esc to cancel · Tab to amend_" in out
        # Tool call line: not bolded, parens escaped.
        assert "  Read\\(/etc/hosts\\)" in out
        # Unselected option: plain, with its indent intact and `.` escaped.
        assert "   2\\. No" in out


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
