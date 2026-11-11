# Cheat Maker: Bring your notes to a new game version

Date: 2026-11-11. Kein: "Re-finding after a game update: cheats find their addresses again
after a patch." Generic for every game; Returnal (PPSA01285) is only the example. This is
the work architecture v3 D17 left for v3.1 ("move capture hooks and slots on update;
chains/vtables re-learn"). It builds on **Find it next time** (user guide 6.4), combined
cheats (6, docs/spec-restart-combined.md) and **Update for this version** (6.6, v2).
Points marked [OPEN] use their default until Kein says otherwise.

## Why

The app keeps its notes per game *and version* (`projects/{TITLEID}_{version}.cmproj`).
After an update the new version has no notes, so every Find it next time cheat, every
combined cheat and their fixed code parts are gone, and the player has to search and
learn everything again. Today:

- v2 moves only cheat files (GoldHEN / CheatRunner). A file that holds the find-it-again
  hook or a PC-off cheat fails anyway: "it uses game code at +X the app can't follow"
  (the hook's slot is in the game's data, not its code).
- Hooks save a fingerprint of their place in the code (`signature`), but without checking
  that it is unique, and it doesn't allow for the field moving inside the object.
- The notes' fixed code parts never get a fingerprint (fingerprints are only learned when
  a cheat file of the running version is opened).
- The object's class (vtable) moves with an update; the saved one makes every check
  fail ("different kind of object").

## Words

- **Bring over**: copy the old version's notes to the new version and find every value's
  new place.
- **Old notes / new notes**: the notes file of the version before / after the update.
- **Fingerprint**: a short piece of the game's code around a place, with the parts that
  change between versions left open (v2 calls it a signature).
- **Check it**: brought over, but the player hasn't confirmed it yet.

## What comes over, at a glance

| In the notes | How it comes over | Player needed | If it fails |
|---|---|---|---|
| Labels, fields, types, last values, cheats and their values | Copied | No | - |
| Found by the game's code (hook) | Fingerprint found in the new code; new field offset read from it; new spare room and slot; slot checked 60 s; class learned again 10 s | Play normally, about 2 min | Object kept with no way to find it (AC-U7) |
| Other fields in the same object | Shifted like the hooked field | Yes (question) | Question says No: search |
| It doesn't move (static) | Same offset tried | Yes (question) | Search |
| A memory path (chain) | Tried as is; counts as new (not proven) | Yes (question) | Dropped |
| A memory search (class) | Learned again from the object at the first Yes | Yes (question) | Off until then |
| Fixed code change (code_patch part) | v2 rules | Test it | Combined cheat not brought over |
| Fixed value in the game's data (module_value part) | Not brought over [OPEN] | - | Combined cheat not brought over |
| History, Make it work crash marks | Not brought over | - | - |

## Acceptance criteria

### AC-U1 Noticing an update
- On Connect and Refresh, the app looks for old notes when the running title and version
  have no notes, or notes with no objects and no cheats. It also needs notes for the same
  title ID with another version that hold at least one object.
- Versions are compared number by number after dropping leading zeros (`03.000.005` <
  `03.001.000`; `1.05` = `01.05`). Text that isn't a number is compared as text. The
  source is the closest older version.
  [OPEN: Kein, downgrades. Default: if only newer notes exist, the closest newer one is
  offered with the same flow, worded "made for a newer version".]
- Never offered when the version is "unknown" or empty, or for another title ID (so never
  between a PS4 CUSA and a PS5 PPSA game, or between regions).
- The offer is a status line with two buttons, not a pop-up: "Your notes for Returnal are
  for version 03.000.005; the game is now 03.001.000. Press **Bring them over** and keep
  playing for about 2 minutes." **Bring them over** / **Not now**.
  [OPEN: Kein, ask first or do it by itself? Default: ask (the player must be in game, and
  hooks get written).]
- **Not now**: not offered again for this pair of versions. **Advanced: Bring notes from
  another version...** stays available.
- Nothing is written to the game before the press. Log:
  `action=carry_offer title=... from=... to=... objects=N cheats=N`.
