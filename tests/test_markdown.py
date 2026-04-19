"""Tests for Markdown → Telegram MarkdownV2 conversion."""

import pytest

from ccmux_telegram.markdown import _escape_mdv2, convert_markdown


class TestEscapeMdv2:
    @pytest.mark.parametrize(
        "input_text,expected",
        [
            (
                "_*[]()~>#+\\-=|{}.!",
                "\\_\\*\\[\\]\\(\\)\\~\\>\\#\\+\\\\\\-\\=\\|\\{\\}\\.\\!",
            ),
            ("hello world 123", "hello world 123"),
            ("", ""),
        ],
        ids=["special-chars", "alphanumeric-unchanged", "empty-string"],
    )
    def test_escape(self, input_text: str, expected: str) -> None:
        assert _escape_mdv2(input_text) == expected


class TestConvertMarkdown:
    def test_plain_text(self) -> None:
        result = convert_markdown("hello world")
        assert "hello world" in result

    def test_bold(self) -> None:
        result = convert_markdown("**bold text**")
        assert "*bold text*" in result
        assert "**bold text**" not in result

    def test_code_block_preserved(self) -> None:
        result = convert_markdown("```python\nprint('hi')\n```")
        assert "```" in result
        assert "print" in result

    def test_blockquote_renders_as_expandable_quote(self) -> None:
        """A standalone Markdown blockquote from the backend is rendered
        as a Telegram expandable blockquote (`>…||`)."""
        text = "> quoted content"
        result = convert_markdown(text)
        assert ">quoted content||" in result

    def test_multiline_blockquote(self) -> None:
        text = "> line one\n> line two\n> line three"
        result = convert_markdown(text)
        assert ">line one" in result
        assert ">line two" in result
        assert ">line three" in result
        assert result.rstrip().endswith("||")

    def test_mixed_text_and_blockquote(self) -> None:
        text = "before\n> inside quote\nafter"
        result = convert_markdown(text)
        assert "before" in result
        assert ">inside quote||" in result
        assert "after" in result

    def test_code_block_with_gt_inside_is_not_blockquote(self) -> None:
        """A `>` line inside a fenced code block must not be treated as
        a Markdown blockquote — otherwise the ccmux backend's Bash
        output (which may contain shell prompts) would be mangled."""
        text = "```\n> not a quote\n```"
        result = convert_markdown(text)
        # No expandable quote syntax should appear; the line stays
        # inside the code block.
        assert "||" not in result

    def test_empty_blockquote_line(self) -> None:
        """A bare `>` marks an empty line within a blockquote."""
        text = "> line one\n>\n> line three"
        result = convert_markdown(text)
        assert ">line one" in result
        assert ">line three" in result
        assert result.rstrip().endswith("||")
