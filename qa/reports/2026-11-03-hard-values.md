# QA report: finding hard values (AC-H1 to AC-H6)

- Date: 2026-11-03. QA: qa-engineer agent
- Build under test: `f08a14e` (HEAD: Phase 1 `8c30a01`, Phase 2 `42d24b4`, review fixes `f08a14e`). The diff vs `f9643ef` is in scope.
- Environment: Linux, Python 3.13.18, PySide6 offscreen. Tested on FakeConsole and on `FakeNgServer`, which drives the real PS5Debug-NG client over the wire. UI flows use pytest-qt with `TaskRunner(sync=True)`.
- Inputs: `docs/spec-hard-values.md` (AC-H1..H6), `docs/implementation-notes.md`, the diff, and the existing tests.

## 1. Summary for the product owner

I tested these features on a fake PS5, in core logic and in the Find view:
- the new searches: "Went up by" / "Went down by", the tolerance that fits the number typed, "Game stores it as", and Stand still
- the Find-it guide
- Test the hits

The first round ran on `42d24b4`. It found 7 bugs that the main session fixed in `f08a14e` while I was testing. I re-ran every case on `f08a14e`. Those 7 now pass and stay as regression tests (section 6). The bugs below still reproduce on HEAD.

**Verdict: ⚠️ Ready with known issues.** None of the open bugs loses data. A game that already ran Test the hits has every value put back, on every path I could force: answer, Not sure, Stop, closing the window, Stop in the middle of a round, a failed read, a failed write, a write that lands and then errors, and a failed put-back with a later retry.

Open bugs: **7** (High 0, Medium 4, Low 3).
- **BUG-H1 (Medium)**: when the player types a number with decimals ("87.5", "87.0"), the search only tries Decimal (4 bytes). A value the game keeps as a Decimal (8 bytes), or as a whole number, is no longer found. Before this change it was.
- **BUG-H5 (Medium)**: "I don't know the number" ignores the "Game stores it as" choice. A later "Went down by 15" on a 0-to-1 bar finds nothing.
- **BUG-H6 (Medium)**: in Test the hits, typing 1.5 for a whole-number value writes 2 but asks "Did it change to 1.5?". A truthful "No" throws away the real address.
- **BUG-H7 (Medium)**: when the console refuses Test the hits' write, the window still enables Yes / No / Not sure. Answering a question that was never asked narrows the list wrongly.
- **BUG-H2 (Low)**: with x10, x100 or x1000, a stored value too big for 4 bytes (for example 3,000,000 shown as 3,000,000,000) is never found.
- **BUG-H3 (Low)**: `default_unit()` crashes on small decimals such as 1e-05 when no typed text is passed. It is not reachable from the UI today.
- **BUG-H4 (Low)**: if the player presses "Try it" a second time with an empty number box, the old search ends, but Went up, Went down, Same, Went up by / down by and Stand still stay enabled.

## 2. Scope

**In scope:**
- `core/values.py`: `Compare.*_BY`, `unit_of`, `parse_amount`, `Scale`, `make_scale`, `parse_filter`, `matches`
- `core/scanner.py`: `first_scan` / `next_scan` units, window and scale; `stand_still`
- `core/guide.py`, `core/hit_tester.py`
- `console/ps5debug_ng.py`: compare codes 6 and 8, the ValueBetween first scan, the classic-path limits
- `ui/find_wizard.py`: the new buttons, the guide, Try it, the scale row
- `ui/hit_test_dialog.py`

**Out of scope:**
- The real console behaviour of IncreasedBy, DecreasedBy and ValueBetween, and the float rule for them. These are only on the console (C11a to C11c).
- Phase 3 (encoded values).

**Risk areas, by priority:**
1. Test the hits leaving test values in game memory.
2. Typed numbers no longer finding values that were found before (regression).
3. PC vs console disagreeing on what matches.
4. Scale carried into the wrong search.
5. Buttons enabled with no search.

## 3. Test results

The baseline on `42d24b4` was 441 passed and 1 skipped. Final run on `f08a14e` with the QA file: **529 passed, 1 skipped, 9 xfailed**. Ruff check and format are clean.

