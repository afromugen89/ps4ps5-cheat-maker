# Console test results

## C1: connect and detect (2026-11-01, Kein's PS5, PS5Debug-NG reports "1.3")

| | PS5 game | PS4 game |
|---|---|---|
| Title ID / version | PPSA01285 (Returnal) / 03.000.005 | CUSA33778 / 01.03 |
| Detected | yes (after the pid-0 fix) | yes |
| Memory maps | 38,108 (36,467 writable, 12.0 GiB) | 48,722 (44,411 writable, 4.3 GiB) |
| Main module | maps named `executable`, base 0x400000 | maps named `executable`, base 0x400000 |

Findings that changed the code:
- Process info for pid 0 makes PS5Debug-NG close the connection: never asked now.
- The foreground-app command works and gives the version; its name field is the process name ("eboot.bin"), so the title ID is shown instead.
- The game's module is the `executable` maps. Maps named `(NoName)eboot.bin` are elsewhere and were wrongly picked as the base before.
- Writable memory is large (up to 12 GiB, ~4,000 ranges after joining touching maps). C3 measures whether the first search fits in 60 s.

## C2: not reachable: passed (2026-11-01)

## C3: first search (2026-11-01, Returnal, a10316f)

- Exact 4-byte search: ~3 minutes, 637,388 matches. Misses the 60 s target (AC-3.5).
- Next build (parallel compare + timing log) to re-measure.
- Re-measured on fa89a9c (parallel compare on, flags=0x80): 101.7 s, 2,842 requests, 24,677 regions, 11,857 MiB, 570,058 matches. About 117 MiB/s. Still over 60 s.
- Decision (Kein, 2026-11-01): accept ~100 s on big PS5 games; AC-3.5 now 2 min for large games, 60 s for smaller; progress bar shows time left (f3ff28e).
- Second search: PROC_SCAN_GET (bulk read) timed out on NG 1.3 with 49,538 candidates. Replaced: few candidates (<=400 read spans) use plain reads; more re-scan the candidate ranges on the console and keep the overlap.

- C3 Set value: NG 1.3 expects PROC_WRITE as header (pid, addr, size) -> OK -> data bytes -> OK. Sending the bytes inside the request hung the server. Confirmed on the PS5 by writing back an unchanged value; fixed in the client.
- C3 Obolites (Returnal): INT32 search 10 -> 51 -> 13 -> 35 left 4 heap addresses; Set/Freeze wrote all 4 and they held 1,000,000, but the in-game count reverted on each pickup. All 4 are copies (UI/event objects that point at each other); the real counter was not found as INT32. Writes themselves verified.
- C3 PASSED (Kein, 2026-11-01): Set and Freeze hold the value in game after re-searching.
  - Returnal Obolites real counter is INT64 at 4-byte-aligned addresses (0x1020f48f34, 0x1020f49354). INT64 first search 7,737 matches in 152.9 s (over the 2-min AC-3.5 target), then 2 matches. INT32 hits were display copies. Hint now suggests 8-byte search when Set doesn't stick.
- Health (Returnal): INT64 100 -> 80 gave 0. Float exact searches on the PS5 used NG ExactValue (bit-exact), so a rounded on-screen number missed. Now float/double searches send NG FuzzyValue (|diff| < 1.0, scan_compare.c) and the app keeps its own +-0.5 rule on the results. Note: NG's parallel flag only applies to ExactValue, so float searches may be slower. Needs a console run.
