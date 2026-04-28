"""Basic bot command handlers — /start, /text, /esc, /usage, /unbind, /rebind_topic, /rebind_window.

Provides Telegram command handlers for bot-specific actions (not forwarded
to Claude Code):
  - start_command: Welcome message
  - text_command: Capture tmux pane as plain text
  - esc_command: Send Escape key to interrupt Claude
  - usage_command: Fetch and display Claude Code usage stats
  - unbind_command: Unbind topic from session without killing window
  - rebind_topic_command: Unbind and re-trigger session picker flow
  - rebind_window_command: Refresh the bound session's active window via reconcile

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
from .sweep import sweep_messages, sweep_tracked

logger = logging.getLogger(__name__)

# Chrome separator: a `────` line spanning most of the terminal width that
# marks the top of Claude Code's prompt box + status bar. Short decorative
# dividers inside UI bodies are well under 20 chars, so this threshold is
# safe.
_CHROME_SEPARATOR_MIN_LEN = 20
_CHROME_SEARCH_WINDOW = 10


def _find_chrome_separators(lines: list[str]) -> list[int]:
    """Indices of all `────` chrome separator lines within the last window of lines.

    Claude Code's TUI renders two separators at the bottom of the pane: one
    above the prompt box and one between the prompt box and the status bar.
    """
    search_start = max(0, len(lines) - _CHROME_SEARCH_WINDOW)
    return [
        i
        for i in range(search_start, len(lines))
        if len(lines[i].strip()) >= _CHROME_SEPARATOR_MIN_LEN
        and all(c == "─" for c in lines[i].strip())
    ]


def _strip_pane_chrome(text: str) -> str:
    """Drop Claude Code's bottom prompt box + status bar from a pane capture."""
    lines = text.splitlines()
    seps = _find_chrome_separators(lines)
    if seps:
        lines = lines[: seps[0]]
    return "\n".join(lines).rstrip()


def _extract_pane_chrome(text: str) -> str:
    """Return only Claude Code's status bar (content below the last `────` separator)."""
    lines = text.splitlines()
    seps = _find_chrome_separators(lines)
    if not seps:
        return ""
    return "\n".join(lines[seps[-1] + 1 :]).rstrip()


@authorized(notify=True)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show welcome message, or trigger the binding picker.

    ``/start`` is the Telegram-conventional first command. In an unbound
    forum topic the same flow that plain-text messages trigger is what
    the user expects, so route through :func:`handle_unbound_topic` and
    show the tmux-session picker directly. In already-bound topics (and
    the no-thread private chat) keep the welcome text.
    """
    clear_browse_state(context.user_data)

    if not update.message:
        return

    user = update.effective_user
    thread_id = get_thread_id(update)
    topic = get_topic(user.id, thread_id) if user and thread_id is not None else None

    if user is not None and thread_id is not None and topic is None:
        # Lazy import to avoid circular (binding_flow -> command_basic).
        from .binding_flow import handle_unbound_topic

        await handle_unbound_topic(update, context, user, thread_id, text="")
        return

    if topic is not None:
        await safe_reply(
            update.message,
            f"✅ Bound to `{topic.session_name}`.\n\n"
            "/rebind_topic — switch to another session\n"
            "/history — view past messages\n"
            "/unbind — remove this binding",
        )
        return

    await safe_reply(
        update.message,
        "🤖 *Claude Code Monitor*\n\n"
        "Each topic is a session. Create a new topic to start.",
    )


@authorized()
@sweep_tracked
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
            "tmux or Claude may be down. Use /rebind_window to refresh, or /rebind_topic to switch.",
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

    text = _strip_pane_chrome(text)

    # Wrap in code block for monospace display
    if len(text) > 4000:
        text = text[-4000:]
        # Cut at first newline to avoid a partial line
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1 :]
    # Break up triple backticks to avoid breaking MarkdownV2 code blocks
    text = text.replace("```", "` ` `")
    # Explicit `text` language prevents Telegram's client-side auto-detection
    # (which otherwise highlights TUI captures as Python).
    await safe_reply(update.message, f"```text\n{text}\n```")


@authorized()
@sweep_tracked
async def bar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture just Claude Code's bottom chrome (prompt box + status bar)."""
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
            "tmux or Claude may be down. Use /rebind_window to refresh, or /rebind_topic to switch.",
        )
        return
    if not topic.window_id:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    pair = await get_tm_and_window(topic.window_id)
    if not pair:
        await safe_reply(
            update.message,
            f"❌ Session '{topic.session_name}' window no longer exists.",
        )
        return
    tm, w = pair

    raw = await tm.capture_pane(w.window_id)
    if not raw:
        await safe_reply(update.message, "❌ Failed to capture pane content.")
        return

    chrome = _extract_pane_chrome(raw)
    if not chrome:
        await safe_reply(update.message, "❌ No chrome separator found in pane.")
        return

    chrome = chrome.replace("```", "` ` `")
    await safe_reply(update.message, f"```text\n{chrome}\n```")


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
async def rebind_topic_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /rebind_topic — show picker to choose a different session.

    Keeps the current binding until the user confirms a new selection.
    Cancelling the picker leaves the existing binding untouched.
    """
    user = update.effective_user
    assert user
    thread_id = get_thread_id(update)
    if thread_id is None or update.message is None:
        if update.message:
            await safe_reply(
                update.message, "❌ Use /rebind_topic inside a named topic."
            )
        return

    from .binding_flow import handle_unbound_topic

    await handle_unbound_topic(update, _context, user, thread_id, text="")


def _get_backend():
    """Resolve the backend singleton.

    Wrapped so tests can patch `_get_backend` rather than the underlying
    `get_default_backend` from ccmux.api.
    """
    from ccmux.api import get_default_backend

    return get_default_backend()


@authorized()
async def rebind_window_command(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /rebind_window — refresh the bound session's window mapping.

    Calls backend.reconcile_instance for the topic's session_name and,
    if a live Claude window is found, installs an in-memory override.
    Reports outcome inline. Use this when the bot reports "Binding to
    X is not alive" but the underlying tmux session still has Claude
    running (just in a different window than ``claude_instances.json``
    records).
    """
    user = update.effective_user
    if user is None or update.message is None:
        return
    thread_id = get_thread_id(update)
    topic = get_topic(user.id, thread_id) if thread_id is not None else None

    if topic is None:
        await safe_reply(
            update.message,
            "❌ No session bound here. Use /rebind_topic first.",
        )
        return

    backend = _get_backend()
    inst = await backend.reconcile_instance(topic.session_name)
    if inst is None:
        await safe_reply(
            update.message,
            f"⚠️ Session `{topic.session_name}` has no live Claude. "
            "Use /rebind_topic to switch, or /start to spawn a new Claude.",
        )
        return

    backend.claude_instances.set_override(topic.session_name, inst)
    await safe_reply(
        update.message,
        f"✅ Refreshed binding: `{topic.session_name}` → `{inst.window_id}`.",
    )


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
            "tmux or Claude may be down. Use /rebind_window to refresh, or /rebind_topic to switch.",
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
@sweep_tracked
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
            "tmux or Claude may be down. Use /rebind_window to refresh, or /rebind_topic to switch.",
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


@authorized()
@sweep_tracked
async def sweep_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete this user's bot-command messages and replies in the current topic."""
    user = update.effective_user
    assert user
    if not update.message:
        return

    thread_id = get_thread_id(update) or 0
    chat_id = update.message.chat_id
    # The decorator already registered this /sweep message for deletion,
    # so it gets cleaned up along with everything else.
    await sweep_messages(context.bot, user.id, thread_id, chat_id)
