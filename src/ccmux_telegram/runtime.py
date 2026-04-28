"""Process-wide singletons for the Telegram frontend.

Two singletons:

- ``topics``       : ``TopicBindings`` — topic -> session_name map (writes
  ``topic_bindings.json``).
- ``event_reader`` : backend ``EventLogReader`` — projects
  ``~/.ccmux/claude_events.jsonl`` to ``dict[tmux_session_name,
  CurrentClaudeBinding]``. Shared with ``DefaultBackend`` so the same
  reader services both the slow-loop ``verify_all`` (backend) and
  frontend lookups.

Helpers join topics with window info derived from the reader. Liveness
queries go through the frontend state cache (``state_cache.py``).
"""

from __future__ import annotations

import logging

from ccmux.api import CurrentClaudeBinding, EventLogReader
from ccmux.config import config as backend_config

from .topic_bindings import TopicBinding, TopicBindings

logger = logging.getLogger(__name__)


topics = TopicBindings()
event_reader = EventLogReader(backend_config.config_dir / "claude_events.jsonl")


def _binding_for(session_name: str) -> CurrentClaudeBinding | None:
    return event_reader.get(session_name)


def _join_window_id(topic: TopicBinding) -> TopicBinding:
    """Populate ``window_id`` on a TopicBinding from the event-log reader."""
    binding = _binding_for(topic.session_name)
    if binding is None or not binding.window_id:
        return topic
    return TopicBinding(
        user_id=topic.user_id,
        thread_id=topic.thread_id,
        group_chat_id=topic.group_chat_id,
        window_id=binding.window_id,
        session_name=topic.session_name,
    )


def get_topic(user_id: int, thread_id: int | None) -> TopicBinding | None:
    """Return the TopicBinding for a topic with ``window_id`` joined in."""
    base = topics.get(user_id, thread_id)
    if base is None:
        return None
    return _join_window_id(base)


def get_topic_for_claude_session(claude_session_id: str) -> TopicBinding | None:
    """Return the TopicBinding whose Claude session matches the given id."""
    if not claude_session_id:
        return None
    for binding in event_reader.all_alive():
        if binding.claude_session_id != claude_session_id:
            continue
        for topic in topics.all():
            if topic.session_name == binding.tmux_session_name:
                return TopicBinding(
                    user_id=topic.user_id,
                    thread_id=topic.thread_id,
                    group_chat_id=topic.group_chat_id,
                    window_id=binding.window_id,
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
    """Iterate every TopicBinding with window_id joined from the reader."""
    for topic in topics.all():
        yield _join_window_id(topic)


def get_topic_by_window_id(window_id: str) -> TopicBinding | None:
    """Return the TopicBinding whose bound window_id matches, else None."""
    if not window_id:
        return None
    for binding in event_reader.all_alive():
        if binding.window_id != window_id:
            continue
        for topic in topics.all():
            if topic.session_name == binding.tmux_session_name:
                return TopicBinding(
                    user_id=topic.user_id,
                    thread_id=topic.thread_id,
                    group_chat_id=topic.group_chat_id,
                    window_id=window_id,
                    session_name=topic.session_name,
                )
    return None


__all__ = [
    "TopicBinding",
    "CurrentClaudeBinding",
    "event_reader",
    "get_topic",
    "get_topic_by_session_name",
    "get_topic_by_window_id",
    "get_topic_for_claude_session",
    "iter_topics_joined",
    "topics",
]
