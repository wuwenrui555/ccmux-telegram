"""Startup reconcile pass: silent best-effort fix for known bindings."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccmux.api import ClaudeInstance


@pytest.mark.asyncio
async def test_startup_pass_calls_reconcile_for_each_unique_session() -> None:
    from ccmux_telegram.main import _run_startup_reconcile

    bindings = [
        MagicMock(session_name="ccmux"),
        MagicMock(session_name="outlook"),
        MagicMock(session_name="ccmux"),  # dup, must dedupe
    ]
    fake_topics = MagicMock()
    fake_topics.all = MagicMock(return_value=bindings)

    fake_inst = ClaudeInstance("ccmux", "@22", "sid", "/")
    fake_backend = MagicMock()
    fake_backend.reconcile_instance = AsyncMock(return_value=fake_inst)
    fake_backend.claude_instances = MagicMock()
    fake_backend.claude_instances.set_override = MagicMock()

    await _run_startup_reconcile(fake_topics, fake_backend)

    # Called twice — once for each unique session name.
    assert fake_backend.reconcile_instance.await_count == 2
    # Override applied for both successful results.
    assert fake_backend.claude_instances.set_override.call_count == 2


@pytest.mark.asyncio
async def test_startup_pass_skips_set_override_when_reconcile_returns_none() -> None:
    from ccmux_telegram.main import _run_startup_reconcile

    bindings = [MagicMock(session_name="dead-session")]
    fake_topics = MagicMock()
    fake_topics.all = MagicMock(return_value=bindings)

    fake_backend = MagicMock()
    fake_backend.reconcile_instance = AsyncMock(return_value=None)
    fake_backend.claude_instances = MagicMock()
    fake_backend.claude_instances.set_override = MagicMock()

    await _run_startup_reconcile(fake_topics, fake_backend)

    fake_backend.reconcile_instance.assert_awaited_once_with("dead-session")
    fake_backend.claude_instances.set_override.assert_not_called()
