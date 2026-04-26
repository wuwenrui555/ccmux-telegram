"""Tests for response_builder.build_response_parts."""

from ccmux_telegram.message_in import build_response_parts


class TestBuildResponseParts:
    def test_user_message_has_emoji_prefix(self):
        parts = build_response_parts("hello", is_complete=True, role="user")
        assert len(parts) == 1
        assert "\U0001f464" in parts[0]

    def test_user_message_strips_relay_tag(self):
        """[from ccmux] prefix lives in JSONL for debug; Telegram echo
        should be clean."""
        parts = build_response_parts(
            "[from ccmux] hello", is_complete=True, role="user"
        )
        assert len(parts) == 1
        assert "[from ccmux]" not in parts[0]
        assert parts[0].endswith("hello")

    def test_user_message_truncated_at_3000_chars(self):
        long_text = "a" * 4000
        parts = build_response_parts(long_text, is_complete=True, role="user")
        assert len(parts) == 1
        short_parts = build_response_parts("b" * 100, is_complete=True, role="user")
        assert len(parts[0]) < len(long_text)
        assert len(short_parts[0]) < len(parts[0])

    def test_thinking_content_truncated_at_500_chars(self):
        inner_lines = "\n".join(f"> {'x' * 100}" for _ in range(10))  # ~1000 chars
        parts = build_response_parts(
            inner_lines, is_complete=True, content_type="thinking"
        )
        assert len(parts) == 1
        assert "truncated" in parts[0].lower()

    def test_plain_text_single_part(self):
        parts = build_response_parts("short text", is_complete=True)
        assert len(parts) == 1

    def test_plain_text_multi_part_has_page_suffix(self):
        long_text = "\n".join(f"line {i} " + "padding" * 50 for i in range(200))
        parts = build_response_parts(long_text, is_complete=True)
        assert len(parts) > 1
        assert "1/" in parts[0]

    def test_blockquote_stays_atomic(self):
        """A standalone `>` blockquote region is kept as a single part
        so the markdown layer can render it as one expandable quote."""
        text = "\n".join(f"> thought line {i}" for i in range(50))
        parts = build_response_parts(text, is_complete=False, content_type="thinking")
        assert len(parts) == 1

    def test_thinking_has_prefix(self):
        parts = build_response_parts(
            "some thought", is_complete=True, content_type="thinking"
        )
        assert len(parts) == 1
        assert "Thinking" in parts[0]

    def test_assistant_text_no_prefix(self):
        parts = build_response_parts(
            "hello world", is_complete=True, content_type="text", role="assistant"
        )
        assert len(parts) == 1
        assert "\U0001f464" not in parts[0]
        assert "Thinking" not in parts[0]
