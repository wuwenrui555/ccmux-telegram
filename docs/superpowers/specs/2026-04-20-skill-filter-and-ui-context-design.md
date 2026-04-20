# Skill Body Filter & Interactive UI Context Injection

Design doc for two Telegram-frontend UX fixes. Both are frontend-only;
backend (`ccmux-backend`) is untouched.

## Problem

**P1 — Skill tool floods the chat.** When Claude invokes the `Skill` tool,
the backend emits a `tool_use` summary (`**Skill**(name)`) followed by a
`tool_result` containing the skill's full body. Even rendered as a Telegram
expandable blockquote, the body is many thousands of characters and shows
a multi-line preview above the "Show More" fold, pushing real conversation
off-screen.

**P2 — Permission UI lacks context.** When Claude Code shows a
`PermissionPrompt` or `BashApproval` in its tmux pane (e.g. "Do you want
to make this edit? 1. Yes / 2. No"), the frontend captures and forwards
just that prompt. The user sees the question but not *what* is being
edited — file path, diff, command, etc. The backend already has the data
(the preceding `tool_use` JSONL block), but it is not surfaced through
the `ccmux.api` event stream.

## Goals

- Skill `tool_result` bodies do not flood the chat by default; behavior is
  gated by an env var so an advanced user can re-enable it.
- Permission-style UI messages carry the relevant tool input inline so the
  user can decide without switching context.
- Zero changes to `ccmux-backend`. No new `ccmux.api` surface.

## Non-Goals

- Generating server-side stats rows (e.g. `⎿ Skill loaded (N lines)`).
  The `tool_use` summary alone is enough context. This can be added later
  if backend ships an unrelated minor/major bump.
- Fancy diff rendering (syntax-highlighted, side-by-side). Plain unified
  diff inside a Telegram expandable blockquote is sufficient.
- Covering `AskUserQuestion` / `ExitPlanMode` / `RestoreCheckpoint` / `Settings`
  prompts. Those originate from tools whose input is either already
  surfaced (AskUserQuestion question text, ExitPlanMode plan body) or
  not tied to a specific file/command that needs re-showing.

## Approach

Two independent features share a single design principle: derive
everything from the `ClaudeMessage` event stream the frontend already
consumes, plus on-demand JSONL reads via the public `TranscriptParser`.
No new backend dependencies.

### Feature A: Skill body filter

- Add frontend config flag `show_skill_bodies` from env
  `CCMUX_SHOW_SKILL_BODIES` (default `false`).
- In `message_in.handle_new_message`, add a gate alongside the existing
  `show_tool_calls` / `show_thinking` gates:

  ```python
  if (
      not config.show_skill_bodies
      and msg.content_type == "tool_result"
      and msg.tool_name == "Skill"
  ):
      return
  ```

- The corresponding `tool_use` summary (`**Skill**(name)`) is untouched
  so the user still sees that a skill was invoked.

### Feature B: Tool input cache + UI injection

New module `ccmux_telegram/tool_context.py`. Responsibilities:

1. **Cache** the pending tool input per `window_id`, keyed by
   `tool_use_id`.
2. **Populate** the cache by reading the raw `input` dict from the
   session's JSONL file when a `tool_use` ClaudeMessage arrives — the
   ClaudeMessage itself does not carry `input_data`.
3. **Format** the cached input into a Telegram markdown fragment tuned
   per tool (Edit → unified diff, Write → file + content preview, Bash
   → command + description, fallback → key=value lines).
4. **Clear** the cache when the matching `tool_result` arrives, when the
   cache's per-entry TTL expires, or when the window is unbound.

Integration points:

- `message_in.handle_new_message` calls `tool_context.record(...)` on
  every `tool_use` ClaudeMessage and `tool_context.clear(...)` on every
  `tool_result`. Calls happen before the existing display gates so
  caching proceeds even when the user has suppressed tool_calls display.
- `prompt.handle_interactive_ui` calls `tool_context.get_pending(window_id)`
  just before building the Telegram message. If a cached entry exists
  for a tool that benefits from context (see list below), its formatted
  block is prepended to the pane-captured UI content, separated by a
  blank line.

### Tools that get context injected

- `Edit`, `NotebookEdit` — unified diff (old_string → new_string) in
  expandable blockquote, prefixed with `file_path`.
- `Write` — `file_path` header + content preview in expandable
  blockquote.
- `Bash` — fenced `command` + optional `description` line.
- Any other tool — readable `key: value` lines for the input dict
  (truncated per-value), in expandable blockquote.

The formatter picks a variant by `tool_name`; unknown tools get the
fallback. No hard-coded allowlist — the "should we inject?" decision
is handled by the presence of a cached entry, so any tool_use that
leads to a PermissionPrompt will be annotated.

## Data Flow

**Skill filter:**

```
ClaudeMessage(content_type=tool_result, tool_name=Skill)
  → message_in.handle_new_message
    → gate: if not show_skill_bodies: return
  (tool_use summary "**Skill**(name)" was emitted earlier, untouched)
```

**UI context injection:**

```
ClaudeMessage(content_type=tool_use, tool_name=Edit, tool_use_id=T, session_id=S)
  → tool_context.record(window_id=W, tool_name=Edit, tool_use_id=T,
                        session_id=S)
    → async: locate JSONL via public API:
        wb = get_default_backend().get_window_binding(W)
        encoded = WindowBindings.encode_cwd(wb.cwd)
        path = ccmux.config.config.claude_projects_path /
               encoded / f"{S}.jsonl"
      → read last ~64KB
      → TranscriptParser.parse_line each → find entry whose
        message.content[*].type == "tool_use" and .id == T
      → cache[W] is a deque capped at _MAX_PENDING=5;
        append (tool_name, T, input_dict, loop.time())

Pane shows PermissionPrompt or BashApproval
  → prompt.handle_interactive_ui(window_id=W, ...)
    → pane_text = tm.capture_pane(...)
    → ui_content = extract_interactive_content(pane_text)
    → entry = tool_context.get_pending(W)
    → if entry and entry.is_fresh():
        header = format_input_for_ui(entry.tool_name, entry.input)
        text = f"{header}\n\n{ui_content.content}"
      else:
        text = ui_content.content
    → send_message / edit_message_text with text + keyboard

ClaudeMessage(content_type=tool_result, tool_use_id=T)
  → tool_context.clear(window_id=W, tool_use_id=T)
```

## Module Layout

**New:**

- `src/ccmux_telegram/tool_context.py`
  - `@dataclass class PendingToolContext`
    - `tool_name: str`
    - `tool_use_id: str`
    - `input: dict | None`
    - `recorded_at: float` (monotonic)
  - `async def record(window_id, tool_name, tool_use_id, session_id) -> None`
  - `def clear(window_id, tool_use_id) -> None`
  - `def get_pending(window_id) -> PendingToolContext | None` (returns
    only entries within `_TTL_SECONDS`)
  - `def format_input_for_ui(tool_name, input_dict) -> str`
  - `_PENDING: dict[str, collections.deque[PendingToolContext]] = {}`
    per-window deque bounded by `_MAX_PENDING`; module-level, single
    bot process, no concurrency issues beyond asyncio serial.
  - `_TTL_SECONDS = 60.0`
  - `_MAX_PENDING = 5` — per-window cap; older entries auto-evict.
  - `_JSONL_TAIL_BYTES = 64 * 1024`
  - `get_pending(window_id)` returns the **most recently recorded**
    non-stale entry (deque tail). Rationale: when Claude issues
    parallel tool_uses and a PermissionPrompt fires, the prompt is
    almost always for the most-recent tool. Edge case (prompt for an
    earlier pending tool) is an accepted limitation; the user can
    scroll up to the tool_use summary for detail.

- `tests/test_tool_context.py` — unit coverage for cache lifecycle,
  TTL eviction, JSONL parse, each `format_input_for_ui` branch.
- `tests/test_message_in_skill_filter.py` — `handle_new_message`
  with `show_skill_bodies` in both states, ensures only `tool_result`
  for Skill is suppressed and other tools pass.
- `tests/test_tool_context_parallel.py` — parallel tool_uses:
  recording 3 back-to-back tool_uses yields a deque of 3, and
  `get_pending` returns the newest. Recording past `_MAX_PENDING`
  evicts the oldest.

**Modified:**

- `src/ccmux_telegram/config.py`
  - Add:
    ```python
    self.show_skill_bodies = (
        os.getenv("CCMUX_SHOW_SKILL_BODIES", "").lower() == "true"
    )
    ```
    (Default `False` because the user opted in to A: "完全不推送".)

- `src/ccmux_telegram/message_in.py`
  - Add import: `from . import tool_context`.
  - In `handle_new_message`, before the existing `show_tool_calls` /
    `show_thinking` gates, branch on content_type:
    - `tool_use` → `await tool_context.record(msg, window_id)`.
    - `tool_result` → `tool_context.clear(window_id, msg.tool_use_id)`.
  - Add Skill filter gate after existing gates:
    ```python
    if (
        not config.show_skill_bodies
        and msg.content_type == "tool_result"
        and msg.tool_name == "Skill"
    ):
        return
    ```

- `src/ccmux_telegram/prompt.py`
  - In `handle_interactive_ui`, after `content = extract_interactive_content(...)`
    and before building the keyboard, call
    `tool_context.get_pending(window_id)` and, when present, prepend
    the formatted block to `text`.

**Not touched:**

- `ccmux-backend` (policy: frozen v1.0 API).
- `markdown.py`, `sender.py`, `message_queue.py` — reuse existing
  blockquote rendering and split-message behavior.
- `prompt_state.py` — interactive-mode tracking unchanged.

## Error Handling

- **JSONL file missing / unreadable / parse error** in
  `tool_context.record`: cache entry stored with `input=None`. UI
  injection falls back to showing just `tool_name`. Logged at `DEBUG`.
- **JSONL tail does not contain the `tool_use_id`**: happens when the
  tool_use was written earlier than the tail window. Accept — cache
  entry with `input=None`. (64KB is generous; Edit/Write tool_use
  blocks are always the last assistant turn when a permission prompt
  fires.)
- **TTL exceeded** when `handle_interactive_ui` reads the cache: treat
  as stale and ignore. Prevents leaked context from a previous tool
  whose tool_result event was lost.
- **Async race between `record` and `handle_interactive_ui`**:
  PermissionPrompt appears in the pane only after Claude emits the
  `tool_use` JSONL line, so `record` is guaranteed to run first in the
  event sequence. If `record`'s async JSONL read has not completed,
  `get_pending` returns the partially-populated entry (may have
  `input=None`); the handler re-invocation on the user's next
  keystroke (refresh, arrow, enter) re-reads the cache and the second
  render shows full context. Acceptable.
- **format_input_for_ui given malformed input_dict**: defensive — any
  key lookup failure or non-string value is coerced to `str(value)` or
  skipped. Never raises.

## Testing

- Unit — `tool_context`:
  - `record` stores entry, `get_pending` returns it.
  - `clear` removes entry.
  - TTL expiration drops entries.
  - JSONL parse: finds `input` for matching `tool_use_id` in tail;
    returns None when id absent; handles malformed JSON lines.
  - `format_input_for_ui` for Edit, NotebookEdit, Write, Bash, unknown tool.

- Unit — `message_in` Skill filter:
  - `show_skill_bodies=False`, tool_result with tool_name="Skill" →
    no enqueue.
  - `show_skill_bodies=True`, same input → enqueues normally.
  - `show_skill_bodies=False`, tool_result with tool_name="Read" →
    enqueues normally (non-Skill bypasses gate).
  - `show_skill_bodies=False`, tool_use with tool_name="Skill" →
    still enqueues (only tool_result is suppressed).

- Integration — `prompt.handle_interactive_ui`:
  - Seed `tool_context` with an Edit entry, inject a pane snapshot
    containing PermissionPrompt, assert the sent Telegram text
    contains both the diff and the pane prompt.
  - No cache entry → Telegram text matches current behavior exactly.

- Regression — run existing `tests/` suite; Skill filter must not
  affect non-Skill tool rendering, and tool_context calls must be
  no-ops when no interactive UI fires.

## Docs

- Update `README.md` env var section to list
  `CCMUX_SHOW_SKILL_BODIES`.
- Note in `README.md` that permission prompts now include diff/command
  context automatically.

## Rollout

Single commit per feature, both in a `feature/ui-clarity` branch
branched from `dev`, merged to `dev` after green CI. Version bump to
`1.2.0` on next release (new feature, fully additive).
