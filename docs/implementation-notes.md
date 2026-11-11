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

## v3: find it again (2026-11-02)

Built from architecture v3 (recommended "anchor hook + class (vtable) fallback",
approved by Kein), steps 1-8, plus the pointer-scan cap fix from step 10. Step 9
(moving hooks and slots on a game update) is deferred to v3.1 (D17).

- `console`: `ConsoleClient.watch(pid, addr, size, kind, timeout) -> [WatchHit(rip, regs)]`,
  `kind` "write" or "access" (breaktype 3, PROTOCOL.md §7.3). The real client parses
  `reg64` (FreeBSD `struct reg`) from the 0x4A0 trap packet. **UNVERIFIED** on the
  console: breaktype 3 and every register except RIP (C10a, `tools/watch_regs.py`).
- `core/project.py`: `.cmproj` in app data `projects/{titleid}_{version}.cmproj`
  (objects, fields with plausible range and last value, anchors, live cheats, history).
- `core/cave.capture_code`: the capture routine (vtable filter `cmp qword [obj], imm32`,
  or `push tmp; mov tmp, imm64; cmp [obj], tmp; pop tmp` for a vtable above 2 GiB;
  Player-only field filter, or none; optional keep-at-value). Slot = 16 bytes (object
  address + counter) just below the Player-only table. The keep-value (PC-off) routine
  has its own room just below the capture routine, so switching only re-aims the jump.
- Safety rules from review: only an instruction of 5+ bytes is hooked (the jump never
  covers a `ret`, a branch or code other paths jump into); spans with a `call` are
  refused; only candidates whose registers prove `[reg+disp]` (64-bit reg, no fs/gs, no
  index, disp >= 0) are used. Routines are never zeroed after unhooking (a preempted
  thread may still be inside); install reuses a routine left in place. Hooks the app
  put in are recorded the moment they go in and come out on close, or when the last
  cheat on that value is turned off (back in when one is turned on again). A hook
  CheatRunner loaded is never taken out. An object without a class check (weak) that
  moves is not followed until the player confirms the new place.
- `core/anchor_hook.py`: hook candidates from a read-write watch (readers first, must
  be `[reg+disp]`), plan, prove slot (one write watchpoint on the first 8 bytes for 60 s
  in the app, plus a value check of all 16; C10b runs 10 min), install (never twice;
  detects CheatRunner-loaded bytes), remove, read slot, and the routine as code lines.
- `core/vtable_find.py`: one exact 8-byte search for the class pointer.
- `core/refind.py`: learn (static -> done; else hook + vtable, optional memory path),
  find again in the order static -> proven chain -> slot -> unproven chain -> vtable
  scan, verify before any write, disagreement = write nothing. A chain that re-checks
  on the first restart is "proven" and then the hook isn't installed (simple games stay
  simple). `Tracker` re-verifies every freeze tick and re-finds at most once a second.
- Freezer: tracked entries pause (write nothing) until the target verifies again.
- UI: My cheats -> Find it next time / Find it again; auto find on connect; confirm
  once per run; pick dialog when methods disagree; failed -> search pre-filled; Save
  explains app-only cheats and offers the hook in the file (off by default, D14);
  "Also make a PC-off cheat" (capture + keep value, AC-13.1).
- Decisions inside the design: the slot proof watches 8 of the 16 bytes (NG watchpoint index 0 only) and checks the
  other 8 by value. Fake-PS5 "two dumps" are generated in code (`tests/fakegame.py`,
  with a tiny interpreter for our routine), not recorded: no Returnal dumps exist yet.
- Fixed: the pointer scan's 20,000-node cap kept the first nodes in memory order; it
  now keeps the nearest (smallest offset).
