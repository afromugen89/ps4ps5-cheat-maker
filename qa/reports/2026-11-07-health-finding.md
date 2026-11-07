# QA report: easy health finding (percent search, It's full now, About X%, grouped copies)

## 0. Re-test of the fixes (`f9a10e7`, `1c071b3`), 2026-11-07

**Verdict: ⚠️ Ready with known issues.** BUG-01 to BUG-04 are fixed and verified. The re-test found one new bug, **BUG-05 (High)**: after It's full now, a 0% check done on the PC ties every match to "percent", so the next % drops the real value. The over-the-limit path is fixed (BUG-02); this is the under-the-limit path of the same 0% scenario.

Full suite on `1c071b3` plus the QA file: **729 passed, 1 skipped, 2 xfailed**. Both xfails are BUG-05. Ruff check and format are clean.

| Item | Result | Evidence |
|---|---|---|
| BUG-01 subnormal decimal twin | **Fixed** | The twin is never kept "of full" and never grouped. For int health alone, 1 INT32 match is left and no group forms. I agree that is correct: the spec groups only 2 to 500 matches, and a twin is not a copy. I rewrote the two tests to assert this. `test_bug01_int_health_group_head_is_the_whole_number`: 1 match `(HP, INT32, 25, of full)`. `test_bug01_ui_set_on_the_one_row_writes_a_whole_number`: Set 200 writes 200, and Make a cheat uses the int address and type. New: `test_bug01_int_health_with_fraction_groups_without_the_twin`, where the group is {int health, fraction}, likely real, +1 copy. Plain unknown search: pass. |
| BUG-02 0% as went down, then % / About | **Fixed** | The percent is noted, not wiped. `test_bug02_*` pass. |
| BUG-03 1% / 0% duplicates | **Fixed** | `one_way_each` keeps the narrowest way. `test_bug03_*` pass on FakeConsole. New `test_rt03_ng_one_percent_lists_each_address_once` passes on the NG client: 1.5 is listed once, as "percent". |
| BUG-04 noted scan drops inf / huge | **Fixed** | `scan_rebase`. `test_bug04_noted_scan_drops_nothing` passes on FakeConsole. New `test_rt01_ng_noted_on_the_console_snapshot_keeps_inf_and_huge` passes: the NG snapshot keeps +inf, -inf and 3e38, the count is unchanged, and the next % narrows. A decimal that is NaN now is still dropped on NG snapshots (documented, no NG "keep all" compare). |
| NG PC-side noted with more than 4,000 spans is refused | **Pass** | New `test_rt02_...`: a percent search on the PC, then It's full now, then the same % over the limit with `MAX_READ_SPANS=0`. ValueError "...too many to note a percent...". Count, history and last_pct are unchanged, and the same % works once readable. |
| last_pct == 0 handled | **Pass** over the limit (BUG-02 tests). **Fail** under the limit: BUG-05 | |
| Minor B: a plain number not 0..100 in a Full search | **Pass** | New `test_rt05_...`: typing `250` shows "...type the % the game shows... press Start over", and the search is unchanged. `100` still counts as 100%. |
| Minor C: Test the hits uses the head's way | **Pass** | I reviewed `_test_candidates`. The implementor's UI test `test_test_the_hits_uses_the_heads_way_over_the_most_common` passes. |
| Tree pick by (address, type) | **Pass** | Covered by the existing UI tests and TC-23..25. |

### BUG-05: a 0% check on the PC ties every match to "percent", so the next % drops the real value
- Severity: High. Priority: P1
- Automated tests: `tests/qa/test_health_finding_qa.py::test_bug05_zero_percent_on_the_pc_then_heal_keeps_the_value[False]` (30%) and `[True]` (About 25%), both strict xfail.
- Environment: `1c071b3`, FakeConsole, default `percent_pc_limit` (the check runs on the PC).
- Steps to reproduce: 1. Shield 1,700, then press It's full now (Decimal). 2. The shield breaks to 0 and the game shows 0%: type `0%`. This runs on the PC; the shield is kept. 3. It recharges to 510: type `30%`, or press About 25%.
- Expected result: the shield is kept as 30% of its value when full (AC-P2). The Full value (1,700) was known at the 0% check.
- Actual result: `count=0 history=['full', '0%', '30%']`, and the same for `about 25%`. After step 2, the shield's way is `percent`, not `of full`.
- Suspected cause: `core/percent_filter.py` `check()` returns the first way that matches, in the order percent, fraction, x10, of full. A value of 0 at 0% fits every way, so it is tied to "percent" (0 ± 1). From then on `ways_to_try` only checks that way. The same happens at low percents whenever the value lands in the percent window: a value whose full is 120 reads 1.2 at 1%. Possible fixes: (a) leave the match untied (way None) when more than one way fits, so the next check tries every way again; or (b) in a Full search, prefer "of full" when its Full value is known.
- Impact for the product owner: a player tracking a shield that breaks to 0% loses the whole search on the next percent when the list was already small.

