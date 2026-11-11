# QA report: Check which is real (spec-real-check AC-R1 to AC-R5)

- Date: 2026-11-11
- Branch / commit under test: `claude/project-thread-o2y09l` @ `aaae091`
- Environment: Linux 6.18 x86_64, Python 3.13.18, PySide6 6.12.0 (offscreen), pytest-qt 4.5.0
- Automated tests: `tests/qa/test_real_check_qa.py` (44 pass, 12 strict xfail = open bugs)

**How it was tested.** The working tree changed while this QA round ran: someone else made
uncommitted edits to `real_check.py`, `find_wizard.py`, `hints.py` and the unit/ui tests. To
test the committed code, every result below comes from a clean export of `aaae091`
(`git archive aaae091`), with the new QA file copied in. The "WT" column shows what the same
tests do against those uncommitted changes, for information only.

## Product owner summary

Check which is real works in the main case. On a grouped or flat list it finds the game's
own value, marks the copies, moves the real one to the top and puts the original values back,
including after Cancel and after a lost link where the console still takes writes. The core
logic still has holes that can **name the wrong address as real** or **leave a test value in
the game**:

- 3 High: a value's own overlapping bytes count as "following" it (false real, BUG-01); a
  value that was 0 counts any ticking value as a follower (false real, BUG-08); a write the
  console refuses after it already landed is never put back (BUG-09).
- 4 Medium: an area unloading during the check aborts the whole check (BUG-02); a new game or
  a lost search during the check brings old marks back (BUG-03); the clean-up after an error
  can write a stale original over the game's own value (BUG-04); a one-match group never
  shows its mark (BUG-06).
- 2 Low: the smallest decimal gets a "test value" equal to itself (BUG-05); the hint says
  "kept the test value" when no value kept it (BUG-07).

Release recommendation for `aaae091`: **Not ready.** The uncommitted changes in the working
tree already fix BUG-02, 03, 04, 07, 08 and 09, and the cross-group part of BUG-01 (my tests
for them pass there). BUG-01 (flat list), BUG-05 at the probe level and BUG-06 are still open.
Re-test once those changes are committed.

## Scope

In scope: `core/real_check.py` (test value, target selection, rounds, verdicts, put-back,
error paths), `core/grouping.promote`, `ui/find_wizard.py` (`do_check_real`, `_after_check`,
`_check_failed`, `_checked_first`, marks in the table and tree, hints, button states, clearing
on Start over, new game and lost search).
Out of scope: smart cheat builder; a real PS5 (all console behaviour is simulated with
FakeConsole `game_ticks` and monkeypatched read/write).

## Test plan and results

