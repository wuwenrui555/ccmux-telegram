# Binding Self-Heal Design

- **Date**: 2026-04-27
- **Repos affected**: `ccmux-backend`, `ccmux-telegram`
- **Status**: design accepted; implementation pending

## Problem

`claude_instances.json` maps a tmux session name (`instance_id`) to a tmux
window id (`window_id`) and a Claude session id (`session_id`). The map is
written **only** by the `ccmux hook` CLI on Claude Code `SessionStart`. When
the actual tmux window for a Claude instance changes without a corresponding
`SessionStart` event, the entry becomes stale.

The known trigger is `tmux-continuum` auto-restore on macOS: every tmux
restore re-spawns Claude in a new window with a new `window_id`, but Claude
preserves its existing `session_id`. The hook does fire on each respawn, but
the hook also has a "one Claude per tmux session" guard that refuses to
overwrite an existing entry with a different `session_id`. The end state is
that `claude_instances.json` can hold a `window_id` that no longer exists.

The bot detects this indirectly. `state_monitor._classify_from_pane` calls
`find_window_by_id(stored_window_id)`; when the window is gone it returns
`None` and the state cache for that instance never gets populated.
`StateCache.is_alive(instance_id)` therefore returns `False`, and on the next
user message the bot replies:

> ⚠️ Binding to `<name>` is not alive right now. tmux or Claude may be down.
> Use /rebind to reconnect to a different session.

The message is misleading: tmux and Claude are both fine; the bot is just
looking at the wrong window. The only end-user fix today is `/clear` in the
target Claude window (which discards the conversation, often hundreds of
thousands of tokens) or a manual edit of `claude_instances.json`.

## Goals

1. `ccmux-backend` exposes a pure read function that, given an `instance_id`,
   returns the Claude window the bot should now talk to (or `None` if no
   Claude is running in that tmux session).
2. `ccmux-backend` exposes an in-memory override layer on
   `ClaudeInstanceRegistry` so the frontend can apply reconcile results
   without touching `claude_instances.json`.
3. `ccmux-telegram` uses these in three places:
   - **Startup**: silent best-effort reconcile pass for every known binding.
   - **`/rebind_window` command**: explicit user-triggered reconcile in a topic.
   - **Transition detector**: when a previously-broken binding becomes alive
     again — for any reason, including the user editing
     `claude_instances.json` by hand or via an agent — the bot posts a
     confirmation in the topic.
4. `claude_instances.json` remains hook-owned. Reconcile results live only in
   process memory.

## Non-goals

- **Auto-fix at runtime without telling the user.** When a binding goes stale
  while the bot is online and the user is interacting, the bot reports the
  problem and waits. The user (or an agent) decides how to fix it. This is a
  deliberate UX choice: the user should know which Claude window their topic
  is talking to, so silent window-swaps are off the table.
- **Eliminating the underlying tmux-continuum behavior.** We work around it.
- **Persistent override across bot restarts.** The fix is cheap to recompute
  on the next startup pass, and skipping persistence avoids any new write
  path on the lock file.
- **A multi-window picker UI for `/rebind_window`.** When several Claude
  windows share one tmux session, the deterministic priority order in the
  reconcile algorithm picks one. Power users who want a different one can
  edit `claude_instances.json` directly; the transition detector will report
  the change.

## Architecture

```text
┌─────────────────────────────────────────────┐
│ ccmux-backend                               │
│                                             │
│   ClaudeInstanceRegistry                    │
│   ├─ _data         (loaded from file)      │
│   ├─ _overrides    (NEW, in-memory)        │
│   ├─ get(name)     override > file value   │
│   ├─ set_override(name, instance)          │
│   └─ clear_override(name)                  │
│                                             │
│   Backend.reconcile_instance(               │
│       instance_id) -> ClaudeInstance | None │
│                                             │
│   pid_session_resolver (NEW shared module)  │
│     resolve_for_pane(pane_id)               │
│       -> (session_id, cwd) | None           │
│   (refactor: hook.py already has this code  │
│    privately as _resolve_session_via_pid;   │
│    lift, share, make portable on macOS.)    │
│                                             │
└─────────────────────────────────────────────┘
                  ▲
                  │ ccmux.api
                  │
┌─────────────────────────────────────────────┐
│ ccmux-telegram                              │
│                                             │
│   main.py                                   │
│     - startup: reconcile pass               │
│     - background asyncio task: BindingHealth│
│       polling loop (~0.5 s) → post ✅        │
│                                             │
│   command_basic.py                          │
│     - /rebind  → REMOVED                    │
│     - /rebind_topic  (rename of old handler)│
│     - /rebind_window (NEW handler)          │
│                                             │
│   message_out.py                            │
│     - update ⚠️ wording (multiple sites)    │
│                                             │
│   binding_health.py (NEW)                   │
│     class BindingHealth                     │
│     enum Transition (STABLE/RECOVERED/LOST) │
│                                             │
└─────────────────────────────────────────────┘
```

