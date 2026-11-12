# QA report: Linked values in Make it work

- Date: 2026-11-12
- Branch / commit under test: `claude/project-thread-f4p6m0` @ `8c84171` (worktree, detached HEAD)
- Spec: docs/spec-linked-values.md (L1-L17, measurable targets). Design: docs/architecture-linked-values.md
- Code: core/linked.py, core/builder.py, core/build_notes.py, ui/build_dialog.py, ui/find_wizard.py, ui/cheats_view.py
- Environment: Linux 6.18, Python 3.13.18, PySide6 6.12.0, Qt offscreen, fake console (`tests/fakegame.py`, `console/fake.py`), imports from the worktree's `src`
- Automated tests: `tests/qa/test_linked_values_qa.py` (54 pass, 7 strict xfail = open bugs)
- Full suite: baseline 2266 passed, 3 skipped. With the QA file: **2320 passed, 3 skipped, 7 xfailed**

## Product owner summary

I tested the whole linked-values flow on the fake Returnal game:
- one, two and three places
- all three effects
- Only me per place
- the fallback freeze
- Cancel, close, Disconnect, a lost link, the PS button, the game going away
- crash and kept notes across a reconnect to the same or another game process
- the My cheats rows, Find's pool hand-over and cheat files

I also checked the pure judging rules with edge cases: zero, negative and whole-number values, NaN, unreadable members, a member that recovers, and small Half damage hits.

The core works. Make it work fixes health and its fraction as one cheat, never writes the copies, and puts every place back on every exit path I tried: the module bytes are identical afterwards. A simple value still gets one test and Done with `linked=none`. Crash notes blame only the fix under test.

Bugs: **0 Critical, 0 High, 2 Medium, 5 Low.**

- BUG-01 (Medium): once the 3-place limit is hit, the fallback adds a 4th place as a freeze, so the cheat has more places than L10 allows. Fixed in 1509fba.
- BUG-02 (Medium): with a full pool (16 members far apart) and 36 ms per console read, which is the latency the fake uses for a real PS5, each member is read only every ~1.36 s. L3 says at least once a second. Fixed in 0246482.
- BUG-03 (Low): a member that couldn't be read in place 2's test is listed in Advanced as "not needed: it kept up after place 2 was fixed". Fixed in e920505.
- BUG-04 (Low): a hit that lands between two members' last readings splits one value and its copy into two clusters. Fixed in f5d685a.
- BUG-05 (Low): a member that is behind for only 0.65 s counts as "fell behind". The spec's rule is "for at least 1 s in a row". Fixed in cee297a.
- BUG-06 (Low): after a cheat file is reloaded, the multi-place cheat's Full cell loses its tooltip "Make it work again to change Full." It stays read-only. Fixed in 01e5c68.
- BUG-07 (Low): the Collect step at place 2 writes its crash note and rollback log as place 1. Fixed in 1509fba.

Release recommendation: **Ready with known issues.** Nothing here gives a wrong fix, leaves bytes in the game or blames the wrong code.
- BUG-01 only shows on games that keep one value in 4 or more places.
- BUG-02 only shows with very large pools, and it only weakens the "every second" timing. The judge uses timestamps, so it stays correct.

Run C22a with the `build_linked_reads` log to see BUG-02's real-console numbers.

## Scope

**In scope:** L1-L17 and the FakeConsole measurable targets: the fell-behind rule for all effects, clusters and their order, put back of 2 and 3 places on every path, and the crash note.

**Out of scope or not testable here:**
- A real PS5 (C22a-h): real latency, real timing, the 5-minute and 7-minute budgets.
- The OPEN items: they keep their defaults (no trim, 3 places, freeze fallback, Full read-only).

## Test plan and results

