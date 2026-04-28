"""Tests for TopicRenamer: format + edit_forum_topic dispatch + caching."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError

from ccmux_telegram.topic_rename import TopicRenamer, desired_topic_name


class TestDesiredName:
    def test_alive_with_window(self) -> None:
        assert (
            desired_topic_name("daily", is_alive=True, window_id="@5")
            == "✅ | daily (@5)"
        )

    def test_dead_with_window(self) -> None:
        assert (
            desired_topic_name("daily", is_alive=False, window_id="@5")
            == "⚠️ | daily (@5)"
        )

    def test_alive_without_window_id(self) -> None:
        assert desired_topic_name("daily", is_alive=True, window_id="") == "✅ | daily"

    def test_dead_without_window_id(self) -> None:
        assert desired_topic_name("daily", is_alive=False, window_id="") == "⚠️ | daily"


class TestRenameDispatch:
    @pytest.mark.asyncio
    async def test_first_call_edits(self) -> None:
        renamer = TopicRenamer()
        bot = AsyncMock()
        await renamer.maybe_rename(
            bot,
            group_chat_id=-100,
            thread_id=10,
            session_name="daily",
            is_alive=True,
            window_id="@5",
        )
        bot.edit_forum_topic.assert_awaited_once_with(
            chat_id=-100, message_thread_id=10, name="✅ | daily (@5)"
        )

    @pytest.mark.asyncio
    async def test_second_call_with_same_state_skips(self) -> None:
        renamer = TopicRenamer()
        bot = AsyncMock()
        for _ in range(2):
            await renamer.maybe_rename(
                bot,
                group_chat_id=-100,
                thread_id=10,
                session_name="daily",
                is_alive=True,
                window_id="@5",
            )
        bot.edit_forum_topic.assert_awaited_once()  # second was a cache hit

    @pytest.mark.asyncio
    async def test_window_id_change_triggers_rename(self) -> None:
        renamer = TopicRenamer()
        bot = AsyncMock()
        await renamer.maybe_rename(
            bot,
            group_chat_id=-100,
            thread_id=10,
            session_name="daily",
            is_alive=True,
            window_id="@5",
        )
        await renamer.maybe_rename(
            bot,
            group_chat_id=-100,
            thread_id=10,
            session_name="daily",
            is_alive=True,
            window_id="@17",
        )
        assert bot.edit_forum_topic.await_count == 2
        # Second call had the new name.
        last = bot.edit_forum_topic.await_args_list[1].kwargs
        assert last["name"] == "✅ | daily (@17)"

    @pytest.mark.asyncio
    async def test_alive_to_dead_triggers_rename(self) -> None:
        renamer = TopicRenamer()
        bot = AsyncMock()
        await renamer.maybe_rename(
            bot,
            group_chat_id=-100,
            thread_id=10,
            session_name="daily",
            is_alive=True,
            window_id="@5",
        )
        await renamer.maybe_rename(
            bot,
            group_chat_id=-100,
            thread_id=10,
            session_name="daily",
            is_alive=False,
            window_id="@5",
        )
        assert bot.edit_forum_topic.await_count == 2
        last = bot.edit_forum_topic.await_args_list[1].kwargs
        assert last["name"] == "⚠️ | daily (@5)"

    @pytest.mark.asyncio
    async def test_topic_not_modified_is_silent_and_caches(self) -> None:
        """Telegram returns Topic_not_modified when the name already matches.
        TopicRenamer should swallow the error and cache so future ticks skip."""
        renamer = TopicRenamer()
        bot = AsyncMock()
        bot.edit_forum_topic.side_effect = [
            BadRequest("Topic_not_modified"),
            None,  # would be called if cache miss
        ]
        await renamer.maybe_rename(
            bot,
            group_chat_id=-100,
            thread_id=10,
            session_name="daily",
            is_alive=True,
            window_id="@5",
        )
        # Second call: same desired name. Cache hit, no second API call.
        await renamer.maybe_rename(
            bot,
            group_chat_id=-100,
            thread_id=10,
            session_name="daily",
            is_alive=True,
            window_id="@5",
        )
        bot.edit_forum_topic.assert_awaited_once()


class TestRenameAutoUnbindOnDeletedTopic:
    @pytest.fixture(autouse=True)
    def _isolate_topics(self, monkeypatch, tmp_path):
        from ccmux_telegram.topic_bindings import TopicBindings

        fresh = TopicBindings(state_file=tmp_path / "topic_bindings.json")
        monkeypatch.setattr("ccmux_telegram.runtime.topics", fresh)
        return fresh

    @pytest.mark.asyncio
    async def test_thread_not_found_triggers_auto_unbind(self, _isolate_topics) -> None:
        _isolate_topics.bind(
            user_id=1, thread_id=10, group_chat_id=-100, session_name="daily"
        )
        renamer = TopicRenamer()
        bot = AsyncMock()
        bot.edit_forum_topic.side_effect = BadRequest("Message thread not found")

        await renamer.maybe_rename(
            bot,
            group_chat_id=-100,
            thread_id=10,
            session_name="daily",
            is_alive=True,
            window_id="@5",
        )

        # Binding should have been auto-unbound.
        assert _isolate_topics.get(user_id=1, thread_id=10) is None


class TestRenameNonBadRequestErrors:
    @pytest.mark.asyncio
    async def test_network_error_logs_no_cache(self, caplog) -> None:
        renamer = TopicRenamer()
        bot = AsyncMock()
        bot.edit_forum_topic.side_effect = NetworkError("connection reset")
        with caplog.at_level("ERROR"):
            await renamer.maybe_rename(
                bot,
                group_chat_id=-100,
                thread_id=10,
                session_name="daily",
                is_alive=True,
                window_id="@5",
            )
        # Not cached: a retry on next tick should call edit again.
        bot.edit_forum_topic.side_effect = None
        bot.edit_forum_topic.reset_mock()
        await renamer.maybe_rename(
            bot,
            group_chat_id=-100,
            thread_id=10,
            session_name="daily",
            is_alive=True,
            window_id="@5",
        )
        bot.edit_forum_topic.assert_awaited_once()
