"""Constants for the live terminal monitor."""

NO_PROCESSES_MESSAGE = (
    "No running mongod or mongos processes found.\n"
    "Start MongoDB nodes first, then run: mrun monitor"
)
NO_MRUN_PROCESSES_MESSAGE = (
    "No running mongorun-managed MongoDB processes found.\n"
    "Start nodes with mrun first, or run: mrun monitor --all"
)
PROCESS_DISCOVERY_ERROR_MESSAGE = (
    "mrun monitor could not list local processes: %s"
)
AUTH_REQUIRED_STATUS = "auth required"
ROLE_PASSWORD_REQUIRED = "Password Required"

ANSI_RESET = "\033[0m"
ANSI_YELLOW = "\033[33m"
ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"
ANSI_TEAL = "\033[38;5;44m"
ANSI_DIM = "\033[2m"
ANSI_INVERSE = "\033[7m"
ANSI_BOLD = "\033[1m"
ANSI_DEFAULT = "\033[39m"
ANSI_ROLE_PRIMARY = "\033[38;5;71m"
ANSI_ROLE_SECONDARY = "\033[38;5;179m"
ANSI_ROLE_WARNING = "\033[38;5;203m"
ANSI_KEY_HINT = "\033[38;5;81m"
PANE_HEADER_COLORS = {
    "cpu": ANSI_TEAL,
    "memory": ANSI_GREEN,
    "network": ANSI_YELLOW,
    "disk": ANSI_RED,
    "logs": ANSI_TEAL,
    "disk status": ANSI_RED,
    "network status": ANSI_YELLOW,
    "storage subsystem": ANSI_GREEN,
    "other subsystems": ANSI_TEAL,
}
ANSI_PRETTY_KEY_DARK = "\033[38;5;81m"
ANSI_PRETTY_STRING_DARK = "\033[38;5;114m"
ANSI_PRETTY_NUMBER_DARK = "\033[38;5;215m"
ANSI_PRETTY_KEYWORD_DARK = "\033[38;5;141m"
ANSI_PRETTY_PUNCT_DARK = "\033[38;5;245m"
ANSI_PRETTY_KEY_LIGHT = "\033[38;5;25m"
ANSI_PRETTY_STRING_LIGHT = "\033[38;5;28m"
ANSI_PRETTY_NUMBER_LIGHT = "\033[38;5;130m"
ANSI_PRETTY_KEYWORD_LIGHT = "\033[38;5;90m"
ANSI_PRETTY_PUNCT_LIGHT = "\033[38;5;240m"
ANSI_SEARCH_HIT = "\033[33m\033[7m"
STYLE_SELECTED = "\x00selected\x00"
STYLE_YANKED = "\x00yanked\x00"
STYLE_SEVERITY_FATAL = "\x00severity:fatal\x00"
STYLE_SEVERITY_ERROR = "\x00severity:error\x00"
STYLE_SEVERITY_WARNING = "\x00severity:warning\x00"
STYLE_SEVERITY_INFO = "\x00severity:info\x00"
STYLE_SEVERITY_DEBUG = "\x00severity:debug\x00"
STYLE_TABLE_HEADER = "\x00table-header\x00"
STYLE_ROLE_PRIMARY = "\x00role:primary\x00"
STYLE_ROLE_SECONDARY = "\x00role:secondary\x00"
STYLE_ROLE_WARNING = "\x00role:warning\x00"
STYLE_ROLE_DIM = "\x00role:dim\x00"
REFRESH_INTERVALS = (1.0, 5.0, 10.0)
DEFAULT_CURRENT_OP_LIMIT = 10
MAX_CURRENT_OP_LIMIT = 500
ESCAPE_READ_TIMEOUT = 0.03
KEY_POLL_INTERVAL = 0.01
LOG_POLL_INTERVAL = 0.5
PANE_ORDER = ("cpu", "memory", "network", "disk", "logs")
PANE_TITLES = {
    "cpu": "CPU Usage",
    "memory": "Memory Usage",
    "network": "Network Usage",
    "disk": "Disk Usage",
    "logs": "Log Tail",
}
STATUS_DEFAULT_PANELS = ("disk", "network", "storage", "subsystems")
STATUS_PROMOTION_SEQUENCE = (1, 2, 0)
STATUS_SLOT_LABELS = {
    0: "top-left",
    1: "top-right",
    2: "bottom-left",
    3: "bottom-right",
}
STATUS_PANEL_TITLES = {
    "disk": "DISK STATUS",
    "network": "NETWORK STATUS",
    "storage": "STORAGE SUBSYSTEM",
    "subsystems": "OTHER SUBSYSTEMS",
}
STATUS_PANEL_COLORS = {
    "disk": "disk status",
    "network": "network status",
    "storage": "storage subsystem",
    "subsystems": "other subsystems",
}
STATUS_RAW_PANEL_PREFIX = "raw:"
STYLE_MARKERS = (
    STYLE_SELECTED,
    STYLE_YANKED,
    STYLE_SEVERITY_FATAL,
    STYLE_SEVERITY_ERROR,
    STYLE_SEVERITY_WARNING,
    STYLE_SEVERITY_INFO,
    STYLE_SEVERITY_DEBUG,
    STYLE_TABLE_HEADER,
    STYLE_ROLE_PRIMARY,
    STYLE_ROLE_SECONDARY,
    STYLE_ROLE_WARNING,
    STYLE_ROLE_DIM,
)
