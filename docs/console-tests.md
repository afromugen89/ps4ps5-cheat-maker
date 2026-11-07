# Console tests (need the real PS5)

Everything else is covered by the automated tests on the fake PS5. These checks
prove the app against the real console. Run them in order; each says what to send back.

Setup on the PC: install as in the README, then `python -m cheatmaker`.
Setup on the PS5: fw 13.60, load **PS5Debug-NG 1.3.2+**. For C1-C3 have one **PS5 game (PPSA)**
and one **PS4 game (CUSA)** ready. Back up your saves first.

| # | Test | Steps | Pass when | Send back |
|---|---|---|---|---|
| C1 (spike S1) | Connect and detect | Start a PS5 game. Type the PS5's IP, press Connect. Repeat with a PS4 game (press Refresh after switching). | "Connected" within 5 s; the top line shows the right title, `PPSA…`/`CUSA…`, version, and "PS4 game" for the PS4 one. | The whole top line for both games. Then run `python tools/record_dump.py <PS5 IP> dumps\ps5game --max-mb 16` (and `dumps\ps4game`) and send the two `manifest.json` files (no .bin files needed). |
| C2 | Not reachable | Close PS5Debug-NG (or type a wrong IP), press Connect. | Within 10 s: "Can't reach the PS5. Load PS5Debug-NG…". | Nothing if it passes. |
| C3 (spike S2) | Find, set, freeze | In each game: type your gold/ammo, press Search; spend some, type the new number, Search again, until ≤10 rows. Set it to 99999. Then Freeze ammo and shoot. | First search under 60 s; value changes in the game; frozen value doesn't drop. | How long the first search took in each game, and the match counts after each search. |
| C4 (spike S3) | Lock it | Make "Infinite Health" from a found health value. My cheats → select it → Lock it → Start → take damage within 30 s. Pick Option 1 → Test it → take damage. Allow the Windows Firewall prompt (port 755) if it appears. | One or more options appear; with Test it on, health stops dropping and the game keeps running. | Which option worked, and whether enemies also stopped taking damage. |
| C5 (spike S4) | Save and CheatRunner | Keep it → Save… → Save to PC. Load **ftpsrv**, press Send to PS5. Open CheatRunner (`http://<PS5 IP>:9999`). Restart the game and turn the cheat on in CheatRunner. Do it for the PS5 game and the PS4 game. | CheatRunner lists the file and cheat by name; it works after the restart. | The file name the app made (it uses the version the PS5 reports) and whether CheatRunner picked it up. |
| C6 | Lost connection | Freeze a value, then turn off the PS5's network (or pull the cable). | Within 5 s: "Lost connection to the PS5", every cheat unticked, app still running. | Nothing if it passes. |

If anything fails: press **Copy log** and paste it into the thread.

## Known gaps until these pass

- **"I don't know the number"** works on the fake PS5 only. On the real PS5 it says it isn't ready yet: the
  first unknown-value search must run on the console (architecture Q3), and the wire format for that
  mode needs C3 data first. See `docs/implementation-notes.md`.
- The **game version** comes from PS5Debug-NG's foreground-app command. If 1.3.2 doesn't have it, the app
  falls back to the process list and shows version "unknown"; C1 tells us which.
- The main module is found by the map name `eboot.bin`; otherwise the lowest executable map is used and logged. C1's
  `manifest.json` confirms the real names for PS5 and PS4 games.

## C7 Pointer scan (added 2026-11-01)

1. Find health (Float). With its row selected, press **Pointer scan**, keep Levels 3 and
   Max offset 0x1000, press **Find pointers**. Note the time and count (log:
   `action=pointer_level`, `action=pointer_pass`, `action=pointer_scan`).
2. Restart the game, find health again, select its row, open **Pointer scan**, press
   **Re-check with this address**. Expect a smaller list that still leads to health.
3. Repeat the restart + re-check once more, then **Use this pointer**: the row should
   show live health, and Set / Freeze should work.
4. After the next restart, open **Pointer scan** with no row selected and **Use this
   pointer** straight away: health should show without searching.

Pass: at least one pointer survives two restarts. Pointer cheats are live only; they
can't be saved into GoldHEN / CheatRunner files (those hold fixed byte patches).

## C8 Lock it, player only (added 2026-11-01)

