"""Pro window strings (spec-pro). The guided screens keep theirs in ``ui/hints.py``."""

WINDOW_TITLE = "Cheat Maker Pro"
OPEN_BUTTON = "Pro"
OPEN_BUTTON_TIP = "Open the Pro window: scanner, code and memory views, cheat editor."

# -- game header and liveness (AC-1.1, AC-1.2) ------------------------------------------
NOT_CONNECTED = "Not connected. Connect in the main window first."
NO_GAME = "No game. Start a game on the PS5, then press Refresh in the main window."
GAME_NOT_RUNNING = "Game not running. Start it again, then press Refresh in the main window."
GAME = "{title} · {title_id} · version {version} · PID {pid}"

# -- safety notice, once per game (spec "Safety text") -------------------------------------
NOTICE_TITLE = "Before you use Pro on this game"
NOTICE = (
    "Pro changes the game's memory and code directly.\n\n"
    "Use it only in single-player games played offline.\n\n"
    "Back up your save first: a wrong change can crash the game or damage the save."
)

TAB_SCANNER = "Scanner"
TAB_ADDRESSES = "Address list"
TAB_CODE = "Code"
TAB_MEMORY = "Memory"
TAB_WATCH = "Watch"
TAB_CHEATS = "Cheats"
WATCH_LATER = "Find what writes or accesses an address comes in a later update."
