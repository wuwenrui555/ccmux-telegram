"""Tag for messages relayed by ccmux-telegram into a Claude Code tmux pane.

Bot-relayed messages (text / photo / voice) get this prefix prepended before
`send_keys` so the JSONL transcript distinguishes them from text typed
directly in the tmux pane. The echo path back to Telegram strips the prefix
so users see clean output.

Slash commands forwarded to Claude Code (`/clear`, `/cost`, etc.) are NOT
tagged — they're CC's TUI commands handled locally and never reach the
JSONL transcript, and a leading `[from ccmux]` would also break CC's
`/`-prefix recognition.
"""

RELAY_TAG = "[from ccmux]"
RELAY_TAG_PREFIX = f"{RELAY_TAG} "


def tag_relayed(text: str) -> str:
    """Prepend the relay tag so Claude Code's JSONL records the source."""
    return f"{RELAY_TAG_PREFIX}{text}"


def strip_relay_tag(text: str) -> str:
    """Remove the relay tag if present (used when echoing back to Telegram)."""
    if text.startswith(RELAY_TAG_PREFIX):
        return text[len(RELAY_TAG_PREFIX) :]
    return text
