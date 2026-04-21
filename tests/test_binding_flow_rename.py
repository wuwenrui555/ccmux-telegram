"""``_rename_topic_to_session`` swallows Telegram's no-op response.

The Bot API has no getter for a forum topic's name, so the binding
flow always calls ``edit_forum_topic``. When the topic already has the
target name, Telegram replies ``BadRequest: Topic_not_modified`` --
not a failure. Make sure that reply is silent; reserve the warning
for real failures (permissions, deleted topic, etc.).
"""

from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from ccmux_telegram.binding_flow import _rename_topic_to_session


@pytest.mark.asyncio
async def test_topic_not_modified_is_silent(caplog):
    bot = AsyncMock()
    bot.edit_forum_topic.side_effect = BadRequest("Topic_not_modified")

    with caplog.at_level("WARNING", logger="ccmux_telegram.binding_flow"):
        await _rename_topic_to_session(bot, chat_id=-100, thread_id=1, session_name="x")

    bot.edit_forum_topic.assert_awaited_once()
    # No warning-level log recorded for this case.
    assert not any("Failed to rename" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_real_bad_request_is_logged_as_warning(caplog):
    bot = AsyncMock()
    bot.edit_forum_topic.side_effect = BadRequest("Topic not found")

    with caplog.at_level("WARNING", logger="ccmux_telegram.binding_flow"):
        await _rename_topic_to_session(bot, chat_id=-100, thread_id=1, session_name="x")

    assert any(
        "Failed to rename" in r.message and "Topic not found" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_unexpected_exception_is_logged_as_warning(caplog):
    bot = AsyncMock()
    bot.edit_forum_topic.side_effect = RuntimeError("network down")

    with caplog.at_level("WARNING", logger="ccmux_telegram.binding_flow"):
        await _rename_topic_to_session(bot, chat_id=-100, thread_id=1, session_name="x")

    assert any(
        "Failed to rename" in r.message and "network down" in r.message
        for r in caplog.records
    )