## Backend changes (`ccmux-backend`)

### `ClaudeInstanceRegistry` — add in-memory override layer

Existing `_data` (loaded from `claude_instances.json`) is not touched. A
new `_overrides` dict layered on top.

```python
class ClaudeInstanceRegistry:
    _data: dict[str, dict[str, str]]            # existing
    _overrides: dict[str, ClaudeInstance]        # NEW, init empty in __init__

    def set_override(self, instance_id: str, instance: ClaudeInstance) -> None:
        """Install or replace the in-memory override for `instance_id`."""
        self._overrides[instance_id] = instance

    def clear_override(self, instance_id: str) -> None:
        """Remove the in-memory override (no-op if not present)."""
        self._overrides.pop(instance_id, None)
```

`get`, `get_by_window_id`, `find_by_session_id`, `contains`, and `all` all
need to consult overrides first. Concretely:

- `get(instance_id)`: if `instance_id in _overrides`, return that; else
  existing logic.
- `get_by_window_id(window_id)`: scan overrides first, then fall back to
  `_data`. (An override for instance X with the queried window_id wins over
  a stale entry in `_data`.)
- `find_by_session_id(session_id)`: same pattern.
- `contains(instance_id)`: true if `instance_id` is in either map and has
  non-empty `window_id` and `session_id`.
- `all()`: union of overrides and `_data` entries that have a `window_id`.
  When an `instance_id` is in both, override wins.

### `Backend.reconcile_instance(instance_id)` — pure read

Signature: `async def reconcile_instance(instance_id: str) -> ClaudeInstance | None`

Async because it calls `TmuxSession.list_windows()` (already async).
**No writes**, no override mutations. The caller (frontend) decides whether
to apply the result via `set_override`.

Algorithm:

1. Resolve the tmux session for `instance_id` (existing `tmux_registry`
   lookup; create-on-miss is fine because `list_windows` returns `[]` for a
   missing session).
2. Call `TmuxSession.list_windows()` to get all windows in that session.
3. Filter to "Claude windows": those whose `pane_current_command` is in the
   `_claude_proc_names()` set (existing helper in `state_monitor`).
4. If zero Claude windows: return `None`.
5. If exactly one Claude window: pick it. Resolve its `(session_id, cwd)`
   via `pid_session_resolver.resolve_for_pane(active_pane.pane_id)`. Do
   **not** preserve the recorded `session_id` from `_data[instance_id]` —
   the recorded value may refer to a previous Claude that no longer
   occupies this window, in which case reusing it would steer the bot's
   `message_monitor` at the wrong JSONL. If the resolver returns `None`,
   fall back to the recorded `session_id` and `cwd` (best-effort) and log
   a warning.
6. If more than one Claude window, resolve by priority:
   - **a. Match recorded `session_id`.** For each candidate, call
     `resolve_for_pane(active_pane.pane_id)` to get its current
     `session_id`. If any candidate's `session_id` equals
     `_data[instance_id].session_id`, pick that window. (The Claude
     instance migrated to a new window via `--resume`; we follow it.)
   - **b. JSONL mtime.** If a/ resolution returned no `session_id` match
     (resolver failed for all candidates, or none matched), pick the
     window whose `resolve_for_pane` returned the most-recent JSONL
     mtime. (Newest active session wins.)
   - **c. Fallback.** If both a/ and b/ produced nothing usable (e.g.,
     resolver failed everywhere), pick the candidate with the lowest
     tmux `window_index`. Stable, deterministic.
7. Return `ClaudeInstance(instance_id=instance_id, window_id=picked.window_id,
   session_id=resolved-or-recorded, cwd=resolved-or-recorded)`.

