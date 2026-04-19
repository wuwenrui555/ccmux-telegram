# Changelog

All notable changes to `ccmux-telegram` are documented here. Versions
are aligned with the backend `ccmux` library: a frontend 1.x release
depends on backend 1.x.

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
