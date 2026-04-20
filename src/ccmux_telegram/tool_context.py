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
from pathlib import Path

import aiofiles

from ccmux.api import (
    ClaudeMessage,
    TranscriptParser,
    WindowBindings,
    get_default_backend,
)
from ccmux.config import config as _backend_config

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
                # Drop the partial first line when mid-file.
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
