"""AUQ message lifecycle bound to tool_use_id / tool_result.

Once an AskUserQuestion / ExitPlanMode tool_use is observed, the Telegram
interactive button message must persist until the matching tool_result
arrives — even if the pane briefly transitions out of the Blocked state
(e.g., when the user picks "Chat about this", which opens a side chat
with claude without answering the AUQ). The tool_result is the
authoritative signal that the prompt has actually been resolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccmux.api import ClaudeMessage, Idle

from ccmux_telegram import prompt_state
from ccmux_telegram.state_cache import get_state_cache
from ccmux_telegram.status_line import on_state


# ---- Fakes / fixtures (mirrors test_status_monitor.py patterns) ----------


@dataclass
class _FakeBot:
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.calls.append(("edit_message_text", kwargs))

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(("send_message", kwargs))

        @dataclass
        class _Sent:
            message_id: int = 0

        return _Sent()

    async def delete_message(self, **kwargs: Any) -> None:
        self.calls.append(("delete_message", kwargs))


@pytest.fixture
def fresh_state():
    """Reset every prompt_state and state_cache dict between tests."""
    cache = get_state_cache()
    cache._data.clear()  # noqa: SLF001
    prompt_state._interactive_mode.clear()  # noqa: SLF001
    prompt_state._interactive_msgs.clear()  # noqa: SLF001
    prompt_state._pending_prompt_tool_uses.clear()  # noqa: SLF001
    yield
    cache._data.clear()
    prompt_state._interactive_mode.clear()  # noqa: SLF001
    prompt_state._interactive_msgs.clear()  # noqa: SLF001
    prompt_state._pending_prompt_tool_uses.clear()  # noqa: SLF001


@pytest.fixture
def topic_binding(monkeypatch):
    @dataclass
    class _FakeTopic:
        user_id: int = 1
        thread_id: int = 42
        group_chat_id: int = 100
        window_id: str = "@7"
        session_name: str = "alpha"

    fake = _FakeTopic()
    monkeypatch.setattr(
        "ccmux_telegram.runtime.get_topic_by_session_name",
        lambda instance_id: fake if instance_id == "alpha" else None,
    )
    return fake


def _make_topic(user_id: int = 1, window_id: str = "@7", thread_id: int = 42):
    topic = MagicMock()
    topic.user_id = user_id
    topic.window_id = window_id
    topic.thread_id = thread_id
    topic.group_chat_id = 100
    return topic


def _msg(
    *,
    content_type: str,
    tool_name: str | None = None,
    tool_use_id: str | None = None,
    role: str = "assistant",
) -> ClaudeMessage:
    return ClaudeMessage(
        session_id="sess-1",
        role=role,  # type: ignore[arg-type]
        content_type=content_type,  # type: ignore[arg-type]
        text="",
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        is_complete=True,
    )


# ---- prompt_state pending helpers ----------------------------------------


class TestPendingHelpers:
    def test_set_get_clear_round_trip(self, fresh_state):
        assert prompt_state.get_pending_prompt_tool_use(1, 42) is None
        prompt_state.set_pending_prompt_tool_use(1, 42, "tool_abc")
        assert prompt_state.get_pending_prompt_tool_use(1, 42) == "tool_abc"
        popped = prompt_state.clear_pending_prompt_tool_use(1, 42)
        assert popped == "tool_abc"
        assert prompt_state.get_pending_prompt_tool_use(1, 42) is None

    def test_per_user_thread_isolation(self, fresh_state):
        prompt_state.set_pending_prompt_tool_use(1, 42, "tu1")
        prompt_state.set_pending_prompt_tool_use(1, 99, "tu2")
        prompt_state.set_pending_prompt_tool_use(2, 42, "tu3")
        assert prompt_state.get_pending_prompt_tool_use(1, 42) == "tu1"
        assert prompt_state.get_pending_prompt_tool_use(1, 99) == "tu2"
        assert prompt_state.get_pending_prompt_tool_use(2, 42) == "tu3"

    def test_pop_interactive_state_also_clears_pending(self, fresh_state):
        # Hard cleanup paths (binding lifecycle, etc.) clear all three
        # dicts together.
        prompt_state.set_interactive_mode(1, "@7", 42)
        prompt_state.set_interactive_msg_id(1, 555, 42)
        prompt_state.set_pending_prompt_tool_use(1, 42, "tool_abc")

        prompt_state.pop_interactive_state(1, 42)
        assert prompt_state.get_pending_prompt_tool_use(1, 42) is None


# ---- status_line: Idle dispatch + pending suppression --------------------


class TestIdleSuppressedWhilePending:
    @pytest.mark.asyncio
    async def test_idle_does_not_clear_when_pending_is_set(
        self, fresh_state, topic_binding, monkeypatch
    ) -> None:
        """While AUQ is pending, an Idle observation must NOT clear the msg."""
        cleared: list[tuple] = []

        async def fake_clear(*args, **kwargs):
            cleared.append((args, kwargs))

        monkeypatch.setattr(
            "ccmux_telegram.status_line.clear_interactive_msg", fake_clear
        )
        monkeypatch.setattr(
            "ccmux_telegram.status_line.get_interactive_window",
            lambda user_id, thread_id: "@7",
        )
        prompt_state.set_pending_prompt_tool_use(1, 42, "tool_abc")

        await on_state("alpha", Idle(), bot=_FakeBot())

        assert cleared == []  # suppressed

    @pytest.mark.asyncio
    async def test_idle_clears_when_no_pending(
        self, fresh_state, topic_binding, monkeypatch
    ) -> None:
        """Without a pending tool_use, behaviour is unchanged from v5.2.x."""
        cleared: list[tuple] = []

        async def fake_clear(user_id, bot, thread_id, *, chat_id):
            cleared.append((user_id, thread_id, chat_id))

        monkeypatch.setattr(
            "ccmux_telegram.status_line.clear_interactive_msg", fake_clear
        )
        monkeypatch.setattr(
            "ccmux_telegram.status_line.get_interactive_window",
            lambda user_id, thread_id: "@7",
        )

        await on_state("alpha", Idle(), bot=_FakeBot())

        assert cleared == [(1, 42, 100)]


# ---- message_in: tool_use sets pending; tool_result resolves it ----------


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


class TestMessageInLifecycle:
    @pytest.mark.asyncio
    async def test_auq_tool_use_sets_pending_after_handled(
        self, fresh_state, cfg
    ) -> None:
        """AUQ tool_use that renders successfully records the tool_use_id."""
        from ccmux_telegram import message_in

        msg = _msg(
            content_type="tool_use", tool_name="AskUserQuestion", tool_use_id="tu1"
        )
        topic = _make_topic()
        with (
            patch.object(
                message_in, "get_topic_for_claude_session", return_value=topic
            ),
            patch.object(
                message_in,
                "handle_interactive_ui",
                new=AsyncMock(return_value=True),
            ),
            patch.object(message_in, "get_message_queue", return_value=None),
        ):
            await message_in.handle_new_message("sess-1", msg, MagicMock())

        assert prompt_state.get_pending_prompt_tool_use(1, 42) == "tu1"

    @pytest.mark.asyncio
    async def test_auq_tool_use_does_not_set_pending_when_unhandled(
        self, fresh_state, cfg
    ) -> None:
        """If the prompt UI fails to render, pending stays unset."""
        from ccmux_telegram import message_in

        msg = _msg(
            content_type="tool_use", tool_name="AskUserQuestion", tool_use_id="tu1"
        )
        topic = _make_topic()
        with (
            patch.object(
                message_in, "get_topic_for_claude_session", return_value=topic
            ),
            patch.object(
                message_in,
                "handle_interactive_ui",
                new=AsyncMock(return_value=False),
            ),
            patch.object(message_in, "get_message_queue", return_value=None),
        ):
            await message_in.handle_new_message("sess-1", msg, MagicMock())

        assert prompt_state.get_pending_prompt_tool_use(1, 42) is None

    @pytest.mark.asyncio
    async def test_matching_tool_result_clears_pending_and_msg(
        self, fresh_state, cfg
    ) -> None:
        """The tool_result for the tracked tool_use_id closes the prompt."""
        from ccmux_telegram import message_in

        prompt_state.set_pending_prompt_tool_use(1, 42, "tu1")

        msg = _msg(content_type="tool_result", tool_use_id="tu1", role="user")
        topic = _make_topic()
        clear_mock = AsyncMock()
        with (
            patch.object(
                message_in, "get_topic_for_claude_session", return_value=topic
            ),
            patch.object(message_in, "clear_interactive_msg", clear_mock),
        ):
            await message_in.handle_new_message("sess-1", msg, MagicMock())

        assert prompt_state.get_pending_prompt_tool_use(1, 42) is None
        clear_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unrelated_tool_result_leaves_pending_alone(
        self, fresh_state, cfg
    ) -> None:
        """tool_result for some other tool must not clear the pending AUQ."""
        from ccmux_telegram import message_in

        prompt_state.set_pending_prompt_tool_use(1, 42, "tu_auq")

        msg = _msg(content_type="tool_result", tool_use_id="tu_other", role="user")
        topic = _make_topic()
        clear_mock = AsyncMock()
        with (
            patch.object(
                message_in, "get_topic_for_claude_session", return_value=topic
            ),
            patch.object(message_in, "clear_interactive_msg", clear_mock),
        ):
            await message_in.handle_new_message("sess-1", msg, MagicMock())

        assert prompt_state.get_pending_prompt_tool_use(1, 42) == "tu_auq"
        clear_mock.assert_not_awaited()