All tests are in `tests/qa/test_hard_values_qa.py`, named `test_tc_hNN_*`.

| TC | Title | Prio | Result | Notes |
|---|---|---|---|---|
| TC-H01 | `unit_of` for 87, 87.5, 0.87, -0.87, 87.50, 1,000.5, .5, "87.", 1e3, 0 | H | Pass | |
| TC-H02 | Exact float inside one unit, including negatives and 0 | H | Pass | |
| TC-H03 | Exact float exactly 1 away is not a match (AC-H2 "<") | H | Pass | Failed on 42d24b4 (`<=`), fixed in f08a14e |
| TC-H04 | Filter box: open ends, negatives, 0.87 keeps 0.865 but not 0.37 | M | Pass | |
| TC-H05 | Typed 0.87 keeps the window only | H | Pass | |
| TC-H06 | Typed 87.5 stored as Decimal (8 bytes) found (AC-3.6) | H | **Fail** | BUG-H1. 42d24b4 and f9643ef both found it as DOUBLE before |
| TC-H06b | Typed "87.0" still finds the whole number 87 | M | **Fail** | BUG-H1 |
| TC-H07 | Typed decimal on NG without the snapshot engine | H | Pass | Failed on 42d24b4 ("Update it"), fixed (ValueBetween) |
| TC-H08 | Typed decimal on NG with snapshot uses ValueBetween (4) | M | Pass | |
| TC-H09 | Typed decimal next search on classic NG with many matches | M | Pass | Failed on 42d24b4 ("too many… type the number"), fixed |
| TC-H10 | `default_unit` for ints and plain decimals | L | Pass | |
| TC-H10b | `default_unit(1e-5)` | L | **Fail** | BUG-H3 (IndexError) |
| TC-H11 | Float changed-by tolerance, direction, negatives, NaN or inf previous, fraction | H | Pass | |
| TC-H12 | Int changed-by is exact, INT64 big values | H | Pass | |
| TC-H13 | Int "went up by 0.5 / 1.5" refused, no scan | M | Pass | On 42d24b4, int(0.5) == 0 kept unchanged values; fixed (plain error) |
| TC-H13b | x10 with amount 1.5 is a whole 15 | M | Pass | |
| TC-H14 | Amount 0, -4 or empty: plain error, no scan | M | Pass | Also UI: existing `test_went_down_by_needs_an_amount` |
| TC-H15 | Changed-by on classic NG with many matches: "too many" message | M | Pass | |
| TC-H16 | Fraction + changed-by on NG | M | Pass | |
| TC-H17 | x10 with typed 87.5 finds whole 875 | M | Pass | |
| TC-H18 | x10 window on PC keeps 861..879, not 860 or 880 | M | Pass | |
| TC-H18b | Same window on NG (console agrees with PC) | M | Pass | Failed on 42d24b4 (NG kept 860), fixed (PC re-check) |
| TC-H19 | Scale applies to next exact and changed-by, history label, reset | H | Pass | |
| TC-H20 | Lost search forgets the scale | M | Pass | |
| TC-H21 | Fraction max 0 or negative refused; unknown kind refused | L | Pass | |
| TC-H22 | Negative value with x10 | L | Pass | |
| TC-H23 | x1000 value above 2^31 found | L | **Fail** | BUG-H2 |
| TC-H24 | Stand still stops after 2 quiet scans; sleeps 2 s between | M | Pass | |
| TC-H25 | Stand still runs 5 scans while counts keep dropping | M | Pass | |
| TC-H26 | Stand still: Cancel, stops at 1 left, refused with no search | M | Pass | |
| TC-H27 | Guide fallback order skips what was tried; x1000 ignored | H | Pass | |
| TC-H28 | Guide counts for a bar: Move, Stand still, Test hits, Make cheat; new value resets | M | Pass | |
| TC-H29 | 300 matches after the first search suggests narrowing, not Test the hits | M | Pass | Failed on 42d24b4, fixed |
| TC-H30 | UI: Try it runs fraction, then x10, then x100; finds 8700; history "87 (x100)" | H | Pass | |
| TC-H31 | UI: skipped question guessed; Start over resets the guide | M | Pass | |
| TC-H32 | UI: Try it with an empty box (Advanced was off) | L | Pass | |
| TC-H32b | UI: second Try it with an empty box (Advanced already on) | L | **Fail** | BUG-H4 |
| TC-H33 | UI: "I don't know" + fraction, then Went down by 15 | M | **Fail** | BUG-H5 |
| TC-H34 | UI enablement of By, Stand still, Test hits and the show buttons: off before connect, before a search, at 1 match, at more than 500, after Start over, after a lost search | H | Pass | |
| TC-H35 | Scale max enabled for fraction only; bad max (0, -3, abc) gives a message, no search | M | Pass | |
| TC-H36 | Stand still shows "Don't touch the controller" | L | Pass | |
| TC-H37 | Yes, No and Not sure each restore half A | H | Pass | |
| TC-H38 | Odd counts (3 to 499), real one first, last or middle; ≤ ceil(log2 n) rounds | H | Pass | |
| TC-H39 | 500 OK, 501 refused, duplicates count once | M | Pass | |
| TC-H40 | Stop while waiting restores far-apart addresses (module data and heap) | H | Pass | |
| TC-H41 | Failed read: nothing written, can retry | H | Pass | |
| TC-H42 | Failed put-back: the rest restored, the failed one kept for retry, retry works | H | Pass | |
| TC-H43 | A write that lands then errors is put back | M | Pass | Failed on 42d24b4, fixed (noted before write) |
| TC-H44 | Suggested test value at type limits, -0.0, inf, negative doubles | L | Pass | |
| TC-H45 | Dialog: Not sure, Stop and window close all put values back | H | Pass | |
| TC-H46 | Dialog: a refused write does not ask a question | M | **Fail** | BUG-H7 (2 cases) |
| TC-H46b | Dialog: test value abc, too big or empty gives a message, nothing written | M | Pass | |
| TC-H47 | Warning shown once; winner added and selected; Set enabled | H | Pass | |
| TC-H48 | Dialog: 1.5 for a whole number is refused or asked as written | M | **Fail** | BUG-H6 |
| TC-H48b | Dialog with x10: prefill 111, writes 1110, asks 111, restores | M | Pass | |
| TC-H49 | Lost link during the test: the usual lost handling, no crash | H | Pass | |
| TC-H50 | Guide suggests Test the hits once narrowing is stuck; never Stand still for a number | M | Pass | |
| TC-H51 | Stop in the middle of a round restores what was written | H | Pass | |

