<!-- markdownlint-disable MD025 MD029 MD031 MD032 MD056 -->

# Binding Self-Heal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: per the user's preferences (see `~/.claude/CLAUDE.md`), use **`executing-plans-test-first`** to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Use `managing-git-branches` for any branch / merge / release / tag operations.

**Goal:** Stop the bot from saying "Binding to X is not alive" when the only problem is a stale `window_id` in `claude_instances.json`. Backend gains a pure `reconcile_instance` API and an in-memory override layer; frontend uses it on startup, on `/rebind_window`, and as a periodic transition detector that posts `✅ Binding to X recovered` when an external fix takes effect.

**Architecture:** Backend exposes capability, frontend orchestrates. `claude_instances.json` stays hook-owned — no new file writers. Reconcile results live only in `ClaudeInstanceRegistry._overrides` (process memory). The hook's existing `_resolve_session_via_pid` is lifted into a shared `pid_session_resolver` module and made portable on macOS so reconcile can identify Claude windows on either platform.

**Tech stack:** Python 3.12, libtmux, python-telegram-bot, python-dotenv, asyncio, pyright, pytest, ruff. `uv` for tool installs and dev environments. git-flow.

**Spec:** `ccmux-telegram/docs/superpowers/specs/2026-04-27-binding-self-heal-design.md`

**Repos:**
- Backend: `~/projects/ccmux/ccmux-backend` (currently `dev` is at v3.0.2)
- Frontend: `~/projects/ccmux/ccmux-telegram` (currently on `feature/binding-self-heal` where this plan and the spec live)

**Phasing:** Phase 1 (backend) ships v3.1.0 first; Phase 2 (telegram) bumps its `ccmux` dep to `>=3.1.0,<4.0.0` and ships v3.2.0.

---

## File structure

### Backend (`ccmux-backend`)

| File | Action | Responsibility |
|---|---|---|
| `src/ccmux/pid_session_resolver.py` | **create** | Lifted from hook.py: tmux pane → claude pid → session_id + cwd. Cross-platform. |
| `src/ccmux/hook.py` | modify | Replace local `_resolve_session_via_pid` (and friends) with import from new module. No behavior change for the hook's primary path. |
| `src/ccmux/claude_instance.py` | modify | Add `_overrides` dict + `set_override` / `clear_override`; teach `get`, `get_by_window_id`, `find_by_session_id`, `contains`, `all` to consult overrides first. |
| `src/ccmux/backend.py` | modify | Add `reconcile_instance(instance_id) -> ClaudeInstance | None` to `Backend` Protocol and `DefaultBackend`. |
| `tests/test_pid_session_resolver.py` | **create** | Unit tests for the lifted module. |
| `tests/test_claude_instance_overrides.py` | **create** | Unit tests for the override layer. |
| `tests/test_reconcile_instance.py` | **create** | Unit tests for the reconcile algorithm (a/b/c priority). |
| `pyproject.toml` | modify | Version bump 3.0.2 → 3.1.0. |
| `CHANGELOG.md` | modify | Added section under 3.1.0. |

### Frontend (`ccmux-telegram`)

| File | Action | Responsibility |
|---|---|---|
| `src/ccmux_telegram/binding_health.py` | **create** | `BindingHealth` class + `Transition` enum. |
| `src/ccmux_telegram/main.py` | modify | Startup reconcile pass + background `binding_health` polling task. |
| `src/ccmux_telegram/command_basic.py` | modify | Rename `/rebind` to `/rebind_topic`; add `/rebind_window`. |
| `src/ccmux_telegram/message_out.py` | modify | Refresh ⚠️ wording at all 9 call sites. |
| `tests/test_binding_health.py` | **create** | Unit tests for the transition detector. |
| `tests/test_rebind_window.py` | **create** | Unit tests for the new command. |
| `tests/test_rebind_topic.py` | **rename / new** | Tests for the renamed command (or extend existing rebind tests). |
| `pyproject.toml` | modify | Version bump 3.1.2 → 3.2.0; ccmux dep bump to `>=3.1.0,<4.0.0`. |
| `CHANGELOG.md` | modify | Added/Changed/Removed entries under 3.2.0. |

---

# Phase 1 — Backend (`ccmux-backend`)

## Task 1: Set up backend feature branch

**Files:** none (git only)

- [ ] **Step 1: Verify clean tree on dev**

```bash
cd ~/projects/ccmux/ccmux-backend
git fetch origin
git checkout dev
git pull --ff-only
git status
```

Expected: clean working tree, dev tracks origin/dev.

- [ ] **Step 2: Branch off dev per managing-git-branches Feature Flow**

```bash
git checkout -b feature/reconcile-instance dev
```

- [ ] **Step 3: Confirm**

```bash
git status -sb
```

Expected: `## feature/reconcile-instance`.

---

## Task 2: Lift pid resolver into new module (refactor, no behavior change)

**Files:**
- Create: `src/ccmux/pid_session_resolver.py`
- Modify: `src/ccmux/hook.py`
- Test: `tests/test_pid_session_resolver.py`

`hook.py` currently has `_resolve_session_via_pid`, `_find_claude_pid`, `_encode_project_dir`, `_UUID_RE`, `_SESSION_FILE_RE`, `_PANE_RE` as module-level helpers. Lift them into a new module under their existing names (mark public ones without leading underscore where the spec requires) and update `hook.py` to import.

- [ ] **Step 1: Write a failing test for the public entry point**

```python
# tests/test_pid_session_resolver.py
"""Unit tests for ccmux.pid_session_resolver."""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ccmux.pid_session_resolver import resolve_for_pane


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect Path.home() to a tmp dir so we can stage ~/.claude/."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _stage_claude_session(home: Path, claude_pid: int, cwd: str, session_id: str) -> None:
    """Create the files resolve_for_pane reads."""
    sessions_dir = home / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{claude_pid}.json").write_text(
        json.dumps({"cwd": cwd})
    )
    encoded = cwd.replace("/", "-").replace("_", "-").replace(".", "-")
    proj = home / ".claude" / "projects" / encoded
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{session_id}.jsonl").write_text("{}\n")


def test_resolve_for_pane_happy_path(fake_home: Path) -> None:
    pane = "%17"
    shell_pid = 4321
    claude_pid = 4322
    cwd = "/Users/wenruiwu"
    sid = "11111111-2222-3333-4444-555555555555"
    _stage_claude_session(fake_home, claude_pid, cwd, sid)

    def fake_run(args, **kwargs):
        class _R:
            returncode = 0
            stdout = ""
        if args[0] == "tmux":
            _R.stdout = f"{shell_pid}\n"
        elif args[0] == "pgrep":
            _R.stdout = f"{claude_pid}\n"
        else:
            raise AssertionError(f"unexpected: {args}")
        return _R()

    with patch("ccmux.pid_session_resolver.subprocess.run", side_effect=fake_run):
        result = resolve_for_pane(pane)

    assert result == (sid, cwd)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_pid_session_resolver.py -v
```