1. Find health, make a cheat, Lock it -> Start, take a hit. Tick Advanced to see the
   instructions; for Returnal 03.000.005 the one to pick is `vmovss [r12+2ACh],xmm1`
   (offset 0x277B43F, see Celogamez's file).
2. Pick it, press **Player only...**, then **Collect** and fight for 20 s: get hit and hit
   enemies. Expect a field list with `+0x8: you 0x40048` near the top.
3. Keep the value (e.g. 999), **Test it**, take hits: health stays, enemies still die.
4. **Keep it**, Save, load in CheatRunner after a restart.

Log lines: `action=player_only_collect` (hook, cave, table addresses, objects count).
While collecting, the app briefly hooks the instruction and uses the last 512 bytes of
the game's data area as a table (only if they are zero; cleared afterwards). The small
collecting routine stays in the spare code bytes after unhooking (nothing jumps there
any more; a thread still inside it must not find garbage).

## C9 Cheats that survive a game update (v2, added 2026-11-01)

Part A, any time (reads only, writes nothing to the PS5):

1. Run Returnal 03.000.005, connect, **Open cheat file...** and pick Celogamez's
   `PPSA01285_03.000.005.json` (or any of your own saved files for this version).
2. The app learns a signature for each code line in the background. Check the log for
   `action=learn_signature offset=0x277b43f sig=C4 C1 7A 11 8C 24 AC 02 00 00 ...` and the
   file `%APPDATA%\CheatMaker\signatures\PPSA01285_03.000.005.json`.

Part B, after the next Returnal update (or with any game where you have an older file):

1. Run the new version, open the old file. The warning says "made for version ...", and
   an **Update for this version** button appears.
2. Press it. Expect "Updated N of M cheats for version ...". Cheats it couldn't move are
   listed with the reason.
3. Tick each moved cheat and test it in game, then **Save**. The file is named for the new
   version.

Log lines: `action=read_code`, `action=update_version from=... to=... moved=... failed=...`.
Without Part A's signatures, the app tries each line's original bytes alone (6+ bytes)
and lists those cheats as "found from their original bytes only (test them)".

## C10 Find it again (v3, added 2026-11-02)

Run C4, C7 and C8 first if you can; C10 does not depend on them, but their results
tell us which method to trust first. Use Returnal 03.000.005 (or any game whose value
moves after a restart). Allow the Windows Firewall prompt for port 755.

| # | Test | Steps | Pass when | Send back |
|---|---|---|---|---|
| C10a | Watch with registers (spike, no UI) | Find health (Decimal), note its address (shown in the Address column). Run `python tools/watch_regs.py <PS5 IP> <address> --seconds 10 --out c10a.json` and play (take a hit). | At least one **reader** is listed, and a line says `r12+offset == address: yes` (or another register). | `c10a.json` and the printed list. |
| C10b | Spare memory stays free | My cheats -> select health -> **Find it next time** -> Start, then keep playing for the whole check, including loading a new biome. For the long version: run it once more with `PROVE_SECONDS` set to 600 (ask in the thread). | "Ready: ... found by the game's code". Log: `action=slot_prove ... writes=0 zero=True`. | The `action=slot_prove` and `action=hook_plan` log lines. |
| C10c | Found again after a restart | After C10b, close the game, start it again, connect. | Within 10 s the status line says "... found again by the game's code"; tick the cheat, press Yes, health changes. No number typed. | The status line and `action=refind` log lines. |
| C10d | Hook loaded by CheatRunner | Save with **Also put the find-it-again hook in the file** ticked, Send to PS5, restart the game, turn the hook on in CheatRunner, then connect. | Found again as in C10c; log says `action=hook_install ... already in the game` (nothing patched twice). | Those log lines. |
| C10e | Fallback: memory search | After a restart and connect, in My cheats tick **Advanced: show details**, then **Find again without the game's code**, and press **Find it again**. | "found again by a memory search" in 3 min or less; time in `action=refind ... seconds=`. | The `action=vtable_scan` and `action=refind` lines. |
| C10f | Freeze across a biome and a death | Tick health (frozen), change biome, die once. | The freeze pauses (log `paused: target not verified`), then resumes within 5 s on the new character (`resumed`); no crash, no wrong value anywhere. | The `action=freeze` and `action=tracker` lines. |
| C10g | A PS4 game (CUSA) | Find a value that moves in a PS4 game, Find it next time, restart, connect. Also note the module base on the top line / log (`base=`). | Found again as in C10c. If the base isn't 0x400000, say so: the class check then uses the longer form (scratch register). | The `action=detect_game` and `action=hook_plan` lines. |
| C10 (AC-14.1) | Full proof | Health (Decimal) and Obolites (8-byte whole number): Find it next time for both, then 3 restarts, 2 biome changes, 1 death. | Both re-found every time; no wrong write. | Copy log. |

What is still unverified on the console (all behind fake-PS5 tests):
- Read-write watchpoints (breaktype 3) and the register block in the 0x4A0 trap
  packet (only RIP was confirmed in C4). C10a checks both.
- Writing the capture routine into the game's code and the 16 bytes into its spare
  data (same path as Lock it / Player only, C4/C8 not passed yet).
- That the end of the game's last data area is really unused (C10b).
- How long the memory search for the object's class takes on Returnal (expect about
  150 s, like the 8-byte search in C3).
