# Cheat Maker: Check which is real

Date: 2026-11-11. Kein picked this (idea 1) and then the smart cheat builder (idea 2) in the
thread "Show grouped results when scan says found it", and said "go", which is the OK for
the app to write test values on its own as long as it puts the originals back.

Generic for every game: no game-specific logic. It builds on the groups from
docs/spec-health-finding.md (AC-G1 to AC-G4), and works on a plain list too.

## Why

After grouping, the player still has to work out which address the game really uses.
Returnal (PPSA01285 03.000.005) showed why it matters: `0x12E0EA5C10` / `5C00` were
recalculated copies, and a Lock it on them hid the real health. Today that takes Set by
hand or Test the hits, which asks a question per round.

## How it tells them apart

- A copy (HUD, menu, a value worked out from another) is written by the game from the
  real value again and again, so a test value written to it is soon put back.
- The real value keeps a test value (nothing else writes it), and its linked copies
  follow it: they change to the test value times their usual factor.

## Acceptance criteria

### AC-R1 What is checked
- A **Check which is real** button in Find, enabled when a search is active and at least
  one match is listed.
- Grouped list: each group's members, best first, up to 8 per group and 64 in all.
  Matches that never changed, and addresses the app is freezing, are left out.
- Flat list (no groups yet): the listed matches, up to 16, as one set.

### AC-R2 The test value
- About 3/4 of the current value (lower, so a game that caps a value at its max doesn't
  put it back by itself). 0 and 1 (and 0.0) go up by 7 instead. It always fits the type.

### AC-R3 Rounds and verdicts
- One round writes one member of every group at once, waits 1 s while the game runs, then
  reads every member of those groups.
- **copy**: the address no longer holds the test value.
- **real**: it kept the test value and at least one other member moved from x to about
  x times (test / original), within 2% (whole numbers get 1 of slack).
- **kept the test value**: it kept it and nothing followed (a lone match, or a copy the
  game never refreshes).
- **not checked**: it couldn't be read or written, or it shares bytes with another
  address written in the same round.

### AC-R4 Putting things back
- After each round, every address that still holds the test value gets its original
  bytes back; where the game already wrote its own value, that value stays. Then a 0.5 s
  wait lets the copies follow before the next round.
- On an error (lost link) or Cancel, the same put-back runs for what was written.
  Cancel stops after the current round.

### AC-R5 How it shows
- In a group, the member found real (most followers; the first on a tie) becomes the
  group's row, so Set, Freeze and Make a cheat use it; those groups move to the top. The
  group row says **checked: real**, expanded rows say their own mark.
- In a flat list the mark is added to the value column.
- The hint says what was found: the real address and how many copies followed it; or
  that nothing followed; or that the game put every value back.
- The marks stay until Start over, a new game, or Find it again clears the search.

## Not in this spec
- Smart cheat builder (next PR).
- A setting for the 1 s wait (add one if console test C17d shows a game needs it).
