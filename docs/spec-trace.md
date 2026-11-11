# Cheat Maker: Find the real one behind it (trace from the shown number)

Date: 2026-11-11. Phase 3 of docs/spec-hard-values.md. Asked by Kein: the app should handle
hard values by itself, no script editors, generic for every game (Returnal is only the
worked example).

## Why
Searching the exact number on screen is easy, but what it finds is often a copy kept for
the screen (HUD, menu, a widget). Freezing a copy does nothing. Check which is real can
only choose between matches that were found; when every match is a copy, the real value
was never in the list (it is a fraction, x100, an 8-byte number, or several copies away).

## How it works (plain words)
The game copies the real value into the screen copy over and over. The app watches who
writes the copy, reads the game's code just before that write to see where the number was
read from and how it was changed on the way (x100, decimal to whole), works out that
address, checks it really holds the matching number, and tests it with a write as Check
which is real does. If that place is also a copy, it does the same again, up to 4 times.

## Words used here
- **Copy**: a match the game writes again from somewhere else.
- **Source**: the place the game read the number from before writing the copy.
- **Hop**: one step from a copy to its source.
- **Way**: how the real value relates to the screen: its number type and factor
  (screen = real x factor), e.g. "Decimal (4 bytes), screen shows it x100".

## Depends on
- Console test C10a (registers in a watch hit). Until C20a passes, the button sits under
  Advanced with the label "(being tested)". [OPEN: C10a / C20a]

## Acceptance criteria

### AC-T1 Where it starts
- Given the Find list shows rows, When the player selects one row that has an address and
  a number type of 1, 2, 4 or 8 bytes, Then **Find the real one behind it** is enabled.
  Otherwise it is disabled with the tooltip "Pick one row in the list."
  [OPEN: Kein, button name. Default: "Find the real one behind it"]
- A group row traces its head (as Set does); an expanded member row traces that member.
- The guide (core/guide.py) suggests it (one highlighted button, one hint line) when:
  Check which is real marked every checked row **copy**; or a Set/Freeze on the selected
  row was put back by the game (the value differs from the written one within 2 s). Hint:
  "These look like copies the screen uses. Press Find the real one behind it."
- Before the first trace in a run, the same one-time notice as Make it work: "Save your
  game first. This writes test values and briefly pauses the game."

### AC-T2 Watching the copy (each hop)
- Given Start is pressed, Then any freeze the app holds on the watched address is paused
  for the trace and resumed after it; the hint says "Keep playing and make the number
  change (take a hit, spend some) within 30 seconds."
- A write watch runs on the address for up to 30 s, ending early 2 s after at least 3 hits.
- Alignment: an 8-byte value not 8-aligned is watched as its 4 aligned low bytes; a
  4-byte value not 4-aligned as its aligned 2 bytes containing the address (if neither
  fits: "This address can't be watched", no trace).
- Writers are found as Lock it does (`instruction_before`). Up to 3 distinct writers,
  most hits first, are traced; a writer that stores a constant is skipped (Advanced:
  "sets it to a fixed number").
- No hit: "Nothing wrote to it. Make the number change while it watches, then try again."

### AC-T3 Reading the code before the writer
- The app decodes up to 256 bytes before the writer (straight line, same voting as
  `instruction_before`) and follows the written value backwards until it reaches one of:
  a memory load (a source), a constant, the result of a call, the start of the code block
  (ret, int3/padding, an unconditional jump), or a step it doesn't follow.
- Steps followed (each recorded in the way):
  - register moves, zero/sign extension, moves between general and XMM registers;
  - int <-> float and float <-> double conversions (round or cut, recorded which);
  - multiply/divide by a constant (immediate, or a constant read from a RIP-relative
    address);
  - divide by a second loaded value: that value is a **max**; the numerator is the source;
    factor = constant / max, taken from the max's value now (max address under Advanced);
  - the lane offset inside a vector load/store (value at byte k of the store comes from
    byte k of the load).
- Anything else (add/sub of a value, xor/rol/not/bswap, two loads multiplied, branches
  inside the path) stops the hop with a reason under Advanced. xor/rol/not/bswap gives the
  result "scrambled" (AC-T8).
- The value read from the same address as the watched one (read-modify-write) means the
  watched address is already real: hop ends, AC-T7.

### AC-T4 Working out the source address
In this order; the first that gives an address passing AC-T5 wins:
1. **From the registers at the trap**: base/index registers not written between the load
   and the writer (and not by the writer) are taken from the hit; address = base +
   index x scale + displacement. RIP-relative: fixed address (next instruction + disp).
2. **Replay the loads**: when a base register was overwritten by an earlier load from an
   address whose registers survive, read those pointers from memory now and work it out
   (`[[rbx+30]+8]+10`). Block copies (`rep movs`): source = watched + (rsi - rdi).
