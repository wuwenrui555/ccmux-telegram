"""State event consumer — Telegram-side translator for ClaudeState.

Consumes (instance_id, ClaudeState) observations from the backend and
performs all Telegram-facing actions:

  - Working(text)       → enqueue status line update.
  - Idle()              → clear any dangling interactive message.
  - Blocked(ui, content) → hand off to the interactive UI handler.
  - Dead()              → surface a 'Resuming session…' placeholder.

Every observation updates the module-level StateCache first so
downstream consumers (topic_bindings.is_alive) see the latest state
before any side effect runs.
"""

from __future__ import annotations

import logging

from telegram import Bot

from ccmux.api import Blocked, ClaudeState, Dead, Idle, Working

from .message_queue import enqueue_status_update
from .prompt_state import get_interactive_window, get_pending_prompt_tool_use
from .state_cache import get_state_cache

logger = logging.getLogger(__name__)


# Thin forwarding wrappers so that tests can monkeypatch
# `ccmux_telegram.status_line.clear_interactive_msg` /
# `ccmux_telegram.status_line.handle_interactive_ui` without importing
# `ccmux_telegram.prompt` at module load.
async def clear_interactive_msg(*args, **kwargs):  # type: ignore[no-untyped-def]
    from .prompt import clear_interactive_msg as _f

    return await _f(*args, **kwargs)


async def handle_interactive_ui(*args, **kwargs):  # type: ignore[no-untyped-def]
    from .prompt import handle_interactive_ui as _f

    return await _f(*args, **kwargs)


async def on_state(instance_id: str, state: ClaudeState, *, bot: Bot) -> None:
    """Apply a ClaudeState observation to the Telegram side.

    `instance_id` is the stable backend identifier (equal to
    `topic.session_name` in the current TopicBinding). Frontend
    resolves `window_id` via `backend.get_instance(instance_id).window_id`
    when it needs the tmux handle.
    """
    # Always update the cache first so downstream consumers see the
    # latest observed state even if we early-return below. The return
    # value edge-triggers dispatch so the backend's per-tick re-emit
    # does not spam identical Telegram payloads.
    changed = get_state_cache().update(instance_id, state)

    from .runtime import get_topic_by_session_name

    topic = get_topic_by_session_name(instance_id)

    if topic is None:
        # Instance has no bound Telegram topic; nothing to render.
        return

    if not changed:
        # State identical to last observation; skip dispatch to avoid
        # re-sending the same keyboard / status line every fast tick.
        return

    user_id = topic.user_id
    thread_id = topic.thread_id
    chat_id = topic.group_chat_id
    window_id = topic.window_id  # joined by runtime helper; may be "" if pending

    try:
        await _dispatch(bot, state, instance_id, user_id, thread_id, chat_id, window_id)
    except Exception as e:
        logger.debug(
            "on_state dispatch error for instance=%s user=%d thread=%s: %s",
            instance_id,
            user_id,
            thread_id,
            e,
        )


async def _dispatch(
    bot: Bot,
    state: ClaudeState,
    instance_id: str,
    user_id: int,
    thread_id: int | None,
    chat_id: int,
    window_id: str,
) -> None:
    match state:
        case Working(status_text=text):
            await enqueue_status_update(
                bot,
                user_id,
                window_id,
                text,
                thread_id=thread_id,
                chat_id=chat_id,
            )

        case Idle():
            # Clear any dangling interactive message bound to this instance,
            # unless an AUQ / ExitPlanMode tool_use is still pending. CC's
            # "Chat about this" item opens a side-chat that briefly puts
            # the pane back into Idle (full input chrome rendered) without
            # actually answering the prompt; deleting the button on that
            # transient Idle would leave the user with no way to answer
            # the still-open AUQ. The matching tool_result clears the
            # pending tool_use_id and is what truly closes the prompt.
            if (
                get_interactive_window(user_id, thread_id) == window_id
                and get_pending_prompt_tool_use(user_id, thread_id) is None
            ):
                await clear_interactive_msg(user_id, bot, thread_id, chat_id=chat_id)
            # Flush any user messages buffered while this instance was
            # Working / Blocked. The buffer is per-window and FIFO.
            if window_id:
                from .message_dispatch import drain_for_window

                await drain_for_window(bot, window_id)

        case Blocked(ui=ui, content=content):
            await handle_interactive_ui(
                bot,
                user_id,
                window_id,
                thread_id,
                chat_id=chat_id,
                ui=ui,
                content=content,
            )

        case Dead():
            await enqueue_status_update(
                bot,
                user_id,
                window_id,
                "Resuming session\u2026",
                thread_id=thread_id,
                chat_id=chat_id,
            )
