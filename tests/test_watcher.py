"""Tests for the watcher feature (group-topic dashboard).

Covers:
  - TopicBindings watcher set/get/clear/is_watcher
  - classify() with ClaudeState variants
  - WatcherService.process() state machine
  - tick() aggregates topics + delivers to user's watcher topic (group chat,
    specific thread_id)
  - Fresh send vs edit: fresh when bell added or its preview changed
  - Cross-chat deep-link format (3-segment with recent message id)
  - /watcher command toggles current topic as watcher
  - on_source_closed: drops entry; clears registration if it IS the watcher
  - Dead watcher topic -> auto-clear
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccmux.api import Blocked, BlockedUI, Dead, Idle, Working

from ccmux_telegram import watcher as W
from ccmux_telegram.topic_bindings import TopicBinding, TopicBindings


def _make_topic(
    *,
    user_id: int = 1,
    thread_id: int = 42,
    group_chat_id: int = -1001234567890,
    session_name: str = "proj",
) -> TopicBinding:
    return TopicBinding(
        user_id=user_id,
        thread_id=thread_id,
        group_chat_id=group_chat_id,
        window_id="@5",
        session_name=session_name,
    )


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------


class TestClassify:
    def test_working(self) -> None:
        assert W.classify(Working(status_text="Reading\u2026")) == "working"

    def test_idle(self) -> None:
        assert W.classify(Idle()) == "waiting"

    def test_blocked(self) -> None:
        assert (
            W.classify(
                Blocked(
                    ui=BlockedUI.PERMISSION_PROMPT, content="Do you want to proceed?"
                )
            )
            == "waiting"
        )

    def test_dead(self) -> None:
        assert W.classify(Dead()) == "resuming"


# ---------------------------------------------------------------------------
# TopicBindings watcher
# ---------------------------------------------------------------------------


class TestTopicBindingsWatcher:
    def test_set_get_clear_is_watcher(self, tmp_path):
        tb = TopicBindings(state_file=tmp_path / "s.json")
        assert tb.get_watcher(1) is None
        assert not tb.is_watcher(1, 7)
        tb.set_watcher(1, -1001, 7)
        assert tb.get_watcher(1) == (-1001, 7)
        assert tb.is_watcher(1, 7)
        assert not tb.is_watcher(1, 99)

        # Persistence
        tb2 = TopicBindings(state_file=tmp_path / "s.json")
        assert tb2.get_watcher(1) == (-1001, 7)

        tb2.clear_watcher(1)
        tb3 = TopicBindings(state_file=tmp_path / "s.json")
        assert tb3.get_watcher(1) is None


# ---------------------------------------------------------------------------
# WatcherService.process() state machine
# ---------------------------------------------------------------------------


class TestProcessStateMachine:
    def test_working_clears_waiting_timer(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ccmux_telegram.watcher._topics.is_watcher",
            lambda uid, tid: False,
        )
        svc = W.WatcherService()
        t = _make_topic()
        svc.process("proj", Idle(), topic=t)
        key = (t.user_id, t.thread_id)
        assert svc._entries[key].current_state == "waiting"
        assert svc._entries[key].first_waiting_at is not None

        svc.process("proj", Working(status_text="Thinking\u2026"), topic=t)
        assert svc._entries[key].current_state == "working"
        assert svc._entries[key].first_waiting_at is None

    def test_idle_starts_waiting_timer(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ccmux_telegram.watcher._topics.is_watcher",
            lambda uid, tid: False,
        )
        svc = W.WatcherService()
        t = _make_topic()
        before = time.monotonic()
        svc.process("proj", Idle(), topic=t)
        entry = svc._entries[(t.user_id, t.thread_id)]
        assert entry.current_state == "waiting"
        assert entry.first_waiting_at is not None
        assert entry.first_waiting_at >= before

    def test_blocked_also_sets_waiting(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ccmux_telegram.watcher._topics.is_watcher",
            lambda uid, tid: False,
        )
        svc = W.WatcherService()
        t = _make_topic()
        svc.process(
            "proj",
            Blocked(ui=BlockedUI.ASK_USER_QUESTION, content="?"),
            topic=t,
        )
        assert svc._entries[(t.user_id, t.thread_id)].current_state == "waiting"

    def test_dead_treated_as_waiting_for_debounce(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ccmux_telegram.watcher._topics.is_watcher",
            lambda uid, tid: False,
        )
        svc = W.WatcherService()
        t = _make_topic()
        svc.process("proj", Dead(), topic=t)
        # Collapses into "waiting" bucket so the debounce timer arms.
        assert svc._entries[(t.user_id, t.thread_id)].current_state == "waiting"
        assert svc._entries[(t.user_id, t.thread_id)].first_waiting_at is not None

    def test_no_topic_is_dropped(self) -> None:
        svc = W.WatcherService()
        svc.process("proj", Idle(), topic=None)
        assert svc._entries == {}

    def test_watcher_own_topic_is_skipped(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "ccmux_telegram.watcher._topics.is_watcher",
            lambda uid, tid: True,
        )
        svc = W.WatcherService()
        svc.process("proj", Idle(), topic=_make_topic())
        assert svc._entries == {}

    def test_waiting_after_working(self) -> None:
        svc = W.WatcherService()
        topic = _make_topic()
        with patch.object(W._topics, "is_watcher", return_value=False):
            svc.process("proj", Working(status_text="Thinking\u2026 (1s)"), topic=topic)
            svc.process("proj", Idle(), topic=topic)
        entry = svc._entries[(1, 42)]
        assert entry.current_state == "waiting"
        assert entry.first_waiting_at is not None

    def test_watcher_topic_not_tracked(self) -> None:
        svc = W.WatcherService()
        topic = _make_topic()
        with patch.object(W._topics, "is_watcher", return_value=True):
            svc.process("proj", Idle(), topic=topic)
        assert svc._entries == {}


# ---------------------------------------------------------------------------
# Dashboard (delivered to group watcher topic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWatcherDashboard:
    async def test_no_dashboard_if_no_watcher_registered(self):
        svc = W.WatcherService()
        bot = AsyncMock()
        topic = _make_topic()
        with (
            patch.object(W._topics, "is_watcher", return_value=False),
            patch.object(W._topics, "all", side_effect=lambda: iter([topic])),
            patch.object(W._topics, "get_watcher", return_value=None),
        ):
            svc.process("proj", Idle(), topic=topic)
            svc._entries[(1, 42)].first_waiting_at = (
                time.monotonic() - W.DEBOUNCE_SECONDS - 1
            )
            await svc.tick(bot)
        bot.send_message.assert_not_called()

    async def test_dashboard_sent_to_watcher_topic(self):
        svc = W.WatcherService()
        sent = MagicMock()
        sent.message_id = 777
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=sent)
        topic = _make_topic()
        with (
            patch.object(W._topics, "is_watcher", return_value=False),
            patch.object(W._topics, "all", side_effect=lambda: iter([topic])),
            patch.object(W._topics, "get_watcher", return_value=(-1001, 9)),
            patch.object(
                W, "_fetch_last_assistant_preview", AsyncMock(return_value="hi")
            ),
        ):
            svc.process("proj", Working(status_text="Thinking\u2026 (1s)"), topic=topic)
            svc.process("proj", Idle(), topic=topic)
            svc._entries[(1, 42)].first_waiting_at = (
                time.monotonic() - W.DEBOUNCE_SECONDS - 1
            )
            await svc.tick(bot)
        bot.send_message.assert_awaited_once()
        call = bot.send_message.await_args
        assert call.kwargs["chat_id"] == -1001
        assert call.kwargs["message_thread_id"] == 9
        assert call.kwargs["parse_mode"] == "HTML"
        assert "🔔" in call.kwargs["text"]

    async def test_edit_when_only_working_rearrangement(self):
        svc = W.WatcherService()
        sent = MagicMock()
        sent.message_id = 777
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=sent)
        bot.edit_message_text = AsyncMock()
        topic = _make_topic()
        with (
            patch.object(W._topics, "is_watcher", return_value=False),
            patch.object(W._topics, "all", side_effect=lambda: iter([topic])),
            patch.object(W._topics, "get_watcher", return_value=(-1001, 9)),
            patch.object(
                W, "_fetch_last_assistant_preview", AsyncMock(return_value="hi")
            ),
        ):
            svc.process("proj", Working(status_text="Thinking\u2026 (1s)"), topic=topic)
            svc.process("proj", Idle(), topic=topic)
            svc._entries[(1, 42)].first_waiting_at = (
                time.monotonic() - W.DEBOUNCE_SECONDS - 1
            )
            await svc.tick(bot)
            # Back to working -> bell removed
            svc.process("proj", Working(status_text="Thinking\u2026 (1s)"), topic=topic)
            await svc.tick(bot)
        assert bot.send_message.await_count == 1
        assert bot.edit_message_text.await_count == 1

    async def test_preview_change_within_waiting_edits_silently(self):
        # Preview text changing while the same bell stays waiting should NOT
        # re-ping (silent edit). Re-ping only on a new tid appearing.
        svc = W.WatcherService()
        sent1 = MagicMock()
        sent1.message_id = 100
        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=[sent1])
        topic = _make_topic()
        with (
            patch.object(W._topics, "is_watcher", return_value=False),
            patch.object(W._topics, "all", side_effect=lambda: iter([topic])),
            patch.object(W._topics, "get_watcher", return_value=(-1001, 9)),
            patch.object(
                W, "_fetch_last_assistant_preview", AsyncMock(return_value="first")
            ),
        ):
            svc.process("proj", Working(status_text="Thinking\u2026 (1s)"), topic=topic)
            svc.process("proj", Idle(), topic=topic)
            svc._entries[(1, 42)].first_waiting_at = (
                time.monotonic() - W.DEBOUNCE_SECONDS - 1
            )
            await svc.tick(bot)
        with (
            patch.object(W._topics, "is_watcher", return_value=False),
            patch.object(W._topics, "all", side_effect=lambda: iter([topic])),
            patch.object(W._topics, "get_watcher", return_value=(-1001, 9)),
            patch.object(
                W, "_fetch_last_assistant_preview", AsyncMock(return_value="second")
            ),
        ):
            await svc.tick(bot)
        assert bot.send_message.await_count == 1
        assert bot.edit_message_text.await_count == 1
        bot.delete_message.assert_not_awaited()

    async def test_dashboard_deleted_when_no_rows(self):
        svc = W.WatcherService()
        bot = AsyncMock()
        svc._dashboards[1] = W._Dashboard(message_id=777, last_rendered="...")
        with (
            patch.object(W._topics, "all", return_value=iter([])),
            patch.object(W._topics, "get_watcher", return_value=(-1001, 9)),
        ):
            await svc.tick(bot)
        bot.delete_message.assert_awaited_once_with(chat_id=-1001, message_id=777)

    async def test_dead_watcher_topic_auto_clears(self):
        from telegram.error import BadRequest

        svc = W.WatcherService()
        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=BadRequest("Message thread not found"))
        topic = _make_topic()
        mock_clear = MagicMock()
        with (
            patch.object(W._topics, "is_watcher", return_value=False),
            patch.object(W._topics, "all", side_effect=lambda: iter([topic])),
            patch.object(W._topics, "get_watcher", return_value=(-1001, 9)),
            patch.object(W._topics, "clear_watcher", mock_clear),
            patch.object(
                W, "_fetch_last_assistant_preview", AsyncMock(return_value="hi")
            ),
        ):
            svc.process("proj", Working(status_text="Thinking\u2026 (1s)"), topic=topic)
            svc.process("proj", Idle(), topic=topic)
            svc._entries[(1, 42)].first_waiting_at = (
                time.monotonic() - W.DEBOUNCE_SECONDS - 1
            )
            await svc.tick(bot)
        mock_clear.assert_called_once_with(1)

    async def test_on_source_closed_drops_entry(self):
        svc = W.WatcherService()
        svc._entries[(1, 42)] = W._SourceEntry(source_thread_id=42)
        bot = AsyncMock()
        with patch.object(W._topics, "get_watcher", return_value=(-1001, 9)):
            await svc.on_source_closed(bot, user_id=1, source_thread_id=42)
        assert (1, 42) not in svc._entries

    async def test_on_source_closed_clears_if_watcher_itself(self):
        svc = W.WatcherService()
        bot = AsyncMock()
        mock_clear = MagicMock()
        with (
            patch.object(W._topics, "get_watcher", return_value=(-1001, 42)),
            patch.object(W._topics, "clear_watcher", mock_clear),
        ):
            await svc.on_source_closed(bot, user_id=1, source_thread_id=42)
        mock_clear.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# Deep-link
# ---------------------------------------------------------------------------


class TestDeepLink:
    def test_fallback_thread_id(self):
        url = W._build_topic_deeplink(-1001234567890, 42)
        assert url == "https://t.me/c/1234567890/42/42"

    def test_with_msg_id(self):
        url = W._build_topic_deeplink(-1001234567890, 42, message_id=9999)
        assert url == "https://t.me/c/1234567890/42/9999"


# ---------------------------------------------------------------------------
# /watcher command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWatcherCommand:
    async def test_register_current_topic(self):
        user = MagicMock()
        user.id = 1
        msg = MagicMock()
        msg.chat.id = -1001
        update = MagicMock()
        update.effective_user = user
        update.message = msg
        with (
            patch("ccmux_telegram.util.is_user_allowed", return_value=True),
            patch("ccmux_telegram.util.get_thread_id", return_value=7),
            patch.object(W._topics, "get", return_value=None),
            patch.object(W._topics, "get_watcher", return_value=None),
            patch.object(W._topics, "set_watcher") as mock_set,
            patch(
                "ccmux_telegram.watcher.safe_reply", new_callable=AsyncMock
            ) as mock_reply,
        ):
            await W.watcher_command(update, MagicMock())
        mock_set.assert_called_once_with(1, -1001, 7)
        mock_reply.assert_awaited_once()

    async def test_toggle_off_same_topic(self):
        user = MagicMock()
        user.id = 1
        msg = MagicMock()
        msg.chat.id = -1001
        update = MagicMock()
        update.effective_user = user
        update.message = msg
        with (
            patch("ccmux_telegram.util.is_user_allowed", return_value=True),
            patch("ccmux_telegram.util.get_thread_id", return_value=7),
            patch.object(W._topics, "get", return_value=None),
            patch.object(W._topics, "get_watcher", return_value=(-1001, 7)),
            patch.object(W._topics, "clear_watcher") as mock_clear,
            patch("ccmux_telegram.watcher.safe_reply", new_callable=AsyncMock),
        ):
            await W.watcher_command(update, MagicMock())
        mock_clear.assert_called_once_with(1)

    async def test_refuses_in_bound_topic(self):
        user = MagicMock()
        user.id = 1
        msg = MagicMock()
        msg.chat.id = -1001
        update = MagicMock()
        update.effective_user = user
        update.message = msg
        existing = _make_topic(thread_id=7, session_name="foo")
        with (
            patch("ccmux_telegram.util.is_user_allowed", return_value=True),
            patch("ccmux_telegram.util.get_thread_id", return_value=7),
            patch.object(W._topics, "get", return_value=existing),
            patch.object(W._topics, "set_watcher") as mock_set,
            patch("ccmux_telegram.watcher.safe_reply", new_callable=AsyncMock),
        ):
            await W.watcher_command(update, MagicMock())
        mock_set.assert_not_called()


# ---------------------------------------------------------------------------
# binding_flow refuses to bind watcher topic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_unbound_topic_refuses_watcher():
    from ccmux_telegram.binding_flow import handle_unbound_topic

    user = MagicMock()
    user.id = 1
    update = MagicMock()
    update.message = MagicMock()
    context = MagicMock()
    context.user_data = {}
    with (
        patch("ccmux_telegram.binding_flow._topics.is_watcher", return_value=True),
        patch(
            "ccmux_telegram.binding_flow.safe_reply", new_callable=AsyncMock
        ) as mock_reply,
    ):
        await handle_unbound_topic(update, context, user, thread_id=7, text="hi")
    mock_reply.assert_awaited_once()
