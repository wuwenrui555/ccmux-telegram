"""Tests for tool_context — pending tool_use cache, TTL, and formatters."""

import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_pending():
    from ccmux_telegram import tool_context

    tool_context._PENDING.clear()
    yield
    tool_context._PENDING.clear()


def _entry(tool_name="Edit", tool_use_id="t1", input_data=None, recorded_at=None):
    from ccmux_telegram.tool_context import PendingToolContext

    return PendingToolContext(
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        input=input_data if input_data is not None else {"file_path": "/a.py"},
        recorded_at=recorded_at if recorded_at is not None else time.monotonic(),
    )


class TestCacheLifecycle:
    def test_record_and_get(self):
        from ccmux_telegram import tool_context

        e = _entry()
        tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        ).append(e)
        assert tool_context.get_pending("@1") is e

    def test_get_returns_none_when_absent(self):
        from ccmux_telegram import tool_context

        assert tool_context.get_pending("@missing") is None

    def test_clear_removes_matching_tool_use_id(self):
        from ccmux_telegram import tool_context

        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        dq.append(_entry(tool_use_id="t1"))
        dq.append(_entry(tool_use_id="t2"))
        tool_context.clear("@1", "t1")
        remaining = [e.tool_use_id for e in tool_context._PENDING["@1"]]
        assert remaining == ["t2"]

    def test_clear_noop_when_id_unknown(self):
        from ccmux_telegram import tool_context

        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        dq.append(_entry(tool_use_id="t1"))
        tool_context.clear("@1", "t-missing")
        assert len(tool_context._PENDING["@1"]) == 1

    def test_ttl_expiry_drops_stale_on_get(self):
        from ccmux_telegram import tool_context

        old = _entry(recorded_at=time.monotonic() - (tool_context._TTL_SECONDS + 5))
        fresh = _entry(tool_use_id="t2")
        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        dq.append(old)
        dq.append(fresh)
        result = tool_context.get_pending("@1")
        assert result is fresh

    def test_get_returns_newest_of_multiple_fresh(self):
        from ccmux_telegram import tool_context

        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        dq.append(_entry(tool_use_id="t1"))
        dq.append(_entry(tool_use_id="t2"))
        result = tool_context.get_pending("@1")
        assert result.tool_use_id == "t2"

    def test_cap_evicts_oldest(self):
        from ccmux_telegram import tool_context

        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        total = tool_context._MAX_PENDING + 2
        for i in range(total):
            dq.append(_entry(tool_use_id=f"t{i}"))
        ids = [e.tool_use_id for e in dq]
        assert len(ids) == tool_context._MAX_PENDING
        assert ids == [f"t{i}" for i in range(2, total)]


class TestRecordAllStale:
    def test_get_returns_none_when_all_stale(self):
        from ccmux_telegram import tool_context

        old = _entry(recorded_at=time.monotonic() - (tool_context._TTL_SECONDS + 5))
        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        dq.append(old)
        assert tool_context.get_pending("@1") is None


import json

from ccmux.api import ClaudeMessage


