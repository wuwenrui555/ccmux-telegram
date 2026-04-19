# ccmux-telegram

Telegram bot frontend for [`ccmux`](https://github.com/wuwenrui555/ccmux-backend) — bridges Telegram Forum topics to Claude Code sessions running in tmux.

Each topic binds to one tmux session. Messages you send in the topic go to Claude; everything Claude writes (text, thinking, tool calls, interactive prompts) streams back to the topic in real time.

This is a thin client. All the heavy lifting — tmux orchestration, JSONL parsing, session tracking — lives in the [`ccmux`](https://github.com/wuwenrui555/ccmux-backend) backend library. This repo consumes the `ccmux.api` Protocol and maps it onto the Telegram side: topic bindings, message queue, rate limiting, rendering, watcher dashboard, slash commands.

## Features

- One Telegram topic per tmux session, one tmux session per Claude instance
- Real-time relay of text, thinking, tool_use / tool_result, interactive prompts
- Inline picker flows: bind to existing tmux session, create new session, resume Claude session
- Slash commands: `/start`, `/text`, `/esc`, `/usage`, `/history`, `/unbind`, `/rebind`, `/watcher`
- Voice-message transcription (OpenAI Whisper, optional)
- Image forwarding (photos are sent to Claude as base64 attachments)
- Watcher dashboard: one topic lights up as the "who needs me" summary when others go silent
- Auto-resume of dead Claude sessions via `claude --resume`

## Install

```bash
uv add ccmux-telegram  # or: pip install ccmux-telegram
```

Configure once:

```bash
# ~/.ccmux/.env
TELEGRAM_BOT_TOKEN=your_bot_token
ALLOWED_USERS=your_telegram_user_id[,another_user_id]
OPENAI_API_KEY=...   # optional, for voice transcription
```

Then install the `ccmux hook` (from the backend package) so Claude Code's `SessionStart` populates the window map:

```bash
ccmux hook --install
```

Run the bot:

```bash
ccmux-telegram
```

## Dependency on ccmux

`ccmux_telegram` imports exclusively from `ccmux.api`. Any change to `ccmux.api` is a breaking change for this package — the `ccmux` pin in `pyproject.toml` must be a compatible version range.

## License

MIT (see `LICENSE`).
