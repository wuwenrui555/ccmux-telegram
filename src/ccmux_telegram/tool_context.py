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
