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

from .config import config
from .runtime import get_topic_for_claude_session
from ccmux.api import ClaudeMessage, TranscriptParser
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

    # Truncate thinking content to keep it compact
    if content_type == "thinking" and is_complete:
        start_tag = TranscriptParser.EXPANDABLE_QUOTE_START
        end_tag = TranscriptParser.EXPANDABLE_QUOTE_END
        max_thinking = 500
        if start_tag in text and end_tag in text:
            inner = text[text.index(start_tag) + len(start_tag) : text.index(end_tag)]
            if len(inner) > max_thinking:
                inner = inner[:max_thinking] + "\n\n… (thinking truncated)"
            text = start_tag + inner + end_tag
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

    # If text contains expandable quote sentinels, don't split —
    # the quote must stay atomic. Truncation is handled by
    # _render_expandable_quote in markdown_v2.py.
    if TranscriptParser.EXPANDABLE_QUOTE_START in text:
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
