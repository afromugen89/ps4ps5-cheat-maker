# Cheat Maker: smart cheat builder ("Make it work")

Date: 2026-11-11. Asked by Kein (idea 2 in the thread "Show grouped results when scan says
found it"), after Check which is real (docs/spec-real-check.md). Kein already approved the
app writing to the game's memory on its own as long as it puts things back.

Generic for every game: no game-specific logic, names, offsets or tables. Simple games stay
simple: if the first fix works, the player sees one test and a "Done". No script editor, no
custom code from the player.

## Why

Today a "This session only" value becomes a PC-off cheat through Lock it, and possibly
Player only. The player picks options, reads warnings, tests by hand, and has to undo a bad
option themselves. Kein's Returnal tests (2026-11-08) crashed the game and the PS5 twice:
once by NOPing a 2-byte store through a bare pointer, and once with a Player only on
`vmovss [r14],xmm0`. Returnal's health also needed two decimals held (health and its 0-1
fraction). The builder does all of this by itself, and only with fixes that pass the safety
rules below.

## Words used here

- **Target:** an address the builder makes a fix for. The first is the row the player picked.
- **Writer:** an instruction caught writing a target (`locker.find_writers`).
- **Method:** one way to fix one writer (table in B3).
- **Candidate:** one method applied to one writer, or to every eligible writer at once (B4).
- **Hook:** a 5-byte `jmp` over the writer into a routine in the spare zero bytes at the end
  of the game's code ("the cave"), as Player only does today.
- **Counter:** 16 spare bytes in the game's data (`cave.find_slot`) that the test routine
  increments each time it runs. It proves the game tried to change the value.

## Effects

| Effect (as shown) | Exact meaning | Applies to |
|---|---|---|
| **Never goes down** | A write that would make the value lower stores the old value instead. Writes that raise or keep it run as normal. | Every supported type |
| **Always full** | Every write by a patched writer stores the Full number. Full = the value when the player pressed Start, editable in the dialog and later in My cheats. | Every supported type |
| **Half damage** | A write that lowers the value lowers it by half as much. Whole numbers round so that a drop of 1 or more stays at least 1 (7 becomes 4, 1 stays 1). Raising writes run as normal. | Every supported type |
| **Only me** (tick box, with any effect) | The effect applies only to the object the target belongs to; every other object goes through the original code unchanged. | Writers of the form `[reg+disp]`, disp other than 0 |

Supported value types in this version: Decimal (4 bytes), Whole number (4 bytes), Whole
number (8 bytes). Any other type: "This kind of value isn't supported yet. Use Lock it." and
no console work is done.

## Acceptance criteria

### B1 Where it starts
- Given a Find row or a My cheats row whose value has one address and a supported type, When
  the player presses **Make it work...**, Then a dialog opens with the effects as choices
  (Never goes down selected) plus the **Only me** tick box, and a **Start** button.
- Given the row is in a group that has not been checked yet, When the player presses Start,
  Then Check which is real runs first (one group only) and the builder uses the member it
  marks real. If nothing is marked real, it uses the row and the hint says "Not sure this is
  the real one."
- Given a combined cheat, or a cheat that is found again each run, Then the button is
  disabled, with the tooltip "Pick a single value."
- Lock it and Player only are unchanged.
- Before the first build in a session, a one-line notice: "Save your game first. The game may
  restart if a fix doesn't work." [OPEN: Kein, a notice with "don't show again"? Default: yes]

### B2 Watching for the code (step 1, about 30 s)
- When Start is pressed, any freeze the app holds on the target is released (as Lock it
  does). The hint says "Play and make it change (take a hit) within 30 seconds."
- A write watchpoint runs for 30 s, or ends early once at least 3 writes from at least 1
  instruction were caught and 5 s more have passed.
- For "Always full", Full is read just before the watch starts. The hint first says "Fill it
  up, then press Start."
- No writer caught: "Nothing changed it. Try again and make it change while the bar moves."
  Nothing is patched.

### B3 Safety rules and methods (decided before anything is written)
Every writer is classified first. A writer that breaks a rule is listed under "Skipped" with
its reason and is never patched.
- **S1** `Writer.shared` (writes through a bare pointer, `[reg]` with no offset): skipped by
  every method.
- **S2** `nop_unsafe` (atomic, `lock`, rep, jumps or calls, writes a register): skipped by
  every method.
- **S3 (flags rule, new)** If an instruction after the writer reads a flag before anything
  rewrites it: the plain NOP method is skipped. A hook routine must leave ZF, SF and OF as the
  original would for the value it actually stored. If a following instruction reads CF, PF
  or AF, the writer is skipped.
- **S4** The store size is not the target's size (for example a vector store covering several
  fields), or the writer writes more than one memory place: skipped.
- **S5** A hook needs `cave.hook_span(single=True)` to succeed. The routine saves and
  restores the flags around its own checks (`pushfq`/`popfq` past the red zone, as
  `capture_code` does). The game's writer runs first and the effect's store goes over it, so
  flags come from the game's own instruction.
- **S6** Every patched byte (hook, routine, counter) is inside the game's module. The routine
  and counter areas must be zero and not reserved by saved notes. The hook site must still
  hold the original bytes (`verify_original`).
- **S7** A method that once crashed the game (B6) is skipped for this title ID, version and
  writer.

Methods, tried in this order for each effect:

