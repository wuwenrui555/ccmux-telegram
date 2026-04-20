# Changelog

All notable changes to `ccmux-telegram` are documented here. Versions
are aligned with the backend `ccmux` library: a frontend 1.x release
depends on backend 1.x.

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
  Markdown blockquotes** (`> ` lines) emitted by the backend and
  rendered as Telegram expandable blockquotes (`**>…||`). Code fences
  are respected — `>` inside a fenced block is not treated as a
  blockquote.
- `sender.strip_sentinels` removed. Plain-text fallback no longer needs
  post-processing; the backend's `> ` output is already human-readable.
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
