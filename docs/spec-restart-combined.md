# Cheat Maker: Combined cheats that survive restarts

Date: 2026-11-11. Kein picked "Cheats that survive restarts" (idea 2 of the list in the
project chat). Generic for every game: no game-specific logic. Returnal is only the
example. It builds on **Combine** (user guide 6) and **Find it next time** (user guide
6.4); a combined cheat of values that don't move works exactly as today.

## Why

Returnal (PPSA01285 03.000.005) infinite health needs **two** heap floats kept together:
`health / 100` (3.0 at max 300) and the health **fraction** (1.0). Both move on every
restart. Today:

- **Combine** joins them into one cheat, but refuses cheats that are found again
  ("cheats found again after a restart can't be combined").
- **Find it next time** learns one value. On a combined cheat the button is off: the
  combined cheat has no number type and more than one address.

So health works for one session only, or as two found-again cheats ticked one by one.
The goal: one cheat, one tick, every run, with the PC connected.

## Words

- **Part**: one place a combined cheat writes. It keeps the name of the cheat it came
  from ("health", "fraction"); if that cheat wrote several places: "health (1)",
  "health (2)".
- **Fixed part**: in the game's module or a code change (Works after restart? **Yes**).
- **Moving part**: a value found by a search, not learned yet (**This session only**).
- **Learned part**: found again each run (an object and field in the game's notes).

## Acceptance criteria

### AC-1 What Combine keeps
- Combine keeps, for every part, its number type and whether it is fixed, moving or
  learned. The rest is as today: one tick turns all parts on, the value can't be
  edited, it freezes if any source cheat froze.
- Combine accepts any mix of fixed, moving and learned cheats. The **Combine** button is
  on whenever two or more cheats are picked.
- At most 4 parts that move (moving + learned) in one cheat. More: "Combine up to 4
  values that move; these have 5." Fixed parts have no limit.
- Two parts that share any byte are refused: "health and fraction overlap in memory."
- **Works after restart?** says **This session only** while any part is moving;
  **Found again each run** when no part is moving and at least one is learned; **Yes**
  when every part is fixed.
- Given found-again "health" and session-only "fraction", when the player combines
  them, then one row "health + fraction" shows **This session only** and **Find it next
  time** is on for it.
- Combining found-again cheats removes their own entries from the notes (their objects
  stay); the new combined cheat replaces them, so next run they are not listed alone.

### AC-2 Find it next time on a combined cheat
- The button is on when connected, the cheat has at least one moving part, every moving
  part has a number type, and every learned part is found this run.
- One **Start** learns every moving part, one after another, in the same dialog. Fixed
  and learned parts are skipped. Only one watchpoint is used at a time.
- The hint names the part: "Part 1 of 2: health. Play normally and take a hit if you
  can." Before Start it says how long: about 70 s per part (more with *Also look for a
  memory path*, which applies to every part). The progress bar covers all parts.
- Each part is learned exactly as a single cheat is today (same steps, waits, checks
  and log lines). If the cheat is on, it is turned off first, as today.
- Two parts never get the same spare room for their hook (routine or slot), even before
  the notes are saved.
- Two parts found in the same object (same object start and same class) share one
  object in the notes: two fields, one hook.
- When all parts are learned: the notes are saved once, the row says **Found again
  each run**, and the hint lists each part: "Ready: health will be found by the game's
  code; fraction will be found by a memory path."
- If any part can't be learned, or the player presses **Cancel**: every hook put in
  during this dialog is taken out, the notes and the cheat are as before (still This
  session only), and the hint names the part and the reason: "Couldn't learn fraction
  (part 2 of 2): nothing in the game touched the value while we watched. Nothing was
  changed." Cancel stops within 2 s.
- **Also make a PC-off cheat** is not shown for a combined cheat.

### AC-3 Next run: finding and the first tick
- On connect (and on **Find it again**) every learned part's object is found again as
  today: same order of methods, same checks, same pick-one dialog if methods disagree.
- The combined cheat comes back as one row, unticked; its parts never come back as rows
  of their own.
