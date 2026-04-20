"""Skill tool_result gating in handle_new_message."""

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


@pytest.fixture(autouse=True)
def _patch_config(monkeypatch):
    from ccmux_telegram import message_in

    cfg = MagicMock()
    cfg.show_tool_calls = True
    cfg.show_thinking = True
    cfg.show_skill_bodies = False
    monkeypatch.setattr(message_in, "config", cfg)
    return cfg


@pytest.mark.asyncio
async def test_skill_tool_result_suppressed_by_default(_patch_config):
    """Skill tool_result is dropped when show_skill_bodies is False."""
    from ccmux_telegram import message_in

    msg = ClaudeMessage(
        session_id="s1",
        role="assistant",
        content_type="tool_result",
        text="## skill body here\n" * 200,
        tool_use_id="t1",
        tool_name="Skill",
        is_complete=True,
    )
    with (
        patch.object(
            message_in,
            "get_topic_for_claude_session",
            return_value=_make_topic(),
        ),
        patch.object(message_in, "enqueue_content_message", new=AsyncMock()) as enq,
    ):
        await message_in.handle_new_message(msg, AsyncMock())

    enq.assert_not_called()


@pytest.mark.asyncio
async def test_skill_tool_result_emitted_when_enabled(_patch_config):
    """Skill tool_result passes through when show_skill_bodies is True."""
    _patch_config.show_skill_bodies = True
    from ccmux_telegram import message_in

    msg = ClaudeMessage(
        session_id="s1",
        role="assistant",
        content_type="tool_result",
        text="> skill body\n",
        tool_use_id="t1",
        tool_name="Skill",
        is_complete=True,
    )
    with (
        patch.object(
            message_in,
            "get_topic_for_claude_session",
            return_value=_make_topic(),
        ),
        patch.object(message_in, "enqueue_content_message", new=AsyncMock()) as enq,
    ):
        await message_in.handle_new_message(msg, AsyncMock())

    enq.assert_called_once()


@pytest.mark.asyncio
async def test_non_skill_tool_result_unaffected(_patch_config):
    """Non-Skill tool_result is emitted regardless of show_skill_bodies."""
    from ccmux_telegram import message_in

    msg = ClaudeMessage(
        session_id="s1",
        role="assistant",
        content_type="tool_result",
        text="  ⎿  Read 30 lines",
        tool_use_id="t1",
        tool_name="Read",
        is_complete=True,
    )
    with (
        patch.object(
            message_in,
            "get_topic_for_claude_session",
            return_value=_make_topic(),
        ),
        patch.object(message_in, "enqueue_content_message", new=AsyncMock()) as enq,
    ):
        await message_in.handle_new_message(msg, AsyncMock())

    enq.assert_called_once()


@pytest.mark.asyncio
async def test_skill_tool_use_still_emitted(_patch_config):
    """Skill tool_use summary is not suppressed — only tool_result is."""
    from ccmux_telegram import message_in

    msg = ClaudeMessage(
        session_id="s1",
        role="assistant",
        content_type="tool_use",
        text="**Skill**(brainstorming)",
        tool_use_id="t1",
        tool_name="Skill",
        is_complete=True,
    )
    with (
        patch.object(
            message_in,
            "get_topic_for_claude_session",
            return_value=_make_topic(),
        ),
        patch.object(message_in, "enqueue_content_message", new=AsyncMock()) as enq,
    ):
        await message_in.handle_new_message(msg, AsyncMock())

    enq.assert_called_once()
