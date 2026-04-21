"""Tests for message_dispatch — state-gated send to Claude Code."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccmux.api import Blocked, BlockedUI, Dead, Idle, Working

from ccmux_telegram import message_dispatch as md


@pytest.fixture(autouse=True)
def _clear_pending():
    """Each test starts with an empty pending buffer."""
    md._pending_clear_all()
    yield
    md._pending_clear_all()


def _make_topic(session_name: str = "session-a"):
    topic = MagicMock()
    topic.session_name = session_name
    topic.group_chat_id = 1000
    topic.window_id = "@1"
    return topic


async def _dispatch(bot, window_id="@1", text="hi", chat_id=1000, message_id=42):
    return await md.dispatch_text(
        bot=bot,
        chat_id=chat_id,
        message_id=message_id,
        window_id=window_id,
        text=text,
    )


@pytest.mark.asyncio
async def test_idle_sends_immediately_and_reacts_sent():
    bot = MagicMock()
    bot.set_message_reaction = AsyncMock()
    fake_backend = MagicMock()
    fake_backend.tmux.send_text = AsyncMock(return_value=(True, ""))

    with (
        patch.object(md, "get_default_backend", return_value=fake_backend),
        patch.object(md, "get_topic_by_window_id", return_value=_make_topic()),
        patch.object(md, "get_state_cache") as mock_cache,
    ):
        mock_cache.return_value.get.return_value = Idle()

        ok, _ = await _dispatch(bot)

        assert ok
        fake_backend.tmux.send_text.assert_awaited_once_with("@1", "hi")
        assert md._pending_snapshot("@1") == []
        # sent reaction applied
        bot.set_message_reaction.assert_awaited_once()
        args = bot.set_message_reaction.call_args
        assert args.kwargs["reaction"][0].emoji == md._REACTION_SENT


@pytest.mark.asyncio
async def test_working_pends_and_reacts_pending():
    bot = MagicMock()
    bot.set_message_reaction = AsyncMock()
    fake_backend = MagicMock()
    fake_backend.tmux.send_text = AsyncMock(return_value=(True, ""))

    with (
        patch.object(md, "get_default_backend", return_value=fake_backend),
        patch.object(md, "get_topic_by_window_id", return_value=_make_topic()),
        patch.object(md, "get_state_cache") as mock_cache,
    ):
        mock_cache.return_value.get.return_value = Working(status_text="Running…")

        ok, _ = await _dispatch(bot, text="one")

        assert ok
        # did not send through the backend
        fake_backend.tmux.send_text.assert_not_awaited()
        # pending buffer got the item
        snapshot = md._pending_snapshot("@1")
        assert len(snapshot) == 1
        assert snapshot[0].text == "one"
        # pending reaction applied
        bot.set_message_reaction.assert_awaited_once()
        args = bot.set_message_reaction.call_args
        assert args.kwargs["reaction"][0].emoji == md._REACTION_PENDING


@pytest.mark.asyncio
async def test_blocked_also_pends():
    bot = MagicMock()
    bot.set_message_reaction = AsyncMock()
    fake_backend = MagicMock()
    fake_backend.tmux.send_text = AsyncMock(return_value=(True, ""))

    with (
        patch.object(md, "get_default_backend", return_value=fake_backend),
        patch.object(md, "get_topic_by_window_id", return_value=_make_topic()),
        patch.object(md, "get_state_cache") as mock_cache,
    ):
        mock_cache.return_value.get.return_value = Blocked(
            ui=BlockedUI.PERMISSION_PROMPT, content="Do you…"
        )

        ok, _ = await _dispatch(bot)

        assert ok
        fake_backend.tmux.send_text.assert_not_awaited()
        assert len(md._pending_snapshot("@1")) == 1


@pytest.mark.asyncio
async def test_dead_rejects_with_error_message():
    bot = MagicMock()
    bot.set_message_reaction = AsyncMock()
    fake_backend = MagicMock()
    fake_backend.tmux.send_text = AsyncMock(return_value=(True, ""))

    with (
        patch.object(md, "get_default_backend", return_value=fake_backend),
        patch.object(md, "get_topic_by_window_id", return_value=_make_topic()),
        patch.object(md, "get_state_cache") as mock_cache,
    ):
        mock_cache.return_value.get.return_value = Dead()

        ok, err = await _dispatch(bot)

        assert not ok
        assert "Claude" in err
        fake_backend.tmux.send_text.assert_not_awaited()
        assert md._pending_snapshot("@1") == []
        # No reaction on dead-reject: the caller surfaces a text error.
        bot.set_message_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_state_sends_immediately():
    """No state cached for the window: fall through to send-now so the
    first message ever sent to a fresh binding does not hang."""
    bot = MagicMock()
    bot.set_message_reaction = AsyncMock()
    fake_backend = MagicMock()
    fake_backend.tmux.send_text = AsyncMock(return_value=(True, ""))

    with (
        patch.object(md, "get_default_backend", return_value=fake_backend),
        patch.object(md, "get_topic_by_window_id", return_value=_make_topic()),
        patch.object(md, "get_state_cache") as mock_cache,
    ):
        mock_cache.return_value.get.return_value = None

        ok, _ = await _dispatch(bot)

        assert ok
        fake_backend.tmux.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_drain_flushes_pending_in_fifo_order():
    bot = MagicMock()
    bot.set_message_reaction = AsyncMock()
    fake_backend = MagicMock()
    fake_backend.tmux.send_text = AsyncMock(return_value=(True, ""))

    with (
        patch.object(md, "get_default_backend", return_value=fake_backend),
        patch.object(md, "get_topic_by_window_id", return_value=_make_topic()),
        patch.object(md, "get_state_cache") as mock_cache,
    ):
        mock_cache.return_value.get.return_value = Working(status_text="Running…")
        await _dispatch(bot, text="first", message_id=1)
        await _dispatch(bot, text="second", message_id=2)
        await _dispatch(bot, text="third", message_id=3)

    assert len(md._pending_snapshot("@1")) == 3

    # Now drain — backend should receive all three in order.
    with patch.object(md, "get_default_backend", return_value=fake_backend):
        await md.drain_for_window(bot, "@1")

    sent_calls = fake_backend.tmux.send_text.await_args_list
    assert [c.args[1] for c in sent_calls] == ["first", "second", "third"]
    assert md._pending_snapshot("@1") == []


@pytest.mark.asyncio
async def test_drain_on_empty_queue_is_noop():
    bot = MagicMock()
    fake_backend = MagicMock()
    fake_backend.tmux.send_text = AsyncMock()

    with patch.object(md, "get_default_backend", return_value=fake_backend):
        await md.drain_for_window(bot, "@nonexistent")

    fake_backend.tmux.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaction_failure_does_not_break_send():
    """A broken set_message_reaction must not propagate — reactions
    are a nice-to-have, the send path is primary."""
    bot = MagicMock()
    bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("no permission"))
    fake_backend = MagicMock()
    fake_backend.tmux.send_text = AsyncMock(return_value=(True, ""))

    with (
        patch.object(md, "get_default_backend", return_value=fake_backend),
        patch.object(md, "get_topic_by_window_id", return_value=_make_topic()),
        patch.object(md, "get_state_cache") as mock_cache,
    ):
        mock_cache.return_value.get.return_value = Idle()
        ok, _ = await _dispatch(bot)

    assert ok
    fake_backend.tmux.send_text.assert_awaited_once()
