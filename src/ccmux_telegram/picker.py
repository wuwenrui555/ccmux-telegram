"""Directory browser, tmux session/window pickers, and permission UI for session creation.

Provides UIs in Telegram for:
  - Tmux session picker: select or create a tmux session for unbound topics
  - Window picker: list windows within a tmux session for binding
  - Directory browser: navigate directory hierarchies to create new sessions
  - Permission picker: choose normal or skip-permissions mode

Key components:
  - DIRS_PER_PAGE: Number of directories shown per page
  - User state keys for tracking browse/picker session
  - build_tmux_session_picker: Build tmux session selector UI
  - build_window_picker: Build window picker UI within a session
  - build_directory_browser: Build directory browser UI
  - build_permission_picker: Build permission bypass selector UI
  - clear_*_state: Clear various picker states from user_data
"""

from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .config import config
from .callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_PERM_NORMAL,
    CB_PERM_SKIP,
    CB_TMUX_FILTER_ALL,
    CB_TMUX_FILTER_BOUND,
    CB_TMUX_FILTER_UNBOUND,
    CB_TMUX_SESSION_CANCEL,
    CB_TMUX_SESSION_NEW,
    CB_TMUX_SESSION_SELECT,
    CB_TMUX_STEAL,
    CB_TMUX_WIN_BIND,
    CB_TMUX_WIN_CANCEL,
    CB_TMUX_WIN_NEW,
)

# Directories per page in directory browser
DIRS_PER_PAGE = 6

# User state keys
STATE_KEY = "state"
STATE_BROWSING_DIRECTORY = "browsing_directory"
STATE_SELECTING_TMUX_SESSION = "selecting_tmux_session"
STATE_SELECTING_TMUX_WINDOW = "selecting_tmux_window"
STATE_SELECTING_PERMISSION = "selecting_permission"
STATE_AWAITING_SESSION_NAME = "awaiting_session_name"
BROWSE_PATH_KEY = "browse_path"
BROWSE_PAGE_KEY = "browse_page"
BROWSE_DIRS_KEY = "browse_dirs"
TMUX_SESSIONS_KEY = "tmux_sessions"
TMUX_WINDOWS_KEY = "tmux_windows"

# Cross-flow scratch keys carried in user_data. Kept centralized here so the
# string spellings live in one place; binding flows consume/clear them.
PENDING_THREAD_ID_KEY = "_pending_thread_id"
PENDING_THREAD_TEXT_KEY = "_pending_thread_text"
SELECTED_PATH_KEY = "_selected_path"
SESSION_NAME_KEY = "_session_name"
TMUX_FILTER_MODE_KEY = "_tmux_filter_mode"
STEAL_SESSION_NAME_KEY = "_steal_session_name"


def clear_browse_state(user_data: dict | None) -> None:
    """Clear directory browsing state keys from user_data."""
    if user_data is not None:
        for key in (STATE_KEY, BROWSE_PATH_KEY, BROWSE_PAGE_KEY, BROWSE_DIRS_KEY):
            user_data.pop(key, None)


def clear_tmux_session_picker_state(user_data: dict | None) -> None:
    """Clear tmux session picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(TMUX_SESSIONS_KEY, None)
        user_data.pop(TMUX_FILTER_MODE_KEY, None)
        user_data.pop(STEAL_SESSION_NAME_KEY, None)


def clear_tmux_window_picker_state(user_data: dict | None) -> None:
    """Clear tmux window picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(TMUX_WINDOWS_KEY, None)


