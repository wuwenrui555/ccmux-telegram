"""Watcher — "who's waiting for you" dashboard topic.

Registers one topic (per user) as a dashboard. For every other bound
topic, tracks whether it is currently working or waiting (idle with
no prompt). When a source topic stays waiting for DEBOUNCE_SECONDS,
the watcher sends/edits one message in the dashboard topic. When the
source transitions back to working, the message is deleted.

Entry points:

  - watcher_command: the /watcher handler that registers the caller's
    current topic as their dashboard.
  - WatcherService.process: called from status_line.consume_status_one
    with every observed WindowStatus; updates in-memory state only.
  - WatcherService.tick: called once per status batch; performs the
    actual send/edit/delete against Telegram after applying the
    debounce and reconciling against current bindings.

See docs/superpowers/specs/2026-04-15-watcher-design.md for full design.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from telegram import Bot, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ccmux.api import get_default_backend
from ccmux.api import WindowStatus
from .runtime import topics as _topics
from .util import authorized
from .sender import NO_LINK_PREVIEW, safe_reply
from .topic_bindings import TopicBinding


def _html_escape(s: str) -> str:
    """Escape the four characters Telegram HTML parse_mode requires."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


logger = logging.getLogger(__name__)

# A source topic must be continuously "waiting" for this many seconds
# before the watcher notification fires. Absorbs transient idle windows
# between tool_result and the next assistant turn.
DEBOUNCE_SECONDS = 2.5

# Fallback preview when the last turn ended without an assistant text
# message (e.g. a tool_use + tool_result cycle with no follow-up text).
_FALLBACK_PREVIEW = "(上一步是 tool 调用)"

_PREVIEW_MAX_CHARS = 200

# Telegram BadRequest messages that indicate the watcher topic no longer
# exists (user deleted it). Match substring; wording varies across versions.
_DEAD_WATCHER_ERROR_FRAGMENTS = (
    "message thread not found",
    "thread not found",
    "topic_closed",
    "chat not found",
    "bot was kicked",
)


def _is_dead_watcher_error(exc: Exception | None) -> bool:
    if exc is None:
        return False
    msg = str(exc).lower()
    return any(frag in msg for frag in _DEAD_WATCHER_ERROR_FRAGMENTS)


SourceState = Literal["working", "waiting"]


def classify(status: WindowStatus) -> SourceState | None:
    """Derive a SourceState from a single WindowStatus observation.

    Returns None for transient observations (pane capture failed, window
    gone) — caller should leave prior state untouched.

    Claude Code's status line shows `…` (U+2026) only while actively
    working ("Thinking… (12s)"). Post-completion summaries use past
    tense with no ellipsis ("Sautéed for 5m 46s"). So the ellipsis is
    a reliable signal of "currently processing"; anything else — even
    a non-empty status line — means Claude is idle.
    """
    if not status.window_exists or not status.pane_captured:
        return None
    if status.interactive_ui is not None:
        return "working"
    if status.status_text is not None and "…" in status.status_text:
        return "working"
    return "waiting"


@dataclass
class _SourceEntry:
    """In-memory state for one watched source topic."""

    source_thread_id: int
    current_state: SourceState = "working"
    first_waiting_at: float | None = None  # monotonic; None when not waiting


@dataclass
class _Dashboard:
    """One aggregated dashboard message per user."""

    message_id: int | None = None
    last_rendered: str = ""  # dedupe edits
    # Per-🔔 content snapshot: {source_thread_id: preview_text}. Any change
    # here (new topic, removed topic, or preview text changed on an existing
    # 🔔) triggers a fresh send (delete old + send new) so the user sees an
    # unread badge + hears the notification. edit_message_text is silent in
    # Telegram clients, so we only use it when the waiting content is
    # unchanged but the ⏳ block rearranged.
    last_waiting_content: dict[int, str] = field(default_factory=dict)