| TC-ID | Title | Pri | Result (aaae091) | WT | Notes / test |
|---|---|---|---|---|---|
| TC-01 | Test value fits and differs at INT8/16/32/64 edges (lo, lo+1, -2..4, hi-1, hi); lower for >=2; 0/1 -> +7 | High | Pass | Pass | `test_tc01_*` |
| TC-01b | Decimal test value differs (0, -0, 1, 3, -8, FLT_MAX, 1e-30) | Med | Pass | Pass | `test_tc01b_*` |
| TC-01c | Smallest decimal (1.4e-45) | Low | **Fail BUG-05** | Fixed (marked not checked) | `test_bug05_*` |
| TC-02 | 10 groups x 8 -> first 8 whole, 64 total | High | Pass | Pass | `test_tc02_*` |
| TC-03 | 63 singles + group of 8 -> last group cut to 1 | Med | Pass | Pass | `test_tc03_*` |
| TC-04 | Unchanged groups and frozen members left out | High | Pass | Pass | `test_tc04_*` |
| TC-05 | Flat list: mixed types kept, order kept, duplicates removed | Med | Pass | Pass | `test_tc05_*` |
| TC-06 | Mixed types in one group (FLOAT real, INT16 x10 HUD, DOUBLE /100) | High | Pass | Pass | `test_tc06_*` |
| TC-07 | INT8 127 / -128 / 1 / 0, INT16 32767 real with INT32 x10 copy | High | Pass | Pass | `test_tc07_*` |
| TC-08 | Follower tolerance boundary 2% (+1 for ints) | Med | Pass | Pass | `test_tc08_*` |
| TC-09 | Tie on followers -> first listed | Med | Pass | Pass | `test_tc09_*` |
| TC-10 | Same bytes in two groups, same round -> not checked, nothing mixed | High | Pass | Pass | `test_tc10_*` |
| TC-10b | Same address as 4 bytes and 1 byte in a flat list | High | **Fail BUG-01** | Still fails | `test_bug01_same_address_*` |
| TC-10c | Overlap across groups in different rounds | High | **Fail BUG-01** | Fixed | `test_bug01_overlap_across_groups_*` |
| TC-11 | Cancel after round 1/2/3/4: memory equals the start | High | Pass | Pass | `test_tc11_*` |
| TC-12 | Cancel set before start: no writes | Low | Pass | Pass | `test_tc12_*` |
| TC-13 | ConnectionLost on group 2's write: group 1 put back | High | Pass | Pass | `test_tc13_*` |
| TC-14 | ConnectionLost on the read back: all put back | High | Pass | Pass | `test_tc14_*` |
| TC-15 | Link down for good: raises, doesn't hang | High | Pass | Pass | `test_tc15_*` |
| TC-16 | WriteFailed on one group: others checked, nothing left | High | Pass | Pass | `test_tc16_*` |
| TC-16b | Write lands, then reported refused | High | **Fail BUG-09** | Fixed | `test_bug09_*` |
| TC-17 | ReadFailed on part of a cluster (region edge within READ_SPAN) | High | Pass | Pass | `test_tc17_*` |
| TC-17b | Area unloads during the 1 s wait | High | **Fail BUG-02** | Fixed | `test_bug02_*` |
| TC-18 | Single-address ReadFailed in a cluster | Med | Pass | Pass | `test_tc18_*` |
| TC-18b | Error put-back keeps the game's own value | Med | **Fail BUG-04** | Fixed | `test_bug04_*` |
| TC-18c | Tested value 0, a ticking value in the group | High | **Fail BUG-08** | Fixed | `test_bug08_*` |
| TC-19 | `promote()` with NaN / 0 / short factors; likely re-indexed; k=0 identity | Med | Pass | Pass | `test_tc19_*` (NaN propagates, no crash; factors aren't used outside grouping yet) |
| TC-20 | Button: off with no search / unlisted matches; on with listed; only Cancel while busy | High | Pass | Pass | `test_tc20_*` |
| TC-21 | UI Cancel mid-check: Stopped hint, one round of marks, memory back | High | Pass | Pass | `test_tc21_*` |
| TC-22 | Marks cleared by lost search / new game | High | Pass | Pass | `test_tc22_*`, `test_tc22b_*` |
| TC-23 | Marks stay after Search again; real one still heads its group | Med | Pass | Pass | `test_tc23_*` |
| TC-24 | New game / lost search while the check runs | Med | **Fail BUG-03** | Fixed | `test_bug03_*` |
| TC-25 | One-match group shows its mark | Med | **Fail BUG-06** | Still fails | `test_bug06_*` |
| TC-26 | Hint when writes are refused / copies plus not checked | Low | **Fail BUG-07** | Fixed | `test_bug07_*` |
| Reg | Full existing suite | High | Pass | n/a | 863 passed, 1 skipped, 12 xfailed |

## Bugs

### BUG-01: Overlapping bytes count as a follower, so a value is called "real" with nothing following it
- Severity: High. Priority: P1.
- Related: TC-10b, TC-10c. Tests: `tests/qa/test_real_check_qa.py::test_bug01_same_address_two_sizes_in_a_flat_list`, `::test_bug01_overlap_across_groups_makes_a_false_real`
- Environment: aaae091, Linux, Python 3.13.18
- Steps: (a) Flat list with rows `(A, INT32)` and `(A, INT8)`, A holding 100, and no game
  activity. Run the check. (b) Group 1 `[A INT32 = 100]`, group 2 `[B FLOAT 8.0, A INT8]`. Run the check.
- Expected (AC-R3): **real** only when *another* member moved by the test factor. With no game
  activity the verdict is "kept the test value".
- Actual: (a) both rows `Result(REAL, followers=1)`; (b) B is `Result(REAL, followers=1)`.
  Writing 75 to A makes A's low byte read 75, which is exactly 3/4 like every test value.
- Suspected cause: `real_check.py` `_judge` counts every group member except `t` itself,
  including members whose bytes overlap a target written this round (its own or another
  group's). The overlap check in `_round` only compares the tested targets.
- Impact: The app puts the wrong address at the top as "checked: real", so a cheat made from
  it doesn't change the game.

### BUG-08: When the tested value was 0, any change in the group counts as following it
- Severity: High. Priority: P1.
- Related: TC-18c. Test: `::test_bug08_ticking_value_is_not_a_follower_of_a_zero`
- Steps: Group (or flat list) `[ammo INT32 = 0, timer INT32 = 1000]`. The timer goes up by 1
  every frame. Run the check.
- Expected (AC-R3): a follower moves from x to about x x (test / original). A timer doesn't
  follow, so ammo is "kept the test value".
- Actual: ammo is `Result(REAL, followers=1)`.
- Suspected cause: `_follows`: `if old == 0 or not math.isfinite(old): return True`.
- Impact: In a flat list (up to 16 unrelated rows), any value at 0 next to anything that
  moves gets "checked: real".

### BUG-09: A write the console refuses after it landed leaves the test value in the game
- Severity: High. Priority: P2 (depends on PS5Debug-NG refusing after it got the bytes).
- Related: TC-16b. Test: `::test_bug09_write_refused_after_landing_is_put_back`
- Steps: Make `write()` store the bytes and then raise `WriteFailed`, as `ps5debug_ng.write`
  can (its second `ok()` comes after `send(part)`). Check `[DEAD = 300.0]`.
- Expected (AC-R4): every address that still holds the test value gets its original back.
- Actual: DEAD stays at 225.0. Evidence: `assert 225.0 == 300.0`.
- Suspected cause: `_round`: `except WriteFailed: written.pop()  # refused: nothing landed`.
- Impact: A health or ammo value can stay at 3/4 after the check.

### BUG-02: An area unloading during the wait aborts the whole check
- Severity: Medium. Priority: P2.
- Related: TC-17b. Test: `::test_bug02_area_unloaded_during_the_wait_is_not_checked`
- Steps: Group 1 is an address in a region that is removed during the 1 s wait. Group 2 is
  the REAL/HUD/FRAC/DEAD group. Run the check.
- Expected (AC-R3): that address is "not checked" and the rest is judged.
- Actual: `WriteFailed: address 0x50000000+4 is not mapped in pid 77` comes out of `run()`.
  All results are thrown away, the remaining rounds don't run, and the UI shows an error.
- Suspected cause: `_put_back` writes the original when `after` has no entry (unreadable),
  and the put-back error is raised at the end of the round.
- Impact: Changing area or opening a menu during the check makes it fail with an error and
  no answer.

### BUG-03: Marks come back after a new game or a lost search that happened during the check
- Severity: Medium. Priority: P2.
- Related: TC-24. Tests: `::test_bug03_new_game_during_the_check_drops_its_marks`, `::test_bug03_search_lost_during_the_check_drops_its_marks`
- Steps: Start the check. While it runs, `game_changed` (Connect > Refresh) or `search_lost`
  (Find it again on another tab) fires. The check finishes.
- Expected (AC-R5): the marks are cleared by a new game or by Find it again clearing the search.
- Actual: `_after_check` runs `self.verdicts.update(checker.results)` after the reset, so 5
  old marks are stored and turn up on the next search that lists those addresses. The
  lost-search hint is also overwritten.
- Suspected cause: `find_wizard.py` `_after_check` doesn't check whether the search or game
  it started on is still current.
- Impact: Old "checked: real" marks and their reordering show up in a new game.

### BUG-04: The clean-up after an error writes a stale original over the game's own value
- Severity: Medium. Priority: P3.
- Related: TC-18b. Test: `::test_bug04_error_put_back_keeps_the_games_own_value`
- Steps: Check `[HUD]`. During the read back the game writes HUD = 225 and the read raises
  `ProtocolError`, with the link still up.
- Expected (AC-R4): "the same put-back runs": where the game already wrote its own value, it stays.
- Actual: HUD writes are `[test 225, original 300]`: the stale 300 is written over the game's value.
- Suspected cause: `_round` `except Exception: self._put_back(..., None)`. With `after=None`
  every address is restored without checking.
- Impact: A damage or pickup that happened during the check can be undone.

### BUG-06: A group with one match never shows its check mark
- Severity: Medium. Priority: P2.
- Related: TC-25. Test: `::test_bug06_lone_group_shows_its_mark`
- Steps: Have a grouped list that contains a one-match group (a changed match with no
  copies). Run the check with nothing following.
- Expected (AC-R5): every checked match shows its mark. The hint even says 'Rows marked "kept
  the test value" are the best bets'.
- Actual: the lone row shows `0.10` with empty Notes. It has no expanded rows, and the group
  row only ever shows "checked: real".
- Suspected cause: `find_wizard.py` `_item_text` (group row branch) and `_row_text`
  (`not self.grouped`).
- Impact: The player is told to look for marked rows that aren't marked.

### BUG-07: The hint says values were kept when none were
- Severity: Low. Priority: P3.
- Related: TC-26. Tests: `::test_bug07_hint_when_every_write_is_refused`, `::test_bug07_hint_when_copies_and_one_not_checked`
- Steps: (a) `fake.fail_writes = True`, then check. (b) The game puts every value back and
  one write is refused.
- Expected (AC-R5): the hint says what was found: nothing could be checked, or the game put
  every value back.
- Actual: both show `CHECK_REAL_KEPT` ("Rows marked 'kept the test value' are the best bets"),
  but no row is KEPT.
- Suspected cause: `_after_check` falls back to `CHECK_REAL_KEPT` whenever the results are
  not all COPY.
- Impact: Misleading advice after a failed or all-copies check.

### BUG-05: The smallest decimal gets a "test value" equal to itself
- Severity: Low. Priority: P3.
- Related: TC-01c. Test: `::test_bug05_subnormal_float_is_not_called_kept`
- Steps: Flat list with a FLOAT holding bytes `01 00 00 00` (1.4e-45). Check.
- Expected (AC-R2/R3): an actual test value, or "not checked".
- Actual: `probe_value` gives 1.05e-45, which encodes to the same bytes. The verdict is
  "kept the test value" though nothing was tested.
- Suspected cause: `probe_value` float branch (`current * 0.75`) has no check that the
  encoded bytes differ.
- Impact: Rare; a junk match is presented as a best bet.

## Automated tests added

`tests/qa/test_real_check_qa.py` (56 tests, lint and format clean):
- Unit: test values (TC-01), CHECK_LIMIT and skipping (TC-02 to TC-05), verdicts with mixed
  types, INT edges, tolerance and ties (TC-06 to TC-09), overlaps (TC-10), put-back on
  cancel, lost link, refused write and partial reads (TC-11 to TC-18), `promote` (TC-19).
- UI (pytest-qt offscreen, FakeConsole): button states (TC-20), Cancel (TC-21), marks
  cleared or kept (TC-22, TC-23).
- 12 strict xfail tests for BUG-01 to BUG-09.

Run on aaae091: full suite `863 passed, 1 skipped, 12 xfailed`. QA file: `44 passed, 12 xfailed`.
`ruff check src tests`: all checks passed.
Run on the current uncommitted working tree: 9 of the 12 xfail tests now XPASS (strict),
which is expected because those bugs are fixed there. Remove those markers when the fix is
committed.

## Not tested / risks

- Real PS5 timing: whether 1 s settle and 0.5 s follow-back are enough (spec defers this to
  console test C17d). If a copy hasn't followed back yet, the next round's "before" read holds
  a test-derived value. This isn't covered by the simulation.
- Lost link for good: test values stay in memory. The spec has no "put back after
  reconnecting" step, and the uncommitted `not_restored` list in the working tree hints at
  one. Worth a decision from the product owner.
- Freezer and check overlap: frozen addresses are skipped by exact address only (aaae091).
  A frozen INT32 at A overlapping an INT16 target at A+2 isn't skipped, so the freeze loop
  would make it look like a copy. Not automated; the working tree changes the skip to an
  overlap test.
- Async TaskRunner (real thread pool): BUG-03 was reproduced in sync mode by firing the
  signal from a game tick, which matches the queued-signal order. Double-clicking isn't
  possible because the button is disabled while busy.
- `promote` with an out-of-range or negative `k` raises IndexError or duplicates members.
  The UI can't reach this (it always passes a valid index), so it isn't reported.

## BUGS FOR FEATURE-IMPLEMENTOR

1. BUG-01 (High): overlapping bytes count as a follower. Make `test_bug01_same_address_two_sizes_in_a_flat_list` (still failing on the WT) and `test_bug01_overlap_across_groups_makes_a_false_real` pass. In `_judge`, exclude members whose bytes overlap the tested target itself, not only other written targets.
2. BUG-08 (High): a value that was 0 counts any change as a follower. `test_bug08_*`. `_follows` returns True for `old == 0`.
3. BUG-09 (High): a refused write that landed is never put back. `test_bug09_*`. `written.pop()` on WriteFailed.
4. BUG-02 (Medium): an area unloading mid-check aborts the whole check. `test_bug02_*`. `_put_back` writes to unreadable addresses and raises.
5. BUG-03 (Medium): marks come back after a new game or lost search during the check. `test_bug03_*` (2 tests). `_after_check` needs to ignore stale results.
6. BUG-04 (Medium): the error put-back overwrites the game's value. `test_bug04_*`. `_put_back(after=None)`.
7. BUG-06 (Medium): a one-match group shows no mark. `test_bug06_*` (still failing on the WT). `_item_text` group-row branch.
8. BUG-07 (Low): misleading KEPT hint. `test_bug07_*` (2 tests). `_after_check` fallback.
9. BUG-05 (Low): the smallest decimal's test value equals the original. `test_bug05_*`.

## Re-test after fixes (2026-11-11)

All 9 bugs (BUG-01 to BUG-09) fixed and the review findings applied: originals read right
before each write and again before each put-back, results of a check that outlived its
search are dropped, a zero tried value only counts zeros that changed, overlapping bytes
are never followers, refused writes stay noted for put-back, new hints for "not checked"
and "couldn't put back". The xfail markers are removed; tests/qa/test_real_check_qa.py:
56 passed. Full suite: 889 passed, 1 skipped. Ruff clean.