- Given notes only for 03.000.005 and 03.001.000 running, when the player connects, then
  the offer shows within 5 s of "Connected".
- Opening an old cheat file works as today (v2 warning and Update button).

### AC-U2 Fingerprints are learned while the old version runs
- Whenever notes for the running version are loaded (Connect, Refresh) or saved (Find it
  next time, Combine, Bring them over), every hook and every fixed code part in them must
  have a fingerprint that matches exactly once in the code. For those that don't, the app
  reads the game's code once in the background (reads only) and learns them with v2 rules
  (16 to 96 bytes, unique in the code, the original bytes laid over our own jumps and
  patches, so it works with cheats and hooks on or off).
- Hooks keep their fingerprint in the notes (`signature`). One saved before this change
  (not checked for uniqueness) is replaced. Fixed code parts go in
  `signatures/{TITLE}_{version}.json`, the same file as v2.
- At most one code read at a time, once per version per app run. It doesn't delay Find
  it again and stops on disconnect.
- Log: `action=learn_signature source=notes object=... offset=0x...` and
  `action=learn_signatures source=notes saved=N version=... seconds=...`.
- Given notes with one hook and the old version running, when the player connects, then
  within 3 minutes [OPEN: C21a measures] the hook's fingerprint is unique and saved, and
  nothing was written to the game for it.
- Old notes with no fingerprints are still brought over with the fallback in AC-U4 / AC-U6.

### AC-U3 Bring them over: what the player does and sees
- Needs a connection and the game in front. While it runs, Find it next time, Lock it,
  Make it work and Combine are off.
- The hint shows each step in plain words: "Reading the game's code (reads only)...",
  "Finding Health in the new version: play normally, take a hit if you can.", "Checking
  the spare memory (about a minute, keep playing)...". A progress bar covers all steps.
- Order: objects found by the game's code first, then static ones, then memory paths.
- **Cancel** stops within 2 s. Every hook put in is taken out. No new notes are written
  and the old notes are unchanged. The offer stays.
- At the end, one line in v2 style: "Brought over 3 of 4 cheats for version 03.001.000.
  Check them: Health, Obolites. Not brought over: Shield (the game's code changed where
  it was found)." Each cheat's **Works after restart?** says **Found again each run
  (check it)** until it is confirmed (AC-U8).
- Time, not counting the player's answers: at most 3 minutes for Returnal with two hooked
  objects [OPEN: C21 measures]. A game whose notes only hold static values or memory
  paths needs no hook and no slot check: done within 30 s.
- If the connection drops midway: nothing is saved, and the offer comes back on the next
  connect.

