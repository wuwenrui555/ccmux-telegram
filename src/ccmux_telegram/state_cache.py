"""Frontend-side last-state cache.

The backend is a stateless observer; it re-emits ClaudeState every
fast tick. Consumers that need "is this instance alive?" or edge
detection maintain the cache themselves.

This module is the single in-process cache shared by every consumer
that cares (topic_bindings.is_alive, watcher, status_line).
"""

from __future__ import annotations

from ccmux.api import ClaudeState, Dead


class StateCache:
    """``{instance_id: last ClaudeState}`` with a convenience ``is_alive``."""

    def __init__(self) -> None:
        self._data: dict[str, ClaudeState] = {}

    def update(self, instance_id: str, state: ClaudeState) -> None:
        self._data[instance_id] = state

    def get(self, instance_id: str) -> ClaudeState | None:
        return self._data.get(instance_id)

    def is_alive(self, instance_id: str) -> bool:
        state = self._data.get(instance_id)
        if state is None:
            return False
        return not isinstance(state, Dead)


# Module-level singleton for convenience.
_cache = StateCache()


def get_state_cache() -> StateCache:
    return _cache
