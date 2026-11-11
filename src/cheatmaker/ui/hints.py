"""One-line "what to do next" hints for every screen (AC-8.2)."""

CONNECT = "Type your PS5's IP address (Settings > Network on the PS5) and press Connect."
NO_GAME = "Start a game on the PS5, then press Refresh."
FIND_START = "Type a number you see in the game (like your gold), then press Search."
FIND_UNKNOWN = "Now make the value change in the game, then press Went up, Went down or Same."
FIND_NARROW = "Change the number in the game (spend gold, take damage), then type the new number."
FIND_STILL = "Don't touch the controller while this runs."
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
SEARCH_LOST = (
    "Your search was ended because Cheat Maker searched memory to find a value again. "
    "Press Search to start it again."
)
OLD_NOTES = (
    "These notes come from an older Cheat Maker, so the game's code no longer helps "
    "find your values. Press Find it next time once more to make it fast again."
)
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
PC_OFF_TIP = (
    "Makes a cheat from the same place in the game's code that keeps your value (only "
    "yours) and can be saved for CheatRunner, so it works after a restart without the PC."
)
PC_OFF_MADE = 'Made "{name}". Tick it to test it, then Save: it works with the PC off.'
PATH_LOST_HOOK = (
    "{name} moved, so Cheat Maker now uses the game's code to follow it. Your cheat "
    "goes on as soon as it's found again."
)
PATH_LOST = "{name} moved and Cheat Maker stopped changing it. Press Find it again."
KEPT_FIRST = "OK, Cheat Maker keeps changing the {name} it had before."
CONFIRM_MOVED = (
    "{name} moved (a new area?). Cheat Maker can't be sure it found the right one, so "
    "it stopped changing it. It shows {value} now.\n\nTake a hit and watch this number. "
    "Press Yes only if it changes with your {name}."
)

# -- combined cheats that survive restarts (spec-restart-combined) ---------------------
PART_SECONDS = 70  # one part: watch 10 s, check the spare memory 60 s
FIND_NEXT_START_PARTS = (
    "Find it next time: press Start and play normally (take a hit if you can). Cheat "
    "Maker learns how the game keeps {parts}, one after another: about {secs} s per "
    "part, about {total} s in all (longer with a memory path, which is looked for for "
    "every part). Then it finds {name} again by itself after a restart or a new area."
)
FIND_NEXT_PART = "Part {k} of {n}: {part}."
READY_PART = {
    "static": "{name} doesn't move",
    "hook_slot": "{name} will be found by the game's code",
    "chain": "{name} will be found by a memory path",
    "vtable_scan": "{name} will be found by a memory search (up to 3 minutes)",
}
READY_PARTS = "Ready: {parts}."
LEARN_PART_FAILED = "Couldn't learn {part} (part {k} of {n}): {reason} Nothing was changed."
LEARN_PART_CANCELLED = "you pressed Cancel."
COMBINED_FOUND = "Ready: {name} found again ({parts}, {secs:.1f} s). Tick it to use it."
PART_COUNT = {True: "{n} part", False: "{n} parts"}  # keyed by "just one"
COMBINE_NEEDS_NOTES = (
    "Connect to the game first: cheats found again each run can only be combined while "
    "Cheat Maker has the game's notes."
)
COMBINED_NOT_FOUND = (
    "Couldn't find {part} (part of {name}) this time. Delete {name} and make it again, "
    "or search for {part}."
)
PART_OF = "{part} (part of {name})"
CONFIRM_PARTS = (
    "Is this your {name}? {values} right now.\n\n"
    "Take a hit and watch these numbers change. Press Yes only if they do."
)
CONFIRM_PART_VALUE = "{part} shows {value}"
TEST_HITS_WARNING = (
    "This writes to the game's memory. Save your game first; a wrong address can crash it."
)
TEST_HITS_START = (
    "Press Write it. Half of the candidates get the test value for a moment, then you say "
    "whether the game showed it."
)
TEST_HITS_ASK = "Did the number in the game change to {value}? (Open the menu or look at the HUD.)"
TEST_HITS_NEXT = "Values put back. Press Write it for the next question."
TEST_HITS_FOUND = (
    "Found it at {addr:#x}. It's in the Find list now: set it, freeze it or make a cheat."
)
TEST_HITS_STOPPING = "Stopping: putting the values back first..."
TEST_HITS_STOPPED = "Stopped. Every value was put back."
FIND_TEST_HITS = "Not sure which row is real? Press Check which is real, or Test the hits."

