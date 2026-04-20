"""handle_permission_callback behaviour on the Normal/Skip buttons.

Regression focus: the handler previously never called `query.answer()`.
For flows that take several seconds (SessionStart hook wait, binding
writes) the Telegram client considered the button unresponsive and
re-delivered the callback. The second delivery arrived after
SESSION_NAME_KEY had been popped, so session_name silently became the
empty string and create_session crashed with BadSessionName.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_update_with_query(callback_data: str = "PERM_NORMAL"):
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.chat = MagicMock()
    query.message.chat.id = -1001
    user = MagicMock()
    user.id = 42
    update = MagicMock()
    update.callback_query = query
    update.effective_user = user
    update.effective_message = MagicMock()
    update.effective_message.message_thread_id = 7
    return update, query


def _make_context(user_data: dict | None):
    ctx = MagicMock()
    ctx.user_data = user_data
    return ctx


@pytest.mark.asyncio
async def test_answers_query_before_heavy_work() -> None:
    """`query.answer()` must fire before the async create flow.

    Without it, the Telegram client sees the button as pending and may
    redeliver the callback, causing a double-create.
    """
    from ccmux_telegram import binding_callbacks

    update, query = _make_update_with_query("PERM_NORMAL")
    ctx = _make_context(
        {
            binding_callbacks.SESSION_NAME_KEY: "test",
            binding_callbacks.SELECTED_PATH_KEY: "/tmp",
            binding_callbacks.PENDING_THREAD_ID_KEY: 7,
        }
    )

    creation_call_order: list[str] = []

    async def _fake_create(*args, **kwargs):
        creation_call_order.append("create")

    def _answer_side_effect(*args, **kwargs):
        creation_call_order.append("answer")

    query.answer.side_effect = _answer_side_effect

    with patch.object(
        binding_callbacks, "_create_session_and_bind", side_effect=_fake_create
    ):
        await binding_callbacks.handle_permission_callback(update, ctx)

    assert "answer" in creation_call_order, "query.answer() was never called"
    assert creation_call_order.index("answer") < creation_call_order.index("create"), (
        "query.answer() must precede _create_session_and_bind"
    )


@pytest.mark.asyncio
async def test_empty_session_name_short_circuits() -> None:
    """When session_name is missing (e.g. stale redelivery), do not create.

    Defence-in-depth: even with `query.answer()` in place, a user who
    double-taps the button or a delayed retransmission could still fire
    a second callback after SESSION_NAME_KEY has been popped. Refuse it.
    """
    from ccmux_telegram import binding_callbacks

    update, query = _make_update_with_query("PERM_NORMAL")
    ctx = _make_context(
        {
            # SESSION_NAME_KEY intentionally absent — simulates a stale
            # second callback arriving after the first handler popped it.
            binding_callbacks.SELECTED_PATH_KEY: "/tmp",
            binding_callbacks.PENDING_THREAD_ID_KEY: 7,
        }
    )

    with (
        patch.object(
            binding_callbacks, "_create_session_and_bind", new=AsyncMock()
        ) as create,
        patch.object(binding_callbacks, "safe_edit", new=AsyncMock()),
    ):
        await binding_callbacks.handle_permission_callback(update, ctx)

    create.assert_not_called()
    query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_empty_session_name_user_data_none_short_circuits() -> None:
    """When context.user_data is None entirely, do not create either."""
    from ccmux_telegram import binding_callbacks

    update, query = _make_update_with_query("PERM_SKIP")
    ctx = _make_context(user_data=None)

    with (
        patch.object(
            binding_callbacks, "_create_session_and_bind", new=AsyncMock()
        ) as create,
        patch.object(binding_callbacks, "safe_edit", new=AsyncMock()),
    ):
        await binding_callbacks.handle_permission_callback(update, ctx)

    create.assert_not_called()


@pytest.mark.asyncio
async def test_happy_path_still_creates() -> None:
    """With a valid SESSION_NAME_KEY the handler still dispatches creation."""
    from ccmux_telegram import binding_callbacks

    update, query = _make_update_with_query("PERM_NORMAL")
    ctx = _make_context(
        {
            binding_callbacks.SESSION_NAME_KEY: "myproj",
            binding_callbacks.SELECTED_PATH_KEY: "/tmp",
            binding_callbacks.PENDING_THREAD_ID_KEY: 7,
        }
    )

    with patch.object(
        binding_callbacks, "_create_session_and_bind", new=AsyncMock()
    ) as create:
        await binding_callbacks.handle_permission_callback(update, ctx)

    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    args = create.await_args.args
    # session_name is the 4th positional arg in current signature.
    assert "myproj" in args or kwargs.get("session_name") == "myproj"
