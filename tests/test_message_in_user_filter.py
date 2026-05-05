"""Frontend filter on role=="user" messages.

Backend `ccmux` v5.0.0 stops filtering user messages. Frontend takes
ownership of the toggle: at the top of `handle_new_message`, drop
role==user messages when `config.show_user_messages` is false.
Default `true` preserves the current echo-to-Telegram behavior.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccmux.api import ClaudeMessage


def _make_topic(user_id: int = 1, window_id: str = "@5", thread_id: int = 42):
    topic = MagicMock()
    topic.user_id = user_id
    topic.window_id = window_id
    topic.thread_id = thread_id
    topic.group_chat_id = 100
    return topic


def _user_msg() -> ClaudeMessage:
    return ClaudeMessage(
        session_id="sess-1",
        role="user",
        content_type="text",
        text="hello from cc",
        is_complete=True,
    )


def _assistant_msg() -> ClaudeMessage:
    return ClaudeMessage(
        session_id="sess-1",
        role="assistant",
        content_type="text",
        text="hi back",
        is_complete=True,
    )


@pytest.fixture
def cfg(monkeypatch):
    from ccmux_telegram import message_in

    config_mock = MagicMock()
    config_mock.show_tool_calls = True
    config_mock.show_thinking = True
    config_mock.show_skill_bodies = False
    config_mock.tool_calls_allowlist = frozenset({"Skill"})
    config_mock.show_user_messages = True
    monkeypatch.setattr(message_in, "config", config_mock)
    return config_mock


@pytest.mark.asyncio
async def test_user_message_dropped_when_show_user_messages_false(cfg):
    cfg.show_user_messages = False

    from ccmux_telegram import message_in

    topic = _make_topic()
    with patch.object(message_in, "get_topic_for_claude_session", return_value=topic):
        with patch.object(message_in, "enqueue_content_message", new=AsyncMock()) as eq:
            await message_in.handle_new_message("sess-1", _user_msg(), MagicMock())

    assert eq.await_count == 0


@pytest.mark.asyncio
async def test_user_message_emitted_when_show_user_messages_true(cfg):
    cfg.show_user_messages = True

    from ccmux_telegram import message_in

    topic = _make_topic()
    with patch.object(message_in, "get_topic_for_claude_session", return_value=topic):
        with patch.object(message_in, "enqueue_content_message", new=AsyncMock()) as eq:
            await message_in.handle_new_message("sess-1", _user_msg(), MagicMock())

    assert eq.await_count >= 1


@pytest.mark.asyncio
async def test_assistant_message_unaffected_by_toggle(cfg):
    cfg.show_user_messages = False  # toggle off: assistants still flow

    from ccmux_telegram import message_in

    topic = _make_topic()
    with patch.object(message_in, "get_topic_for_claude_session", return_value=topic):
        with patch.object(message_in, "enqueue_content_message", new=AsyncMock()) as eq:
            await message_in.handle_new_message("sess-1", _assistant_msg(), MagicMock())

    assert eq.await_count >= 1