def _write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestRecordJsonl:
    @pytest.mark.asyncio
    async def test_record_populates_input_from_jsonl(self, tmp_path):
        from ccmux_telegram import tool_context

        session_id = "sess-1"
        window_id = "@1"
        encoded_cwd = "encoded-cwd"
        jsonl = tmp_path / encoded_cwd / f"{session_id}.jsonl"
        _write_jsonl(
            jsonl,
            [
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "cwd": "/tmp/proj",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tuse-42",
                                "name": "Edit",
                                "input": {
                                    "file_path": "/tmp/proj/a.py",
                                    "old_string": "old",
                                    "new_string": "new",
                                },
                            }
                        ]
                    },
                }
            ],
        )

        wb = MagicMock()
        wb.cwd = "/tmp/proj"

        backend = MagicMock()
        backend.get_window_binding.return_value = wb

        fake_encode = MagicMock(return_value=encoded_cwd)

        fake_config = MagicMock()
        fake_config.claude_projects_path = tmp_path

        with (
            patch.object(tool_context, "get_default_backend", return_value=backend),
            patch.object(
                tool_context, "WindowBindings", MagicMock(encode_cwd=fake_encode)
            ),
            patch.object(tool_context, "_backend_config", fake_config),
        ):
            msg = ClaudeMessage(
                session_id=session_id,
                role="assistant",
                content_type="tool_use",
                text="**Edit**(a.py)",
                tool_use_id="tuse-42",
                tool_name="Edit",
                is_complete=True,
            )
            await tool_context.record(msg, window_id)

        entry = tool_context.get_pending(window_id)
        assert entry is not None
        assert entry.tool_name == "Edit"
        assert entry.tool_use_id == "tuse-42"
        assert entry.input == {
            "file_path": "/tmp/proj/a.py",
            "old_string": "old",
            "new_string": "new",
        }

    @pytest.mark.asyncio
    async def test_record_tolerates_missing_file(self, tmp_path):
        from ccmux_telegram import tool_context

        wb = MagicMock()
        wb.cwd = "/tmp/proj"
        backend = MagicMock()
        backend.get_window_binding.return_value = wb
        fake_config = MagicMock()
        fake_config.claude_projects_path = tmp_path  # file does not exist

        with (
            patch.object(tool_context, "get_default_backend", return_value=backend),
            patch.object(
                tool_context,
                "WindowBindings",
                MagicMock(encode_cwd=MagicMock(return_value="nope")),
            ),
            patch.object(tool_context, "_backend_config", fake_config),
        ):
            msg = ClaudeMessage(
                session_id="missing-sess",
                role="assistant",
                content_type="tool_use",
                text="**Bash**(ls)",
                tool_use_id="tuse-99",
                tool_name="Bash",
                is_complete=True,
            )
            await tool_context.record(msg, "@1")

        entry = tool_context.get_pending("@1")
        assert entry is not None
        assert entry.tool_use_id == "tuse-99"
        assert entry.input is None

    @pytest.mark.asyncio
    async def test_record_tolerates_missing_tool_use_id_in_tail(self, tmp_path):
        from ccmux_telegram import tool_context

        session_id = "sess-2"
        encoded_cwd = "e2"
        jsonl = tmp_path / encoded_cwd / f"{session_id}.jsonl"
        _write_jsonl(
            jsonl,
            [
                {
                    "type": "assistant",
                    "sessionId": session_id,
                    "cwd": "/tmp/proj",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "other-id",
                                "name": "Read",
                                "input": {"file_path": "/x"},
                            }
                        ]
                    },
                }
            ],
        )

        wb = MagicMock()
        wb.cwd = "/tmp/proj"
        backend = MagicMock()
        backend.get_window_binding.return_value = wb
        fake_config = MagicMock()
        fake_config.claude_projects_path = tmp_path

        with (
            patch.object(tool_context, "get_default_backend", return_value=backend),
            patch.object(
                tool_context,
                "WindowBindings",
                MagicMock(encode_cwd=MagicMock(return_value=encoded_cwd)),
            ),
            patch.object(tool_context, "_backend_config", fake_config),
        ):
            msg = ClaudeMessage(
                session_id=session_id,
                role="assistant",
                content_type="tool_use",
                text="**Edit**(a.py)",
                tool_use_id="not-in-file",
                tool_name="Edit",
                is_complete=True,
            )
            await tool_context.record(msg, "@1")

        entry = tool_context.get_pending("@1")
        assert entry is not None
        assert entry.input is None

    @pytest.mark.asyncio
    async def test_record_skips_when_binding_missing(self, tmp_path):
        from ccmux_telegram import tool_context

        backend = MagicMock()
        backend.get_window_binding.return_value = None
        fake_config = MagicMock()
        fake_config.claude_projects_path = tmp_path

        with (
            patch.object(tool_context, "get_default_backend", return_value=backend),
            patch.object(tool_context, "_backend_config", fake_config),
        ):
            msg = ClaudeMessage(
                session_id="s",
                role="assistant",
                content_type="tool_use",
                text="**X**",
                tool_use_id="t1",
                tool_name="X",
                is_complete=True,
            )
            await tool_context.record(msg, "@nope")

        entry = tool_context.get_pending("@nope")
        assert entry is not None
        assert entry.input is None


class TestFormatInputForUi:
    def test_edit_renders_unified_diff(self):
        from ccmux_telegram.tool_context import format_input_for_ui

        text = format_input_for_ui(
            "Edit",
            {
                "file_path": "/tmp/proj/a.py",
                "old_string": "x = 1\ny = 2\n",
                "new_string": "x = 2\ny = 2\n",
            },
        )
        assert "/tmp/proj/a.py" in text
        assert "-x = 1" in text
        assert "+x = 2" in text
        assert text.lstrip().startswith(">") or "\n>" in text

    def test_notebook_edit_renders_diff_with_cell_id(self):
        from ccmux_telegram.tool_context import format_input_for_ui

        text = format_input_for_ui(
            "NotebookEdit",
            {
                "notebook_path": "/tmp/proj/a.ipynb",
                "cell_id": "c1",
                "old_string": "foo",
                "new_string": "bar",
            },
        )
        assert "/tmp/proj/a.ipynb" in text
        assert "c1" in text

    def test_write_renders_path_and_content_preview(self):
        from ccmux_telegram.tool_context import format_input_for_ui

        text = format_input_for_ui(
            "Write",
            {"file_path": "/tmp/x.txt", "content": "hello world\n" * 5},
        )
        assert "/tmp/x.txt" in text
        assert "hello world" in text

    def test_bash_renders_command_and_description(self):
        from ccmux_telegram.tool_context import format_input_for_ui

        text = format_input_for_ui(
            "Bash",
            {"command": "ls -la", "description": "list files"},
        )
        assert "ls -la" in text
        assert "list files" in text

    def test_bash_without_description(self):
        from ccmux_telegram.tool_context import format_input_for_ui

        text = format_input_for_ui("Bash", {"command": "pwd"})
        assert "pwd" in text

    def test_unknown_tool_falls_back_to_key_value(self):
        from ccmux_telegram.tool_context import format_input_for_ui

        text = format_input_for_ui(
            "SomethingNew",
            {"foo": "bar", "n": 7, "flag": True},
        )
        assert "foo" in text and "bar" in text
        assert "n" in text and "7" in text
        assert "flag" in text

    def test_none_input_returns_tool_name_only(self):
        from ccmux_telegram.tool_context import format_input_for_ui

        text = format_input_for_ui("Edit", None)
        assert "Edit" in text

    def test_non_string_values_are_coerced(self):
        from ccmux_telegram.tool_context import format_input_for_ui

        text = format_input_for_ui(
            "Edit",
            {"file_path": 42, "old_string": None, "new_string": ["a", "b"]},
        )
        assert "42" in text
