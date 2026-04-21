"""Pre-mark directories as trusted in Claude Code's ~/.claude.json.

Claude Code gates access to a new working directory behind a "Trust
this folder?" confirmation. Until the user answers, no SessionStart
hook fires, so ccmux's window_bindings never gets an entry for the
new window and messages to the bound topic fail with "has no window
yet". The picker flow is an explicit user gesture (they picked the
target directory on purpose), so we treat that as equivalent consent
and flip the trust flag before starting Claude.

The helper is deliberately best-effort: any failure returns False
rather than raising, so the caller can fall through to the old
behaviour and the user sees the pre-fix "has no window yet" error
(manual `tmux attach` + Enter still works). Worst case we do not
make things worse.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_CLAUDE_JSON = Path.home() / ".claude.json"


def mark_dir_trusted(dir_path: str | Path) -> bool:
    """Set hasTrustDialogAccepted=True for `dir_path` in ~/.claude.json.

    Returns True when the file reflects the trusted state (either we
    wrote it or it was already set). Returns False on any failure
    (unreadable/corrupt JSON, unexpected schema, write error).
    """
    try:
        abspath = str(Path(dir_path).expanduser().resolve())
    except (OSError, RuntimeError) as e:
        logger.warning("Could not resolve dir %r: %s", dir_path, e)
        return False

    data: dict = {}
    if _CLAUDE_JSON.is_file():
        try:
            data = json.loads(_CLAUDE_JSON.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Could not read %s: %s", _CLAUDE_JSON, e)
            return False
        if not isinstance(data, dict):
            logger.warning(
                "%s top-level is not an object; refusing to modify", _CLAUDE_JSON
            )
            return False

    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        logger.warning("%s.projects is not an object; refusing to modify", _CLAUDE_JSON)
        return False

    entry = projects.setdefault(abspath, {})
    if not isinstance(entry, dict):
        logger.warning(
            "%s.projects[%r] is not an object; refusing to modify",
            _CLAUDE_JSON,
            abspath,
        )
        return False

    if entry.get("hasTrustDialogAccepted") is True:
        return True

    entry["hasTrustDialogAccepted"] = True

    tmp = _CLAUDE_JSON.with_name(_CLAUDE_JSON.name + ".ccmux-tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, _CLAUDE_JSON)
    except OSError as e:
        logger.warning("Could not write %s: %s", _CLAUDE_JSON, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    logger.info("Marked %s as trusted in ~/.claude.json", abspath)
    return True
