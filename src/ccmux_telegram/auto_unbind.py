"""Auto-unbind on Telegram ``message thread not found`` errors.

Telegram has no ``forum_topic_deleted`` update — when the user deletes
a topic, the bot only finds out by the next outbound attempt failing
with ``BadRequest: message thread not found``. This module centralises
the detection + cleanup so every send/edit site can opt in via a
single helper call.

Public surface:

- ``is_thread_deleted_error(exc) -> bool`` — narrow predicate against
  ``telegram.error.BadRequest``.
- ``maybe_unbind(exc, chat_id, thread_id) -> bool`` — if the error is
  a thread-deleted BadRequest, removes the matching topic_bindings
  rows and returns True; otherwise False.
"""

from __future__ import annotations

import logging

from telegram.error import BadRequest

logger = logging.getLogger(__name__)


# Telegram returns one of these message strings when a thread (forum
# topic) referenced in the request no longer exists. Match prefixes
# loosely so minor wording drift across Bot API versions still triggers
# cleanup.
_THREAD_DELETED_NEEDLES = (
    "message thread not found",
    "topic_not_found",
    "topic not found",
)


def is_thread_deleted_error(exc: BaseException) -> bool:
    """True iff ``exc`` is a Telegram BadRequest indicating a deleted topic."""
    if not isinstance(exc, BadRequest):
        return False
    msg = str(exc).lower()
    return any(needle in msg for needle in _THREAD_DELETED_NEEDLES)


def maybe_unbind(
    exc: BaseException, chat_id: int | None, thread_id: int | None
) -> bool:
    """If ``exc`` is a thread-deleted error, drop matching bindings.

    Returns True iff at least one binding was removed.
    """
    if chat_id is None or thread_id is None:
        return False
    if not is_thread_deleted_error(exc):
        return False
    # Imported lazily to avoid an import cycle: runtime imports
    # topic_bindings, which is fine, but we want to keep this module
    # cheap to import from sender / queue layers.
    from .runtime import topics

    removed = topics.unbind_by_thread(chat_id, thread_id)
    if removed:
        logger.warning(
            "Telegram returned 'thread not found' for chat=%d thread=%d; "
            "auto-unbound %d binding(s)",
            chat_id,
            thread_id,
            len(removed),
        )
    return bool(removed)