class WatcherService:
    """Per-process watcher. One aggregated dashboard message per user that
    edits in place as source topics flip between working and waiting."""

    def __init__(self) -> None:
        # (user_id, source_thread_id) -> _SourceEntry
        self._entries: dict[tuple[int, int], _SourceEntry] = {}
        # user_id -> _Dashboard (single persistent message)
        self._dashboards: dict[int, _Dashboard] = {}

    # -------- observation (called per status) --------

    def process(
        self, status: WindowStatus, *, topic: "TopicBinding | None" = None
    ) -> None:
        """Update in-memory state from a single WindowStatus observation.

        The consumer resolves window_id → topic and passes it in;
        observations for unbound windows are dropped here. Does no
        Telegram I/O — tick() renders/edits the dashboard after the
        status batch.
        """
        if topic is None or topic.thread_id is None:
            return
        # Skip the watcher topic itself — it's the dashboard destination,
        # not a monitored source.
        if _topics.is_watcher(topic.user_id, topic.thread_id):
            return

        new_state = classify(status)
        if new_state is None:
            return  # transient, keep prior state

        key = (topic.user_id, topic.thread_id)
        entry = self._entries.get(key)
        if entry is None:
            entry = _SourceEntry(source_thread_id=topic.thread_id)
            self._entries[key] = entry

        if new_state == "waiting":
            if entry.current_state == "working":
                entry.first_waiting_at = time.monotonic()
            entry.current_state = "waiting"
        else:  # "working"
            entry.current_state = "working"
            entry.first_waiting_at = None

    # -------- time-based dispatch (called after each status batch) --------

    async def tick(self, bot: Bot) -> None:
        """Render the dashboard for each user, delivered to their watcher topic."""
        bound = {(t.user_id, t.thread_id) for t in _topics.all()}

        # Reconcile: drop entries for sources that are no longer bound
        for key in list(self._entries.keys()):
            if key not in bound:
                self._entries.pop(key, None)

        # Group alive entries by user
        by_user: dict[int, list[tuple[TopicBinding, _SourceEntry]]] = {}
        for t in _topics.all():
            entry = self._entries.get((t.user_id, t.thread_id))
            if entry is None:
                continue
            by_user.setdefault(t.user_id, []).append((t, entry))

        now = time.monotonic()
        for user_id, rows in by_user.items():
            watcher = _topics.get_watcher(user_id)
            if watcher is None:
                continue
            chat_id, watcher_tid = watcher
            await self._render_user_dashboard(
                bot, user_id, chat_id, watcher_tid, rows, now
            )

        # Users whose dashboards exist but have no rows → clear dashboard
        for user_id, dash in list(self._dashboards.items()):
            if user_id in by_user:
                continue
            if dash.message_id is None:
                continue
            watcher = _topics.get_watcher(user_id)
            if watcher is None:
                continue
            chat_id, _ = watcher
            await self._delete_message(bot, chat_id, dash.message_id)
            dash.message_id = None
            dash.last_rendered = ""

    async def _render_user_dashboard(
        self,
        bot: Bot,
        user_id: int,
        watcher_chat_id: int,
        watcher_tid: int,
        rows: list[tuple[TopicBinding, _SourceEntry]],
        now: float,
    ) -> None:
        """Render and send/edit the aggregated dashboard for one user."""

        # Sort rows: waiting first (by first_waiting_at asc), then working by name
        def sort_key(row: tuple[TopicBinding, _SourceEntry]) -> tuple[int, float, str]:
            topic, entry = row
            if self._effective_waiting(entry, now):
                return (0, entry.first_waiting_at or 0.0, topic.session_name)
            return (1, 0.0, topic.session_name)

        rows_sorted = sorted(rows, key=sort_key)

        # Build status lines (all topics)
        status_lines: list[str] = []
        waiting_rows: list[tuple[TopicBinding, _SourceEntry]] = []
        for topic, entry in rows_sorted:
            is_waiting = self._effective_waiting(entry, now)
            symbol = "🔔" if is_waiting else "⏳"
            status_lines.append(f"{symbol} {topic.session_name}")
            if is_waiting:
                waiting_rows.append((topic, entry))

        # Build waiting detail lines (links + preview). The link targets
        # the source topic in its group (topic.group_chat_id); cross-chat
        # from the user's DM navigates reliably. When we know a recent
        # message_id sent to the topic, use that as the target so the
        # click lands near the latest activity (instead of the topic's
        # root / creation message).
        from .message_queue import get_last_content_msg

        detail_lines: list[str] = []
        current_waiting_content: dict[int, str] = {}
        for topic, _entry in waiting_rows:
            recent_msg_id = get_last_content_msg(user_id, topic.thread_id)
            url = _build_topic_deeplink(
                topic.group_chat_id, topic.thread_id, recent_msg_id
            )
            preview = await _fetch_last_assistant_preview(topic.session_name)
            current_waiting_content[topic.thread_id] = preview
            name_esc = _html_escape(topic.session_name)
            preview_esc = _html_escape(preview)
            detail_lines.append(f'🔔 <a href="{url}">{name_esc}</a>\n{preview_esc}')

        # HTML parse_mode (MarkdownV2 silently suppresses notifications in
        # some Telegram clients; HTML rings reliably).
        status_block = "\n".join(_html_escape(line) for line in status_lines)
        text_parts = [status_block]
        if detail_lines:
            text_parts.append("\n\n待回复：\n\n" + "\n\n".join(detail_lines))
        text = "".join(text_parts)

        dash = self._dashboards.setdefault(user_id, _Dashboard())

        # Dedupe: nothing changed at all
        if text == dash.last_rendered and dash.message_id is not None:
            return

        # Fresh send = delete old + send new. Telegram rings + badge only
        # on send; edit is silent. We send fresh only when a 🔔 is NEW
        # (tid absent from last snapshot). Preview-text changes inside the
        # same waiting window → edit (silent): not worth the API cost,
        # and user attends per working→waiting transition anyway.
        should_ping = any(
            tid not in dash.last_waiting_content for tid in current_waiting_content
        )

        if dash.message_id is None or should_ping:
            if dash.message_id is not None:
                await self._delete_message(bot, watcher_chat_id, dash.message_id)
                dash.message_id = None
            await self._send_new_dashboard(
                bot, user_id, watcher_chat_id, watcher_tid, text, dash
            )
        else:
            await self._edit_dashboard(bot, user_id, watcher_chat_id, text, dash)

        dash.last_waiting_content = current_waiting_content

    @staticmethod
    def _effective_waiting(entry: _SourceEntry, now: float) -> bool:
        """Waiting after the debounce threshold has elapsed."""
        if entry.current_state != "waiting":
            return False
        if entry.first_waiting_at is None:
            return False
        return (now - entry.first_waiting_at) >= DEBOUNCE_SECONDS

    async def _send_new_dashboard(
        self,
        bot: Bot,
        user_id: int,
        watcher_chat_id: int,
        watcher_tid: int,
        text: str,
        dash: _Dashboard,
    ) -> None:
        """Send a fresh dashboard message to the user's watcher topic."""
        sent = None
        last_error: Exception | None = None
        try:
            sent = await bot.send_message(
                chat_id=watcher_chat_id,
                text=text,
                message_thread_id=watcher_tid,
                parse_mode="HTML",
                link_preview_options=NO_LINK_PREVIEW,
                disable_notification=False,
            )
        except Exception as e:
            last_error = e
            logger.debug("Watcher dashboard send failed: %s", e)
        if sent is not None:
            dash.message_id = sent.message_id
            dash.last_rendered = text
            logger.info(
                "Watcher dashboard sent to user %d (msg_id=%d) — should ping",
                user_id,
                sent.message_id,
            )
            return
        # Send failed — if the watcher topic is gone, auto-clear so we stop
        # hammering a dead thread every tick.
        if _is_dead_watcher_error(last_error):
            logger.warning(
                "Watcher topic user=%d chat=%d thread=%d appears deleted (%s); "
                "clearing registration.",
                user_id,
                watcher_chat_id,
                watcher_tid,
                last_error,
            )
            _topics.clear_watcher(user_id)
        else:
            logger.debug("Watcher send final failure: %s", last_error)

    async def _edit_dashboard(
        self,
        bot: Bot,
        user_id: int,
        watcher_chat_id: int,
        text: str,
        dash: _Dashboard,
    ) -> None:
        assert dash.message_id is not None
        try:
            await bot.edit_message_text(
                chat_id=watcher_chat_id,
                message_id=dash.message_id,
                text=text,
                parse_mode="HTML",
                link_preview_options=NO_LINK_PREVIEW,
            )
            dash.last_rendered = text
        except BadRequest as e:
            msg = str(e).lower()
            if "not modified" in msg:
                dash.last_rendered = text
                return
            # Message deleted externally → resend next tick
            logger.debug("Watcher dashboard edit failed (%s); resending", e)
            dash.message_id = None
            dash.last_rendered = ""
        except Exception as e:
            logger.debug("Watcher dashboard edit failed: %s", e)

    # -------- topic close hook --------

    async def on_source_closed(
        self, bot: Bot, user_id: int, source_thread_id: int
    ) -> None:
        """Called when a source Telegram topic is closed/deleted."""
        del bot
        # If the closed topic IS the registered watcher, clear registration
        # and drop the dashboard record (message is gone with the topic).
        watcher = _topics.get_watcher(user_id)
        if watcher is not None and watcher[1] == source_thread_id:
            _topics.clear_watcher(user_id)
            dash = self._dashboards.pop(user_id, None)
            if dash is not None:
                dash.message_id = None
            logger.info(
                "Watcher topic closed (user=%d thread=%d); cleared registration.",
                user_id,
                source_thread_id,
            )
            return

        # Otherwise it's a source topic — drop its entry, next tick re-renders.
        self._entries.pop((user_id, source_thread_id), None)
        logger.info(
            "Watcher stopped for closed source topic (user=%d thread=%d).",
            user_id,
            source_thread_id,
        )

    async def _delete_message(self, bot: Bot, chat_id: int, message_id: int) -> None:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.debug("Watcher delete failed: %s", e)


