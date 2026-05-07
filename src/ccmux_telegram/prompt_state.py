"""Per-topic interactive prompt state.

Tracks which topics are currently showing a Claude interactive prompt
(AskUserQuestion / ExitPlanMode / Permission prompt / RestoreCheckpoint)
so outbound/inbound/status modules can coordinate without reaching into
the UI layer.

State is keyed by (user_id, thread_id_or_0) for Telegram topic support.

Key items:
  - PROMPT_TOOL_NAMES: tool names that trigger a prompt UI when they
    appear as tool_use in JSONL.
  - get/set/clear_interactive_mode: tracks which window is in interactive
    mode (so outbound knows to route user input as terminal keys).
  - get/set/pop_interactive_msg_id: tracks the Telegram message_id of
    the active prompt (so UI can edit or delete it).
  - get/set/clear_pending_prompt_tool_use: pairs the active prompt with
    the JSONL ``tool_use_id`` of the AUQ / ExitPlanMode call that
    spawned it. The matching ``tool_result`` is the authoritative
    "prompt resolved" signal — until then, transient Idle / Working
    pane states (e.g. CC's "Chat about this" side chat) must NOT
    clear the interactive message.
"""

import logging

logger = logging.getLogger(__name__)

# Tool names that trigger a prompt UI via JSONL (terminal capture + inline keyboard).
PROMPT_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

# (user_id, thread_id_or_0) -> window_id currently in interactive mode.
_interactive_mode: dict[tuple[int, int], str] = {}

# (user_id, thread_id_or_0) -> Telegram message_id of the active prompt.
_interactive_msgs: dict[tuple[int, int], int] = {}

# (user_id, thread_id_or_0) -> tool_use_id of the unresolved prompt-tool call.
_pending_prompt_tool_uses: dict[tuple[int, int], str] = {}


def get_interactive_window(user_id: int, thread_id: int | None = None) -> str | None:
    """Return the window_id currently in interactive mode, or None."""
    return _interactive_mode.get((user_id, thread_id or 0))


def set_interactive_mode(
    user_id: int, window_id: str, thread_id: int | None = None
) -> None:
    """Mark a topic as in interactive mode bound to `window_id`."""
    logger.debug(
        "Set interactive mode: user=%d, window_id=%s, thread=%s",
        user_id,
        window_id,
        thread_id,
    )
    _interactive_mode[(user_id, thread_id or 0)] = window_id


def clear_interactive_mode(user_id: int, thread_id: int | None = None) -> None:
    """Clear interactive-mode tracking (state only; UI message untouched)."""
    logger.debug("Clear interactive mode: user=%d, thread=%s", user_id, thread_id)
    _interactive_mode.pop((user_id, thread_id or 0), None)


def get_interactive_msg_id(user_id: int, thread_id: int | None = None) -> int | None:
    """Return the Telegram message_id of the active prompt, or None."""
    return _interactive_msgs.get((user_id, thread_id or 0))


def set_interactive_msg_id(
    user_id: int, msg_id: int, thread_id: int | None = None
) -> None:
    """Record the Telegram message_id of the active prompt."""
    _interactive_msgs[(user_id, thread_id or 0)] = msg_id


def pop_interactive_state(user_id: int, thread_id: int | None = None) -> int | None:
    """Clear all interactive tracking; return the popped msg_id if any.

    Used when dismissing a prompt (mode, msg_id, and pending tool_use_id
    should all be cleared together).
    """
    ikey = (user_id, thread_id or 0)
    msg_id = _interactive_msgs.pop(ikey, None)
    _interactive_mode.pop(ikey, None)
    _pending_prompt_tool_uses.pop(ikey, None)
    logger.debug(
        "Pop interactive state: user=%d, thread=%s, msg_id=%s",
        user_id,
        thread_id,
        msg_id,
    )
    return msg_id


def get_pending_prompt_tool_use(
    user_id: int, thread_id: int | None = None
) -> str | None:
    """Return the unresolved prompt-tool ``tool_use_id``, or None."""
    return _pending_prompt_tool_uses.get((user_id, thread_id or 0))


def set_pending_prompt_tool_use(
    user_id: int, thread_id: int | None, tool_use_id: str
) -> None:
    """Record the ``tool_use_id`` of the prompt that just opened."""
    logger.debug(
        "Set pending prompt tool_use: user=%d, thread=%s, tool_use_id=%s",
        user_id,
        thread_id,
        tool_use_id,
    )
    _pending_prompt_tool_uses[(user_id, thread_id or 0)] = tool_use_id


def clear_pending_prompt_tool_use(
    user_id: int, thread_id: int | None = None
) -> str | None:
    """Forget the tracked tool_use_id; return the popped value if any."""
    popped = _pending_prompt_tool_uses.pop((user_id, thread_id or 0), None)
    if popped is not None:
        logger.debug(
            "Clear pending prompt tool_use: user=%d, thread=%s, tool_use_id=%s",
            user_id,
            thread_id,
            popped,
        )
    return popped
