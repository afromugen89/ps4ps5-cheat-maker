"""One-line "what to do next" hints for every screen (AC-8.2)."""

CONNECT = "Type your PS5's IP address (Settings > Network on the PS5) and press Connect."
NO_GAME = "Start a game on the PS5, then press Refresh."
FIND_START = "Type a number you see in the game (like your gold), then press Search."
FIND_UNKNOWN = "Now make the value change in the game, then press Went up, Went down or Same."
FIND_NARROW = "Change the number in the game (spend gold, take damage), then type the new number."
FIND_NONE = "Nothing found. Check the number and search again, or try Advanced > type."
FIND_FEW = (
    "Found it! Pick a row, then make a cheat, set it, or freeze it. If the game puts "
    "the number back, search again with Advanced > 8-byte whole number."
)
CHEATS_EMPTY = "No cheats yet. Find a value first, or open a cheat file."
CHEATS = "Tick a cheat to try it. When you're happy, press Save."
CHEATS_SESSION = (
    'Cheats marked "This session only" can\'t be saved. Press Lock it to make them stick.'
)
LOCK_READY = "Press Start, then make the value change in the game within 30 seconds."
LOCK_WATCHING = "Watching... make the value change in the game now."
LOCK_TESTING = (
    "Option {n} is on. Play a bit: if the value still changes, pick the next option and "
    "press Test it. If it stops, press Keep it."
)
LOCK_ALL_ON = (
    "All options are on. Take a hit: if the value stops changing, press Keep it (or test "
    "options one by one to find the single one that does it)."
)
LOCK_NOT_LANDED = (
    "The PS5 said OK but option {n} didn't change the game's code. Try the next option."
)
LOCK_PICK = "Pick an option and press Test it. If the game acts strangely, try another."
SAVE = "Pick a format and press Save to PC, or Send to PS5 if ftpsrv is running."
POINTER_START = (
    "Find pointers looks for paths from the game's fixed memory to this value. It runs "
    "one PS5 search per level, so 3 levels take a few minutes."
)
POINTER_NO_TARGET = (
    "These are the pointers saved for this game version. Pick one and press Use this "
    "pointer. To re-check them, find the value again and open Pointer scan from its row."
)
POINTER_RUNNING = "Searching... keep the game where it is (don't load a new area)."
POINTER_FOUND = (
    "Found some. Now restart the game, find the value again, and press Re-check with this "
    "address: the pointers that still lead to it are the ones that last."
)
POINTER_NONE = "No pointers found. Try more levels or a bigger max offset."
POINTER_RECHECKED = (
    "{kept} of {before} pointers still lead here. Pick one and press Use this pointer."
)
