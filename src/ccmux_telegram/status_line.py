"""Status event consumer — Telegram-side translator for WindowStatus.

Consumes the raw WindowStatus observations produced by StatusMonitor and
performs all Telegram-facing actions:

  - Enqueue status-line updates (respecting queue non-empty → skip).
  - Show/hide interactive prompt UI (permission prompts, AskUserQuestion,
    ExitPlanMode, etc.) detected via terminal polling rather than JSONL.
  - Track interactive-mode state transitions (same window, switched window,
    prompt just disappeared).

This keeps the server-side status_monitor free of Telegram knowledge.
"""

import logging

from telegram import Bot

from ccmux.api import WindowStatus
from .prompt import clear_interactive_msg, handle_interactive_ui
from .prompt_state import get_interactive_window
from .message_queue import enqueue_status_update, get_message_queue
from .watcher import get_service as _get_watcher_service

logger = logging.getLogger(__name__)


async def consume_statuses(bot: Bot, statuses: list[WindowStatus]) -> None:
    """Apply every status observation to the Telegram side."""
    for s in statuses:
        await consume_status_one(bot, s)


async def consume_status_one(bot: Bot, s: WindowStatus) -> None:
    """Single-status variant — convenience wrapper around `_consume_one`.

    Resolves the backend-native `WindowStatus` (window_id only) to the
    owning topic binding. Observations for windows with no bound topic
    are ignored at the consumer layer.
    """
    from .runtime import get_topic_by_window_id

    topic = get_topic_by_window_id(s.window_id)
    if topic is None:
        # Window has no Telegram binding — feed the watcher anyway (it
        # filters by its own registration) and skip status rendering.
        try:
            _get_watcher_service().process(s, topic=None)
        except Exception as e:
            logger.debug("Watcher process error: %s", e)
        return

    try:
        await _consume_one(bot, s, topic)
    except Exception as e:
        logger.debug(
            "Status consume error for user %d thread %s: %s",
            topic.user_id,
            topic.thread_id,
            e,
        )
    try:
        _get_watcher_service().process(s, topic=topic)
    except Exception as e:
        logger.debug("Watcher process error: %s", e)


async def _consume_one(bot: Bot, s: WindowStatus, topic) -> None:
    user_id = topic.user_id
    thread_id = topic.thread_id
    chat_id = topic.group_chat_id
    window_id = s.window_id

    queue = get_message_queue(user_id, thread_id or 0)
    skip_status = queue is not None and not queue.empty()

    # Window gone → clear status (unless queue is actively draining)
    if not s.window_exists:
        if not skip_status:
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id, chat_id=chat_id
            )
        return

    # Transient capture failure → keep existing status message
    if not s.pane_captured:
        return

    interactive_window = get_interactive_window(user_id, thread_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if s.interactive_ui is not None:
            # UI still showing — skip status update (user is interacting)
            return
        # UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(user_id, bot, thread_id, chat_id=chat_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window — clear stale
        await clear_interactive_msg(user_id, bot, thread_id, chat_id=chat_id)

    # New interactive UI? Always checked regardless of skip_status.
    if should_check_new_ui and s.interactive_ui is not None:
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        await handle_interactive_ui(bot, user_id, window_id, thread_id, chat_id=chat_id)
        return

    # Normal status line — skip when queue is draining
    if skip_status:
        return

    if s.status_text:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            s.status_text,
            thread_id=thread_id,
            chat_id=chat_id,
        )
    # If no status line, keep existing status message (don't clear on transient state)
