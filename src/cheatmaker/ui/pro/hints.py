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

# -- Cheats tab and cave editor (PR-8) ------------------------------------------------------
CHEATS = "Pick a cheat. Guided cheats open read-only: press Edit in Pro to change them."
CHEATS_EMPTY = "No cheats yet. Press New cheat, or Import a cheat file."
GUIDED = "{name} (guided)"
GUIDED_PREVIEW = "Made in the guided screens. Press Edit in Pro to change it here."
ASSEMBLER_MISSING = (
    "Assembler not found: caves can't be assembled. Reinstall Cheat Maker, or set "
    "nasm_path in settings.json to your nasm.exe."
)
NEW_CHEAT_TITLE = "New cheat"
NEW_CHEAT_NAME = "Name of the cheat:"
HOOK_PROMPT = "Where to hook (e.g. eboot.bin+0x1234):"
NOP_PROMPT = "Which instruction to NOP out (e.g. eboot.bin+0x1234):"
NEW_CHEAT_NAME_DEFAULT = "New cheat"
TURN_OFF_FIRST = "Turn the cheat off first: its bytes are in the game right now."
LAST_ENTRY = "A cheat needs at least one entry. Delete the cheat in My cheats instead."
EDITED_IN_PRO = (
    "“{name}” was edited in Pro, so the guided screens can't change it. Open the Pro "
    "window to change it."
)
CAVE_SIZE = "Cave: {size} bytes · room for {room}"
CAVE_SIZE_UNKNOWN = "Cave: {size} bytes"
CAVE_TOO_BIG = "The cave needs {size} bytes, {room} free."
ASM_ERROR = "Line {line}: {message}"
ASSEMBLING = "Assembling..."
BUILT = "Built {name}: {size} bytes at {place}."
VAR_SAVED = "{name} is now {value}. It goes into the game when the cheat is turned on."
VAR_WRITTEN = "{name} is now {value} in the game."
VAR_NEEDS_BUILD = "{name} changed. Press Build to put it in the cave."
VAR_NEW = "value"
STALE = "A cave needs Build again before this cheat can be turned on."

# -- export and import (PR-12, PR-14) ---------------------------------------------------------
EXPORT_TITLE = "Export for CheatRunner"
EXPORT_COUNT = "{n} cheat(s) go in the file."
EXPORT_LEFT_OUT = "These need Cheat Maker running, so they are left out:"
EXPORT_NONE_LEFT_OUT = "Every cheat goes in the file."
MAKE_CAVE = "Make a cave at its writer"
MAKE_CAVE_LATER = "Needs the Watch tab, which comes in a later update."
EXPORT_SAVED = "Saved to {path}."
IMPORT_TITLE = "Import a cheat file"
IMPORT_REFUSED = "Not imported:"
IMPORT_NOTHING = "Nothing in this file can be imported."
CONVERT = "Convert to assembly"
CONVERTED = "{name}: {n} cave(s) are now assembly."
NO_CAVE = "{name} has no cave to convert."
IMPORT_ADD = "Add to my cheats"
IMPORTED = "Imported {n} cheat(s)."
