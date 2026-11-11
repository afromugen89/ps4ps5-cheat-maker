# QA report: Bring your notes to a new game version (spec-game-update AC-U1 to AC-U13)

- Date: 2026-11-11
- Branch / commit under test: `claude/project-thread-jhmjbm` @ `2ac15f3` (QA worktree snapshot)
- Environment: Linux 6.18 x86_64, Python 3.13.18, PySide6 6.12.0 (offscreen), pytest + pytest-qt,
  fake PS5 (`tests/fakegame.py` Layout V1 -> V2, `fight()`)
- Automated tests: `tests/qa/test_game_update_qa.py` (16 pass, 10 strict xfail = 9 open bugs)
- Full suite: `2082 passed, 3 skipped, 10 xfailed` (baseline before QA: `2066 passed, 3 skipped`).
  `ruff check` and `ruff format --check` are clean on the new file.
- GitHub issues: none created (instructed). The issue list is at the end of this report.

## Product owner summary

I tested bringing notes over on the fake PS5: the offer and Not now, version order and source
choice, format 4 and older notes, the run (Cancel, failures, the summary), the question with Yes
and No, combined cheats, the field shift, Advanced merge, the background fingerprint learner,
Update for this version with the hook lines (AC-U9), the wording, and the user guide / console
test docs.

The main path works. V1 notes come over to V2:
- the hooked field moves +8;
- the player is asked once, naming the old version;
- **No writes nothing**, and the question comes back on the next tick;
- Yes writes at the new place and clears "check it".

The old notes and old fingerprints stay byte for byte the same after success, failure and Cancel.
A combined cheat with one value not found writes nothing. Formats 3 and 4 load and come over, and
broken or newer-format old notes are skipped without a crash. Every hint is plain words.

The code review already found 13 issues, which are being fixed now and are not repeated here. On
top of those:

Bugs: 9 (0 Critical, 0 High, 6 Medium, 3 Low).
- BUG-02 (Medium): Bring them over reads the game's code without taking the "one code read at a
  time" slot. Two big reads can run at once.
- BUG-03 (Medium): Advanced merge silently drops a new-named cheat if its value is already in
  the notes.
- BUG-05 (Medium, docs): console test C21e expects `action=hook_remove` after Cancel during
  "Checking the spare memory". No hook is in at that step, so a correct app fails C21e.
- BUG-06 (Medium): after a failed bring-over (for example, nobody took a hit), Advanced can't
  bring the failed values again. It says "Brought over 0 of 0 values".
