# Cheat Maker: linked values in Make it work

Date: 2026-11-12. Asked by Kein ("Proceed with 5"). Builds on Make it work
(docs/spec-cheat-builder.md, B7 "A second address", cut there), Check which is real
(docs/spec-real-check.md), grouping (docs/spec-health-finding.md AC-G1 to AC-G4) and
combined cheats (docs/spec-restart-combined.md). Replaces B7.

Generic for every game: no game-specific logic, names, offsets or tables. Returnal is only
the example. Simple games stay exactly as simple: if the picked value has no linked place
that falls behind, the player sees one test and Done, with no extra step, wait or question.

## Why

Returnal (PPSA01285 03.000.005, health max 300, console tests 2026-11-08): infinite health
needed two places held: `health / 100` (3.0, at 0x1381150AAC) and the health fraction
(1.0, at 0x14A146B834). They are in different objects, far apart; in another scan the
fraction sat 16 bytes before health. The percent (0-100, 0x20D67E0C08) was not needed.
0x12E0EA5C10 / 5C00 are copies the game recalculates: holding them showed a full bar while
the player died. Make it work today fixes one place, so the player still has to find and
lock the second one by hand.

## Words used here

- **Place:** one address the builder fixes. Place 1 is the one Make it work picks today (B1).
  A **linked place** is any other.
- **Pool:** the other members of place 1's Find group: linked by the same ratio over at
  least 3 scans (AC-G2), at any distance.
- **Factor (k):** a pool member's value divided by place 1's value, read at the same moment.
- **Falls behind:** during a test, a member is lower than k x place 1's value by more than
  2% (decimals) or more than 1 (whole numbers), at the end of the test or for at least 1 s
  in a row.
- **Cluster:** members that fell behind and kept the same ratio to each other during the
  test (within 2%): one value the game keeps plus its copies.

## Acceptance criteria

### L1 When it runs
- Given a build whose place 1 passed (B5), When the pool is empty or no pool member fell
  behind in that passing test, Then the build ends as today (B8): no extra step, no extra
  question, no new hint, and the log says `linked=none`.
- Given a member fell behind, Then the hint says "<name> held, but the game also keeps it in
  a second place that still went down. Fixing that too." and the linked step runs (L5 to L7).
- Lock it, Player only, and builds with no group behave exactly as today.

### L2 The pool
- Given Make it work opened from a Find row that is in a group, Then the builder gets every
  group member with its factor and its Check which is real mark, whether or not the group
  was checked before (today only unchecked groups are passed).
- Given a session cheat made from a grouped Find row, Then the cheat remembers that group
  (in memory only, never saved) and Make it work from My cheats uses it. Other My cheats
  rows have no pool.
- At most 16 members, in group order (best first), not counting place 1 and members
  marked copy.
- When Start is pressed: the app's own freezes on place 1 and on every pool member are
  released, each member is read once, and a member whose value isn't k x place 1 (within 2%)
  is dropped and logged (the game may have restarted since the search).
- A member that shares any byte with place 1 or with a member already taken is left out.

### L3 Watching the pool during a test
- During every live test (B5) of the build, every pool member is read at least once a
  second, in reads batched per 4 KiB. The target's own samples per 20 s test drop by no
  more than 10% compared with a build without a pool. When the console is too slow for
  both, the value's own readings come first; late pool reads are logged.
- Given a passing test, Then every member is judged: **kept up** or **fell behind**
  (Words). A member that wasn't readable is **unknown** and is not fixed.
- The judging is the same for every effect: Never goes down, Always full and Half damage
  (with Half damage, k x place 1 falls by half too, so a value that falls by the game's
  full amount is behind).

### L4 Copies are never fixed
- A fell-behind member marked **copy** (by Check which is real in Find or in B1) is never
  fixed or frozen.
- Fell-behind members with no mark yet are checked by Check which is real (spec-real-check
  AC-R2 to AC-R4) as one group, with place 1's fix still in. Its put back runs as there.
- A member marked copy, or that couldn't be checked, is shown under Advanced with its
  reason and is not fixed.