`pid_session_resolver` is a refactor of the existing private function
`_resolve_session_via_pid` in `hook.py`. The function already implements
the full chain: tmux pane → shell pid → claude pid →
`~/.claude/sessions/<claude_pid>.json` → launch cwd → newest JSONL in
`~/.claude/projects/<encoded-cwd>/`. Lift the function into a new module
`ccmux.pid_session_resolver` exposing `resolve_for_pane(pane_id: str) ->
tuple[str, str] | None` (returns `(session_id, launch_cwd)`). Update
`hook.py` to import from the new module.

**Cross-platform note**: the existing `_find_claude_pid` reads
`/proc/<pid>/cmdline` to confirm a child pid is Claude, which is
Linux-only. For the hook this is a non-issue because its primary stdin
path doesn't need this fallback, and `_find_claude_pid` is only
exercised when stdin is empty. For reconcile we need it to work on
macOS too. The lift-and-share refactor is the right time to swap the
`/proc/<pid>/cmdline` confirmation for a portable signal: among the
direct children of the shell, pick the one whose
`~/.claude/sessions/<pid>.json` exists. That file is created by Claude
Code itself on startup, so its presence is a reliable "this child is
Claude" signal on both platforms. No behavior change for the hook's
existing happy path; macOS gains correct fallback behavior.

### Public API surface (`ccmux.api`)

The existing `Backend` Protocol gains `reconcile_instance`. The existing
`ClaudeInstanceRegistry` gains `set_override` and `clear_override`. Both
classes are already exported from `ccmux.api`, so no edits to the
`__all__` list.

`pid_session_resolver` stays internal for now (no use case for direct
frontend access).

## Frontend changes (`ccmux-telegram`)

### `main.py` — startup reconcile pass

After `_topics.load()` and after the backend is constructed, iterate over
the unique session names referenced by topic bindings and reconcile each
once before serving any Telegram traffic.

```python
seen: set[str] = set()
for binding in _topics.all():
    name = binding.session_name
    if name in seen:
        continue
    seen.add(name)
    inst = await backend.reconcile_instance(name)
    if inst is not None:
        backend.claude_instances.set_override(name, inst)
```

Silent. No Telegram messages. The bot was offline; the user has nothing to
react to.

### `command_basic.py` — `/rebind_topic`, `/rebind_window`

Rename the existing `/rebind` handler:

- Telegram command name: `rebind_topic`
- Function name: `rebind_topic_command`
- Description (BotFather menu, `/help`): "Pick a different tmux session for
  this topic."

Add a new `/rebind_window` handler:

- Only valid in a topic that's already bound. If the topic is unbound,
  reply: `❌ No session bound here. Use /rebind_topic first.`
- Calls `backend.reconcile_instance(topic.session_name)`.
- Success path: `backend.claude_instances.set_override(name, inst)` and
  reply `✅ Refreshed binding: <name> → <window_id>`.
- `None` path: reply `⚠️ Session has no live Claude. Use /rebind_topic to
  switch, or /start to spawn a new Claude.`

`/rebind` is **removed entirely** — no alias. CHANGELOG documents the
rename under a "Removed" entry referencing the new commands.

Update `/help` text and the BotFather command list to reflect the rename
and the new command.

### `message_out.py` — refresh ⚠️ wording

Every existing call site that posts:

> ⚠️ Binding to `<name>` is not alive right now. tmux or Claude may be down.
> Use /rebind to reconnect to a different session.

becomes:

> ⚠️ Binding to `<name>` is not alive right now. tmux or Claude may be down.
> Use /rebind_window to refresh, or /rebind_topic to switch.

There are nine call sites total (per current `grep`); a single
search-and-replace covers them all.

### `binding_health.py` — NEW transition detector

```python
import enum

class Transition(enum.Enum):
    STABLE = "stable"
    RECOVERED = "recovered"
    LOST = "lost"


class BindingHealth:
    """Per-binding alive-transition tracker. Single-threaded usage."""

    def __init__(self) -> None:
        self._was_alive: dict[str, bool] = {}

    def observe(self, instance_id: str, is_alive_now: bool) -> Transition:
        """Record `is_alive_now` and return the transition vs last call.

        First observation defaults `prev = True` so a healthy binding does
        not generate a spurious RECOVERED on startup.
        """
        prev = self._was_alive.get(instance_id, True)
        self._was_alive[instance_id] = is_alive_now
        if not prev and is_alive_now:
            return Transition.RECOVERED
        if prev and not is_alive_now:
            return Transition.LOST
        return Transition.STABLE
```

