"""User-to-Claude message handlers — outbound relay.

Handles all message types sent by the user in Telegram and forwards them
to Claude Code via tmux:
  - text_handler: Plain text messages (delegates picker state to binding_flow)
  - photo_handler: Photos downloaded and forwarded as file paths
  - voice_handler: Voice messages transcribed via OpenAI API then forwarded
  - forward_command_handler: Slash commands forwarded to Claude Code
  - unsupported_content_handler: Rejects unsupported media types

The OpenAI voice-transcription helpers live in `voice_transcribe.py` and
the background bash capture lives in `bash_capture.py`.
"""

import asyncio
import logging
import time

from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from .config import config
from .util import authorized, ccmux_dir, get_thread_id, get_tm_and_window
from .runtime import get_topic, topics as _topics
from ccmux.api import extract_interactive_content
from .bash_capture import cancel_bash_capture, start_bash_capture
from .binding_flow import handle_text_in_picker_state, handle_unbound_topic
from .message_dispatch import dispatch_text
from .prompt import handle_interactive_ui
from .relay_tag import tag_relayed
from .sender import safe_reply
from .prompt_state import get_interactive_window
from .message_queue import clear_status_msg_info, enqueue_status_update
from .voice_transcribe import transcribe_voice

logger = logging.getLogger(__name__)

# --- Image directory for incoming photos ---
_IMAGES_DIR = ccmux_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)


# --- Shared target-resolution helper ---


