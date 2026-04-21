"""Tests for TopicBindings, WindowBindings, and the runtime join helpers.

Replaces the previous facade tests: the behaviour once covered by the
removed compat facade now lives in two separate classes
(`TopicBindings`, `WindowBindings`) plus a handful of join helpers in
`ccmux.runtime`. These tests carry forward the behaviour coverage.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ccmux.api import ClaudeInstanceRegistry as WindowBindings
from ccmux_telegram.topic_bindings import TopicBinding, TopicBindings


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Provide a temporary config directory with the two state files.

    Returns a dict with `state_file`, `bindings_file`, and
    `claude_projects_path` paths.
    """
    state_file = tmp_path / "topic_bindings.json"
    bindings_file = tmp_path / "window_bindings.json"
    claude_projects_path = tmp_path / "projects"
    claude_projects_path.mkdir()

    mock_config = MagicMock()
    mock_config.state_file = state_file
    mock_config.bindings_file = bindings_file
    mock_config.claude_projects_path = claude_projects_path
    mock_config.config_dir = tmp_path

    monkeypatch.setattr("ccmux_telegram.topic_bindings.config", mock_config)
    # ccmux.window_bindings no longer exists; ClaudeInstanceRegistry does not
    # use a config file path — this patch is a no-op placeholder for B5.
    # monkeypatch.setattr("ccmux.window_bindings.config", mock_config)

    return {
        "state_file": state_file,
        "bindings_file": bindings_file,
        "claude_projects_path": claude_projects_path,
    }


@pytest.fixture
def topics(tmp_config) -> TopicBindings:
    return TopicBindings()


@pytest.fixture
def windows(tmp_config) -> WindowBindings:
    return WindowBindings()


def _write_state(state_file: Path, data: dict) -> None:
    state_file.write_text(json.dumps(data))


def _write_bindings_file(bindings_file: Path, data: dict) -> None:
    bindings_file.write_text(json.dumps(data))


def _make_binding_entry(
    window_id: str = "@1",
    session_id: str = "aaaa0000-0000-0000-0000-000000000000",
    cwd: str = "/tmp",
) -> dict:
    return {"window_id": window_id, "session_id": session_id, "cwd": cwd}


def _join(topic: TopicBinding, windows: WindowBindings) -> TopicBinding:
    """Replicate runtime._join_window_id for test assertions."""
    info = windows.get_by_session_name(topic.session_name)
    if info is None or not info.window_id:
        return topic
    return TopicBinding(
        user_id=topic.user_id,
        thread_id=topic.thread_id,
        group_chat_id=topic.group_chat_id,
        window_id=info.window_id,
        session_name=topic.session_name,
    )


# ---------------------------------------------------------------------------
# TopicBindings: state file read/write
# ---------------------------------------------------------------------------


class TestTopicBindingsReadStateFile:
    def test_empty_file(self, tmp_config) -> None:
        _write_state(tmp_config["state_file"], {})
        t = TopicBindings()
        assert t._raw_state == {}

    def test_file_not_exists(self, tmp_config) -> None:
        t = TopicBindings()
        assert t._raw_state == {}

    def test_valid_state(self, tmp_config) -> None:
        _write_state(
            tmp_config["state_file"],
            {
                "100": {
                    "1": {"tmux_session_name": "aclf", "group_chat_id": -999},
                    "2": {"tmux_session_name": "daily", "group_chat_id": -888},
                }
            },
        )
        t = TopicBindings()
        assert len(t._raw_state) == 2
        assert t._raw_state[(100, 1)] == ("aclf", -999)
        assert t._raw_state[(100, 2)] == ("daily", -888)

    def test_skips_missing_session_name(self, tmp_config) -> None:
        _write_state(
            tmp_config["state_file"],
            {"100": {"1": {"group_chat_id": -999}}},
        )
        t = TopicBindings()
        assert t._raw_state == {}

    def test_skips_missing_group_chat_id(self, tmp_config) -> None:
        _write_state(
            tmp_config["state_file"],
            {"100": {"1": {"tmux_session_name": "aclf"}}},
        )
        t = TopicBindings()
        assert t._raw_state == {}

    def test_skips_non_int_group_chat_id(self, tmp_config) -> None:
        _write_state(
            tmp_config["state_file"],
            {"100": {"1": {"tmux_session_name": "aclf", "group_chat_id": "bad"}}},
        )
        t = TopicBindings()
        assert t._raw_state == {}

    def test_skips_invalid_user_key(self, tmp_config) -> None:
        _write_state(
            tmp_config["state_file"],
            {
                "not_a_number": {
                    "1": {"tmux_session_name": "aclf", "group_chat_id": -999},
                }
            },
        )
        t = TopicBindings()
        assert t._raw_state == {}

    def test_skips_invalid_thread_key(self, tmp_config) -> None:
        _write_state(
            tmp_config["state_file"],
            {
                "100": {
                    "not_a_number": {
                        "tmux_session_name": "aclf",
                        "group_chat_id": -999,
                    },
                }
            },
        )
        t = TopicBindings()
        assert t._raw_state == {}

    def test_skips_non_dict_topics(self, tmp_config) -> None:
        _write_state(tmp_config["state_file"], {"100": "bad"})
        t = TopicBindings()
        assert t._raw_state == {}

    def test_corrupt_json(self, tmp_config) -> None:
        tmp_config["state_file"].write_text("{broken")
        t = TopicBindings()
        assert t._raw_state == {}


