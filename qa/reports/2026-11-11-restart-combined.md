# QA report: Combined cheats that survive restarts

- Date: 2026-11-11
- Branch / commit under test: `claude/project-thread-spmgbz` @ `78ec57c` (diff vs `main`)
- Spec: docs/spec-restart-combined.md (AC-1..AC-8). Design: docs/architecture-restart-combined.md
- Environment: Linux 6.18, Python 3.13.18, PySide6 6.12.0, Qt offscreen, fake console (`tests/fakegame.py`)
- Automated tests: `tests/qa/test_qa_restart_combined.py` (40 pass, 5 strict xfail = open bugs)

## Product owner summary

I tested combining cheats of every kind (fixed, moving, found-again), learning a combined
cheat in one **Start** (including failures and Cancel at each stage), the next run (one
row, one question, a missing part), per-part freezing, unticking, deleting, the notes
file (formats 1/2/3, and a format 3 file opened by the current `main` code), cheat-file
exclusion, and the disabled Lock it / Make it work buttons.

The core of the feature works: the Returnal-style two-part health is learned in one Start,
comes back as one row, asks one question, writes both parts, and pauses/resumes one part on
its own. Rollback after a failure or Cancel works for all the normal paths (watch, prove,
install).

Bugs: **0 Critical, 2 High, 1 Medium, 2 Low.**

- BUG-01 (High): the headline flow (found-again health + new fraction, then Find it next
  time) puts fraction in a **second object with a second hook**, even though both values sit
  in the same game object. The spec says they should share one.
- BUG-02 (High): combining found-again cheats **while not connected** isn't saved. Next run
  the old single cheat comes back as its own row and the combined cheat is gone.
- BUG-03 (Medium): a fixed (module) part and a session part on the same byte are not
  refused as overlapping.
- BUG-04 (Low): an unexpected error (not a Cheat Maker error) while learning skips the
  rollback of the reserved spare rooms.
- BUG-05 (Low): the status line says "1 parts".

