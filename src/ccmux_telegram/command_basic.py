"""Basic bot command handlers — /start, /text, /esc, /usage, /unbind, /rebind.

Provides Telegram command handlers for bot-specific actions (not forwarded
to Claude Code):
  - start_command: Welcome message
  - text_command: Capture tmux pane as plain text
  - esc_command: Send Escape key to interrupt Claude
  - usage_command: Fetch and display Claude Code usage stats
  - unbind_command: Unbind topic from session without killing window
  - rebind_command: Unbind and re-trigger session picker flow

The /history command lives in `command_history.py`.
"""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from .runtime import get_topic, topics as _topics
from .util import authorized, get_thread_id, get_tm_and_window
from .binding_lifecycle import clear_topic_state
from .picker import clear_browse_state
from .sender import safe_reply

logger = logging.getLogger(__name__)


@authorized(notify=True)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show welcome message."""
    clear_browse_state(context.user_data)

    if update.message:
        await safe_reply(
            update.message,
            "🤖 *Claude Code Monitor*\n\n"
            "Each topic is a session. Create a new topic to start.",
        )


@authorized()
async def text_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture the current tmux pane and send it as plain text."""
    user = update.effective_user
    assert user
    if not update.message:
        return

    thread_id = get_thread_id(update)
    topic = get_topic(user.id, thread_id)
    if not topic:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return
    if not _topics.is_alive(topic):
        await safe_reply(
            update.message,
            f"⚠️ Binding to `{topic.session_name}` is not alive right now. "
            "tmux or Claude may be down. Use /rebind to reconnect to a different session.",
        )
        return
    if not topic.window_id:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return
    wid = topic.window_id

    pair = await get_tm_and_window(wid)
    if not pair:
        await safe_reply(
            update.message,
            f"❌ Session '{topic.session_name}' window no longer exists.",
        )
        return
    tm, w = pair

    text = await tm.capture_pane(w.window_id)
    if not text:
        await safe_reply(update.message, "❌ Failed to capture pane content.")
        return

    # Wrap in code block for monospace display
    if len(text) > 4000:
        text = text[-4000:]
        # Cut at first newline to avoid a partial line
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1 :]
    # Break up triple backticks to avoid breaking MarkdownV2 code blocks
    text = text.replace("```", "` ` `")
    await safe_reply(update.message, f"```\n{text}\n```")


@authorized()
async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unbind this topic from its Claude session without killing the window."""
    user = update.effective_user
    assert user
    if not update.message:
        return

    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    topic = get_topic(user.id, thread_id)
    if not topic:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    session_name = topic.session_name
    _topics.unbind(user.id, thread_id)
    await clear_topic_state(user.id, thread_id, context.bot, context.user_data)

    await safe_reply(
        update.message,
        f"✅ Topic unbound from session '{session_name}'.\n"
        "The Claude session is still running in tmux.\n"
        "Send a message to bind to a new session.",
    )


@authorized()
async def rebind_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /rebind — show picker to choose a different session.

    Keeps the current binding until the user confirms a new selection.
    Cancelling the picker leaves the existing binding untouched.
    """
    user = update.effective_user
    assert user
    thread_id = get_thread_id(update)
    if thread_id is None or update.message is None:
        if update.message:
            await safe_reply(update.message, "❌ Use /rebind inside a named topic.")
        return

    from .binding_flow import handle_unbound_topic

    await handle_unbound_topic(update, _context, user, thread_id, text="")


@authorized()
async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape key to interrupt Claude."""
    user = update.effective_user
    assert user
    if not update.message:
        return

    thread_id = get_thread_id(update)
    topic = get_topic(user.id, thread_id)
    if not topic:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return
    if not _topics.is_alive(topic):
        await safe_reply(
            update.message,
            f"⚠️ Binding to `{topic.session_name}` is not alive right now. "
            "tmux or Claude may be down. Use /rebind to reconnect to a different session.",
        )
        return
    if not topic.window_id:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return
    wid = topic.window_id

    pair = await get_tm_and_window(wid)
    if not pair:
        await safe_reply(
            update.message,
            f"❌ Session '{topic.session_name}' window no longer exists.",
        )
        return
    tm, w = pair

    # Send Escape control character (no enter)
    await tm.send_keys(w.window_id, "\x1b", enter=False)
    await safe_reply(update.message, "⎋ Sent Escape")


@authorized()
async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch Claude Code usage stats from TUI and send to Telegram."""
    user = update.effective_user
    assert user
    if not update.message:
        return

    thread_id = get_thread_id(update)
    topic = get_topic(user.id, thread_id)
    if not topic:
        await safe_reply(update.message, "No session bound to this topic.")
        return
    if not _topics.is_alive(topic):
        await safe_reply(
            update.message,
            f"⚠️ Binding to `{topic.session_name}` is not alive right now. "
            "tmux or Claude may be down. Use /rebind to reconnect to a different session.",
        )
        return
    if not topic.window_id:
        await safe_reply(update.message, "No session bound to this topic.")
        return
    wid = topic.window_id

    pair = await get_tm_and_window(wid)
    if not pair:
        await safe_reply(
            update.message,
            f"Session '{topic.session_name}' window no longer exists.",
        )
        return
    tm, w = pair

    # Send /usage command to Claude Code TUI
    await tm.send_keys(w.window_id, "/usage")
    # Wait for the modal to render
    await asyncio.sleep(2.0)
    # Capture the pane content
    pane_text = await tm.capture_pane(w.window_id)
    # Dismiss the modal
    await tm.send_keys(w.window_id, "Escape", enter=False, literal=False)

    if not pane_text:
        await safe_reply(update.message, "Failed to capture usage info.")
        return

    # Try to parse structured usage info
    from ccmux.api import parse_usage_output

    usage = parse_usage_output(pane_text)
    if usage and usage.parsed_lines:
        text = "\n".join(usage.parsed_lines)
        await safe_reply(update.message, f"```\n{text}\n```")
    else:
        # Fallback: send raw pane capture trimmed
        trimmed = pane_text.strip()
        if len(trimmed) > 3000:
            trimmed = trimmed[:3000] + "\n... (truncated)"
        await safe_reply(update.message, f"```\n{trimmed}\n```")
