"""Tests for the per-instance last-state cache."""

from ccmux.api import Dead, Idle, Working

from ccmux_telegram.state_cache import StateCache


class TestStateCache:
    def test_unknown_instance_is_not_alive(self) -> None:
        cache = StateCache()
        assert cache.is_alive("missing") is False

    def test_working_is_alive(self) -> None:
        cache = StateCache()
        cache.update("a", Working(status_text="Thinking…"))
        assert cache.is_alive("a") is True

    def test_idle_is_alive(self) -> None:
        cache = StateCache()
        cache.update("a", Idle())
        assert cache.is_alive("a") is True

    def test_dead_is_not_alive(self) -> None:
        cache = StateCache()
        cache.update("a", Dead())
        assert cache.is_alive("a") is False

    def test_most_recent_state_wins(self) -> None:
        cache = StateCache()
        cache.update("a", Working(status_text="Reading…"))
        assert cache.is_alive("a") is True
        cache.update("a", Dead())
        assert cache.is_alive("a") is False

    def test_get_returns_last_state(self) -> None:
        cache = StateCache()
        w = Working(status_text="Running…")
        cache.update("a", w)
        assert cache.get("a") is w
        assert cache.get("missing") is None


class TestStateCacheChangeDetection:
    """``update()`` returns True only when the new state differs."""

    def test_first_update_is_change(self) -> None:
        cache = StateCache()
        assert cache.update("a", Idle()) is True

    def test_same_state_is_not_change(self) -> None:
        cache = StateCache()
        cache.update("a", Idle())
        assert cache.update("a", Idle()) is False

    def test_different_working_text_is_change(self) -> None:
        cache = StateCache()
        cache.update("a", Working(status_text="Reading…"))
        assert cache.update("a", Working(status_text="Thinking…")) is True

    def test_same_working_text_is_not_change(self) -> None:
        cache = StateCache()
        cache.update("a", Working(status_text="Reading…"))
        assert cache.update("a", Working(status_text="Reading…")) is False

    def test_different_variant_is_change(self) -> None:
        cache = StateCache()
        cache.update("a", Idle())
        assert cache.update("a", Dead()) is True

    def test_change_is_per_instance(self) -> None:
        cache = StateCache()
        cache.update("a", Idle())
        # A separate instance_id is always a change (no prior state).
        assert cache.update("b", Idle()) is True