3. **One caller up**: when the value arrives in an argument register at the start of the
   code block, search the first 512 bytes of the stack at the trap for return addresses
   into the game's code that follow a call, read back from each call site for that
   register, and apply 1 and 2 there (registers that a call keeps: rbx, rbp, r12-r15).
4. **Execute breakpoint at the load** (setting `trace_exec_breakpoint`, default off until
   C20d passes): 1-byte execute breakpoint on the load, at most 16 hits or 10 s; registers
   are before the load, so the address is exact. Only hits whose address passes AC-T5 are
   kept. 16 hits at the same place with identical registers = "the breakpoint repeats":
   stop and log it.
5. Otherwise the hop has no address: AC-T8 "Search for it that way".

### AC-T5 Memory check (before anything is written)
- Given a candidate source and the recorded way, Then the source's value through the way
  must equal the copy's value now: exact for whole numbers; for a cut conversion
  floor(x) == shown; for a rounded one |x - shown| <= 0.5; for decimals within 1% or 1
  unit of the shown number's last digit, whichever is larger.
- It is checked once the copy has held one value for 1 s, and must hold at **two
  different shown values** (the player changes the number once; the hint says so). Wait up
  to 30 s for the change; no change -> "Change the number in the game so the app can
  check." and the hop ends unconfirmed.
- A candidate that fails is dropped (Advanced: "held 12, expected 87").

### AC-T6 Write test and the next hop
- A source that passes AC-T5 gets the write test from Check which is real (`RealChecker`),
  as one group: [source, the original copy, every earlier hop].
  - **real** (the copies followed): trace ends with this source (AC-T7).
  - **kept the test value**, nothing followed: trace ends with this source, marked
    "checked: kept the test value" (the screen may only update when the number changes).
  - **copy** (the game put it back): it becomes the next copy to watch (AC-T2), with the
    way so far carried on.
  - **not checked**: trace ends, AC-T8.
- At most 4 hops. An address seen twice ends the trace (AC-T8 "goes round in a circle").
- The same put-back rules as Check which is real (AC-R4) apply to every write.

### AC-T7 Result
- The found address is added to the Find list as a typed-in row (like Test the hits'
  winner), with its number type and way, selected.
- Columns: **Value now** shows the stored value and the screen value: `0.87 (screen: 87)`;
  **Stored as** shows the way ("x0.01 of the screen", "share of max"); **Notes** shows
  "found behind <copy address>" and the check mark (AC-T6).
- Hint: "Found the value the game uses: 0.87, a decimal; the screen shows it x100." (or
  "...: 51, an 8-byte whole number; the screen shows it as is.")
- Set and the Make a cheat value: on a traced row the typed number is **what the screen
  shows** and is converted with the way before it is written (87 -> 0.87). Freeze holds the
  current bytes as today. Make it work and Lock it use the row's address and type.
  [OPEN: Kein, should percent/fraction rows type in screen units too? Default: no, only
  traced rows]
- If the start row was already real: "This is already the value the game uses." No row.
- Works after restart: as today (`session.classify`): a fixed address -> Yes; otherwise
  This session only (Lock it / Make it work / Find it next time apply as usual).

### AC-T8 When it can't finish
Plain messages, one per reason; nothing stays written, the debugger is detached:
- No writer: AC-T2 message.
- No address but the way is known (type and factor): "The game keeps it as <type>, the
  screen shows it <factor>. Search for it that way?" with **Search for it that way**: Start
  over, fill Advanced (type, scale with the traced factor, shown as "traced x0.01"), and
  type the current screen number; the player presses Search. The guide highlights it.
- Way not followed: "The game works this number out in a way the app can't follow yet.
  Try Check which is real or I don't know the number."
- Scrambled: "The game scrambles this value, which isn't supported yet." (Advanced shows
  where.) [OPEN: Kein, OK to only report scrambled values in this version? Default: yes]
- Circle / 4 hops reached: "Followed it 4 steps without reaching the real value." The
  last checked source is offered as a row marked "not sure".

### AC-T9 Progress, Cancel, safety
- One dialog: steps "Watching (1 of up to 4)", "Reading the game's code", "Checking", with
  a progress bar and Cancel. Cancel ends within 2 s: watch cleared, debugger detached,
  test values put back, paused freezes resumed.
- Only one debugger job at a time: the button is disabled while Lock it, Make it work or
  Find it next time is collecting, and vice versa.
- A lost connection: same put-back as Check which is real; hint "Lost connection to the
  PS5." If something couldn't be put back, the address is named in Advanced.
- Whole trace: no more than 4 minutes including the player's changes; each hop without
  waiting for the player no more than 15 s.

