"""Telegram-side rendering for `Working.status_text`.

The ccmux backend returns `status_text` verbatim from the terminal
pane: a spinner line followed by optional TodoWrite rows in CC's
original form (`⎿` elbow on the first row, unicode checkbox glyphs
`◻` / `◼` / `✔`, CC indentation, `      … +N pending` overflow tail).

This module is the single place that translates that raw form into
what looks good inside a Telegram message:

- drop the `⎿` elbow connector,
- normalize every row's leading whitespace to two spaces,
- map CC's unicode glyphs to ASCII brackets (`[ ]` / `[>]` / `[x]`)
  so they render at a stable narrow width without emoji variants,
- wrap completed rows in GitHub-flavored `~~...~~` strikethrough
  (telegramify_markdown converts to the MarkdownV2 single-tilde form),
- truncate long rows to keep individual status messages within a
  sensible display budget, while keeping the closing `~~` balanced
  on completed rows so the MarkdownV2 parse does not fall back.

Keep all Telegram-specific rendering decisions here. Parser-level
changes (what counts as skippable, spinner detection, chrome
handling) belong in ccmux-backend.
"""

from __future__ import annotations

_TODO_ROW_MAX_LEN = 50
_ELBOW = "⎿"

_BOX_PENDING = "[ ]"
_BOX_IN_PROGRESS = "[>]"
_BOX_DONE = "[x]"

_BRACKET_MAP: dict[str, str] = {
    "◻": _BOX_PENDING,
    "☐": _BOX_PENDING,
    "◼": _BOX_IN_PROGRESS,
    "☒": _BOX_DONE,
    "✔": _BOX_DONE,
    "✓": _BOX_DONE,
}

_CHECKBOX_GLYPHS = frozenset(_BRACKET_MAP)

_INDENT = "  "
_STRIKE_OPEN = "~~"
_STRIKE_CLOSE = "~~"


def _render_row(line: str) -> str:
    """Render one TodoWrite row (checkbox or overflow tail)."""
    stripped = line.lstrip()
    if stripped.startswith(_ELBOW):
        stripped = stripped[1:].lstrip()

    if stripped and stripped[0] in _CHECKBOX_GLYPHS:
        glyph = stripped[0]
        rest = stripped[1:].lstrip()
        bracket = _BRACKET_MAP[glyph]
        body = f"{bracket} {rest}"
        budget = _TODO_ROW_MAX_LEN - len(_INDENT)
        if bracket == _BOX_DONE:
            budget -= len(_STRIKE_OPEN) + len(_STRIKE_CLOSE)
        if len(body) > budget:
            body = body[:budget] + "…"
        if bracket == _BOX_DONE:
            return f"{_INDENT}{_STRIKE_OPEN}{body}{_STRIKE_CLOSE}"
        return f"{_INDENT}{body}"

    # Overflow tail ("… +N pending") or any row not leading with a
    # checkbox: normalize indent, truncate, pass through.
    result = f"{_INDENT}{stripped}"
    if len(result) > _TODO_ROW_MAX_LEN:
        result = result[:_TODO_ROW_MAX_LEN] + "…"
    return result


def render_status_text(raw: str) -> str:
    """Translate backend `Working.status_text` for a Telegram message.

    The first line is the spinner text and is passed through verbatim
    (Telegram renders its characters fine, no special escaping needed
    for the status sentence itself). Every subsequent line is treated
    as a TodoWrite row and run through `_render_row`.
    """
    if not raw:
        return raw
    lines = raw.split("\n")
    if len(lines) == 1:
        return raw
    rendered_rows = [_render_row(line) for line in lines[1:]]
    return "\n".join([lines[0], *rendered_rows])