---


- Date: 2026-11-07. QA: qa-engineer agent
- Build under test: `d9b02ef` on `claude/project-thread-mqbzh2` (diff `origin/main...HEAD`: `4aab0ca`, `3e8d07e`, `1a143b9`, `53fb260`, `d9b02ef`).
- Environment: Linux, Python 3.13.18, PySide6 6.12.0 offscreen. Scanner flows on FakeConsole; the real PS5Debug-NG client driven over the wire by `FakeNgServer` (tests/unit/test_ps5debug_ng.py); Find view via pytest-qt with `TaskRunner(sync=True)`.
- Inputs: `docs/spec-health-finding.md` (AC-P1..P4, AC-G1..G4), `docs/architecture-health-finding.md`, `docs/user-guide.md` 5.1, 5.3, 5.4, 5.7, the diff and the tests added with it.
- GitHub issues: not created (asked to skip). The bugs are listed here.

## 1. Summary for the product owner

I tested typing a percent (`40%`), "It's full now", the About 75 / 50 / 25% buttons and the grouped list. I tested them in the core, on the PS5Debug-NG client over a fake console, and in the Find view. I followed the risk list: the PC-check limit, heals during a Full search, "noted" scans, tools used after the search moves to the PC, Start over and game change, scan files, group order and labels, switching between the grouped list and the plain table, and plain number searches.

Most of it works as the spec says. These all behave correctly, also on the NG client:
- the Full chain going down, healing back up, and the About buttons
- the exact limit boundary
- Stand still, Went up / down, Same and Changed after the search has moved to the PC
- the Stored as column
- reset on Start over and on a game change
- plain number searches

**Verdict: ⚠️ Ready with known issues.** BUG-01 should be fixed first, because it can write a wrong value into the game.

Open bugs: **4** (High 2, Medium 0, Low 2).
- **BUG-01 (High)**: when health is a whole number, its group row in the list is a 4-byte-decimal reading of the same bytes. The row shows 0.00. Set, Freeze and Make a cheat on that row use the decimal type, so Set 200 writes 1,128,792,064 into health. This also happens in plain "I don't know the number" searches, not only percent ones.
- **BUG-02 (High)**: "It's full now", then a percent of 0% while there are still too many matches (for example a broken shield), then any percent once the list is small enough: every match is dropped, the real one too. The search ends with 0 matches.
- **BUG-03 (Low)**: searching `1%` or `0%` lists the same address 2 or 3 times and counts it more than once.
- **BUG-04 (Low)**: a "noted" percent scan says "nothing dropped" but drops decimal values that are infinite or above 1e37. Real game values aren't affected.

## 2. Scope

