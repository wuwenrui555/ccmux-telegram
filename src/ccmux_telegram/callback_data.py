"""Callback data constants for Telegram inline keyboards.

Defines all CB_* prefixes used for routing callback queries in the bot.
Each prefix identifies a specific action or navigation target.

Constants:
  - CB_HISTORY_*: History pagination
  - CB_DIR_*: Directory browser navigation
  - CB_WIN_*: Window picker (bind existing unbound window)
  - CB_ASK_*: Interactive UI navigation (arrows, enter, esc)
  - CB_TMUX_FILTER_*: Tmux session picker filter tabs
  - CB_TMUX_STEAL*: Steal a session bound to another topic
"""

# History pagination
CB_HISTORY_PREV = "hp:"  # history page older
CB_HISTORY_NEXT = "hn:"  # history page newer

# Directory browser
CB_DIR_SELECT = "db:sel:"
CB_DIR_UP = "db:up"
CB_DIR_CONFIRM = "db:confirm"
CB_DIR_CANCEL = "db:cancel"
CB_DIR_PAGE = "db:page:"

# Window picker (bind existing unbound window)
CB_WIN_BIND = "wb:sel:"  # wb:sel:<index>
CB_WIN_NEW = "wb:new"  # proceed to directory browser
CB_WIN_CANCEL = "wb:cancel"

# Interactive UI (aq: prefix kept for backward compatibility)
CB_ASK_UP = "aq:up:"  # aq:up:<window>
CB_ASK_DOWN = "aq:down:"  # aq:down:<window>
CB_ASK_LEFT = "aq:left:"  # aq:left:<window>
CB_ASK_RIGHT = "aq:right:"  # aq:right:<window>
CB_ASK_ESC = "aq:esc:"  # aq:esc:<window>
CB_ASK_ENTER = "aq:enter:"  # aq:enter:<window>
CB_ASK_SPACE = "aq:spc:"  # aq:spc:<window>
CB_ASK_TAB = "aq:tab:"  # aq:tab:<window>
CB_ASK_REFRESH = "aq:ref:"  # aq:ref:<window>

# Tmux session selector (new topic binding)
CB_TMUX_SESSION_SELECT = "ts:sel:"  # ts:sel:<index>
CB_TMUX_SESSION_NEW = "ts:new"
CB_TMUX_SESSION_CANCEL = "ts:cancel"

# Tmux session picker filter tabs
CB_TMUX_FILTER_ALL = "ts:f:all"
CB_TMUX_FILTER_UNBOUND = "ts:f:unbound"
CB_TMUX_FILTER_BOUND = "ts:f:bound"

# Tmux session steal flow (pick a session already bound to another topic)
CB_TMUX_STEAL = "ts:steal:"  # ts:steal:<index>
CB_TMUX_STEAL_CONFIRM = "ts:steal_ok"
CB_TMUX_STEAL_CANCEL = "ts:steal_no"

# Tmux window selector (within a session, for multi-window sessions)
CB_TMUX_WIN_BIND = "tw:sel:"  # tw:sel:<index>
CB_TMUX_WIN_NEW = "tw:new"  # create new Claude window in this session
CB_TMUX_WIN_CANCEL = "tw:cancel"

# Permission bypass selector (new session creation)
CB_PERM_NORMAL = "pm:norm"
CB_PERM_SKIP = "pm:skip"