| Effect | 1st | 2nd |
|---|---|---|
| Never goes down | Hook: skip a lowering write (keeps the old value), with counter | Plain NOP (only when the writer is under 5 bytes or a hook won't fit, and S2/S3 allow it) |
| Always full | Hook: store Full, with counter | Plain NOP, only if the value now equals Full |
| Half damage | Hook: halve a lowering write, with counter | (none) |
| + Only me | Same as above, but the routine first compares the object: its class (vtable) if the player's differs from every collected object, else the best field (`cave.guess_fields`). Plain NOP is never used. | |

### B4 Order of candidates
- Writers are tried most hits first. Each eligible writer gets its methods in table order,
  one candidate at a time.
- After the single-writer candidates, if at least 2 writers are eligible: one candidate with
  the 1st method on all of them together.
- At most 6 candidates per build. [OPEN: Kein, 6 fixes of 20 s (about 3 min worst case)?
  Default: yes]
- Only me: before the first candidate, the Collect step from Player only runs (20 s, "get hit
  and hit enemies"). If no other object is seen, the hint says so and Collect runs once more.
  If it fails again, the build stops with "Couldn't tell you apart from enemies."

### B5 The live test (each candidate, 20 s)
- The routine bytes go in first, then the jump (as `anchor_hook.install`). `patch_landed`
  must read back true, otherwise the candidate fails as "the PS5 didn't take the change".
- The hint says "Testing fix N of M: keep playing and take a hit."
- While it runs, the app reads every target at least every 250 ms and checks Alive every 1 s.
  [OPEN for architect A2: 100 ms if the PS5 allows it]
- **Alive** = the PS5 answers a read of the game's memory within 2 s, and the game's process
  is still listed and still in front. [OPEN for architect A1: an access-watchpoint heartbeat
  on the target as a third check, confirmed on console C18a]
- **Pass** (all must hold):
  - the counter is at least 1, or for a plain NOP the player answers **Yes, I got hit** to
    "Did you take a hit?";
  - Alive the whole time;
  - the effect check: **Never goes down**: no reading lower than the value at test start
    (decimals ±0.01, whole numbers exact). **Always full**: every reading after the first
    counter tick equals Full (decimals ±0.5%, whole numbers exact). **Half damage**: at least
    one drop is seen, and every drop is about half the game's (decimals ±2%, whole numbers
    rounded as in Effects).
  - **Only me** also: at least one collected other object's value at the same offset went
    down during the test. If none did, the app asks "Did enemies still take damage? Yes / No
    / Not sure"; only Yes passes. [OPEN: Kein, may the app ask one Yes/No per fix when it
    can't tell? Default: yes]
- **Fail** = any pass rule broken, the 20 s run out with no counter tick (hint: "You didn't
  get hit; trying again"; the same candidate is repeated once), or Cancel.

### B6 Putting things back
- On fail, Cancel, closing the dialog, Disconnect or closing the app: the original bytes go
  back at the hook site first. After 2 s, the routine and counter bytes are zeroed.
- On an Alive failure the same put-back runs at once. The hint says "The game stopped
  responding with fix N. Put back. Trying the next one."
- Before each candidate's bytes go in, the app writes a note on the PC: title ID, version,
  writer offset, method, time. It is cleared when the candidate ends. If the connection is
  lost with a note open, that method is marked "crashed the game" and is skipped from then on
  (S7). On the next connect the hint says "Fix N crashed the game last time. Your game is
  unchanged after a restart."
- The app never claims to have rolled back when the PS5 didn't answer. In that case the hint
  says "Restart the game: fixes only live in memory until then."

### B7 A second address (Returnal-style values)
- After a candidate passes, if the target's group has a "likely real" neighbour (within 64
  bytes, at another scale) that went down during the test while the target held: the builder
  runs B2 to B5 for that neighbour with the same effect and Full scaled by the group factor,
  then combines both passes into one cheat.
- At most 2 targets per build. A third: "This value needs more than two fixes; use Lock it."

### B8 The result
- On pass, the counter is taken out and the passed fix goes in as a cheat replacing the
  source row in My cheats (as Lock it does). Name: "<source name>: <effect>" (+ " (only
  me)"). It is on.
- Lines saved: hook: the routine (CODE_PATCH) plus the jump (CODE_PATCH, on = jump, off =
  original). Plain NOP: one CODE_PATCH. Always full keeps Full editable (`value_positions`).
- Works after restart: **Yes** for NOP, a hook without Only me, or Only me by class. **Check
  after restart** for Only me by field.
- No candidate passed: "None of the safe fixes worked for this value. Your game is
  unchanged." Nothing is added. The skipped writers and reasons are shown under Advanced.

### B9 Logs
- `action=build_start`, `action=build_writer`, `action=build_try`, `action=build_result`,
  `action=build_rollback`, `action=build_crash_mark`.

## Measurable targets
- A simple game (value with one non-shared writer): from Start to Done in no more than 60 s
  with one test.
- Returnal health, Never goes down + Only me: passes in no more than 6 candidates, enemies
  still die, no crash, health and fraction held. Console test C18b.
- FakeConsole: every routine's behaviour is unit-tested for lowering, raising and equal
  writes, for each supported type, and for flags left after it. Rollback is tested to leave
  the module bytes as they were for every fail path.

## Not in this version
- Effects beyond these (never goes up, multiply, set on event), values over 2 addresses,
  other types, writers skipped by S1 to S4, editing the half-damage factor [OPEN: Kein,
  25/50/75%? Default: 50% only], changing Lock it / Player only.
- Two existing flags bugs found while writing this (Lock it's NOP check ignores flags a
  following branch reads; Player only's `cmp` changes flags without saving them) are fixed
  in their own small change before or with this one.

## Console tests to add
- C18a: Alive checks on Returnal and one simple game.
- C18b: Returnal health, Never goes down + Only me.
- C18c: a simple game, money, Never goes down, one test.
- C18d: Always full on a whole-number value.
- C18e: Half damage on a decimal value, with the logged drops.

[OPEN: Kein, the button is called "Make it work..." on a Find row and in My cheats, and Lock
it stays? Default: yes]
