# Changelog

<!-- markdownlint-disable MD024 MD046 -->

All notable changes to `ccmux-telegram` are documented here. Versions
are aligned with the backend `ccmux` library: a frontend 1.x release
depends on backend 1.x.

## [Unreleased]

## 4.1.1 — 2026-04-28

### Changed

- Topic-status banners switch from ``✅`` / ``⚠️`` to ``🟢`` /
  ``🔴``. The check / warning glyphs render too small to be
  legible in Telegram clients' topic-list rows; the colored
  circles are substantially more visible. No code-shape change.

## 4.1.0 — 2026-04-28

### Changed (BREAKING for the topic UX)

- **Status is shown by topic name, not by chat messages.** The
  ``✅ Binding to <name> recovered`` notification posted on every
  RECOVERED transition is removed. Instead, the bot renames each
  bound forum topic to a structured banner reflecting current
  status:

  ```text
  ✅ | <tmux_session_name> (<window_id>)    -- alive
  ⚠️ | <tmux_session_name> (<window_id>)    -- not alive
  ```

  The desired name is recomputed every binding-health tick (~0.5 s)
  from ``state_cache.is_alive(name)`` plus the current
  ``window_id`` from the event-log reader. An in-memory cache
  suppresses redundant ``edit_forum_topic`` calls; only a name that
  differs from the cache hits the API. ``BadRequest:
  Topic_not_modified`` is silenced and treated as a successful
  render.

  Telegram's ``BadRequest: message thread not found`` continues to
  trigger the v4.0.1 auto-unbind path.

### Removed

- ``ccmux_telegram.binding_health`` module
  (``BindingHealth`` / ``Transition``). With the rename approach,
  per-binding alive transitions are no longer tracked separately —
  the desired-name diff against the in-memory cache is the trigger.
- ``main._binding_health_iteration`` /
  ``_run_binding_health_loop``. Replaced by
  ``_topic_status_iteration`` / ``_run_topic_status_loop``.
- ``test_binding_health.py``, ``test_binding_health_loop.py``.

### Added

- ``ccmux_telegram.topic_rename`` module: ``desired_topic_name``
  pure function and ``TopicRenamer`` class. 14 new tests
  (``test_topic_rename.py``).

### Migration

Nothing for the user. On bot start the next iteration's rename pass
formats every bound topic to the new banner. Topics where the user
manually set a custom name will be overwritten — that is
intentional, since the format is now the authoritative source of
status.

## 4.0.1 — 2026-04-28

### Added

- Auto-unbind on deleted topics. When Telegram returns
  ``BadRequest: message thread not found`` (the only signal that a
  forum topic was deleted — there is no ``forum_topic_deleted``
  update), the bot now drops the matching ``topic_bindings.json``
  row instead of retrying forever. Wired into:
  - the per-topic message queue worker (covers status updates and
    Claude content; on auto-unbind the worker also drains any
    queued tasks for that thread)
  - ``sender.safe_send``
  - ``main._binding_health_iteration`` (the ``✅ Binding ...
    recovered`` notice)
- ``TopicBindings.unbind_by_thread(group_chat_id, thread_id)``
  helper used by the above.
- ``ccmux_telegram.auto_unbind`` module: ``is_thread_deleted_error``
  predicate + ``maybe_unbind`` helper. Match is narrow — other
  ``BadRequest`` variants (``Chat not found``, ``Topic_not_modified``,
  etc.) and non-``BadRequest`` errors are passthroughs.

### Why

Workflow was: in the Telegram client, run ``/unbind`` first, then
delete the topic. Skipping the ``/unbind`` left a stale row in
``topic_bindings.json`` and the bot spent forever retrying outbound
messages to a deleted thread. Now the lazy cleanup catches it on
the first send failure.

## 4.0.0 — 2026-04-28

Frontend migrates to the v4.0.0 event-log API of `ccmux-backend`.
The v3.1 override layer plumbing is gone; the backend's
`EventLogReader` is the single source of truth for "which Claude
window is in which tmux session". Self-heals on every user prompt.

