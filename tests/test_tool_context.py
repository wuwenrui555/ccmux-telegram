"""Tests for tool_context — pending tool_use cache, TTL, and formatters."""

import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_pending():
    from ccmux_telegram import tool_context

    tool_context._PENDING.clear()
    yield
    tool_context._PENDING.clear()


def _entry(tool_name="Edit", tool_use_id="t1", input_data=None, recorded_at=None):
    from ccmux_telegram.tool_context import PendingToolContext

    return PendingToolContext(
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        input=input_data if input_data is not None else {"file_path": "/a.py"},
        recorded_at=recorded_at if recorded_at is not None else time.monotonic(),
    )


class TestCacheLifecycle:
    def test_record_and_get(self):
        from ccmux_telegram import tool_context

        e = _entry()
        tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        ).append(e)
        assert tool_context.get_pending("@1") is e

    def test_get_returns_none_when_absent(self):
        from ccmux_telegram import tool_context

        assert tool_context.get_pending("@missing") is None

    def test_clear_removes_matching_tool_use_id(self):
        from ccmux_telegram import tool_context

        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        dq.append(_entry(tool_use_id="t1"))
        dq.append(_entry(tool_use_id="t2"))
        tool_context.clear("@1", "t1")
        remaining = [e.tool_use_id for e in tool_context._PENDING["@1"]]
        assert remaining == ["t2"]

    def test_clear_noop_when_id_unknown(self):
        from ccmux_telegram import tool_context

        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        dq.append(_entry(tool_use_id="t1"))
        tool_context.clear("@1", "t-missing")
        assert len(tool_context._PENDING["@1"]) == 1

    def test_ttl_expiry_drops_stale_on_get(self):
        from ccmux_telegram import tool_context

        old = _entry(recorded_at=time.monotonic() - (tool_context._TTL_SECONDS + 5))
        fresh = _entry(tool_use_id="t2")
        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        dq.append(old)
        dq.append(fresh)
        result = tool_context.get_pending("@1")
        assert result is fresh

    def test_get_returns_newest_of_multiple_fresh(self):
        from ccmux_telegram import tool_context

        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        dq.append(_entry(tool_use_id="t1"))
        dq.append(_entry(tool_use_id="t2"))
        result = tool_context.get_pending("@1")
        assert result.tool_use_id == "t2"

    def test_cap_evicts_oldest(self):
        from ccmux_telegram import tool_context

        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        total = tool_context._MAX_PENDING + 2
        for i in range(total):
            dq.append(_entry(tool_use_id=f"t{i}"))
        ids = [e.tool_use_id for e in dq]
        assert len(ids) == tool_context._MAX_PENDING
        assert ids == [f"t{i}" for i in range(2, total)]


class TestRecordAllStale:
    def test_get_returns_none_when_all_stale(self):
        from ccmux_telegram import tool_context

        old = _entry(recorded_at=time.monotonic() - (tool_context._TTL_SECONDS + 5))
        dq = tool_context._PENDING.setdefault(
            "@1",
            tool_context.collections.deque(maxlen=tool_context._MAX_PENDING),
        )
        dq.append(old)
        assert tool_context.get_pending("@1") is None
