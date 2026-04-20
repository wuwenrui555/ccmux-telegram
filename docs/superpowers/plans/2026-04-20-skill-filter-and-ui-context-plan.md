# Skill Body Filter & Interactive UI Context Injection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **User preference:** Use `executing-plans-test-first` (per `~/.claude/CLAUDE.md`) in place of `superpowers:executing-plans`.

**Goal:** Stop flooding the Telegram chat with Skill tool bodies, and inject the pending tool input (file path, diff, command) into permission-prompt UI messages so the user can decide without scrolling.

**Architecture:** Two frontend-only features in `ccmux-telegram`. Feature A adds an env-gated suppression branch in `message_in.handle_new_message`. Feature B adds a new module `tool_context.py` that caches the last N pending tool_use inputs per window (read on-demand from the session's JSONL tail via public `TranscriptParser`) and prepends a formatted context block to permission-prompt Telegram messages. Backend `ccmux-backend` is not changed.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio, `python-telegram-bot`, `aiofiles`, `ccmux>=1.0,<2.0` (public `TranscriptParser`, `WindowBindings.encode_cwd`, `get_default_backend`).

---

## File Structure

### New files

- `src/ccmux_telegram/tool_context.py` — per-window pending tool_use cache + JSONL tail reader + per-tool input formatter.
- `tests/test_tool_context.py` — unit coverage of cache lifecycle, TTL, parallel tool_use handling, JSONL parse, and each formatter branch.
- `tests/test_message_in_skill_filter.py` — unit coverage of the Skill tool_result gate in `handle_new_message`.
- `tests/test_prompt_context_injection.py` — integration: seed cache, fire a permission-prompt pane, assert the Telegram `send_message` text contains both the context block and the pane UI.

### Modified files

- `src/ccmux_telegram/config.py` — add `show_skill_bodies` from `CCMUX_SHOW_SKILL_BODIES` (default `False`).
- `src/ccmux_telegram/message_in.py` — call `tool_context.record` / `tool_context.clear` in the ClaudeMessage path; add Skill gate after existing gates.
- `src/ccmux_telegram/prompt.py` — in `handle_interactive_ui`, prepend the formatted tool-context block before sending or editing the Telegram message.
- `README.md` — document the new env var and the UI-context behavior.
- `CHANGELOG.md` — add an unreleased entry.

### Files NOT touched

- `ccmux-backend` (API is frozen at v1.0).
- `markdown.py`, `sender.py`, `message_queue.py` — reuse existing blockquote rendering and split-message behavior.
- `prompt_state.py` — interactive-mode tracking unchanged.

---

## Task 1: Config flag `show_skill_bodies`

**Files:**
- Test: `tests/test_config.py` (append)
- Modify: `src/ccmux_telegram/config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py` inside the existing `TestConfigValid` class (keep `@pytest.mark.usefixtures("_base_env")` covering it):

```python
    def test_show_skill_bodies_default_false(self):
        cfg = Config()
        assert cfg.show_skill_bodies is False

    def test_show_skill_bodies_true(self, monkeypatch):
        monkeypatch.setenv("CCMUX_SHOW_SKILL_BODIES", "true")
        cfg = Config()
        assert cfg.show_skill_bodies is True

    def test_show_skill_bodies_false_explicit(self, monkeypatch):
        monkeypatch.setenv("CCMUX_SHOW_SKILL_BODIES", "false")
        cfg = Config()
        assert cfg.show_skill_bodies is False
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_config.py -k show_skill_bodies -v
```

Expected: three FAILs — `AttributeError: 'Config' object has no attribute 'show_skill_bodies'`.

- [ ] **Step 3: Implement — add the flag**

In `src/ccmux_telegram/config.py`, inside `Config.__init__` right after the `self.show_thinking = ...` line, insert:

```python
        self.show_skill_bodies = (
            os.getenv("CCMUX_SHOW_SKILL_BODIES", "").lower() == "true"
        )
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/test_config.py -k show_skill_bodies -v
```

Expected: three PASS.

- [ ] **Step 5: Commit**

```
git add src/ccmux_telegram/config.py tests/test_config.py
git commit -m "feat(config): add CCMUX_SHOW_SKILL_BODIES flag"
```

---

## Task 2: Skill tool_result gate in `handle_new_message`

**Files:**
- Create test: `tests/test_message_in_skill_filter.py`
- Modify: `src/ccmux_telegram/message_in.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_message_in_skill_filter.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_message_in_skill_filter.py -v
```

Expected: all four FAIL — Skill tool_result currently goes through `enqueue_content_message`.

- [ ] **Step 3: Implement — add Skill gate**

Open `src/ccmux_telegram/message_in.py`. Locate the existing gate block (currently around line 190–200):

```python
    # Skip tool call notifications when CCMUX_SHOW_TOOL_CALLS=false
    if not config.show_tool_calls and msg.content_type in (
        "tool_use",
        "tool_result",
    ):
        return

    # Skip thinking blocks when CCMUX_SHOW_THINKING=false. Currently
    # these blocks have empty content (CC only records a signature),
    # so suppressing them just removes a placeholder.
    if not config.show_thinking and msg.content_type == "thinking":
        return
```

Immediately after that `show_thinking` gate, append:

```python
    # Skip Skill tool_result bodies when CCMUX_SHOW_SKILL_BODIES=false.
    # The Skill tool_use summary is preserved; only the full skill body
    # (tool_result) is suppressed to avoid flooding the chat.
    if (
        not config.show_skill_bodies
        and msg.content_type == "tool_result"
        and msg.tool_name == "Skill"
    ):
        return
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/test_message_in_skill_filter.py -v
```

Expected: all four PASS.

- [ ] **Step 5: Run the full test suite to check no regressions**

```
uv run pytest -q
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```
git add src/ccmux_telegram/message_in.py tests/test_message_in_skill_filter.py
git commit -m "feat(message_in): gate Skill tool_result bodies behind CCMUX_SHOW_SKILL_BODIES"
```

---

## Task 3: `tool_context` module — cache skeleton + TTL

**Files:**
- Create: `src/ccmux_telegram/tool_context.py`
- Create test: `tests/test_tool_context.py`

This task sets up the data structures and synchronous cache operations (record without JSONL read, get, clear, TTL, per-window cap). JSONL reading is added in Task 4. Formatters are added in Task 5.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tool_context.py`:

```python
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
        tool_context._PENDING.setdefault("@1", tool_context.collections.deque(
            maxlen=tool_context._MAX_PENDING
        )).append(e)
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
        # First two ids (t0, t1) evicted; deque starts at t2.
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
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_tool_context.py -v
```

Expected: collection error — `ModuleNotFoundError: No module named 'ccmux_telegram.tool_context'`.

- [ ] **Step 3: Implement — create module skeleton**

Create `src/ccmux_telegram/tool_context.py`:

```python
"""Pending tool_use context cache for permission-prompt UI injection.

When Claude emits a tool_use ClaudeMessage, `record(...)` reads the
full tool `input` dict from the session's JSONL tail and caches it
keyed by window_id. When a PermissionPrompt or BashApproval UI fires
in the pane, `prompt.handle_interactive_ui` calls `get_pending(...)`
to retrieve the most-recent non-stale entry and prepends its
formatted representation to the Telegram message.

Per-window cache is a bounded deque; old entries auto-evict and
entries older than `_TTL_SECONDS` are ignored by `get_pending`.
"""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_TTL_SECONDS = 60.0
_MAX_PENDING = 5
_JSONL_TAIL_BYTES = 64 * 1024


@dataclass
class PendingToolContext:
    tool_name: str
    tool_use_id: str
    input: dict | None
    recorded_at: float  # time.monotonic()


# window_id -> deque of PendingToolContext (newest last)
_PENDING: dict[str, "collections.deque[PendingToolContext]"] = {}


def _deque_for(window_id: str) -> "collections.deque[PendingToolContext]":
    dq = _PENDING.get(window_id)
    if dq is None:
        dq = collections.deque(maxlen=_MAX_PENDING)
        _PENDING[window_id] = dq
    return dq


def get_pending(window_id: str) -> PendingToolContext | None:
    """Return the most-recently recorded non-stale entry, or None."""
    dq = _PENDING.get(window_id)
    if not dq:
        return None
    cutoff = time.monotonic() - _TTL_SECONDS
    for entry in reversed(dq):
        if entry.recorded_at >= cutoff:
            return entry
    return None


def clear(window_id: str, tool_use_id: str) -> None:
    """Remove the matching entry from the window's cache, if present."""
    dq = _PENDING.get(window_id)
    if not dq:
        return
    remaining = [e for e in dq if e.tool_use_id != tool_use_id]
    dq.clear()
    dq.extend(remaining)
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/test_tool_context.py -v
```

Expected: all eight tests PASS.

- [ ] **Step 5: Commit**

```
git add src/ccmux_telegram/tool_context.py tests/test_tool_context.py
git commit -m "feat(tool_context): add pending tool_use cache with TTL and per-window cap"
```

---

## Task 4: `tool_context.record` — async JSONL tail lookup

**Files:**
- Modify: `src/ccmux_telegram/tool_context.py`
- Modify: `tests/test_tool_context.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tool_context.py`:

```python
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
            patch.object(tool_context, "WindowBindings", MagicMock(encode_cwd=fake_encode)),
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

        # Still records with input=None so UI injection can at least show tool_name.
        entry = tool_context.get_pending("@nope")
        assert entry is not None
        assert entry.input is None
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_tool_context.py::TestRecordJsonl -v
```

Expected: all four FAIL — `record` coroutine does not exist.

- [ ] **Step 3: Implement — add `record` and JSONL tail reader**

Edit `src/ccmux_telegram/tool_context.py`. At the top, extend imports and add the module-level aliases:

```python
from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiofiles

from ccmux.api import ClaudeMessage, TranscriptParser, WindowBindings, get_default_backend
from ccmux.config import config as _backend_config
```

(Keep the existing `_TTL_SECONDS`, `_MAX_PENDING`, `_JSONL_TAIL_BYTES`, dataclass, and `_PENDING` block unchanged. Re-order imports to alphabetical.)

Append these helpers below `clear`:

```python
async def _read_jsonl_tail(path: Path) -> list[dict]:
    """Parse up to the last `_JSONL_TAIL_BYTES` of `path` into JSONL dicts."""
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        start = max(0, size - _JSONL_TAIL_BYTES)
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            if start > 0:
                await f.seek(start)
                # Drop the partial first line.
                await f.readline()
            text = await f.read()
    except OSError as e:
        logger.debug("tool_context: failed to read %s: %s", path, e)
        return []

    entries: list[dict] = []
    for line in text.splitlines():
        parsed = TranscriptParser.parse_line(line)
        if parsed is not None:
            entries.append(parsed)
    return entries


def _find_tool_use_input(entries: list[dict], tool_use_id: str) -> dict | None:
    """Scan JSONL entries newest-first for a tool_use with matching id."""
    for entry in reversed(entries):
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            if block.get("id") != tool_use_id:
                continue
            inp = block.get("input")
            return inp if isinstance(inp, dict) else None
    return None


def _resolve_jsonl_path(window_id: str, session_id: str) -> Path | None:
    try:
        backend = get_default_backend()
    except RuntimeError:
        return None
    wb = backend.get_window_binding(window_id)
    if wb is None:
        return None
    try:
        encoded = WindowBindings.encode_cwd(wb.cwd)
    except Exception as e:
        logger.debug("tool_context: encode_cwd failed for %s: %s", wb.cwd, e)
        return None
    return Path(_backend_config.claude_projects_path) / encoded / f"{session_id}.jsonl"


async def record(msg: ClaudeMessage, window_id: str) -> None:
    """Cache a pending tool_use with its full `input` dict read from JSONL.

    Safe to call on every tool_use event. On any failure (missing binding,
    missing file, malformed JSONL, tool_use_id absent from the tail), the
    entry is still stored with `input=None` so the UI injection path has
    at least the tool_name to display.
    """
    if msg.content_type != "tool_use" or not msg.tool_use_id or not msg.tool_name:
        return

    input_data: dict | None = None
    path = _resolve_jsonl_path(window_id, msg.session_id)
    if path is not None:
        entries = await _read_jsonl_tail(path)
        input_data = _find_tool_use_input(entries, msg.tool_use_id)

    entry = PendingToolContext(
        tool_name=msg.tool_name,
        tool_use_id=msg.tool_use_id,
        input=input_data,
        recorded_at=time.monotonic(),
    )
    _deque_for(window_id).append(entry)
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/test_tool_context.py -v
```

Expected: all twelve tests PASS.

- [ ] **Step 5: Commit**

```
git add src/ccmux_telegram/tool_context.py tests/test_tool_context.py
git commit -m "feat(tool_context): async record reading tool_use input from JSONL tail"
```

---

## Task 5: `tool_context.format_input_for_ui` — per-tool formatters

**Files:**
- Modify: `src/ccmux_telegram/tool_context.py`
- Modify: `tests/test_tool_context.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tool_context.py`:

```python
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
        # Unified diff markers present.
        assert "- x = 1" in text or ">- x = 1" in text
        assert "+ x = 2" in text or ">+ x = 2" in text
        # Wrapped in expandable blockquote.
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
        # Should at least indicate the tool name.
        assert "Edit" in text

    def test_non_string_values_are_coerced(self):
        from ccmux_telegram.tool_context import format_input_for_ui

        text = format_input_for_ui(
            "Edit",
            {"file_path": 42, "old_string": None, "new_string": ["a", "b"]},
        )
        assert "42" in text  # no crash
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_tool_context.py::TestFormatInputForUi -v
```

Expected: all eight FAIL — `format_input_for_ui` does not exist.

- [ ] **Step 3: Implement — add formatter**

At the top of `src/ccmux_telegram/tool_context.py`, add these imports next to the existing ones:

```python
import difflib
from typing import Any
```

Then append to the end of the same file:

```python
_VALUE_CLIP = 600


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _clip(text: str, limit: int = _VALUE_CLIP) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _as_blockquote(text: str) -> str:
    if not text:
        return ""
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


def _format_edit(input_dict: dict) -> str:
    file_path = _coerce_str(input_dict.get("file_path") or input_dict.get("notebook_path"))
    cell_id = input_dict.get("cell_id")
    header = f"**Edit** `{file_path}`"
    if cell_id:
        header += f" (cell `{_coerce_str(cell_id)}`)"

    old = _coerce_str(input_dict.get("old_string")).splitlines(keepends=False)
    new = _coerce_str(input_dict.get("new_string")).splitlines(keepends=False)
    diff_lines = list(
        difflib.unified_diff(old, new, fromfile="old", tofile="new", lineterm="")
    )
    diff = "\n".join(diff_lines) if diff_lines else "(no diff)"
    diff = _clip(diff)
    return header + "\n" + _as_blockquote(diff)


def _format_write(input_dict: dict) -> str:
    file_path = _coerce_str(input_dict.get("file_path"))
    content = _clip(_coerce_str(input_dict.get("content")))
    header = f"**Write** `{file_path}`"
    body = _as_blockquote(content) if content else ""
    return header + ("\n" + body if body else "")


def _format_bash(input_dict: dict) -> str:
    command = _coerce_str(input_dict.get("command"))
    description = _coerce_str(input_dict.get("description"))
    lines = [f"**Bash** `{_clip(command, 200)}`"]
    if description:
        lines.append(_clip(description, 200))
    return "\n".join(lines)


def _format_fallback(tool_name: str, input_dict: dict) -> str:
    items: list[str] = []
    for k, v in input_dict.items():
        items.append(f"{k}: {_clip(_coerce_str(v), 200)}")
    body = "\n".join(items)
    body = _clip(body)
    return f"**{tool_name}**\n" + _as_blockquote(body)


def format_input_for_ui(tool_name: str, input_dict: dict | None) -> str:
    """Render a tool's input as Markdown for injection into a permission UI message.

    Output is standard Markdown: a short header line plus (for long values)
    a `>` blockquote region. The Telegram send layer turns contiguous
    blockquotes into expandable quotes.
    """
    if input_dict is None:
        return f"**{tool_name}**"

    try:
        if tool_name in ("Edit", "NotebookEdit"):
            return _format_edit(input_dict)
        if tool_name == "Write":
            return _format_write(input_dict)
        if tool_name == "Bash":
            return _format_bash(input_dict)
        return _format_fallback(tool_name, input_dict)
    except Exception as e:
        logger.debug("format_input_for_ui failed for %s: %s", tool_name, e)
        return f"**{tool_name}**"
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/test_tool_context.py::TestFormatInputForUi -v
```

Expected: all eight PASS.

- [ ] **Step 5: Run full module tests**

```
uv run pytest tests/test_tool_context.py -v
```

Expected: all twenty tests PASS.

- [ ] **Step 6: Commit**

```
git add src/ccmux_telegram/tool_context.py tests/test_tool_context.py
git commit -m "feat(tool_context): add per-tool input formatter for UI injection"
```

---

## Task 6: Wire `tool_context` into `message_in.handle_new_message`

**Files:**
- Modify: `src/ccmux_telegram/message_in.py`
- Modify: `tests/test_message_in_skill_filter.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_message_in_skill_filter.py`:

```python
class TestToolContextWiring:
    @pytest.mark.asyncio
    async def test_tool_use_calls_record(self, _patch_config):
        from ccmux_telegram import message_in

        msg = ClaudeMessage(
            session_id="s1",
            role="assistant",
            content_type="tool_use",
            text="**Edit**(a.py)",
            tool_use_id="t1",
            tool_name="Edit",
            is_complete=True,
        )
        with (
            patch.object(
                message_in,
                "get_topic_for_claude_session",
                return_value=_make_topic(),
            ),
            patch.object(message_in, "enqueue_content_message", new=AsyncMock()),
            patch.object(message_in, "tool_context") as tc,
        ):
            tc.record = AsyncMock()
            tc.clear = MagicMock()
            await message_in.handle_new_message(msg, AsyncMock())

        tc.record.assert_awaited_once()
        args, _ = tc.record.call_args
        assert args[0] is msg
        assert args[1] == "@5"

    @pytest.mark.asyncio
    async def test_tool_result_calls_clear(self, _patch_config):
        from ccmux_telegram import message_in

        msg = ClaudeMessage(
            session_id="s1",
            role="assistant",
            content_type="tool_result",
            text="  ⎿  Read 10 lines",
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
            patch.object(message_in, "tool_context") as tc,
        ):
            tc.record = AsyncMock()
            tc.clear = MagicMock()
            await message_in.handle_new_message(msg, AsyncMock())

        tc.clear.assert_called_once_with("@5", "t1")

    @pytest.mark.asyncio
    async def test_text_message_does_not_touch_tool_context(self, _patch_config):
        from ccmux_telegram import message_in

        msg = ClaudeMessage(
            session_id="s1",
            role="assistant",
            content_type="text",
            text="hello",
            is_complete=True,
        )
        with (
            patch.object(
                message_in,
                "get_topic_for_claude_session",
                return_value=_make_topic(),
            ),
            patch.object(message_in, "enqueue_content_message", new=AsyncMock()),
            patch.object(message_in, "tool_context") as tc,
        ):
            tc.record = AsyncMock()
            tc.clear = MagicMock()
            await message_in.handle_new_message(msg, AsyncMock())

        tc.record.assert_not_called()
        tc.clear.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_message_in_skill_filter.py::TestToolContextWiring -v
```

Expected: all three FAIL — `message_in` does not yet import or call `tool_context`.

- [ ] **Step 3: Implement — wire calls**

Edit `src/ccmux_telegram/message_in.py`. Add import:

```python
from . import tool_context
```

(near the other local imports, e.g. next to `from .prompt import ...`).

Inside `handle_new_message`, after the existing `PROMPT_TOOL_NAMES` block (around line 168–181) and before the "Any non-interactive message means the interaction is complete" comment, insert:

```python
    # Maintain pending tool_use cache for permission-prompt UI injection.
    if msg.content_type == "tool_use" and msg.tool_use_id and msg.tool_name:
        await tool_context.record(msg, wid)
    elif msg.content_type == "tool_result" and msg.tool_use_id:
        tool_context.clear(wid, msg.tool_use_id)
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/test_message_in_skill_filter.py -v
```

Expected: all seven (four earlier + three new) PASS.

- [ ] **Step 5: Run full test suite**

```
uv run pytest -q
```

Expected: green.

- [ ] **Step 6: Commit**

```
git add src/ccmux_telegram/message_in.py tests/test_message_in_skill_filter.py
git commit -m "feat(message_in): populate tool_context cache on tool_use/tool_result events"
```

---

## Task 7: Inject context into `handle_interactive_ui`

**Files:**
- Modify: `src/ccmux_telegram/prompt.py`
- Create test: `tests/test_prompt_context_injection.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_prompt_context_injection.py`:

```python
"""handle_interactive_ui must prepend cached tool_context to the UI message."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_state():
    from ccmux_telegram import tool_context
    from ccmux_telegram.prompt_state import _interactive_mode, _interactive_msgs

    tool_context._PENDING.clear()
    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    tool_context._PENDING.clear()
    _interactive_mode.clear()
    _interactive_msgs.clear()


def _seed_edit_cache(window_id: str):
    from ccmux_telegram import tool_context

    dq = tool_context._deque_for(window_id)
    dq.append(
        tool_context.PendingToolContext(
            tool_name="Edit",
            tool_use_id="tu-1",
            input={
                "file_path": "/tmp/proj/a.py",
                "old_string": "x = 1",
                "new_string": "x = 2",
            },
            recorded_at=time.monotonic(),
        )
    )


@pytest.mark.asyncio
async def test_permission_prompt_prepends_edit_context(sample_pane_permission):
    from ccmux_telegram.prompt import handle_interactive_ui

    window_id = "@5"
    _seed_edit_cache(window_id)

    mock_bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 321
    mock_bot.send_message.return_value = sent

    mock_window = MagicMock()
    mock_window.window_id = window_id

    with (
        patch("ccmux_telegram.prompt.tmux_registry") as mock_registry,
        patch("ccmux_telegram.prompt.get_topic"),
    ):
        mock_tm = MagicMock()
        mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tm.capture_pane = AsyncMock(return_value=sample_pane_permission)
        mock_registry.get_by_window_id.return_value = mock_tm

        result = await handle_interactive_ui(
            mock_bot, user_id=1, window_id=window_id, thread_id=42, chat_id=100
        )

    assert result is True
    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    # Context (file path + diff tokens) appears before the pane content.
    assert "/tmp/proj/a.py" in sent_text
    assert "Do you want to proceed?" in sent_text
    assert sent_text.index("/tmp/proj/a.py") < sent_text.index("Do you want to proceed?")


@pytest.mark.asyncio
async def test_no_cache_behavior_unchanged(sample_pane_permission):
    """When cache is empty, handle_interactive_ui sends just the pane UI."""
    from ccmux_telegram.prompt import handle_interactive_ui

    window_id = "@7"
    mock_bot = AsyncMock()
    sent = MagicMock()
    sent.message_id = 321
    mock_bot.send_message.return_value = sent

    mock_window = MagicMock()
    mock_window.window_id = window_id

    with (
        patch("ccmux_telegram.prompt.tmux_registry") as mock_registry,
        patch("ccmux_telegram.prompt.get_topic"),
    ):
        mock_tm = MagicMock()
        mock_tm.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tm.capture_pane = AsyncMock(return_value=sample_pane_permission)
        mock_registry.get_by_window_id.return_value = mock_tm

        result = await handle_interactive_ui(
            mock_bot, user_id=1, window_id=window_id, thread_id=42, chat_id=100
        )

    assert result is True
    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    assert "Do you want to proceed?" in sent_text
    # No Edit header injected when cache is empty.
    assert "**Edit**" not in sent_text
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/test_prompt_context_injection.py -v
```

Expected: `test_permission_prompt_prepends_edit_context` FAILs (context not injected); `test_no_cache_behavior_unchanged` PASSes (current behavior).

- [ ] **Step 3: Implement — prepend cached context**

Edit `src/ccmux_telegram/prompt.py`. Add import near other local imports:

```python
from . import tool_context
```

Inside `handle_interactive_ui`, locate the block:

```python
    # Build message with navigation keyboard
    keyboard = _build_interactive_keyboard(window_id, ui_name=content.name)

    # Send as plain text (no markdown conversion)
    text = content.content
```

Replace the two "Send as plain text" lines with:

```python
    # Send as plain text (no markdown conversion)
    text = content.content

    # Prepend cached tool_context (Edit diff, Bash command, etc.) so the
    # user can decide without scrolling back for the preceding tool_use.
    cached = tool_context.get_pending(window_id)
    if cached is not None:
        header = tool_context.format_input_for_ui(cached.tool_name, cached.input)
        if header:
            text = f"{header}\n\n{text}"
```

- [ ] **Step 4: Run test to verify it passes**

```
uv run pytest tests/test_prompt_context_injection.py -v
```

Expected: both tests PASS.

- [ ] **Step 5: Run full test suite**

```
uv run pytest -q
```

Expected: green. Previously passing `test_prompt.py::test_handle_settings_ui_sends_keyboard` continues to pass (settings UI has no cached tool_use, so no injection).

- [ ] **Step 6: Commit**

```
git add src/ccmux_telegram/prompt.py tests/test_prompt_context_injection.py
git commit -m "feat(prompt): inject pending tool_use context into permission UI messages"
```

---

## Task 8: Docs — README + CHANGELOG

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update README env var section**

Open `README.md`. Locate the section listing env vars (search for `CCMUX_SHOW_THINKING`). Add after it:

```markdown
- `CCMUX_SHOW_SKILL_BODIES` (default `false`) — when `true`, the full
  body of a `Skill` tool result is forwarded to Telegram. Default off
  so skill invocations do not flood the chat; the `Skill(name)` tool-use
  summary is always shown.
```

In the same file, add a short note in the behavior section (or wherever permission prompts are discussed; if no such section, append to the end of the "Features" / overview section):

```markdown
Permission prompts (file edits, `Bash` approvals, etc.) include the
pending tool's input inline — file path + unified diff for edits,
command + description for Bash — so you can approve or reject without
scrolling back for context.
```

- [ ] **Step 2: Update CHANGELOG**

Open `CHANGELOG.md`. Directly under `# Changelog` (before the first released section), insert:

```markdown
## [Unreleased]

### Added
- `CCMUX_SHOW_SKILL_BODIES` env flag (default `false`) that suppresses
  `Skill` tool_result bodies so Skill invocations do not flood the chat.
- Permission-prompt and bash-approval Telegram messages now include the
  pending tool's input (file path, unified diff, command, or key/value
  dump) inline, fetched from the session JSONL on demand.
```

- [ ] **Step 3: Commit**

```
git add README.md CHANGELOG.md
git commit -m "docs: document Skill body gate and permission-UI context injection"
```

---

## Task 9: Final verification

- [ ] **Step 1: Run full suite quietly**

```
uv run pytest -q
```

Expected: all green, no xfail/xpass surprises.

- [ ] **Step 2: Run linter**

```
uv run ruff check src tests
```

Expected: clean.

- [ ] **Step 3: Type-check**

```
uv run pyright src
```

Expected: no new errors vs. baseline. (If pyright was not previously clean, compare delta only.)

- [ ] **Step 4: Smoke-verify by importing**

```
uv run python -c "from ccmux_telegram import tool_context, message_in, prompt, config; print('ok')"
```

Expected: `ok`.

- [ ] **Step 5: Branch summary**

```
git log --oneline feature/ui-clarity ^dev
```

Expected: the commits from Tasks 1–8 (plus the earlier `docs: add spec ...` commit).

---

## Out of scope (deferred)

- Moving the Skill stats line (`⎿ Skill loaded (N lines)`) into the backend `_format_tool_result_text`. Requires a backend change; current design does not need it.
- Collapsing the Skill tool_result with an expandable-blockquote variant that keeps a small preview. Can be added later as a third value for `CCMUX_SHOW_SKILL_BODIES` (e.g. `preview`).
- Covering `AskUserQuestion` / `ExitPlanMode` prompts with context injection. Those already carry their own descriptive content.
