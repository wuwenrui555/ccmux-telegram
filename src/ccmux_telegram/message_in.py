"""Claude-to-user message delivery — inbound relay.

Processes ClaudeMessage objects from the MessageMonitor and delivers them
to Telegram users via the message queue system.

Core responsibilities:
  - handle_new_message: Route JSONL entries to the correct Telegram topic,
    handling interactive tools, tool_use/tool_result pairing, and
    content message enqueuing.
  - build_response_parts: Build paginated display blocks from Claude text.
"""

import logging

from telegram import Bot

import re

from .config import config
from .runtime import get_topic_for_claude_session
from ccmux.api import ClaudeMessage
from .prompt import clear_interactive_msg, handle_interactive_ui
from .prompt_state import (
    PROMPT_TOOL_NAMES,
    clear_interactive_mode,
    get_interactive_msg_id,
    set_interactive_mode,
)
from .markdown import convert_markdown_tables
from .sender import split_message
from .message_queue import enqueue_content_message, get_message_queue

logger = logging.getLogger(__name__)

# Matches a single Markdown blockquote line: "> content" or bare ">".
_BLOCKQUOTE_LINE_RE = re.compile(r"^>(?: (.*))?$")


def _is_blockquote_only(text: str) -> bool:
    """True when every non-empty line of `text` is a Markdown blockquote.

    Used to detect parser output that should stay atomic (a single
    collapsible region in the rendered message).
    """
    stripped = text.strip()
    if not stripped:
        return False
    return all(_BLOCKQUOTE_LINE_RE.match(line) for line in stripped.split("\n") if line)


def _strip_blockquote(text: str) -> str:
    """Return the inner content of a blockquote-only text region."""
    lines: list[str] = []
    for line in text.split("\n"):
        m = _BLOCKQUOTE_LINE_RE.match(line)
        lines.append(m.group(1) or "" if m else line)
    return "\n".join(lines)


def _as_blockquote(text: str) -> str:
    """Re-prefix each line with `> ` so the region renders as a blockquote."""
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


def build_response_parts(
    text: str,
    is_complete: bool,
    content_type: str = "text",
    role: str = "assistant",
) -> list[str]:
    """Build paginated response messages for Telegram.

    Returns a list of raw markdown strings, each within Telegram's 4096 char limit.
    Multi-part messages get a [1/N] suffix.
    Markdown-to-MarkdownV2 conversion is done by the send layer, not here.
    """
    text = text.strip()

    # User messages: add emoji prefix (no newline)
    if role == "user":
        prefix = "👤 "
        separator = ""
        # User messages are typically short, no special processing needed
        if len(text) > 3000:
            text = text[:3000] + "…"
        return [f"{prefix}{text}"]

    # Truncate thinking content to keep it compact. The backend wraps
    # thinking in a standard Markdown blockquote (`> ` lines); strip the
    # prefix to measure the inner length, then re-add it after clipping.
    if content_type == "thinking" and is_complete:
        max_thinking = 500
        if _is_blockquote_only(text):
            inner = _strip_blockquote(text)
            if len(inner) > max_thinking:
                inner = inner[:max_thinking] + "\n\n… (thinking truncated)"
                text = _as_blockquote(inner)
        elif len(text) > max_thinking:
            text = text[:max_thinking] + "\n\n… (thinking truncated)"

    # Format based on content type
    if content_type == "thinking":
        # Thinking: prefix with "∴ Thinking…" and single newline
        prefix = "∴ Thinking…"
        separator = "\n"
    else:
        # Plain text: no prefix
        prefix = ""
        separator = ""

    # If the body is a standalone blockquote, keep it atomic — the
    # markdown layer renders it as a single Telegram expandable quote
    # and handles its own truncation.
    if _is_blockquote_only(text):
        if prefix:
            return [f"{prefix}{separator}{text}"]
        return [text]

    # Convert tables to card-style before splitting so tables aren't broken
    # across messages. The send layer's convert_markdown() call is idempotent.
    text = convert_markdown_tables(text)

    # Split first, then assemble each chunk.
    # Use conservative max to leave room for MarkdownV2 expansion at send layer.
    max_text = 3000 - len(prefix) - len(separator)

    text_chunks = split_message(text, max_length=max_text)
    total = len(text_chunks)

    if total == 1:
        if prefix:
            return [f"{prefix}{separator}{text_chunks[0]}"]
        return [text_chunks[0]]

    parts = []
    for i, chunk in enumerate(text_chunks, 1):
        if prefix:
            parts.append(f"{prefix}{separator}{chunk}\n\n[{i}/{total}]")
        else:
            parts.append(f"{chunk}\n\n[{i}/{total}]")
    return parts


async def handle_new_message(msg: ClaudeMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via thread_bindings to deliver to the correct topic.
    """
    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    # Find the bound topic for this Claude session
    topic = get_topic_for_claude_session(msg.session_id)

    if not topic:
        logger.info(f"No active binding for session {msg.session_id}")
        return

    user_id = topic.user_id
    wid = topic.window_id
    thread_id = topic.thread_id

    # Handle interactive tools specially - capture terminal and send UI
    if msg.tool_name in PROMPT_TOOL_NAMES and msg.content_type == "tool_use":
        set_interactive_mode(user_id, wid, thread_id)
        queue = get_message_queue(user_id, thread_id or 0)
        if queue:
            await queue.join()
        import asyncio

        await asyncio.sleep(0.3)
        handled = await handle_interactive_ui(
            bot, user_id, wid, thread_id, chat_id=topic.group_chat_id
        )
        if handled:
            return
        clear_interactive_mode(user_id, thread_id)

    # Any non-interactive message means the interaction is complete
    if get_interactive_msg_id(user_id, thread_id):
        await clear_interactive_msg(
            user_id, bot, thread_id, chat_id=topic.group_chat_id
        )

    # Skip tool call notifications when CCMUX_SHOW_TOOL_CALLS=false
    if not config.show_tool_calls and msg.content_type in (
        "tool_use",
        "tool_result",
    ):
        return

    # Skip thinking blocks when CCMUX_SHOW_THINKING=false. Currently
    # these blocks have empty content (CC only records a signature),
    # so suppressing them just removes a placeholder.
    if not config.show_thinking and msg.content_type == "thinking":
        return

    # Skip Skill tool_result bodies when CCMUX_SHOW_SKILL_BODIES=false.
    # The Skill tool_use summary is preserved; only the full skill body
    # (tool_result) is suppressed to avoid flooding the chat.
    if (
        not config.show_skill_bodies
        and msg.content_type == "tool_result"
        and msg.tool_name == "Skill"
    ):
        return

    parts = build_response_parts(
        msg.text,
        msg.is_complete,
        msg.content_type,
        msg.role,
    )

    if msg.is_complete:
        await enqueue_content_message(
            bot=bot,
            user_id=user_id,
            window_id=wid,
            parts=parts,
            tool_use_id=msg.tool_use_id,
            content_type=msg.content_type,
            text=msg.text,
            thread_id=thread_id,
            image_data=msg.image_data,
            chat_id=topic.group_chat_id,
        )
