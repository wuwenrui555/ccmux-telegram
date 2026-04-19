"""Unit tests for Config — env var loading, validation, and user access."""

import pytest

from ccmux_telegram.config import Config


@pytest.fixture
def _base_env(monkeypatch, tmp_path):
    # chdir to tmp_path so load_dotenv won't find the real .env in repo root
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    monkeypatch.setenv("ALLOWED_USERS", "12345")
    monkeypatch.setenv("CCMUX_DIR", str(tmp_path))


@pytest.mark.usefixtures("_base_env")
class TestConfigValid:
    def test_valid_config(self):
        cfg = Config()
        assert cfg.telegram_bot_token == "test:token"
        assert cfg.allowed_users == {12345}

    def test_is_user_allowed_true(self):
        cfg = Config()
        assert cfg.is_user_allowed(12345) is True

    def test_is_user_allowed_false(self):
        cfg = Config()
        assert cfg.is_user_allowed(99999) is False


@pytest.mark.usefixtures("_base_env")
class TestConfigMissingEnv:
    def test_missing_telegram_bot_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            Config()

    def test_missing_allowed_users(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
        with pytest.raises(ValueError, match="ALLOWED_USERS"):
            Config()

    def test_non_numeric_allowed_users(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_USERS", "abc")
        with pytest.raises(ValueError, match="non-numeric"):
            Config()


@pytest.mark.usefixtures("_base_env")
class TestConfigOpenAI:
    def test_openai_defaults(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        cfg = Config()
        assert cfg.openai_api_key == ""
        assert cfg.openai_base_url == "https://api.openai.com/v1"

    def test_openai_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        cfg = Config()
        assert cfg.openai_api_key == "sk-test-123"

    def test_openai_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example.com/v1")
        cfg = Config()
        assert cfg.openai_base_url == "https://proxy.example.com/v1"

    def test_openai_api_key_scrubbed_from_env(self, monkeypatch):
        import os

        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        Config()
        assert os.environ.get("OPENAI_API_KEY") is None