Expected: `ImportError: cannot import name 'resolve_for_pane' from 'ccmux.pid_session_resolver'` (module doesn't exist yet).

- [ ] **Step 3: Create the new module, copying logic verbatim from hook.py**

Create `src/ccmux/pid_session_resolver.py` with everything copied from hook.py and renamed `_resolve_session_via_pid` → `resolve_for_pane` (keep the underscore-prefixed helpers private to this module):

```python
"""Map a tmux pane to its (Claude session_id, launch_cwd).

Lifted from ``hook.py`` so the same chain can be used by reconcile
logic in the backend's runtime path. Behavior unchanged from the
existing ``_resolve_session_via_pid``; the only public symbol is
``resolve_for_pane``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SESSION_FILE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$"
)
_PANE_RE = re.compile(r"^%\d+$")


def _encode_project_dir(cwd: str) -> str:
    return re.sub(r"[/_.]", "-", cwd)


def _find_claude_pid(shell_pid: int) -> int | None:
    """Return the direct `claude` child PID of `shell_pid`, or None."""
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(shell_pid)],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    for token in result.stdout.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        try:
            raw = cmdline_path.read_bytes()
        except OSError:
            continue
        argv0 = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
        if Path(argv0).name == "claude":
            return pid
    return None


def resolve_for_pane(pane_id: str) -> tuple[str, str] | None:
    """Recover ``(session_id, launch_cwd)`` for the Claude in ``pane_id``.

    Returns ``None`` if any step fails.
    """
    if not _PANE_RE.match(pane_id):
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_pid}"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    try:
        shell_pid = int(result.stdout.strip())
    except ValueError:
        return None

    claude_pid = _find_claude_pid(shell_pid)
    if claude_pid is None:
        return None

    sessions_file = Path.home() / ".claude" / "sessions" / f"{claude_pid}.json"
    try:
        launch_info = json.loads(sessions_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    launch_cwd = launch_info.get("cwd", "")
    if not launch_cwd or not os.path.isabs(launch_cwd):
        return None

    project_dir = Path.home() / ".claude" / "projects" / _encode_project_dir(launch_cwd)
    try:
        candidates = [
            p
            for p in project_dir.iterdir()
            if p.is_file() and _SESSION_FILE_RE.match(p.name)
        ]
    except OSError:
        return None
    if not candidates:
        return None

    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return newest.stem, launch_cwd
```

- [ ] **Step 4: Update hook.py to import from the new module**

In `src/ccmux/hook.py`:
1. Delete the local definitions of `_resolve_session_via_pid`, `_find_claude_pid`, `_encode_project_dir`, `_UUID_RE`, `_SESSION_FILE_RE`, `_PANE_RE`.
2. Add at top of file (with other imports):

```python
from . import pid_session_resolver
from .pid_session_resolver import (
    _PANE_RE,
    _SESSION_FILE_RE,
    _UUID_RE,
    _encode_project_dir,
    _find_claude_pid,
)
```

3. Replace every call to `_resolve_session_via_pid(pane_id)` in hook.py with `pid_session_resolver.resolve_for_pane(pane_id)`.

(The underscore-prefixed re-imports preserve any tests that may reach into hook.py for those names. They can be removed in a follow-up after a grep confirms no consumers exist.)

- [ ] **Step 5: Run the new test, plus the existing hook tests, to verify nothing regressed**

```bash
uv run pytest tests/test_pid_session_resolver.py tests/test_hook.py -v
```

Expected: all pass.

- [ ] **Step 6: Run full test suite + pre-commit**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/ccmux/pid_session_resolver.py src/ccmux/hook.py tests/test_pid_session_resolver.py
git commit -m "refactor(hook): lift pid_session_resolver into shared module

Pure refactor; hook.py imports the same logic from the new module.
Used by upcoming reconcile_instance work that needs to identify
Claude windows from the bot's runtime path, not just the hook."
```

---

## Task 3: Make `pid_session_resolver` portable on macOS

**Files:**
- Modify: `src/ccmux/pid_session_resolver.py`
- Test: `tests/test_pid_session_resolver.py`

The current `_find_claude_pid` reads `/proc/<pid>/cmdline` to confirm a child is Claude — that's Linux-only. Replace the `cmdline` check with a portable signal: among the direct children of the shell, pick the one whose `~/.claude/sessions/<pid>.json` exists. That file is created by Claude Code itself on startup and is identical on both platforms.

- [ ] **Step 1: Write a failing test for the macOS path**

Add to `tests/test_pid_session_resolver.py`:

```python
def test_find_claude_pid_picks_child_with_sessions_file(
    fake_home: Path,
) -> None:
    """Without /proc, the resolver must still pick the Claude child."""
    from ccmux.pid_session_resolver import _find_claude_pid

    # Two children of the shell. Only one has a sessions file.
    shell_pid = 1000
    sibling_pid = 1001  # not Claude
    claude_pid = 1002

    sessions_dir = fake_home / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{claude_pid}.json").write_text("{}")

    def fake_run(args, **kwargs):
        class _R:
            returncode = 0
            stdout = f"{sibling_pid}\n{claude_pid}\n"
        return _R()

    with patch("ccmux.pid_session_resolver.subprocess.run", side_effect=fake_run):
        # Force /proc lookup to fail so we exercise the portable path.
        with patch.object(
            Path, "read_bytes", side_effect=OSError("no /proc")
        ):
            result = _find_claude_pid(shell_pid)

    assert result == claude_pid


def test_find_claude_pid_returns_none_when_no_child_has_sessions_file(
    fake_home: Path,
) -> None:
    from ccmux.pid_session_resolver import _find_claude_pid

    def fake_run(args, **kwargs):
        class _R:
            returncode = 0
            stdout = "1001\n1002\n"
        return _R()

    with patch("ccmux.pid_session_resolver.subprocess.run", side_effect=fake_run):
        with patch.object(
            Path, "read_bytes", side_effect=OSError("no /proc")
        ):
            result = _find_claude_pid(1000)

    assert result is None
```

- [ ] **Step 2: Run the test, verify failure**

```bash
uv run pytest tests/test_pid_session_resolver.py::test_find_claude_pid_picks_child_with_sessions_file -v
```

Expected: FAIL — current implementation returns `None` because `/proc/<pid>/cmdline` raises and we don't have the sessions-file fallback yet.

- [ ] **Step 3: Implement the portable signal in `_find_claude_pid`**

Replace the body of `_find_claude_pid` in `src/ccmux/pid_session_resolver.py`:

```python
def _find_claude_pid(shell_pid: int) -> int | None:
    """Return the direct `claude` child PID of `shell_pid`, or None.

    Strategy: enumerate direct children via `pgrep -P`. For each
    candidate, prefer the Linux signal `/proc/<pid>/cmdline` matching
    `claude` (cheap, exact). If that's not available — i.e. on macOS
    where `/proc` doesn't exist — fall back to a portable signal: a
    Claude Code instance writes ``~/.claude/sessions/<pid>.json`` on
    startup, so the presence of that file uniquely identifies it.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(shell_pid)],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None

    sessions_dir = Path.home() / ".claude" / "sessions"
    for token in result.stdout.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        # Linux: try /proc/<pid>/cmdline first.
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        try:
            raw = cmdline_path.read_bytes()
        except OSError:
            raw = None
        if raw is not None:
            argv0 = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
            if Path(argv0).name == "claude":
                return pid
            # On Linux, if cmdline read succeeded but didn't match,
            # this child is definitely not Claude — skip the fallback
            # for this pid.
            continue
        # Fallback (macOS, or Linux with restricted /proc): does this
        # child own a Claude sessions file?
        if (sessions_dir / f"{pid}.json").exists():
            return pid
    return None
```

- [ ] **Step 4: Run all `pid_session_resolver` tests**

```bash
uv run pytest tests/test_pid_session_resolver.py -v
```

Expected: all pass (happy path + new portable-signal tests).

- [ ] **Step 5: Run full suite**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/ccmux/pid_session_resolver.py tests/test_pid_session_resolver.py
git commit -m "fix(pid_session_resolver): support macOS via sessions-file presence

The /proc/<pid>/cmdline check is Linux-only. Add a portable fallback
that uses ~/.claude/sessions/<pid>.json as a 'this child is Claude'
signal. Linux behavior unchanged when /proc is readable."
```

---

## Task 4: Add override layer to `ClaudeInstanceRegistry`

**Files:**
- Modify: `src/ccmux/claude_instance.py`
- Test: `tests/test_claude_instance_overrides.py`

Add `_overrides: dict[str, ClaudeInstance]` to `ClaudeInstanceRegistry`, initialised empty in `__init__`. Add `set_override` and `clear_override`. Teach `get`, `get_by_window_id`, `find_by_session_id`, `contains`, and `all` to consult overrides first.

- [ ] **Step 1: Write failing tests for the override semantics**

```python
# tests/test_claude_instance_overrides.py
"""Override-layer behavior of ClaudeInstanceRegistry."""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from ccmux.claude_instance import ClaudeInstance, ClaudeInstanceRegistry


@pytest.fixture
def map_file(tmp_path: Path) -> Path:
    p = tmp_path / "claude_instances.json"
    p.write_text(json.dumps({
        "outlook": {
            "window_id": "@35",
            "session_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "cwd": "/Users/wenruiwu",
        },
    }))
    return p


def test_get_returns_override_over_file(map_file: Path) -> None:
    reg = ClaudeInstanceRegistry(map_file=map_file)
    override = ClaudeInstance(
        instance_id="outlook",
        window_id="@22",
        session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        cwd="/Users/wenruiwu",
    )
    reg.set_override("outlook", override)
    assert reg.get("outlook") == override


def test_clear_override_reverts_to_file(map_file: Path) -> None:
    reg = ClaudeInstanceRegistry(map_file=map_file)
    override = ClaudeInstance(
        instance_id="outlook",
        window_id="@22",
        session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        cwd="/Users/wenruiwu",
    )
    reg.set_override("outlook", override)
    reg.clear_override("outlook")
    inst = reg.get("outlook")
    assert inst is not None
    assert inst.window_id == "@35"


def test_set_override_for_unmapped_instance(tmp_path: Path) -> None:
    map_file = tmp_path / "empty.json"
    map_file.write_text("{}")
    reg = ClaudeInstanceRegistry(map_file=map_file)
    override = ClaudeInstance(
        instance_id="ghost",
        window_id="@99",
        session_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        cwd="/tmp",
    )
    reg.set_override("ghost", override)
    assert reg.get("ghost") == override


def test_get_by_window_id_consults_overrides(map_file: Path) -> None:
    reg = ClaudeInstanceRegistry(map_file=map_file)
    override = ClaudeInstance(
        instance_id="outlook",
        window_id="@22",
        session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        cwd="/Users/wenruiwu",
    )
    reg.set_override("outlook", override)
    found = reg.get_by_window_id("@22")
    assert found is not None and found.instance_id == "outlook"


def test_find_by_session_id_consults_overrides(map_file: Path) -> None:
    reg = ClaudeInstanceRegistry(map_file=map_file)
    sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    override = ClaudeInstance(
        instance_id="outlook",
        window_id="@22",
        session_id=sid,
        cwd="/Users/wenruiwu",
    )
    reg.set_override("outlook", override)
    found = reg.find_by_session_id(sid)
    assert found is not None and found.window_id == "@22"


def test_clear_override_is_noop_when_absent(map_file: Path) -> None:
    reg = ClaudeInstanceRegistry(map_file=map_file)
    reg.clear_override("not-there")  # must not raise
```

- [ ] **Step 2: Run tests, verify failure**

```bash
uv run pytest tests/test_claude_instance_overrides.py -v
```

Expected: FAIL with `AttributeError: 'ClaudeInstanceRegistry' object has no attribute 'set_override'`.

- [ ] **Step 3: Implement the override layer**

In `src/ccmux/claude_instance.py`:

```python
class ClaudeInstanceRegistry:
    def __init__(self, map_file: Path | None = None) -> None:
        self._map_file = map_file if map_file is not None else config.instances_file
        self._data: dict[str, dict[str, str]] = {}
        self._overrides: dict[str, ClaudeInstance] = {}  # NEW
        self._read()

    # NEW
    def set_override(self, instance_id: str, instance: ClaudeInstance) -> None:
        """Install or replace an in-memory override for `instance_id`."""
        self._overrides[instance_id] = instance

    # NEW
    def clear_override(self, instance_id: str) -> None:
        """Remove the in-memory override (no-op if not present)."""
        self._overrides.pop(instance_id, None)

    # MODIFIED: consult overrides first
    def get(self, instance_id: str) -> ClaudeInstance | None:
        if instance_id in self._overrides:
            return self._overrides[instance_id]
        entry = self._data.get(instance_id)
        if not entry:
            return None
        return self._to_instance(instance_id, entry)

    # MODIFIED
    def get_by_window_id(self, window_id: str) -> ClaudeInstance | None:
        if not window_id:
            return None
        for instance_id, override in self._overrides.items():
            if override.window_id == window_id:
                return override
        for instance_id, entry in self._data.items():
            if instance_id in self._overrides:
                continue  # override already considered above
            if entry.get("window_id") == window_id:
                return self._to_instance(instance_id, entry)
        return None

    # MODIFIED
    def find_by_session_id(self, session_id: str) -> ClaudeInstance | None:
        if not session_id:
            return None
        for instance_id, override in self._overrides.items():
            if override.session_id == session_id:
                return override
        for instance_id, entry in self._data.items():
            if instance_id in self._overrides:
                continue
            if entry.get("session_id") == session_id:
                return self._to_instance(instance_id, entry)
        return None

    # MODIFIED
    def contains(self, instance_id: str) -> bool:
        if instance_id in self._overrides:
            o = self._overrides[instance_id]
            return bool(o.window_id and o.session_id)
        entry = self._data.get(instance_id)
        return bool(entry and entry.get("window_id") and entry.get("session_id"))

    # MODIFIED
    def all(self) -> Iterator[ClaudeInstance]:
        seen: set[str] = set()
        for instance_id, override in self._overrides.items():
            if override.window_id:
                seen.add(instance_id)
                yield override
        for instance_id, entry in list(self._data.items()):
            if instance_id in seen:
                continue
            wid = entry.get("window_id", "")
            if wid:
                yield self._to_instance(instance_id, entry)
```

- [ ] **Step 4: Run override tests**

```bash
uv run pytest tests/test_claude_instance_overrides.py -v
```

Expected: all pass.

- [ ] **Step 5: Run full suite**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/ccmux/claude_instance.py tests/test_claude_instance_overrides.py
git commit -m "feat(registry): in-memory override layer for ClaudeInstanceRegistry

set_override/clear_override let the runtime path correct stale
window_ids without touching claude_instances.json (still hook-owned).
get/get_by_window_id/find_by_session_id/contains/all consult overrides
first."
```

---

## Task 5: Add `Backend.reconcile_instance`

**Files:**
- Modify: `src/ccmux/backend.py`
- Test: `tests/test_reconcile_instance.py`

Add the method to the `Backend` Protocol and implement it on `DefaultBackend`. The algorithm follows the spec's priority a/b/c.

- [ ] **Step 1: Write failing tests for the four key paths**

```python
# tests/test_reconcile_instance.py
"""reconcile_instance algorithm coverage."""
from __future__ import annotations
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ccmux.api import (
    ClaudeInstance,
    ClaudeInstanceRegistry,
    DefaultBackend,
    TmuxWindow,
    tmux_registry,
)


def _window(wid: str, cmd: str = "claude") -> TmuxWindow:
    return TmuxWindow(window_id=wid, cwd="/Users/wenruiwu", pane_current_command=cmd)


@pytest.fixture
def registry(tmp_path: Path) -> ClaudeInstanceRegistry:
    map_file = tmp_path / "claude_instances.json"
    map_file.write_text("{}")
    return ClaudeInstanceRegistry(map_file=map_file)


@pytest.fixture
def backend(registry: ClaudeInstanceRegistry) -> DefaultBackend:
    return DefaultBackend(tmux_registry=tmux_registry, claude_instances=registry)


@pytest.mark.asyncio
async def test_reconcile_no_claude_windows(backend: DefaultBackend) -> None:
    fake_session = AsyncMock()
    fake_session.list_windows.return_value = [
        _window("@10", cmd="zsh"),  # not Claude
    ]
    with patch.object(tmux_registry, "get_or_create", return_value=fake_session):
        result = await backend.reconcile_instance("outlook")
    assert result is None


@pytest.mark.asyncio
async def test_reconcile_single_claude_window(backend: DefaultBackend) -> None:
    fake_session = AsyncMock()
    fake_session.list_windows.return_value = [_window("@22")]
    with patch.object(tmux_registry, "get_or_create", return_value=fake_session):
        with patch(
            "ccmux.backend._resolve_via_pane",
            return_value=("aaaa-bbbb", "/Users/wenruiwu"),
        ):
            result = await backend.reconcile_instance("outlook")
    assert result is not None
    assert result.window_id == "@22"
    assert result.session_id == "aaaa-bbbb"


@pytest.mark.asyncio
async def test_reconcile_multiple_session_id_match(backend: DefaultBackend, registry: ClaudeInstanceRegistry) -> None:
    # Pre-record outlook → @35 / sid X
    sid_x = "11111111-1111-1111-1111-111111111111"
    sid_y = "22222222-2222-2222-2222-222222222222"
    registry._data["outlook"] = {
        "window_id": "@35", "session_id": sid_x, "cwd": "/Users/wenruiwu",
    }

    fake_session = AsyncMock()
    fake_session.list_windows.return_value = [_window("@22"), _window("@34")]

    def fake_resolve(pane_id: str):
        # @22's pane resolves to sid_y; @34's resolves to sid_x (the recorded match)
        return {
            "%22": (sid_y, "/Users/wenruiwu"),
            "%34": (sid_x, "/Users/wenruiwu"),
        }[pane_id]

    with patch.object(tmux_registry, "get_or_create", return_value=fake_session):
        with patch("ccmux.backend._pane_id_for_window", side_effect=lambda w: f"%{w[1:]}"):
            with patch("ccmux.backend._resolve_via_pane", side_effect=fake_resolve):
                result = await backend.reconcile_instance("outlook")

    assert result is not None
    assert result.window_id == "@34"
    assert result.session_id == sid_x


@pytest.mark.asyncio
async def test_reconcile_multiple_falls_back_to_lowest_window_index(
    backend: DefaultBackend,
) -> None:
    fake_session = AsyncMock()
    # list_windows returns in tmux's order; lowest window_index first.
    fake_session.list_windows.return_value = [_window("@22"), _window("@34")]

    with patch.object(tmux_registry, "get_or_create", return_value=fake_session):
        with patch("ccmux.backend._pane_id_for_window", side_effect=lambda w: f"%{w[1:]}"):
            with patch("ccmux.backend._resolve_via_pane", return_value=None):
                result = await backend.reconcile_instance("outlook")

    assert result is not None
    assert result.window_id == "@22"  # first in list_windows order
```

- [ ] **Step 2: Run, expect failures**

```bash
uv run pytest tests/test_reconcile_instance.py -v
```

Expected: FAIL with `AttributeError: 'DefaultBackend' object has no attribute 'reconcile_instance'`.

- [ ] **Step 3: Add the method to `Backend` Protocol and `DefaultBackend`**

In `src/ccmux/backend.py`, add to the `Backend` Protocol:

```python
class Backend(Protocol):
    # ... existing members ...

    async def reconcile_instance(
        self, instance_id: str
    ) -> ClaudeInstance | None:
        """Return the ClaudeInstance the bot should be talking to for
        `instance_id`, computed from current tmux state. Pure read; no
        writes, no side effects beyond the libtmux query.
        """
        ...
```

Add helper functions at module level (private to backend.py):

```python
from .pid_session_resolver import resolve_for_pane as _resolve_via_pane
from .state_monitor import _claude_proc_names


def _pane_id_for_window(window_id: str) -> str:
    """Map a tmux window_id (`@N`) to its active-pane id (`%M`)."""
    # libtmux: Window.active_pane.pane_id. Caller passes Window.window_id;
    # in the reconcile loop we have the libtmux Window object available
    # so we read .active_pane.pane_id directly there. This helper exists
    # mainly as a seam for tests.
    raise NotImplementedError(
        "called only via patched seam in tests; in production we read "
        "Window.active_pane.pane_id directly"
    )
```

(See implementation below — production code reads `pane_id` directly from libtmux objects; the `_pane_id_for_window` seam is only patched in tests.)

Then on `DefaultBackend`:

```python
class DefaultBackend:
    # ... existing ...

    async def reconcile_instance(
        self, instance_id: str
    ) -> ClaudeInstance | None:
        tm = self._tmux_registry.get_or_create(instance_id)
        windows = await tm.list_windows()

        proc_names = _claude_proc_names()
        claude_windows = [w for w in windows if w.pane_current_command in proc_names]
        if not claude_windows:
            return None

        recorded = self._claude_instances.get(instance_id)
        recorded_session_id = recorded.session_id if recorded else ""

        # Resolve (session_id, cwd) for each candidate.
        # In production, get pane_id from libtmux's Window.active_pane.
        # Here we use a small async helper that defers to libtmux.
        resolved: list[tuple[TmuxWindow, tuple[str, str] | None]] = []
        for w in claude_windows:
            pane_id = await self._active_pane_id(w.window_id)
            r = _resolve_via_pane(pane_id) if pane_id else None
            resolved.append((w, r))

        # Priority a: match recorded session_id.
        if recorded_session_id:
            for w, r in resolved:
                if r is not None and r[0] == recorded_session_id:
                    return ClaudeInstance(
                        instance_id=instance_id,
                        window_id=w.window_id,
                        session_id=r[0],
                        cwd=r[1],
                    )

        # Priority b: most-recent JSONL mtime via the resolved tuple's
        # session_id. We re-stat the JSONLs here.
        from pathlib import Path as _Path

        scored: list[tuple[float, TmuxWindow, tuple[str, str]]] = []
        for w, r in resolved:
            if r is None:
                continue
            sid, cwd = r
            jsonl = (
                _Path.home() / ".claude" / "projects"
                / _Path(cwd.replace("/", "-").replace("_", "-").replace(".", "-"))
                / f"{sid}.jsonl"
            )
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            scored.append((mtime, w, r))

        if scored:
            scored.sort(key=lambda t: t[0], reverse=True)
            _, w, (sid, cwd) = scored[0]
            return ClaudeInstance(
                instance_id=instance_id,
                window_id=w.window_id,
                session_id=sid,
                cwd=cwd,
            )

        # Priority c: fallback — first claude window by tmux's order.
        # list_windows returns in tmux window-index order, so element 0
        # is the lowest index.
        w0 = claude_windows[0]
        # If we have a resolved (session_id, cwd) for w0, use it.
        # Otherwise fall back to the recorded values to keep
        # message_monitor pointed at *some* JSONL.
        for w, r in resolved:
            if w.window_id == w0.window_id and r is not None:
                return ClaudeInstance(
                    instance_id=instance_id,
                    window_id=w.window_id,
                    session_id=r[0],
                    cwd=r[1],
                )
        return ClaudeInstance(
            instance_id=instance_id,
            window_id=w0.window_id,
            session_id=recorded_session_id,
            cwd=recorded.cwd if recorded else "",
        )

    async def _active_pane_id(self, window_id: str) -> str:
        """Return the `%N` pane id of the active pane in `window_id`."""
        # libtmux call: query the registered TmuxSession for this window.
        tm = self._tmux_registry.get_by_window_id(window_id)
        if tm is None:
            return ""
        return await tm.active_pane_id(window_id)
```

Add `active_pane_id` to `TmuxSession` in `src/ccmux/tmux.py`:

```python
async def active_pane_id(self, window_id: str) -> str:
    def _sync() -> str:
        session = self.get_session()
        if not session:
            return ""
        for window in session.windows:
            if (window.window_id or "") == window_id:
                pane = window.active_pane
                return (pane.pane_id or "") if pane else ""
        return ""

    return await asyncio.to_thread(_sync)
```

- [ ] **Step 4: Run reconcile tests**

```bash
uv run pytest tests/test_reconcile_instance.py -v
```

Expected: all pass.

- [ ] **Step 5: Run full suite + lint**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/ccmux/backend.py src/ccmux/tmux.py tests/test_reconcile_instance.py
git commit -m "feat(backend): add reconcile_instance API

Pure read: given a tmux session name, return the ClaudeInstance the
bot should now talk to (or None if no Claude lives there). Caller
applies the result via ClaudeInstanceRegistry.set_override.

Priority: recorded session_id match → JSONL mtime → lowest tmux
window_index. Uses pid_session_resolver to identify session_ids."
```

---

## Task 6: Verify nothing else regressed; final pre-push checks

**Files:** none (test only)

- [ ] **Step 1: Re-run lint, type, tests once more**

```bash
cd ~/projects/ccmux/ccmux-backend
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

All must pass.

- [ ] **Step 2: Push the feature branch (origin only; not yet to dev)**

```bash
git push -u origin feature/reconcile-instance
```

---

## Task 7: Merge feature → dev (Feature Flow)

**Files:** none (git only)

- [ ] **Step 1: Use `managing-git-branches` Feature Flow**

```bash
cd ~/projects/ccmux/ccmux-backend
git checkout dev
git merge feature/reconcile-instance --no-ff
```

(no `-m`; default merge message)

- [ ] **Step 2: Run pre-push checks on the merged dev**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

- [ ] **Step 3: Push dev**

```bash
git push origin dev
```

- [ ] **Step 4: Delete merged feature branch**

```bash
git branch -d feature/reconcile-instance
git push origin --delete feature/reconcile-instance
```

---

## Task 8: Release backend v3.1.0 (Release Flow)

**Files:**
- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Branch off dev**

```bash
cd ~/projects/ccmux/ccmux-backend
git checkout -b release/v3.1.0 dev
```

- [ ] **Step 2: Bump version**

Edit `pyproject.toml`: change `version = "3.0.2"` to `version = "3.1.0"`.

- [ ] **Step 3: Update CHANGELOG**

Insert under `## [Unreleased]`:

```markdown
## 3.1.0 — 2026-04-27

### Added

- `Backend.reconcile_instance(instance_id)` — pure read API that, given
  a tmux session name, returns the `ClaudeInstance` the caller should
  bind to. Uses `pid_session_resolver.resolve_for_pane` to identify the
  session_id of each candidate window; resolves multi-window cases by
  recorded `session_id` match → most-recent JSONL mtime → lowest
  tmux window_index.
- `ClaudeInstanceRegistry.set_override` /
  `ClaudeInstanceRegistry.clear_override` — in-memory override layer
  letting consumers correct stale `window_id` mappings without
  touching `claude_instances.json`. The hook remains the only writer
  of that file.
- `pid_session_resolver` module — public-ish entry point
  `resolve_for_pane(pane_id) -> (session_id, cwd) | None`. Lifted from
  the existing private `_resolve_session_via_pid` in `hook.py` so the
  same chain is reusable from runtime code.
- `TmuxSession.active_pane_id(window_id)` — small helper used by
  `reconcile_instance` to map a `@N` window id to its `%M` active-pane
  id for the resolver.

### Changed

- `_find_claude_pid` is now portable on macOS. The previous
  Linux-only `/proc/<pid>/cmdline` check is preserved as the fast
  path; when `/proc` isn't readable, it falls back to a
  cross-platform signal — the presence of
  `~/.claude/sessions/<pid>.json`, which Claude Code itself writes on
  startup. The existing happy path for the hook on Linux is
  unchanged.
```

- [ ] **Step 4: Commit release prep**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: bump version to 3.1.0 and update CHANGELOG"
```

- [ ] **Step 5: Merge release into main with `--no-ff`**

```bash
git checkout main
git merge release/v3.1.0 --no-ff
```

- [ ] **Step 6: Tag**

```bash
git tag v3.1.0 -m "v3.1.0: reconcile_instance API + override layer"
```

- [ ] **Step 7: Merge release into dev with `--no-ff`**

```bash
git checkout dev
git merge release/v3.1.0 --no-ff
```

- [ ] **Step 8: Pre-push checks on merged trees**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

- [ ] **Step 9: Push everything**

```bash
git push origin main dev --tags
```

- [ ] **Step 10: Delete release branch**

```bash
git branch -d release/v3.1.0
```

Backend is now at v3.1.0 with the new API published. Telegram phase can begin.

---

# Phase 2 — Frontend (`ccmux-telegram`)

The feature branch `feature/binding-self-heal` already exists from the spec phase. All Phase 2 tasks land on it.

## Task 9: Bump backend dependency on the feature branch

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Switch to the feature branch**

```bash
cd ~/projects/ccmux/ccmux-telegram
git checkout feature/binding-self-heal
git pull --ff-only origin feature/binding-self-heal 2>/dev/null || true
git status -sb
```

- [ ] **Step 2: Pull the freshly-released backend**

```bash
git -C ~/projects/ccmux/ccmux-backend pull --ff-only
uv tool install --reinstall --editable ~/projects/ccmux/ccmux-backend
```

(Confirms the editable install reflects v3.1.0.)

- [ ] **Step 3: Bump dep in `pyproject.toml`**

```diff
-    "ccmux>=3.0.0,<4.0.0",
+    "ccmux>=3.1.0,<4.0.0",
```

- [ ] **Step 4: Refresh telegram's editable install so it sees the new dep constraint**

```bash
uv tool install --reinstall --editable ~/projects/ccmux/ccmux-telegram
```

- [ ] **Step 5: Smoke-check that the new backend symbols are importable**

```bash
uv run python -c "from ccmux.api import Backend, ClaudeInstanceRegistry; \
  assert hasattr(Backend, 'reconcile_instance'); \
  assert hasattr(ClaudeInstanceRegistry, 'set_override'); print('ok')"
```

Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml
git commit -m "chore: bump ccmux dep to >=3.1.0,<4.0.0

Required by the upcoming /rebind_window and binding self-heal work
which uses the new reconcile_instance API and override layer."
```

---

## Task 10: Add `binding_health.py` module

**Files:**
- Create: `src/ccmux_telegram/binding_health.py`
- Test: `tests/test_binding_health.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_binding_health.py
"""Transition tracker behavior."""
from __future__ import annotations

from ccmux_telegram.binding_health import BindingHealth, Transition


def test_first_observation_alive_is_stable() -> None:
    h = BindingHealth()
    assert h.observe("ccmux", True) is Transition.STABLE


def test_first_observation_not_alive_is_lost() -> None:
    h = BindingHealth()
    # Default prev=True, so True→False = LOST on first call.
    assert h.observe("ccmux", False) is Transition.LOST


def test_recovered_on_false_to_true() -> None:
    h = BindingHealth()
    h.observe("ccmux", False)
    assert h.observe("ccmux", True) is Transition.RECOVERED


def test_lost_on_true_to_false() -> None:
    h = BindingHealth()
    h.observe("ccmux", True)
    assert h.observe("ccmux", False) is Transition.LOST


def test_stable_when_unchanged() -> None:
    h = BindingHealth()
    h.observe("ccmux", True)
    assert h.observe("ccmux", True) is Transition.STABLE
    h.observe("ccmux", False)
    assert h.observe("ccmux", False) is Transition.STABLE


def test_independent_per_instance() -> None:
    h = BindingHealth()
    h.observe("a", True)
    h.observe("b", False)
    # b's transition tracking does not leak into a.
    assert h.observe("a", True) is Transition.STABLE
    assert h.observe("b", True) is Transition.RECOVERED
```

- [ ] **Step 2: Run, verify failure**

```bash
uv run pytest tests/test_binding_health.py -v
```

Expected: `ImportError: cannot import name 'BindingHealth'`.

- [ ] **Step 3: Implement the module**

```python
# src/ccmux_telegram/binding_health.py
"""Per-binding alive-transition tracker.

Drives the bot's "✅ recovered" notifications. Single-threaded usage:
called from the main asyncio loop only.
"""

from __future__ import annotations

import enum


class Transition(enum.Enum):
    STABLE = "stable"
    RECOVERED = "recovered"
    LOST = "lost"


class BindingHealth:
    """Records last-observed `is_alive` and reports flips."""

    def __init__(self) -> None:
        self._was_alive: dict[str, bool] = {}

    def observe(self, instance_id: str, is_alive_now: bool) -> Transition:
        """Record `is_alive_now` and return the transition vs last call.

        First observation defaults `prev=True` so that healthy bindings
        do not generate spurious RECOVERED at startup. The trade-off is
        that a binding broken before the bot started yields LOST on its
        first observation; that's harmless because LOST is never
        notified proactively.
        """
        prev = self._was_alive.get(instance_id, True)
        self._was_alive[instance_id] = is_alive_now
        if not prev and is_alive_now:
            return Transition.RECOVERED
        if prev and not is_alive_now:
            return Transition.LOST
        return Transition.STABLE
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_binding_health.py -v
```

Expected: all pass.

- [ ] **Step 5: Pre-push checks**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

- [ ] **Step 6: Commit**

```bash
git add src/ccmux_telegram/binding_health.py tests/test_binding_health.py
git commit -m "feat(binding_health): per-binding alive-transition tracker

Records last is_alive value per instance_id and returns
STABLE/RECOVERED/LOST on each observe(). Used by the upcoming
periodic loop in main.py to post '✅ recovered' notifications when an
external fix flips a binding from broken back to alive."
```

---

## Task 11: Refresh ⚠️ wording in `message_out.py`

**Files:**
- Modify: `src/ccmux_telegram/message_out.py`
- Test: regex grep test in `tests/test_rebind_messaging.py`

- [ ] **Step 1: Write a guardrail test**

```python
# tests/test_rebind_messaging.py
"""Guardrails for the '/rebind' → '/rebind_window'/'/rebind_topic' rename."""
from __future__ import annotations
import pathlib

import pytest


SRC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "ccmux_telegram"


def _read_all_python() -> str:
    text: list[str] = []
    for p in SRC_ROOT.rglob("*.py"):
        text.append(p.read_text())
    return "\n".join(text)


def test_no_bare_rebind_command_in_user_text() -> None:
    """The user-facing string '/rebind' (not /rebind_topic, not
    /rebind_window) must not appear in any shipped Python file."""
    text = _read_all_python()
    # Allow /rebind_topic and /rebind_window; flag bare /rebind.
    import re
    matches = [
        m.group(0)
        for m in re.finditer(r"/rebind(?!_topic|_window)\b", text)
    ]
    assert not matches, f"Found bare /rebind references: {matches}"


def test_message_out_uses_new_wording() -> None:
    out = (SRC_ROOT / "message_out.py").read_text()
    assert "/rebind_window to refresh" in out
    assert "/rebind_topic to switch" in out
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/test_rebind_messaging.py -v
```

Expected: FAIL — current wording uses bare `/rebind`.

- [ ] **Step 3: Edit `message_out.py`**

Replace every occurrence of the literal:

```text
"⚠️ Binding to `{topic.session_name}` is not alive right now. "
"tmux or Claude may be down. Use /rebind to reconnect to a different session.",
```

with:

```text
"⚠️ Binding to `{topic.session_name}` is not alive right now. "
"tmux or Claude may be down. Use /rebind_window to refresh, "
"or /rebind_topic to switch.",
```

There are nine call sites in `message_out.py`; do a single search-and-replace on the substring `"Use /rebind to reconnect to a different session."` → `"Use /rebind_window to refresh, or /rebind_topic to switch."`.

- [ ] **Step 4: Run the guardrail tests**

```bash
uv run pytest tests/test_rebind_messaging.py -v
```

Expected: all pass.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest
```

Expected: green. (The change is text-only; existing functional tests aren't affected unless they assert old wording — fix any that do as part of this task.)

- [ ] **Step 6: Commit**

```bash
git add src/ccmux_telegram/message_out.py tests/test_rebind_messaging.py
git commit -m "feat(message_out): refresh ⚠️ wording for /rebind_window+/rebind_topic

The 'binding not alive' notice now points at /rebind_window to refresh
the binding and /rebind_topic to switch sessions, matching the new
command surface."
```

---

## Task 12: Rename `/rebind` → `/rebind_topic`

**Files:**
- Modify: `src/ccmux_telegram/command_basic.py`
- Modify: `src/ccmux_telegram/main.py` (CommandHandler registration)
- Modify (any): `src/ccmux_telegram/menu.py` or wherever the BotFather menu strings live
- Tests: extend `tests/test_rebind_messaging.py` and any existing rebind handler tests

- [ ] **Step 1: Find existing rebind registration**

```bash
grep -nR '"rebind"\|rebind_command\|/rebind\b' src/ccmux_telegram/ tests/
```

Note every hit; each is a touch-point for this task.

- [ ] **Step 2: Write the guardrail test for the rename**

Add to `tests/test_rebind_messaging.py`:

```python
def test_rebind_topic_handler_registered() -> None:
    """The CommandHandler is registered under the new name."""
    text = (SRC_ROOT / "main.py").read_text()
    assert 'CommandHandler("rebind_topic"' in text
    assert 'CommandHandler("rebind"' not in text


def test_rebind_topic_function_exists() -> None:
    from ccmux_telegram import command_basic
    assert hasattr(command_basic, "rebind_topic_command")
    assert not hasattr(command_basic, "rebind_command")
```

- [ ] **Step 3: Run, expect failure**

```bash
uv run pytest tests/test_rebind_messaging.py -v
```

Expected: FAIL — `rebind_command` still exists.

- [ ] **Step 4: Rename in `command_basic.py`**

- Rename the function `async def rebind_command(...)` → `async def rebind_topic_command(...)`.
- Update the docstring/help text inside to refer to "Pick a different tmux session for this topic."

- [ ] **Step 5: Update `main.py` handler registration**

Change:
```python
application.add_handler(CommandHandler("rebind", rebind_command))
```
to:
```python
application.add_handler(CommandHandler("rebind_topic", rebind_topic_command))
```

If `rebind_command` is imported by name in `main.py`, update the import too.

- [ ] **Step 6: Update menu / help strings**

Find where the BotFather command list is set (search for `set_my_commands` or similar). Replace `("rebind", "...")` with `("rebind_topic", "Pick a different tmux session for this topic")`. If there's a `/help` text that mentions `/rebind`, update it.

- [ ] **Step 7: Run the suite**

```bash
uv run pytest
```

Expected: all green. If older tests imported `rebind_command`, update them to use the new name.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(commands): rename /rebind to /rebind_topic

No alias kept — /rebind is removed. The command's behavior is
unchanged; the rename clarifies that this command swaps the
topic→session binding (to be paired with /rebind_window for the
session→window refresh)."
```

---

## Task 13: Add `/rebind_window` command

**Files:**
- Modify: `src/ccmux_telegram/command_basic.py`
- Modify: `src/ccmux_telegram/main.py` (handler registration + menu)
- Test: `tests/test_rebind_window.py`

- [ ] **Step 1: Write failing tests for the three branches**

```python
# tests/test_rebind_window.py
"""Behavior of the /rebind_window command."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccmux.api import ClaudeInstance


@pytest.fixture
def update_with_bound_topic() -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 8559840605
    update.message = MagicMock()
    update.message.message_thread_id = 2
    update.message.reply_text = AsyncMock()
    return update


@pytest.fixture
def context() -> MagicMock:
    return MagicMock()


@pytest.mark.asyncio
async def test_rebind_window_unbound(update_with_bound_topic, context) -> None:
    from ccmux_telegram import command_basic
    update = update_with_bound_topic
    with patch("ccmux_telegram.command_basic.get_topic", return_value=None):
        await command_basic.rebind_window_command(update, context)
    msg = update.message.reply_text.await_args.args[0]
    assert "❌" in msg
    assert "/rebind_topic" in msg


@pytest.mark.asyncio
async def test_rebind_window_success(update_with_bound_topic, context) -> None:
    from ccmux_telegram import command_basic
    update = update_with_bound_topic

    fake_topic = MagicMock(session_name="outlook")
    fake_inst = ClaudeInstance(
        instance_id="outlook",
        window_id="@22",
        session_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        cwd="/Users/wenruiwu",
    )
    fake_backend = MagicMock()
    fake_backend.reconcile_instance = AsyncMock(return_value=fake_inst)
    fake_backend.claude_instances.set_override = MagicMock()

    with patch("ccmux_telegram.command_basic.get_topic", return_value=fake_topic):
        with patch("ccmux_telegram.command_basic.get_backend", return_value=fake_backend):
            await command_basic.rebind_window_command(update, context)

    fake_backend.claude_instances.set_override.assert_called_once_with(
        "outlook", fake_inst
    )
    msg = update.message.reply_text.await_args.args[0]
    assert "✅" in msg
    assert "@22" in msg


@pytest.mark.asyncio
async def test_rebind_window_no_live_claude(update_with_bound_topic, context) -> None:
    from ccmux_telegram import command_basic
    update = update_with_bound_topic

    fake_topic = MagicMock(session_name="outlook")
    fake_backend = MagicMock()
    fake_backend.reconcile_instance = AsyncMock(return_value=None)

    with patch("ccmux_telegram.command_basic.get_topic", return_value=fake_topic):
        with patch("ccmux_telegram.command_basic.get_backend", return_value=fake_backend):
            await command_basic.rebind_window_command(update, context)

    msg = update.message.reply_text.await_args.args[0]
    assert "⚠️" in msg
    assert "/rebind_topic" in msg
    assert "/start" in msg


def test_rebind_window_handler_registered() -> None:
    text = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src" / "ccmux_telegram" / "main.py"
    ).read_text()
    assert 'CommandHandler("rebind_window"' in text
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/test_rebind_window.py -v
```

Expected: FAIL — `rebind_window_command` does not exist.

- [ ] **Step 3: Implement the handler in `command_basic.py`**

Append:

```python
async def rebind_window_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Refresh the bound session's window mapping.

    Calls backend.reconcile_instance for the topic's session_name and,
    if a live Claude window is found, installs an in-memory override.
    Reports the outcome inline.
    """
    user = update.effective_user
    if user is None or update.message is None:
        return
    thread_id = get_thread_id(update)
    topic = get_topic(user.id, thread_id) if thread_id is not None else None

    if topic is None:
        await safe_reply(
            update.message,
            "❌ No session bound here. Use /rebind_topic first.",
        )
        return

    backend = get_backend()
    inst = await backend.reconcile_instance(topic.session_name)
    if inst is None:
        await safe_reply(
            update.message,
            f"⚠️ Session `{topic.session_name}` has no live Claude. "
            "Use /rebind_topic to switch, or /start to spawn a new Claude.",
        )
        return

    backend.claude_instances.set_override(topic.session_name, inst)
    await safe_reply(
        update.message,
        f"✅ Refreshed binding: `{topic.session_name}` → `{inst.window_id}`.",
    )
```

(`get_backend` is whatever existing accessor the bot uses — likely `get_default_backend()` from `ccmux.api`. Use the same accessor existing handlers use.)

- [ ] **Step 4: Register in `main.py`**

```python
application.add_handler(CommandHandler("rebind_window", rebind_window_command))
```

Add `("rebind_window", "Refresh which window of the bound session this topic uses")` to the BotFather menu list.

- [ ] **Step 5: Run the new tests**

```bash
uv run pytest tests/test_rebind_window.py -v
```

Expected: all pass.

- [ ] **Step 6: Pre-push**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

- [ ] **Step 7: Commit**

```bash
git add src/ccmux_telegram/command_basic.py src/ccmux_telegram/main.py tests/test_rebind_window.py
git commit -m "feat(commands): add /rebind_window

Calls the backend's reconcile_instance and installs the in-memory
override on success. Replies inline:
  - ✅ on success with the new window_id
  - ⚠️ when the session has no live Claude
  - ❌ when the topic is unbound (suggests /rebind_topic)"
```

---

## Task 14: Startup reconcile pass in `main.py`

**Files:**
- Modify: `src/ccmux_telegram/main.py`
- Test: `tests/test_startup_reconcile.py`

- [ ] **Step 1: Write a failing test**

```python
# tests/test_startup_reconcile.py
"""Startup reconcile pass: silent best-effort fix for known bindings."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccmux.api import ClaudeInstance