**Totals:** 58 TCs, 50 pass and 8 fail, covering 7 open bugs and 9 xfail tests. 7 TCs failed on `42d24b4` and pass on `f08a14e`.

## 4. Bugs (still reproduce on f08a14e)

### BUG-H1: Typed decimals only search Decimal (4 bytes); doubles and whole numbers are no longer found
- Severity: Medium. Priority: P2.
- Related test cases: TC-H06, TC-H06b.
- Automated tests:
  - `tests/qa/test_hard_values_qa.py::test_tc_h06_typed_decimal_still_tries_double`
  - `tests/qa/test_hard_values_qa.py::test_tc_h06b_typed_87_point_0_still_finds_whole_number`
- Steps to reproduce:
  1. The game keeps 87.5 as a Decimal (8 bytes). No 4-byte float is near it.
  2. Type 87.5 and press Search, with "Pick for me".
- Expected: AC-3.6: the likely types are tried in order, as before this change (`f9643ef` finds it as DOUBLE). The AC-H5 fallback list says "other types (as today)".
- Actual: `0 matches`, and `sc.vtype is FLOAT`. "87.0" with a whole number 87 in memory also gives 0.
- Suspected cause: `scanner.py` `first_scan`. With `u != 1` it sets `vtype = FLOAT if scale.decimal or u < 1 else INT32` and runs a single type, instead of looping over `guess_types` (for example FLOAT, then DOUBLE; and INT32 for an integral value).
- Impact for the product owner: a player who types the decimal the game shows can get "Nothing found" where the old version found it.