### Wire-up — independent periodic asyncio task

A new background asyncio task in `main.py` polls `state_cache.is_alive` for
every active binding and drives the detector. Cadence: `0.5 s`, matching
the backend's `CCMUX_MONITOR_POLL_INTERVAL` default. The task runs for the
lifetime of the bot.

Why a separate poll rather than piggybacking on the backend's `on_state`
callback: a stale binding may emit no state at all (the state monitor
returns `None` when `find_window_by_id` fails), so an `on_state`-driven
detector would miss the `LOST` shoulder. A separate poll observes
transitions reliably regardless of whether state was emitted that tick.

```python
async def _run_binding_health_loop(
    bot: telegram.Bot,
    interval: float = 0.5,
) -> None:
    while True:
        try:
            for binding in _topics.all():
                t = _binding_health.observe(
                    binding.session_name,
                    _state_cache.is_alive(binding.session_name),
                )
                if t is Transition.RECOVERED:
                    try:
                        await bot.send_message(
                            chat_id=binding.group_chat_id,
                            message_thread_id=binding.thread_id,
                            text=f"✅ Binding to `{binding.session_name}` recovered.",
                            parse_mode="MarkdownV2",
                        )
                    except Exception:
                        logger.exception("Failed to post recovery notice")
        except Exception:
            logger.exception("binding_health loop iteration failed")
        await asyncio.sleep(interval)
```

`Transition.LOST` is intentionally **not** posted here. Posting `LOST`
proactively would double-fire with the existing `message_out.py` ⚠️ that
fires on the next user send. The rule "`LOST` is whatever made the next
user-send fail" keeps the user-facing surface minimal.

## State variables and naming

Single source of truth: anything alive-related goes through `is_alive` /
`was_alive`. Do not introduce `is_broken` or `was_broken`.

| Where | Var / method (type) | Meaning |
|---|---|---|
| backend `state_monitor` | emits `ClaudeState` (union of `Working`/`Idle`/`Blocked`/`Dead`) | current emitted state |
| frontend `state_cache` | `_data[id]: dict[str, ClaudeState]` | last-seen state for an instance |
| frontend `state_cache` | `is_alive(id) -> bool` | derived: `state ∉ {None, Dead}` |
| backend `ClaudeInstanceRegistry` | `_overrides[name]: dict[str, ClaudeInstance]` | in-memory override |
| frontend `binding_health` | `_was_alive[id]: dict[str, bool]` | last observed `is_alive` value |
| frontend `binding_health` | `observe(id, now) -> Transition` | flip-detection entry point |
| frontend `binding_health` | `Transition` enum (`STABLE` / `RECOVERED` / `LOST`) | observe's return value |

External consumers do not touch `_overrides` or `_was_alive` directly; both
are accessed via methods (`set_override`/`clear_override`/`get`,
`observe`).

## UX summary

| Trigger | Detected by | Bot reaction |
|---|---|---|
| Startup, binding stale | startup pass | silent reconcile + override |
| Startup, no Claude in session | startup pass | silent (no override, no msg) |
| User sends, binding `is_alive=False` | `message_out.py` | reply ⚠️ in topic |
| User runs `/rebind_window`, succeeds | command handler | reply ✅ inline |
| User runs `/rebind_window`, no Claude | command handler | reply ⚠️ inline |
| External edit fixes a stale binding (file edit, agent script, future hook fire) | `binding_health` flip | post ✅ to topic |
| Binding goes alive → not alive | (intentional no-op) | nothing; `message_out.py` reports on next user send |

## Error handling

- `reconcile_instance` should swallow tmux errors and return `None`. Callers
  fall back to the existing "not alive" path. Raising would force every
  caller into a try/except with no useful recovery.
- `pid_session_resolver.resolve_for_pane` may fail at any step (`pgrep`
  returns no children, no child has a `~/.claude/sessions/<pid>.json`, the
  sessions file is unparsable, the project dir contains no JSONLs, the
  process exited mid-resolve). All failure paths return `None`. The
  reconcile algorithm treats `None` as "session_id unknown" and falls back
  through priority steps a → b → c.