@pytest.mark.asyncio
async def test_startup_pass_calls_reconcile_for_each_unique_session() -> None:
    from ccmux_telegram.main import _run_startup_reconcile

    bindings = [
        MagicMock(session_name="ccmux"),
        MagicMock(session_name="outlook"),
        MagicMock(session_name="ccmux"),  # dup, must dedupe
    ]
    fake_topics = MagicMock()
    fake_topics.all = MagicMock(return_value=bindings)

    fake_inst = ClaudeInstance("ccmux", "@22", "sid", "/")
    fake_backend = MagicMock()
    fake_backend.reconcile_instance = AsyncMock(return_value=fake_inst)
    fake_backend.claude_instances.set_override = MagicMock()

    await _run_startup_reconcile(fake_topics, fake_backend)

    # Called twice — once for each unique session name.
    assert fake_backend.reconcile_instance.await_count == 2
    # Override applied for both successful results.
    assert fake_backend.claude_instances.set_override.call_count == 2


@pytest.mark.asyncio
async def test_startup_pass_skips_set_override_when_reconcile_returns_none() -> None:
    from ccmux_telegram.main import _run_startup_reconcile

    bindings = [MagicMock(session_name="dead-session")]
    fake_topics = MagicMock()
    fake_topics.all = MagicMock(return_value=bindings)

    fake_backend = MagicMock()
    fake_backend.reconcile_instance = AsyncMock(return_value=None)
    fake_backend.claude_instances.set_override = MagicMock()

    await _run_startup_reconcile(fake_topics, fake_backend)

    fake_backend.reconcile_instance.assert_awaited_once_with("dead-session")
    fake_backend.claude_instances.set_override.assert_not_called()
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/test_startup_reconcile.py -v
```

Expected: FAIL — `_run_startup_reconcile` doesn't exist.

- [ ] **Step 3: Add the function and call it before `application.run_polling()`**

In `src/ccmux_telegram/main.py`, somewhere near the top of the file:

```python
async def _run_startup_reconcile(topics, backend) -> None:
    """Best-effort pass to fix stale bindings before the bot serves.

    Silent: no Telegram messages are emitted; the user wasn't online
    to react to anything.
    """
    seen: set[str] = set()
    for binding in topics.all():
        name = binding.session_name
        if name in seen:
            continue
        seen.add(name)
        try:
            inst = await backend.reconcile_instance(name)
        except Exception:
            logger.exception("startup reconcile failed for %s", name)
            continue
        if inst is not None:
            backend.claude_instances.set_override(name, inst)