### L5 Which place next
- Fell-behind members are sorted into clusters. One member per cluster is tried, in this
  order: marked real, then kept the test value; within that, "likely real" first, then the
  most members in its cluster, then the lowest address.
- After a linked place passes, its passing test is judged again as in L3 with every fix in.
  A cluster whose members now keep up needs nothing more. A member that still falls behind
  starts the next linked place (within L10).
- [OPEN: Kein, after the last place passes, take each linked fix out for one more 20 s test
  and drop it if everything still holds ("trim")? Default: no.]

### L6 Building a linked place
- Each linked place goes through B2 (watch, 30 s), B3 (safety rules), B4 (order) and B5
  (live test) as place 1 does, with the same effect, while every earlier passed fix stays in.
- A writer that is the same instruction as an already fixed writer is skipped: "it's the
  same code as an earlier fix".
- Each place gets its own counter and its own code rooms; none of them overlaps another
  place's or an open cheat's.
- Before a linked place's watch, place 1's passed fix is switched to its kept routine
  (counter out, as B8), so the only counter running is the linked place's.
- The hints name the place: "Place 2 of 2: play and make it change (take a hit) within 30
  seconds." and "Testing fix N of M for place 2: keep playing and take a hit."

### L7 Only me on a linked place
- Given Only me, When the linked writer writes through the same object start as place 1's
  writer, Then place 1's filter is used as is.
- Otherwise the Collect step (B4, 20 s, "get hit and hit enemies") runs once for that place
  and learns its own filter. If it can't tell the player apart: L11.
- B5's enemies check applies to each place on its own.

### L8 Full for a linked place
- Always full: each linked place's Full = place 1's Full x (that place's value / place 1's
  value), both read at the same moment just before the first watch. If the player typed a
  Full, the same ratio is applied to it.
- B5's Always full check uses that place's own Full (decimals within 0.5%, whole numbers
  exact after rounding to the type).
- [OPEN: Kein, should Full stay editable in My cheats on a cheat with 2 or more places (all
  places rescaled)? Default: not in this version; the Full box is read-only and its tooltip
  says "Make it work again to change Full."]

### L9 What "works" means for the whole cheat
- The cheat is done when the last fixed place's test passed (B5) and, in that same test
  (every fix in), no member left to fix still fell behind, and Alive held the whole time.
- No extra final test.

### L10 Limits
- At most 3 places per build (place 1 + 2 linked). [OPEN: Kein, 3? Default: 3]
- At most 4 fixes tried per linked place, and at most 12 tests per build (repeats after
  "You didn't get hit" count).
- Given a member still falls behind once a limit is hit, Then L11 applies to it with the
  reason "This value is kept in more places than the app can fix on its own."

### L11 When a linked place can't be fixed
- Applies when no fix passed for a linked place, a limit was hit, its type isn't supported,
  or Only me can't tell the player apart there.
- Never goes down / Always full: the passed fixes are kept and the linked place is held by
  a freeze (Never goes down: its value when its test started; Always full: its Full). The
  result is one combined cheat (L13). Hint: "The second place couldn't be fixed with a safe
  code change, so the app holds it while the PC is connected. Press Find it next time to
  keep it after a restart."
- Half damage: the passed fixes are kept, nothing more is added. Hint: "<name> takes half
  damage, but the game also keeps it in a second place that still goes down fully. The
  cheat may not be enough on its own."
- [OPEN: Kein, keep fix 1 plus a freeze (default), keep fix 1 only, or put everything back?]

### L12 Cancel, close, crash
- Cancel, closing the dialog, Disconnect or closing the app at any step puts back every fix
  of this build (B6 order per place, the newest place first). Hint: "Your game is
  unchanged."
- Before each test's bytes go in, the crash note names only the fix under test. Places
  already passed but not saved yet are noted separately so they are put back on the next
  connect. If the connection is lost, only the fix under test is marked "crashed the game"
  (S7).
- If a put back isn't confirmed: the B6 wording ("Restart the game: fixes only live in
  memory until then."); nothing more is tried.