class TestTopicBindingsSaveStateFile:
    def test_saves_correct_format(self, topics, tmp_config) -> None:
        topics._raw_state[(100, 1)] = ("aclf", -999)
        topics._raw_state[(100, 2)] = ("daily", -888)
        topics._save_state_file()

        data = json.loads(tmp_config["state_file"].read_text())
        assert data["100"]["1"] == {
            "tmux_session_name": "aclf",
            "group_chat_id": -999,
        }
        assert data["100"]["2"] == {
            "tmux_session_name": "daily",
            "group_chat_id": -888,
        }

    def test_saves_empty(self, topics, tmp_config) -> None:
        topics._save_state_file()
        data = json.loads(tmp_config["state_file"].read_text())
        assert data == {}


# ---------------------------------------------------------------------------
# WindowBindings: session_map file read
# ---------------------------------------------------------------------------


class TestSessionMapReadSessionMapFile:
    def test_empty_file(self, tmp_config) -> None:
        _write_bindings_file(tmp_config["bindings_file"], {})
        w = WindowBindings()
        assert w.raw == {}

    def test_file_not_exists(self, tmp_config) -> None:
        w = WindowBindings()
        assert w.raw == {}

    def test_valid_session_map(self, tmp_config) -> None:
        _write_bindings_file(
            tmp_config["bindings_file"],
            {"aclf": _make_binding_entry("@4", "sid-aclf", "/home/aclf")},
        )
        w = WindowBindings()
        assert "aclf" in w._data
        assert w._data["aclf"]["window_id"] == "@4"

    def test_skips_non_dict_entries(self, tmp_config) -> None:
        _write_bindings_file(
            tmp_config["bindings_file"],
            {"good": _make_binding_entry(), "bad": "not a dict"},
        )
        w = WindowBindings()
        assert "good" in w._data
        assert "bad" not in w._data

    def test_corrupt_json(self, tmp_config) -> None:
        tmp_config["bindings_file"].write_text("{broken")
        w = WindowBindings()
        assert w.raw == {}


# ---------------------------------------------------------------------------
# Join: topic + window -> TopicBinding with window_id populated
# ---------------------------------------------------------------------------


class TestTopicWindowJoin:
    def test_live_join(self, tmp_config) -> None:
        _write_state(
            tmp_config["state_file"],
            {"100": {"1": {"tmux_session_name": "aclf", "group_chat_id": -999}}},
        )
        _write_bindings_file(
            tmp_config["bindings_file"],
            {"aclf": _make_binding_entry("@4", "sid-aclf", "/home/aclf")},
        )
        topics = TopicBindings()
        windows = WindowBindings()

        topic = topics.get(100, 1)
        assert topic is not None
        assert topic.session_name == "aclf"
        assert topic.window_id == ""  # unjoined
        joined = _join(topic, windows)
        assert joined.window_id == "@4"

        info = windows.get_by_session_name("aclf")
        assert info is not None
        assert info.claude_session_id == "sid-aclf"
        assert info.cwd == "/home/aclf"

    def test_pending_when_session_map_missing(self, tmp_config) -> None:
        _write_state(
            tmp_config["state_file"],
            {"100": {"1": {"tmux_session_name": "aclf", "group_chat_id": -999}}},
        )
        topics = TopicBindings()
        windows = WindowBindings()

        topic = topics.get(100, 1)
        assert topic is not None
        assert topic.session_name == "aclf"
        joined = _join(topic, windows)
        assert joined.window_id == ""
        assert topics.all_session_names() == {"aclf"}


# ---------------------------------------------------------------------------
# WindowBindings.load / is_session_in_map
# ---------------------------------------------------------------------------


class TestSessionMapLoad:
    @pytest.mark.asyncio
    async def test_reload_picks_up_new_entry(self, windows, tmp_config) -> None:
        _write_bindings_file(
            tmp_config["bindings_file"],
            {"aclf": _make_binding_entry("@4", "sid-aclf")},
        )
        await windows.load()
        info = windows.get_by_session_name("aclf")
        assert info is not None
        assert info.window_id == "@4"


class TestIsSessionInMap:
    def test_present_and_complete(self, windows, tmp_config) -> None:
        _write_bindings_file(
            tmp_config["bindings_file"],
            {"aclf": _make_binding_entry()},
        )
        windows._read()
        assert windows.is_session_in_map("aclf") is True

    def test_not_present(self, windows) -> None:
        assert windows.is_session_in_map("nonexistent") is False

    def test_incomplete_entry(self, windows, tmp_config) -> None:
        _write_bindings_file(
            tmp_config["bindings_file"],
            {"aclf": {"window_id": "", "session_id": "", "cwd": ""}},
        )
        windows._read()
        assert windows.is_session_in_map("aclf") is False


