"""Frontend utility helpers — Telegram-facing pieces only.

`ccmux_dir()` and `atomic_write_json()` are mirrored from the backend
package on purpose: the frontend has its own state files
(`topic_bindings.json`) and shouldn't have to import backend
internals just to manage them.
"""

import json
import logging
import os
import tempfile
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Coroutine

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CCMUX_DIR_ENV = "CCMUX_DIR"


def ccmux_dir() -> Path:
    """Resolve the ccmux config directory from `CCMUX_DIR` or `~/.ccmux`."""
    raw = os.environ.get(CCMUX_DIR_ENV, "")
    return Path(raw) if raw else Path.home() / ".ccmux"


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON data to a file atomically (temp+rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=indent)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def is_user_allowed(user_id: int | None) -> bool:
    """Check if a user is authorized to use the bot."""
    from .config import config

    return user_id is not None and config.is_user_allowed(user_id)


Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]


def authorized(*, notify: bool = False) -> Callable[[Handler], Handler]:
    """Decorator gating a PTB handler on `is_user_allowed(user.id)`.

    Unauthorized or anonymous updates are dropped. With ``notify=True``
    the user receives a one-shot "unauthorized" reply before the drop.
    """
    from .sender import safe_reply

    def decorator(handler: Handler) -> Handler:
        @wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            if not user or not is_user_allowed(user.id):
                if notify and update.message:
                    await safe_reply(
                        update.message, "You are not authorized to use this bot."
                    )
                return
            await handler(update, context)

        return wrapper

    return decorator


def get_thread_id(update: Update) -> int | None:
    """Extract thread_id from an update, returning None if not in a named topic."""
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


async def get_tm_and_window(window_id: str):
    """Look up TmuxSession and window. Returns (tm, w) or None."""
    from ccmux.api import tmux_registry

    tm = tmux_registry.get_by_window_id(window_id)
    if not tm:
        logger.warning("No TmuxSession for window_id=%s", window_id)
        return None
    w = await tm.find_window_by_id(window_id)
    if not w:
        logger.warning("Window not found: %s", window_id)
        return None
    return tm, w


def has_window_binding(session_name: str) -> bool:
    """True iff the event log holds a current binding for ``session_name``
    with both ``window_id`` and ``claude_session_id`` populated.
    """
    from .runtime import event_reader

    binding = event_reader.get(session_name)
    return binding is not None and bool(binding.window_id and binding.claude_session_id)
