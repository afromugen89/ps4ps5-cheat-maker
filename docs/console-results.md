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