# ---------------------------------------------------------------------------
# TopicBindings.bind / unbind
# ---------------------------------------------------------------------------


class TestBind:
    def test_bind_returns_topic_with_empty_window_id(self, topics) -> None:
        result = topics.bind(100, 1, "aclf", -999)
        assert result.session_name == "aclf"
        assert result.window_id == ""
        assert result.group_chat_id == -999

    def test_bind_persists_to_raw_state(self, topics) -> None:
        topics.bind(100, 1, "aclf", -999)
        assert (100, 1) in topics._raw_state
        assert topics._raw_state[(100, 1)] == ("aclf", -999)

    def test_bind_overwrites_existing(self, topics) -> None:
        topics.bind(100, 1, "aclf", -999)
        topics.bind(100, 1, "daily", -888)

        result = topics.get(100, 1)
        assert result is not None
        assert result.session_name == "daily"

    def test_bind_saves_state_file(self, topics, tmp_config) -> None:
        topics.bind(100, 1, "aclf", -999)
        data = json.loads(tmp_config["state_file"].read_text())
        assert data["100"]["1"]["tmux_session_name"] == "aclf"


class TestUnbind:
    def test_unbind_returns_prior_binding(self, topics) -> None:
        topics.bind(100, 1, "aclf", -999)
        result = topics.unbind(100, 1)

        assert result is not None
        assert result.session_name == "aclf"
        assert topics.get(100, 1) is None

    def test_unbind_nonexistent(self, topics) -> None:
        assert topics.unbind(100, 999) is None

    def test_unbind_saves_state_file(self, topics, tmp_config) -> None:
        topics.bind(100, 1, "aclf", -999)
        topics.unbind(100, 1)
        data = json.loads(tmp_config["state_file"].read_text())
        assert "1" not in data.get("100", {})


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


class TestGetBindings:
    @pytest.fixture(autouse=True)
    def _setup(self, topics, windows, tmp_config) -> None:
        _write_bindings_file(
            tmp_config["bindings_file"],
            {"aclf": _make_binding_entry("@4", "sid-aclf")},
        )
        windows._read()
        topics.bind(100, 1, "aclf", -999)

    def test_get_by_thread(self, topics) -> None:
        assert topics.get(100, 1) is not None

    def test_get_by_thread_not_found(self, topics) -> None:
        assert topics.get(100, 999) is None

    def test_get_by_thread_none_thread_id(self, topics) -> None:
        assert topics.get(100, None) is None


class TestIterAndNames:
    def test_iter(self, topics, windows, tmp_config) -> None:
        _write_bindings_file(
            tmp_config["bindings_file"],
            {
                "aclf": _make_binding_entry("@4", "sid-aclf"),
                "daily": _make_binding_entry("@3", "sid-daily"),
            },
        )
        windows._read()
        topics.bind(100, 1, "aclf", -999)
        topics.bind(100, 2, "daily", -888)

        names = {b.session_name for b in topics.all()}
        assert names == {"aclf", "daily"}

    def test_iter_includes_pending(self, topics) -> None:
        topics.bind(100, 1, "aclf", -999)
        bindings = list(topics.all())
        assert len(bindings) == 1
        assert bindings[0].session_name == "aclf"
        assert bindings[0].window_id == ""

    def test_all_session_names_includes_pending(self, topics) -> None:
        topics.bind(100, 1, "aclf", -999)
        assert topics.all_session_names() == {"aclf"}


# ---------------------------------------------------------------------------
# TopicBindings.is_alive
# ---------------------------------------------------------------------------


class TestAliveStatus:
    def test_pending_binding_without_window_id_is_alive(self, topics) -> None:
        """A topic bound before the hook fires has no window_id; optimistic True."""
        topics.bind(100, 1, "aclf", -999)
        topic = topics.get(100, 1)
        assert topic is not None
        assert topic.window_id == ""
        assert topics.is_alive(topic) is True

    def test_is_alive_delegates_to_backend(self, topics) -> None:
        """With a window_id, is_alive consults runtime.windows.is_window_alive."""
        from ccmux_telegram import runtime

        topics.bind(100, 1, "aclf", -999)
        base = topics.get(100, 1)
        assert base is not None
        topic = TopicBinding(
            user_id=base.user_id,
            thread_id=base.thread_id,
            group_chat_id=base.group_chat_id,
            window_id="@7",
            session_name=base.session_name,
        )

        with patch.object(runtime, "is_window_alive", return_value=False):
            assert topics.is_alive(topic) is False
        with patch.object(runtime, "is_window_alive", return_value=True):
            assert topics.is_alive(topic) is True
