# Changelog

All notable changes to `ccmux-telegram` are documented here. Versions
are aligned with the backend `ccmux` library: a frontend 1.x release
depends on backend 1.x.

## [Unreleased]

## 2.0.0 — 2026-04-20

### Changed

- Upgraded to ccmux-backend v2.0.0 (required; v1.x is incompatible).
- Consumers migrated to the new `on_state` / `on_message` dual
  callback:
  - `status_line.consume_statuses` replaced by
    `status_line.on_state(instance_id, ClaudeState, *, bot=)`.
  - `watcher.classify` now takes a `ClaudeState` and returns
    `working | waiting | resuming`.
  - `topic_bindings.is_alive` reads from the new frontend-side
    `StateCache` instead of `backend.is_alive`.
- `tool_context._resolve_jsonl_path` uses the new
  `ClaudeInstanceRegistry.get_by_window_id` lookup.
- `bot.py::post_init` invokes `backend.start(on_state=..., on_message=...)`.
- Renames carried over from the backend:
  `WindowBinding` → `ClaudeInstance`,
  `WindowBindings` → `ClaudeInstanceRegistry`,
  `claude_session_id` → `session_id` field.

### Removed

- Every reference to `WindowStatus`, the old `PaneState` StrEnum,
  `WindowBinding`, `WindowBindings`, `Backend.is_alive`,
  `Backend.get_window_binding`, and the two-callback
  `start(on_message, on_status)` signature.

### Note on persistence

ccmux-backend changes `$CCMUX_DIR/window_bindings.json` to
`$CCMUX_DIR/claude_instances.json` with no migration. After upgrading,
existing users must re-bind their Claude sessions.

## 1.2.1 — 2026-04-20

### Changed

- Apply `ruff format` across `src/` and `tests/`. v1.2.0 landed on
  `main` with 3 files flagged by the CI `ruff format --check` step;
  this hotfix reformats them so CI on `main` is green again. No
  behaviour change.

## 1.2.0 — 2026-04-20

### Added

- `CCMUX_TOOL_CALLS_ALLOWLIST` env var (default `"Skill"`, comma
  separated). Tools on the list bypass `CCMUX_SHOW_TOOL_CALLS=false`,
  so users who keep tool-call notifications off can still see a
  one-line `**Skill**(name)` signal for explicit skill invocations.
- `CCMUX_SHOW_SKILL_BODIES` env flag (default `false`) that suppresses
  `Skill` tool_result bodies so Skill invocations do not flood the chat.
  The `Skill(name)` tool-use summary is always shown; only the full body
  is gated. Set `true` to get the full body back.
- Permission-prompt and bash-approval Telegram messages now include the
  pending tool's input (file path, unified diff, command, or key/value
  dump) inline, fetched from the session JSONL on demand. Users can
  approve or reject without scrolling back for context.
- `claude_trust.mark_dir_trusted` — pre-sets the target directory's
  `hasTrustDialogAccepted` flag in `~/.claude.json` before launching
  Claude in a newly-created session. Without this, Claude blocks on
  the "Trust this folder?" dialog, the `SessionStart` hook never
  fires, and the bound topic ends up stuck on "has no window yet".
  Write is atomic (tmp + rename) and best-effort: failures fall
  through to the old behaviour.

### Fixed

- Claude Code injects the full skill body as a plain user-role text
  block (starting with `"Base directory for this skill:"`) rather than
  as a tool_result, so the tool_result-based gate never caught it.
  Suppress the injected body under `CCMUX_SHOW_SKILL_BODIES`.
- `handle_permission_callback` now acks the Telegram callback up
  front. The downstream create flow can take several seconds
  (SessionStart hook wait, bindings write) and during that time the
  Telegram client still shows the button as pending. After the
  ~15-second callback window the client (or an impatient user)
  redelivered the same callback, producing a duplicate handler
  invocation that crashed on the now-empty `SESSION_NAME_KEY`.
- `handle_permission_callback` atomically claims `SESSION_NAME_KEY`
  with a single `pop`. Stale duplicate deliveries get `None` and
  return silently instead of overwriting the first handler's success
  UI with a "session name missing" warning.

## 1.1.0 — 2026-04-19

### Added

- `STATUS_MIN_INTERVAL` (env: `CCMUX_STATUS_MIN_INTERVAL`, default
  `5.0` seconds) — producer-side throttle in `enqueue_status_update`.
  Caps status text updates to one edit per interval per (user, thread),
  so the ticking "Computing… (Ns)" counter no longer consumes one
  Telegram edit per second. Set to `0` to disable.

### Changed

- `status_line._consume_one` no longer suppresses status updates when
  the content queue has pending work. The producer-side throttle keeps
  edit volume bounded while restoring the "Claude is alive" signal
  during active tool-call bursts. Previously status was dropped
  entirely whenever content was flowing.
- `status_clear` tasks are not throttled and now reset the throttle
  cursor, so the first status of the next thinking burst lands
  immediately rather than waiting out the 5-second window.

## 1.0.1 — 2026-04-19

### Fixed

- CI test collection failed because `ccmux_telegram.config` instantiates
  `Config()` at module import and raises when `TELEGRAM_BOT_TOKEN` /
  `ALLOWED_USERS` are missing. `tests/conftest.py` now seeds fake
  values at module load so pytest can collect and run without a real
  bot token. Local dev still uses the real token from the shell env or
  `.env` (conftest uses `os.environ.setdefault`, does not overwrite).

## 1.0.0 — 2026-04-19

First stable release, paired with `ccmux` 1.0.0.

### Changed (breaking)

- Consumes the renamed `ccmux.api` surface: `Backend` / `DefaultBackend`
  instead of `ClaudeBackend` / `DefaultClaudeBackend`,
  `TmuxSessionRegistry` instead of `TmuxManagerRegistry`,
  `WindowBindings` instead of `WindowRegistry`.
- `ccmux-telegram.markdown` no longer relies on
  `TranscriptParser.EXPANDABLE_QUOTE_*` sentinel tokens (removed in
  backend v1.0). Collapsible regions are detected from **standard
  Markdown blockquotes** (`>` lines) emitted by the backend and
  rendered as Telegram expandable blockquotes (`**>…||`). Code fences
  are respected — `>` inside a fenced block is not treated as a
  blockquote.
- `sender.strip_sentinels` removed. Plain-text fallback no longer needs
  post-processing; the backend's `>` output is already human-readable.
- `message_in.py` replaces sentinel-based atomicity checks with
  blockquote detection (`_is_blockquote_only` / `_strip_blockquote` /
  `_as_blockquote`).
- `status_monitor` integration points now pass a `TmuxSessionRegistry`
  explicitly (see backend v1.0 notes) instead of relying on the old
  module-level `tmux_registry` import. Tests updated accordingly.

### Added

- `CCMUX_SHOW_THINKING` env toggle (default `true`). Set `false` to
  drop `∴ Thinking…` messages — useful because current Claude Code
  JSONL records only a signature for extended-thinking blocks, so the
  rendered message is a content-less placeholder anyway.

### Dependencies

- `ccmux` pinned to `>=1.0.0,<2.0.0`.

### Notes

- Backend version alignment is intentional: each frontend major follows
  the backend major. Minor releases on either side are compatible as
  long as the `ccmux.api` surface doesn't break.