### Changed (BREAKING)

- Requires `ccmux>=4.0.0,<5.0.0`. Earlier backends are not
  supported (the symbols this frontend used to consume —
  `ClaudeInstanceRegistry`, `reconcile_instance`, `set_override` —
  are deleted in `ccmux` v4.0.0).

- `/rebind_window` command removed (no alias). The reader auto-
  refreshes the binding on every `UserPromptSubmit` hook fire, so
  a manual refresh is no longer meaningful. Use `/rebind_topic` to
  switch a topic to a different tmux session.

- Startup reconcile pass removed from `main.py`. The reader's own
  `start()` does the initial full read of
  `~/.ccmux/claude_events.jsonl`.

### Removed

- `/rebind_window` handler, BotCommand entry, and registration.
- All `set_override` / `clear_override` / `reconcile_instance`
  call sites across `binding_callbacks.py`, `command_basic.py`,
  and `main.py`.
- `runtime.windows` (a `ClaudeInstanceRegistry` singleton) →
  replaced by `runtime.event_reader` (an `EventLogReader`).

### Migration

1. Upgrade `ccmux-backend` to `>=4.0.0` first; run
   `ccmux hook --install` so `~/.claude/settings.json` registers
   the new `UserPromptSubmit` handler.
2. Restart `ccmux-telegram`. On first start the reader does an
   initial read of `claude_events.jsonl`. Existing
   `topic_bindings.json` entries are preserved and continue
   pointing at the same tmux session names; routing now goes
   through the reader rather than the deleted registry.
3. Already-running Claude sessions need to `/clear`, exit, or
   auto-resume for their next hook event to populate the log.

## 3.2.0 — 2026-04-27

### Added

- `/rebind_window` command. Refreshes the binding's window mapping by
  asking the backend's `reconcile_instance` for the live Claude
  window in the bound tmux session. Replies `✅ Refreshed binding:
  <session> → <window_id>` on success, `⚠️ Session has no live
  Claude` when the session is empty, or `❌ No session bound here`
  in an unbound topic.
- Startup reconcile pass. On bot start, every unique `session_name`
  in `topic_bindings.json` is reconciled once and any returned
  instance is installed as an in-memory override on
  `ClaudeInstanceRegistry`. Silent — no Telegram messages are sent
  while the bot is offline.
- Periodic binding-health detector. Background asyncio task at 0.5 s
  cadence observes `state_cache.is_alive` per binding and, on a
  false → true transition for any cause (manual edit of
  `claude_instances.json`, `/rebind_window`, future hook fires),
  posts `✅ Binding to <session> recovered.` to the bound topic.
  Per-binding `LOST` is intentionally not posted — the existing
  ⚠️ on next user send already covers that.
- New module `ccmux_telegram.binding_health` with `BindingHealth` and
  `Transition` (`STABLE` / `RECOVERED` / `LOST`).

### Changed

- The "binding not alive" warning now points at `/rebind_window` to
  refresh the window mapping and `/rebind_topic` to switch sessions
  (was: "Use /rebind to reconnect").
- `pyproject.toml` requires `ccmux>=3.1.1,<4.0.0` for the new
  `Backend.reconcile_instance` API, the `ClaudeInstanceRegistry`
  override layer, the `claude_instances` accessor, and the
  cross-platform mtime-based `pid_session_resolver` fix.

### Removed

- `/rebind` is gone. Use `/rebind_topic` to swap which tmux session a
  topic talks to. Use `/rebind_window` to refresh the current
  session's window mapping when the bot reports "not alive". Drop
  /rebind muscle memory.

## 3.1.2 — 2026-04-27

### Removed

- `Autostart` placeholder dropped from the `Installation` section
  in the README. v3.1.1 added a TODO note pointing at a future
  systemd user unit + LaunchAgent plist, but autostart is not on
  the roadmap; manual launch inside `__ccmux__` is the intended
  workflow. Removing the placeholder so collaborators don't read
  it as a planned feature.

## 3.1.1 — 2026-04-27

