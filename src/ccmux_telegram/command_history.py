"""Message history display with pagination.

Provides history viewing functionality for Claude Code sessions:
  - _build_history_keyboard: Build inline keyboard for page navigation
  - send_history: Send or edit message history with pagination support
  - history_command: Telegram /history command handler
  - handle_history_callback: History pagination callback handler
"""

import logging
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .runtime import get_topic, windows as _windows
from .topic_bindings import TopicBinding
from .util import authorized, get_thread_id
from .callback_data import CB_HISTORY_NEXT, CB_HISTORY_PREV
from .sender import safe_edit, safe_reply, safe_send, split_message

logger = logging.getLogger(__name__)


def _build_history_keyboard(
    page_index: int,
    total_pages: int,
    start_byte: int = 0,
    end_byte: int = 0,
) -> InlineKeyboardMarkup | None:
    """Build inline keyboard for history pagination.

    Callback format: `hp:<page>:<start>:<end>` or `hn:<page>:<start>:<end>`.
    Binding is resolved from the callback query's (user_id, thread_id).
    """
    if total_pages <= 1:
        return None

    buttons = []
    if page_index > 0:
        cb_data = f"{CB_HISTORY_PREV}{page_index - 1}:{start_byte}:{end_byte}"
        buttons.append(InlineKeyboardButton("◀ Older", callback_data=cb_data[:64]))

    buttons.append(
        InlineKeyboardButton(f"{page_index + 1}/{total_pages}", callback_data="noop")
    )

    if page_index < total_pages - 1:
        cb_data = f"{CB_HISTORY_NEXT}{page_index + 1}:{start_byte}:{end_byte}"
        buttons.append(InlineKeyboardButton("Newer ▶", callback_data=cb_data[:64]))

    return InlineKeyboardMarkup([buttons])


async def send_history(
    target: Any,
    topic: TopicBinding,
    offset: int = -1,
    edit: bool = False,
    *,
    start_byte: int = 0,
    end_byte: int = 0,
    bot: Bot | None = None,
    message_thread_id: int | None = None,
) -> None:
    """Send or edit message history for a bound topic's Claude session."""
    display_name = topic.session_name
    is_unread = start_byte > 0 or end_byte > 0

    from ccmux.api import get_default_backend

    instance = _windows.get(topic.session_name)
    if instance is None or not instance.session_id:
        messages: list[dict] = []
    else:
        messages = await get_default_backend().claude.get_history(
            instance.session_id,
            start_byte=start_byte,
            end_byte=end_byte if end_byte > 0 else None,
        )
    logger.info(
        "send_history %s: %d messages (unread=%s)",
        display_name,
        len(messages),
        is_unread,
    )

    if not messages:
        if is_unread:
            text = f"📬 [{display_name}] No unread messages."
        else:
            text = f"📋 [{display_name}] No messages yet."
        keyboard = None
    else:
        from ccmux.config import config as backend_config

        if not backend_config.show_user_messages:
            messages = [m for m in messages if m["role"] == "assistant"]
        total = len(messages)
        if total == 0:
            if is_unread:
                text = f"📬 [{display_name}] No unread messages."
            else:
                text = f"📋 [{display_name}] No messages yet."
            keyboard = None
            if edit:
                await safe_edit(target, text, reply_markup=keyboard)
            elif bot is not None:
                await safe_send(
                    bot,
                    topic.group_chat_id,
                    text,
                    message_thread_id=message_thread_id,
                    reply_markup=keyboard,
                )
            else:
                await safe_reply(target, text, reply_markup=keyboard)
            return

        if is_unread:
            header = f"📬 [{display_name}] {total} unread messages"
        else:
            header = f"📋 [{display_name}] Messages ({total} total)"

        lines = [header]
        for msg in messages:
            ts = msg.get("timestamp")
            if ts:
                try:
                    time_part = ts.split("T")[1] if "T" in ts else ts
                    hh_mm = time_part[:5]
                except (IndexError, TypeError):
                    hh_mm = ""
            else:
                hh_mm = ""

            if hh_mm:
                lines.append(f"───── {hh_mm} ─────")
            else:
                lines.append("─────────────")

            msg_text = msg["text"]
            content_type = msg.get("content_type", "text")
            msg_role = msg.get("role", "assistant")

            # History renders in a plain-text list view. The backend's
            # `> ` blockquote markers are left intact so the quoted lines
            # remain readable and the history page is easy to scan.

            if msg_role == "user":
                lines.append(f"👤 {msg_text}")
            elif content_type == "thinking":
                lines.append(f"∴ Thinking…\n{msg_text}")
            else:
                lines.append(msg_text)
        full_text = "\n\n".join(lines)
        pages = split_message(full_text, max_length=4096)

        if offset < 0:
            offset = len(pages) - 1
        page_index = max(0, min(offset, len(pages) - 1))
        text = pages[page_index]
        keyboard = _build_history_keyboard(page_index, len(pages), start_byte, end_byte)

    if edit:
        await safe_edit(target, text, reply_markup=keyboard)
    elif bot is not None:
        await safe_send(
            bot,
            topic.group_chat_id,
            text,
            message_thread_id=message_thread_id,
            reply_markup=keyboard,
        )
    else:
        await safe_reply(target, text, reply_markup=keyboard)


@authorized()
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the bound topic."""
    user = update.effective_user
    assert user
    if not update.message:
        return

    thread_id = get_thread_id(update)
    topic = get_topic(user.id, thread_id)
    logger.info("/history user=%d thread=%s", user.id, thread_id)
    if not topic:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    await send_history(update.message, topic)


async def handle_history_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle history pagination callbacks (prev/next)."""
    query = update.callback_query
    assert query and query.data
    data = query.data

    # Format: hp:<page>:<start>:<end> or hn:<page>:<start>:<end>
    prefix_len = len(CB_HISTORY_PREV)  # same length for both
    rest = data[prefix_len:]
    try:
        parts = rest.split(":")
        if len(parts) < 3:
            await query.answer("Invalid data")
            return
        offset = int(parts[0])
        start_byte = int(parts[1])
        end_byte = int(parts[2])
    except (ValueError, IndexError):
        await query.answer("Invalid data")
        return

    user = query.from_user
    thread_id = get_thread_id(update)
    topic = get_topic(user.id, thread_id) if user else None
    if not topic:
        await query.answer("No binding for this topic", show_alert=True)
        return

    await send_history(
        query,
        topic,
        offset=offset,
        edit=True,
        start_byte=start_byte,
        end_byte=end_byte,
    )
    await query.answer("Page updated")
