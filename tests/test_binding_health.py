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
