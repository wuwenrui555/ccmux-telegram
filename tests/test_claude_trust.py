"""Tests for claude_trust.mark_dir_trusted.

Claude Code shows a "Trust this folder?" prompt the first time it
opens a directory. Until the user answers, SessionStart hooks do not
fire and ccmux's window_bindings stays empty for that window, so
messages to the bound topic fail with "has no window yet". The picker
flow is an explicit user gesture (they chose the target directory on
purpose), so mark_dir_trusted pre-populates the trust flag in
~/.claude.json and lets Claude skip the dialog.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def claude_json(tmp_path, monkeypatch):
    """Redirect the module-level _CLAUDE_JSON constant to a temp file."""
    from ccmux_telegram import claude_trust

    path = tmp_path / ".claude.json"
    monkeypatch.setattr(claude_trust, "_CLAUDE_JSON", path)
    return path


def test_sets_flag_when_file_missing(claude_json, tmp_path):
    """No ~/.claude.json yet: create a minimal one with the trust flag."""
    from ccmux_telegram.claude_trust import mark_dir_trusted

    target = tmp_path / "proj"
    target.mkdir()

    assert mark_dir_trusted(target) is True
    data = json.loads(claude_json.read_text())
    assert data["projects"][str(target.resolve())]["hasTrustDialogAccepted"] is True


def test_adds_entry_when_dir_missing(claude_json, tmp_path):
    """File exists with other projects: add a new entry, preserve others."""
    claude_json.write_text(
        json.dumps(
            {
                "projects": {
                    "/some/other/proj": {"hasTrustDialogAccepted": True, "keep": 1}
                },
                "unrelatedTopLevel": "preserve me",
            }
        )
    )

    from ccmux_telegram.claude_trust import mark_dir_trusted

    target = tmp_path / "proj"
    target.mkdir()

    assert mark_dir_trusted(target) is True
    data = json.loads(claude_json.read_text())
    assert data["projects"][str(target.resolve())]["hasTrustDialogAccepted"] is True
    # Other entries untouched.
    assert data["projects"]["/some/other/proj"] == {
        "hasTrustDialogAccepted": True,
        "keep": 1,
    }
    assert data["unrelatedTopLevel"] == "preserve me"


def test_flips_false_to_true_preserving_siblings(claude_json, tmp_path):
    """Entry exists with hasTrustDialogAccepted=False: flip to True,
    leave the other keys in that entry alone."""
    target = tmp_path / "proj"
    target.mkdir()
    abspath = str(target.resolve())
    claude_json.write_text(
        json.dumps(
            {
                "projects": {
                    abspath: {
                        "hasTrustDialogAccepted": False,
                        "allowedTools": ["Bash"],
                        "lastCost": 0.42,
                    }
                }
            }
        )
    )

    from ccmux_telegram.claude_trust import mark_dir_trusted

    assert mark_dir_trusted(target) is True
    entry = json.loads(claude_json.read_text())["projects"][abspath]
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["allowedTools"] == ["Bash"]
    assert entry["lastCost"] == 0.42


def test_idempotent_when_already_true(claude_json, tmp_path):
    """Already True: the helper reports success without rewriting the file."""
    target = tmp_path / "proj"
    target.mkdir()
    abspath = str(target.resolve())
    original = {
        "projects": {abspath: {"hasTrustDialogAccepted": True, "marker": "keep"}}
    }
    claude_json.write_text(json.dumps(original))

    from ccmux_telegram.claude_trust import mark_dir_trusted

    assert mark_dir_trusted(target) is True
    assert json.loads(claude_json.read_text()) == original


def test_returns_false_on_corrupt_json(claude_json, tmp_path):
    """Refuse to clobber a file we could not parse."""
    claude_json.write_text("}{not json{{{")

    from ccmux_telegram.claude_trust import mark_dir_trusted

    target = tmp_path / "proj"
    target.mkdir()
    assert mark_dir_trusted(target) is False
    # File left untouched.
    assert claude_json.read_text() == "}{not json{{{"


def test_returns_false_when_projects_is_not_object(claude_json, tmp_path):
    """Odd schema: refuse rather than silently create a wrong shape."""
    claude_json.write_text(json.dumps({"projects": "not a dict"}))

    from ccmux_telegram.claude_trust import mark_dir_trusted

    target = tmp_path / "proj"
    target.mkdir()
    assert mark_dir_trusted(target) is False


def test_write_failure_returns_false(claude_json, tmp_path):
    """Write-side IO error surfaces as False, not an exception."""
    claude_json.write_text(json.dumps({"projects": {}}))

    from ccmux_telegram import claude_trust

    target = tmp_path / "proj"
    target.mkdir()
    with patch.object(
        claude_trust.os, "replace", side_effect=OSError("permission denied")
    ):
        assert claude_trust.mark_dir_trusted(target) is False


def test_atomic_write_via_tmp_then_replace(claude_json, tmp_path):
    """Writes go through a sibling tmp file + os.replace, never a naked
    truncate-then-write of the real file."""
    claude_json.write_text(json.dumps({"projects": {}}))

    from ccmux_telegram import claude_trust

    target = tmp_path / "proj"
    target.mkdir()

    replace_calls: list[tuple[str, str]] = []
    original_replace = claude_trust.os.replace

    def _spy_replace(src, dst):
        replace_calls.append((str(src), str(dst)))
        return original_replace(src, dst)

    with patch.object(claude_trust.os, "replace", side_effect=_spy_replace):
        assert claude_trust.mark_dir_trusted(target) is True

    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    assert dst == str(claude_json)
    assert src != dst
    assert src.startswith(str(claude_json))


@pytest.mark.asyncio
async def test_create_session_and_bind_pre_trusts_selected_path(
    claude_json, tmp_path, monkeypatch
):
    """binding_flow._create_session_and_bind calls mark_dir_trusted before
    handing off to tmux so Claude does not block on the trust dialog."""
    from unittest.mock import AsyncMock, MagicMock

    claude_json.write_text(json.dumps({"projects": {}}))

    from ccmux_telegram import binding_flow

    target = tmp_path / "proj"
    target.mkdir()

    # Stub out the actual tmux work; we only care that trust is marked
    # before tm.get_session()/create_session would have run.
    fake_tm = MagicMock()
    fake_tm.get_session = MagicMock(return_value=None)
    fake_tm.create_session = AsyncMock(
        return_value=(True, "Created", "win", "@100")
    )
    monkeypatch.setattr(
        binding_flow.tmux_registry, "get_or_create", lambda _n: fake_tm
    )
    monkeypatch.setattr(
        binding_flow.tmux_registry, "update_window_map", lambda *_args: None
    )

    # Short-circuit the post-creation hook wait loop and binding writes.
    async def _instant_load():
        return None

    fake_windows = MagicMock()
    fake_windows.load = AsyncMock(side_effect=_instant_load)
    fake_windows.is_session_in_map = MagicMock(return_value=True)
    monkeypatch.setattr(binding_flow, "_windows", fake_windows)

    fake_topics = MagicMock()
    monkeypatch.setattr(binding_flow, "_topics", fake_topics)

    query = MagicMock()
    query.message = MagicMock()
    query.message.chat = MagicMock()
    query.message.chat.id = -1
    query.answer = AsyncMock()
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot = MagicMock()
    ctx.bot.edit_forum_topic = AsyncMock()

    user = MagicMock()
    user.id = 1

    # Patch safe_edit so we don't try to hit Telegram.
    monkeypatch.setattr(binding_flow, "safe_edit", AsyncMock())

    await binding_flow._create_session_and_bind(
        query,
        ctx,
        user,
        str(target),
        "proj",
        pending_thread_id=42,
        skip_permissions=False,
    )

    data = json.loads(claude_json.read_text())
    assert data["projects"][str(target.resolve())]["hasTrustDialogAccepted"] is True
