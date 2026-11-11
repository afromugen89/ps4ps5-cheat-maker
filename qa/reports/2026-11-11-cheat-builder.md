# QA report: smart cheat builder ("Make it work...")

Date: 2026-11-11. Branch `builder-wip` (worktree /home/user/cm-builder), HEAD 8fa17a0,
commits after 11eda38. Linux, Python 3.13.18, PySide6 6.12.0, offscreen Qt.
Spec: docs/spec-cheat-builder.md (B1-B9). Design: docs/architecture-cheat-builder.md.
Automated tests: tests/qa/test_cheat_builder_qa.py.

## Product owner summary

I tested the whole "Make it work..." feature on the fake game (a small x86 interpreter
runs the game's code and our fixes): the safety rules, the order fixes are tried in,
the pass/fail rules, the effects themselves, crash notes, putting the game back, and
the dialog in My cheats and Find.

The core works: the simple path (one test, then Done) works, the effects compute
correctly (7 becomes 4, 1 stays 1, signed and 8-byte values, flags), the safety rules
skip the dangerous instructions, and the existing rollback tests hold.

The problems are in the less common paths. The worst are:

- **BUG-01 (High):** pressing Cancel or Disconnect at the moment a fix is being written
  can leave the jump in the game, pointing at code that is zeroed 2 s later. The game
  then crashes. The window is small (milliseconds).
- **BUG-02 (High):** if the PS5 doesn't take the put-back, the dialog moves on and ends
  with "Your game is unchanged" while the fix is still in the game. This breaks the
  rule in B6.
- **BUG-03 (High):** Always full uses the value from when the dialog opened, not from
  when Start was pressed. A player who follows the hint "Fill it up, then press Start"
  ends up locked at their old, lower value.
- **BUG-04 (Medium):** a hit that lands between two reads can make a working Half damage
  (or Always full) fix fail.
- **BUG-05 (Medium):** a damaged crash-notes file can stop every build with an error.
- **BUG-06 (Medium):** closing the dialog while it is "Keeping the fix" gives a cheat
  marked on that isn't in the game.
- **BUG-07 (Medium):** Disconnect during Only me's Collect step leaves the collect hook
  in the game.

Bugs by severity: Critical 0, High 3, Medium 4, Low 2 (9 in total).

B7 (a second address, Returnal style) is not built; there is a `TODO(B7)` in
`builder.finalize`. The design lists it first in the cut order.

**Release recommendation: Not ready.** Fix BUG-01, BUG-02 and BUG-03 first: each one
can leave the player's game changed, or crashed, without them knowing. The Medium
bugs can follow.

## Scope

In scope: B1-B9, effect routines (`cave.effect_code`), `core/builder.py`,
`core/build_notes.py`, `ui/build_dialog.py`, and the entry points in Find and My cheats.
Out of scope: console tests C18a-e (they need a real PS5), unicorn cross-checks (already
in test_minicpu_vs_unicorn.py) and B7 (not built).

Risk areas: rollback on every fail/cancel/disconnect/close path; crash notes; S1-S7;
effect semantics per type; the counter-less saved routine; judge tolerances; NO_HIT
repeat; the cap of 6; UI enable rules and messages; Find with Check which is real.

Baseline before QA: 1452 passed, 2 skipped (27 s).

## Test results

| TC-ID | Title | Prio | Result | Notes |
|---|---|---|---|---|
| TC-10 | S1 bare-pointer writer skipped by every effect/Only me | High | Pass | qa::test_tc10_14_rules_skip_every_method |
| TC-11 | S2 lock add / xchg / xadd skipped | High | Pass | same |
| TC-12 | S3 flags: ZF/SF/OF -> hook+flags_fix, no NOP; CF/unknown/unchecked -> skip | High | Pass | test_tc12_flags_rules |
| TC-13 | S2 string stores (movsd, rep stosd) skipped | Med | Pass | test_tc13 |
| TC-14 | S4 store size (8-byte over int32, movups over float) | High | Pass | test_tc10_14 |
| TC-15 | S7 crashed method skips only that method | High | Pass | test_tc15 |
| TC-16 | Only me never NOPs; short writer reason; Half has no NOP | Med | Pass | test_tc16 |
| TC-17 | S6 site changed / outside module writes nothing | High | Pass | existing unit test (site_changed) |
| TC-20 | Most hits first, then the "all" candidate | Med | Pass | test_tc20 |
| TC-21 | Cap of 6; one writer has no "all" | Med | Pass | test_tc21 |
| TC-30 | Never goes down tolerance (decimal +-0.01, whole exact) | High | Pass | test_tc30 |
| TC-31 | Always full tolerance (0.5 %, exact), readings before first tick ignored | High | Pass | test_tc31, 31b |
| TC-32 | Half damage judging 7->4, 1->1, wrong rounding fails, decimal 2 % | High | Pass | test_tc32 |
| TC-33 | No tick = NO_HIT; Half with only raises = NO_HIT | Med | Pass | test_tc33 |
| TC-34 | Not alive fails first; Only me needs Yes (Not sure = fail) | Med | Pass | test_tc34 |
| TC-35 | Sampling race (hit between value and counter reads) | High | **Fail** | BUG-04 |
| TC-40 | Effects on the CPU: INT32/INT64 half, overflow, signed never-down, equal, Always full over a raise | High | Pass | test_tc40 (plus the existing matrix) |
| TC-41 | Decimal NaN / -0.0 / -inf behaviour | Med | Pass | existing test_never_down_decimal_edges |
| TC-50 | NO_HIT repeats once and can then pass | Med | Pass | test_tc50 |
| TC-51 | Lost link keeps the note; reconnect to the same pid puts back, no crash mark | High | Pass | test_tc51 |
| TC-52 | Saved cheat has no counter lines and still halves | High | Pass | test_tc52 |
| TC-53 | Every fail path restores the module (existing 9 paths) | High | Pass | test_builder_flow |
| TC-54 | Cancel/Disconnect while the fix is being written | High | **Fail** | BUG-01 |
| TC-55 | Close while finalizing | Med | **Fail** | BUG-06 |
| TC-56 | Disconnect during Only me's Collect | Med | **Fail** | BUG-07 |
| TC-57 | Damaged crash-notes JSON (bad "crashed" list) | Med | **Fail** | BUG-05 (bad top level/"open" handled: Pass) |
| TC-58 | Game gone -> crash mark; next connect "Fix N crashed" | High | Pass | existing tests |
| TC-60 | My cheats button disabled when disconnected / combined / anchored / code | Med | Pass | test_tc60 + existing |
| TC-61 | Find button needs a row | Low | Pass | test_tc61 |
| TC-62 | Dialog: three effects, Never goes down default, Only me off, Full hidden | Med | Pass | test_tc62 |
| TC-63 | Fail in dialog: restored, listed under Advanced, none worked | High | Pass | test_tc63 |
| TC-64 | Pass in dialog replaces the source row; Close after Done keeps the cheat | Med | Pass | test_tc64 |
| TC-65 | Put-back not taken by the PS5, dialog wording | High | **Fail** | BUG-02 |
| TC-66 | Always full: Full read at Start | High | **Fail** | BUG-03 |
| TC-67 | Full prefill precision for large decimals | Low | **Fail** | BUG-08 |
| TC-68 | B9 logs: build_result on a build that ends without a cheat | Low | **Fail** | BUG-09 |
| TC-69 | Unsupported types (DOUBLE, INT16): message, no console work | Med | Pass | existing UI test |
| TC-70 | Find: unchecked group checked first, verdicts merged | Med | Pass | existing UI test |
| TC-71 | One-time notice / "don't show again" | Low | Pass | existing UI tests |
| TC-72 | B7 second address | Med | Not run | Not built (TODO in finalize; design cut list) |
| TC-73 | Console C18a-e | High | Not run | Needs a real PS5 |

## Bugs

### BUG-01: Cancel/Disconnect while a fix is being written leaves the jump in the game
- Severity: High. Priority: P1.
- Related test case: TC-54. Automated test:
  `tests/qa/test_cheat_builder_qa.py::test_bug01_rollback_during_install_leaves_the_jump`
- Steps: 1. Start a build and reach a test. 2. Call `Builder.close()` (Cancel,
  dialog close, or `before_disconnect`) after the routine is written but before the
  jump is written.
- Expected: the module bytes are as before (B6).
- Actual: the site holds `E9 ...` (our jump). `rollback` has already popped
  `_installed` and zeroed the rooms. The worker then writes the jump, sees the routine
  missing (`landed` is False), and calls `rollback`, which returns NOTHING. On a real
  PS5 (2 s delay before zeroing) the worker's `landed` check passes and
  `_taken_back` returns CANCELLED/RESTORED. Then the room is zeroed under a live jump,
  and the game crashes.
- Evidence: `AssertionError: the jump stayed in` (`b'\xe9\xeb7...' != b'\xc4\xc1z...'`).
- Suspected cause: builder.py:756-773. `_install_and_test` writes outside `self._lock`
  and doesn't re-check `_taken_back` between and after its writes. `rollback` can run
  in the middle.
- Impact: Cancel or Disconnect at the wrong moment can crash the player's game while
  the app says "Stopped. Your game is unchanged."

### BUG-02: an unanswered put-back is reported as "Your game is unchanged"
- Severity: High. Priority: P1.
- Related test case: TC-65. Automated test: `test_bug02_not_restored_is_never_called_unchanged`
- Steps: 1. My cheats > Make it work, Start. 2. During the test, the PS5 stops
   accepting writes (`fake.fail_writes = True`). 3. The test ends with no hit.
- Expected (B6): "The app never claims to have rolled back when the PS5 didn't answer";
  the hint says "Restart the game: fixes only live in memory until then." and the build
  stops.
- Actual: the outcome is NO_HIT with `rollback=NOT_RESTORED`. The dialog repeats the
  fix, which fails because the site is "not what we expected", and ends with "None of
  the safe fixes worked for this value. Your game is unchanged." The jump is still at
  the site. FAIL with NOT_RESTORED does the same ("Fix N didn't work..."). So do
  CANCELLED and `_failed` (`close()` result ignored, "Stopped. Your game is unchanged").
  If a later candidate on another writer installs, `open_note` overwrites the open
  note, and the first fix is lost from the crash notes too.
- Suspected cause: build_dialog.py `_tried` only checks `rollback` for LOST.
  `_failed`/`reject` ignore `close()`'s result. Builder.next_candidate also doesn't
  refuse after NOT_RESTORED.
- Impact: the player is told the game is clean while a test hook is still running in it.

### BUG-03: Always full uses the value from when the dialog opened, not when Start was pressed
- Severity: High. Priority: P1.
- Related test case: TC-66. Automated test: `test_bug03_full_is_read_at_start`
- Steps: 1. Health is 80. Open Make it work. 2. Pick Always full; the hint says "Fill it
  up, then press Start." 3. Fill up to 100 and press Start without typing in Full.
- Expected (B2/Effects): "Full = the value when the player pressed Start"; "Full is
  read just before the watch starts".
- Actual: Full is 80. The field was prefilled in `__init__`, `do_start` passes it as
  `full=`, and `Builder.watch` only reads Full when it is None. The cheat holds 80.
  Related: when Check which is real switches to another group member, Full still comes
  from the row the dialog opened on.
- Evidence: `AssertionError: Full is 80.0`.
- Suspected cause: build_dialog.py `_prefill_full` (called once) and `do_start`.
- Impact: a player who does what the hint says gets "always 80" health instead of full.

### BUG-04: a hit between the value read and the counter read fails a working fix
- Severity: Medium. Priority: P2.
- Related test case: TC-35. Automated test: `test_bug04_hit_between_value_and_counter_reads`
- Steps: Half damage on a decimal. The game writes (100 -> 40) after `_live_test` reads
  the value but before it reads the counter slot.
- Expected: PASS. The routine did halve the hit (health is 70).
- Actual: `FAIL, 'it went down without the fix running'`. The samples are (100, count 1),
  then (70, count 1). The same race fails Always full when the value at test start is
  below Full: the sample (old value, count 1) gives "the value didn't stay full".
- Suspected cause: builder.py:815-820 reads the value, then the slot, in two requests.
  `judge` treats the pair as one moment.
- Impact: working fixes are rejected at random (about RTT/250 ms per hit). The build
  may end with "None of the safe fixes worked".

### BUG-05: a damaged crash-notes list stops the builder
- Severity: Medium. Priority: P2.
- Related test case: TC-57. Automated test: `test_bug05_damaged_crash_list[...]`
- Steps: build_notes.json = `{"open": null, "crashed": null}` (or `5`, `{}`, or
  `[{"writer_off": "x"}]`), then Start.
- Expected: as the module says, "A damaged file must not stop the builder". Treat it as
  no crash marks.
- Actual: `TypeError: 'NoneType' object is not iterable`. The other values give
  TypeError, AttributeError (`mark_crashed` on a dict) and ValueError. The dialog shows
  a generic error.
- Suspected cause: build_notes.py `_load` only checks the top level. `is_crashed`
  doesn't guard `int(...)`.
- Impact: one bad edit or partial file disables Make it work until the file is deleted.

### BUG-06: closing during "Keeping the fix" gives an "on" cheat that isn't in the game
- Severity: Medium. Priority: P2.
- Related test case: TC-55. Automated test: `test_bug06_close_while_finalizing`
- Steps: the test passes, `finalize` runs, and `Builder.close()` runs during
  finalize's 2 s wait (Close/Disconnect).
- Expected: either finalize fails and the module is as before, or the cheat it returns
  matches the game.
- Actual: `rollback` restores the site and zeroes both rooms (the saved room
  included). `finalize` carries on and returns a cheat with `enabled=True`. Its lines
  are not in the game; the saved room is zeros. With `live` set, `turn_on` rewrites
  them, but on a real PS5 the rollback's delayed zeroing can land after that and zero
  the routine under a live jump. In the dialog, `_finished` still adds the cheat after
  the dialog was closed.
- Suspected cause: builder.py:964-1001. Finalize has no lock or `_taken_back` checks.
- Impact: after Close, My cheats can show a cheat as on that does nothing, or (rarely)
  the game crashes.

### BUG-07: Only me's Collect hook isn't taken out by Disconnect
- Severity: Medium. Priority: P2.
- Related test case: TC-56. Automated test: `test_bug07_disconnect_during_collect_leaves_the_sampler_hook`
- Steps: Only me, Start, and Disconnect during the 20 s Collect.
  `before_disconnect` calls `Builder.close()`, then the link goes.
- Expected (B6): on Disconnect, the original bytes go back first.
- Actual: `close()` returns NOTHING because Collect isn't tracked in `_installed`.
  `collect_objects`' own `finally` can't write any more, and the sampler jump stays at
  the writer. No crash note is open, so the next connect doesn't fix it either.
- Suspected cause: builder.py `collect` and cave.py `collect_objects`. Cancel only
  works through the cancel Event, which Disconnect doesn't set.
- Impact: the game keeps running a test hook until it restarts. It is harmless while
  the routine stays, but not what the player was told.

### BUG-08: Full prefill loses digits on large decimals
- Severity: Low. Priority: P3.
- Related test case: TC-67. Automated test: `test_bug08_full_prefill_keeps_the_decimal_exactly`
- Actual: 1234567.0 is shown as "1.23457e+06", so Full becomes 1234570.0. It is within
  the 0.5 % judge, so the cheat holds a slightly different number. Typed decimals also
  never compare equal to the float32 value, so `now_full` is always False and the
  Always full NOP fallback is never offered for decimals.
- Suspected cause: build_dialog.py `_prefill_full` uses `f"{v:g}"`.

### BUG-09: no action=build_result log when a build ends without a cheat
- Severity: Low. Priority: P3.
- Related test case: TC-68. Automated test: `test_bug09_build_result_logged_when_nothing_worked`
- Actual: `build_result` is only logged in `finalize`. "None worked", "Nothing changed
  it" and Cancel leave no result line (B9).

## Automated tests added

`tests/qa/test_cheat_builder_qa.py` (31 test functions, 57 cases):
- Pure: S1-S4/S7 classification, flags rules, candidate order and cap, judge
  tolerances per effect and type, NO_HIT rules, Only me answers.
- minicpu: Half/Never down/Always full edge values (INT32/INT64 rounding, overflow,
  signed, equal).
- Flows: NO_HIT then pass, lost link and note survival with put-back on reconnect,
  the saved routine without the counter.
- UI: enable rules, dialog defaults, the fail path in the dialog, the pass path and
  Close after Done.
- 12 strict xfail cases for BUG-01 to BUG-09.

Full run: `1497 passed, 2 skipped, 12 xfailed in 28.87s`. `ruff check src tests` is clean,
and the new file is ruff-formatted. No production code was changed.

## Not tested / risks

- B7 is not built. Its acceptance criteria are unmet (a design-allowed cut). The spec's
  Returnal target (health and fraction) depends on it.
- Console behaviour (C18a-e): Alive timing, foreground_app support, the real
  watchpoint `until`, and whether the 5-byte jump write is atomic (design risk 2).
- Observations, not filed as bugs:
  - Never goes down ignores NaN readings (the game may store NaN through the
    routine).
  - Half damage keeps the game's write when old minus new overflows, so a huge drop
    is not halved and the judge fails it.
  - `rollback` sleeps 2 s on the UI thread when called from Close/Cancel.
  - A writer like `mov [rsp+x],r` is classified HOOK and only refused in
    `effect_code`. It uses up a candidate and gets no NOP fallback.
- The race bugs (01, 06) were reproduced with deterministic interleavings. The real
  windows are milliseconds wide.