**In scope:** `core/values.py` (percent parsing, parts, `open_window`, `part_value`), `core/percent_filter.py`, `core/grouping.py`, `core/scanner.py` (percent_scan, mark_full, about_scan, `_percent_pc`, `_note_percent`, values/groups), `core/scan_log.py` Stored as, `core/guide.py` FULL/PERCENT, `console/local_scan.py` / `fake.py` / `ps5debug_ng.py` (ways, MultiScan, scan_restrict), `settings.percent_pc_limit`, `ui/find_wizard.py` (It's full now, About buttons, tree).

**Out of scope / not runnable here:** real-console behaviour (C15, C12b: whether snapshot rows carry the last-scan value), `percent_pc_limit` timing on the PS5, the auto real-vs-copy check, smart cheat builder.

**Risk areas, by priority:** (1) the real value is dropped by a percent check, (2) a group row acts on the wrong address or type, (3) PC vs console disagree at tolerance edges, (4) state left behind after Start over / game change, (5) plain number searches change.

## 3. Test results

The baseline on `d9b02ef` was 676 passed and 1 skipped. Final run with the QA file: **707 passed, 1 skipped, 8 xfailed** (`QT_QPA_PLATFORM=offscreen pytest -q`). Ruff check and format are clean.

All new tests are in `tests/qa/test_health_finding_qa.py`, named `test_tcNN_*` and `test_bugNN_*`.

| TC | Title | Prio | Result | Notes |
|---|---|---|---|---|
| TC-01 | Full search with count exactly at `percent_pc_limit` checks on the PC | H | Pass | `count > limit` is the cut-off |
| TC-02 | One over the limit runs as "went down" | H | Pass | |
| TC-03 | Plain search: % refused over the limit (search kept), PC check at the limit | H | Pass | |
| TC-04 | Full chain over the limit with a heal: went down / went up / about as went down | H | Pass | |
| TC-05 | Full chain on the PC: 40%, heal 65%, About 50/75 (heal)/25 keep exactly health and its fraction | H | Pass | ways: of full, fraction |
| TC-06 | About never keeps plain-percent / x10 matches (AC-P3) | M | Pass | |
| TC-07 | Same % twice over the limit is noted, then the next one narrows | H | Pass | |
| TC-08 | Manual compares between Full and the first % are noted, real value kept | H | Pass | |
| TC-09 | Went down / up / Same / Changed after scan_restrict keep the ways | H | Pass | |
| TC-10 | Stand still after scan_restrict, keeps last_pct | M | Pass | |
| TC-11 | Scan file Stored as header and column after the Full chain | M | Pass | |
| TC-12 | Plain number scan file has no Stored as column | M | Pass | |
| TC-13 | Game change clears full_mode, percent_search, full, values, groups, parts | H | Pass | |
| TC-14 | Start over then a plain number is a plain search | H | Pass | |
| TC-15 | `40.5%` searches whole numbers only as x10 | M | Pass | |
| TC-16 | 4-byte decimal edges for x10 and percent: console path and PC check agree | H | Pass | 410, 390, 41.0 out; 409.99, 39.0001 in |
| TC-17 | Of-full tolerance is ±1 point, inclusive | M | Pass | |
| TC-18 | PS4 game (CUSA) Full chain and grouping same as PS5 | M | Pass | |
| TC-19 | NG client end-to-end (limit 20,000 and 5): Full, went down, %, heal, About, Stand still | H | Pass | both paths |
| TC-20 | Group order: likely real, then smaller, display copies labelled, "didn't change" last | M | Pass | |
| TC-21 | Ratio drifting >1% is not linked | M | Pass | |
| TC-22 | Groups vanish at 1 match | M | Pass | |
| TC-23 | UI: tree goes back to the table when one match is left | M | Pass | |
| TC-24 | UI: Start over and game change clear the tree and hide About | H | Pass | |
| TC-25 | UI: typed-in address under the groups acts on itself | M | Pass | |
| TC-26 | UI: plain number search unchanged (table, no About) | H | Pass | |
| TC-27 | Int health, Full chain: group row acts on the whole number | H | **Fail** | BUG-01 |
| TC-28 | Int health, plain unknown search: group head is the whole number | H | **Fail** | BUG-01 |
| TC-29 | UI: Set 200 on the group row writes 200 | H | **Fail** | BUG-01 |
| TC-30 | 0% as went down, then 30% / About 50% on the PC keeps the real value | H | **Fail** | BUG-02 |
| TC-31 | `1%` / `0%` list each address once | L | **Fail** | BUG-03 |
| TC-32 | Noted scan drops nothing | L | **Fail** | BUG-04 |
| TC-33 | `parse_percent` edge inputs (`%`, `-0%`, `100.0001%`, `40%%`, `nan%`, full-width digits, `1e2%`) | M | Pass | manual, plain-English errors |
| TC-34 | Picked type INT8 with `40.5%` | L | Pass | "no number type can hold that percent" |

The existing feature tests (tests/unit/test_percent.py, test_grouping.py, tests/ui/test_health_finding.py, NG percent tests) all pass and already cover AC-P1 basics, AC-P4, Full again confirm, the scan-12 measurable target and Test the hits on groups. I didn't repeat those cases.

## 4. Bugs

### BUG-01: a whole number's 4-byte-decimal twin heads its group; Set / Make a cheat use the wrong type
- Severity: High. Priority: P1
- Related test case: TC-27..29. Automated tests: `tests/qa/test_health_finding_qa.py::test_bug01_int_health_group_head_is_the_whole_number`, `::test_bug01_plain_unknown_search_group_head_is_the_whole_number`, `::test_bug01_ui_set_on_group_row_writes_a_whole_number`
- Environment: `d9b02ef`, Linux, Python 3.13.18, FakeConsole
- Steps to reproduce: 1. The game keeps health as a 4-byte whole number, max 250 (`0x20000400` = 250). 2. Press It's full now. 3. Set health to 100 and type `40%`, then 50 / `20%`, then 25 / `10%`. 4. The list shows one group row. Select it, type 200 in New value and press Set.
- Expected result: AC-G4: "Make a cheat, Freeze and Test the hits on a group row act on the likely-real address", the whole number, with its own type. The value becomes 200. The same bytes read as a decimal are not a copy (AC-G2), so the list shouldn't show "+1 copy".
- Actual result: the group is `[0x20000400 FLOAT "of full" 3.5e-44, 0x20000400 INT32 "of full" 25]` and the head is the FLOAT. The row shows `0.00` with "+1 copy". Set writes `struct.pack("<f", 200)` and health reads back `1128792064`. Make a cheat makes a Decimal cheat. A plain "I don't know the number" search followed by Went down x3 gives the same head.
- Evidence: `pytest --runxfail -k bug01` gives `assert 1128792064 == 200`, and `Group(members=[Member(... vtype=FLOAT, values=(1.4e-43, 7.0e-44, 3.5e-44)), Member(... INT32 ..., values=(100, 50, 25))], likely=set(), factors=[1.0, 7.1e+44])`
- Suspected cause: `core/grouping.py` `group_matches`. The float view of an int is a denormal, which is exactly proportional to the int, so `link_ratio` links them. `_overlap` only stops them counting as neighbours. Head order is `(not likely, -neighbours, address)`, which ties, and the earlier FLOAT key wins. The PC check also keeps the denormal as "of full", in `percent_filter.matches_way` / `scanner._percent_pc`. Possible fixes: never put two members at overlapping addresses in one group, and keep the one whose value is plausible (drop or demote subnormal floats). Or break the head tie in favour of the type whose decoded value isn't subnormal.
- Impact for the product owner: a player with whole-number health picks the obvious single row, presses Set or Make a cheat, and gets a garbage health value or a cheat of the wrong type.

### BUG-02: after a 0% scan run as a plain compare, the next PC percent check drops every match
- Severity: High. Priority: P1
- Related test case: TC-30. Automated tests: `::test_bug02_zero_percent_then_percent_keeps_the_real_value`, `::test_bug02_zero_percent_then_about_keeps_the_real_value`
- Environment: `d9b02ef`, FakeConsole (`pc_limit=10` to force the over-limit path)
- Steps to reproduce: 1. Shield 1,700 at full: press It's full now. 2. The shield breaks and the game shows 0%: type `0%`. There are still more matches than `percent_pc_limit`, so it runs as "0% (as went down)", and now there are fewer. 3. It recharges to 510: type `30%`, or press About 50% at 850.
- Expected result: AC-P2: the real shield is kept, as 30% of its value when full. If the app can't work out the value when full, it should only note the percent (as it does after a manual compare: "Noted, nothing dropped").
- Actual result: `count=0 history=['full', '0% (as went down)', '30%']`. Every match is dropped and the search is over.
- Evidence: `pytest --runxfail -k bug02` gives `AssertionError: count=0 history=['full', '0% (as went down)', '30%']`
- Suspected cause: `core/scanner.py` `_percent_pc`. The note guard only checks `self.full_mode and last is None and any(key not in self.full)`. With `last_pct == 0`, `percent_filter.full_estimate` returns None for every match, so the of-full way can't match and only plain percent, fraction and x10 can. The guard should also cover `last_pct == 0` (any match with no Full estimate). Or `_percent_pc` should note whenever `est is None` for a match not tied to a plain way.
- Impact for the product owner: a player tracking a shield or bar that hits 0% loses the whole search on the next percent and has to start over.

### BUG-03: `1%` and `0%` list the same address once per overlapping way
- Severity: Low. Priority: P3
- Related test case: TC-31. Automated test: `::test_bug03_low_percent_lists_each_address_once[1-1.5]`, `[0-0.5]`
- Steps to reproduce: a Decimal 1.5 in memory. Type `1%` (or `0%`, with any 0 / 0.5 in memory).
- Expected result: each address is listed and counted once (AC-P1: one search over the three ways).
- Actual result: 1.5 matches both "percent" (0..2) and "x10" (0..20), so it is listed twice and counted twice. For `0%` every zero decimal is counted three times, and every zero whole number twice. On a real game that is millions of duplicate rows.
- Suspected cause: `values.percent_parts` / `local_scan` and NG `_parts_start`. The windows for percent, fraction and x10 overlap when the percent is 1 or less. Dedupe per (address, type), keeping the narrowest way, or clip the windows so they don't overlap.
- Impact for the product owner: a near-empty health bar gives a confusing, inflated match count with doubled rows.

### BUG-04: a "noted" percent scan drops decimals that are infinite or above 1e37
- Severity: Low. Priority: P3
- Related test case: TC-32. Automated test: `::test_bug04_noted_scan_drops_nothing`
- Steps to reproduce: Full search (Decimal) over memory that holds `inf` and `3e38`. Over the limit, type `100%` (same as Full, so it is noted).
- Expected result: hint FIND_PERCENT_NOTED: "Noted, nothing dropped". The count is unchanged.
- Actual result: 1088 goes to 1086.
- Suspected cause: `scanner.KEEP_ALL = (-1e37, 1e37)` BETWEEN. Values outside it, and non-finite ones, fail the range.
- Impact for the product owner: none for real game values. The message is just not strictly true. Fix it or reword the hint.

## 5. Spec deviations and observations (not filed as bugs)

- **Grouping needs 3 recorded scans, not 2** (`GROUP_SCANS = 3`, `MIN_CHANGES = 2`). AC-G1 says "from at least 2 scans". The user guide documents 3, and two changes make a stronger link. Kein should confirm. It delays groups by one scan, which counts against the "≤ 4 scans" target when the first percent checks run over the limit.
- **Full again clears every value when full and runs a Same scan.** This is a documented architecture deviation from "kept until Start over".
- **"Went down by N" in a Full search after the PC check** scales N per way. Fraction parts read it as percentage points, but "of full" parts read it as the raw amount. The meaning is unclear and nothing documents it. A risk, not filed.
- **Test the hits after BUG-01**: the int/float twins leave the search with two types (`sc.vtype is None`), so Test the hits refuses ("works on one number type") even when the grouped list shows a single row.
- A typed range in a Full search is compared to raw values for "of full" parts, not as a percent. A plain number is a percent there (documented).

## 6. Automated tests added

- `tests/qa/test_health_finding_qa.py` has 39 tests: 31 pass, and 8 are strict xfail (BUG-01 x3, BUG-02 x2, BUG-03 x2, BUG-04 x1).
  - TC-01..03: `percent_pc_limit` boundary
  - TC-04..08: Full chain, heals, About, noted scans
  - TC-09..12: PC-side search with plain tools, scan files
  - TC-13..14: reset
  - TC-15..17: int/float parts and 4-byte decimal edges
  - TC-18..19: PS4 game and the NG client end to end
  - TC-20..22: grouping order, labels and limits
  - TC-23..26: Find view tree/table switching, reset, typed-in address, plain search
- Full suite: **707 passed, 1 skipped, 8 xfailed**. Ruff clean.

## 7. Not tested / risks

- The real console (C15, C12b). Whether NG snapshot rows carry the value at the last scan is still the main unverified assumption of the Full chain. Here it is only checked on the fake server.
- Timing of the 5-pass percent first scan and of `scan_results(20,000)` on the PS5. Also how big a `0%` first scan gets on a real game (see BUG-03).
- How the tree looks (column widths, expand arrows) and its keyboard navigation. Only behaviour was asserted.
- Kein's Returnal ≤ 4 scans / 3 minutes target: console only.

## 8. Recommendation

⚠️ Ready with known issues once BUG-01 is fixed. Without that fix, int-health games get a wrong-type row that the player is very likely to act on. BUG-02 should go in the same fix round. BUG-03 and BUG-04 can wait.
