"""Process-wide singletons for the Telegram frontend.

Two singletons:

- `topics`  : `TopicBindings` — topic -> session_name map (writes
  `topic_bindings.json`).
- `windows` : backend `WindowRegistry` instance, passed into
  `DefaultBackend` by `main.py` so the same object services both
  the slow-loop `verify_all` (backend) and frontend lookups.

Helpers join topics with window info. Liveness queries go through the
backend via `windows.is_window_alive(window_id)`.
"""

from __future__ import annotations

import logging

from ccmux.api import WindowBindings, WindowBinding

from .topic_bindings import TopicBinding, TopicBindings

logger = logging.getLogger(__name__)

topics = TopicBindings()
windows = WindowBindings()


def _join_window_id(topic: TopicBinding) -> TopicBinding:
    """Populate `window_id` on a TopicBinding by consulting WindowRegistry."""
    info = windows.get_by_session_name(topic.session_name)
    if info is None or not info.window_id:
        return topic
    return TopicBinding(
        user_id=topic.user_id,
        thread_id=topic.thread_id,
        group_chat_id=topic.group_chat_id,
        window_id=info.window_id,
        session_name=topic.session_name,
    )


def get_topic(user_id: int, thread_id: int | None) -> TopicBinding | None:
    """Return the TopicBinding for a topic with `window_id` joined in."""
    base = topics.get(user_id, thread_id)
    if base is None:
        return None
    return _join_window_id(base)


def get_topic_for_claude_session(claude_session_id: str) -> TopicBinding | None:
    """Return the TopicBinding whose Claude session matches the given id."""
    info = windows.find_by_claude_session_id(claude_session_id)
    if info is None:
        return None
    for topic in topics.all():
        if topic.session_name == info.session_name:
            return TopicBinding(
                user_id=topic.user_id,
                thread_id=topic.thread_id,
                group_chat_id=topic.group_chat_id,
                window_id=info.window_id,
                session_name=topic.session_name,
            )
    return None


def get_topic_by_session_name(session_name: str) -> TopicBinding | None:
    """First TopicBinding with the given session_name, with window_id joined."""
    for topic in topics.all():
        if topic.session_name == session_name:
            return _join_window_id(topic)
    return None


def iter_topics_joined():
    """Iterate every TopicBinding with window_id joined from WindowRegistry."""
    for topic in topics.all():
        yield _join_window_id(topic)


def get_topic_by_window_id(window_id: str) -> TopicBinding | None:
    """Return the TopicBinding whose bound window_id matches, else None."""
    if not window_id:
        return None
    info = windows.get(window_id)
    if info is None:
        return None
    for topic in topics.all():
        if topic.session_name == info.session_name:
            return TopicBinding(
                user_id=topic.user_id,
                thread_id=topic.thread_id,
                group_chat_id=topic.group_chat_id,
                window_id=window_id,
                session_name=topic.session_name,
            )
    return None


def is_window_alive(window_id: str) -> bool:
    """Cached liveness verdict (delegates to backend Protocol)."""
    from ccmux.api import get_default_backend

    return get_default_backend().is_alive(window_id)


async def resolve_stale_ids() -> None:
    """Reload window map on startup."""
    await windows.load()


__all__ = [
    "TopicBinding",
    "WindowBinding",
    "get_topic",
    "get_topic_by_session_name",
    "get_topic_by_window_id",
    "get_topic_for_claude_session",
    "is_window_alive",
    "iter_topics_joined",
    "resolve_stale_ids",
    "topics",
    "windows",
]
