"""Tests for status_line.on_state — ClaudeState to Telegram translator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ccmux.api import Blocked, BlockedUI, Dead, Idle, Working

from ccmux_telegram.state_cache import get_state_cache
from ccmux_telegram.status_line import on_state


# ---- Fakes ----------------------------------------------------------------


@dataclass
class _FakeBot:
    """Minimal telegram.Bot double; records calls but returns safe defaults."""

    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.calls.append(("edit_message_text", kwargs))

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(("send_message", kwargs))

        @dataclass
        class _Sent:
            message_id: int = 0

        return _Sent()


# ---- Fixtures -------------------------------------------------------------


@pytest.fixture
def fresh_cache():
    """Reset the shared state cache between tests."""
    cache = get_state_cache()
    cache._data.clear()  # noqa: SLF001  — test-only reset
    yield cache
    cache._data.clear()


@pytest.fixture
def topic_binding(monkeypatch):
    """Register a fake topic so get_topic_by_session_name returns something."""

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


# ---- Tests ----------------------------------------------------------------


class TestOnStateUpdatesCache:
    @pytest.mark.asyncio
    async def test_cache_updates_even_when_no_topic_bound(
        self, fresh_cache, monkeypatch
    ) -> None:
        """State cache must update for every instance, even ones with no
        topic binding, so future binding queries see the observation."""
        monkeypatch.setattr(
            "ccmux_telegram.runtime.get_topic_by_session_name",
            lambda instance_id: None,
        )
        bot = _FakeBot()

        await on_state("orphan", Working(status_text="Reading\u2026"), bot=bot)

        assert fresh_cache.get("orphan") is not None
        assert isinstance(fresh_cache.get("orphan"), Working)
        # No telegram calls for orphan instances.
        assert bot.calls == []


class TestWorking:
    @pytest.mark.asyncio
    async def test_working_enqueues_status(
        self, fresh_cache, topic_binding, monkeypatch
    ) -> None:
        calls: list[tuple] = []

        async def fake_enqueue(
            bot, user_id, window_id, text, *, thread_id=None, chat_id=None
        ):
            calls.append((user_id, window_id, text, thread_id, chat_id))

        monkeypatch.setattr(
            "ccmux_telegram.status_line.enqueue_status_update", fake_enqueue
        )
        bot = _FakeBot()

        await on_state("alpha", Working(status_text="Reading file\u2026"), bot=bot)

        assert calls == [(1, "@7", "Reading file\u2026", 42, 100)]


class TestIdle:
    @pytest.mark.asyncio
    async def test_idle_clears_interactive_when_bound(
        self, fresh_cache, topic_binding, monkeypatch
    ) -> None:
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

    @pytest.mark.asyncio
    async def test_idle_skips_clear_when_interactive_is_elsewhere(
        self, fresh_cache, topic_binding, monkeypatch
    ) -> None:
        cleared: list[tuple] = []

        async def fake_clear(*args, **kwargs):
            cleared.append((args, kwargs))

        monkeypatch.setattr(
            "ccmux_telegram.status_line.clear_interactive_msg", fake_clear
        )
        monkeypatch.setattr(
            "ccmux_telegram.status_line.get_interactive_window",
            lambda user_id, thread_id: "@other",
        )

        await on_state("alpha", Idle(), bot=_FakeBot())

        assert cleared == []


class TestBlocked:
    @pytest.mark.asyncio
    async def test_blocked_calls_handle_interactive_ui(
        self, fresh_cache, topic_binding, monkeypatch
    ) -> None:
        calls: list[dict] = []

        async def fake_handle(
            bot, user_id, window_id, thread_id, *, chat_id, ui, content
        ):
            calls.append(
                {
                    "user_id": user_id,
                    "window_id": window_id,
                    "thread_id": thread_id,
                    "chat_id": chat_id,
                    "ui": ui,
                    "content": content,
                }
            )
            return True

        monkeypatch.setattr(
            "ccmux_telegram.status_line.handle_interactive_ui", fake_handle
        )

        await on_state(
            "alpha",
            Blocked(ui=BlockedUI.PERMISSION_PROMPT, content="Do you want to proceed?"),
            bot=_FakeBot(),
        )

        assert len(calls) == 1
        assert calls[0]["ui"] is BlockedUI.PERMISSION_PROMPT
        assert calls[0]["content"] == "Do you want to proceed?"


class TestDead:
    @pytest.mark.asyncio
    async def test_dead_enqueues_resuming_status(
        self, fresh_cache, topic_binding, monkeypatch
    ) -> None:
        calls: list[tuple] = []

        async def fake_enqueue(
            bot, user_id, window_id, text, *, thread_id=None, chat_id=None
        ):
            calls.append((user_id, window_id, text))

        monkeypatch.setattr(
            "ccmux_telegram.status_line.enqueue_status_update", fake_enqueue
        )

        await on_state("alpha", Dead(), bot=_FakeBot())

        assert calls == [(1, "@7", "Resuming session\u2026")]


class TestEdgeTriggeredDispatch:
    """on_state only dispatches when the observed state actually changed.

    Backend re-emits ClaudeState every fast tick; level-triggered
    dispatch would spam identical Telegram payloads and trip Telegram's
    "message is not modified" rejection.
    """

    @pytest.mark.asyncio
    async def test_repeat_same_state_dispatches_once(
        self, fresh_cache, topic_binding, monkeypatch
    ) -> None:
        calls: list[tuple] = []

        async def fake_enqueue(
            bot, user_id, window_id, text, *, thread_id=None, chat_id=None
        ):
            calls.append((user_id, text))

        monkeypatch.setattr(
            "ccmux_telegram.status_line.enqueue_status_update", fake_enqueue
        )

        state = Working(status_text="Reading\u2026")
        await on_state("alpha", state, bot=_FakeBot())
        await on_state("alpha", state, bot=_FakeBot())
        await on_state("alpha", state, bot=_FakeBot())

        assert calls == [(1, "Reading\u2026")]

    @pytest.mark.asyncio
    async def test_changed_state_dispatches_again(
        self, fresh_cache, topic_binding, monkeypatch
    ) -> None:
        calls: list[tuple] = []

        async def fake_enqueue(
            bot, user_id, window_id, text, *, thread_id=None, chat_id=None
        ):
            calls.append((user_id, text))

        monkeypatch.setattr(
            "ccmux_telegram.status_line.enqueue_status_update", fake_enqueue
        )

        await on_state("alpha", Working(status_text="Reading\u2026"), bot=_FakeBot())
        await on_state("alpha", Working(status_text="Writing\u2026"), bot=_FakeBot())

        assert calls == [(1, "Reading\u2026"), (1, "Writing\u2026")]

    @pytest.mark.asyncio
    async def test_watcher_receives_every_observation(
        self, fresh_cache, topic_binding, monkeypatch
    ) -> None:
        """Watcher must see every tick, not just state changes -- its
        dashboard needs the full observation stream."""
        observed: list[tuple] = []

        class _FakeService:
            def process(self, instance_id, state, *, topic):
                observed.append((instance_id, state))

        monkeypatch.setattr(
            "ccmux_telegram.watcher.get_service", lambda: _FakeService()
        )

        state = Idle()
        await on_state("alpha", state, bot=_FakeBot())
        await on_state("alpha", state, bot=_FakeBot())
        await on_state("alpha", state, bot=_FakeBot())

        assert len(observed) == 3