- Given two hooked parts, when the player connects, then both are found within 20 s.
- The status line has one line per combined cheat: "Ready: Health found again (2 parts,
  4.2 s). Tick it to use it." If a part isn't found: "Couldn't find fraction (part of
  Health) this time. Delete Health and make it again, or search for fraction." The
  guided search opens with that part's type and last value, as today.
- The cheat can be ticked only when every learned part is found and checks out.
  Otherwise nothing is written: "Health: fraction isn't found right now, so nothing was
  changed. Press Find it again."
- Before its first write each run, the app asks **one** question for the whole cheat
  (skipped if every part's object was already confirmed this run):
  "Is this your Health? health shows 3.00, fraction shows 1.00 right now. Take a hit
  and watch these numbers change. Press Yes only if they do."
  **Yes** confirms every part's object; **No** writes nothing (today's "OK, nothing was
  changed" message). The question doesn't claim the numbers match the screen: a part
  may be stored as /100 or as a fraction.
- Ticking writes every part. A learned part is only ever written at its checked place.

### AC-4 While it is on
- Every freeze tick (10 a second) each learned part is checked on its own. A part that
  stops checking out is not written; the other parts keep being written (no less safe
  than two separate cheats today: each write still goes only to a checked place). A
  part found again is written again within 5 s (as AC-12.5). *Default chosen by Claude;
  Kein can ask for "pause the whole cheat" instead.*
- The log names the part: `action=freeze key=... part=fraction paused` / `resumed`.
- A part whose object moved and has no class check gets today's "moved" question,
  naming it: "fraction (part of Health) moved ...". **No** turns the whole cheat off,
  as today for a single cheat.
- Unticking: fixed parts get their original bytes back; learned parts are left as they
  are (as today for found-again cheats).
- A part's hook stays in while any cheat that uses it is on, and comes out with
  today's rules (last cheat off, disconnect, close).

### AC-5 The game's notes (.cmproj)
- Format version 3. A combined cheat with no moving part and at least one learned part
  is saved as one entry: name, freeze or set, and its parts in order. A learned part
  holds object, field and value; a fixed part holds its bytes (module, offset, on, off).
- Given notes in format 1 or 2, when they load, then every object and cheat is the
  same as with the older app; they are saved back as format 3.
- Given notes in format 3, when an older app opens them, then it says "made by a newer
  Cheat Maker" and keeps the file as `.cmproj.bak` (today's rule): nothing is lost
  silently.
- Deleting a combined cheat removes its entry; the objects stay (as today).

### AC-6 Unchanged behaviour
- Single found-again cheats, combined cheats of fixed parts, Lock it, Make it work, the
  Find tab and cheat files work as before; existing tests still pass.
- A combined cheat with any moving or learned part is never written to a GoldHEN or
  CheatRunner file (none of its parts), as today for found-again cheats.
- Lock it and Make it work stay off for combined cheats.

### AC-7 User guide
- Section 6 (Combine), 6.4 and 7.2 say: combine first, then Find it next time once on
  the combined cheat; learning takes about a minute per part; how to redo a part that
  is not found.

### AC-8 Console test C19 (docs/console-tests.md)
- Returnal, health as two parts. Make health and fraction cheats, Combine, Find it next
  time, Start, play and take hits until Ready. Then 3 restarts, 1 biome change, 1 death.
- Pass: each run, one row comes back; both parts found within 20 s; one question;
  health stays full through hits; freeze pauses and resumes across the biome change
  and the death; no crash, no wrong write.
- Send: the `action=learn` lines (they show whether both parts are in one object:
  user guide 7.2 says they sit side by side), `action=refind`, `action=freeze` and
  `action=tracker` lines.

## Not in this spec
- Fixing one lost part without redoing the cheat (a "Replace part" button may come
  later; for now delete the combined cheat and make it again).
- Learning parts at the same time (would halve the time; uses 2 of the 4 debug
  registers).
- PC-off cheats, splitting, or editing the values of a combined cheat.
