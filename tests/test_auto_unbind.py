"""Tests for auto_unbind: detection + cleanup of deleted-topic bindings.

When Telegram returns ``BadRequest: message thread not found`` (the
only signal that a forum topic was deleted), the helper must:

- match the error narrowly (do not unbind on other BadRequest variants)
- remove the matching ``(group_chat_id, thread_id)`` row(s) from
  ``topic_bindings.json``
- leave unrelated bindings alone
"""

from __future__ import annotations

from pathlib import Path

import pytest
from telegram.error import BadRequest, NetworkError

from ccmux_telegram import auto_unbind


@pytest.fixture
def isolated_topics(monkeypatch, tmp_path: Path):
    """Replace ``ccmux_telegram.runtime.topics`` with a fresh instance
    backed by a temp ``topic_bindings.json``.
    """
    from ccmux_telegram.topic_bindings import TopicBindings

    state_file = tmp_path / "topic_bindings.json"
    fresh = TopicBindings(state_file=state_file)
    monkeypatch.setattr("ccmux_telegram.runtime.topics", fresh)
    return fresh


# ---------- predicate ----------


class TestIsThreadDeletedError:
    def test_message_thread_not_found(self) -> None:
        assert auto_unbind.is_thread_deleted_error(
            BadRequest("Message thread not found")
        )

    def test_topic_not_found_underscore(self) -> None:
        assert auto_unbind.is_thread_deleted_error(BadRequest("Topic_not_found"))

    def test_topic_not_found_space(self) -> None:
        assert auto_unbind.is_thread_deleted_error(BadRequest("topic not found"))

    def test_other_bad_request_not_matched(self) -> None:
        assert not auto_unbind.is_thread_deleted_error(BadRequest("Chat not found"))
        assert not auto_unbind.is_thread_deleted_error(BadRequest("Topic_not_modified"))

    def test_non_bad_request_not_matched(self) -> None:
        assert not auto_unbind.is_thread_deleted_error(NetworkError("connection reset"))
        assert not auto_unbind.is_thread_deleted_error(RuntimeError("oops"))


# ---------- maybe_unbind ----------


class TestMaybeUnbind:
    def test_no_op_on_unrelated_error(self, isolated_topics) -> None:
        isolated_topics.bind(
            user_id=1, thread_id=10, group_chat_id=-100, session_name="alpha"
        )
        result = auto_unbind.maybe_unbind(
            NetworkError("timeout"), chat_id=-100, thread_id=10
        )
        assert result is False
        assert isolated_topics.get(user_id=1, thread_id=10) is not None

    def test_no_op_when_no_matching_binding(self, isolated_topics) -> None:
        result = auto_unbind.maybe_unbind(
            BadRequest("Message thread not found"),
            chat_id=-100,
            thread_id=999,
        )
        assert result is False

    def test_removes_matching_binding(self, isolated_topics) -> None:
        isolated_topics.bind(
            user_id=1, thread_id=10, group_chat_id=-100, session_name="alpha"
        )
        result = auto_unbind.maybe_unbind(
            BadRequest("Message thread not found"),
            chat_id=-100,
            thread_id=10,
        )
        assert result is True
        assert isolated_topics.get(user_id=1, thread_id=10) is None

    def test_leaves_other_threads_in_same_chat_alone(self, isolated_topics) -> None:
        isolated_topics.bind(
            user_id=1, thread_id=10, group_chat_id=-100, session_name="alpha"
        )
        isolated_topics.bind(
            user_id=1, thread_id=20, group_chat_id=-100, session_name="beta"
        )
        auto_unbind.maybe_unbind(
            BadRequest("Message thread not found"),
            chat_id=-100,
            thread_id=10,
        )
        assert isolated_topics.get(user_id=1, thread_id=10) is None
        assert isolated_topics.get(user_id=1, thread_id=20) is not None

    def test_leaves_same_thread_in_other_chat_alone(self, isolated_topics) -> None:
        isolated_topics.bind(
            user_id=1, thread_id=10, group_chat_id=-100, session_name="alpha"
        )
        isolated_topics.bind(
            user_id=1, thread_id=10, group_chat_id=-200, session_name="beta"
        )
        auto_unbind.maybe_unbind(
            BadRequest("Message thread not found"),
            chat_id=-100,
            thread_id=10,
        )
        # Same (thread_id, user_id) pair would only support one entry in
        # the underlying dict; the second .bind() above overwrote the first.
        # Sanity-check that the chat=-200 entry survives.
        survivor = isolated_topics.get(user_id=1, thread_id=10)
        assert survivor is not None
        assert survivor.group_chat_id == -200

    def test_chat_or_thread_none_returns_false(self, isolated_topics) -> None:
        assert (
            auto_unbind.maybe_unbind(
                BadRequest("Message thread not found"),
                chat_id=None,
                thread_id=10,
            )
            is False
        )
        assert (
            auto_unbind.maybe_unbind(
                BadRequest("Message thread not found"),
                chat_id=-100,
                thread_id=None,
            )
            is False
        )


# ---------- TopicBindings.unbind_by_thread direct ----------


class TestUnbindByThread:
    def test_returns_empty_when_no_match(self, isolated_topics) -> None:
        removed = isolated_topics.unbind_by_thread(group_chat_id=-100, thread_id=999)
        assert removed == []

    def test_removes_one(self, isolated_topics) -> None:
        isolated_topics.bind(
            user_id=1, thread_id=10, group_chat_id=-100, session_name="alpha"
        )
        removed = isolated_topics.unbind_by_thread(group_chat_id=-100, thread_id=10)
        assert len(removed) == 1
        assert removed[0].session_name == "alpha"
        assert removed[0].thread_id == 10

    def test_persists_after_remove(self, isolated_topics, tmp_path) -> None:
        from ccmux_telegram.topic_bindings import TopicBindings

        isolated_topics.bind(
            user_id=1, thread_id=10, group_chat_id=-100, session_name="alpha"
        )
        isolated_topics.unbind_by_thread(group_chat_id=-100, thread_id=10)

        # Re-load from disk; the row must be gone.
        reloaded = TopicBindings(state_file=tmp_path / "topic_bindings.json")
        assert reloaded.get(user_id=1, thread_id=10) is None