def _build_topic_deeplink(
    chat_id: int, thread_id: int, message_id: int | None = None
) -> str:
    """Build a Telegram forum-topic deep-link (cross-chat from DM).

    Format: `https://t.me/c/<internal>/<thread_id>/<message_id>`.

    `message_id` is the specific message in the topic to scroll to.
    When None, falls back to `thread_id` — which lands on the topic's
    creation message (top of the topic). Callers should pass a recent
    bot-sent message_id so clicks land near current activity instead.

    Cross-chat navigation via this 3-segment form works on current
    Telegram clients (verified 2026-04-15). Same-chat cross-topic
    navigation does NOT work on most clients — that's why the watcher
    delivers the dashboard to the user's DM rather than a group topic.
    """
    as_str = str(chat_id)
    if as_str.startswith("-100"):
        internal = as_str[4:]
    else:
        internal = as_str.lstrip("-")
    msg = message_id if message_id is not None else thread_id
    return f"https://t.me/c/{internal}/{thread_id}/{msg}"


async def _fetch_last_assistant_preview(session_name: str) -> str:
    """Read the last assistant-text message from the source's JSONL.

    Returns a fallback string when the last turn ended without any text
    (e.g. tool_use + tool_result only). Preview is truncated to
    _PREVIEW_MAX_CHARS.
    """
    try:
        backend = get_default_backend()
    except RuntimeError:
        return _FALLBACK_PREVIEW

    # Find the source topic by session_name → WindowBinding → session_id.
    from .runtime import get_topic_by_session_name, windows

    topic = get_topic_by_session_name(session_name)
    if topic is None or not topic.window_id:
        return _FALLBACK_PREVIEW
    window = windows.get(topic.window_id)
    if window is None or not window.claude_session_id:
        return _FALLBACK_PREVIEW

    try:
        messages = await backend.claude.get_history(window.claude_session_id)
    except Exception as e:
        logger.debug("Watcher preview fetch failed: %s", e)
        return _FALLBACK_PREVIEW

    # Scan from the end for the last assistant text message.
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        if msg.get("content_type") != "text":
            continue
        text = (msg.get("text") or "").strip()
        if text:
            return _truncate(text, _PREVIEW_MAX_CHARS)
    return _FALLBACK_PREVIEW


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ----------------------------------------------------------------------
# /watcher command
# ----------------------------------------------------------------------


