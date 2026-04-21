"""Tests for status_render — TodoWrite row translation for Telegram."""

from ccmux_telegram.status_render import render_status_text


# ── no-op cases ────────────────────────────────────────────────────────


def test_empty_input_passes_through():
    assert render_status_text("") == ""


def test_single_line_passes_through():
    """Spinner-only status has no TodoWrite rows to render."""
    assert render_status_text("Thinking…") == "Thinking…"


def test_spinner_line_itself_never_rewritten():
    """Even when followed by rows, the first line is unchanged."""
    raw = "Nesting… (12s · thinking)\n  ⎿  ◼ Task 1\n"
    rendered = render_status_text(raw)
    assert rendered.split("\n")[0] == "Nesting… (12s · thinking)"


# ── elbow dropping and indent normalization ────────────────────────────


def test_elbow_connector_dropped():
    raw = "Spinner…\n  ⎿  ◼ Task text"
    assert render_status_text(raw) == "Spinner…\n  [>] Task text"


def test_subsequent_rows_reindented_to_two_spaces():
    raw = "Spinner…\n  ⎿  ◼ Row 1\n     ◻ Row 2\n     ◻ Row 3"
    assert render_status_text(raw) == (
        "Spinner…\n  [>] Row 1\n  [ ] Row 2\n  [ ] Row 3"
    )


def test_overflow_tail_indent_normalized():
    raw = "Spinner…\n  ⎿  ◼ Task\n      … +7 pending"
    assert render_status_text(raw) == ("Spinner…\n  [>] Task\n  … +7 pending")


# ── glyph → bracket translation ────────────────────────────────────────


def test_pending_glyphs_translate_to_empty_bracket():
    for glyph in ("◻", "☐"):
        raw = f"Sp…\n  {glyph} Task"
        assert render_status_text(raw) == "Sp…\n  [ ] Task", glyph


def test_in_progress_glyph_translates_to_arrow_bracket():
    raw = "Sp…\n  ◼ Task"
    assert render_status_text(raw) == "Sp…\n  [>] Task"


def test_done_glyphs_translate_to_x_bracket_with_strikethrough():
    for glyph in ("✔", "✓", "☒"):
        raw = f"Sp…\n  {glyph} Task"
        assert render_status_text(raw) == "Sp…\n  ~~[x] Task~~", glyph


# ── truncation ─────────────────────────────────────────────────────────


def test_long_row_truncated_at_50_chars():
    raw = f"Sp…\n  ◼ {'A' * 80}"
    out = render_status_text(raw).split("\n")[1]
    assert out.startswith("  [>] ")
    assert out.endswith("…")
    assert len(out) == 51  # 50 budget + trailing ellipsis


def test_done_row_truncation_preserves_closing_tilde():
    """Unbalanced `~~...~~` would make MarkdownV2 fall back to plain."""
    raw = f"Sp…\n  ✔ {'B' * 80}"
    out = render_status_text(raw).split("\n")[1]
    assert out.startswith("  ~~[x] ")
    assert out.endswith("~~")


def test_row_at_exactly_50_chars_not_truncated():
    """Budget is inclusive: only rows exceeding 50 get the ellipsis."""
    task_text = "X" * 44  # 2 indent + "[>] " (4) + 44 = 50
    raw = f"Sp…\n  ◼ {task_text}"
    assert render_status_text(raw) == f"Sp…\n  [>] {task_text}"


# ── integration-ish ────────────────────────────────────────────────────


def test_full_todowrite_block_renders_as_expected():
    raw = (
        "Refactoring parser… (2m · thinking)\n"
        "  ⎿  ✔ Refactor data layer\n"
        "     ◼ Refactor parser\n"
        "     ◻ Refactor state machine\n"
        "      … +6 pending, 1 completed"
    )
    assert render_status_text(raw) == (
        "Refactoring parser… (2m · thinking)\n"
        "  ~~[x] Refactor data layer~~\n"
        "  [>] Refactor parser\n"
        "  [ ] Refactor state machine\n"
        "  … +6 pending, 1 completed"
    )