| TC-ID | Title | Spec | Priority | Result | Notes |
|---|---|---|---|---|---|
| TC-01 | No pool: no `build_pool` / `build_linked_*` lines, `build_result ... places=1 linked=none` | L1, L17 | High | Pass | also golden `tests/unit/test_builder_golden.py` green |
| TC-02 | Pool whose members keep up (HUD copy, unchecked): one test, `LinkedPlan(True)`, linked=none | L1 | High | Pass | |
| TC-03 | Pool: 16 cap not counting place 1 and copies, group order, rebased factors | L2 | Medium | Pass | 20 members + 5 copies |
| TC-03b | Pool: shared-byte members, negative/NaN factor dropped, B1 marks overlay | L2 | Medium | Pass | |
| TC-04 | Start check: 2% rule, NaN, unreadable, int slack, place 1 = 0 | L2 | Medium | Pass | |
| TC-05 | Find freezes on pool members released at Start, counted | L2 | Medium | Pass | UI |
| TC-06 | Judge edges: 2.0% vs 2.1%, zero, negative, int gap 1/2, int truncation, NaN, unreadable, unreadable at end, one reading, recovers, run broken by unreadable, single blip, above | L3 | High | Pass | 17 cases |
| TC-07 | Half damage small hits: one 2% hit within rule, two hits behind; whole numbers | L3 | High | Pass | see Observation O-1 |
| TC-08 | "1 s in a row": two behind readings 0.65 s apart mid-test | L3 | Low | **Fail** | BUG-05; fixed in cee297a |
| TC-09a | 16 spans, no latency: every member at least once a second | L3 | High | Pass | worst gap 1.0 s |
| TC-09b | 16 spans, 36 ms per read: every member at least once a second | L3 | High | **Fail** | BUG-02 (254 late reads, gap 1.36 s); fixed in 0246482 |
| TC-10 | 16 spans at 36 ms: target samples at least 90% of no pool | L3 | High | Pass | 55 vs 56 |
| TC-11 | Copies never written over 2 places + 3 more hits (writes checked as (pid, addr, data)) | L4 | Critical-risk | Pass | only the L4 check writes the unmarked percent |
| TC-11b | Fallback writes only the failing place's member | L4, L11 | High | Pass | |
| TC-12 | Clusters: last readings straddle a hit | L5 | Medium | **Fail** | BUG-04; fixed in f5d685a |
| TC-13 | Clusters: readings 0.4 s apart, hits mid-test, still one cluster | L5 | Medium | Pass | |
| TC-14 | Pick order: real, likely, size, address; copies/unchecked never picked | L5 | Medium | Pass | |
| TC-15 | 3 places: own rooms and counters, no overlap with each other or an open cheat | L6 | High | Pass | |
| TC-16 | Collect at place 2 noted as place 2 | L6, L12 | Low | **Fail** | BUG-07; fixed in 1509fba |
| TC-17 | Only me per place: reuse on the same object, Collect once on another, can't-tell-apart = fallback | L7 | High | Pass | existing `tests/unit/test_builder_places.py` (re-run) |
| TC-18 | Whole-number place 2 for Always full (Full 100 exact), Never goes down, Half damage | L8, L6 | High | Pass | |
| TC-19 | Place limit: result never more than MAX_PLACES places | L10 | Medium | **Fail** | BUG-01; fixed in 1509fba |
| TC-19b | 12-test limit never fires without a pool | L10, L17 | Medium | Pass | |
| TC-20 | 4 fixes per linked place, 12 tests | L10 | Medium | Pass | existing tests (re-run) |
| TC-21 | Freeze result: combined, session-only, not saveable, Save off, Find it next time on, Full read-only, notes clear | L11, L13, L14 | High | Pass | UI |
| TC-22a | Cancel during place 2's Collect: module identical | L12 | Critical-risk | Pass | |
| TC-22b | Cancel during the L4 check: module identical, percent back | L12 | Critical-risk | Pass | |
| TC-22c | finalize can't keep place 2, then close: module identical | L12 | High | Pass | message "Your game is unchanged" is said before close (dialog closes first, so true) |
| TC-22d | Fallback can't read the member, then close: module identical | L12 | High | Pass | |
| TC-22e | PS button in place 3's test: kept places stay, no crash mark; close: identical | L12, L9 | High | Pass | |
| TC-22f | Disconnect as place 2's watch starts (UI): identical, "Stopped. Your game is unchanged." | L12 | Critical-risk | Pass | |
| TC-22g | Disconnect during place 2's test (UI): identical | L12 | Critical-risk | Pass | |
| TC-22h | Fail / Cancel / close / close from another thread with 2 and 3 places, game gone, lost link, NOT_RESTORED | L12 | Critical-risk | Pass | existing tests (re-run) |
| TC-23a | App died between places; reconnect same process: place 1 put back, KEPT_PUT_BACK, not marked | L12 | High | Pass | |
| TC-23b | App died during place 2's test; reconnect same process: both put back, nothing marked | L12 | High | Pass | |
| TC-23c | Reconnect to another process (3 places): only place 3's writer marked crashed, kept dropped | L12 | High | Pass | |
| TC-23d | Old notes file without `kept`; damaged kept entries | L12 | Medium | Pass | |
| TC-24 | 2-place cheat round-trips GoldHEN JSON and CheatRunner .shn | L13 | High | Pass | |
| TC-25a | Full read-only with tooltip on a 2-place cheat | L8 | Low | Pass | |
| TC-25b | Same after reloading the cheat file | L8 | Low | **Fail** | BUG-06; fixed in 01e5c68 |
| TC-26 | Advanced: unreadable member not called "kept up" | L15 | Low | **Fail** | BUG-03; fixed in e920505 |
| TC-27 | Log lines L16 in a 2-place build | L16 | Medium | Pass | |
| TC-28 | Hints name the place; L1 hint once; Done text | L1, L6, L13 | Medium | Pass | existing UI tests (re-run) |
| TC-29 | Combine drops the pool; the pool doesn't affect equality; only grouped rows have one | L2 | Medium | Pass | plus existing UI test |