# -- percent search and groups (spec-health-finding) ----------------------------------
FIND_PERCENT_TOO_MANY = (
    "Too many to check by percent yet. Take another hit or heal, then type the % again."
)
FIND_PERCENT_NOTED = (
    "Noted, nothing dropped: Cheat Maker needs to see the % change from the last one. "
    "Take a hit or heal, then type the new % (or pick About 75 / 50 / 25%)."
)
TEST_HITS_RAW = (
    "These keep the number as their own amount, not as a percent: type the test value "
    "as that amount (the list shows it)."
)
FIND_FULL_CONFIRM = (
    "Mark it full again? Keep it full and don't touch the controller: Cheat Maker runs a "
    "Same scan, which drops values that changed."
)
FIND_FEW_GROUPING = (
    "Almost there. To fold copies of the same value into groups, make the value change "
    "{n} more time{s} in the game, then search again (type the new number, or press Went "
    "up / Went down). Same doesn't help here: copies only show when they change together."
)
FIND_GROUPS = (
    'Rows that change together are one group. "likely real" sits next to the same value '
    "at another scale; press the arrow to see every copy."
)

# -- check which is real (spec-real-check) ----------------------------------------------
CHECK_REAL_TIP = (
    "Writes a test value to each listed match for about a second, then puts it back. "
    "Copies get put back by the game; the real one keeps it and its copies follow."
)
CHECK_REAL_RUNNING = (
    "Checking... keep playing (don't pause) and keep the value steady for a few seconds. "
    "Each match gets a test value for about a second, then its own value back."
)
CHECK_REAL_FOUND = (
    "Found the real one at {addr}: {n} {copies} followed it. It's at the top of the "
    "list: make a cheat, set it, or freeze it."
)
CHECK_REAL_KEPT = (
    'Nothing followed a test value, so no copies were found. Rows marked "kept the test '
    'value" are the best bets: try Set or Freeze on them.'
)
CHECK_REAL_NONE = (
    "The game put every test value back, so these look like copies of a value that isn't "
    "listed. Search again, or try Advanced > another number type."
)
CHECK_REAL_STOPPED = "Stopped. Everything that was checked has its own value back."
CHECK_REAL_FAILED = (
    "Couldn't check these: they couldn't be read or written right now. Search again, "
    "then try once more."
)
CHECK_REAL_STOPPING = "Stopping after this round: putting the values back first..."
CHECK_REAL_NOT_RESTORED = (
    "The connection dropped before these got their own value back: {addrs}. Reload a "
    "save or restart the game if a number looks wrong."
)

# -- Make it work (spec-cheat-builder) ---------------------------------------------------
BUILD_BUTTON = "Make it work..."
BUILD_BUTTON_TIP = (
    "Let Cheat Maker find the game code that changes this value, try safe fixes one by "
    "one, and keep the one that works"
)
BUILD_PICK_SINGLE = "Pick a single value."
BUILD_READY = "Pick what the cheat should do, then press Start."
BUILD_FILL_UP = "Fill it up, then press Start."
BUILD_NOTICE_TITLE = "Save your game first"
BUILD_NOTICE = "Save your game first. The game may restart if a fix doesn't work."
BUILD_NOTICE_HIDE = "Don't show this again"
BUILD_CHECKING = "Checking which one is the real value..."
BUILD_WATCHING = "Play and make it change (take a hit) within 30 seconds."
BUILD_COLLECTING = "Get hit and hit enemies for 20 seconds."
BUILD_FINISHING = "It works! Keeping the fix..."
BUILD_DONE = 'Done: "{name}" is in My cheats and on. Works after a restart: {after}.'
BUILD_TRY_ONLY_ME = "If enemies stop dying, try again with Only me."
BUILD_CANCELLED = "Stopped. Your game is unchanged."
BUILD_FIX_FAILED = "Fix {n} didn't work ({reason}). Trying the next one."
BUILD_NONE_WORKED = "None of the safe fixes worked for this value. Your game is unchanged."
BUILD_FULL_LABEL = "Full:"
BUILD_FULL_BAD = "Type the Full number first."
BUILD_ONLY_ME = "Only me"
BUILD_ONLY_ME_TIP = "Change only your character; enemies go through the game's own code"
BUILD_ADVANCED = "Advanced: show details"
BUILD_ADV_WRITER = "Writer {addr}: {text} (changed it {hits} times)"
BUILD_ADV_SKIPPED = "Skipped {addr}: {text} - {reason}"
BUILD_ADV_TRIED = "Fix {n}: {label} - {result}"
BUILD_Q_TITLE = "Make it work"
BUILD_Q_ENEMIES_HINT = "Not sure counts as No: the fix is put back."