```

In the bot's main entry (the `async def main(): ...` or equivalent):

```python
# After topic_bindings loaded and backend obtained, before run_polling:
await _run_startup_reconcile(_topics, backend)
```

- [ ] **Step 4: Run the new tests**

```bash
uv run pytest tests/test_startup_reconcile.py -v
```

Expected: all pass.

- [ ] **Step 5: Pre-push**

```bash
uv run pytest
```

- [ ] **Step 6: Commit**

```bash
git add src/ccmux_telegram/main.py tests/test_startup_reconcile.py
git commit -m "feat(main): startup reconcile pass

Before the bot serves, iterate every unique session_name in
topic_bindings and call backend.reconcile_instance once per name.
Apply returned instances as overrides. Silent — the bot was offline
and the user has nothing to react to."
```

---

## Task 15: Periodic `binding_health` polling task

**Files:**
- Modify: `src/ccmux_telegram/main.py`
- Test: `tests/test_binding_health_loop.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_binding_health_loop.py
"""Periodic loop drives BindingHealth.observe and posts ✅."""
from __future__ import annotations
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccmux_telegram.binding_health import BindingHealth, Transition


@pytest.mark.asyncio
async def test_loop_posts_recovered_message() -> None:
    from ccmux_telegram.main import _binding_health_iteration

    bindings = [MagicMock(session_name="ccmux", group_chat_id=-100, thread_id=1160)]
    topics = MagicMock(); topics.all = MagicMock(return_value=bindings)

    state_cache = MagicMock()
    state_cache.is_alive = MagicMock(return_value=True)

    health = BindingHealth()
    # Seed: last observation was False, so True will produce RECOVERED.
    health.observe("ccmux", False)

    bot = MagicMock(); bot.send_message = AsyncMock()

    await _binding_health_iteration(topics, state_cache, health, bot)

    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "recovered" in text.lower()
    assert "ccmux" in text


