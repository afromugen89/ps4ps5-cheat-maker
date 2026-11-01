# Implementation notes (feature-implementor, v1 build)

## What was built

All of architecture §11 steps 1-11 that don't need a console, in slices (see `git log`):
skeleton + logging + settings (AC-1.3, AC-9), ConsoleClient + FakeConsole + contract tests
(AC-10.1), cheat model + templates + GoldHEN `.json`/`.shn` with golden files (AC-5, AC-6.6,
AC-7), session/scanner/freezer/Lock it core (AC-2/3/4, D8), the real PS5Debug-NG client and
ftpsrv uploader (AC-6.3), and the PySide6 screens (AC-1 to AC-9). Step 12 (PyInstaller build
and the AC-8.1 usability test) is not done.

## Decisions inside the design

- `ConsoleClient` gained `foreground_app()` (game title + version for AC-2.1) and the scan methods
  take the region list and progress/cancel. Small additions to the sketched interface.
- Real client: first scan runs on the console (turbo scan streaming, falling back to the classic
  staged scan), in 64 MiB slices for progress/Cancel. "Next scan" re-reads candidates in bulk with
  `CMD_PROC_SCAN_GET` and compares on the PC (candidate lists are small after an exact first scan).
- Writes rely on PS5Debug-NG 1.3.2's server-side verified writes, not a PC read-back (a read-back
  races with the game during freeze).
- Lock it decodes backwards from the watchpoint RIP with iced-x86 and keeps the longest
  memory-writing instruction that ends exactly at the RIP; patches are NOPs, `off` = original bytes,
  and the code is checked before patching.
- `.shn` is written exactly like the real GoldHEN files (declares utf-16, stored as ASCII with CRLF).
- Saved cheats that came from a value write their value once when toggled (files can't freeze).

## v2: cheats that survive a game update (2026-11-01)

Spec §5 left two items for v2: wider code patching and update-proof cheats. Lock it,
player-only caves and the pointer scan already shipped in the v1 branch, so v2 starts
with update-proof cheats (`core/updater.py`).

- **Learn** (old version running, read only): for every code line that isn't a cave,
  store a signature: the original bytes plus the following instructions, with jump
  distances and RIP-relative displacements as `??`. Grown until unique in the game's
  code (16 to 96 bytes). Saved to app data `signatures/{title}_{version}.json`.
  Runs automatically whenever a file's own version is running and a line has no
  signature yet. Live bytes are overlaid with each line's `off` bytes, so cheats can be on.
- **Move** (new version running): patches move to their unique signature match
  (fallback: the `off` bytes alone, 6+ bytes, unique, reported as a guess). Caves
  (patches over zero bytes) move together to spare zeros at the end of the code, and
  lines inside a cave move with it. Every jump / RIP-relative operand in moved code is
  re-aimed; a target outside the moved patches and caves fails that cheat with a reason.
  Instruction sizes never change, so short jumps inside caves stay valid.
- Not handled: saved static values (`MODULE_VALUE`), session-only cheats, and patches
  whose original instruction itself changed between versions (reported, not guessed).

## Unknown-value search on the PS5 (resolved 2026-11-01)

Kein asked for it to be built. It uses PS5Debug-NG's own snapshot engine, so memory stays
on the console: `TURBOSCAN_START` with `TS_SNAPSHOT | TS_SNAPSHOT_SEGMENTS` (compare 11,
no seed), then `TURBOSCAN_COUNT` with `TS_SERVER_RESIDENT` for Went up (5) / Went down (7)
/ Changed (9) / Same (10) / exact (0, fuzzy 1 for floats), `TURBOSCAN_GET` for the rows,
`TURBOSCAN_END` to free it, and `0xBDAACC17` cancel on a second connection. Wire order was
confirmed on Kein's PS5 (NG 1.3, engines 0xfff). Float snapshots set `TS_FLOAT_SIMPLE`.
The snapshot is tied to the connection, so a reconnect means starting the search again.

## ARCHITECT CONSULTATION (original question, kept for history)

```
Decision: How the first "unknown value" search runs on the real PS5.
Context: Architecture Q3 says unknown-value snapshots stay on the console (turbo scan,
  server-resident mode). The resident/snapshot sub-protocol isn't fully documented for
  multi-region sessions (ps5dbg also leaves it unimplemented). Pulling all writable memory to
  the PC is GBs over Wi-Fi and too slow to compare in Python.
Options I see: (a) turbo scan server-resident mode, one session per region, after C3 shows the
  wire format works; (b) unknown-value limited to a size cap (e.g. 256 MB of the largest
  writable regions) pulled to the PC, with a warning; (c) leave it fake-only in v1 and rely on
  exact search + Lock it.
Blocking: AC-3.3 on the real PS5 only. Everything else works; the UI shows a plain message.
```