- The 5-byte jump over the hooked instruction is written in one PS5Debug-NG write but
  not atomically: a game thread reaching that instruction in the same instant could
  run half-old, half-new bytes. Same risk as Lock it / Player only; watch for crashes
  right when the hook goes in (connect, Find it next time, ticking a cheat).
- That the game's module sits at a fixed base for PS4 (CUSA) games too, and that object
  classes (vtables) are in the module there (C10g).

## C11 Finding hard values (added 2026-11-03)

| # | Test | Steps | Pass when | Send back |
|---|---|---|---|---|
| C11a | Went up by / Went down by on the console | Find: **I don't know the number** (any game, Decimal). Take a hit you can read the size of (or spend a known amount). Type it in **How much** and press **Went down by**; then gain some and press **Went up by**. | The count drops each time and the real value is still in the list. Log `action=resident_next cmp=decreased by`. If the count goes to 0 every time, the PS5 may compare floats exactly: say so. | The `action=resident_next` lines. |
| C11b | Typed decimals and fraction of max | Pick a value that is a bar. Advanced > **Game stores it as: a fraction (0 to 1) of max**, max 100, type the percent you see, Search. Then type a decimal you see somewhere (e.g. 1.5) with Advanced off. | Both searches finish (the first takes about as long as an unknown search) and keep fewer matches than the same number without decimals. | The `action=scan_first` and `action=scan_next` lines. |
| C11c | Test the hits | Search a number until 2 to 500 matches are left (Returnal health or Obolites found several copies in C3). Save your game. Press **Test the hits**, Write it, answer each question. | It ends with one address; Set on that row changes the game. All other values look normal after the test (no copy left at the test value). | Copy log (`action=test_hits` lines). |

## C12 Scan files (added 2026-11-06)

| # | Test | Steps | Pass when | Send back |
|---|---|---|---|---|
| C12a | Files after an exact search | Any game. Search a number you see, then **Search again** with the new number after it changes. Press **Scan files**. | The folder has `scan-001.txt` and `scan-002.txt`; each lists hex addresses with the number you searched for. | The two files (or their first lines). |
| C12b | Files after an unknown search | **I don't know the number**, then **Went down**, then **Same**. Note the live list's values. | Three files. The first lists 100,000 addresses and says how many there were in all. In the last file the values match the live list (not the value before the change). The extra time per scan is small (log `action=scan_results n=... secs=...`). | The `action=scan_results` lines; say if any file shows the old value. |

## C13 Faster scanner (added 2026-11-07)

New speed settings in `settings.json` (app data folder): `scan_connections` (default 4:
connections that scan at once; 1 = the old single connection) and `fast_scan` (default
true: ask PS5Debug-NG to read the game's memory in place, `TS_USE_ALIASING` /
`TS_RESCAN_ALIASING`). Reconnect after changing them. C3 baseline: Returnal exact 4-byte
search 101.7 s (2,842 requests), 8-byte search 152.9 s.

