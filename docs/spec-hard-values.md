# Cheat Maker: finding hard values (Phase 1 + Phase 2)

Date: 2026-11-03. Asked by Kein in thread "Improve finding hard initial values"; Kein chose
"Phase 1 then Phase 2". Strategy doc: https://claude.ai/artifact/JdNCQuGxFaSzJ9bpVEaWqy

Must stay generic for every game (no game-specific logic). Simple games must stay simple:
the default path (type the number, Search) does not change.

Note: strategy item #5 (pointer scan keeps first 20k nodes) is already fixed on main
(`pointers.keep_nearest`), so it is dropped.

## Phase 1: sharper searches

### AC-H1 Changed by an amount
- Given an active search, When the player types an amount N and presses "Went up by" or
  "Went down by", Then only matches whose value changed by N since the last scan are kept.
- Whole numbers: exact difference. Decimals: |(cur - prev) - N| < tolerance of N (AC-H2).
- N must be > 0; a negative or empty amount gives a plain-English error, no scan.
- New `Compare.INCREASED_BY` / `Compare.DECREASED_BY` (needs_value and needs_previous).
- PC-side compare (`values.matches`, local scan, FakeConsole) and the NG console-side
  snapshot (server compare codes IncreasedBy = 6, DecreasedBy = 8; UNVERIFIED on console,
  console test C11a). On the classic (non-resident) NG path with too many candidates, the
  same "too many to compare" message as today.

### AC-H2 Tolerance that fits the number
- Exact float search tolerance = one unit of the last digit typed: "87" -> 1.0,
  "87.5" -> 0.1, "0.87" -> 0.01. A match is |memory - typed| < unit.
- Replaces the fixed 0.5 in `values.matches` and in `parse_filter` (filter box).
- Console: whole typed numbers keep NG FuzzyValue (< 1.0, same rule). Typed decimals use
  NG ValueBetween [typed - unit, typed + unit] (console test C11b).
- The unit is derived from the text the player typed, so `parse_number` (or a helper next
  to it) must report the number of decimals.

### AC-H3 Scaled values
- A "Game stores it as" choice in the Find view's advanced row: "the number shown"
  (default), "a fraction (0 to 1) of max", "x10", "x100", "x1000". Max defaults to 100
  and is editable when the fraction option is picked.
- The scale applies to every typed number of that search: first exact search, later exact
  scans and changed-by amounts. Fraction always searches Decimal (4 bytes) unless the
  player picked a type.
- The tolerance unit scales too (87 of 100 as a fraction -> 0.87 with unit 0.01).
- The scale is shown in the search history and kept until Start over.

### AC-H4 Stand still sweep
- With an active search, a "Stand still" button runs an Unchanged scan every 2 s,
  5 times by default, with progress and Cancel.
- Stops early when the count is unchanged for 2 scans in a row, or hits 0 or 1.
- Hint text: "Don't touch the controller while this runs."

## Phase 2: guidance

### AC-H5 Find-it guide
- Pure core logic in `core/guide.py` (no Qt), shown by the Find view as one hint line plus
  one highlighted suggested button.
- First question: "What does the game show?" -> A number / A bar or meter / Nothing.
  - A number: suggest typing it and Search.
  - A bar or meter / Nothing: suggest "I don't know the number" (unknown first scan).
- After every scan the guide suggests the next step:
  - 0 matches after an exact search: next fallback in this order, skipping ones already
    tried for this value: other types (as today), fraction of 100, x10, x100, then
    "I don't know the number". The hint names what will be tried.
  - 0 matches after a compare: "The value may be stored another way. Start over and try
    <next fallback>."
  - More than the show limit: for a number, "Change it in the game, then type the new
    number" (or "Went up by / down by" if the player knows the amount); for a bar or
    nothing, "Make it go up or down in the game, then press Went up / Went down", and
    "Stand still" after two compare scans in a row.
  - 2 to 500 matches and some still change together: suggest "Test the hits" (AC-H6).
  - 1 match: suggest "Make a cheat".
- The player can ignore the guide; every button keeps working as today.

### AC-H6 Test the hits
- Available when 2 to 500 matches are left (`TEST_LIMIT = 500`).
- Core logic in `core/hit_tester.py` (no Qt): holds candidate addresses + type; each round
  splits the candidates into halves A and B, writes a test value to every address in A
  once (not frozen), remembering the original bytes.
- The player is asked "Did the number in the game change to <value>?" Yes -> keep A,
  No -> keep B, "Not sure" -> put values back, end with no result.
- After every answer the original bytes of A are written back.
- Ends when 1 candidate remains (about log2(n) rounds; 500 -> 9). The winner is added to
  the Find list the same way "Add address" does, selected, ready for Set / Freeze / Make a
  cheat.
- Test value: prefilled, editable. Whole numbers: current value of the first candidate
  + 11 (clamped to the type). Decimals: current x 0.5, or + 11 if current is 0.
- A lost connection or a failed write puts back what it can, then shows the usual
  connection message.
- Warning shown once before the first round: "This writes to the game's memory. Save
  your game first; a wrong address can crash it."

## Console tests to add (docs/console-tests.md)
- C11a Went up by / Went down by on an unknown snapshot search.
- C11b Decimal typed search (e.g. 0.87 as fraction) on the console.
- C11c Test the hits narrows a real game's 2+ matches to the real value.

## Out of scope
- Encoded values (Phase 3, after C10 passes). Percent change searches.
