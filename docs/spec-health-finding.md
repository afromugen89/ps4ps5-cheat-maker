# Cheat Maker: easy health finding (percent search + grouping)

Date: 2026-11-07. Asked by Kein in the thread "Make health addresses easy to find".
Kein picked "Bar % search + grouping" first. The auto real-vs-copy check, the guide's
bar recipe and the smart cheat builder come later and are not in this spec.

Must stay generic for every game (no game-specific logic). Simple games stay simple: typing
a plain number and pressing Search works exactly as it does today.

## Why (evidence from Kein's Returnal scans 9-12, PPSA01285 03.000.005)

- Path used: unknown > 11 went-up/went-down scans, Decimal (4 bytes). Scan 9 still had
  16,881 matches, scan 12 had 59.
- Real health is a small block: `0x12E0EA40B0` health 90 > 65 > 40, `0x12E0EA40A0`
  (16 bytes before) the same as a fraction 0.9 > 0.65 > 0.4, `0x12E0EA40BC` health / 3.
  The other 0.4 values (for example `0x1121177F94`) are display copies.
- Returnal shows a health bar during play and an exact health percentage on the pause
  screen.
- Went up/went down alone keeps thousands of unrelated values (timers, physics). The
  percentage the game shows was never used.

## Goal

A player who can see a percentage, or only a bar, finds the address that holds a value
in a few scans, and the result list shows linked copies as one group, with the likely
real one marked.

## Part 1: percent search

### AC-P1 Typing a percent
- Given no active search, When the player types a number ending in `%` (for example
  `40%`, `40.5 %`) and presses Search, Then one search runs that keeps addresses holding
  that percent stored any of these ways: the percent itself (40), a fraction (0.4) or
  x10 (400).
- Types searched: Decimal (4 bytes) for all three; Whole number (4 bytes) for the percent
  and x10 when they are whole numbers. If the player picked a type, only that type.
- Tolerance follows AC-H2 on the typed text: `40%` = 40 ±1, 0.4 ±0.01, 400 ±10;
  `40.5%` = 40.5 ±0.1 and so on. Matches with value less than one unit away count.
- Allowed range 0 to 100 (inclusive). Outside that, or not a number: plain-English error,
  no scan.
- Later scans in the same search may also be typed as `N%` and use the same three ways.
  Each match stays tied to the way it matched (percent, fraction or x10), so a fraction
  hit is compared as a fraction next time.
- Search history shows `40%`. Scan files (PR #19) get a "Stored as" column.
- Rows that match fewer than 3 ways are fine; the empty ways are dropped (as PR #21 does
  for empty types).

### AC-P2 Mark "Full" and search by percent of full
For values that aren't out of 100 (max health 250, 1,700 shield and so on).
- Given no active search, When the player presses **"It's full now"**, Then an unknown
  first scan runs (FLOAT + INT32, as "I don't know the number" today) and the app
  remembers it as the Full point.
- Given a Full point, When the player types `N%` and presses Search, Then besides the
  three ways of AC-P1 the search keeps addresses whose value now is N% of their value at
  Full, within ±1 percentage point (`40%` keeps 39-41% of full).
- Values that were 0 at Full never match a percent-of-full scan.
- The Full point is kept until Start over. A second press of "It's full now" during a
  search re-marks Full (asks to confirm, since the list is not cut down by it).
- `[OPEN for architect]` PS5Debug-NG has no "percent of the snapshot value" compare, so
  this is done PC-side, which needs each candidate's Full value. Proposed: the first
  scan after Full runs on the console as plain "went down" or "went up" (whichever the
  percent says) and keeps the Full value from the snapshot rows; the percent filter
  then runs on the PC once the candidates fit in the limit (setting `percent_pc_limit`,
  default to be measured on console, test C15). Above the limit the hint says "Too many
  to check by percent yet. Take another hit or heal, then type the % again" and that scan
  is a plain went up/went down. Whether the snapshot rows carry the previous value is
  still unverified (C12).

### AC-P3 Only a bar, no number
- Given an active search with a Full point, the Find view also offers **About 75% /
  About 50% / About 25%** buttons. Each is a percent-of-full scan (AC-P2) with ±12.5
  percentage points instead of ±1, and also keeps fraction values in the same range.
  The plain percent / x10 ways are not used here.

### AC-P4 Guide
- When the player answers "A bar or meter" in the Find-it guide (AC-H5), the first hint
  becomes: "Fill it up, then press It's full now." After that: "Make it go down, then
  type the % if the game shows it anywhere (pause or stats screen), or pick About 75 / 50
  / 25%."
- When the player answers "A number" and types `N%`, the guide behaves as for any number.

## Part 2: group linked copies

### AC-G1 When groups appear
- Grouping runs when 2 to 500 matches are left (same limit as Test the hits) and the
  app has the value of every match from at least 3 recorded scans (with 2, any two values
  that both went down look "linked"). It needs no extra console
  work beyond the live read the list already does; from 500 matches down, the app keeps
  each match's value after every scan.

### AC-G2 What counts as one group
- Two matches are in the same group when, over every scan where both have values, one is
  always the other times the same number (within 1%). So 0.9/0.65/0.4 and 90/65/40 group
  together (x100); 30/21.67/13.33 joins them (x33.3); 267.4/21.2 do not.
- A match that never changed in the recorded scans isn't grouped (shown on its own, last).

### AC-G3 Likely real
- Within a group, a match is marked **"likely real"** when another group member, or any
  value that changed with it, sits within 64 bytes of it at a different scale (for
  example health next to its fraction). These are shown first, then ones with the most
  in-group neighbours.
- Groups whose members are all the same value and far apart (more than 64 KiB) are
  labelled "copies (probably display)".
- This is a hint only, never automatic deletion.

### AC-G4 How it looks
- The result list shows one row per group: the "likely real" address, its value, the way
  it's stored if known (percent, fraction, x10, plain), and "+N copies". Expanding the row
  shows every address.
- "Make a cheat", "Freeze" and "Test the hits" on a group row act on the likely-real
  address; on an expanded row, on that address.
- Groups are ordered: groups with a likely real first, then by size (smaller first).

## Measurable target

- `[OPEN: Kein to confirm]` Proposed: with Returnal (pause-screen %), from a full health
  bar to the real health address listed as "likely real" in no more than 4 scans and
  under 3 minutes, on console test C15. Baseline: 12 scans, 59 matches, no grouping.
- In tests (FakeConsole) with a recorded dump shaped like scan 12: the 59 matches show
  as at most 8 rows, with `0x12E0EA40B0` or `0x12E0EA40A0` marked likely real.

## Not in this spec

- Auto real-vs-copy check (writes to memory), guide bar recipe beyond AC-P4, smart cheat
  builder (Kein wants no script writing: the app should handle code injection itself).
- `[OPEN: Kein]` Finding the address again after a restart. Today the anchor hook and
  "Find it again" cover that; out of scope unless Kein says otherwise.
- `[OPEN: Kein]` May the app write test values on its own (needed later for the auto
  check and the smart cheat builder)?

## Console tests to add

- C15: Returnal, AC-P1 `N%` from a full bar, then AC-P2 with a non-100-max value in any
  game, measuring scans and time; record `percent_pc_limit` timing.