## Bugs

### BUG-01: The 3-place limit is exceeded by the fallback freeze
- Status: Fixed in 1509fba
- Severity: Medium
- Priority: P2
- Related test case: TC-19 | Automated test: `tests/qa/test_linked_values_qa.py::test_tc19_bug01_place_limit_counts_the_freeze`
- Environment: 8c84171, Linux, Python 3.13.18, fake console
- Steps to reproduce:
  1. Set `MAX_PLACES` to 2. This is the same code path as 3 places plus a 4th value.
  2. Build with the third place (`setup(third=True)`) and pass places 1 and 2. The third value still falls.
  3. Call `next_place()`. It returns `fallback_reason=LIMIT`.
  4. Call `fallback(reason=LIMIT)`.
- Expected result: L10 says "At most 3 places per build (place 1 + 2 linked)", and the design says "Freeze places count toward MAX_PLACES". The result has at most MAX_PLACES places. A member still behind at the limit is listed as not fixed, or the limit is checked before the last place so the freeze is the last place.
- Actual result: `res.places == 3` with MAX_PLACES 2. Parts are `['Health', 'Health (2)', 'Health (3)', 'Health (4)', 'Health (place 3)']`. With the real limit, a 4th place is frozen.
- Evidence: `AssertionError: (3, ['Health', 'Health (2)', 'Health (3)', 'Health (4)', 'Health (place 3)'])  assert 3 <= 2`
- Suspected cause:
  - `src/cheatmaker/core/builder.py:1627`: `self.place >= lk.MAX_PLACES` sends the build to the fallback after place MAX_PLACES has already been kept.
  - `fallback` (`builder.py:1797`, `n = len(self._kept) + 1`) then holds the member as one more place.
  - The spec text conflicts here: L10 says L11 applies at a limit. This needs Kein to pick the intended reading, but the code currently goes past the stated limit.
- Impact for the product owner: on a game that keeps one value in 4 or more places, the player gets a cheat that holds 4 places, one by a freeze, beyond the stated limit of 3.

