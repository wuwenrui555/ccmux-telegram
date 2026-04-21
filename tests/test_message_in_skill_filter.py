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
    cfg.tool_calls_allowlist = frozenset({"Skill"})
    monkeypatch.setattr(message_in, "config", cfg)
    return cfg


class TestHandleNewMessageDoesNotRaceStateMonitor:
    """Regression: state_monitor owns the interactive-msg lifecycle.

    Claude Code 2.1.x writes a non-PROMPT ``tool_use`` (Read/Write/Edit/
    Bash) to JSONL while its permission UI is still on the pane. Before
    this fix, ``handle_new_message`` blanket-cleared the interactive
    msg on any non-PROMPT tool_use, racing state_monitor's freshly
    sent Blocked message. Keep state_monitor as the single clearer."""

    @pytest.mark.asyncio
    async def test_non_prompt_tool_use_does_not_clear_interactive_msg(
        self, _patch_config
    ):
        from ccmux_telegram import message_in
        from ccmux_telegram.prompt_state import (
            _interactive_mode,
            _interactive_msgs,
            set_interactive_mode,
            set_interactive_msg_id,
        )

        _interactive_mode.clear()
        _interactive_msgs.clear()
        set_interactive_msg_id(1, 17399, 42)
        set_interactive_mode(1, "@5", 42)

        msg = ClaudeMessage(
            session_id="s1",
            role="assistant",
            content_type="tool_use",
            text="**Read**(/etc/passwd)",
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
            patch.object(message_in, "enqueue_content_message", new=AsyncMock()),
            patch.object(message_in, "handle_interactive_ui", new=AsyncMock()) as iu,
        ):
            await message_in.handle_new_message("test-instance", msg, AsyncMock())

        # The non-PROMPT tool path must not touch the interactive msg.
        iu.assert_not_called()
        assert _interactive_msgs.get((1, 42)) == 17399
        _interactive_mode.clear()
        _interactive_msgs.clear()


@pytest.mark.asyncio
async def test_skill_tool_use_still_emitted(_patch_config):
    """Skill tool_use summary is not suppressed, only tool_result is."""
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
        await message_in.handle_new_message("test-instance", msg, AsyncMock())

    enq.assert_called_once()


class TestToolCallsAllowlist:
    @pytest.mark.asyncio
    async def test_bash_tool_use_dropped_when_tool_calls_off(self, _patch_config):
        """Non-allowlisted tool_use is dropped when show_tool_calls is False."""
        _patch_config.show_tool_calls = False
        from ccmux_telegram import message_in

        msg = ClaudeMessage(
            session_id="s1",
            role="assistant",
            content_type="tool_use",
            text="**Bash**(ls)",
            tool_use_id="t1",
            tool_name="Bash",
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
            await message_in.handle_new_message("test-instance", msg, AsyncMock())

        enq.assert_not_called()

    @pytest.mark.asyncio
    async def test_skill_tool_use_passes_via_allowlist(self, _patch_config):
        """Skill tool_use is forwarded even when show_tool_calls is False."""
        _patch_config.show_tool_calls = False
        from ccmux_telegram import message_in

        msg = ClaudeMessage(
            session_id="s1",
            role="assistant",
            content_type="tool_use",
            text="**Skill**(superpowers:requesting-code-review)",
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
            await message_in.handle_new_message("test-instance", msg, AsyncMock())

        enq.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_allowlist_entry_passes(self, _patch_config):
        """Adding a tool name to the allowlist exempts it from show_tool_calls=False."""
        _patch_config.show_tool_calls = False
        _patch_config.tool_calls_allowlist = frozenset({"Read"})
        from ccmux_telegram import message_in

        msg = ClaudeMessage(
            session_id="s1",
            role="assistant",
            content_type="tool_use",
            text="**Read**(foo.py)",
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
            await message_in.handle_new_message("test-instance", msg, AsyncMock())

        enq.assert_called_once()


class TestSkillBodyUserText:
    SKILL_BODY = (
        "Base directory for this skill: /path/to/skill\n\n"
        "# Some Skill\n\nLots of content...\n"
    )

    @pytest.mark.asyncio
    async def test_skill_body_user_text_suppressed(self, _patch_config):
        """User-role text starting with skill-body marker is dropped."""
        from ccmux_telegram import message_in

        msg = ClaudeMessage(
            session_id="s1",
            role="user",
            content_type="text",
            text=self.SKILL_BODY,
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
            await message_in.handle_new_message("test-instance", msg, AsyncMock())

        enq.assert_not_called()

    @pytest.mark.asyncio
    async def test_skill_body_passes_when_bodies_enabled(self, _patch_config):
        """Skill body user text passes when show_skill_bodies is True."""
        _patch_config.show_skill_bodies = True
        from ccmux_telegram import message_in

        msg = ClaudeMessage(
            session_id="s1",
            role="user",
            content_type="text",
            text=self.SKILL_BODY,
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
            await message_in.handle_new_message("test-instance", msg, AsyncMock())

        enq.assert_called_once()

    @pytest.mark.asyncio
    async def test_regular_user_text_unaffected(self, _patch_config):
        """Normal user-role text is forwarded regardless of skill gate."""
        from ccmux_telegram import message_in

        msg = ClaudeMessage(
            session_id="s1",
            role="user",
            content_type="text",
            text="just a regular user message",
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
            await message_in.handle_new_message("test-instance", msg, AsyncMock())

        enq.assert_called_once()

    @pytest.mark.asyncio
    async def test_skill_body_with_leading_whitespace_suppressed(self, _patch_config):
        """Leading whitespace before the skill marker is tolerated."""
        from ccmux_telegram import message_in

        msg = ClaudeMessage(
            session_id="s1",
            role="user",
            content_type="text",
            text="  \n" + self.SKILL_BODY,
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
            await message_in.handle_new_message("test-instance", msg, AsyncMock())

        enq.assert_not_called()