- File system race: if `claude_instances.json` is being rewritten by the
  hook exactly when the registry reloads, existing `_read` already swallows
  `JSONDecodeError`. The override layer is process-memory only and not
  affected.
- Posting to Telegram may fail (network, chat archived). Catch and log;
  do not retry. Keep the binding marked alive in `_was_alive` so the loop
  doesn't spam attempts.

## Testing

Backend (`ccmux-backend`):

- `reconcile_instance` unit tests with mocked tmux and temp JSONL fixtures:
  - zero Claude windows → `None`
  - one Claude window → that window
  - multiple, recorded `session_id` matches one → that one wins
  - multiple, no `session_id` match → mtime winner
  - multiple, mtimes equal/unknown → lowest `window_index`
- `ClaudeInstanceRegistry` override layer:
  - `set_override` then `get` returns override
  - `clear_override` reverts to file-backed value
  - `set_override` for an instance not in `_data` still resolves via `get`
  - `get_by_window_id` and `find_by_session_id` consult overrides first
- `pid_session_resolver.resolve_for_pane`:
  - Happy path: mocked `tmux display-message` → mocked `pgrep -P` →
    fake `~/.claude/sessions/<pid>.json` on disk → fake JSONLs in
    `~/.claude/projects/<encoded-cwd>/` → returns `(session_id, cwd)`.
  - Same fixture works on Linux and macOS (no `/proc` reliance after the
    refactor).
  - Missing `~/.claude/sessions/<pid>.json` for every child → `None`.
  - Multiple children, only one has a sessions file → that one wins.
  - `pgrep` returns nothing → `None`.

Frontend (`ccmux-telegram`):

- `BindingHealth.observe`:
  - first call with `now=True` → `STABLE`
  - first call with `now=False` → `LOST` (because `prev` defaults `True`)
  - F→T → `RECOVERED`
  - T→F → `LOST`
  - F→F and T→T → `STABLE`
- `/rebind_window` handler:
  - bound topic + reconcile returns instance → reply ✅, override set
  - bound topic + reconcile returns `None` → reply ⚠️, no override
  - unbound topic → reply ❌, no backend call
- Startup reconcile pass: enumerates unique session names, calls
  `reconcile_instance` once per name, applies returned overrides.
- `message_out.py` ⚠️ wording: snapshot test on the new text (or grep test
  to ensure no `/rebind` literal remains).

Integration / smoke:

- Touch `claude_instances.json` externally (rewrite a stale entry to a live
  window). Within ~1 s, the backend's state monitor populates the state
  cache for that instance, the frontend's `binding_health` polling task
  observes the F→T transition, and a ✅ message is posted to the bound
  topic.
- `/rebind_topic` (renamed) still drives the session picker.
- `/rebind` no longer exists; Telegram returns its standard "command not
  found" surface.

## Migration / rollout

- `ccmux-backend` ships first with the new API (`reconcile_instance`,
  `set_override`, `clear_override`). Versioning: a minor bump (e.g.
  `3.0.2` → `3.1.0`) since `ccmux.api` gains new symbols but no existing
  symbol changes shape.
- `ccmux-telegram` ships next, with `dependencies` bumped to require the
  new backend (`ccmux>=3.1.0,<4.0.0`). Telegram version: a minor bump
  (e.g. `3.1.2` → `3.2.0`) — no breaking shape changes for installed
  Python code, but the user-facing `/rebind` command is removed.
- CHANGELOG entries:
  - `ccmux-backend`: under "Added", the new API and `pid_session_resolver`
    refactor.
  - `ccmux-telegram`: under "Added", `/rebind_window` and the startup
    reconcile pass and the recovery notification. Under "Changed", the
    refreshed ⚠️ wording. Under "Removed", `/rebind`, with a note pointing
    at `/rebind_topic`.

## Out of scope

- Watching `claude_instances.json` via `inotify` / `fsevents`. Polling at
  fast-tick rate (~0.5 s) is fine.
- A multi-window picker UI for `/rebind_window`. Power users edit the JSON.
- Persisting overrides across bot restarts.
- Touching the `ccmux hook` write path. The hook stays the only writer.
- A CLI subcommand on `ccmux-telegram` for shell-side reconcile triggers.
  The user explicitly chose the simpler "edit `claude_instances.json`
  directly" path for shell triggering, leveraging the existing reload +
  the new transition detector.

## Open questions

(none at design time; resolve at implementation if surprises emerge)