@authorized()
async def watcher_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle: the current topic becomes the user's watcher dashboard.

    Must be called inside a topic (not DM). Running it in the already-
    registered topic disables the watcher; in a different topic moves
    the registration to that topic.
    """
    del context
    user = update.effective_user
    assert user
    msg = update.message
    if msg is None:
        return

    from .util import get_thread_id

    thread_id = get_thread_id(update)
    if thread_id is None or msg.chat is None:
        await safe_reply(msg, "❌ `/watcher` 只能在 group 的 topic 里用。")
        return

    # Refuse if the topic is already a bound Claude source
    existing = _topics.get(user.id, thread_id)
    if existing is not None:
        await safe_reply(
            msg,
            f"❌ 当前 topic 已绑定 session `{existing.session_name}`。\n"
            "先 /unbind 才能设为 watcher。",
        )
        return

    current = _topics.get_watcher(user.id)
    if current is not None and current == (msg.chat.id, thread_id):
        _topics.clear_watcher(user.id)
        await safe_reply(msg, "🔕 Watcher 已关闭。")
        return

    _topics.set_watcher(user.id, msg.chat.id, thread_id)
    await safe_reply(
        msg,
        "🔔 Watcher 已开启。\n"
        "这个 topic 会收到聚合 dashboard：⏳ 工作中 / 🔔 等你回复。\n"
        "（同群跨 topic 的链接点击无效是 Telegram 客户端限制；session 名认一下手动切 topic。）",
    )


# ----------------------------------------------------------------------
# Module-level service singleton (simple DI)
# ----------------------------------------------------------------------

_service = WatcherService()


def get_service() -> WatcherService:
    """Return the process-wide WatcherService."""
    return _service