### L13 The result
- Every place fixed in code: one Make it work cheat, not marked combined, replacing the
  source row as today, named "<name>: <effect>" (+ " (only me)"), on. It has a routine line
  and a jump line per fixed writer (or a switch-off line), places in order. It can be saved
  to GoldHEN and CheatRunner files. Works after restart: **Yes**, or **Check after
  restart** if any place's Only me used a field.
- With a freeze (L11): one combined cheat with parts "<name>" (the code fix, fixed) and
  "<name> (place 2)" (the freeze, a moving part with its number type), named
  "<name>: <effect>". Works after restart: **This session only**. Not written to cheat
  files (spec-restart-combined AC-6).
- Done hint, all fixed: "Done. <name> is held in N places by one cheat."

### L14 Restart and Find it next time
- A cheat with all places fixed in code needs nothing after a restart (as B8).
- A cheat with a freeze part: Find it next time is on and learns the freeze part exactly as
  for any combined cheat (spec-restart-combined AC-2 to AC-4).

### L15 What the player sees
- Before Start nothing changes. The progress hint counts places only once the linked step
  starts.
- Advanced lists every pool member: address, type, factor, its mark (real / kept / copy),
  "kept up" or "fell behind", and what was done ("fixed", "held by a freeze", "not needed:
  it kept up after place N was fixed", "copy: never touched").

### L16 Logs
- `action=build_pool members=N dropped=N released=N`
- `action=build_linked_check n=<place> member=<addr> k=<factor> result=kept_up|behind|unknown worst=<value>`
- `action=build_linked_pick place=N member=<addr> mark=<real|kept> cluster=<size> reason=...`
- `action=build_linked_result place=N result=pass|fail|fallback_freeze|limit`
- `action=build_result ... places=N linked=none|fixed|freeze|partial`

### L17 Unchanged
- Lock it, Player only, Check which is real, Combine, Find it next time and existing tests
  are unchanged. With no pool, Make it work's code path, timing and log lines are as today,
  plus `linked=none`.

## Measurable targets
- Simple game (C22d): Start to Done in 60 s or less, one test, `linked=none`.
- Returnal health, Never goes down (C22a): one cheat, two places, within 5 min from Start;
  10 hits with no health loss and no death; the percent (0-100) value and the copies are
  not fixed.
- Worst case per build: 12 tests (240 s) + 3 watches (90 s) + Collect (40 s at most) +
  checks, under 7 min.
- FakeConsole unit tests: the fell-behind rule for all three effects with Returnal-shaped
  samples (fraction behind, percent following the fraction, copies put back); clusters and
  their order; put back of 2 and 3 places on every fail, cancel and lost path leaves the
  module bytes as they were; crash note blames only the fix under test.

## Not in this version
- Searching memory for linked places when there is no Find group.
- Make it work on a combined cheat (its parts as the places). [OPEN: Kein, next version?
  This conflicts with spec-restart-combined AC-6.]
- Editing Full later on a cheat with more than one place (L8 OPEN).
- More than 3 places; the trim test (L5 OPEN).
- Saving the pool in the game's notes.

## Console tests to add (docs/console-tests.md)
- C22a: Returnal health, Never goes down, no Only me. Pass: two places fixed, the percent
  value and the copies not touched; 10 hits, no death; restart, load the file in
  CheatRunner, health still holds. Send: `action=build_` lines, the Advanced list.
- C22b: Returnal health, Never goes down + Only me. Pass: as C22a, and enemies still die.
  Say whether Collect ran twice.
- C22c: Returnal, Always full. Pass: health jumps back to full after each hit; the
  fraction's Full is 1.0 (within 0.5%).
- C22d: A simple game (money, one place). Pass: one test, Done within 60 s, `linked=none`.
- C22e: A value with a HUD copy only (any game). Pass: no linked step; the copy is marked
  copy or kept up.
- C22f: Cancel during place 2's test. Pass: within 2 s both fixes are put back; health goes
  down again on the next hit.
- C22g: Returnal, Half damage. Pass: both places lose about half per hit.
- C22h: A PS4 game with a decimal health and a fraction, any of the above.
