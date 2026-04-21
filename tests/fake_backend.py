"""In-memory FakeBackend that satisfies the Backend Protocol.

Use in tests that need to exercise frontend code paths without
spinning up tmux, JSONL files, or polling loops. Records every call on
`.calls` and exposes knobs for canned return values.

Example:

    fake = FakeBackend()
    fake.instances["alpha"] = ClaudeInstance(
        instance_id="alpha", window_id="@0", session_id="sess-1", cwd="/tmp"
    )
    set_default_backend(fake)
    ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ccmux.api import (
    ClaudeInstance,
    ClaudeSession,
    ClaudeState,
    ClaudeMessage,
    TmuxWindow,
)


@dataclass
class _FakeTmuxOps:
    """Tmux-side fake. Shares the parent backend's call log and state."""

    _parent: FakeBackend

    async def send_text(self, window_id: str, text: str) -> tuple[bool, str]:
        self._parent._record("tmux.send_text", window_id, text)
        return True, "ok"

    async def send_keys(self, window_id: str, keys: list[str]) -> None:
        self._parent._record("tmux.send_keys", window_id, keys)

    async def capture_pane(self, window_id: str) -> str:
        self._parent._record("tmux.capture_pane", window_id)
        return self._parent.pane_text.get(window_id, "")

    async def create_window(self, cwd: str, session_name: str | None = None) -> str:
        self._parent._record("tmux.create_window", cwd, session_name)
        return "@fake"

    async def list_windows(self) -> list[TmuxWindow]:
        self._parent._record("tmux.list_windows")
        return []


@dataclass
class _FakeClaudeOps:
    """Claude-JSONL-side fake. Shares the parent backend's call log and state."""

    _parent: FakeBackend

    async def list_sessions(self, cwd: str) -> list[ClaudeSession]:
        self._parent._record("claude.list_sessions", cwd)
        return self._parent.claude_sessions.get(cwd, [])

    async def get_history(
        self,
        session_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> list[dict]:
        self._parent._record(
            "claude.get_history", session_id, start_byte=start_byte, end_byte=end_byte
        )
        return self._parent.history.get(session_id, [])


@dataclass
class FakeBackend:
    """In-memory double for the Backend Protocol.

    Sub-namespace impls (`.tmux`, `.claude`) record onto the same `calls`
    list, prefixed with their domain — so tests can assert precise
    dispatch (`"tmux.send_text"` vs `"claude.get_history"`).
    """

    calls: list[tuple[str, tuple, dict]] = field(default_factory=list)
    instances: dict[str, ClaudeInstance] = field(default_factory=dict)
    pane_text: dict[str, str] = field(default_factory=dict)
    claude_sessions: dict[str, list[ClaudeSession]] = field(default_factory=dict)
    history: dict[str, list[dict]] = field(default_factory=dict)
    on_state: Callable[[str, ClaudeState], Awaitable[None]] | None = None
    on_message: Callable[[str, ClaudeMessage], Awaitable[None]] | None = None
    started: bool = False
    stopped: bool = False
    tmux: Any = field(init=False)
    claude: Any = field(init=False)

    def __post_init__(self) -> None:
        self.tmux = _FakeTmuxOps(self)
        self.claude = _FakeClaudeOps(self)

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def get_instance(self, instance_id: str) -> ClaudeInstance | None:
        self._record("get_instance", instance_id)
        return self.instances.get(instance_id)

    async def start(
        self,
        on_state: Callable[[str, ClaudeState], Awaitable[None]],
        on_message: Callable[[str, ClaudeMessage], Awaitable[None]],
    ) -> None:
        self._record("start")
        self.on_state = on_state
        self.on_message = on_message
        self.started = True

    async def stop(self) -> None:
        self._record("stop")
        self.stopped = True

    # ---- Test-helper methods (not part of the Protocol) ----

    async def emit_state(self, instance_id: str, state: ClaudeState) -> None:
        """Simulate a real-time ClaudeState dispatch to the registered callback."""
        assert self.on_state is not None, "Call start() before emit_state()"
        await self.on_state(instance_id, state)

    async def emit_message(self, instance_id: str, msg: ClaudeMessage) -> None:
        """Simulate a real-time ClaudeMessage dispatch to the registered callback."""
        assert self.on_message is not None, "Call start() before emit_message()"
        await self.on_message(instance_id, msg)
