"""Per-topic sweep log: track bot-owned commands + replies for /sweep cleanup.

`/sweep` deletes every message registered here for a given (user, thread)
pair. Tracking is in-memory only — it starts fresh on bot restart, which is
fine because Telegram bots can only delete messages up to 48 h old anyway.

Integration:
  - `sweep_tracked` decorator wraps "own" command handlers. It logs the
    incoming `/xxx` message and activates a context variable so any
    `safe_reply` inside the handler auto-logs the outgoing message.
  - `sender.safe_reply` checks the context variable and, if set, calls
    `track_msg` with the sent message id.
  - `/sweep` calls `sweep_messages` to delete and clear the log.
"""

from __future__ import annotations

import contextvars
import logging
from functools import wraps
from typing import Any, Awaitable, Callable

from telegram import Bot, Update
from telegram.ext import ContextTypes

from .util import get_thread_id

logger = logging.getLogger(__name__)

_SWEEP_LOG: dict[tuple[int, int], list[int]] = {}

# Active (user_id, thread_id) for auto-tracking replies sent inside a
# sweep-tracked command handler. `None` outside such handlers.
_ACTIVE: contextvars.ContextVar[tuple[int, int] | None] = contextvars.ContextVar(
    "ccmux_sweep_active", default=None
)


def track_msg(user_id: int, thread_id: int, *msg_ids: int) -> None:
    """Register one or more message ids to be deleted on /sweep."""
    if not msg_ids:
        return
    _SWEEP_LOG.setdefault((user_id, thread_id), []).extend(msg_ids)


def track_active(msg_id: int) -> None:
    """Track a message against whichever (user, thread) is currently active."""
    key = _ACTIVE.get()
    if key is not None:
        track_msg(key[0], key[1], msg_id)


async def sweep_messages(bot: Bot, user_id: int, thread_id: int, chat_id: int) -> int:
    """Delete all tracked messages for this topic. Returns count deleted."""
    ids = _SWEEP_LOG.pop((user_id, thread_id), [])
    deleted = 0
    for msg_id in ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted += 1
        except Exception as e:
            logger.debug("sweep: failed to delete %d: %s", msg_id, e)
    return deleted


HandlerFn = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]


def sweep_tracked(fn: HandlerFn) -> HandlerFn:
    """Decorator: log the command message id and enable auto-tracking for replies."""

    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        user = update.effective_user
        msg = update.message
        if user is None or msg is None:
            return await fn(update, context)
        thread_id = get_thread_id(update) or 0
        track_msg(user.id, thread_id, msg.message_id)
        token = _ACTIVE.set((user.id, thread_id))
        try:
            return await fn(update, context)
        finally:
            _ACTIVE.reset(token)

    return wrapper