- BUG-07 (Medium): Update for this version can drop a cheat that moved fine, when another cheat
  in the file has the same name and fails (files that hold the notes' hook only).
- BUG-08 (Medium): the field shift can put a field before the start of the object (negative
  offset), and the value still counts as brought over.
- BUG-01, BUG-04, BUG-09 (Low): a crash on unusual digits in a version, a merged value's
  question naming the wrong version, and a capitalised reason inside the summary brackets.

Release recommendation: **Ready with known issues**, once the review fixes have landed. Nothing
found here writes to the game without a Yes. BUG-05 should be fixed before the console run of
C21, or the tester will report a false failure.

## Scope

In:
- AC-U1: offer, version compare, source choice, Not now.
- AC-U2: background learner and the one-code-read rule.
- AC-U3: the run, Cancel, summary.
- AC-U4: field shift.
- AC-U7: format 4, old notes untouched, merge.
- AC-U8: question, Yes/No, combined.
- AC-U9: Update for this version with hook lines.
- AC-U10: older formats.
- AC-U12/U13: docs accuracy for user guide 6.6 and C21.

Out: anything in the code review list (not re-filed); a real console (timings C21a/b, real
Returnal class groups); `tools/make_old_notes.py` beyond the existing tests.

Risk areas: the first write to a brought-over value, old-notes integrity, code reads on the
console, Advanced merge, Update for this version.

## Test plan and results

| TC-ID | Title | Priority | Result | Notes |
|---|---|---|---|---|
| TC-01 | Version compare rules and closest older source | High | Pass | `03.000.005 < 03.001.000`, `1.05 == 01.05`, `1.9 < 1.10`, text after numbers |
| TC-02 | Odd version text never crashes | Low | **Fail** | BUG-01 |
| TC-03 | PS4 / other region with the same numbers is never a source | High | Pass | CUSA01285, PPSA01286 |
| TC-04 | Broken / newer-format old notes are skipped | Medium | Pass | |
| TC-05 | Not now is per pair; the next update is offered | Medium | Pass | |
| TC-06 | Broken `carry_declined` setting resets, offer shows | Low | Pass | |
| TC-07 | Old notes + fingerprints byte-identical after a *failed* bring-over; new file format 4 | High | Pass | success and Cancel are covered by existing tests |
| TC-08 | Format-3 old notes come over | Medium | Pass | |
| TC-09 | No writes nothing, plain hint, asked again | High | Pass | no write into the game's data at all |
| TC-10 | Yes writes at the new place, check cleared | High | Pass | |
| TC-11 | Fields that aren't hooked shift with the hooked one | Medium | Pass | Health 32 +8 |
| TC-12 | Bring-over read holds the one-code-read slot | Medium | **Fail** | BUG-02 |
| TC-13 | Advanced waits while another code read runs | Medium | **Fail** | BUG-02 |
| TC-14 | Background learner never starts while carrying | Medium | Pass | |
| TC-15 | Merge keeps new-named cheats on values already here | Medium | **Fail** | BUG-03 |
| TC-16 | Merged value's question names its own version | Low | **Fail** | BUG-04 |
| TC-17 | Merge never changes values already here | High | Pass | |
| TC-18 | C21e pass line matches Cancel during the slot check | Medium | **Fail** | BUG-05 (docs) |
| TC-19 | Cancel during the slot check: no jump, no file, offer back | High | Pass | |
| TC-20 | All bring-over texts and reasons are plain words | Medium | Pass | no vtable/cave/pointer/signature/slot/hook |
| TC-21 | Summary line matches the spec example | Medium | Pass | |
| TC-22 | Advanced repeats a failed bring-over | Medium | **Fail** | BUG-06 |
| TC-23 | AC-U9 keeps a moved cheat whose name repeats | Medium | **Fail** | BUG-07 |
| TC-24 | Field shift never gives a negative offset | Medium | **Fail** | BUG-08 |
| TC-25 | Slot-check reason reads as part of the summary | Low | **Fail** | BUG-09 |
| TC-26 | Combined cheat with a value not found: no question, no write | High | Pass | |
| TC-27 | Regression: full suite | High | Pass | 2082 passed, 3 skipped, 10 xfailed |

Manual review (no automated test):
- User guide 6.6 and section 9 (AC-U12): content matches behaviour except the points in
  "Other observations".
- C21a-g (AC-U13): C21e is wrong (BUG-05).
- C21f's `already in the game` line can only be checked on a console.

## Bugs

### BUG-01: A version with a superscript digit crashes `version_key`
- Severity: Low
- Priority: P3
- Related test case: TC-02 | Automated test: `tests/qa/test_game_update_qa.py::test_tc02_odd_version_text_never_crashes`
- Environment: 2ac15f3, Linux, Python 3.13.18
- Steps to reproduce: `cr.version_key("1.0²")`, or save notes for version `1.0²` and call
  `cr.find_source(TITLE, "03.001.000")`.
- Test data: `"1.0²"`
- Expected result: versions are compared, and text that isn't a number is compared as text
  (AC-U1). No crash.
- Actual result: `ValueError: invalid literal for int() with base 10: '²'`
- Evidence: `src/cheatmaker/core/carry.py:85 ValueError`
- Suspected cause: `carry.py:84-86`. `_TOKEN` puts `²` in a text token (`\w`, not `\d`), but
  `t.isdigit()` is True for `²`, so `int(t)` runs. Use `t.isdecimal()`, or test which regex group
  matched. `check_offer` calls it on Connect, and `_other_notes` calls it on every notes file.
- Impact for the product owner: an unusual version text (on the console or in any notes file
  for the game) makes Connect fail with an error instead of showing no offer.

### BUG-02: Bring them over reads the game's code without taking the one-code-read slot
- Severity: Medium
- Priority: P2
- Related test case: TC-12, TC-13 | Automated tests: `test_tc12_bring_over_read_takes_the_one_code_read_slot`, `test_tc13_advanced_waits_while_another_code_read_runs`
- Environment: as above
- Steps to reproduce:
  1. V1 notes, V2 running. Connect.
  2. Press Bring them over. During its `read_code_image`, `ctl.code_reading` is False.
  3. Or: set `ctl.code_reading = True` (the background learner or the cheat-file learner is
     reading) and press Advanced. A second code read starts.
- Expected result: AC-U2 says "At most one code read at a time". The design says
  `Controller.code_reading` gives "one code read at a time".
- Actual result: TC-12 sees `[False]` during the run's read. TC-13 counts one read while
  `code_reading` was already True.
- Suspected cause: `ui/carry.py:369-413`. `CarryOver.start` never checks or sets
  `ctl.code_reading`, and doesn't cancel `NoteFingerprints`. `bring_btn` is enabled whenever
  connected and not carrying (`cheats_view.py` `_update_buttons`). While a run reads, the
  cheat-file learner (`CheatsView._maybe_learn`) is also free to start.
- Impact for the product owner: on the PS5, two reads of the whole game code (tens to hundreds
  of MB over Wi-Fi) can run at once. The bring-over gets slower and the debug server is
  stressed.

### BUG-03: Advanced merge silently drops a new-named cheat on a value already in the notes
- Severity: Medium
- Priority: P2
- Related test case: TC-15 | Automated test: `test_tc15_merge_keeps_new_named_cheats_on_values_already_here`
- Steps to reproduce:
  1. Old notes (03.000.005) hold Health, Obolites and the cheats "Infinite health", "Rich" and
     "Health 500" (set 500 on Health).
  2. New notes (03.001.000) hold Health and "Infinite health".
  3. Advanced, then pick 03.000.005.
- Expected result: AC-U7 says Advanced "only adds objects and cheats whose names aren't in the
  new notes yet". "Health 500" isn't in the new notes, so it should be added on the Health
  already there, or at least named in the summary.
- Actual result: the saved cheats are `['Infinite health', 'Rich']`. "Health 500" is gone and
  no message mentions it.
- Suspected cause: `core/carry.py:453-455` (`for_merge`). A cheat is skipped when any of its
  objects was skipped (`any(o not in ids ...)`). It isn't remapped to the existing object with
  the same label.
- Impact for the product owner: a cheat the player made on the old version silently doesn't
  come over, and nothing says so.

### BUG-04: A merged value's question names the wrong version
- Severity: Low
- Priority: P3
- Related test case: TC-16 | Automated test: `test_tc16_merged_values_question_names_their_own_version`
- Steps to reproduce:
  1. New notes were brought over earlier from 03.000.004 (`carried_from` is 03.000.004).
  2. Advanced brings Obolites from 03.000.005.
  3. Tick "Rich".
- Expected result: AC-U8: "It was brought over from version 03.000.005".
- Actual result: "Is this your Obolites? It was brought over from version 03.000.004 ...".
- Suspected cause: `ui/find_again.py` `_carried_version()` uses the notes-wide `carried_from`.
  `adopt_carried(merge_into=...)` keeps the first value (and sets it on notes that were never
  brought over). The version has to be stored per object (or per merge).
- Impact for the product owner: the safety question names the wrong game version.

### BUG-05: Console test C21e expects `action=hook_remove` after Cancel during the slot check
- Severity: Medium (docs: blocks console sign-off)
- Priority: P2
- Related test case: TC-18 | Automated test: `test_tc18_c21e_pass_line_matches_cancel_during_the_slot_check`
- Steps to reproduce: run the bring-over and press Cancel during "Checking the spare memory"
  (the `prove_slot` step).
- Expected result: the C21e "Pass when" column (`docs/console-tests.md`) and the behaviour
  agree.
- Actual result: C21e says "Stops within 2 s; `action=hook_remove`; ...". The hook goes in
  only *after* the 60 s check (`carry.place_hook`: `prove_slot`, then `install`), so no
  `hook_remove` is logged. The test captures no such line.
- Suspected cause: `docs/console-tests.md` C21e row. Either drop `action=hook_remove` there,
  or move the Cancel to "Finding Health in the new version" *after* the hook went in.
- Impact for the product owner: on the console run, a tester following C21e marks a correct
  app as failed.

### BUG-06: After a failed bring-over, Advanced can't bring the failed values again
- Severity: Medium
- Priority: P2
- Related test case: TC-22 | Automated test: `test_tc22_advanced_repeats_a_failed_bring_over`
- Steps to reproduce:
  1. V1 notes, V2 running. Bring them over, and nobody takes a hit during the 10 s collect.
  2. The summary says "Not brought over: Infinite health (nothing in the game touched it while
     we watched)". Health is saved with no way to be found. The offer is gone, because the new
     notes now hold values.
  3. Advanced, then pick 03.000.005, and play.