### Changed

- README now has a real `Installation` section walking through the
  side-by-side clone of `ccmux-backend` and `ccmux-telegram` and the
  two `uv tool install --editable` commands needed to expose both
  CLIs (the backend provides `ccmux`, the frontend provides
  `ccmux-telegram`). Previously the README only described the
  post-install steps (`ccmux hook --install`, `.env`, `tmux`) and
  implied the package would be pulled transitively, which doesn't
  work because the `ccmux` dependency is resolved via a
  `tool.uv.sources` relative path and is not on PyPI.
- Prerequisites bullet for `ccmux-backend` rewritten to describe the
  side-by-side clone and to make clear that `ccmux-backend` is the
  runtime library this bot imports, not just the home of the
  `ccmux hook` CLI.
- Added an `Autostart` placeholder noting that a systemd user unit
  (Linux) and LaunchAgent plist (macOS) are TODO.

## 3.1.0 — 2026-04-25

### Added

- Bot-relayed messages from Telegram now carry a `[from ccmux]`
  prefix in the Claude Code JSONL transcript so it's possible to
  tell at a glance which user-role entries originated from the
  Telegram bridge versus direct keystrokes typed into the tmux
  pane. Applied at the three relay paths (`text_handler`,
  `photo_handler`, `voice_handler`) before `dispatch_text`. The
  echo path back to Telegram (`build_response_parts` for
  `role == "user"`) strips the prefix so the user-facing
  rendering stays clean.
- New module `ccmux_telegram.relay_tag` exposes
  `RELAY_TAG_PREFIX`, `tag_relayed`, and `strip_relay_tag` for the
  prepend / strip pair. `tests/test_relay_tag.py` covers
  round-trip identity, partial-match safety, and empty input.

  Slash commands forwarded to Claude Code (`/clear`, `/cost`, …)
  are intentionally NOT tagged: they're CC TUI commands, never
  reach JSONL, and a leading `[from ccmux]` would also break
  CC's `/`-prefix recognition.

## 3.0.0 — 2026-04-25

### Removed (BREAKING)

- The `/watcher` dashboard command and its underlying
  `WatcherService` are removed. The "who's waiting for you"
  aggregator topic feature was unused; every wire that fed it
  (background tick loop, status-line process hook, topic-close
  notification, deep-link message-id tracking, persistent watcher
  registration in `topic_bindings.json`) is gone with it. Users
  who had registered a watcher topic see one orphan
  `_meta.watcher` key in their `topic_bindings.json`; on next
  startup `_read_state_file` strips it and re-saves the file
  cleanly.

### Added

- `/text` — capture the current tmux pane as plain text. Strips
  Claude Code's chrome (prompt box + status bar) before sending so
  the user sees only the conversational pane content. Forces the
  Telegram code-fence language to `text` to suppress client-side
  Python auto-highlighting on TUI captures.
- `/bar` — capture only Claude Code's status bar (the pane content
  below the last `────` chrome separator). Useful for grabbing the
  spinner / token meter without the surrounding noise.
- `/sweep` — delete this topic's bot-owned commands and their
  replies. Tracking is opt-in via a new `@sweep_tracked` decorator
  combined with a `contextvars.ContextVar` wired into
  `safe_reply` / `safe_send`, so any reply produced inside a
  decorated handler is auto-registered for sweep. Currently
  applied to `/text`, `/bar`, `/history`, `/usage`, and `/sweep`
  itself.

### Changed

- Telegram menu order is rebuilt around the new commands: `/start`,
  `/esc`, `/bar`, `/text`, `/sweep`, `/history`, `/unbind`,
  `/rebind`, `/usage`, then the forwarded Claude Code commands.
- `sender.safe_send` now returns `Message | None` instead of
  `None`, so the sweep log can pick up the outgoing message id.