@pytest.mark.asyncio
async def test_loop_does_not_post_on_stable() -> None:
    from ccmux_telegram.main import _binding_health_iteration

    bindings = [MagicMock(session_name="ccmux", group_chat_id=-100, thread_id=1160)]
    topics = MagicMock(); topics.all = MagicMock(return_value=bindings)

    state_cache = MagicMock(); state_cache.is_alive = MagicMock(return_value=True)
    health = BindingHealth()  # default prev=True → STABLE

    bot = MagicMock(); bot.send_message = AsyncMock()

    await _binding_health_iteration(topics, state_cache, health, bot)

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_does_not_post_on_lost() -> None:
    from ccmux_telegram.main import _binding_health_iteration

    bindings = [MagicMock(session_name="ccmux", group_chat_id=-100, thread_id=1160)]
    topics = MagicMock(); topics.all = MagicMock(return_value=bindings)

    state_cache = MagicMock(); state_cache.is_alive = MagicMock(return_value=False)
    # prev defaults True → False yields LOST
    health = BindingHealth()

    bot = MagicMock(); bot.send_message = AsyncMock()

    await _binding_health_iteration(topics, state_cache, health, bot)

    bot.send_message.assert_not_awaited()
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/test_binding_health_loop.py -v
```

Expected: FAIL — `_binding_health_iteration` doesn't exist.

- [ ] **Step 3: Implement the iteration helper and the loop in `main.py`**

```python
async def _binding_health_iteration(
    topics, state_cache, health: "BindingHealth", bot,
) -> None:
    """One pass of the binding-health detector. Used by the loop and
    exposed for unit testing without spinning a real loop."""
    for binding in topics.all():
        name = binding.session_name
        is_alive_now = state_cache.is_alive(name)
        t = health.observe(name, is_alive_now)
        if t is Transition.RECOVERED:
            try:
                await bot.send_message(
                    chat_id=binding.group_chat_id,
                    message_thread_id=binding.thread_id,
                    text=f"✅ Binding to `{name}` recovered.",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                logger.exception("Failed to post recovery notice for %s", name)


async def _run_binding_health_loop(
    topics, state_cache, health: "BindingHealth", bot,
    interval: float = 0.5,
) -> None:
    while True:
        try:
            await _binding_health_iteration(topics, state_cache, health, bot)
        except Exception:
            logger.exception("binding_health iteration failed")
        await asyncio.sleep(interval)
```

In the main entry, after the bot is started, schedule the loop as a background task:

```python
_binding_health = BindingHealth()
_health_task = asyncio.create_task(
    _run_binding_health_loop(_topics, _state_cache, _binding_health, application.bot)
)
```

Make sure to cancel `_health_task` on shutdown (whatever the bot's existing shutdown hook is).

- [ ] **Step 4: Run new tests**

```bash
uv run pytest tests/test_binding_health_loop.py -v
```

Expected: all pass.

- [ ] **Step 5: Pre-push**

```bash
uv run pytest
```

- [ ] **Step 6: Commit**

```bash
git add src/ccmux_telegram/main.py tests/test_binding_health_loop.py
git commit -m "feat(main): periodic binding_health detector + ✅ recovery notice

Background asyncio task polls state_cache.is_alive for every binding
every 0.5 s. On a False→True transition (binding recovered for any
reason — manual file edit, /rebind_window, hook fire), posts a ✅
notice to the topic. LOST is not posted here; message_out.py already
warns on the next user send."
```

---

## Task 16: Final pre-push for the feature branch

**Files:** none (verification only)

- [ ] **Step 1: Lint, type, tests**

```bash
cd ~/projects/ccmux/ccmux-telegram
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

All must pass.

- [ ] **Step 2: Manually exercise the smoke path**

```bash
# Verify the new commands and module import cleanly.
uv run python -c "from ccmux_telegram.binding_health import BindingHealth, Transition; \
  h = BindingHealth(); \
  assert h.observe('x', False) is Transition.LOST; \
  assert h.observe('x', True) is Transition.RECOVERED; \
  print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Push the feature branch**

```bash
git push -u origin feature/binding-self-heal
```

---

## Task 17: Merge `feature/binding-self-heal` → `dev`

**Files:** none (git only)

- [ ] **Step 1: Merge with `--no-ff`**

```bash
cd ~/projects/ccmux/ccmux-telegram
git checkout dev
git pull --ff-only origin dev
git merge feature/binding-self-heal --no-ff
```

- [ ] **Step 2: Pre-push checks on merged dev**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

- [ ] **Step 3: Push dev**

```bash
git push origin dev
```

- [ ] **Step 4: Delete merged feature branch**

```bash
git branch -d feature/binding-self-heal
git push origin --delete feature/binding-self-heal
```

---

## Task 18: Release telegram v3.2.0 (Release Flow)

**Files:**
- Modify: `pyproject.toml`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Branch off dev**

```bash
cd ~/projects/ccmux/ccmux-telegram
git checkout -b release/v3.2.0 dev
```

- [ ] **Step 2: Bump version**

`pyproject.toml`: `version = "3.1.2"` → `version = "3.2.0"`.

- [ ] **Step 3: Update CHANGELOG**

Insert under `## [Unreleased]`:

```markdown
## 3.2.0 — 2026-04-27

### Added

- `/rebind_window` command. Refreshes the binding's window mapping by
  asking the backend to reconcile against current tmux state. Replies
  ✅ with the new `window_id` on success, ⚠️ when the bound session
  has no live Claude.
- Startup reconcile pass: on bot start, every unique session_name in
  `topic_bindings.json` is reconciled once and any returned instance is
  installed as an in-memory override. Silent — no Telegram messages.
- Periodic binding-health detector (background asyncio task, 0.5 s
  cadence). On a false→true transition for any binding, posts
  `✅ Binding to <name> recovered.` to that topic. Triggers on any
  cause of recovery: `/rebind_window`, manual edit of
  `claude_instances.json`, future hook fires.

### Changed

- The "binding not alive" warning now points at `/rebind_window` to
  refresh the window mapping and `/rebind_topic` to switch sessions
  (was: "Use /rebind to reconnect").
- `pyproject.toml` requires `ccmux>=3.1.0,<4.0.0` for the new
  `reconcile_instance` API and `ClaudeInstanceRegistry` override
  layer.

### Removed

- `/rebind` is gone. Use `/rebind_topic` to swap which tmux session a
  topic talks to. Use `/rebind_window` to refresh the current
  session's window mapping when the bot reports "not alive".
```

- [ ] **Step 4: Commit release prep**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: bump version to 3.2.0 and update CHANGELOG"
```

- [ ] **Step 5: Merge into main with `--no-ff`**

```bash
git checkout main
git merge release/v3.2.0 --no-ff
```

- [ ] **Step 6: Tag**

```bash
git tag v3.2.0 -m "v3.2.0: /rebind_window, startup reconcile, ✅ recovery notice"
```

- [ ] **Step 7: Merge into dev with `--no-ff`**

```bash
git checkout dev
git merge release/v3.2.0 --no-ff
```

- [ ] **Step 8: Pre-push checks**

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run pyright src/
uv run pytest
```

- [ ] **Step 9: Push**

```bash
git push origin main dev --tags
```

- [ ] **Step 10: Delete release branch**

```bash
git branch -d release/v3.2.0
```

---

## Task 19: Deploy on running hosts

**Files:** none

- [ ] **Step 1: Pull + reinstall on `binks` (Linux)**

```bash
git -C ~/projects/ccmux/ccmux-backend pull --ff-only
git -C ~/projects/ccmux/ccmux-telegram pull --ff-only
uv tool install --reinstall --editable ~/projects/ccmux/ccmux-backend
uv tool install --reinstall --editable ~/projects/ccmux/ccmux-telegram
```

- [ ] **Step 2: Restart binks bot**

```bash
# Stop the existing bot
pgrep -f '/.local/share/uv/tools/ccmux-telegram/bin/python' | xargs -r kill -INT
sleep 3
# Restart it in the __ccmux__ tmux window
tmux send-keys -t __ccmux__:1 "ccmux-telegram" Enter
```

Verify a new PID is running and the log shows "Starting Telegram bot".

- [ ] **Step 3: Pull + reinstall on Mac (via SSH)**

```bash
ssh wenruiwu@al-bid-m2qftv9769 '
git -C ~/projects/ccmux-backend pull --ff-only
git -C ~/projects/ccmux-telegram pull --ff-only
uv tool install --reinstall --editable ~/projects/ccmux-backend
uv tool install --reinstall --editable ~/projects/ccmux-telegram
'
```

- [ ] **Step 4: Restart Mac bot via SSH**

```bash
ssh wenruiwu@al-bid-m2qftv9769 '
pgrep -f "/.local/share/uv/tools/ccmux-telegram/bin/python" | xargs -r kill -INT
sleep 3
tmux send-keys -t __ccmux__:1 "ccmux-telegram" Enter
'
```

Verify in Telegram by sending a message to a topic. If a binding was
stale, the bot now either fixes it silently at startup, or — if the
session has no live Claude — replies ⚠️ with the new wording.

---

## Self-review (writer's pass over the plan vs the spec)

1. **Spec coverage check:**
   - Backend `reconcile_instance` API → Task 5 ✓
   - `ClaudeInstanceRegistry` override layer → Task 4 ✓
   - `pid_session_resolver` lift + macOS portability → Tasks 2, 3 ✓
   - Frontend startup reconcile pass → Task 14 ✓
   - `/rebind_window` command → Task 13 ✓
   - `/rebind` removed (no alias) → Task 12 ✓
   - `/rebind_topic` rename → Task 12 ✓
   - Refreshed ⚠️ wording in `message_out.py` → Task 11 ✓
   - `binding_health.py` module → Task 10 ✓
   - Periodic asyncio task drive + ✅ post → Task 15 ✓
   - Both repos versioned, released through git-flow → Tasks 7-8, 17-18 ✓
   - Mac + binks redeploy → Task 19 ✓

2. **Placeholder scan:** searched for "TBD", "TODO", "implement later", "fill in details", "appropriate error handling", "add validation", "handle edge cases", "Write tests for the above", "Similar to Task". None remain. The "see implementation below" caveat in Task 5 step 3 is followed immediately by the actual implementation, not a placeholder.

3. **Type / signature consistency:**
   - `reconcile_instance(instance_id: str) -> ClaudeInstance | None` — Tasks 5, 13, 14, 15 all use this signature ✓
   - `ClaudeInstance` dataclass fields (`instance_id`, `window_id`, `session_id`, `cwd`) — used identically across tasks ✓
   - `BindingHealth.observe(instance_id, is_alive_now) -> Transition` — Tasks 10, 15 ✓
   - `Transition.RECOVERED` / `LOST` / `STABLE` — same enum across tasks ✓
   - `set_override(instance_id, instance)` and `clear_override(instance_id)` — Tasks 4, 13, 14 use these names ✓

No unresolved gaps.