| # | Test | Steps | Pass when | Send back |
|---|---|---|---|---|
| C13a | First search speed | Returnal (or any big game), same spot as C3. Search a 4-byte number, then an 8-byte number. | Both finish with about the same match counts as C3, faster than 101.7 s and 152.9 s. The game keeps running smoothly while it scans. | The `action=scan_stats` lines (requests, seconds, flags, connections). |
| C13b | Settings compared | Close the app, set `"fast_scan": false`, search the same 4-byte number. Then `"scan_connections": 1` (fast_scan back to true), search again. | All three runs find the same count. | The three `action=scan_stats` lines, so we keep the fastest default. |
| C13c | Next search with many matches | Search a common number (thousands of matches), change it in game, search the new number. Then press **Went down** on a list of a few thousand. | The next search takes seconds, not a full re-scan. Went down works on lists up to ~4,000 nearby groups instead of saying "too many". | The `action=scan_stats` / `action=scan_next` lines. |
| C13d | Unknown search narrowing | **I don't know the number**, then **Went down** and **Same** a few times. | Each narrowing is faster than before (compare with an old log if you have one). Counts behave as in C11a. | The `action=resident_next` lines with times. |
| C13e | No crash, no stuck connection | Do C13a twice in a row, cancel one search half way, then use Set and Freeze. | Nothing hangs; PS5Debug-NG doesn't need a restart. | Say if anything stopped answering. |

## C14 Unknown search over decimals and whole numbers (added 2026-11-07)

| # | Test | Steps | Pass when | Send back |
|---|---|---|---|---|
| C14a | Both kinds at once | Any game. **I don't know the number** with Advanced off. | The count shows decimals and whole numbers. Log `action=multi_type_snapshot counts={'FLOAT': ..., 'INT32': ...}`. Takes up to about twice an old unknown search. The whole-number snapshot is stored in full (no float filter), so it needs more room on the PS5; if it doesn't fit, the log says `action=multi_type_skip` and the search carries on with decimals only. | The `action=multi_type_snapshot` and `action=snapshot` lines. |
| C14b | Narrow to a whole number | Spend some of a whole-number value (gold, ammo). **Went down**, then type the new number and Search. | Only whole numbers are left, the real value is in the list, and Set changes it in game. | The `action=scan_next ... types=` lines. |
| C14c | Narrow to a bar | Start again; take damage on a health bar; **Went down**, **Same**, **Went down**. | The bar is still found (decimals), as before this change. | The `action=scan_next` lines. |

## C15 Percent search and groups (added 2026-11-07)

Spec: docs/spec-health-finding.md. Save your game first. Copy the log after each test.

| # | Test | Steps | Pass when | Send back |
|---|---|---|---|---|
| C15a | Percent from a full bar (Returnal) | Full health. Press **It's full now**. Take a hit, open the pause screen, type the health % it shows (`65%`), **Search**. Repeat with a second hit. Time it from the first press. | Health is listed as one row marked **likely real** (`0x...B0` health or `0x...A0` its fraction) in 4 scans or fewer and under 3 minutes (baseline: 12 scans, 59 matches, no groups). | Number of scans, minutes, the `action=scan_percent_pc` and `action=groups` lines, and the scan files' **Stored as** column. |
| C15b | Typed percent first | **Start over**. Type the pause-screen percent (`40%`) with Advanced off and press **Search**, then type the next percent after a hit. | Both finish; the log shows one `action=scan_part` line per way (percent, fraction, x10 for decimals; percent and x10 for whole numbers). Say how long the first search took and how many `INT32:x10` matches it kept: if that part is slow it can be dropped. | The `action=scan_percent` and `action=scan_part` lines. |
| C15c | Not out of 100, and the PC limit | Any game with a value whose max isn't 100 (shield 1,700, health 250). **It's full now**, lower it, type the % it shows or press **About 50%**. Note when the hint says "Too many to check by percent yet". | The value is found. Also tells us how long the PC check takes: log `action=scan_percent_pc ... checked=N` and its time, so we can set the `percent_pc_limit` default (now 20,000). | Those log lines with times, and whether a PC check over ~10,000 matches took more than 10 s. |
| C15d | Snapshot rows are last-scan values (C12b) | During C15a after a **Went down**, compare a scan file's values with the live list. | The file's values are the values when the scan ran (not the value before the change). The Full chain depends on this. | Say if any value is the old one. |
| C15e | A noted percent keeps everything on the PS5 | **Start over**, press **It's full now** (many matches), then type `100%` without changing anything. | The hint says the % was noted and the match count is the same as before (a range compare from -inf to inf keeps every decimal but NaN). | The count before and after, and the `action=resident_next` and `action=scan_percent_noted` lines. |