async def _resolve_target(
    update: Update,
    require_topic: bool = True,
) -> tuple[object, str, int, int, Message] | None:
    """Validate + look up topic/window for an outbound handler.

    Returns `(topic, wid, chat_id, thread_id, message)` on success, or
    `None` after replying with the appropriate error. The topic object is
    the same `Topic` struct returned by `get_topic`; typed as `object`
    here to avoid a circular import.

    Caller must be wrapped in `@authorized(...)` — this helper assumes the
    user is already verified.
    """
    user = update.effective_user
    assert user
    if not update.message:
        return None
    message = update.message

    thread_id = get_thread_id(update)
    if require_topic and thread_id is None:
        await safe_reply(
            message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return None

    topic = get_topic(user.id, thread_id)
    if topic is None:
        await safe_reply(message, "❌ No session bound to this topic.")
        return None

    if not _topics.is_alive(topic):
        await safe_reply(
            message,
            f"⚠️ Binding to `{topic.session_name}` is not alive right now. "
            "tmux or Claude may be down. Use /rebind_topic to switch.",
        )
        return None

    wid = topic.window_id
    if not wid:
        await safe_reply(
            message,
            f"⚠️ Session '{topic.session_name}' has no window yet. "
            "Try again in a moment.",
        )
        return None

    pair = await get_tm_and_window(wid)
    if not pair:
        logger.info(
            "Window %s not found (user=%d, thread=%s); binding preserved",
            wid,
            user.id,
            thread_id,
        )
        await safe_reply(
            message,
            f"❌ Session '{topic.session_name}' window no longer exists. "
            "Use /rebind_topic to connect to a different session.",
        )
        return None

    return (
        topic,
        wid,
        topic.group_chat_id,
        thread_id if thread_id is not None else 0,
        message,
    )


@authorized(notify=True)
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages from the user."""
    user = update.effective_user
    assert user

    if not update.message or not update.message.text:
        return

    thread_id = get_thread_id(update)
    text = update.message.text

    # If a picker UI flow is active, delegate to binding module
    if await handle_text_in_picker_state(update.message, context, thread_id):
        return

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    topic = get_topic(user.id, thread_id)
    if topic is None:
        await handle_unbound_topic(update, context, user, thread_id, text)
        return

    if not _topics.is_alive(topic):
        await safe_reply(
            update.message,
            f"⚠️ Binding to `{topic.session_name}` is not alive right now. "
            "tmux or Claude may be down. Use /rebind_topic to switch.",
        )
        return

    wid = topic.window_id
    if not wid:
        # Defensive: alive but no window_id shouldn't happen normally
        await safe_reply(
            update.message,
            f"⚠️ Session '{topic.session_name}' has no window yet. "
            "Try again in a moment.",
        )
        return

    # Bound topic — forward to bound window
    pair = await get_tm_and_window(wid)
    if not pair:
        logger.info(
            "Window %s not found (user=%d, thread=%d); binding preserved",
            wid,
            user.id,
            thread_id,
        )
        await safe_reply(
            update.message,
            f"❌ Session '{topic.session_name}' window no longer exists. "
            "Use /rebind_topic to connect to a different session.",
        )
        return
    tm, w = pair

    # Fire-and-forget: TYPING is decorative; awaiting it would queue
    # behind the per-chat rate limiter and add ~1s of perceived
    # latency to every inbound message.
    asyncio.create_task(update.message.chat.send_action(ChatAction.TYPING))
    await enqueue_status_update(
        context.bot,
        user.id,
        wid,
        None,
        thread_id=thread_id,
        chat_id=topic.group_chat_id,
    )

    # Cancel any running bash capture — new message pushes pane content down
    cancel_bash_capture(user.id, thread_id)

    # Check for pending interactive UI before sending text.
    # This catches UIs (permission prompts, etc.) that status polling might have missed.
    pane_text = await tm.capture_pane(w.window_id)
    if pane_text and extract_interactive_content(pane_text) is not None:
        # UI detected — show it to user, then send text (acts as Enter)
        logger.info(
            "Detected pending interactive UI before sending text (user=%d, thread=%s)",
            user.id,
            thread_id,
        )
        await handle_interactive_ui(
            context.bot, user.id, wid, thread_id, chat_id=topic.group_chat_id
        )
        # Small delay to let UI render in Telegram before text arrives
        await asyncio.sleep(0.3)

    success, message = await dispatch_text(
        bot=context.bot,
        chat_id=topic.group_chat_id,
        message_id=update.message.message_id,
        window_id=wid,
        text=tag_relayed(text),
    )
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    # Start background capture for ! bash command output
    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]  # strip leading "!"
        start_bash_capture(
            context.bot, user.id, thread_id, wid, bash_cmd, topic.group_chat_id
        )

    # If in interactive mode, refresh the UI after sending text
    interactive_window = get_interactive_window(user.id, thread_id)
    if interactive_window and interactive_window == wid:
        await asyncio.sleep(0.2)
        await handle_interactive_ui(
            context.bot, user.id, wid, thread_id, chat_id=topic.group_chat_id
        )


@authorized(notify=True)
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to Claude Code."""
    user = update.effective_user
    assert user

    if not update.message or not update.message.photo:
        return

    thread_id = get_thread_id(update)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    topic = get_topic(user.id, thread_id)
    if topic is None:
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return

    if not _topics.is_alive(topic):
        await safe_reply(
            update.message,
            f"⚠️ Binding to `{topic.session_name}` is not alive right now. "
            "tmux or Claude may be down. Use /rebind_topic to switch.",
        )
        return

    wid = topic.window_id
    if not wid:
        await safe_reply(
            update.message,
            f"⚠️ Session '{topic.session_name}' has no window yet.",
        )
        return

    pair = await get_tm_and_window(wid)
    if not pair:
        logger.info(
            "Window %s not found for photo (user=%d, thread=%d); binding preserved",
            wid,
            user.id,
            thread_id,
        )
        await safe_reply(
            update.message,
            f"❌ Session '{topic.session_name}' window no longer exists. "
            "Use /rebind_topic to connect to a different session.",
        )
        return

    # Download the highest-resolution photo
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    # Save to ~/.ccmux/images/<timestamp>_<file_unique_id>.jpg
    filename = f"{int(time.time())}_{photo.file_unique_id}.jpg"
    file_path = _IMAGES_DIR / filename
    await tg_file.download_to_drive(file_path)

    # Build the message to send to Claude Code
    caption = update.message.caption or ""
    if caption:
        text_to_send = f"{caption}\n\n(image attached: {file_path})"
    else:
        text_to_send = f"(image attached: {file_path})"

    # Fire-and-forget: TYPING is decorative; awaiting it would queue
    # behind the per-chat rate limiter and add ~1s of perceived
    # latency to every inbound message.
    asyncio.create_task(update.message.chat.send_action(ChatAction.TYPING))
    clear_status_msg_info(user.id, thread_id)

    success, message = await dispatch_text(
        bot=context.bot,
        chat_id=topic.group_chat_id,
        message_id=update.message.message_id,
        window_id=wid,
        text=tag_relayed(text_to_send),
    )
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    # Confirm to user
    await safe_reply(update.message, "📷 Image sent to Claude Code.")


@authorized(notify=True)
async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe via OpenAI and forward text to Claude Code."""
    user = update.effective_user
    assert user

    if not update.message or not update.message.voice:
        return

    if not config.openai_api_key:
        await safe_reply(
            update.message,
            "⚠ Voice transcription requires an OpenAI API key.\n"
            "Set `OPENAI_API_KEY` in your `.env` file and restart the bot.",
        )
        return

    thread_id = get_thread_id(update)

    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    topic = get_topic(user.id, thread_id)
    if topic is None:
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return

    if not _topics.is_alive(topic):
        await safe_reply(
            update.message,
            f"⚠️ Binding to `{topic.session_name}` is not alive right now. "
            "tmux or Claude may be down. Use /rebind_topic to switch.",
        )
        return

    wid = topic.window_id
    if not wid:
        await safe_reply(
            update.message,
            f"⚠️ Session '{topic.session_name}' has no window yet.",
        )
        return

    pair = await get_tm_and_window(wid)
    if not pair:
        logger.info(
            "Window %s not found for voice (user=%d, thread=%d); binding preserved",
            wid,
            user.id,
            thread_id,
        )
        await safe_reply(
            update.message,
            f"❌ Session '{topic.session_name}' window no longer exists. "
            "Use /rebind_topic to connect to a different session.",
        )
        return

    # Download voice as in-memory bytes
    voice_file = await update.message.voice.get_file()
    ogg_data = bytes(await voice_file.download_as_bytearray())

    # Transcribe
    try:
        text = await transcribe_voice(ogg_data)
    except ValueError as e:
        await safe_reply(update.message, f"⚠ {e}")
        return
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await safe_reply(update.message, f"⚠ Transcription failed: {e}")
        return

    # Fire-and-forget: TYPING is decorative; awaiting it would queue
    # behind the per-chat rate limiter and add ~1s of perceived
    # latency to every inbound message.
    asyncio.create_task(update.message.chat.send_action(ChatAction.TYPING))
    clear_status_msg_info(user.id, thread_id)

    success, message = await dispatch_text(
        bot=context.bot,
        chat_id=topic.group_chat_id,
        message_id=update.message.message_id,
        window_id=wid,
        text=tag_relayed(text),
    )
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    await safe_reply(update.message, f'🎤 "{text}"')


@authorized()
async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any non-bot command as a slash command to the active Claude Code session."""
    user = update.effective_user
    assert user
    if not update.message:
        return

    thread_id = get_thread_id(update)

    cmd_text = update.message.text or ""
    # The full text is already a slash command like "/clear" or "/compact foo"
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    topic = get_topic(user.id, thread_id)
    if topic is None:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    if not _topics.is_alive(topic):
        await safe_reply(
            update.message,
            f"⚠️ Binding to `{topic.session_name}` is not alive right now. "
            "tmux or Claude may be down. Use /rebind_topic to switch.",
        )
        return

    wid = topic.window_id
    if not wid:
        await safe_reply(
            update.message, f"⚠️ Session '{topic.session_name}' has no window yet."
        )
        return

    pair = await get_tm_and_window(wid)
    if not pair:
        await safe_reply(
            update.message,
            f"❌ Session '{topic.session_name}' window no longer exists.",
        )
        return

    display = topic.session_name
    logger.info(
        "Forwarding command %s to session %s (user=%d)", cc_slash, display, user.id
    )
    # Fire-and-forget: TYPING is decorative; awaiting it would queue
    # behind the per-chat rate limiter and add ~1s of perceived
    # latency to every inbound message.
    asyncio.create_task(update.message.chat.send_action(ChatAction.TYPING))
    success, message = await dispatch_text(
        bot=context.bot,
        chat_id=topic.group_chat_id,
        message_id=update.message.message_id,
        window_id=wid,
        text=cc_slash,
    )
    if success:
        await safe_reply(update.message, f"⚡ [{display}] Sent: {cc_slash}")
        # Interactive commands (e.g. /model) render a terminal-based UI
        # with no JSONL tool_use entry. The status poller already detects
        # interactive UIs every 1s (status.py), so no proactive detection
        # needed here — the poller handles it.
    else:
        await safe_reply(update.message, f"❌ {message}")


@authorized()
async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to non-text messages (stickers, video, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    assert user
    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        update.message,
        "⚠ Only text, photo, and voice messages are supported. Stickers, video, and other media cannot be forwarded to Claude Code.",
    )
