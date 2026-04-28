"""Topic name auto-update based on binding status.

Replaces the v3.1 "✅ Binding to <name> recovered" / "⚠️ ... lost" chat
notifications, which polluted every topic on every Claude
crash/resume cycle. Instead, the bot renames the forum topic itself
to a single-line status banner:

    🟢 | <tmux_session_name> (<window_id>)    -- Claude alive
    🔴 | <tmux_session_name> (<window_id>)    -- Claude not alive

(v4.1.0 used ✅/⚠️ but those render too small to be visible in
Telegram clients' topic-list rows; the colored circles are
substantially more legible.)

The desired name is recomputed every binding-health tick. An
in-memory ``(group_chat_id, thread_id) → last_rendered`` cache
suppresses redundant ``edit_forum_topic`` API calls; only a name
that differs from the cache triggers a Telegram round-trip.

``BadRequest: Topic_not_modified`` (the topic already matches) is
silenced and treated as a successful render (cached so subsequent
ticks short-circuit). ``BadRequest: message thread not found``
(topic deleted) routes through ``auto_unbind.maybe_unbind`` to
clean up the stale row in ``topic_bindings.json``.
"""

from __future__ import annotations

import logging

from telegram import Bot
from telegram.error import BadRequest

from .auto_unbind import maybe_unbind

logger = logging.getLogger(__name__)


def desired_topic_name(session_name: str, is_alive: bool, window_id: str) -> str:
    """Format the topic name for the given binding status.

    ``window_id`` is included only when non-empty.
    """
    status = "🟢" if is_alive else "🔴"
    if window_id:
        return f"{status} | {session_name} ({window_id})"
    return f"{status} | {session_name}"


class TopicRenamer:
    """Edit forum topic names on binding-status changes.

    Single-threaded usage: called from the main asyncio loop only.
    """

    def __init__(self) -> None:
        self._last_rendered: dict[tuple[int, int], str] = {}

    async def maybe_rename(
        self,
        bot: Bot,
        *,
        group_chat_id: int,
        thread_id: int,
        session_name: str,
        is_alive: bool,
        window_id: str,
    ) -> None:
        desired = desired_topic_name(session_name, is_alive, window_id)
        key = (group_chat_id, thread_id)
        if self._last_rendered.get(key) == desired:
            return

        try:
            await bot.edit_forum_topic(
                chat_id=group_chat_id,
                message_thread_id=thread_id,
                name=desired,
            )
            self._last_rendered[key] = desired
        except BadRequest as e:
            msg = str(e).lower()
            if "topic_not_modified" in msg or "topic not modified" in msg:
                # Already matches; cache so subsequent ticks skip.
                self._last_rendered[key] = desired
                return
            if maybe_unbind(e, group_chat_id, thread_id):
                self._last_rendered.pop(key, None)
                return
            logger.warning(
                "Failed to rename topic %d in chat %d to %r: %s",
                thread_id,
                group_chat_id,
                desired,
                e,
            )
        except Exception:
            logger.exception(
                "Unexpected error renaming topic %d in chat %d",
                thread_id,
                group_chat_id,
            )