- QA round (2026-11-02, BUG-01..09): Connect again takes hooks out first, and a hook
  this app put in is still ours after a lost link or a refresh (install's "already"
  for it is recorded, CheatRunner's are not); the tracker follows the hook's new object
  while the old one still verifies (filtered hooks only: an unfiltered one names
  enemies too and is asked only after verify fails); the PC-off cheat leaves its
  routine in place when off; the keep routine still runs the game's writer, then
  stores (flags for `sub [rax+2ACh],ecx ; jle`); `.cmproj` format 2 with type checks,
  unreadable files kept as `.cmproj.bak`, and format-1 hooks dropped (no PC-off room:
  press Find it next time again); memory paths are tried before any hook; a class
  check widens the plausible range; Find it again's memory search ends the player's
  search visibly. New caves and slots skip the rooms the loaded `.cmproj` keeps.
- **Known limit (C10)**: if the link drops while a hook is in, the app can't take it
  out; it stays in the game until the game restarts. When the same app reconnects to
  the same game process it recognises the hook as its own and takes it out on close.
  After the app itself restarts it can't tell its hook from one CheatRunner loaded
  (same bytes), so it leaves it in (harmless: the routine only records where the
  object is). Restart the game to clear it.

## Hard values, Phase 1 and 2 (2026-11-03)

Spec: `docs/spec-hard-values.md` (AC-H1 to AC-H6).

- **Changed by** (`Compare.INCREASED_BY` / `DECREASED_BY`): the value passed to the client
  is `(amount, tolerance)`, like BETWEEN's `(low, high)`, so the `ConsoleClient` contract
  doesn't change. PC compare uses the tolerance for floats and an exact difference for whole
  numbers; NG gets the amount only (IncreasedBy 6, DecreasedBy 8; its float rule is
  unknown until C11a).
- **Tolerance**: `FLOAT_TOLERANCE` is now 1.0 (strict), the same as NG FuzzyValue, instead
  of 0.5. Typed decimals, and any scale, turn an exact search into a ValueBetween search for
  `value +- unit` (open ends): on the console for the first scan and for a classic-path
  re-scan with too many candidates to read, on the PC otherwise. Reason: NG's exact float
  search is fixed at +-1, which for 0.87 would keep millions of values.
- **Changed by** on whole-number types refuses an amount that isn't whole after scaling.
- **Scale** lives in `Scanner.scale` until Start over. Fraction defaults to Decimal
  (4 bytes); x10/x100/x1000 of a whole number default to 4-byte whole numbers.
- **Guide** (`core/guide.py`) is pure logic; the Find view highlights one button. If the
  player skips the first question, the guide assumes a number for a Search and a bar for
  "I don't know the number". "Try it" runs the next way to store the value.
- **Test the hits** (`core/hit_tester.py`): writes once (no freeze) to half A, restores the
  original bytes after each answer. Each original is noted before its write. Writes and
  put-backs run off the UI thread; Stop or closing the window during a write asks the
  round to stop and closes once the values are back. Test values are typed and asked
  about in the units the game shows (the search's scale). Values the game changes during the few seconds of a
  round are restored to what they were before the round. Limit 500 matches.
- Strategy item "pointer scan keeps the first 20k nodes" was already fixed on main
  (`keep_nearest`), so it's not part of this work.

## Faster scanner (2026-11-07)

Read against PS5Debug-NG's own source (Pharaoh2k/ps5debug-NG, 1.3.2: `main.c`,
`scan_turbo.c`, `PROTOCOL.md`). What made searches slow, and what changed:

- **Round trips.** C3's 4-byte search sent 2,842 requests. NG serves each connection on
  one thread that sleeps 10 ms whenever its socket is idle, so every request paid a
  round trip plus up to 10 ms. Now (a) writable ranges less than 1 MiB apart share a
  request (`plan_slices`; NG reads unmapped memory as zeros, and `inside` drops matches
  that land in a gap), and (b) slices are shared out over `scan_connections` sockets
  (default 4) that scan at the same time. NG documents several connections as the
  better way to parallelize; with more than one, `TS_PARALLEL_COMPARE` is not sent.
  A connection that breaks hands its slice back; if none can be opened the scan runs
  on the main one.
- **Copying memory.** In NG 1.3.2's source `TS_PARALLEL_COMPARE` only applies together
  with `TS_USE_ALIASING`, which we never sent. Aliasing reads the game's memory in place instead of copying it
  (NG falls back to copying on any page it can't map). Sent when the console advertises
  `TSE_ALIASING` and `fast_scan` is on; `TS_RESCAN_ALIASING` likewise for narrowing an
  unknown-value search on the console (NG: about 2-3x).
- **Next search with many candidates** re-scanned every range that held a candidate
  (ranges can be gigabytes). Now only windows around the candidates
  (`candidate_windows`, candidates less than 4 MiB apart share one).
- **Reads one at a time** (~36 ms each). `read_many` now pipelines plain `PROC_READ`s
  (NG reads requests in order with exact lengths, so queued requests are safe), so
  re-checking candidates and the live match list cost about one round trip per 128
  reads. The read path now takes up to 4,000 read groups (was 400), so Went up / down
  works on bigger lists of an exact search.
- Not changed: INT64 on 4-byte alignment and float (fuzzy) searches can't use NG's SIMD
  compare (it needs alignment = size and exact compare); they gain from the extra
  connections only. Scan files cost about 0.3 s per 100,000 lines: left as they are.
- Possible next step: NG's region classify (`0xBDAACC16`) reports uncached GPU memory
  that reads at ~40 MB/s; skipping it could cut first searches further, but it only
  lists 8,192 regions (Returnal has 38,108 maps), so it needs a console look first.
- Console test C13.

## Unknown search over several number types (2026-11-07)

Kein asked for "I don't know the number" to cover whole numbers too. With no type
picked, it now searches `UNKNOWN_TYPES` (4-byte decimal and 4-byte whole number) side by
side. NG keeps one snapshot per connection, so each extra type gets its own connection
(`_multi_start`), and the snapshots are made and narrowed at the same time. A typed
number or range is adapted per type (`value_for`: 87.5 can't be a whole number, a range
is rounded inward), a type with no matches left is dropped (its snapshot freed), and
results carry their `vtype`. The Scanner's `vtype` is None while more than one type is
left (`Scanner.mixed`); scan files then add a Type column. If no second connection can
be opened, the search runs on the first type only. FakeConsole uses `MultiScan`.
Console test C14.

## Find the real one behind it (2026-11-11)

Spec: docs/spec-trace.md, design: docs/architecture-trace.md (steps 1-13, one commit
each). New core modules `trace_way` (how a real value becomes the shown one, float32
faithful, with an inverse that nudges up to 8 ULP), `trace_flow` (decode a window around
the writer, follow the stored register back, work out source addresses, one caller up)
and `tracer` (the hop driver). UI: `ui/trace_dialog.py`, traced rows in the Find tab,
guide `Action.TRACE`. Test tooling: `tests/asm.py`, `tests/tracegame.py`, minicpu
extended (checked against unicorn by `tests/qa/test_minicpu_vs_unicorn.py`).

Choices inside the design, and where it differs from the sketch:

- `trace_flow.follow` takes the shown type (`shown`) so a step's `to` type is known
  without a second pass; `follow_reg`, `store_address`, `caller_window` and `Path.lane`
  are extra helpers. `follow_into_caller` takes the return address, register, lane and
  the hit instead of a prepared caller window. `Candidate.expr` is always an address
  expression without outer brackets (`rbx+10h`, `[[rbx+30h]+8h]+10h`, `watched+(rsi-rdi)`).
- `Tracer` takes `settle_s` (for tests); `TraceResult` carries no message text, only the
  outcome plus `value`, `max_now` and `reason`; progress reports a `Stage`, and every word
  lives in `ui/hints.py`. The "change it once more" wording (`TRACE_CHANGE`) is ours.
- A load-then-store of the same address (read-modify-write) is detected from the
  instructions, not from addresses. The circle check (a source we watched already) runs
  before the write test; a read-modify-write found at hop 2 or later ends FOUND with no
  verdict (shown as "not sure").
- `TraceDialog` takes `frozen={addr: size}` from the Find tab, because the Freezer's
  entries are private. Overlapping freezes and enabled cheats are released during the
  trace (cheats off with `restore=False`) and put back afterwards.
- When Check which is real marks every row a copy, the guide's TRACE hint replaces
  `CHECK_REAL_NONE` (AC-T1). Spotting a Set/Freeze put back by the game is best effort: one
  look 2 s later (`PUT_BACK_S`).
- Step 10 briefly mapped `Action.TRACE` to the Check button until step 11 added
  `trace_btn`.
- Known limitation: a `movzx` of an unsigned 1- or 2-byte number is treated as the signed
  `ValueType` of that size, so values above 127 / 32767 would read wrong.

Unverified console facts, each logged where it is used rather than assumed silently:
registers in a trap (`action=trace_*` with the raw packet, C10a/C20a); `rep movs` reports
its own RIP mid-copy (`rep_at_rip=True (UNVERIFIED ...)`); whether NG sets RF when it
continues after an execute breakpoint (a repeating hit is logged as "the breakpoint
repeats" and ends the fallback; `trace_exec_breakpoint` stays off until C20d). The button
stays under Advanced "(being tested)" until C20a passes (`hints.TRACE_TESTED`).
Console tests C20a-i.

Review follow-ups (2026-11-11):

- A wider whole-number load (`mov rax,[m] ; mov [d],eax`, or `movzx` wider than the
  copy) makes the source the loaded size, with `Kind.TRUNCATE` (wraps forward, kept as
  is going back). `cvt(t)ss2si` into a 1- or 2-byte copy works in a 4-byte register and
  keeps the low bits; an 8-byte whole number to float32 rounds once.
- `writer_at` prefers a `rep movs` at RIP only when the instruction ending at RIP
  doesn't store over the watched address by the trap's registers. `xorps`/`xorpd`
  with a sign mask is a negation: not followed (not "scrambled").
- `Tracer.run` turns any `CheatMakerError` into an outcome: a copy freed mid-check is
  NOT_FOLLOWED, anything else the new FAILED; `trace_result`/`trace_restore` are logged
  on every path, `not_restored` is always attached and shown for every outcome.
- Running out of TOTAL_S in the memory check ends TOO_FAR with the last tested source
  "not sure"; watches are capped at the time left.
- A read-modify-write writer decides "already real" only with strictly the most
  hits, or when no other writer's source passes the check (a tie goes to the check).
- "Search for it that way" isn't offered when the way is the copy's own search (same
  type, factor 1, no share of a max): NOT_FOLLOWED.
- Traced rows are listed by (address, type) next to a search match at the same
  address, and screen units convert only on a row of the traced type. When no real
  value shows the typed number exactly, the nearest is written and the player told.
- The button needs Advanced on while it sits there; suggesting it turns Advanced on.
- `release` resumes what it paused if pausing fails half way, and resumes each item
  on its own; only freezing cheats are paused (a one-shot cheat isn't written again);
  anchored cheats are matched by their tracker's address. A cancelled or closed
  dialog still reports `not_restored` on the status line; a disconnect waits up to
  3 s (`STOP_WAIT_S`) for the trace to put its test values back.

QA and re-review round (2026-11-11, qa/reports/2026-11-11-trace-shown-number.md):

- The walk keeps the narrowest width the value passes through (the writer's store,
  narrower register moves and extends; carried into a caller as `Path.width`). Only a
  load wider than that gets TRUNCATE, so `mov rax,[m] ; mov [d],rax` watched at its low
  4 bytes is a 4-byte source (a Set no longer writes over the field next to it).
- `mov [m],ah` is byte 1 of rax; a move from a high-byte register on the path isn't
  followed. `xchg [m],reg` is a store, not read-modify-write. `movss x,[K] ; mulss x,[m]`
  (and VEX) is followed as `[m]` x K.
- Inverse: an 8-byte decimal narrowed to 4 bytes on the way is nudged in float32 steps;
  a decimal screen value equals the typed one to its digits (relative 1e-6), not by bytes.
- A self-copy writer follows the read-modify-write rule (most hits, or no other source
  passes). The exec fallback carries the source load's registers forward to the max load.
  The guide suggests the trace when every checked row is a copy, ignoring failed rows.
- Find's rows are told apart by (address, type) for "added", "frozen", Freeze and Remove;
  the type each Find freeze was made as is kept in `FindWizard._frozen_type`.
- `TraceDialog.release` pauses Find's freezes from the Freezer's own entries and resumes
  them with the same bytes, read through the Freezer's public `fixed_writes(prefix)`
  (added once the restarts branch landed on main).
- A share of a max of 0 shows "(screen: ?)". TOO_FAR says "1 step", and "ran out of time"
  when TOTAL_S ended it.

## Bring notes to a new game version, steps 1-6 (2026-11-11)

Design: docs/architecture-game-update.md. Steps 7-12 (NoteFingerprints, CarryOver/CarryBar,
the run driver, AC-U9 in do_update, tools/make_old_notes.py, docs) come in a later pass.
Small changes against the design, each keeping the spec's behaviour:

- **Golden notes file is format 4.** `tests/fixtures/golden/...cmproj` now says
  `"format_version": 4` (nothing else changed), so its byte-for-byte round trip still
  holds. Formats 1-3 load as before (test covers 1, 2 and 3). The QA tests TC-50/TC-51
  compare with `pj.FORMAT_VERSION` instead of a literal 3 (same rule, one step up).
- **Reset the class before locate, not in settle.** `rf.verify` checks the saved class,
  which moved with the update, so a static or chain object would never verify. `prepare`
  calls `reset_class` on every object without a hook (and `drop_hook` on a hook that can't
  come over); `settle` still resets an object that ends up with no hook.
- **`choose(session, classes, item)`** takes the session (the Player-only route reads the
  ident dword and calls `cv.compare_objects`) and raises `CarryFailed` instead of
  returning it. A pick is answered with `answer(session, classes, need, address)`;
  "None of these" fails the hook with "none of the places was picked".
- **Steps take an `Item`** (new copy, old object, hook site, shift) instead of
  `(obj, site)`; `collect_classes` and `place_hook` take `reserved=` (the plan's rooms,
  which `place_hook` extends with the new hook's rooms).
- **The hook's own capture rooms are laid over with zeros** in `learn_note_signatures`
  (as well as `(hook, orig)` and the code parts), since the routine holds a copy of the
  hooked instruction.
- **Reasons use "it"** ("the game's code changed where it was found") to fit the summary
  line `Not brought over: Shield (...)`; `find_hook_site(..., label=)` can name the value.
  A failed part reads "part jump couldn't be found (...); make it again".
- **`CarryReport.summary`** says "Brought over N of M values" when the notes have no cheats.
- **`list_sources`** lists older versions (closest first), then newer ones.
- **Fake game:** `build(static=True)` puts Obolites at `layout.static_off` (off by default,
  so no existing search sees an extra value); `fight()` also runs the reader
  (`Game.show`), so a collect on a reader hook sees objects. In run 1 the two enemies
  share one sampler table entry (hashed by address), as on a console.
- **`carried_yes` doesn't emit `cheats_changed`** (it runs inside the tick handler, which
  refills the table itself); `check_pending` emits it after a Yes there.
- **`FindAgain.carrying`** is a property with a `carrying_changed` signal so CheatsView
  refreshes its buttons the moment a bring-over starts or ends.
