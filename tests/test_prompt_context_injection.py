"""handle_interactive_ui must prepend cached tool_context to the UI message."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_state():
    from ccmux_telegram import tool_context
    from ccmux_telegram.prompt_state import _interactive_mode, _interactive_msgs

    tool_context._PENDING.clear()
    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    tool_context._PENDING.clear()
    _interactive_mode.clear()
    _interactive_msgs.clear()


def _seed_edit_cache(window_id: str):
    from ccmux_telegram import tool_context

    dq = tool_context._deque_for(window_id)
    dq.append(
        tool_context.PendingToolContext(
            tool_name="Edit",
            tool_use_id="tu-1",
            input={
                "file_path": "/tmp/proj/a.py",
                "old_string": "x = 1",
                "new_string": "x = 2",
            },
            recorded_at=time.monotonic(),
        )
    )


@pytest.mark.asyncio
async def test_permission_prompt_prepends_edit_context(sample_pane_permission):
    from ccmux_telegram.prompt import handle_interactive_ui

    window_id = "@5"
    _seed_edit_cache(window_id)

    mock_bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 321
    mock_bot.send_message.return_value = sent

    mock_window = MagicMock()
    mock_window.window_id = window_id

    with (
        patch("ccmux_telegram.prompt.tmux_registry") as mock_registry,
        patch("ccmux_telegram.prompt.get_topic"),
    ):
        mock_tm = MagicMock()
        mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tm.capture_pane = AsyncMock(return_value=sample_pane_permission)
        mock_registry.get_by_window_id.return_value = mock_tm

        result = await handle_interactive_ui(
            mock_bot, user_id=1, window_id=window_id, thread_id=42, chat_id=100
        )

    assert result is True
    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    assert "/tmp/proj/a.py" in sent_text
    assert "Do you want to proceed?" in sent_text
    assert sent_text.index("/tmp/proj/a.py") < sent_text.index(
        "Do you want to proceed?"
    )


@pytest.mark.asyncio
async def test_no_cache_behavior_unchanged(sample_pane_permission):
    """When cache is empty, handle_interactive_ui sends just the pane UI."""
    from ccmux_telegram.prompt import handle_interactive_ui

    window_id = "@7"
    mock_bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 321
    mock_bot.send_message.return_value = sent

    mock_window = MagicMock()
    mock_window.window_id = window_id

    with (
        patch("ccmux_telegram.prompt.tmux_registry") as mock_registry,
        patch("ccmux_telegram.prompt.get_topic"),
    ):
        mock_tm = MagicMock()
        mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tm.capture_pane = AsyncMock(return_value=sample_pane_permission)
        mock_registry.get_by_window_id.return_value = mock_tm

        result = await handle_interactive_ui(
            mock_bot, user_id=1, window_id=window_id, thread_id=42, chat_id=100
        )

    assert result is True
    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    assert "Do you want to proceed?" in sent_text
    assert "**Edit**" not in sent_text