- Expected result: the spec ("Not in this spec") says "Repeating a failed bring-over on its own
  (the player uses Advanced)". So Advanced should try again for values that came over with no
  way to be found.
- Actual result: "Brought over 0 of 0 values for version 03.001.000." Health still has
  `anchors == []`.
- Suspected cause: `core/carry.py:441-442` (`for_merge`) skips every label already in the new
  notes, including objects with no anchors. `ui/carry.py` `open_advanced`/`start` always merges
  into non-empty notes.
- Impact for the product owner: one missed hit during the 10 s collect means the player must
  search for Health and press Find it next time again. The retry the spec promises doesn't
  work, and the "0 of 0" message doesn't say why.

### BUG-07: Update for this version drops a moved cheat when another cheat has the same name and fails
- Severity: Medium
- Priority: P2
- Related test case: TC-23 | Automated test: `test_tc23_update_with_hooks_keeps_a_moved_cheat_whose_name_repeats`
- Steps to reproduce:
  1. A V1 cheat file holds "Find Health again" (the notes' hook), two cheats named "No death"
     (one movable, one whose code is gone), and "Health (PC off)".
  2. Bring the notes over.
  3. Press Update for this version.
- Expected result: AC-U10 says v2 updates work as before. v2 alone keeps the "No death" that
  moved.
- Actual result: `['Find Health again', 'Health (PC off)']`. Both "No death" cheats are gone
  from the updated file.
- Suspected cause: `core/carry.py:955-963` (`update_with_hooks`). `rest_failed` is a set of
  *names*, so the moved cheat with the failed one's name is skipped, and `moved_iter` loses it.
  Match by position or identity instead (only files with hook lines take this path).
- Impact for the product owner: a working cheat silently disappears from the updated cheat
  file.

### BUG-08: The field shift can put a field before the start of the object
- Severity: Medium
- Priority: P2
- Related test case: TC-24 | Automated test: `test_tc24_field_shift_never_gives_a_negative_offset`
- Steps to reproduce: notes with Health (hooked, +0x2AC) and a second field at +8. The new
  version's hooked field moved *down* by 0x20 (`dataclasses.replace(fg.V2, field_shift=-0x20)`).
  Run the carry steps.
- Expected result: a field never gets an offset below 0 (it can't lie before the object). It
  should keep its old offset, or fail with a plain reason, and not count as brought over.
- Actual result: `{'Health': 652, 'Health 32': 656, 'Ident': -24}`. The cheat counts as brought
  over, and the notes save `"off": "-0x18"`.
- Suspected cause: `core/carry.py:256-259` (`shift_fields`) adds the shift with no check.
- Impact for the product owner: after one Yes (the question shows the *cheat's* field, which
  may look fine), a cheat on that other field writes into memory in front of the object, which
  can crash the game.

### BUG-09: A slot-check reason shows as a capitalised sentence inside the summary brackets
- Severity: Low
- Priority: P3
- Related test case: TC-25 | Automated test: `test_tc25_slot_check_reason_reads_as_part_of_the_summary`
- Steps to reproduce: the game writes to the spare memory during the check (`prove_slot`
  raises its existing error).
- Expected result: AC-U3's v2-style line, "Not brought over: Infinite health (<reason in
  lower case>)."
- Actual result: "Not brought over: Infinite health (The game is using the spare memory Cheat
  Maker needs. Restart it.)."