### AC-T10 Advanced: show details
- Per hop: watched address, writer address and text, hits, the instruction path followed,
  registers used, how the address was worked out (registers / replay / caller / execute
  breakpoint), memory-check values, write-test verdict, why it stopped.

### AC-T11 Logs
- `action=trace_start`, `trace_watch`, `trace_writer`, `trace_path`, `trace_source`
  (method=regs|replay|caller|exec), `trace_check` (memory check), `trace_test`
  (write test), `trace_hop`, `trace_result`, `trace_restore`. The raw 0x4A0 packet of the
  first hit of each hop is logged in hex at debug level (for future XMM checks).

### AC-T12 Tests without a console (FakeConsole)
Unit tests in core (no Qt) for each pattern, each must give the right address and way:
`movss xmm0,[rbx+10]; movss [rdi+40],xmm0`; `mov eax,[rcx+8]; mov [rdx],eax`;
`cvttss2si eax,xmm0` after a load (cut); `mulss` by a RIP-relative 100.0; `divss` by a loaded
max then `mulss` 100; `mov rax,[rbx+30]; mov rax,[rax+8]; mov ecx,[rax+10]; ...` with rax
reused (replay); RIP-relative global; `rep movsb`; vector store lane offset; setter with
value in xmm1 and caller found on the stack; read-modify-write (already real); xor (scrambled);
a failed memory check (wrong path) gives no row; 2 hops (HUD -> widget -> real); circle.

## Defaults we proceed on
- Button name "Find the real one behind it"; guide suggests it after all-copy checks or a
  Set/Freeze put back.
- 30 s watch per hop, early end 2 s after 3 hits; up to 3 writers per hop; at most 4 hops;
  cycle detection.
- 256-byte decode window; one caller level via a 512-byte stack search.
- Memory check at two different shown values, after 1 s steady.
- Write test reuses RealChecker (1 s settle) with the earlier copies as followers; "kept"
  counts as found.
- Execute breakpoint fallback behind `trace_exec_breakpoint`, off until C20d passes.
- Traced rows: typed numbers are in screen units; Freeze holds raw bytes.
- Scrambled values are reported, not supported.
- Misaligned values: watch the aligned low part.
- Behind Advanced "(being tested)" until C20a passes.

## Out of scope
- Scrambled/encrypted values (xor, rotate, byte swap, keys), values split over two
  places, numbers shown only as text, values summed from several changing sources (base +
  buffs), add/sub of a constant (offset), more than one caller level, software
  breakpoints, reading XMM registers from the packet (logged only), changing Lock it /
  Make it work / Check which is real behaviour, changing percent/fraction rows to screen
  units, the 8-byte alignment of Lock it's own watch (separate fix if C20h shows a need).

## Console tests to add (docs/console-tests.md, C20)
| Id | What | Steps | Pass | Send |
|---|---|---|---|---|
| C20a | Registers are right | Any game; find a value's screen copy; Advanced on; Find the real one behind it; change the number. | Advanced shows the writer and a source worked out "from the registers" that passes the memory check. If it never does, C10a registers are wrong. | `action=trace_*` lines incl. the raw packet. |
| C20b | Returnal health from a screen copy | Search health as shown (whole number, 4 bytes) until a few copies; Check which is real marks copies; Find the real one behind it; take hits when asked. | Ends with a Decimal row marked checked: real/kept, Stored as shows the factor; Freeze on it holds health in game. No crash. | Log, the result row, hops count, total time. |
| C20c | Returnal Obolites | Search Obolites as 4-byte whole number (the 4 copies of C3); Find the real one behind it; pick some up. | Ends at an 8-byte whole number (compare with C3's 0x...f34 style address); Set 1000 holds after a pickup. | Log, addresses. |
| C20d | Execute breakpoint (setting on) | Set `trace_exec_breakpoint: true`; repeat C20b. | The log shows method=exec with hits that differ; the game keeps running; no "repeats" line. If it repeats or hangs, say so (we keep it off). | `trace_source` lines. |
| C20e | Already real | Simple game; find money's real value (Set holds); Find the real one behind it. | "This is already the value the game uses." No row added. | Log. |
| C20f | Cancel and recovery | Start a trace, Cancel during the watch; then run Lock it on the same row. | Cancel within 2 s; game runs; Lock it attaches normally (debugger was detached); no value left at a test value. | Log. |
| C20g | PS4 game | A CUSA game, a value with a screen copy; trace it. | Same as C20b, or a clear AC-T8 message. | Log. |
| C20h | Misaligned 8-byte watch | On C20c's real INT64 (4-aligned), run a trace or Lock it watch. | Writes are caught (hits > 0) when the count changes. | `trace_watch` lines. |
| C20i | Timing | During C20b/C20c note times. | Each hop no more than 15 s plus the player's change; whole trace no more than 4 min. | Times. |
