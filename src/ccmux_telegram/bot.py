"""Telegram bot — application factory and callback router.

Builds the python-telegram-bot Application, registers all handlers, and
manages the bot lifecycle (startup, shutdown, session monitor, status polling).

Delegates all real work to sub-modules:
  - outbound: user-to-Claude message handlers
  - inbound: Claude-to-user message delivery
  - ui.binding: session binding flow and topic lifecycle
  - ui.commands: /esc, /usage, /start, /unbind
  - ui.prompt: interactive prompt UI callback handlers
  - ui.history: /history command and pagination callbacks
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from telegram import BotCommand, Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import config
from .runtime import resolve_stale_ids, windows as _windows
from ccmux.api import DefaultBackend, set_default_backend
from .message_in import handle_new_message
from .bash_capture import shutdown_bash_captures
from .message_out import (
    forward_command_handler,
    photo_handler,
    text_handler,
    unsupported_content_handler,
    voice_handler,
)
from .voice_transcribe import close_client as close_transcribe_client
from .message_queue import shutdown_workers
from .status_line import on_state as _on_state_handler
from . import binding_callbacks as binding
from . import binding_steal
from . import binding_lifecycle
from . import command_basic as commands
from . import command_history as history_mod
from . import prompt
from . import watcher as _watcher
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_PERM_NORMAL,
    CB_PERM_SKIP,
    CB_TMUX_FILTER_ALL,
    CB_TMUX_FILTER_BOUND,
    CB_TMUX_FILTER_UNBOUND,
    CB_TMUX_SESSION_CANCEL,
    CB_TMUX_SESSION_NEW,
    CB_TMUX_SESSION_SELECT,
    CB_TMUX_STEAL,
    CB_TMUX_STEAL_CANCEL,
    CB_TMUX_STEAL_CONFIRM,
    CB_TMUX_WIN_BIND,
    CB_TMUX_WIN_CANCEL,
    CB_TMUX_WIN_NEW,
)

logger = logging.getLogger(__name__)

# Background backend (owns the fast/slow poll loops internally)
_backend: DefaultBackend | None = None
_watcher_tick_task: asyncio.Task[None] | None = None
_WATCHER_TICK_INTERVAL = 1.0  # seconds; debounce-granularity floor

# Claude Code commands shown in bot menu (forwarded via tmux)
CC_COMMANDS: dict[str, str] = {
    "clear": "↗ Clear conversation history",
    "compact": "↗ Compact conversation context",
    "cost": "↗ Show token/cost usage",
    "help": "↗ Show Claude Code help",
    "memory": "↗ Edit CLAUDE.md",
    "model": "↗ Switch AI model",
}


# --- Callback query router ---


# Callback routes: ordered (prefix-or-exact, handler). First match wins.
# Every handler has signature (Update, Context) -> Awaitable[None].
# str.startswith accepts a tuple of strings; each entry here lists the
# full set of callback_data values / prefixes that route to its handler.
_ROUTES: list[tuple[tuple[str, ...], Callable[..., Awaitable[None]]]] = [
    (
        (CB_TMUX_FILTER_ALL, CB_TMUX_FILTER_UNBOUND, CB_TMUX_FILTER_BOUND),
        binding.handle_filter_switch,
    ),
    # Order matters: steal_confirm/cancel both start with "ts:steal" but must
    # match BEFORE the generic CB_TMUX_STEAL prefix.
    ((CB_TMUX_STEAL_CONFIRM,), binding_steal.handle_steal_confirm),
    ((CB_TMUX_STEAL_CANCEL,), binding_steal.handle_steal_cancel),
    ((CB_TMUX_STEAL,), binding_steal.handle_steal_select),
    (
        (CB_TMUX_SESSION_SELECT, CB_TMUX_SESSION_NEW, CB_TMUX_SESSION_CANCEL),
        binding.handle_tmux_session_callback,
    ),
    (
        (CB_TMUX_WIN_BIND, CB_TMUX_WIN_NEW, CB_TMUX_WIN_CANCEL),
        binding.handle_window_callback,
    ),
    ((CB_PERM_NORMAL, CB_PERM_SKIP), binding.handle_permission_callback),
    (
        (CB_DIR_SELECT, CB_DIR_UP, CB_DIR_CONFIRM, CB_DIR_CANCEL, CB_DIR_PAGE),
        binding.handle_directory_callback,
    ),
    ((CB_HISTORY_PREV, CB_HISTORY_NEXT), history_mod.handle_history_callback),
    (
        (
            CB_ASK_UP,
            CB_ASK_DOWN,
            CB_ASK_LEFT,
            CB_ASK_RIGHT,
            CB_ASK_ESC,
            CB_ASK_ENTER,
            CB_ASK_SPACE,
            CB_ASK_TAB,
            CB_ASK_REFRESH,
        ),
        prompt.handle_interactive_callback,
    ),
]


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route callback queries to the appropriate module via _ROUTES table."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        await query.answer("Not authorized")
        return

    data = query.data
    if data == "noop":
        await query.answer()
        return

    for prefixes, handler in _ROUTES:
        if data.startswith(prefixes):
            await handler(update, context)
            return

    await query.answer("Unknown action")


# --- App lifecycle ---


_bootstrap_backend: DefaultBackend | None = None


async def post_init(application: Application) -> None:
    """Start session monitor and status polling after bot initialization."""
    global _backend

    await application.bot.delete_my_commands()

    bot_commands = [
        BotCommand("start", "Show welcome message"),
        BotCommand("history", "Message history for this topic"),
        BotCommand("esc", "Send Escape to interrupt Claude"),
        BotCommand("unbind", "Unbind topic from session (keeps window running)"),
        BotCommand("rebind", "Unbind and pick a different session"),
        BotCommand("usage", "Show Claude Code usage remaining"),
        BotCommand("watcher", "Mark this topic as the waiting-topics dashboard"),
    ]
    for cmd_name, desc in CC_COMMANDS.items():
        bot_commands.append(BotCommand(cmd_name, desc))

    await application.bot.set_my_commands(bot_commands)

    # Re-resolve stale window IDs from persisted state against live tmux windows
    await resolve_stale_ids()

    # Pre-fill global rate limiter bucket on restart.
    rate_limiter = application.bot.rate_limiter
    if rate_limiter and rate_limiter._base_limiter:
        rate_limiter._base_limiter._level = rate_limiter._base_limiter.max_rate
        logger.info("Pre-filled global rate limiter bucket")

    # Backend is composed in main.py and passed in via create_bot(backend=...).
    # Fall back to composing one here (rare; exercised by tests that call
    # create_bot() without explicit wiring).
    # Backend is composed in main.py and passed in via create_bot(backend=...).
    # Fall back to composing one here (rare; exercised by tests that call
    # create_bot() without explicit wiring).
    backend = _bootstrap_backend
    if backend is None:
        from ccmux.api import tmux_registry

        backend = DefaultBackend(
            tmux_registry=tmux_registry,
            registry=_windows,
        )
        set_default_backend(backend)
    _backend = backend

    bot = application.bot

    async def _on_state(instance_id: str, state):  # type: ignore[no-untyped-def]
        # Delegate to status_line.on_state which matches on ClaudeState.
        await _on_state_handler(instance_id, state, bot=bot)

    async def _on_message(instance_id: str, msg):  # type: ignore[no-untyped-def]
        await handle_new_message(instance_id, msg, bot)

    await backend.start(on_state=_on_state, on_message=_on_message)
    logger.info("Backend started")

    # Watcher tick: debounced notification dispatch for the dashboard topic.
    async def _watcher_tick_loop() -> None:
        service = _watcher.get_service()
        while True:
            try:
                await service.tick(bot)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Watcher tick error: %s", e)
            await asyncio.sleep(_WATCHER_TICK_INTERVAL)

    global _watcher_tick_task
    _watcher_tick_task = asyncio.create_task(_watcher_tick_loop())
    logger.info("Watcher tick loop started (interval: %ss)", _WATCHER_TICK_INTERVAL)


async def post_shutdown(application: Application) -> None:
    """Stop polling loops, queue workers."""
    global _backend, _watcher_tick_task

    if _watcher_tick_task is not None:
        _watcher_tick_task.cancel()
        try:
            await _watcher_tick_task
        except asyncio.CancelledError:
            pass
        _watcher_tick_task = None

    if _backend is not None:
        await _backend.stop()
        _backend = None
        set_default_backend(None)

    await shutdown_workers()
    await shutdown_bash_captures()
    await close_transcribe_client()


def create_bot(backend: DefaultBackend | None = None) -> Application:
    """Build and configure the Telegram bot Application with all handlers.

    `backend` should be supplied by the composition root (`main.main`).
    When None (primarily in tests), `post_init` composes a default
    backend on-demand so the Application remains usable standalone.
    """
    global _bootstrap_backend
    _bootstrap_backend = backend

    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", commands.start_command))
    application.add_handler(CommandHandler("history", history_mod.history_command))
    application.add_handler(CommandHandler("text", commands.text_command))
    application.add_handler(CommandHandler("esc", commands.esc_command))
    application.add_handler(CommandHandler("unbind", commands.unbind_command))
    application.add_handler(CommandHandler("rebind", commands.rebind_command))
    application.add_handler(CommandHandler("usage", commands.usage_command))
    application.add_handler(CommandHandler("watcher", _watcher.watcher_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Topic closed event — auto-kill associated window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED,
            binding_lifecycle.topic_closed_handler,
        )
    )
    # Topic edited event — sync renamed topic to tmux window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_EDITED,
            binding_lifecycle.topic_edited_handler,
        )
    )
    # Forward any other /command to Claude Code
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    # Photos: download and forward file path to Claude Code
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Voice: transcribe via OpenAI and forward text to Claude Code
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    # Catch-all: non-text content (stickers, video, etc.)
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    return application