### BUG-H5: "I don't know the number" ignores "Game stores it as"
- Severity: Medium. Priority: P2.
- Related test case: TC-H33.
- Automated test: `::test_tc_h33_unknown_search_uses_the_scale`
- Steps to reproduce:
  1. Health bar 0.874.
  2. Advanced > "Game stores it as: a fraction of max" (100).
  3. Press **I don't know the number**.
  4. Health drops to 0.724. Type 15 in How much and press **Went down by**.
- Expected: AC-H3: the scale applies to every typed number of the search, including changed-by amounts. Fraction searches use Decimal (4 bytes) unless a type was picked.
- Actual: the bar is not in the results. `do_unknown` calls `first_scan(None, vt, …, value_range=rng)` with no `scale`, so `Scanner.scale` stays SHOWN. "Pick for me" also doesn't default to Decimal.
- Suspected cause: `ui/find_wizard.py` `do_unknown`. Pass `scale=self._scale()`. `first_scan` should then keep `scale` and choose FLOAT for a fraction on the unknown path.
- Impact for the product owner: on a 0-to-1 bar, the obvious path ("I don't know", then "Went down by 15") never finds it.

### BUG-H6: Test the hits asks about a different value than it wrote (1.5 on a whole number)
- Severity: Medium. Priority: P2.
- Related test case: TC-H48.
- Automated test: `::test_tc_h48_decimal_test_value_on_whole_number`
- Steps to reproduce:
  1. Search 100 (2 matches, whole numbers). Press **Test the hits**.
  2. Type 1.5 and press **Write it**.
- Expected: either a plain "whole numbers only" message, or the question names the value written.
- Actual: memory holds 2 (`HitTestDialog._stored` rounds it), but the hint asks "Did the number in the game change to 1.5?". A truthful **No** drops half A, which holds the real value.
- Suspected cause: `ui/hit_test_dialog.py` `do_write` / `_written`. It formats `self._value` (as typed) and ignores the stored value. Refuse non-whole shown values for int types when `scale.factor` is a whole number, or show `stored / factor`.
- Impact for the product owner: the test can end on a wrong address, or end with nothing, with no hint as to why.

### BUG-H7: A refused write leaves Test the hits asking a question that was never asked
- Severity: Medium. Priority: P2.
- Related test case: TC-H46, two cases: every write refused, or only the first address refused.
- Automated test: `::test_tc_h46_refused_write_does_not_ask`
- Steps to reproduce:
  1. Search 100. Press **Test the hits**.
  2. The console refuses writes (all of them, or the one address in half A). Press **Write it**.
- Expected: a plain message. Nothing counts as written, so Write it is available again and Yes / No / Not sure stay off.
- Actual:
  - The message shows, but Yes / No / Not sure are enabled and Write it is disabled.
  - Pressing **No** narrows the list from 2 to 1 on a question that was never asked.
  - Every later put-back retries the refused address and fails again, so Stop shows an error each time.
- Suspected cause: `core/hit_tester.py` `write_test`. Since f08a14e, `_written[addr]` is set before `client.write`. That fixed the lands-then-times-out case, but a definite `WriteFailed` (nothing written) also stays noted, and `restore()` now keeps failed entries. So `waiting` is True after a round that wrote nothing.
- Suggested fix: on `WriteFailed`, drop that address from `_written`. Keep it only for `ConnectionLost` or a timeout. Treat a round with no successful write as "not waiting".
- Impact for the product owner: with a value the console won't let us write, the test can "find" the wrong address.

### BUG-H2: Scaled values too big for 4 bytes are never found
- Severity: Low. Priority: P3.
- Related test case: TC-H23.
- Automated test: `::test_tc_h23_huge_scaled_value_uses_8_bytes`
- Steps to reproduce: money 3,000,000 is stored x1000 (3,000,000,000, 8 bytes). Advanced > x1000, type 3000000, Search.
- Expected: found. AC-3.6 already picks 8 bytes for numbers above 2^31.
- Actual: `0 matches`, `vtype INT32`. On NG, encoding the window for INT32 would raise "doesn't fit".
- Suspected cause: `scanner.first_scan` picks INT32 without checking the scaled window against `INT32.int_range()`. Use INT64 (or `guess_types(scaled)`).
- Impact for the product owner: rare; only very large money values with a x1000 scale.

