"""Guardrails for the rebind command surface.

v4.0.0: only ``/rebind_topic`` survives. ``/rebind`` is gone (was the
v2 name) and ``/rebind_window`` is gone (was a v3.1 workaround for
the override layer; the EventLogReader auto-refreshes the binding
on every UserPromptSubmit, so manual refresh is no longer
meaningful).
"""

from __future__ import annotations

import pathlib
import re

SRC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "ccmux_telegram"


def _read_all_python() -> str:
    text: list[str] = []
    for p in SRC_ROOT.rglob("*.py"):
        text.append(p.read_text())
    return "\n".join(text)


def test_no_bare_rebind_command_in_user_text() -> None:
    """The user-facing strings ``/rebind`` (bare) and ``/rebind_window``
    must not appear in any shipped Python file."""
    text = _read_all_python()
    bare = [m.group(0) for m in re.finditer(r"/rebind(?!_topic)\b", text)]
    assert not bare, f"Found bare or removed /rebind references: {bare}"


def test_message_out_uses_new_wording() -> None:
    out = (SRC_ROOT / "message_out.py").read_text()
    assert "/rebind_topic to switch" in out
    assert "/rebind_window" not in out


def test_rebind_topic_handler_registered() -> None:
    """The CommandHandler is registered under the new name in bot.py."""
    text = (SRC_ROOT / "bot.py").read_text()
    assert 'CommandHandler("rebind_topic"' in text
    assert 'CommandHandler("rebind",' not in text
    assert 'CommandHandler("rebind_window"' not in text


def test_rebind_topic_function_exists() -> None:
    from ccmux_telegram import command_basic

    assert hasattr(command_basic, "rebind_topic_command")
    assert not hasattr(command_basic, "rebind_command")
    assert not hasattr(command_basic, "rebind_window_command")
