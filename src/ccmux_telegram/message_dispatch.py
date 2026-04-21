"""State-gated dispatch of user text to Claude Code.

Every `send_text` from Telegram to a CC pane routes through this
module. Before sending, we consult the destination window's last
observed ClaudeState (via the frontend StateCache) and gate:

- `Idle` (or no observation yet): send immediately.
- `Working` / `Blocked`: buffer locally and mark the originating
  Telegram message with a ⏳ reaction. Drain when the window
  transitions to Idle.
- `Dead`: reject with a user-visible error.

Rationale: if we type user text into CC's input box while CC is
Working, the box expands vertically and pushes the spinner out of
`parse_status_line`'s scan range. That flips the bot's state
detection to Idle, which in turn oscillates the pinned status
message and produces duplicate sends. Gating the dispatch at the
source removes the root cause — the spinner stays anchored to
chrome with a one-line input, and everything downstream is stable.

Navigation keys (Up / Down / Enter for prompt responses, Escape to
interrupt) do NOT go through this module. They are responses to the
Blocked UI, not content for a new turn, and must pass through
unconditionally. Those call sites stay on the direct `tm.send_keys`
path in `prompt.py` / `command_basic.py`.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass

from telegram import Bot, ReactionTypeEmoji

from ccmux.api import Blocked, Dead, Working, get_default_backend

from .runtime import get_topic_by_window_id
from .state_cache import get_state_cache

logger = logging.getLogger(__name__)


# Emoji reactions applied to the originating Telegram message so the
# user can see at a glance what happened to what they sent. Pick two
# distinct glyphs so sent-now and pending states never get confused.
_REACTION_SENT = "👤"
_REACTION_PENDING = "🤔"


@dataclass
class _Pending:
    chat_id: int
    message_id: int
    window_id: str
    text: str


# Module-level pending buffer keyed by window_id. In-memory only:
# a bot restart drops everything, which is fine because the user
# can simply resend. Long-term persistence would complicate
# ordering guarantees for no real win.
_pending: dict[str, deque[_Pending]] = defaultdict(deque)


async def dispatch_text(
    *,
    bot: Bot,
    chat_id: int,
    message_id: int,
    window_id: str,
    text: str,
) -> tuple[bool, str]:
    """Send ``text`` to the CC window or pend it based on current state.

    Returns ``(True, "")`` when the text was sent or successfully
    pended; ``(False, error_message)`` when the target is Dead or
    the send failed. The caller surfaces the error to the user via
    its usual reply path.
    """
    state = _get_state_for_window(window_id)

    if isinstance(state, Dead):
        return False, "Claude 已停止，消息未送达"

    if isinstance(state, (Working, Blocked)):
        _pending[window_id].append(
            _Pending(
                chat_id=chat_id, message_id=message_id, window_id=window_id, text=text
            )
        )
        await _set_reaction(bot, chat_id, message_id, _REACTION_PENDING)
        logger.info(
            "Dispatch pending: window=%s, state=%s, queue_len=%d",
            window_id,
            type(state).__name__,
            len(_pending[window_id]),
        )
        return True, ""

    # Idle (or unknown — send through and trust the backend).
    return await _send_now(bot, chat_id, message_id, window_id, text)


async def drain_for_window(bot: Bot, window_id: str) -> None:
    """Flush every pending message for ``window_id`` in FIFO order.

    Intended call site: the ``on_state`` handler when the window
    transitions to Idle. Safe to call when the queue is empty.
    """
    queue = _pending.get(window_id)
    if not queue:
        return
    logger.info(
        "Draining %d pending message(s) for window=%s",
        len(queue),
        window_id,
    )
    while queue:
        item = queue.popleft()
        ok, err = await _send_now(
            bot, item.chat_id, item.message_id, item.window_id, item.text
        )
        if not ok:
            # Surface once per failed message; caller has already
            # left a pending reaction so the user will see the
            # reaction stay ⏳ until they figure out what went wrong.
            logger.warning("drain send failed for window=%s: %s", item.window_id, err)


async def _send_now(
    bot: Bot, chat_id: int, message_id: int, window_id: str, text: str
) -> tuple[bool, str]:
    success, message = await get_default_backend().tmux.send_text(window_id, text)
    if success:
        await _set_reaction(bot, chat_id, message_id, _REACTION_SENT)
    return success, message


def _get_state_for_window(window_id: str):  # type: ignore[no-untyped-def]
    topic = get_topic_by_window_id(window_id)
    if topic is None:
        return None
    return get_state_cache().get(topic.session_name)


async def _set_reaction(bot: Bot, chat_id: int, message_id: int, emoji: str) -> None:
    """Best-effort reaction. Never raises: reactions are a nice-to-have,
    a failure here (bot lacks permission, emoji unsupported, etc.)
    should not break the send path.
    """
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception as e:
        logger.debug("set_message_reaction failed: %s", e)


# Exposed for tests: let them inspect/clear the buffer without
# poking at a private module attribute.
def _pending_snapshot(window_id: str) -> list[_Pending]:
    return list(_pending.get(window_id, deque()))


def _pending_clear_all() -> None:
    _pending.clear()
