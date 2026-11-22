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

# -- Watch (PR-3) -----------------------------------------------------------------------------
WATCH = (
    "Add up to 4 addresses, pick Writes or Accesses, then Start and play until the value "
    "changes. Each instruction that touched it is listed with how often. The game pauses "
    "briefly at every hit."
)
WATCH_OWNER = "Pro's Watch tab"
WATCH_KINDS = {"write": "Writes", "access": "Accesses", "execute": "Runs"}
WATCH_FULL = "Watch at most 4 addresses at a time. Remove one first."
WATCH_NONE = "Add an address to watch first."
WATCH_BUSY = "A watch is running. Stop it first."
WATCH_STARTING = "Starting the watch (the PS5 connects back on port 755)..."
WATCH_RUNNING = "Watching: {hits} hit(s) · {left} s left"
WATCH_ENDED = {
    "time": "Time's up: {hits} hit(s).",
    "hits": "Stopped at {hits} hits (the limit).",
    "stopped": "Stopped: {hits} hit(s).",
    "lost": "The watch ended: {reason} The list shows the hits so far.",
    "failed": "The watch couldn't go on: {reason}",
    "repeats": "{reason}",
}
WATCH_GAME_GONE = "The game isn't running, so the watch ended. Nothing was written."
WATCH_PICK_ROW = "Pick an instruction in the list first."
WATCH_NOT_MODULE = "That instruction isn't in the game's own code, so no cheat can patch it."
REGS_UNVERIFIED = " (unverified)"
REGS_UNVERIFIED_TIP = (
    "Registers other than RIP aren't proven on PS5Debug-NG yet (console test S-P1). Check "
    "them before you rely on them."
)
SHOW_CODE = "Show in Code"
ACCESS = (
    "What does an instruction access? Type where it is (e.g. eboot.bin+0x1234) and press "
    "Run: for {seconds} s every address it reads or writes is listed. The game keeps "
    "running."
)
ACCESS_BUTTON = "What does it access?"
ACCESS_RUNNING = "Sampling: {n} address(es) so far..."
ACCESS_DONE = "{n} address(es) seen in {polls} look(s)."
ACCESS_STOPPED = "Stopped: {n} address(es) seen."
ACCESS_SITE_CHANGED = (
    "The instruction was changed by another cheat while sampling, so it was left as "
    "it is; the sampler's jump may remain until the game restarts."
)
ACCESS_WATCH_TITLE = "No room for the sampler"
ACCESS_WATCH_ASK = (
    "{reason}\n\nWatch the instruction instead? The game pauses briefly each time it runs."
)
ACCESS_WATCHED = (
    "{n} address(es) from {hits} pass(es), worked out from the registers (unverified until "
    "console test S-P1)."
)
ACCESS_ADD = "Add to address list"
ACCESS_BUSY = "A sample is running. Stop it first."
WATCH_SIZE_TIP = "How many bytes to watch: the value's size."

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
MAKE_CAVE_TIP = (
    "Watch what writes this value, then pick the writer and press Make cave entry here: a "
    "cave at the writer works in CheatRunner."
)
MAKE_CAVE_CANT = "Cheat Maker can't tell where this value is right now, so it can't be watched."
EXPORT_SAVED = "Saved to {path}."
IMPORT_TITLE = "Import a cheat file"
IMPORT_REFUSED = "Not imported:"
IMPORT_NOTHING = "Nothing in this file can be imported."
CONVERT = "Convert to assembly"
CONVERTED = "{name}: {n} cave(s) are now assembly."
NO_CAVE = "{name} has no cave to convert."
IMPORT_ADD = "Add to my cheats"
IMPORTED = "Imported {n} cheat(s)."

# -- Code view (PR-6) ---------------------------------------------------------------------------
CODE = "Type an address (e.g. eboot.bin+0x1234) and press Go. Double-click a jump to follow it."
SHOW_ORIGINAL = "Show original"
SHOW_ORIGINAL_TIP = "Show the game's own bytes where cheats that are on changed them."
AOB_COPIED = "Copied: {aob}"
CODE_MAKE_NOP = "Make NOP entry"
CODE_MAKE_CAVE = "Make cave entry"
MAKE_ENTRY_TIP = "Add it to the Pro cheat picked in the Cheats tab, or make a new cheat."

# -- Memory view (PR-7, read-only) ----------------------------------------------------------------
MEMORY = "Type an address and press Go. ?? marks memory the game doesn't have. Read-only for now."
MEMORY_SELECTED = "At {place}:"

# -- Scanner and address list (PR-2) -----------------------------------------------------------
SCANNER = (
    "Pick where to look and the number type, then First scan. Change the value in the game "
    "and Next scan until few are left. Double-click a result to add it to the address list."
)
SCAN_COUNT = "{n:,} found"
SCAN_PAGE = "Page {page} of {pages}"
SCAN_PROGRESS = "{pct}% · {left}"
SCAN_CANCELLED = "Search cancelled."
AOB_FOUND = "{n} place(s) match in the game's code."
AOB_TIP = "Bytes as hex pairs, ?? for any byte, e.g. C4 C1 7A 11 ?? 24"
ADDRESSES = "Addresses you keep. Type in Value to set it once; tick Freeze to keep it there."
ADD_ADDRESS = "Where (e.g. 0x20000040 or eboot.bin+0x4010):"
ADDED = "Added {n} address(es) to the address list."
POINTER_FOUND = "Pointer path for {place}: {chain}"
FIND_WRITES = "Find what writes"
FIND_ACCESSES = "Find what accesses"

# -- messages (the Pro window's status line) -------------------------------------------------
WRITE_FAILED = "The PS5 didn't take the new bytes ({reason}). Check the game is running."
SITE_CHANGED_TITLE = "The game's code isn't what the cheat expects"
SITE_CHANGED = "Nothing was written. These places hold other bytes than recorded:"
SITE_ROW = "{place}\n    expected: {expected}\n    found:    {found}"
CLOSE_FROZEN_TITLE = "Stop the freezes?"
CLOSE_FROZEN = "Closing Pro stops the {n} frozen address(es) in the address list. Close it?"
GUIDED_BUSY = "The main window is busy (a search or a read is running). Try again when it's done."
CONVERT_FAILED = "{name} can't be converted: the assembler failed ({reason})."
TURNED_ON_MEANWHILE = (
    "{name} was turned on meanwhile, so the result wasn't kept. Turn it off, then try again."
)
ALREADY_ON = "{name} was already on in the game."
FREEZE_NOT_IN_PRO = (
    "A freeze cheat can't be edited in Pro. Use Lock it or Make it work on it first."
)
