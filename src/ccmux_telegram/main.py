"""Application entry point — bot bootstrap.

Composes a `DefaultBackend` (from the `ccmux` backend package)
and drives the Telegram bot polling loop.

This package does **not** provide the `ccmux hook` CLI — install the
`ccmux` package (which ships `ccmux.cli:main`) alongside to get that
command.
"""

import asyncio
import logging
import sys

from .binding_health import BindingHealth, Transition

logger = logging.getLogger(__name__)


async def _run_startup_reconcile(topics, backend) -> None:
    """Best-effort pass to fix stale bindings before the bot serves.

    Silent: no Telegram messages are emitted; the user wasn't online
    to react to anything. For each unique session_name across topic
    bindings, ask the backend to reconcile and install any returned
    instance as an in-memory override.
    """
    seen: set[str] = set()
    for binding in topics.all():
        name = binding.session_name
        if name in seen:
            continue
        seen.add(name)
        try:
            inst = await backend.reconcile_instance(name)
        except Exception:
            logger.exception("startup reconcile failed for %s", name)
            continue
        if inst is not None:
            backend.claude_instances.set_override(name, inst)
            logger.info(
                "startup reconcile: %s -> %s (%s)",
                name,
                inst.window_id,
                inst.session_id,
            )


async def _binding_health_iteration(
    topics, state_cache, health: BindingHealth, bot
) -> None:
    """One pass of the binding-health detector.

    Iterates every binding, observes its current ``is_alive`` value,
    and posts ``✅ Binding to X recovered`` when the per-binding
    transition is ``RECOVERED``. ``LOST`` is intentionally not posted
    here; ``message_out.py`` already warns on the next user send.
    """
    for binding in topics.all():
        name = binding.session_name
        is_alive_now = state_cache.is_alive(name)
        t = health.observe(name, is_alive_now)
        if t is Transition.RECOVERED:
            try:
                await bot.send_message(
                    chat_id=binding.group_chat_id,
                    message_thread_id=binding.thread_id,
                    text=f"✅ Binding to `{name}` recovered.",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                logger.exception("Failed to post recovery notice for %s", name)


async def _run_binding_health_loop(
    topics, state_cache, health: BindingHealth, bot, interval: float = 0.5
) -> None:
    while True:
        try:
            await _binding_health_iteration(topics, state_cache, health, bot)
        except Exception:
            logger.exception("binding_health iteration failed")
        await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    # Import config before enabling DEBUG — avoid leaking debug logs on config errors
    try:
        from .config import config
    except ValueError as e:
        from .util import ccmux_dir

        config_dir = ccmux_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with the following content:\n")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        sys.exit(1)

    logging.getLogger("ccmux").setLevel(logging.DEBUG)
    logging.getLogger("ccmux_telegram").setLevel(logging.DEBUG)
    # AIORateLimiter (max_retries=5) handles retries itself; keep INFO for visibility
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)

    log_file = config.config_dir / "ccmux.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logging.getLogger().addHandler(file_handler)

    logger.info("Logging to %s", log_file)

    from ccmux.api import DefaultBackend, tmux_registry, set_default_backend
    from ccmux.config import config as backend_config

    from .runtime import topics, windows

    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Claude projects path: %s", backend_config.claude_projects_path)

    # Recover registry from persisted topic bindings
    bound_names = topics.all_session_names()
    for session_name in bound_names:
        tm = tmux_registry.get_or_create(session_name)
        if tm.get_session() is None:
            logger.warning(
                "Tmux session '%s' no longer exists, will clean up stale bindings",
                session_name,
            )
        else:
            logger.info("Restored TmuxSession for session '%s'", session_name)
    logger.info(
        "Registry recovered: %d sessions", len(tmux_registry.registered_session_names())
    )

    backend = DefaultBackend(tmux_registry=tmux_registry, registry=windows)
    set_default_backend(backend)

    # Startup reconcile pass: silent best-effort fix for stale bindings.
    asyncio.run(_run_startup_reconcile(topics, backend))

    logger.info("Starting Telegram bot...")
    from .bot import create_bot

    application = create_bot(backend=backend)

    # Schedule the per-binding health loop on the same event loop the
    # bot runs on. Chain onto the existing post_init/post_shutdown that
    # create_bot installed — overwriting them would break menu setup,
    # rate-limiter prefill, etc.
    binding_health = BindingHealth()
    from .state_cache import get_state_cache

    state_cache = get_state_cache()

    _existing_post_init = application.post_init
    _existing_post_shutdown = application.post_shutdown

    async def _post_init(app) -> None:
        if _existing_post_init is not None:
            await _existing_post_init(app)
        app.bot_data["_binding_health_task"] = asyncio.create_task(
            _run_binding_health_loop(topics, state_cache, binding_health, app.bot)
        )

    async def _post_shutdown(app) -> None:
        task = app.bot_data.get("_binding_health_task")
        if task is not None:
            task.cancel()
        if _existing_post_shutdown is not None:
            await _existing_post_shutdown(app)

    application.post_init = _post_init
    application.post_shutdown = _post_shutdown

    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
