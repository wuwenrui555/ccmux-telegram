"""Enforce that the frontend imports the backend only via `ccmux.api`.

`ccmux_telegram/*.py` may `from ccmux.api import ...`, but any reach
into a backend submodule (`ccmux.tmux`, `ccmux.backend`, ...) is a
boundary violation. Keeping the constraint enforced at test time means
a bad import can't silently ship — it flips CI red immediately.

(`ccmux.config` is permitted for the display-policy flag
`show_user_messages` only; direct imports of other backend modules are
still rejected.)
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND_ROOT = Path(__file__).resolve().parents[1] / "src" / "ccmux_telegram"

_ALLOWED = {"api", "config"}

_FROM_IMPORT = re.compile(r"^\s*from\s+ccmux\.(\w+)", re.MULTILINE)
_BARE_IMPORT = re.compile(r"^\s*import\s+ccmux\.(\w+)", re.MULTILINE)


def test_frontend_imports_only_via_api() -> None:
    violations: list[str] = []
    for path in FRONTEND_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in (_FROM_IMPORT, _BARE_IMPORT):
            for m in pattern.finditer(text):
                submodule = m.group(1)
                if submodule not in _ALLOWED:
                    violations.append(f"{path.name}: ccmux.{submodule}")
    assert not violations, (
        "frontend may only import from ccmux.api (or ccmux.config):\n  "
        + "\n  ".join(violations)
    )
