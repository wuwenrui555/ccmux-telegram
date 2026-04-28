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