- Suspected cause: `core/carry.py:763-764` and `777-778` wrap `str(exc)` from
  `plan_hook`/`prove_slot`/`install` as the reason. Map these to short carry reasons.
- Impact for the product owner: an odd-looking summary line ("...Restart it.).").

## Other observations (not filed)

- A combined cheat's No says "Search for God to find it in this version". God is the cheat's
  name, not a value, so there is nothing to search for. The existing test pins this text; the
  spec doesn't say otherwise.
- User guide 6.6 says "No changes nothing: search for the value again (6.4)". Section 6.4 is
  Find it next time; searching is section 5.
- "Advanced: Bring notes from an older version..." also lists newer versions (as the notes say).
  The label says "older".
- A cheat that was *not* brought over (its value has no way to be found) still shows "Found
  again each run (check it)" in Works after restart?.
- Risk: Advanced is not refused while Find it again is still running after Connect
  (`start()` checks `carrying`, not `finder.running`). Both put hooks in and set the kept rooms
  at the same time. I couldn't reproduce a failure with the synchronous task runner.
- Risk (spec wording): fingerprints are learned "once per version per app run". A hook made by
  Find it next time later in the same run gets its checked fingerprint only on the next app run.

## Automated tests added

`tests/qa/test_game_update_qa.py` (26 tests; the local `make` fixture reuses
`tests/ui/test_carry_over.py`'s `App`/`Console` and `tests/unit/test_carry.py` helpers):
- AC-U1: TC-01 to TC-06.
- AC-U7: TC-07 (old files byte-identical after failure; format 4 keys) and TC-08 (format 3
  source).
- AC-U8: TC-09 (No writes nothing and asks again) and TC-10 (Yes).
- AC-U4: TC-11 and TC-24 (field shift).
- AC-U2 / one read: TC-12 to TC-14.
- Merge: TC-15 to TC-17. Retry: TC-22.
- Cancel and C21e: TC-18 and TC-19.
- Wording: TC-20, TC-21 and TC-25.
- AC-U9: TC-23.
- Combined with a value not found: TC-26.

Strict xfails, one per bug:
- BUG-01: tc02
- BUG-02: tc12, tc13
- BUG-03: tc15
- BUG-04: tc16
- BUG-05: tc18
- BUG-06: tc22
- BUG-07: tc23
- BUG-08: tc24
- BUG-09: tc25

Full run: `2082 passed, 3 skipped, 10 xfailed in 43.30s`.

## GitHub issues (not created, as instructed)

One issue per bug, label `bug`. Titles:
1. BUG-01: A version with a superscript digit crashes version_key
2. BUG-02: Bring them over reads the game's code without taking the one-code-read slot
3. BUG-03: Advanced merge silently drops a new-named cheat on a value already in the notes
4. BUG-04: A merged value's question names the wrong version
5. BUG-05: Console test C21e expects action=hook_remove after Cancel during the slot check
6. BUG-06: After a failed bring-over, Advanced can't bring the failed values again
7. BUG-07: Update for this version drops a moved cheat when another cheat has the same name and fails
8. BUG-08: The field shift can put a field before the start of the object
9. BUG-09: A slot-check reason shows as a capitalised sentence inside the summary brackets