def clear_permission_picker_state(user_data: dict | None) -> None:
    """Clear permission picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)


def build_tmux_session_picker(
    sessions: list[str],
    filter_mode: str = "unbound",
    bound_map: dict[str, int] | None = None,
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build tmux session picker UI with filter tabs.

    Args:
        sessions: Filtered list of session names to show in current view.
        filter_mode: Active tab — "all", "unbound", or "bound".
        bound_map: Mapping of session_name -> thread_id for bound sessions.
            Used to show which topic owns a bound session and to route
            button presses to the steal flow.

    Returns:
        (text, keyboard, sessions) where sessions is the same list passed in
        (retained for caller caching in user_data).
    """
    bound_map = bound_map or {}

    def _tab_label(text: str, active: str) -> str:
        return f"【{text}】" if filter_mode == active else text

    tab_row = [
        InlineKeyboardButton(
            _tab_label("📂 全部", "all"), callback_data=CB_TMUX_FILTER_ALL
        ),
        InlineKeyboardButton(
            _tab_label("🖥 未绑定", "unbound"), callback_data=CB_TMUX_FILTER_UNBOUND
        ),
        InlineKeyboardButton(
            _tab_label("🔒 已绑定", "bound"), callback_data=CB_TMUX_FILTER_BOUND
        ),
    ]

    lines = ["*Select tmux session:*"]
    if not sessions:
        lines.append("\n_(No sessions in this view)_")

    buttons: list[list[InlineKeyboardButton]] = [tab_row]
    for i, name in enumerate(sessions):
        display = name[:20] + "…" if len(name) > 20 else name
        if name in bound_map:
            cb = f"{CB_TMUX_STEAL}{i}"
            label = f"🔒 {display} → thread {bound_map[name]}"
        else:
            cb = f"{CB_TMUX_SESSION_SELECT}{i}"
            label = f"🖥 {display}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])

    buttons.append(
        [
            InlineKeyboardButton("➕ New Session", callback_data=CB_TMUX_SESSION_NEW),
            InlineKeyboardButton("Cancel", callback_data=CB_TMUX_SESSION_CANCEL),
        ]
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons), sessions


def build_window_picker(
    windows: list[tuple[str, str, str]],
    session_name: str,
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build window picker UI for windows within a tmux session."""
    window_ids = [wid for wid, _, _ in windows]

    lines = [f"*Select window in `{session_name}`:*\n"]
    for _wid, name, cwd in windows:
        display_cwd = cwd.replace(str(Path.home()), "~")
        lines.append(f"• `{name}` — {display_cwd}")

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(windows), 2):
        row = []
        for j in range(min(2, len(windows) - i)):
            name = windows[i + j][1]
            display = name[:12] + "…" if len(name) > 13 else name
            row.append(
                InlineKeyboardButton(
                    f"🖥 {display}", callback_data=f"{CB_TMUX_WIN_BIND}{i + j}"
                )
            )
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton("➕ New Window", callback_data=CB_TMUX_WIN_NEW),
            InlineKeyboardButton("Cancel", callback_data=CB_TMUX_WIN_CANCEL),
        ]
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons), window_ids


def build_directory_browser(
    current_path: str, page: int = 0
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build directory browser UI.

    Returns: (text, keyboard, subdirs) where subdirs is the full list for caching.
    """
    path = Path(current_path).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        path = Path.cwd()

    try:
        subdirs = sorted(
            [
                d.name
                for d in path.iterdir()
                if d.is_dir()
                and (config.show_hidden_dirs or not d.name.startswith("."))
            ]
        )
    except (PermissionError, OSError):
        subdirs = []

    total_pages = max(1, (len(subdirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DIRS_PER_PAGE
    page_dirs = subdirs[start : start + DIRS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(page_dirs), 2):
        row = []
        for j, name in enumerate(page_dirs[i : i + 2]):
            display = name[:12] + "…" if len(name) > 13 else name
            # Use global index (start + i + j) to avoid long dir names in callback_data
            idx = start + i + j
            row.append(
                InlineKeyboardButton(
                    f"📁 {display}", callback_data=f"{CB_DIR_SELECT}{idx}"
                )
            )
        buttons.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_DIR_PAGE}{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_DIR_PAGE}{page + 1}")
            )
        buttons.append(nav)

    action_row: list[InlineKeyboardButton] = []
    # Allow going up unless at filesystem root
    if path != path.parent:
        action_row.append(InlineKeyboardButton("..", callback_data=CB_DIR_UP))
    action_row.append(InlineKeyboardButton("Select", callback_data=CB_DIR_CONFIRM))
    action_row.append(InlineKeyboardButton("Cancel", callback_data=CB_DIR_CANCEL))
    buttons.append(action_row)

    display_path = str(path).replace(str(Path.home()), "~")
    if not subdirs:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\n_(No subdirectories)_"
    else:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\nTap a folder to enter, or select current directory"

    return text, InlineKeyboardMarkup(buttons), subdirs


def build_permission_picker() -> tuple[str, InlineKeyboardMarkup]:
    """Build permission bypass selector UI."""
    skip_default = config.dangerously_skip_permissions
    normal_label = "Normal mode" + (" (default)" if not skip_default else "")
    skip_label = "Skip permissions" + (" (default)" if skip_default else "")

    buttons = [
        [InlineKeyboardButton(f"🔒 {normal_label}", callback_data=CB_PERM_NORMAL)],
        [InlineKeyboardButton(f"⚡ {skip_label}", callback_data=CB_PERM_SKIP)],
    ]

    return "*Permission mode:*", InlineKeyboardMarkup(buttons)