### BUG-02: With a full pool, members are read less than once a second at 36 ms per read
- Status: Fixed in 0246482
- Severity: Medium
- Priority: P2
- Related test case: TC-09b | Automated test: `tests/qa/test_linked_values_qa.py::test_tc09b_bug02_sixteen_spans_at_36ms_are_read_once_a_second`
- Environment: as above
- Steps to reproduce:
  1. Build a pool of 16 members, each more than 4 KiB from the others, so there are 16 spans.
  2. Make every console read cost 36 ms (`Game.slow_reads`), the latency the fake uses for a real PS5.
  3. Run place 1's 20 s test.
- Expected result: L3 says "every pool member is read at least once a second".
- Actual result: 270 reads, 254 of them late (`PoolData.late`). The longest gap between two readings of a member is 1.36 s. Without latency it is exactly 1.0 s. The target's samples stay within budget (55 vs 56; TC-10 passes).
- Evidence: `AssertionError: 254 late reads`. The log line is `action=build_linked_reads place=1 spans=16 reads=270 avg_ms=36 late=254`.
- Suspected cause:
  - At most `MAX_SPAN_READS_PER_TICK = 5` reads per tick (`src/cheatmaker/core/linked.py:40`).
  - With latency, a tick lasts ~0.36 s: the sleep, plus the 3 sample reads at 36 ms, plus 5 pool reads. That gives 5 spans per 0.36 s, about 13.9 per second, which is fewer than 16.
  - In later places the reference span takes one of the 5 reads every tick (`builder.py:2122`), which is worse.
  - The design's risk 1 expected this only at about 60 ms.
- Impact for the product owner: on a game where the value has many linked copies spread through memory, the app checks each one less often than promised. The verdict stays correct because it uses timestamps, but a short fall could be missed.

### BUG-03: An unreadable member is listed as "not needed: it kept up after place N was fixed"
- Status: Fixed in e920505
- Severity: Low
- Priority: P3
- Related test case: TC-26 | Automated test: `tests/qa/test_linked_values_qa.py::test_tc26_bug03_unreadable_member_is_not_called_kept_up`
- Environment: as above
- Steps to reproduce:
  1. Use a pool where the percent is marked kept. It falls behind in place 1's test.
  2. Build place 2 (the fraction).
  3. During place 2's test, make the percent's memory unreadable.
  4. Call `after_pass()`.