### BUG-H3: `default_unit()` raises IndexError for small floats
- Severity: Low. Priority: P3.
- Related test case: TC-H10b.
- Automated test: `::test_tc_h10b_default_unit_small_float`
- Steps to reproduce: `default_unit(1e-5)`, or `scanner.first_scan(0.00001)` / `next_scan(EXACT, 1e-5)` with no `unit`.
- Expected: 1e-05.
- Actual: `IndexError: list index out of range`. `repr(1e-05)` has no ".".
- Suspected cause: `core/scanner.py` `default_unit` splits `repr()` on ".". Use `decimal.Decimal(repr(v)).as_tuple().exponent` or similar. The UI always passes `unit`, so only API callers hit this today.
- Impact for the product owner: none today; a trap for future callers (for example Find it again).

### BUG-H4: A second "Try it" with an empty box ends the search but leaves the compare buttons on
- Severity: Low. Priority: P3.
- Related test case: TC-H32b.
- Automated test: `::test_tc_h32b_second_try_it_with_empty_box_turns_compares_off`
- Steps to reproduce:
  1. Search 4242: 0 matches, and the guide offers "Try it" (fraction).
  2. Press **Try it**: 0 matches again, and the guide offers x10.
  3. Clear the number box and press **Try it**.
- Expected: the search has ended, so Went up, Went down, Same, Went up by / down by and Stand still are disabled.
- Actual: they stay enabled with `scanner.active == False`. Pressing one shows "Start a new search first." The first Try it works only because toggling Advanced on happens to refresh the buttons.
- Suspected cause: `ui/find_wizard.py` `do_try_next` calls `scanner.reset()` and then returns early on an empty box without `_update_buttons()`. It should check the box before resetting, or call `do_reset()` / `_update_buttons()`.
- Impact for the product owner: minor confusion; the buttons look usable but aren't.

## 5. Fixed during this round (found on 42d24b4, verified fixed on f08a14e)

| Was | TC | Fix verified |
|---|---|---|
| Exact float kept values exactly 1 away (`<=`) | TC-H03 | `<` |
| Typed decimal on NG without the snapshot engine: "Update it" | TC-H07 | ValueBetween first scan |
| Typed decimal next search on classic NG with many matches: "too many, type the number" | TC-H09 | BETWEEN allowed |
| NG kept the open window end (860 for "87 x10") | TC-H18b | PC re-check |
| Int "went up by 0.5" kept unchanged values (int(0.5) == 0) | TC-H13 | plain error |
| Guide suggested Test the hits right after the first search (300 matches) | TC-H29, H50 | show_limit / stuck rule |
| A write that landed then errored was not put back | TC-H43 | noted before write (but see BUG-H7) |

## 6. Automated tests added

- `tests/qa/test_hard_values_qa.py`: 58 TCs, 86 test items: 77 pass and 9 strict xfail.
  - Core tests use FakeConsole and `FakeNgServer` (NG wire path; `MAX_READ_SPANS` is monkeypatched for the classic many-matches path).
  - UI tests use pytest-qt with `TaskRunner(sync=True)`, and `HitTestDialog.exec` is monkeypatched to script the player.
  - Its own fixtures (`hv_win`, `hv_fake`, `scan_fake`) avoid the `win` / `console` fixtures from `tests/qa/conftest.py` (fakegame).
- Full run: `QT_QPA_PLATFORM=offscreen pytest -q tests/` gives **529 passed, 1 skipped, 9 xfailed**. `ruff check .` and `ruff format --check .` are clean.

## 7. Not tested / risks

- The console's IncreasedBy / DecreasedBy float rule, ValueBetween boundaries and float32 rounding of the window ends: console only (C11a, C11b). The PC re-check covers the classic path. The resident (snapshot) BETWEEN and changed-by results are not re-checked on the PC.
- Huge typed floats (for example 1e20, or 123456789 stored as float32): the "< 1 unit" rule can never match, because float32 spacing there is larger than 1. Real games are unlikely to show such numbers.
- Test the hits on overlapping candidates (addr and addr+2): the writes to half A change values in half B. Restore is correct, but an answer can mislead. Scans are aligned, so this is unlikely.
- A frozen address among the candidates: the freezer rewrites it during a round. Not tested.
- The search history is not displayed in the UI. The scale label is checked in `Scanner.history` only.

