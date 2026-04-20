"""Markdown → Telegram MarkdownV2 conversion layer.

Wraps `telegramify_markdown` and adds special handling for standard
Markdown blockquotes: any contiguous block of lines starting with `> `
(emitted by the ccmux backend for tool output, thinking blocks, and
diffs) is rendered as a Telegram expandable blockquote
(`**>...||` syntax), so the block collapses in the UI. Non-blockquote
text is handed to `telegramify_markdown` unchanged.

Key function: convert_markdown(text) → MarkdownV2 string.
"""

import re

import mistletoe
from mistletoe.block_token import BlockCode, remove_token
from telegramify_markdown import _update_block, escape_latex
from telegramify_markdown.render import TelegramMarkdownRenderer

_TABLE_SEP_RE = re.compile(r"^[\s|:\-]+$")


def _split_table_row(line: str) -> list[str]:
    """Split a table row by pipes, respecting escaped pipes (\\|)."""
    content = line.strip().strip("|")
    cells = re.split(r"(?<!\\)\|", content)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def convert_markdown_tables(text: str) -> str:
    """Convert markdown tables to card-style key-value format.

    Telegram has no table rendering. This converts each row into a card
    with **Header**: value pairs, separated by horizontal lines — similar
    to how Claude Code renders tables in narrow terminals.

    Skips tables inside code blocks.
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    in_code_block = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Track code blocks
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
            i += 1
            continue

        if in_code_block:
            result.append(line)
            i += 1
            continue

        # Check if this looks like a table header row
        if (
            stripped.startswith("|")
            and stripped.endswith("|")
            and "|" in stripped[1:-1]
        ):
            headers = _split_table_row(stripped)

            # Next line must be separator (---|---|---)
            if i + 1 < len(lines):
                sep_line = lines[i + 1].strip()
                if sep_line.startswith("|") and _TABLE_SEP_RE.match(sep_line):
                    i += 2  # Skip header + separator
                    rows: list[list[str]] = []
                    while i < len(lines):
                        data_line = lines[i].strip()
                        if data_line.startswith("|") and data_line.endswith("|"):
                            rows.append(_split_table_row(data_line))
                            i += 1
                        else:
                            break

                    # Build card-style output
                    separator = "────────────"
                    cards: list[str] = []
                    for row in rows:
                        card_lines: list[str] = []
                        for j, header in enumerate(headers):
                            value = row[j] if j < len(row) else ""
                            if value:
                                card_lines.append(f"**{header}**: {value}")
                            else:
                                card_lines.append(f"**{header}**: —")
                        cards.append("\n".join(card_lines))

                    result.append(f"\n{separator}\n".join(cards))
                    continue

        result.append(line)
        i += 1

    return "\n".join(result)


# Characters that must be escaped in Telegram MarkdownV2 plain text
_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _escape_mdv2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


# Matches a single Markdown blockquote line: "> content" or bare ">" (blank
# line inside a blockquote). Anchored to line start via MULTILINE.
_BLOCKQUOTE_LINE_RE = re.compile(r"^>(?: (.*))?$")

# Max rendered chars for a single expandable quote block.
# Leaves room for surrounding text within Telegram's 4096 char message limit.
_EXPQUOTE_MAX_RENDERED = 3800


def _split_blockquote_segments(text: str) -> list[tuple[bool, str]]:
    """Segment `text` into (is_blockquote, content) pairs.

    Walks line by line, tracking fenced code blocks (``` boundaries) so
    lines starting with `>` inside a code block are not mistaken for
    blockquotes. Contiguous `^> ` lines outside code form one blockquote
    segment; everything else is a plain segment.
    """
    segments: list[tuple[bool, str]] = []
    plain_buf: list[str] = []
    quote_buf: list[str] = []
    in_code = False

    def flush_plain() -> None:
        if plain_buf:
            segments.append((False, "\n".join(plain_buf)))
            plain_buf.clear()

    def flush_quote() -> None:
        if quote_buf:
            segments.append((True, "\n".join(quote_buf)))
            quote_buf.clear()

    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            # Code fence toggles; `>` inside is literal.
            flush_quote()
            in_code = not in_code
            plain_buf.append(line)
            continue

        if not in_code and _BLOCKQUOTE_LINE_RE.match(line):
            flush_plain()
            quote_buf.append(line)
        else:
            flush_quote()
            plain_buf.append(line)

    flush_plain()
    flush_quote()
    return segments


def _render_expandable_quote(block_text: str) -> str:
    """Render a contiguous blockquote segment as a Telegram expandable
    blockquote (raw MarkdownV2).

    `block_text` is the original multi-line source — each line starts
    with `>` or `> `. The leading `> ` is stripped before MarkdownV2
    escaping, then re-prepended in the rendered output.

    Truncates to `_EXPQUOTE_MAX_RENDERED` chars so the final message
    fits within Telegram's 4096 limit.
    """
    # Strip the leading blockquote marker from each source line.
    raw_lines: list[str] = []
    for line in block_text.split("\n"):
        m = _BLOCKQUOTE_LINE_RE.match(line)
        raw_lines.append(m.group(1) or "" if m else line)

    built: list[str] = []
    total_len = 0
    suffix = "\n>… \\(truncated\\)||"
    budget = _EXPQUOTE_MAX_RENDERED - len(suffix)
    truncated = False
    for raw in raw_lines:
        escaped = _escape_mdv2(raw)
        line_cost = 1 + len(escaped) + 1  # ">" + line + "\n"
        if total_len + line_cost > budget:
            remaining = budget - total_len - 2
            if remaining > 20:
                built.append(f">{escaped[:remaining]}")
            truncated = True
            break
        built.append(f">{escaped}")
        total_len += line_cost
    if truncated:
        return "\n".join(built) + suffix
    return "\n".join(built) + "||"


def _markdownify(text: str) -> str:
    """Custom markdownify with our rendering rules.

    Wraps TelegramMarkdownRenderer directly (instead of calling
    telegramify_markdown.markdownify) so we can tweak token rules
    inside the context manager — reset_tokens() in __exit__ would
    otherwise undo any module-level changes.

    Custom rules:
      - Disable indented code blocks (only fenced ``` blocks are code).
    """
    with TelegramMarkdownRenderer(normalize_whitespace=False) as renderer:
        remove_token(BlockCode)
        content = escape_latex(text)
        document = mistletoe.Document(content)
        _update_block(document)
        return renderer.render(document)


def convert_markdown(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2 format.

    Contiguous Markdown blockquote regions (lines starting with `> `)
    are extracted and rendered as Telegram expandable blockquotes so
    they collapse in the UI; surrounding text is handed to
    `telegramify_markdown` unchanged.
    """
    # Convert markdown tables to card-style format before telegramify
    text = convert_markdown_tables(text)

    segments = _split_blockquote_segments(text)
    if not any(is_quote for is_quote, _ in segments):
        return _markdownify(text)

    parts: list[str] = []
    for i, (is_quote, segment) in enumerate(segments):
        if is_quote:
            rendered = _render_expandable_quote(segment)
            # Ensure the expandable quote sits on its own line so the
            # leading ">" is recognized as block syntax by Telegram.
            if parts and not parts[-1].endswith("\n"):
                parts.append("\n")
            parts.append(rendered)
            # Separator after the quote (unless it's the last segment).
            if i < len(segments) - 1:
                parts.append("\n")
        else:
            parts.append(_markdownify(segment))
    return "".join(parts)