- `sweep_tracked`'s handler type is `Callable[..., Coroutine[Any,
  Any, None]]` instead of `Callable[..., Awaitable[Any]]`, matching
  what `python-telegram-bot`'s `CommandHandler` expects.
- `ccmux` dependency upper bound is raised: `>=3.0.0,<4.0.0`.
  Backend v3 is the env-var-namespace BREAKING release; the
  frontend doesn't read those vars itself, so no code change was
  needed beyond the constraint.

### Docs

- README rewritten end-to-end to match the conventions established
  in `ccmux-backend` (badges, Prerequisites section, numbered
  Usage steps, audience-specific bullet sections, NOTE callouts
  with `GitHub - owner/repo` link text only for cross-repo
  redirects). Reflects the current command surface (no /watcher,
  yes /text /bar /sweep) and adds a State files section listing
  what the frontend writes under `$CCMUX_DIR`.
- README's "Run" step explicitly tells the operator to launch the
  bot inside a tmux session matching `CCMUX_TMUX_SESSION_NAME` so
  the backend excludes the bot's own pane from the binding picker.

## 2.3.0 — 2026-04-21

### Added

- `message_dispatch.dispatch_text` gates every user-to-Claude-Code
  text send on the destination window's last observed `ClaudeState`:
  - `Idle` (or no observation yet) sends through immediately.
  - `Working` / `Blocked` buffers the message in an in-memory
    per-window FIFO and applies a 🤔 reaction to the originating
    Telegram message.
  - `Dead` rejects with a user-visible error.
- `status_line.on_state`'s `Idle` branch now drains every pending
  message for the transitioning window, in FIFO order. Delivered
  messages get a 👤 reaction so the user can see at a glance what
  went through.
- All four `send_text` call sites in `message_out.py` (text, photo,
  voice transcript, `/cc` slash command) route through the new
  `dispatch_text`. Navigation keys in `prompt.py` (Up / Down / Enter
  / Escape responses to Blocked UIs) stay on the direct send_keys
  path — they ARE the Blocked response, not new turn content.

### Fixed

- Root cause of the duplicated spinner "stair-step" regression:
  when CC was mid-turn and the user sent a new message, send_keys
  would expand CC's input area vertically, overflow
  `parse_status_line`'s chrome-search window, and flip the bot's
  detected state to Idle. The old status message got deleted and
  republished on the next poll, repeating. Gating at the source
  keeps the input single-line during Working, so the spinner stays
  anchored and state detection stays stable.

### Removed

- The v2.1.2 "Message is not modified" except-path in
  `_queue_status.py`. That was a symptomatic fix for the same
  underlying bug; with gated dispatch the preconditions that
  produce identical rapid-fire status texts no longer occur. The
  original fallback (retry plain-text, then post-new on second
  failure) is back in effect.

### Changed

- `ccmux` dependency bumped to `>=2.5.0,<3.0.0` (was `>=2.5.0` via
  2.2.0; no effective change, but reaffirmed — dispatch leans on
  the raw-pane shape that 2.5.0 introduced).

## 2.2.0 — 2026-04-21

### Added

- `status_render.render_status_text` translates the raw TodoWrite
  block returned by backend v2.5.0 into what reads cleanly inside a
  Telegram message. Rendering rules:
  - drop the `⎿` elbow connector,
  - normalize every row's leading whitespace to two spaces,
  - map unicode checkbox glyphs to ASCII brackets
    (`◻`/`☐` → `[ ]`, `◼` → `[>]`, `✔`/`✓`/`☒` → `[x]`),
  - wrap completed rows in GitHub-flavored `~~…~~` so
    telegramify_markdown converts to MarkdownV2 single-tilde
    strikethrough,
  - truncate rows to 50 characters, preserving the closing `~~` on
    completed rows so MarkdownV2 parsing stays balanced.
- Wired into `_queue_status.py` at both the edit-in-place and
  initial-send paths.

### Changed

- `ccmux` dependency bumped to `>=2.5.0,<3.0.0`. Backend v2.5.0
  returns raw pane text; v2.4.0 and earlier returned pre-formatted
  ASCII brackets which would double-translate with the new render
  layer.

## 2.1.2 — 2026-04-21

### Fixed

- `_process_status_update_task` would post a fresh pinned status
  message whenever two poll ticks produced byte-identical
  `status_text` values. Telegram answered the edit call with
  `BadRequest: Message is not modified`, which the catch treated as
  a generic edit failure and fell through to
  `_do_send_status_message`. On a `working` streak this stair-stepped
  duplicate spinner messages (`Infusing… (3m 14s)`, `Infusing…
  (3m 19s)`, …) in the chat instead of a single in-place edit. The
  catch now identifies the specific error, caches the text so the
  next tick's fast-path skip fires, and returns without falling
  through.

## 2.1.1 — 2026-04-21

### Fixed

- `status_line._dispatch` declared `chat_id: int | None` but then
  passed that parameter into `enqueue_status_update` which requires
  `int`. The only caller (`on_state`) reads `chat_id` from
  `topic.group_chat_id` which is always `int`, so the permissive
  annotation was a lie and pyright rejected the CI build. Tighten
  the parameter to `int`.

## 2.1.0 — 2026-04-21

### Added

- Blocked-UI messages are now styled with Telegram MarkdownV2. The
  first non-empty line (tool title / question) and any selected
  option (`❯ 1. Yes`) render bold; the `Esc to …` / `Enter to …`
  footer renders italic. Implemented with a minimal in-house
  Markdown → MarkdownV2 translator (`_render_mdv2`) that preserves
  leading whitespace and does not re-flow numbered-option lines as
  an ordered list the way `convert_markdown` does.
- `/start` in an *unbound* forum topic now routes through
  `handle_unbound_topic` and shows the tmux-session picker directly,
  matching the behavior of sending any plain-text message.
- `/start` in a *bound* forum topic now shows the bound session name
  and the three commands most likely useful next:

      ✅ Bound to `<session_name>`.

      /rebind — switch to another session
      /history — view past messages
      /unbind — remove this binding

  Replaces the generic "Create a new topic to start" welcome which
  was misleading inside an already-bound topic.
- `/history` and `send_history` now log at info level — every
  invocation records user / thread / message count so live regressions
  can be diagnosed from `~/.ccmux/ccmux.log` without re-running the
  command.

### Changed

- Requires `ccmux>=2.1.0` (was `>=2.0.0`). The 2.1 backend's
  parser walkback is what carries the tool-preview block into the
  pane content that this frontend renders.
- `StateCache.update()` now returns whether the observed state
  actually changed. `status_line.on_state` uses that return value
  to edge-trigger Telegram dispatch: identical consecutive states
  no longer produce redundant edit/send calls. The watcher still
  receives every observation.
- `handle_interactive_ui` distinguishes Telegram's "message is not
  modified" BadRequest as a no-op success rather than a failure.
  Same treatment applied to `binding_flow` topic renames
  (`Topic_not_modified` when the topic already has the target name).

### Fixed

- Interactive-msg lifecycle race: `handle_new_message` used to
  unconditionally call `clear_interactive_msg` on any non-PROMPT
  `tool_use` ClaudeMessage. Claude Code 2.1.x emits these while the
  permission UI is still on the pane, so state_monitor's freshly
  sent Blocked message was wiped ~1 ms after delivery. State_monitor
  is now the single owner of the interactive-msg lifecycle.
- Topic rename no longer logs a warning when Telegram replies
  `Topic_not_modified` (the topic already has the target name).
  Real failures (permissions, topic deleted) still warn.

### Removed

- `tool_context.py` and its call sites in `message_in` and `prompt`.
  The module's only job was reading JSONL to prepend a rich tool
  header to permission prompts; Claude Code 2.1.x does not flush
  `tool_use` to JSONL until the turn completes (i.e. only after
  approval), so `get_pending` always returned `None` during the
  permission UI's lifetime. The pane walkback in ccmux-backend
  2.1.0 replaces this with a working equivalent.
- Associated tests (`tests/test_tool_context.py`,
  `tests/test_prompt_context_injection.py`, and
  `TestToolContextWiring` in `tests/test_message_in_skill_filter.py`).

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
