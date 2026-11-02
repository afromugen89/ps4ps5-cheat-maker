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
    'Cheats marked "This session only" can\'t be saved. Press Lock it to make them stick, '
    "or Find it next time so Cheat Maker finds them again after a restart."
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
PLAYER_ONLY_START = (
    "Player only: press Collect, then fight for 20 seconds: get hit AND hit enemies. The app "
    "notes which characters this instruction changes, then finds what marks you."
)
PLAYER_ONLY_FIGHT = "Collecting... get hit and hit some enemies now."
PLAYER_ONLY_PICK = (
    "Pick the field that marks you (the top one is the best guess), check the value, and "
    "press Test it. Then take a hit: your value should stay, enemies should still die."
)
PLAYER_ONLY_NONE = "No field tells you apart from the others. Collect again with more enemies."
PLAYER_ONLY_TESTING = (
    "On. Take a hit: your value should stay while enemies still lose health. If enemies "
    "stop dying too, pick the next field and Test it again. If it works, press Keep it."
)
PLAYER_ONLY_NOT_LANDED = "The PS5 said OK but the code didn't change. Try again."

UPDATE_RUNNING = "Reading the game's code to find your cheats in this version..."
UPDATE_DONE = (
    "Cheats moved to this version. Tick each one to test it in game, then press Save: "
    "the file is named for this version, so CheatRunner picks it up."
)

# -- v3: find it again ---------------------------------------------------------------
FIND_NEXT_START = (
    "Find it next time: press Start and play normally for about a minute (take a hit if "
    "you can). Cheat Maker learns how the game keeps {name}, so it can find it again by "
    "itself after a restart or a new area."
)
FIND_NEXT_STEPS = {
    "watch": "Watching which part of the game uses {name}... play normally, take a hit.",
    "collect": "Checking which characters that part of the game uses... keep playing.",
    "prove": (
        "Checking that the spare memory Cheat Maker wants is really free... keep playing "
        "(load a new area if you can)."
    ),
    "pointers": "Looking for a memory path to {name}... keep the game where it is.",
}
FIND_NEXT_POINTERS = "Also look for a memory path (slower: can take minutes on big games)"
READY = {
    "static": "Ready: {name} doesn't move, so it works after a restart as it is.",
    "hook_slot": "Ready: {name} will be found by the game's code.",
    "chain": "Ready: {name} will be found by a memory path.",
    "vtable_scan": "Ready: {name} will be found by a memory search (up to 3 minutes).",
}
HOOK_NOTE = (
    "While Cheat Maker is connected it adds a small hook to the game's code to find "
    "{name} again. It's taken out when you disconnect or close Cheat Maker."
)
FINDING = "Finding your values again..."
FOUND_AGAIN = "Ready: {name} found again by {how} ({secs:.1f} s). Tick it to use it."
NOT_FOUND = (
    "Couldn't find {name} this time. Search for it again (the type and last value are "
    "filled in), then press Find it next time to update it."
)
CONFIRM = (
    "Is this your {name}? It shows {value} right now.\n\n"
    "Take a hit (or change it in the game) and watch this number change. Press Yes only "
    "if it does."
)
CONFIRM_NO = "OK, nothing was changed. Press Find it again, or search for {name} again."
PICK = (
    "Cheat Maker found {name} in {n} places and won't change any of them until you pick. "
    "Make the value change in the game, then pick the row that changes."
)
SAVE_APP_ONLY = (
    "{n} cheat(s) work only while Cheat Maker is connected. They're kept in Cheat Maker's "
    "notes for this game and come back next time."
)
SAVE_HOOK = "Also put the find-it-again hook in the file (ready before the PC connects)"
