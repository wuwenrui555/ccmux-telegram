"""Telegram-frontend configuration.

Reads env vars and exposes a `config` singleton for Telegram / OpenAI
specific settings. Backend concerns (tmux session name, Claude paths,
JSONL monitor interval) live in `ccmux.config.config`, imported where
needed — never duplicated here.

.env loading priority: local `.env` (cwd) > `$CCMUX_DIR/.env`.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .util import ccmux_dir

logger = logging.getLogger(__name__)

SENSITIVE_ENV_VARS = {"TELEGRAM_BOT_TOKEN", "ALLOWED_USERS", "OPENAI_API_KEY"}


class Config:
    """Frontend configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccmux_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Topic bindings state
        self.state_file = self.config_dir / "topic_bindings.json"

        # Display toggles (frontend-local rendering policy).
        self.show_tool_calls = (
            os.getenv("CCMUX_SHOW_TOOL_CALLS", "true").lower() != "false"
        )
        # Extended-thinking blocks carry no readable content in current
        # Claude Code JSONL (the `thinking` field is empty, only a
        # signature remains). Leaving this on only produces a noisy
        # "∴ Thinking… (thinking)" placeholder. Default to true for
        # backward compatibility; set false to suppress.
        self.show_thinking = os.getenv("CCMUX_SHOW_THINKING", "true").lower() != "false"
        self.show_skill_bodies = (
            os.getenv("CCMUX_SHOW_SKILL_BODIES", "").lower() == "true"
        )
        self.show_hidden_dirs = (
            os.getenv("CCMUX_SHOW_HIDDEN_DIRS", "").lower() == "true"
        )
        self.dangerously_skip_permissions = (
            os.getenv("CCMUX_DANGEROUSLY_SKIP_PERMISSIONS", "").lower() == "true"
        )

        # OpenAI API for voice message transcription (optional)
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url: str = os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )

        # Scrub sensitive vars from os.environ so child processes never inherit them.
        for var in SENSITIVE_ENV_VARS:
            os.environ.pop(var, None)

        logger.debug(
            "Frontend config: dir=%s, token=%s..., allowed_users=%d",
            self.config_dir,
            self.telegram_bot_token[:8],
            len(self.allowed_users),
        )

    def is_user_allowed(self, user_id: int) -> bool:
        return user_id in self.allowed_users


config = Config()