## 8. Release recommendation

⚠️ Ready with known issues. BUG-H1 and BUG-H5 make the new paths miss values a player will reasonably try. BUG-H6 and BUG-H7 can make Test the hits pick the wrong address. All four are small fixes; fix them before the console tests in C11. Memory safety (values put back) held on every path tested.

## BUGS FOR FEATURE-IMPLEMENTOR

1. **BUG-H1 (Medium)**: typed decimals search only FLOAT.
   - Repro: double 87.5 in memory, `first_scan(87.5, unit=0.1)` gives 0. `first_scan(87.0, unit=0.1)` with int 87 gives 0.
   - Expected: found as DOUBLE, or INT32 when integral, as in AC-3.6.
   - Make pass: `test_tc_h06_typed_decimal_still_tries_double`, `test_tc_h06b_typed_87_point_0_still_finds_whole_number`.
   - Cause: `scanner.first_scan` window branch picks one type; loop over `guess_types(scale.apply(value))`.
2. **BUG-H5 (Medium)**: "I don't know the number" ignores the scale.
   - Repro: fraction scale, unknown, then Went down by 15 does not keep the bar.
   - Make pass: `test_tc_h33_unknown_search_uses_the_scale`.
   - Cause: `find_wizard.do_unknown` doesn't pass `scale` (and Decimal default for a fraction).
3. **BUG-H6 (Medium)**: dialog writes round(1.5) = 2 but asks "change to 1.5".
   - Make pass: `test_tc_h48_decimal_test_value_on_whole_number`.
   - Cause: `hit_test_dialog.do_write` / `_written` show the typed value, not the stored one; refuse a non-whole value for int types, or ask about `stored / factor`.
4. **BUG-H7 (Medium)**: a refused write (`WriteFailed`) leaves the address noted, so the dialog enables Yes / No / Not sure, an answer narrows, and Stop keeps failing.
   - Make pass: `test_tc_h46_refused_write_does_not_ask[True|False]`.
   - Cause: `hit_tester.write_test` notes before writing; on `WriteFailed`, un-note that address. Keep the note only for an ambiguous failure (ConnectionLost or timeout). Keep `test_tc_h43` passing.
5. **BUG-H2 (Low)**: a x1000 value above 2^31 is not found (INT32 forced).
   - Make pass: `test_tc_h23_huge_scaled_value_uses_8_bytes`.
   - Cause: `scanner.first_scan` vtype choice in the window branch.
6. **BUG-H3 (Low)**: `default_unit(1e-5)` raises IndexError.
   - Make pass: `test_tc_h10b_default_unit_small_float`.
   - Cause: `scanner.default_unit` splits `repr` on ".".
7. **BUG-H4 (Low)**: the second Try it with an empty box resets the search without updating the buttons.
   - Make pass: `test_tc_h32b_second_try_it_with_empty_box_turns_compares_off`.
   - Cause: `find_wizard.do_try_next` early return after `scanner.reset()`.

When a bug is fixed, remove its xfail marker. The run has to stay at 0 failures.

## Re-test after fixes (2026-11-03)

All 7 open bugs are fixed in the next commit. Their strict-xfail markers are removed and the 9 tests now pass as regression tests.

| Bug | Fix |
|---|---|
| BUG-H1 | A typed-decimal or scaled search tries every likely type (`guess_types` of the stored value). A fraction keeps only the decimal types. |
| BUG-H2 | Same fix: values too big for 4 bytes go to 8-byte types. |
| BUG-H3 | `default_unit` reads the decimal exponent instead of splitting `repr`. |
| BUG-H4 | `do_try_next` refreshes the buttons when it ends the old search early. |
| BUG-H5 | "I don't know the number" passes the scale, and a fraction defaults to Decimal (4 bytes). |
| BUG-H6 | A whole-number value refuses a test value that isn't whole after scaling. |
| BUG-H7 | A refused write (`WriteFailed`) is un-noted before the round is undone. A lost link or a timeout stays noted and is put back. |

Run: **538 passed, 1 skipped, 0 xfailed**. Ruff check and format are clean. Open bugs: **0**.