### AC-U4 Values found by the game's code (hook)
- The hooked instruction is looked for in the new code, in this order. Each must match
  exactly once:
  (1) the fingerprint;
  (2) the fingerprint with the instruction's field number (`[r12+2ACh]`) also left open,
      because the field may have moved inside the object;
  (3) with no fingerprint, the original bytes under the hook alone, if 6+ bytes ("found
      from its original bytes only": shown in the summary).
  Otherwise it fails: "the game's code changed where Health was found".
- The match must be the same kind of instruction: same operation, memory operand
  `[register + number]` with the same register, same read/write role. Otherwise it fails
  with the same reason.
- The hooked field's new offset is the number in the matched instruction.
  [OPEN: other fields of the same object. Default: shift by the same amount and mark them
  "check it".]
- Original bytes are taken from the new code, and the 5+ byte single-instruction rule is
  checked again.
- A new capture room and slot are picked (never the old offsets, never a room kept by
  other notes or by cheats in My cheats). The slot is checked exactly as Find it next
  time does (60 s, the player keeps playing).
- Class:
  - If the object had one, the hook first collects the objects the instruction touches,
    for 10 s (as Player only collects).
  - Objects that lie in the game's writable memory, start with a pointer into the game's
    module, and whose fields (new offsets) look right are grouped by that pointer.
    Exactly one group: that is the new class, and the hook is rebuilt to keep only it.
    [OPEN: more than one group. Default: the player picks by watching the number change
    (today's pick dialog).]
  - None: the hook fails with "nothing in the game touched Health while we watched".
  - An object whose hook kept a Player-only guess gets the guess made again, as Find it
    next time does. No filter: as before.
- Log: `action=carry_hook object=... old=0x... new=0x... how=exact|loose|original
  field_shift=0x... cave=0x... slot=0x... class=0x...|-` or `result=failed reason=...`.

### AC-U5 Static values, memory paths, memory search
- Static: the old module offset is tried. If the object checks out it is kept, marked
  "check it". Otherwise the object has no way to be found (AC-U7).
- Memory paths: each is tried as it is. One that leads to an object that checks out is
  kept with `runs_seen` 1 (it proves itself again after restarts, as today); the others
  are dropped.
- Class: the object's saved class is cleared, the memory search is off, and field ranges
  go back to the normal range around the last value. At the player's first Yes (AC-U8),
  the class is learned again from the object's first 8 bytes (if they point into the
  module), and the memory search and the wide ranges come back.
- If two ways give different objects: today's pick dialog.
- Log: `action=carry_anchor object=... kind=static|chain result=kept|dropped reason=...`.

### AC-U6 Fixed parts of combined cheats
- Fixed code parts follow v2 rules: the old version's fingerprint; without one, the
  original bytes alone (6+ bytes, unique: "found from original bytes only"). New code
  over spare zero bytes moves together to a new spare room, which is never a room kept
  by hooks or cheats. Jumps are re-aimed, and a target the app can't follow fails.
- Fixed values in the game's data are not brought over: "it changes a value in the
  game's data, which moves with an update". [OPEN: Kein. Default: not brought over.]
- A combined cheat comes over only if every part does. Otherwise the summary names the
  cheat, the part and the reason ("Health: part fraction couldn't be found (...). Make
  it again."). It stays in the old notes.
- Log: `action=carry_part cheat=... part=... kind=... old=0x... new=0x...|- result=...`.

### AC-U7 What is saved
- A new file `{TITLEID}_{new version}.cmproj`, format 4. It adds `"carried_from":
  "<old version>"` and, per object, `"check": true` until the player's Yes. An older app
  says "made by a newer Cheat Maker" and keeps the file (today's rule).
- An object that couldn't be found in any way is still saved, with no way to find it, and
  so are its cheats. On connect the player gets today's "Couldn't find Health this time
  ... search for Health", with the guided search filled in with the type and last value.
  After that, Find it next time works as today.
- The old notes and the old fingerprint file are opened read-only and never written,
  renamed or deleted by this feature. Given the old notes file's contents before
  bringing over, after success, failure or Cancel the contents are byte for byte the
  same.
- New notes that already hold objects or cheats are never overwritten.
  **Advanced: Bring notes from another version...** only adds objects and cheats whose
  names aren't in the new notes yet. [OPEN: default yes]
- After a successful bring-over, the new version's fingerprints are learned straight away
  from the code already read (AC-U2), so the next update works too.
- Log: `action=carry_done from=... to=... objects=N found=N cheats=N carried=N failed=N
  seconds=...` and one `action=carry_cheat name=... result=carried|failed reason=...`
  per cheat.

### AC-U8 The player confirms
- Before the first write to a brought-over object (each run until confirmed): "Is this
  your Health? It was brought over from version 03.000.005 and shows 300 right now. Take
  a hit and watch it change. Press Yes only if it does."
  - **Yes**: the object is confirmed (`check` cleared), its class is learned again
    (AC-U5), and the notes are saved.
  - **No**: nothing is written. Status: "OK, nothing was changed. Search for Health to
    find it in this version."
- A combined cheat asks one question for the whole cheat (as today).
- A cheat with brought-over fixed code parts says **Brought over: test it** until its
  first tick. The hint on ticking: "Test it in the game; untick it if it doesn't work."
- A brought-over object is never written before a Yes.

### AC-U9 Cheat files with the find-it-again hook or a PC-off cheat
- In v2 **Update for this version**, lines that are a notes hook (capture routine and
  jump, or keep-value routine and jump, recognised by the old notes' hook and room
  offsets) are rebuilt from the brought-over notes, not moved.
- If the notes haven't been brought over yet: "bring your notes over first (My cheats)".
  This replaces "it uses game code at +X the app can't follow" for these lines.
  [OPEN: default in scope]

### AC-U10 Unchanged behaviour
- Notes of the running version, Find it again, Find it next time, Combine, Lock it, Make
  it work and v2 cheat-file updates work as before. Existing tests pass.
- Notes in formats 1 to 3 load as before.

### AC-U11 Automated tests (fake PS5)
- A fake game with a second version whose code is shifted, and whose hooked field moved
  by 8 bytes. Every row of the table above gets a pass and a fail case.
- Tests also cover: Cancel at each step; the old notes staying byte-identical; no offer
  for "unknown"; no offer for another title ID; no overwrite of new notes.

### AC-U12 User guide
- Section 6.6 explains, in plain words: connect once on the old version before updating
  (the app learns fingerprints); after the update, press **Bring them over** and keep
  playing; what "check it" means; what to do with a cheat that didn't come over.
- Section 9 adds "Bring over" and "Fingerprint".

### AC-U13 Console tests C21 (docs/console-tests.md)
- C21a to C21g. They need C10 to pass first.

## Not in this spec
- PS4 to PS5 or between regions (different title IDs).
- Merging two versions' notes.
- Learning a fingerprint per field (only the hooked field's offset is read from the code).
- Fixed values in the game's data.
- Carrying history or Make it work crash marks.
- Bringing notes over with the PC off.
- Repeating a failed bring-over on its own (the player uses Advanced).

## Console tests (to add to docs/console-tests.md)

### C21 Bring notes to a new game version (added 2026-11-11)

Spec: docs/spec-game-update.md. Needs C10 (and C19 for combined cheats) to pass first.
Save your game first. `tools/make_old_notes.py` (new) copies the notes and fingerprints
of the running version as if they were from an older version (it sets the real ones
aside as `.keep` and puts them back with `--undo`), so C21b to C21g can run before a real
update.

| # | Test | Steps | Pass when | Send back |
|---|---|---|---|---|
| C21a | Fingerprints, old version (reads only) | Returnal with notes from C10 or C19 (health found by the game's code). Connect and keep playing for 3 min. | Log `action=learn_signatures source=notes saved=N` within 3 min; each hook has a fingerprint; no write other than today's hook. | The `action=learn_signature*` and `action=read_code` lines. |
| C21b | Dry run, Returnal | Close the app, run `python tools/make_old_notes.py PPSA01285 03.000.005 03.000.004`, start the app, Connect. Press **Bring them over**, keep playing, take hits when asked. Tick health, answer Yes, take hits. Then restart the game and connect. | The offer names 03.000.004. Summary "Brought over N of N". Field shift 0 and class the same as before in the log. One question per cheat. Health holds. After the restart, health is found again by the game's code. The 03.000.004 file is unchanged. | The summary, `action=carry_*` lines, time from the press to the summary. Run `--undo` afterwards. |
| C21c | A real update | Any game with notes: run C21a before updating. Update, Connect, **Bring them over**. Check each cheat. | Each brought-over cheat works after a Yes; each cheat that didn't come over has a plain reason; no crash, no wrong write. | Summary, `action=carry_*` lines, which cheats worked. |
| C21d | A simple game | A game whose value doesn't move (static) or has a proven memory path. Dry run as C21b with its title ID and version. | Done within 30 s plus the question; no hook, no slot check in the log. | `action=carry_*` lines. |
| C21e | Cancel | Dry run as C21b, press **Cancel** during "Checking the spare memory". | Stops within 2 s; `action=hook_remove`; no new notes file; offer shows again on the next connect. | The hint and log lines. |
| C21f | Cheat file with the hook | After C21b, open your saved file that has the find-it-again hook (made for the old version), press **Update for this version**, Save, Send to PS5, restart, turn the hook on in CheatRunner, Connect. | Hook lines rebuilt (not "can't follow"); log `action=hook_install ... already in the game`. | Those log lines and the summary. |
| C21g | Combined cheat with a fixed part | A combined cheat with a learned part and a Make it work part (C18 + C19). Dry run. | Comes back as one row: "Brought over: test it"; one question; both parts work. | `action=carry_part` lines. |
