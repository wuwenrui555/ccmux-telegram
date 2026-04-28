"""``/start`` triggers the binding picker in an unbound topic.

Before this change, ``/start`` only sent a welcome string and did
nothing else, so a user following Telegram convention would hit a
dead end: they had to send a separate plain-text message to enter
the picker flow. Route ``/start`` through the same handler that
plain text uses for unbound topics.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccmux_telegram.command_basic import start_command


def _mk_update(thread_id: int | None = 17451, user_id: int = 8559840605):
    update = MagicMock()
    update.message = MagicMock()
    # safe_reply tries MarkdownV2 first, then falls back to plain text;
    # both attribute calls must be awaitable.
    update.message.reply_markdown_v2 = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.message.message_thread_id = thread_id
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


@pytest.fixture
def _authorized(monkeypatch):
    """Short-circuit ``is_user_allowed`` so the @authorized decorator
    (already wrapped at import time) treats the test user as allowed."""
    from ccmux_telegram import util

    monkeypatch.setattr(util, "is_user_allowed", lambda _uid: True)


@pytest.mark.asyncio
async def test_start_in_unbound_topic_invokes_picker(_authorized):
    from ccmux_telegram import command_basic

    update = _mk_update(thread_id=17451)
    context = MagicMock()
    context.user_data = {}

    with (
        patch.object(command_basic, "get_topic", return_value=None),
        patch("ccmux_telegram.binding_flow.handle_unbound_topic", new=AsyncMock()) as h,
    ):
        await start_command(update, context)

    h.assert_awaited_once()
    args, kwargs = h.call_args
    # handle_unbound_topic(update, context, user, thread_id, text)
    assert args[3] == 17451
    assert kwargs.get("text") == "" or (len(args) >= 5 and args[4] == "")


@pytest.mark.asyncio
async def test_start_in_bound_topic_shows_session_info(_authorized):
    """When the topic is already bound, ``/start`` shows the bound
    session name and the three commands the user might want next —
    never re-enters the picker, never shows the generic welcome."""
    from ccmux_telegram import command_basic

    update = _mk_update(thread_id=17451)
    context = MagicMock()
    context.user_data = {}
    fake_topic = MagicMock()
    fake_topic.session_name = "daily"

    with (
        patch.object(command_basic, "get_topic", return_value=fake_topic),
        patch("ccmux_telegram.binding_flow.handle_unbound_topic", new=AsyncMock()) as h,
        patch.object(command_basic, "safe_reply", new=AsyncMock()) as reply,
    ):
        await start_command(update, context)

    h.assert_not_awaited()
    reply.assert_awaited_once()
    body = reply.call_args.args[1]
    assert "`daily`" in body
    assert "/rebind_topic" in body
    assert "/history" in body
    assert "/unbind" in body
    # The generic "Create a new topic" text is for unbound / no-thread
    # contexts only; must not appear here.
    assert "Create a new topic" not in body


@pytest.mark.asyncio
async def test_start_without_thread_id_shows_welcome(_authorized):
    """No forum topic context (private chat) → fall back to welcome."""
    from ccmux_telegram import command_basic

    update = _mk_update(thread_id=None)
    context = MagicMock()
    context.user_data = {}

    with (
        patch.object(command_basic, "get_topic", return_value=None),
        patch("ccmux_telegram.binding_flow.handle_unbound_topic", new=AsyncMock()) as h,
        patch.object(command_basic, "safe_reply", new=AsyncMock()) as reply,
    ):
        await start_command(update, context)

    h.assert_not_awaited()
    reply.assert_awaited_once()
