"""In-memory FakeBackend that satisfies the Backend Protocol.

Use in tests that need to exercise frontend code paths without
spinning up tmux, JSONL files, or polling loops. Records every call on
`.calls` and exposes knobs for canned return values.

Example:

    fake = FakeBackend()
    fake.window_binding["@0"] = WindowBinding(...)
    fake.alive["@0"] = True
    set_default_backend(fake)
    ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ccmux.status_monitor import WindowStatus
from ccmux.tmux import TmuxWindow
from ccmux.claude_transcript_parser import ClaudeMessage
from ccmux.window_bindings import ClaudeSession, WindowBinding


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
    window_binding: dict[str, WindowBinding] = field(default_factory=dict)
    alive: dict[str, bool] = field(default_factory=dict)
    pane_text: dict[str, str] = field(default_factory=dict)
    claude_sessions: dict[str, list[ClaudeSession]] = field(default_factory=dict)
    history: dict[str, list[dict]] = field(default_factory=dict)
    on_message: Callable[[ClaudeMessage], Awaitable[None]] | None = None
    on_status: Callable[[WindowStatus], Awaitable[None]] | None = None
    started: bool = False
    stopped: bool = False
    tmux: Any = field(init=False)
    claude: Any = field(init=False)

    def __post_init__(self) -> None:
        self.tmux = _FakeTmuxOps(self)
        self.claude = _FakeClaudeOps(self)

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def is_alive(self, window_id: str) -> bool:
        self._record("is_alive", window_id)
        return self.alive.get(window_id, False)

    def get_window_binding(self, window_id: str) -> WindowBinding | None:
        self._record("get_window_binding", window_id)
        return self.window_binding.get(window_id)

    async def start(
        self,
        on_message: Callable[[ClaudeMessage], Awaitable[None]],
        on_status: Callable[[WindowStatus], Awaitable[None]],
    ) -> None:
        self._record("start")
        self.on_message = on_message
        self.on_status = on_status
        self.started = True

    async def stop(self) -> None:
        self._record("stop")
        self.stopped = True

    # ---- Test-helper methods (not part of the Protocol) ----

    async def emit_message(self, msg: ClaudeMessage) -> None:
        """Simulate a real-time ClaudeMessage dispatch to the registered callback."""
        assert self.on_message is not None, "Call start() before emit_message()"
        await self.on_message(msg)

    async def emit_status(self, status: WindowStatus) -> None:
        """Simulate a WindowStatus dispatch to the registered callback."""
        assert self.on_status is not None, "Call start() before emit_status()"
        await self.on_status(status)
