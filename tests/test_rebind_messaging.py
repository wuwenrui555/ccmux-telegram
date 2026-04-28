"""Guardrails for the '/rebind' → '/rebind_topic'/'/rebind_window' rename."""

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
    """The user-facing string '/rebind' (not /rebind_topic, not
    /rebind_window) must not appear in any shipped Python file."""
    text = _read_all_python()
    matches = [m.group(0) for m in re.finditer(r"/rebind(?!_topic|_window)\b", text)]
    assert not matches, f"Found bare /rebind references: {matches}"


def test_message_out_uses_new_wording() -> None:
    out = (SRC_ROOT / "message_out.py").read_text()
    assert "/rebind_window to refresh" in out
    assert "/rebind_topic to switch" in out


def test_rebind_topic_handler_registered() -> None:
    """The CommandHandler is registered under the new name in bot.py."""
    text = (SRC_ROOT / "bot.py").read_text()
    assert 'CommandHandler("rebind_topic"' in text
    # Assert the bare /rebind handler line (with closing quote+comma) is gone.
    assert 'CommandHandler("rebind",' not in text


def test_rebind_topic_function_exists() -> None:
    from ccmux_telegram import command_basic

    assert hasattr(command_basic, "rebind_topic_command")
    assert not hasattr(command_basic, "rebind_command")


def test_rebind_window_handler_registered() -> None:
    text = (SRC_ROOT / "bot.py").read_text()
    assert 'CommandHandler("rebind_window"' in text