Release recommendation: **Ready with known issues.** Fix BUG-01 and BUG-02 before the C19
console run if you can. BUG-01 changes what C19 is meant to show ("both parts in one
object"). BUG-02 loses a cheat silently. Both have workarounds: learn both values as new
moving parts, and combine only while connected.

## Scope

In scope: AC-1 Combine, AC-2 Find it next time on a combined cheat, AC-3 next run, AC-4 while
on, AC-5 notes format 3, AC-6 unchanged behaviour and exclusions, AC-7/AC-8 docs (review
only). Out of scope: a real PS5 (the C19 console test), real timing (20 s find and 2 s
cancel can only be checked by logic on the fake, which doesn't wait), and concurrency on
the real freezer thread.

## Test plan and results

| TC-ID | Title | Priority | Result | Notes |
|---|---|---|---|---|
| TC-01 | Combine fixed + moving + learned: names, number types, kinds, keeps "This session only", learn on, source notes entry removed, objects kept | High | Pass | `test_tc01_*` |
| TC-02 | Learn the mixed cheat: only the moving part is learned; format-3 entry has learned+learned+fixed in order; restart: all 3 written; untick restores the fixed byte, leaves learned | High | Pass | `test_tc02_*` |
| TC-03 | All-fixed combined: "Yes", saveable, no notes entry, learn off | Medium | Pass | `test_tc03_*` |
| TC-04 | Limit of 4 moving parts: 5 refused with the exact text; fixed parts unlimited; learned count; a two-line source counts twice; UI changes nothing | High | Pass | `test_tc04_*`, `test_tc04b_*` |
| TC-05 | Overlap: session/session partial vs adjacent; fixed/fixed same module vs other module; identical anchors; learned vs session via `addr_of`; UI with a found-again part | High | Pass | `test_tc05_*`, `test_tc05b_*`, `test_tc05d_*` |
| TC-05c | Overlap: fixed module part vs session part on the same absolute byte | Medium | **Fail** | BUG-03 |
| TC-06 | Part names: "health (1)/(2)", clashes "x, x (2), x (3)", combine of a combined keeps names | Medium | Pass | `test_tc06_*` |
| TC-07 | Combine button on for 2 found-again, off for 1 | Medium | Pass | |
| TC-08 | Found-again + session combined: single entry removed, nothing saved until learned | High | Pass | part of TC-01 |
| TC-09 | Combine found-again cheats while disconnected, then reconnect | High | **Fail** | BUG-02 |
| TC-10 | Learn button: off for a moving part without a number type, off when disconnected, off when nothing moves | High | Pass | `test_tc10_*`, `test_tc10b_*` |
| TC-11 | Same-object merge with a part already learned (found-again + moving in the same object) | High | **Fail** | BUG-01 |
| TC-11b | Same-object merge in one Start (two moving parts): one object, one tracker after restart | High | Pass | |
| TC-12 | Part 1 fails: part 2 never starts; "part 1 of 2" text; rooms restored; cheat untouched | High | Pass | |
| TC-13 | Cancel during the first watch: the watch's `until` says stop; rooms, hooks, memory paths as before | High | Pass | |
| TC-14 | Cancel while proving part 2's slot: part 1's rooms were reserved during part 2 and are restored; no hooks | High | Pass | |
| TC-15 | Cancel after the first install: hooks out, `by_chain` and rooms restored | High | Pass | |
| TC-16 | Unexpected (non-CheatMakerError) error in part 2: rollback | Low | **Fail** | BUG-04 |
| TC-17 | A ticked cheat is turned off before learning and stays off | Medium | Pass | |
| TC-18 | Progress covers all parts and goes up only; the memory-path box applies to every part; PC-off hidden; Start off when done | Medium | Pass | |
| TC-19 | Connection lost in part 2: rollback, dialog not stuck busy | Medium | Pass | |
| TC-30 | One row after restart, after reconnecting in the same run, and after pressing Find it again | High | Pass | |
| TC-31 | One question with both values (two objects); Yes confirms every tracker; not asked again | High | Pass | |
| TC-31b | One object already confirmed, the other not: asked; No writes nothing (not even the confirmed part) | High | Pass | |
| TC-32 | Restart with one part missing (run 3 has no enemies): COMBINED_NOT_FOUND line, search opens with the part's type and value, tick refused, nothing written | High | Pass | |
| TC-33 | Status wording for a combined cheat with one learned part | Low | **Fail** | BUG-05 |
| TC-40 | Freeze: lost part paused (logged once), other part still written, part comes back: resumed and written | High | Pass | `action=freeze key=… part=money paused/resumed` seen |
| TC-41 | Hook stays while a single cheat on the same object is on; comes out with the last one | High | Pass | |
| TC-42 | Lost connection while on: cheat off, paused state cleared | Medium | Pass | |
| TC-44 | "moved" question names the part, No turns the whole cheat off | Medium | Pass | covered by `tests/ui/test_find_again.py::test_combined_moved_question_names_the_part` |
| TC-50 | Format 3 file opened by the older app's rule: not loaded, kept as `.cmproj.bak` on next save | High | Pass | also checked by hand with `main`'s real `project.py` (below) |
| TC-51 | Format 2 notes come back the same in the UI and are saved as format 3; single entry has no `parts` | High | Pass | format 1/2 unit round trip: `tests/unit/test_project.py` |
| TC-52 | Notes with a bad learned part (value too big, missing object, missing field): only that cheat left out, no crash | Medium | Pass | |
| TC-53 | Int part value that doesn't fit its field: cheat left out, no crash | Medium | Pass | |
| TC-54 | Delete one of two combined cheats (cheat on): it's turned off, its entry goes, the other entry and the objects stay | High | Pass | |
| TC-60 | Cheat files: learned+fixed and moving+fixed combined cheats write none of their parts (json and shn); all-fixed is written; Save dialog counts | High | Pass | |
| TC-61 | Lock it and Make it work off for moving, all-fixed and learned combined cheats | High | Pass | |
| TC-62 | Single found-again cheat unchanged (PC-off still offered, notes entry as before) | High | Pass | plus the full existing suite |
| TC-70 | User guide 6, 6.4, 7.2 and console test C19 cover the AC-7/AC-8 points | Low | Pass (review) | manual |

Manual check for TC-50 with the real older code: I saved a format 3 file with the new code
and loaded it with `git show main:src/cheatmaker/core/project.py`:

```
old FORMAT_VERSION 2
OLD APP: ProjectFileError g.cmproj: made by a newer Cheat Maker (format 3)
set aside -> g.cmproj.bak True Health
```

Observation (not a bug in this branch): in the UI the player sees the general "Cheat Maker's
saved 'find it again' notes for this game can't be read…" message. The words "made by a
newer Cheat Maker" appear only in the log. That is how today's (older) app behaves, and this
branch can't change it.

## Bugs

### BUG-01: A moving part in the same object as an already-learned part gets its own object and hook
- Severity: High
- Priority: P1
- Related test case: TC-11 | Automated test: `tests/qa/test_qa_restart_combined.py::test_tc11_same_object_as_an_already_learned_part_shares_it`
- Environment: `78ec57c`, Linux, Python 3.13.18, fake console
- Steps to reproduce:
  1. Connect. Make "Infinite Health" (player +0x2AC) and press Find it next time on it (it becomes found again, obj1).
  2. Make a session cheat "fraction" at player +0x2B0 (same player object, same class).
  3. Combine both. Select the combined cheat, press Find it next time, then Start.
- Expected result: AC-2: "Two parts found in the same object (same object start and same class) share one object in the notes: two fields, one hook." So obj1 gets a second field "fraction" and there is one hook.
- Actual result: a new object obj2 (same vtable 0x5100, same start) with its own HookAnchor at the `sub [rax+2B0h]` site. Two hooks are installed.
  ```
  AssertionError: [('obj1', ['Infinite Health']), ('obj2', ['fraction'])]
  assert 2 == 1
  ```
- Suspected cause: `src/cheatmaker/ui/find_again.py:949-980` (`LearnDialog._learn_parts`). The merge candidates `objs` only hold objects learned in this dialog. The objects of the cheat's learned parts (`cheat.learned_parts`, already tracked in `ctl.live.trackers` with a known address) are never compared.
- Impact for the product owner: in the main Returnal flow (health already found again, then add fraction) the game gets two code hooks instead of one, and the C19 "both parts in one object" log won't show it.

### BUG-02: Combining found-again cheats while disconnected doesn't update the notes
- Severity: High
- Priority: P2
- Related test case: TC-09 | Automated test: `tests/qa/test_qa_restart_combined.py::test_tc09_combine_found_again_while_disconnected`
- Steps to reproduce:
  1. Connect, learn "Infinite Health" (found again), add found-again "fraction".
  2. Disconnect (or the game closes). The rows stay in My cheats.
  3. Select both, press Combine. You get "Combined into …".
  4. Start the game again and Connect.
- Expected result: AC-1: "Combining found-again cheats removes their own entries from the notes … the new combined cheat replaces them, so next run they are not listed alone."
- Actual result: rows are `['Infinite Health + fraction', 'Infinite Health']`, and the notes still hold only `Infinite Health`. After a fresh app start the combined cheat is gone and only the single cheat comes back.
- Suspected cause: `src/cheatmaker/ui/cheats_view.py:337,340`. `finder.forget(c)` and `finder.remember_combined(combined)` both return early when `ctl.project is None` (`src/cheatmaker/ui/find_again.py:634` and `:649`). The project is unloaded on disconnect, but Combine is still allowed. Options: disable Combine for found-again cheats while no notes are loaded, or load and save the notes by path.
- Impact for the product owner: a player who combines after closing the game silently loses the combined cheat next run, and the old single cheat reappears.

### BUG-03: A fixed module part and a session part on the same byte aren't refused as overlapping
- Severity: Medium
- Priority: P3
- Related test case: TC-05c | Automated test: `tests/qa/test_qa_restart_combined.py::test_tc05c_overlap_module_value_and_session_same_byte`
- Steps to reproduce: `combine([Cheat("a", [Location(MODULE_VALUE, 0x4040, b"\x01", b"\x00")]), <session cheat "b" at module_base + 0x4040>])`
- Expected result: AC-1: "Two parts that share any byte are refused: 'a and b overlap in memory.'"
- Actual result: combined without error. Both parts resolve to 0x404040.
- Suspected cause: `src/cheatmaker/core/cheats.py:269-278` (`_span`). Module parts use the space `(module, offset)` and session/learned parts use `("", absolute)`, so they are never compared. `addr_of` (or a `module_base` argument) could map module parts to absolute addresses too.
- Impact for the product owner: rare (it needs a session value inside the game's module), but two parts would then write the same byte with different values.

### BUG-04: An unexpected error while learning skips the rollback of reserved rooms
- Severity: Low
- Priority: P3
- Related test case: TC-16 | Automated test: `tests/qa/test_qa_restart_combined.py::test_tc16_unexpected_error_still_rolls_back`
- Steps to reproduce: combined health (player) + money (enemy). In part 2, `rf.learn` raises a non-CheatMakerError (simulated `RuntimeError`, e.g. a decode bug or an unexpected console reply).
- Expected result: AC-2: "every hook put in during this dialog is taken out … the notes … are as before." `session.reserved_rooms` should be as before.
- Actual result: `reserved_rooms == [(15088, 15600), (19952, 19968)]` instead of `[]`, and the status says "Something went wrong". If the error happens in the install stage, the hooks stay in until the app closes (they are recorded, so they do come out then).
- Suspected cause: `src/cheatmaker/ui/find_again.py:919-930` (`work()`). Only `ConnectionLost` and `CheatMakerError` call `rollback()`. A `try/except BaseException: rollback(); raise` (or `finally` with a success flag) would cover everything.
- Impact for the product owner: after a rare internal error, the app may avoid some free spare memory for the rest of the session. No wrong write.

### BUG-05: Status says "1 parts"
- Severity: Low
- Priority: P3
- Related test case: TC-33 | Automated test: `tests/qa/test_qa_restart_combined.py::test_tc33_part_count_says_part_not_parts`
- Steps to reproduce: combine found-again "Infinite Health" with a fixed byte cheat. Restart and connect.
- Expected result: "Ready: Infinite Health + god found again (1 part, 0.0 s). Tick it to use it."
- Actual result: "… found again (1 parts, 0.0 s) …"
- Suspected cause: `src/cheatmaker/ui/hints.py` `COMBINED_FOUND` and `src/cheatmaker/ui/find_again.py:404-406`. The count isn't made singular.
- Impact for the product owner: cosmetic.

## Automated tests added

`tests/qa/test_qa_restart_combined.py` (45 tests: 40 pass, 5 strict xfail for BUG-01..05).
They reuse the `win`/`console` fixtures from `tests/qa/conftest.py` and the helpers from
`tests/ui/test_find_again.py`. They are deterministic: the fake console and fake game, no
waiting, and Cancel is driven by wrapping the fake's `watch` / `ah.install` / `rf.learn`.
No production code, console code or `tests/fakegame.py` was changed.

Full suite: `QT_QPA_PLATFORM=offscreen pytest -q -p no:cacheprovider` → **1640 passed, 2 skipped,
5 xfailed** (baseline before this work: 1600 passed, 2 skipped). The new file passed 3
repeated runs. ruff check and ruff format are clean.

## Not tested / risks

- Real-console timing: "both found within 20 s" and "Cancel stops within 2 s". Only the logic is checked here: the watch's `until` is asked and says stop. Leave these to C19 on the PS5.
- The real freezer thread racing `turn_off` (`_part_paused` is guarded by a lock; the tests drive `tick()` directly).
- Two combined cheats with the **same** learned parts but different fixed parts: `remember_combined` and `ensure_cheats` key on the learned anchors only, so the second would replace or hide the first. Hard to reach in the UI (the single found-again cheats are consumed by the first Combine). Not filed.
- A float part whose value is NaN would be saved as `NaN` and make the notes unreadable on load (`_number` rejects it). This is the same as the existing single-cheat path. Not filed.
- Renaming a combined cheat isn't written to the notes. Single found-again cheats behave the same today.
- The cancel reason with *Also look for a memory path*: if Cancel is pressed during the memory-path search of a part with no other anchor, the hint shows the learn notes ("…The memory path search was cancelled.") instead of "you pressed Cancel". Acceptable wording, not filed.

## Retest (2026-11-11, after bc18f4e, f312ee8, aeb9adb, f945f42)

Branch HEAD `f945f42`. Full suite: **1666 passed, 2 skipped, 1 xfailed** (the xfail is the
new BUG-06). The QA file has 53 tests: 52 pass, 1 strict xfail.

| Bug | Fix | Retest | Changed expectation reasonable? |
|---|---|---|---|
| BUG-01 | `_seed_objects` copies the learned parts' objects (vtable and start known), so a new part joins them; clashing field names get " (2)" | **Fixed.** `test_tc11_*` passes. New: `test_rt01_*` (one object and hook, survives a fresh-app restart, both parts written), `test_rt02_*` (a failed Start leaves the seeded object and notes untouched), `test_rt03_*` (field-name clash: "fraction (2)", the other cheat's field untouched) | Test body unchanged |
| BUG-02 | Combine is refused for found-again or learned cheats while no notes are loaded (button off, tooltip and message `COMBINE_NEEDS_NOTES`); the guide says to connect first | **Fixed.** `test_tc09_*` | Yes. The spec doesn't require offline combine, refusing loses nothing, and the user is told why. Combining fixed or session cheats offline still works |
| BUG-03 | `combine(..., module_base=)` puts every part in absolute addresses; the UI passes the base when connected | **Fixed.** `test_tc05c_*`. New: `test_rt08_*` refuses it in the UI | Yes. Without a known module base (offline) it can't be told; session parts only exist while connected anyway |
| BUG-04 | `except BaseException` rolls back. A hook that won't come out stays recorded (taken out on close), its rooms stay kept, and the hint says to close the game (`LEARN_PART_STUCK`) | **Fixed.** `test_tc16_*`. New: `test_rt04_*` covers the stuck-hook path | Test body unchanged |
| BUG-05 | `PART_COUNT` gives "1 part" / "n parts" | **Fixed.** `test_tc33_*` | Test body unchanged |

Other changes checked: combining cheats that are on now runs the confirm and before_on
first. `test_rt07_*`: already-confirmed sources give no new question and the combined cheat
stays on with every part written. Renaming a combined cheat while connected updates its
notes entry: `test_rt05_*`.

### BUG-06: Renaming a combined found-again cheat while disconnected gives a second row on reconnect (new, regression from aeb9adb/f312ee8)
- Severity: Medium
- Priority: P2
- Related test case: RT-06 | Automated test: `tests/qa/test_qa_restart_combined.py::test_rt06_rename_while_disconnected_no_second_row` (strict xfail)
- Steps to reproduce:
  1. Connect, learn "Infinite Health", combine it with a fixed cheat ("Infinite Health + god", saved in the notes).
  2. Disconnect (or the game closes).
  3. Rename the row to "Health".
  4. Start the game again and Connect, in the same app.
- Expected result: one row "Health" (AC-3: "The combined cheat comes back as one row"). The notes entry follows the new name, or the rename is refused offline like Combine.
- Actual result: rows are `['Health', 'Infinite Health + god']` and the notes keep the old name. Ticking both writes the same parts from two cheats.
- Suspected cause: notes entries are now keyed by `(name, learned anchors)`: `src/cheatmaker/ui/find_again.py:249` (`ensure_cheats`) and `_key`. `renamed()` returns early when `ctl.project is None` (`find_again.py:688`), so the offline rename never reaches the notes, and on reconnect the old-name entry no longer matches the renamed row. Before this fix, entries were keyed by anchors only and no duplicate appeared.
- Impact for the product owner: a player who renames a combined cheat after closing the game sees it twice next time.

Retest verdict: BUG-01..05 verified fixed. One new Medium regression (BUG-06). The
recommendation stays **Ready with known issues**.
