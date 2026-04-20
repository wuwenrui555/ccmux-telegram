"""Tests for ccmux.util: ccmux_dir, atomic_write_json."""

import json
from pathlib import Path

import pytest

from ccmux_telegram.util import atomic_write_json, ccmux_dir


class TestCcmuxDir:
    def test_returns_env_var_path(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CCMUX_DIR", "/custom/config")
        assert ccmux_dir() == Path("/custom/config")

    def test_returns_default_without_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CCMUX_DIR", raising=False)
        assert ccmux_dir() == Path.home() / ".ccmux"


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path: Path):
        target = tmp_path / "data.json"
        atomic_write_json(target, {"key": "value"})
        result = json.loads(target.read_text(encoding="utf-8"))
        assert result == {"key": "value"}

    def test_creates_parent_directories(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "c" / "data.json"
        atomic_write_json(target, [1, 2, 3])
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == [1, 2, 3]

    def test_round_trip(self, tmp_path: Path):
        data = {"users": [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]}
        target = tmp_path / "round_trip.json"
        atomic_write_json(target, data)
        assert json.loads(target.read_text(encoding="utf-8")) == data

    def test_no_temp_files_left_on_success(self, tmp_path: Path):
        target = tmp_path / "clean.json"
        atomic_write_json(target, {"ok": True})
        remaining = list(tmp_path.glob(".*tmp*"))
        assert remaining == []
