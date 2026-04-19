"""Background tmux pane capture for `!`-prefixed bash commands.

Started by `text_handler` when the user sends a `!`-prefixed message; the
captured output is streamed back to the topic by editing one Telegram
message in place. Stops on cancellation (a follow-up message), on the
30 s ceiling, or on bot shutdown.

Split from `message_out.py` to keep the message handler module focused on
Telegram event routing rather than tmux-polling background work.
"""

import asyncio
import logging

from telegram import Bot

from ccmux.api import extract_bash_output, tmux_registry

from .markdown import convert_markdown
from .sender import NO_LINK_PREVIEW, send_with_fallback

logger = logging.getLogger(__name__)


# Active bash capture tasks: (user_id, thread_id) -> asyncio.Task
_bash_capture_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}


def cancel_bash_capture(user_id: int, thread_id: int) -> None:
    """Cancel any running bash capture for this topic."""
    key = (user_id, thread_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def shutdown_bash_captures() -> None:
    """Cancel every in-flight bash capture task (called from bot shutdown)."""
    if not _bash_capture_tasks:
        return
    tasks = list(_bash_capture_tasks.values())
    _bash_capture_tasks.clear()
    for t in tasks:
        if not t.done():
            t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("bash capture raised during shutdown: %s", e)


async def _capture_bash_output(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    command: str,
    chat_id: int,
) -> None:
    """Background task: capture `!` bash command output from tmux pane.

    Sends the first captured output as a new message, then edits it
    in-place as more output appears. Stops after 30 s or when cancelled
    (e.g. user sends a new message, which pushes content down).
    """
    try:
        # Wait for the command to start producing output
        await asyncio.sleep(2.0)
        msg_id: int | None = None
        last_output: str = ""

        tm = tmux_registry.get_by_window_id(window_id)
        if not tm:
            return

        for _ in range(30):
            raw = await tm.capture_pane(window_id)
            if raw is None:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Skip edit if nothing changed
            if output == last_output:
                await asyncio.sleep(1.0)
                continue

            last_output = output

            # Truncate to fit Telegram's 4096-char limit
            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                # First capture — send a new message
                sent = await send_with_fallback(
                    bot,
                    chat_id,
                    output,
                    message_thread_id=thread_id,
                )
                if sent:
                    msg_id = sent.message_id
            else:
                # Subsequent captures — edit in place
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        _bash_capture_tasks.pop((user_id, thread_id), None)


def start_bash_capture(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    command: str,
    chat_id: int,
) -> asyncio.Task[None]:
    """Spawn a `_capture_bash_output` task and register it under (user, thread)."""
    task = asyncio.create_task(
        _capture_bash_output(bot, user_id, thread_id, window_id, command, chat_id)
    )
    _bash_capture_tasks[(user_id, thread_id)] = task
    return task
