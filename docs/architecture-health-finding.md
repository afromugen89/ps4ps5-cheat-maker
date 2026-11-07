# Architecture: percent search and grouping (spec: docs/spec-health-finding.md)

No new dependencies or services. Generic for every game; plain number search unchanged.
Assumption: the bare values in `TURBOSCAN_GET` snapshot rows are each match's value at the
last scan (C12b). The second/third row values are not relied on.

## Overview

A **part** = (number type, storage way). The client keeps one candidate list per part
(extends PR #21's one list per type). A **way** is the existing `Scale` dataclass
(stored = shown x factor): percent (x1), fraction (x0.01, Decimal only), x10. "of full" is a
label only. The client applies each part's way to the value the Scanner passes, so
`scan_next(cmp, value)` keeps its signature. Percent-of-Full is a pure PC filter; the
Scanner hands survivors back with new `scan_restrict(hits)` and the search continues on the
PC. Grouping only uses value history the Scanner collects in `_record()`.

## Decisions

1. **AC-P1 ways.** `values.parse_percent(text) -> (value, unit) | None` (None if no `%`,
   plain-English error outside 0..100). `percent_parts(value, picked_type)`: FLOAT x
   (percent, fraction, x10); INT32 x (percent, x10) only for whole numbers; only the picked
   type if one was picked. Each part's window = `window(value, unit)` x factor (40% -> 40±1,
   0.4±0.01, 400±10). `scan_start(..., ways: tuple[Scale, ...] = ())`; with ways, vtype may
   be a tuple and cmp must be BETWEEN. `ScanHit.way: Scale | None` (compare=False like
   vtype). `LocalScanSession.way`; `MultiScan.next` scales the value per session then
   `value_for`. Empty parts dropped (`drop_empty`), `merge_results` carries way. New
   `scan_way_counts()`; `scan_counts()` stays per type. NG: one console BETWEEN pass per part
   (NG has one range per compare), `self._scan` becomes a MultiScan; the candidate-window
   console rescan becomes a per-session helper. Later plain numbers typed in a percent
   search count as percent; history shows "40%". Percent search ignores Advanced
   "Game stores it as".
2. **AC-P2/P3 Full.** "It's full now" = unknown first scan (FLOAT+INT32), sets
   `full_mode=True`, `last_pct=100.0`. On `N%` / "About X%" in Full mode:
   - count > `percent_pc_limit`: plain console Went down / Went up / Same (N vs last_pct),
     then `last_pct=N`, hint "Too many to check by percent yet...". History
     "40% (as went down)".
   - count <= limit: `scan_results(count)` (values at last scan, when game showed last_pct);
     `full[a] = value*100/last_pct` (kept once known); read current values with read_many;
     `percent_filter.keep(...)` keeps percent/fraction/x10 windows plus of-full within
     `±tol + N*u_prev/last_pct`; then `scan_restrict(kept)` tagging type + way. About X%:
     ±12.5 points, only of-full and fraction ways.
   - `full == 0` never matches. Manual went up/down/changed/by sets `last_pct=None` unless
     `full` known; then a percent scan over the limit uses Changed and hints to do a
     percent scan first.
   - `scan_restrict(hits) -> int` on ConsoleClient, FakeConsole and NG: replaces the search
     with a PC-side MultiScan grouped by (type, way), each hit's value = baseline. NG: closes
     multi snapshots / resident search, sets `_scan`, `_scan_pid`, `_scan_ranges_list`;
     later scans use pipelined reads.
   - `percent_pc_limit` setting, default 20,000, clamped 1,000..100,000.
   - Re-mark Full during a search: confirm ("Keep it full and don't touch the
     controller"), run a Same scan, `last_pct=100`, clear `full` (deviation from spec: it
     can drop matches).
3. **AC-G grouping.** Scanner keeps `values_by_scan: dict[int, list[float | None]]`;
   `_record()` always fetches rows when count <= GROUP_LIMIT (= hit_tester.TEST_LIMIT, 500);
   prune dropped addresses; reset on Start over / new first scan. `core/grouping.py`:
   `Member(address, vtype, way, values)`, `Group(members, heads, factors, label)`,
   `group_matches(history, tags, ratio_tol=0.01, near=64, far=65536) -> list[Group]`.
   Link: >=2 shared scans, both changed, ratio from first shared non-zero scan, all shared
   scans within 1%; zero on one side only breaks it; union-find. Likely real: another member
   or another changed match changing in the same scans within 64 bytes at a factor not ~1.
   "copies (probably display)": all same value and all >64 KiB apart. Order: likely real
   first then most in-group neighbours; groups with likely real first, then smaller; never-
   changed matches alone, last. `scanner.groups()` -> [] unless 2..500 matches and >=2 scans.
   UI: QTreeWidget, one top item per group (head value, way, "+N copies", tag), children
   members; each item stores its index into rows; Freeze/Make a cheat/Test the hits act on
   head for a group row, member for a child. Grouped list shown when <=500 matches and
   group rows <= show_limit; filter box hides a group when no member matches.
4. **Log, history, guide.** ScanLog "Stored as" column (percent, fraction, x10, of full,
   plain) when any hit has a way; header `# Stored as: ...`. History labels "full", "40%",
   "about 50%", "40% (as went down)", "full again (same)". Guide: Action.FULL/PERCENT,
   Scan.FULL/PERCENT; start(BAR) -> "Fill it up, then press It's full now."; after FULL /
   narrowing in Full mode -> "Make it go down, then type the %... or pick About 75 / 50 /
   25%."; percent search marks fraction and x10 tried. hints.py: FIND_PERCENT_TOO_MANY,
   FIND_FULL_CONFIRM, FIND_GROUPS.

## Risks

- Percent first scan = 5 console passes and their hits to the PC; INT32 x10 may be big
  (drop it if C15 is slow).
- Full chain trusts bare row values are last-scan values (C12b).
- Estimating Full from a non-100% point carries ~±(1/p) relative error; tolerance widened.
- Grouping appears one scan after the list does; non-match neighbours can't vote.
- QTreeWidget swap touches find_wizard selection code and its tests.

## Steps

1. values.py ways + parse_percent (+tests). 2. client.py / local_scan.py / fake.py: way,
   scan_restrict, scan_way_counts. 3. ps5debug_ng.py parts + MultiScan + scan_restrict
   (+fake server tests). 4. core/percent_filter.py (+tests). 5. scanner.py percent_scan,
   mark_full, about_scan, Full mode, values_by_scan; settings percent_pc_limit.
   6. core/grouping.py (+tests incl. a scan-12-shaped case: 59 matches -> <=8 rows, head
   0x12E0EA40B0 or 0x12E0EA40A0); scanner.groups(). 7. scan_log, guide, hints.
   8. find_wizard UI. 9. docs/console-tests.md C15a-c; user-guide.md.