- Expected result: L15 says Advanced lists "kept up" or "fell behind" and what was done. A member that couldn't be read is unknown; it did not keep up.
- Actual result: the row's status is `couldn't be read`, and its done text is `not needed: it kept up after place 2 was fixed`.
- Evidence: `AssertionError: not needed: it kept up after place 2 was fixed`
- Suspected cause: `src/cheatmaker/core/builder.py:1586`. The `elif old is not None and (old.status is BEHIND ...)` branch treats every status other than BEHIND as kept up, including UNKNOWN.
- Impact for the product owner: the Advanced list (sent back from console test C22a) claims a value was checked and fine when the app could not read it.

### BUG-04: One value and its copy are split into two clusters when a hit falls between their last readings
- Status: Fixed in f5d685a
- Severity: Low
- Priority: P3
- Related test case: TC-12 | Automated test: `tests/qa/test_linked_values_qa.py::test_tc12_bug04_last_readings_straddling_a_hit_still_cluster`
- Environment: as above
- Steps to reproduce:
  1. Use the fraction, read every 0.75 s, and the percent (100 × the fraction), read 0.4 s later in another span.
  2. Hits land at 5.1 s, 10.1 s and 19.7 s. The last fraction reading is at 19.5 s and the last percent reading is at 19.9 s.
  3. Call `clusters([fraction, percent], readings)`.
- Expected result: one cluster. L5 Words: "members that fell behind and kept the same ratio to each other during the test".
- Actual result: `[['0x1000'], ['0x9000']]`. When the same hits land mid-test, they cluster correctly (TC-13).
- Suspected cause: `src/cheatmaker/core/linked.py`:
  - `_same_ratio` compares every pair with the last pair only (`end = ratios[-1]`, line 338).
  - `_settled` can't drop the last reading as mid-change, because it has no next reading (line 315).
  - So one mid-hit pair at the end fails the whole comparison.
  - The design's risk 2 covers mid-test splits, but not this anchor.
- Impact for the product owner: usually harmless, because the extra cluster is checked and found to be a copy, or it keeps up after place 2. But the hint can say "Place 2 of 3" when there are only 2 places, and an extra Check which is real may run.

### BUG-05: "Fell behind for at least 1 s in a row" accepts 0.65 s
- Status: Fixed in cee297a
- Severity: Low
- Priority: P3
- Related test case: TC-08 | Automated test: `tests/qa/test_linked_values_qa.py::test_tc08_bug05_behind_for_less_than_a_second_is_kept_up`
- Environment: as above
- Steps to reproduce: the readings are on line, then behind at 4.65 s and 5.3 s (0.65 s apart), then back on line until the end. Call `judge_member`.
- Expected result: KEPT_UP. Spec Words: "at the end of the test or for at least 1 s in a row". The design reads this as 2 readings at least 0.75 s apart.
- Actual result: BEHIND.
- Suspected cause:
  - `src/cheatmaker/core/linked.py:283` counts any 2 consecutive behind readings and never looks at their times.
  - The pool watch reads a span again after as little as 0.65 s (`POOL_EVERY_S - POOL_EARLY_S`, `linked.py:36`). That is below both the spec's 1 s and the design's 0.75 s.
- Impact for the product owner: a copy that lags for just over half a second, for example a slow HUD update, can start an extra linked step that ends in "copy: never touched" or "not needed". This costs time but never causes a wrong fix.

### BUG-06: After reloading, a multi-place cheat's Full cell has no tooltip
- Status: Fixed in 01e5c68
- Severity: Low
- Priority: P3
- Related test case: TC-25b | Automated test: `tests/qa/test_linked_values_qa.py::test_tc25b_bug06_full_tooltip_after_reload`
- Environment: as above
- Steps to reproduce:
  1. Make a 2-place cheat in the UI.
  2. Save it to a GoldHEN JSON file and load it back.
  3. Look at the Value cell in My cheats.
- Expected result: L8 default says "the Full box is read-only and its tooltip says 'Make it work again to change Full.'"
- Actual result: the cell is read-only, but its tooltip is `''`. `Cheat.places` is in memory only, so after a reload `places == 1`.
- Suspected cause: `src/cheatmaker/ui/cheats_view.py:219` shows the tooltip only when `c.places > 1`, and nothing restores `places` on load.
- Impact for the product owner: after a restart, the player sees an empty Full cell they can't edit and no reason why.

### BUG-07: Collect at place 2 is noted and logged as place 1
- Status: Fixed in 1509fba
- Severity: Low
- Priority: P3
- Related test case: TC-16 | Automated test: `tests/qa/test_linked_values_qa.py::test_tc16_bug07_collect_at_place_2_is_noted_as_place_2`
- Environment: as above
- Steps to reproduce:
  1. Build with Only me, with the fraction in another object.
  2. Pass place 1 and start place 2.
  3. Run `collect()`.
- Expected result: L12 crash notes name the fix under test. Every other note at place 2 carries `place=2` (TC-15 and the existing tests).
- Actual result: the Collect sampler's open note has `place=1`, and its rollback logs `place=1`.
- Suspected cause: `src/cheatmaker/core/builder.py:960-968`. `_Installed(...)` in `_collect_objects` doesn't pass `place=self.place`.
- Impact for the product owner: none visible in the app. The notes file and log say the wrong place if the app or game dies during place 2's Collect. Only a crash of the sampler itself is marked, so nothing is mis-blamed.

## Observations (not filed as bugs)

- **O-1, spec conflict (L3 vs Words), Half damage with one small hit.** A single hit smaller than about 4% of the value leaves a member that "falls by the game's full amount" inside the 2% rule, so it is judged kept up (TC-07). L3's sentence "a value that falls by the game's full amount is behind" only holds for bigger hits or two or more hits. This is the design's stated trade-off. If C22g shows Half damage on small hits slipping through, Kein should decide.
- **O-2, time budget.** Collect can run up to 2 × 20 s per place (`collect_tries`, reset in `_begin_place`). That is 120 s for 3 places with Only me, against "Collect (40 s at most)" in the worst-case budget, so the 7-minute bound can be exceeded (240 + 90 + 120 s + checks). This is a spec arithmetic issue. It could not be measured on the fake.
- **O-3, logs.**
  - A fallback logs two `build_linked_result` lines for the same place (`result=fail` and then `result=fallback_freeze`).
  - A limit logs `result=limit` twice (in next_place and in fallback).
  - L16 lists them as one result per place. Log parsers for C22 should take the last line.
- **O-4, part names.** The freeze result's code parts are named "Health", "Health (2)", ... next to the freeze part "Health (place 2)". With 2 places plus a freeze, both "Health (3)" and "Health (place 3)" appear, which is easy to confuse in Find it next time. This follows the design.
- **O-5, tooltip text.** "Make it work again to change Full." appears on rows where Make it work is disabled: code cheats and combined cheats aren't single values. The player has to start again from Find. The text is the spec's.
- **O-6, test-quality finding in an existing test.** `tests/unit/test_builder_places.py:136` checks that the copies are never written with `{addr for addr, *_ in g.fake.writes}`. FakeConsole records `(pid, addr, data)`, so this collects the pid and can never fail. TC-11 now does the check correctly, and it passes. The original test should unpack `for _pid, addr, _d in ...`. I did not change it (QA doesn't edit existing tests).
- **O-7.** When finalize can't keep place 2, it says "Your game is unchanged." while place 1 is still in. The dialog closes the builder first, which puts place 1 back, so the player's message is true (TC-22c).

## Automated tests added

`tests/qa/test_linked_values_qa.py` has 42 test functions, 61 cases with the parameters: 54 pass and 7 are strict xfail (BUG-01 to BUG-07, named `test_tcNN_bugNN_*`). All seven bugs are fixed since and their xfail marks are removed. By area:
- L1/L17: TC-01, TC-02.
- L2: TC-03, TC-03b, TC-04, TC-05 (UI), TC-29.
- L3: TC-06 (17 parametrized judge edges), TC-06b, TC-07, TC-07b, TC-08, TC-09a, TC-09b, TC-10.
- L4: TC-11, TC-11b. These check `FakeConsole.writes` correctly.
- L5: TC-12, TC-13, TC-14.
- L6/L8: TC-15, TC-16, TC-18 (3 effects).
- L10: TC-19, TC-19b.
- L11/L13/L14: TC-21 (UI), TC-24 (GoldHEN and shn), TC-25a, TC-25b.
- L12: TC-22a-g (module bytes identical on every path, 2 and 3 places, UI Disconnect), TC-23a-d (reconnect).
- L15: TC-26.
- L16: TC-27.

Run: `PYTHONPATH=src QT_QPA_PLATFORM=offscreen pytest tests/qa/test_linked_values_qa.py` gives 54 passed, 7 xfailed. With `--runxfail`, each of the 7 fails on its own assertion. The full suite gives 2320 passed, 3 skipped, 7 xfailed. ruff check and format are clean.

## Not tested / risks

- Real PS5 (C22a-h): real read latency (BUG-02 depends on it), the 5-minute and 7-minute targets, a real Returnal pool, and the CheatRunner reload on console.
- Real threads: the freezer thread and the UI's worker thread were exercised with the sync TaskRunner and one cross-thread close test (existing). I did not stress-test races beyond that.
- Find it next time actually learning the freeze part of a linked result. I verified that the button is enabled, but not the learn flow itself, which is unchanged code (restart-combined).
- The OPEN items: trim, more than 3 places, Full editing on multi-place cheats.
