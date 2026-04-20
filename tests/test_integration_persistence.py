"""Integration tests for state-file persistence.

Exercise TopicBindings against a real filesystem path (no mocks). Cheap
enough to run by default — no external services touched, just tmpfs.

Marked `integration` to keep them sortable from pure-logic tests; they
still run in the default test selection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccmux_telegram.topic_bindings import TopicBindings

pytestmark = pytest.mark.integration


def test_bind_persists_and_round_trips(tmp_path: Path) -> None:
    state_file = tmp_path / "topic_bindings.json"

    binder = TopicBindings(state_file=state_file)
    binder.bind(user_id=1, thread_id=10, session_name="alpha", group_chat_id=-100)
    binder.bind(user_id=1, thread_id=11, session_name="beta", group_chat_id=-100)

    # File on disk should reflect both entries.
    # Schema: {user_id: {thread_id: {"tmux_session_name", "group_chat_id"}}}
    assert state_file.exists()
    raw = json.loads(state_file.read_text())
    user_state = raw["1"]
    assert user_state["10"]["tmux_session_name"] == "alpha"
    assert user_state["11"]["tmux_session_name"] == "beta"

    # Fresh instance pointed at the same file should see the same bindings
    reloaded = TopicBindings(state_file=state_file)
    assert reloaded.get(1, 10).session_name == "alpha"
    assert reloaded.get(1, 11).session_name == "beta"


def test_unbind_removes_entry_and_persists(tmp_path: Path) -> None:
    state_file = tmp_path / "topic_bindings.json"

    binder = TopicBindings(state_file=state_file)
    binder.bind(user_id=1, thread_id=10, session_name="alpha", group_chat_id=-100)
    binder.unbind(user_id=1, thread_id=10)

    assert binder.get(1, 10) is None

    reloaded = TopicBindings(state_file=state_file)
    assert reloaded.get(1, 10) is None


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    """atomic_write_json should not leave .tmp files behind on success."""
    state_file = tmp_path / "topic_bindings.json"

    binder = TopicBindings(state_file=state_file)
    for tid in range(20):
        binder.bind(user_id=1, thread_id=tid, session_name=f"s{tid}", group_chat_id=-1)

    leftovers = list(tmp_path.glob(".topic_bindings.json.*"))
    assert leftovers == [], f"temp files leaked: {leftovers}"


def test_corrupt_state_file_starts_fresh(tmp_path: Path) -> None:
    """A malformed JSON state file should not crash startup."""
    state_file = tmp_path / "topic_bindings.json"
    state_file.write_text("{ this is not valid json")

    # Should not raise
    binder = TopicBindings(state_file=state_file)
    assert binder.get(1, 10) is None
