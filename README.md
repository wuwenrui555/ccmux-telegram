# ccmux-telegram

Telegram bot frontend for [`ccmux`](https://github.com/wuwenrui555/ccmux-backend) — bridges Telegram Forum topics to Claude Code sessions running in tmux.

Each topic binds to one tmux session. Messages you send in the topic go to Claude; everything Claude writes (text, thinking, tool calls, interactive prompts) streams back to the topic in real time.

This is a thin client. All the heavy lifting — tmux orchestration, JSONL parsing, session tracking — lives in the [`ccmux`](https://github.com/wuwenrui555/ccmux-backend) backend library. This repo consumes the `ccmux.api` Protocol and maps it onto the Telegram side: topic bindings, message queue, rate limiting, rendering, watcher dashboard, slash commands.

## Features

- One Telegram topic per tmux session, one tmux session per Claude instance
- Real-time relay of text, thinking, tool_use / tool_result, interactive prompts (permission prompts include the pending tool's file path, diff, or command inline so you can decide without scrolling back)
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

## Display toggles

Environment variables that control how much noise the bot pushes to Telegram. All are read from `~/.ccmux/.env` on startup.

- `CCMUX_SHOW_TOOL_CALLS` (default `true`) — relay `tool_use` / `tool_result` blocks (Read, Edit, Bash, …). Set `false` to keep only assistant text + status line.
- `CCMUX_SHOW_THINKING` (default `true`) — relay extended-thinking blocks. Note that current Claude Code JSONL stores only a signature for these (the content is omitted upstream), so the bot can only render a `∴ Thinking… (thinking)` placeholder. Set `false` to drop it entirely.
- `CCMUX_SHOW_SKILL_BODIES` (default `false`) — relay the full body of a `Skill` tool result. Off by default so skill invocations do not flood the chat; the `Skill(name)` tool-use summary is always shown. Set `true` to get the full body.
- `CCMUX_SHOW_HIDDEN_DIRS` (default `false`) — show dotfile-prefixed directories in the cwd picker.
- `CCMUX_DANGEROUSLY_SKIP_PERMISSIONS` (default `false`) — pass `--dangerously-skip-permissions` to newly spawned Claude sessions.
- `CCMUX_SHOW_USER_MESSAGES` (default `true`, backend env) — echo your own messages back into the topic. Set `false` if you find the 👤-prefixed echo redundant.

## Dependency on ccmux

`ccmux_telegram` imports exclusively from `ccmux.api`. Any change to `ccmux.api` is a breaking change for this package — the `ccmux` pin in `pyproject.toml` must be a compatible version range.

## License

MIT (see `LICENSE`).
