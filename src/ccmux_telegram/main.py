"""Application entry point — bot bootstrap.

Composes a `DefaultBackend` (from the `ccmux` backend package)
and drives the Telegram bot polling loop.

This package does **not** provide the `ccmux hook` CLI — install the
`ccmux` package (which ships `ccmux.cli:main`) alongside to get that
command.
"""

import logging
import sys


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

    logger = logging.getLogger(__name__)
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

    backend = DefaultBackend(tmux_registry=tmux_registry, window_bindings=windows)
    set_default_backend(backend)

    logger.info("Starting Telegram bot...")
    from .bot import create_bot

    application = create_bot(backend=backend)
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
