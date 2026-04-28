"""TopicBindings - Telegram-side binding persistence.

Owns `topic_bindings.json`: the mapping from (user_id, thread_id)
forum topics to tmux session names. Does NOT know about Claude sessions
or tmux window IDs - that information lives in `WindowRegistry` and is
joined onto `TopicBinding` views by the facade.

Phase D will move this into telegram/binding/topic_bindings.py.

Key types:

- `TopicBinding`: frozen dataclass exposing user_id, thread_id,
  group_chat_id, window_id (joined by facade), session_name.
- `TopicBindings`: persistent map + bind/unbind helpers. Shares the
  `_alive_status` dict by reference with `WindowRegistry`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import config
from .util import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TopicBinding:
    """Telegram-side view of a bound topic.

    Pairs a Telegram forum topic with a tmux window. Persisted in
    `topic_bindings.json`. Consumers that need Claude session details
    (cwd, claude_session_id) should separately look up `WindowBinding` by
    `window_id`.
    """

    user_id: int
    thread_id: int
    group_chat_id: int
    window_id: str  # foreign key into WindowRegistry; "" if pending
    session_name: str  # display name / tmux session name


class TopicBindings:
    """Persistent (user_id, thread_id) -> (session_name, group_chat_id) map.

    Owns `topic_bindings.json`. No liveness state is stored here —
    `is_alive(topic)` delegates to the backend via `ccmux.api`.
    """

    def __init__(self, state_file: Path | None = None) -> None:
        self._state_file = state_file if state_file is not None else config.state_file
        # in-memory mirror: (user_id, thread_id) -> (session_name, group_chat_id)
        self._raw_state: dict[tuple[int, int], tuple[str, int]] = {}
        self._read_state_file()

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _read_state_file(self) -> None:
        """Load `topic_bindings.json` into `_raw_state`."""
        self._raw_state = {}

        if not self._state_file.exists():
            logger.info("topic_bindings.json not found, starting with empty state")
            return

        try:
            raw = json.loads(self._state_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load topic_bindings.json: %s", e)
            return

        if not isinstance(raw, dict):
            logger.warning("topic_bindings.json root is not a dict, ignoring")
            return

        had_legacy_meta = False
        for user_key, topics in raw.items():
            if not isinstance(topics, dict):
                continue
            try:
                user_id = int(user_key)
            except ValueError:
                continue

            for thread_key, entry in topics.items():
                if thread_key == "_meta":
                    # Legacy watcher key — drop it; a re-save below cleans the file.
                    had_legacy_meta = True
                    continue
                if not isinstance(entry, dict):
                    continue
                try:
                    thread_id = int(thread_key)
                except ValueError:
                    continue

                session_name = entry.get("tmux_session_name", "")
                group_chat_id = entry.get("group_chat_id")
                if not session_name or not isinstance(group_chat_id, int):
                    continue

                self._raw_state[(user_id, thread_id)] = (session_name, group_chat_id)

        logger.info("Loaded %d topic(s) from topic_bindings.json", len(self._raw_state))

        if had_legacy_meta:
            logger.info("Stripping legacy _meta.watcher entries from state file")
            self._save_state_file()

    def _save_state_file(self) -> None:
        """Persist `_raw_state` to `topic_bindings.json`."""
        state: dict[str, dict[str, dict[str, Any]]] = {}

        for (user_id, thread_id), (
            session_name,
            group_chat_id,
        ) in self._raw_state.items():
            user_key = str(user_id)
            state.setdefault(user_key, {})[str(thread_id)] = {
                "tmux_session_name": session_name,
                "group_chat_id": group_chat_id,
            }

        atomic_write_json(self._state_file, state)
        logger.debug("State saved to %s", self._state_file)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def bind(
        self,
        user_id: int,
        thread_id: int,
        session_name: str,
        group_chat_id: int,
    ) -> TopicBinding:
        """Bind a Telegram topic to a tmux session and persist."""
        self._raw_state[(user_id, thread_id)] = (session_name, group_chat_id)
        self._save_state_file()

        logger.info(
            "Bound thread %d -> session '%s' for user %d",
            thread_id,
            session_name,
            user_id,
        )
        return TopicBinding(
            user_id=user_id,
            thread_id=thread_id,
            group_chat_id=group_chat_id,
            window_id="",
            session_name=session_name,
        )

    def unbind(self, user_id: int, thread_id: int) -> TopicBinding | None:
        """Remove a binding. Returns the prior TopicBinding or None."""
        if (user_id, thread_id) not in self._raw_state:
            return None

        session_name, group_chat_id = self._raw_state[(user_id, thread_id)]
        del self._raw_state[(user_id, thread_id)]
        self._save_state_file()

        logger.info("Unbound thread %d for user %d", thread_id, user_id)
        return TopicBinding(
            user_id=user_id,
            thread_id=thread_id,
            group_chat_id=group_chat_id,
            window_id="",
            session_name=session_name,
        )

    def unbind_by_thread(
        self, group_chat_id: int, thread_id: int
    ) -> list[TopicBinding]:
        """Remove every binding matching ``(group_chat_id, thread_id)``.

        Single-user setups have at most one match; multi-user setups
        could have one per user. Used to clean up stale bindings after
        Telegram returns ``BadRequest: message thread not found`` (i.e.
        the topic was deleted).
        """
        keys_to_remove: list[tuple[int, int]] = []
        removed: list[TopicBinding] = []
        for (uid, tid), (session_name, gc_id) in self._raw_state.items():
            if tid == thread_id and gc_id == group_chat_id:
                keys_to_remove.append((uid, tid))
                removed.append(
                    TopicBinding(
                        user_id=uid,
                        thread_id=tid,
                        group_chat_id=gc_id,
                        window_id="",
                        session_name=session_name,
                    )
                )
        if not keys_to_remove:
            return []
        for k in keys_to_remove:
            del self._raw_state[k]
        self._save_state_file()
        for b in removed:
            logger.info(
                "Auto-unbound thread %d (chat %d) for user %d " "(session was '%s')",
                b.thread_id,
                b.group_chat_id,
                b.user_id,
                b.session_name,
            )
        return removed

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get(self, user_id: int, thread_id: int | None) -> TopicBinding | None:
        """Return the TopicBinding for a topic, or None if unbound.

        `window_id` is left empty here. Facade joins against WindowRegistry
        to populate it.
        """
        if thread_id is None:
            return None
        entry = self._raw_state.get((user_id, thread_id))
        if entry is None:
            return None
        session_name, group_chat_id = entry
        return TopicBinding(
            user_id=user_id,
            thread_id=thread_id,
            group_chat_id=group_chat_id,
            window_id="",  # facade overlays window_id from WindowRegistry
            session_name=session_name,
        )

    def all(self) -> Iterator[TopicBinding]:
        """Iterate every persisted topic (window_id left empty for facade to fill)."""
        for (user_id, thread_id), (session_name, group_chat_id) in list(
            self._raw_state.items()
        ):
            yield TopicBinding(
                user_id=user_id,
                thread_id=thread_id,
                group_chat_id=group_chat_id,
                window_id="",
                session_name=session_name,
            )

    def all_session_names(self) -> set[str]:
        return {session_name for (session_name, _) in self._raw_state.values()}

    # ------------------------------------------------------------------
    # Liveness
    # ------------------------------------------------------------------

    def is_alive(self, topic: TopicBinding) -> bool:
        """True iff the bound instance has observed state != Dead.

        An instance that was never observed returns False (treat
        unobserved as not-alive; if the bot just started and there is no
        state yet, consumer code treats that as 'not ready to receive').
        Pending bindings (no window_id) keep the 'alive-until-proven-dead'
        semantics they had in v1.x to preserve the binding-picker UX.
        """
        if not topic.window_id:
            return True  # pending binding — no instance observed yet
        from .state_cache import get_state_cache

        return get_state_cache().is_alive(topic.session_name)
