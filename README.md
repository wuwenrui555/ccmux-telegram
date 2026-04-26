# ccmux-telegram

[![CI](https://github.com/wuwenrui555/ccmux-telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/wuwenrui555/ccmux-telegram/actions/workflows/ci.yml)
[![Latest tag](https://img.shields.io/github/v/tag/wuwenrui555/ccmux-telegram)](https://github.com/wuwenrui555/ccmux-telegram/tags)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/github/license/wuwenrui555/ccmux-telegram)](LICENSE)

Telegram bot frontend for [ccmux-backend] — bridges Telegram Forum topics to Claude Code sessions running in tmux.

Each topic binds to one tmux window. Messages you send in the topic go to Claude; everything Claude writes (text, thinking, tool calls, interactive prompts) streams back to the topic in real time. This is a thin client: tmux orchestration, JSONL parsing, and session tracking all live in [ccmux-backend].

## Prerequisites

- Python ≥3.12
- [`tmux`](https://tmux.github.io/)
- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) (the `claude` CLI)
- [ccmux-backend] — pulled in transitively when you install this package; provides the `ccmux hook` CLI
- A Telegram bot token from [@BotFather](https://t.me/BotFather), in a group/supergroup with **Topics enabled**
- (Optional) An [OpenAI API key](https://platform.openai.com/api-keys) for voice-message transcription

## Commands

### Bot-owned

- `/start` — bind this topic to a session (in unbound topics) or show current binding (in bound topics)
- `/esc` — send `Escape` to interrupt Claude
- `/bar` — capture and show Claude's status bar
- `/text` — capture the current tmux pane as plain text (strips Claude's chrome)
- `/sweep` — delete this topic's bot commands and replies (tracks output-heavy commands: `/text`, `/bar`, `/history`, `/usage`, plus `/sweep` itself)
- `/history` — message history for this topic
- `/unbind` — unbind topic from session (keeps the tmux window running)
- `/rebind` — unbind and pick a different session
- `/usage` — show Claude Code usage remaining

### Forwarded to Claude Code

These commands are relayed verbatim into the tmux window via `send_keys`. The Telegram menu shows them with a leading `↗` so they read as "forwarded".

- `/clear` — clear conversation history
- `/compact` — compact conversation context
- `/cost` — show token / cost usage
- `/help` — show Claude Code help
- `/memory` — edit `CLAUDE.md`
- `/model` — switch AI model

## Usage

### 1. Install the ccmux hook

> [!NOTE]
> The `ccmux hook` CLI ships with the backend. See [GitHub - wuwenrui555/ccmux-backend](https://github.com/wuwenrui555/ccmux-backend) for what the hook does and which state files it writes.

```bash
ccmux hook --install
```

### 2. Configure

Create `~/.ccmux/.env` with at minimum:

```bash
TELEGRAM_BOT_TOKEN=your_bot_token
ALLOWED_USERS=your_telegram_user_id[,another_user_id]
OPENAI_API_KEY=...   # optional, for voice transcription
```

### 3. Run

Run the bot inside a tmux session named `__ccmux__` (or whatever `CCMUX_TMUX_SESSION_NAME` is set to). The backend skips that session when scanning for Claude windows, so the bot's own pane never shows up as a binding candidate. tmux also lets the bot survive shell disconnects.

```bash
tmux new -A -s __ccmux__   # Ctrl-B d to detach
ccmux-telegram
```

In your Telegram supergroup (with Topics enabled), create a topic and send `/start` (or any message) to trigger the session picker.

## Environment variables

Set in `$CCMUX_DIR/.env` (default `~/.ccmux/.env`) or your shell. A local `.env` in the cwd takes precedence.

### Required

- `TELEGRAM_BOT_TOKEN` — bot token from [@BotFather](https://t.me/BotFather)
- `ALLOWED_USERS` — comma-separated Telegram user IDs allowed to use the bot

### Optional

- `OPENAI_API_KEY` — enables voice-message transcription (Whisper)
- `OPENAI_BASE_URL` (default `https://api.openai.com/v1`) — override for OpenAI-compatible endpoints

### Display toggles

- `CCMUX_SHOW_TOOL_CALLS` (default `true`) — relay `tool_use` / `tool_result` blocks. Set `false` to keep only assistant text + status line.
- `CCMUX_SHOW_THINKING` (default `true`) — relay extended-thinking blocks (rendered as a `∴ Thinking…` placeholder; CC's JSONL doesn't expose the actual content). Set `false` to drop it entirely.
- `CCMUX_SHOW_SKILL_BODIES` (default `false`) — relay the full body of a `Skill` tool result. Off by default so skill invocations do not flood the chat; the `Skill(name)` tool-use summary is always shown.
- `CCMUX_TOOL_CALLS_ALLOWLIST` (default `Skill`) — comma-separated tool names whose `tool_use` / `tool_result` are forwarded even when `CCMUX_SHOW_TOOL_CALLS=false`.
- `CCMUX_SHOW_HIDDEN_DIRS` (default `false`) — show dotfile-prefixed directories in the cwd picker.
- `CCMUX_DANGEROUSLY_SKIP_PERMISSIONS` (default `false`) — pass `--dangerously-skip-permissions` to newly spawned Claude sessions.

### Backend variables

`CCMUX_DIR`, `CCMUX_TMUX_SESSION_NAME`, `CCMUX_CLAUDE_COMMAND`, `CCMUX_SHOW_USER_MESSAGES`, `CCMUX_MONITOR_POLL_INTERVAL`, and `CCMUX_CLAUDE_PROC_NAMES` are read by the backend. See [GitHub - wuwenrui555/ccmux-backend](https://github.com/wuwenrui555/ccmux-backend) for their semantics and defaults.

## State files (under `$CCMUX_DIR`, default `~/.ccmux/`)

- `topic_bindings.json` / `topic_bindings.lock` — `(user_id, thread_id) → (session_name, group_chat_id)` map persisted across restarts
- `ccmux.log` — runtime log
- `images/` — photos downloaded from Telegram before forwarding to Claude

Backend-owned files (`claude_instances.json`, `claude_monitor.json`, `drift.log`, `hook.log`, `parser_config.json`) live in the same directory. See [ccmux-backend] for those.

[ccmux-backend]: https://github.com/wuwenrui555/ccmux-backend
