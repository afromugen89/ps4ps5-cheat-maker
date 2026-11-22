# Cheat Maker Pro: refined specification (DRAFT, 2026-11-21)

Status: DRAFT. Recommended answers are filled in as defaults; each one marked `[OPEN: ...]` needs
Kein's confirmation. The source draft is `cheat-maker-pro-draft.md`, and the challenges are in
`cheat-maker-pro-review.md`.

## 1. Goal

Add a "Pro" mode to Cheat Maker for users who want to make each decision by hand: scan, find the
instruction, read code and memory, write NOP or code-cave patches in assembly with variables, and
filter the player from enemies. The result must still export to cheat files CheatRunner runs with
the PC off. The guided mode stays as it is, and both modes share one connection, one cheat model
and one project file per game and version.

Out of scope: SHNEXT and MultiTrainer, engine-specific tools (e.g. Unreal), online games, PS4
consoles (only PS4 games running on a PS5), and memory allocated by the PC tool that CheatRunner
can't recreate.

## 2. Release plan

| Release | Items | Gate |
|---|---|---|
| R0 (no build) | Kein uses ReaperPy as it is on his PS5 for about a week (spike S-P0) | – |
| R1 | PR-1 Pro window, PR-2 manual scanner, PR-3 find what writes/accesses (watchpoints), PR-6 code view, PR-7 memory view (read-only), PR-8 cheat editor (patch/NOP/cave, constant variables), PR-9 reference points, PR-12 export json/shn/mc4, PR-14 import | S-P1, S-P2 pass for the register and access parts of PR-3 |
| R2 | PR-4 filters, PR-5 player vs enemy, PR-10 check templates, PR-13 journal + kernel log, PR-7 editing, writable variables, presets, `.reapproj` import, docking | S-P8 for presets / master code |
| R3 | PR-11 debugger (break, step, halt, registers incl. XMM, stack) | S-P4 to S-P7 pass |

[OPEN: Kein to confirm the R1 cut and to say what the video's "second method" (40:55) does.]
[OPEN: build or adopt. Default: extend Cheat Maker and copy chosen ReaperPy (MIT) modules with credit in NOTICE.]

## 3. Constraints

- Windows PC app, Python 3.12 + PySide6, GPL-3.0. Every new dependency must be free and GPL-3.0-compatible.
- Payload: PS5Debug-NG **1.3.2 or later** on PS5 fw 13.60. [OPEN: confirm the exact build Kein runs; the app logs `version()` at connect.]
- PS5 games (PPSA) and PS4 games on PS5 (CUSA). Main module `eboot.bin`, offsets from the module base.
- No game-specific logic.
- All core logic is tested on FakeConsole (extended with a fake debugger). Every console-facing feature has a C-P* console test.
- Assembler: NASM (BSD-2), bundled and run as a separate program. [OPEN: or Keystone, if its licence turns out to be "GPL-2.0 or later".]
- mc4 needs AES-256-CBC (dependency `cryptography` or equivalent). [OPEN: Kein OK with the public mc4 key being in source?]

## 4. Console spikes (before the gated items)

| ID | What | Pass when |
|---|---|---|
| S-P0 | ReaperPy on Kein's PS5: attach, 1 breakpoint, step, halt/resume, registers, mc4 export loads in CheatRunner | Kein reports each item works or doesn't |
| S-P1 | All general registers in the trap packet (= C10a/C20a) | `rN+disp == watched address` for the writer on 3 hits |
| S-P2 | `access` watchpoint (breaktype 3); 4 slots (index 0-3) at once | Readers and writers listed; each slot reports its own hits |
| S-P3 | Execute watchpoint doesn't fire again on the same instruction (= C20d) | 10 continues with no repeat and no hang |
| S-P4 | Attach once and keep the session: 10 min of watch/continue cycles; detach leaves the game running | No disconnect; the game runs normally after detach |
| S-P5 | INT3 breakpoint, single step, halt/resume, get/set registers, XMM from savefpu | Each command does what PROTOCOL.md says on 3 tries |
| S-P6 | S-P1, S-P2 and S-P5 on a CUSA game | Same results as on PPSA |
| S-P7 | Hit rate: an instruction hit 60×/s; measure the game's frame stutter and the PC's handling | Recorded numbers; sets the limits in PR-3 and PR-11 |
| S-P8 | CheatRunner: an mc4 made by the app loads (also one whose game title has `&`, e.g. "Ratchet & Clank", and a cheat name with `& < > "`: check how CheatRunner shows them); a cave in spare space works; two mods share bytes (what verify does); master code format; meaning of "cave of any size"; whether CheatRunner checks the off-bytes of a cave line whose on and off bytes are the same (Pro and guided routines are written that way) before turning the mod on or off | Kein reports the behaviour; recorded in docs/console-results.md |

## 5. Shared model (both modes)

- One `Session`, one console connection pool, one `Cheat` / `CheatFile` model, one project file
  `projects/{TITLEID}_{VERSION}.cmproj`, **format 5**. Format 5 adds a `pro_cheats` list:
  for each entry, its kind (bytes / nop / cave), module offset, original bytes, assembly source,
  variables (name, type, value, comment), AOB signature, and a comment. Format 4 files load
  unchanged. A format 5 file opened by an older app is set aside (existing `set_aside` rule).
- Each cheat has a **target** badge: `CheatRunner` (only fixed byte patches inside the module) or
  `PC only` (any heap address, pointer path, found-again anchor), computed from the
  existing `Cheat.saveable` / `session_only` / `anchor`.
- Guided cheats show up in Pro. Caves built by cave.py are stored with their assembly text
  (formatted by iced from the instructions that built them).
- Once a cheat has been edited in Pro it is a Pro cheat; the guided wizards (Make it work,
  Lock it options) no longer offer to rebuild it, and say so.

AC-S1. Given a cheat made by Lock it (player only), When Kein opens the Pro window, Then the cheat is listed with one Cave entry (its hook and its cave) that compiles to the same two lines (cave, jump), the cave shows assembly text that assembles to the exact saved bytes, and the badge says CheatRunner.
AC-S2. Given a cheat holding a heap address, a pointer path or a found-again anchor, Then its badge says PC only, and hovering it names the reason ("heap address", "pointer path", "found again each run"; plus "freeze" when such a cheat also freezes). Given a frozen value at a game-module (static) address, Then its badge says CheatRunner with the note "written once (CheatRunner can't freeze)", and it is exported (decided 2026-11-22, option C: guided behaviour and TC-60 unchanged).
AC-S3. Given a format 4 `.cmproj`, When the app loads and saves it, Then the objects, anchors and cheats are unchanged and `format_version` becomes 5.

## 6. Requirements

### PR-1 Pro window (R1)
A second window opened from the main window ("Pro"), with tabs: Scanner, Address list, Code,
Memory, Watch, Cheats. It uses the main window's connection and game. Closing it stops nothing
the guided mode started. [OPEN: docking panels deferred to R2.]

AC-1.1 Given the app is connected to a game, When Pro is opened, Then every tab shows the same game (title ID, version, PID) as the main window, and no new console connection is opened until a tab needs one.
AC-1.2 Given Pro is open, When the game is closed on the PS5, Then within 5 s every tab shows "game not running", and any watch session is ended without writing anything.
AC-1.3 Given the PC firewall blocks port 755, When a Pro feature needs the debugger, Then the app says so within 10 s and names the port, instead of waiting forever.

### PR-2 Manual scanner and address list (R1)
- Value types: signed and unsigned 8/16/32/64-bit integers, float, double; AOB (hex with `??`) over the game module only.
- Compares: exact, between, greater than, less than, increased, decreased, increased by, decreased by, changed, unchanged, unknown first value.
- Regions: game data (writable, not executable), all writable, all readable, game module.
- Results: address, current, previous and first value (first where NG gives 3-value records), paged 1,000 at a time.
- Address list: description, type, value, set, freeze, group, comment, badge; "Find what writes" / "Find what accesses" / "Show in Memory" / "Pointer scan" actions.

AC-2.1 Given a 4-byte exact first scan on a large PS5 game, Then it finishes within 2 min (existing target, AC-3.5) and shows progress with time left.
AC-2.2 Given results, When "greater than 100" is applied as a next scan, Then only results whose value is now > 100 remain (FakeConsole test with known memory).
AC-2.3 Given an address in the list with Freeze on, Then its value is written back at least 10 times a second until Freeze is turned off. The cheat it would make is marked PC only when the address is on the heap, and CheatRunner with the note "written once (CheatRunner can't freeze)" when it is a static address in the game's module (badge decision, option C).
[OPEN: does "greater/less than" need NG compare codes we haven't verified? The C-P2 console test covers it.]

### PR-3 Find what writes / accesses (R1)
- Hardware watchpoint (write or access; 1/2/4/8 bytes) on an address from the address list.
- The session runs until Stop, **60 s**, or **500 hits** (configurable), not 16.
- The list has one row per instruction: module+offset, disassembly, hit count (updated live), and registers of the first and last hit.
- "Find what an instruction accesses" uses the **cave sampler** (hooks the instruction and records the base register into a 64-slot table in spare data) and lists the distinct addresses with counts; the game never stops.

AC-3.1 Given a watch on health and the player taking 3 hits, Then the writer instruction is listed within 1 s of each hit, its count is ≥ 3, and its disassembly is the instruction *before* the reported RIP.
AC-3.2 Given the session ends (Stop, time or hit limit), Then the watchpoint is cleared, the debugger detached, the game keeps running, and the list stays visible.
AC-3.3 Given S-P1 has not passed, Then the register column is marked "unverified" in the UI.
AC-3.4 Given "what does this instruction access" on an instruction that touches `[reg+disp]`, When run for 20 s, Then distinct object addresses are listed, the hook is removed at the end, and the sampler's table is cleared (existing `collect_objects` behaviour).
AC-3.5 Given no spare code space for the sampler, Then the app refuses with "no room for the sampler" and offers a watch instead.

### PR-4 Filters (R2)
A filter is a condition: a register value, a value at `[reg+off]`, or the address being in a list.
A PC-side filter hides or counts hits; a sampler-side filter is compiled into the sampler cave
(reusing `_filter_check`).
AC-4.1 Given the filter `[r12+0x8] == 0x40048`, Then only hits that match are counted, and the non-matching count is shown separately.
[OPEN: game stutter limit for PC-side filters, from S-P7.]

### PR-5 Separate player from enemy (R2)
The objects from PR-3 / PR-4 are shown side by side: 4-byte fields over the first 0x200 bytes, and the
offsets where the player differs from every other object listed first (existing `guess_fields`).
"Use as condition" inserts `cmp dword [<reg>+off], value` / `jne <other>` into the open cave.
AC-5.1 Given 1 player object and ≥ 2 others, Then at least the existing guess list is shown, and the chosen field produces a condition the cave assembles with.

### PR-6 Code view (R1)
Live disassembly (iced-x86, Intel syntax) from any address or `module+offset`: 256 instructions a
page, follow jump/call (double-click), back/forward, watchpoints and cheat sites marked, "copy AOB" (wildcards on rel32/disp32), "make NOP entry", "make cave entry" here.
AC-6.1 Given `eboot.bin+0x5e1536c`, Then the page loads within 1 s on the LAN, and the instruction at that address is selected.
AC-6.2 Given a site already patched by an active cheat, Then the line shows the cheat's name, and "show original" displays the off-bytes disassembled.

### PR-7 Memory view (R1 read-only, R2 edit)
A hex view of 4 KiB at a time with a value inspector (all PR-2 types at the selected byte). R2: edit bytes (journaled, PR-13).
AC-7.1 Given an unreadable address, Then the view shows "?? " for those bytes and doesn't disconnect.

### PR-8 Cheat editor (R1)
- A cheat is a name plus one or more **entries**: Bytes (on bytes, recorded off bytes), NOP (instruction-aligned length), or Cave (assembly text).
- Cave entries: NASM-like Intel syntax; symbols given by the app: `hook` (the patched site), `return` (the first byte after the instructions covered by the jump), `original` (a macro emitting the covered instructions), and each variable by name. Labels are allowed. The app puts the cave in the module's spare code space (`find_cave`) and writes `jmp cave` + NOPs at the hook (`hook_span` rules: no call or branch inside the covered span).
- Variables (R1): read-only constants (`int8`…`int64`, `float`, `double`) with a value and a comment, placed in the cave. Writable variables (data slot) come in R2.
- Bytes are shown next to the source; the cave size and the free space are shown.

AC-8.1 Given a cave source, When typing stops, Then within 0.5 s either the bytes are shown or the first error is shown with its line number and the assembler's message.
AC-8.2 Given a cave that assembles to more bytes than the free space, Then saving and enabling are refused with "needs N bytes, M free".
AC-8.3 Given a hook site where the 5-byte jump would cover a call or a branch, Then the entry is refused with the existing `hook_span` message.
AC-8.4 (enable) Given every site holds its recorded off-bytes, When Enable, Then the cave is written first, then the jump, every site is read back equal to its on-bytes, and the cheat shows On. Given any site differs, Then nothing is written, and the site, the expected bytes and the found bytes are shown.
AC-8.5 (disable) When Disable, Then the jump site is restored first, the cave after 0.5 s, and every site is read back equal to its off-bytes.
AC-8.6 Given a write that spans an address ending in `0xFF`, Then a warning is logged and shown once (NG non-atomic write).
AC-8.7 Given a variable `mult: float = 2.0` used by the cave, When its value is changed to 5.0, Then the bytes are re-assembled; if the cheat is on, only the variable's bytes are rewritten live and read back.
AC-8.8 Given two cheats whose entries overlap, Then enabling the second is refused, naming the first.
[OPEN: Is live rewriting (AC-8.7) wanted, or only in the editor?]

### PR-9 Reference points (R1)
Each entry gets an AOB signature, generated automatically (it must match exactly once in the game module, at most 64 bytes, rel32/disp32 wildcarded) and editable. It is used by "Bring them over" after a game update and by import to re-find offsets.
AC-9.1 Given an entry, When its signature is generated, Then a scan of the module finds exactly 1 match at the entry's site; otherwise the signature is lengthened up to 64 bytes or marked "not unique".
AC-9.2 Given a new game version, When Bring them over is used, Then each Pro entry is re-found by its signature, or listed as "can't find" (existing update rules).

### PR-10 Check templates (R2)
Insertable snippets for the cave editor: compare a field, compare a register, skip if not the player (filter from PR-5), keep a value, never goes down, half damage (from the existing `effect_code`), all as editable text.
AC-10.1 Given any template inserted into an empty cave with `original` and `jmp return`, Then it assembles, and on FakeConsole it gives the same bytes as the matching guided builder.

### PR-11 Debugger (R3, gated on S-P4 to S-P7)
Attach for a session; up to [OPEN: N from S-P5] software breakpoints and 4 hardware watchpoints; continue, step in/over/out, run to, halt/resume; registers (general, flags, XMM0-15) viewable and editable while stopped; stack with return addresses; thread list.
AC-11.1 Given a breakpoint is hit, Then the stop view shows the thread, the registers and 16 stack entries within 1 s.
AC-11.2 Given a thread is stopped, Then the keep-alive never reconnects the debugger connection, and scans and the freezer are paused with a notice.
AC-11.3 Given Detach, Then all breakpoints and watchpoints are removed, and the game runs.

### PR-12 Export for CheatRunner (R1)
Formats: `.json` (GoldHEN), `.shn`, `.mc4` (SHN, AES-256-CBC, base64), named `{TITLEID}_{VERSION}.{ext}`.
Only CheatRunner-badged cheats are exported. Variables are written in at their current value, and
the existing `cheatmaker_value` / `CheatMakerValue*` extension records where they sit so Cheat
Maker can edit them in the file later. No master code (each cheat contains its own cave and jump).
AC-12.1 Given a project with 3 CheatRunner cheats and 1 PC-only cheat, When exported, Then the file holds 3 mods, and a dialog lists the PC-only cheat with its reason and offers "make a cave at its writer" (opens PR-3 → PR-8).
AC-12.2 Given an exported `.mc4`, When it is imported again, Then after decryption it equals `shn.dumps(cf, sanitise=True)` of the same project, byte for byte (the `.shn` text with `& < > "` replaced by `+ ( ) '` in every attribute; identical to the plain `.shn` when no attribute holds those characters).
AC-12.3 Given the exported file is sent to the PS5 and loaded in CheatRunner, When each cheat is turned on, Then it works with the PC off (console test C-P12).
AC-12.4 Given a game title, credit or cheat name containing `& < > "`, Then the `.shn` is valid XML with those characters escaped (unchanged from earlier versions), and the `.mc4` is valid XML with them replaced by `+ ( ) '`, which CheatRunner shows by name (S-P8).
[OPEN: presets (one mod per variable value) and master code, after S-P8.]

### PR-13 Journal and kernel log (R2)
The journal records every write made by the user or by a cheat (not freezer ticks): time, address, old bytes, new bytes, source. Undo one entry, or all of them. The kernel log viewer (port 3232) is read-only and highlights page faults and crashes.
AC-13.1 Given 3 Pro writes, When "undo all", Then the 3 sites hold their old bytes (read back), in reverse order.

### PR-14 Import (R1)
Import `.json` / `.shn` / `.mc4` into the project as Bytes entries; caves are shown disassembled. "Convert to assembly" makes a Cave entry with generated labels whose assembled bytes equal the imported bytes. Mods in section ≠ 0 are refused with a message.
AC-14.1 Given an imported cave, When converted to assembly, Then re-assembling gives the same bytes, or the conversion is refused, naming the instruction it can't express.

## 7. Non-functional
- NG client slots: Pro uses at most 1 debugger connection + 1 kernel-log connection on top of the existing ones; a scan pool shrinks to fit (existing fallback).
- Every Pro write, attach, watch session and export is logged as `action=pro_*`.
- Safety text shown once per game: single-player/offline only; back up the save first.
- Packaging: the assembler binary and licence notices go in the Windows build; NOTICE credits ReaperPy (MIT) for any code copied, and NG.
- Docs: a "Pro mode" chapter in docs/user-guide.md; console tests C-P1… in docs/console-tests.md.

## 8. Open questions (summary)
1. [OPEN: R1 cut and the "second method" at 40:55.]
2. [OPEN: build or adopt; try ReaperPy first?]
3. [OPEN: how CheatRunner places caves ("any size"), its verify behaviour, its master code format (S-P8).]
4. [OPEN: assembler: NASM or Keystone (licence check).]
5. [OPEN: live rewriting of variables; writable variables in R2; presets.]
6. [OPEN: the exact NG build (≥ 1.3.2?).]
7. [OPEN: the mc4 key in source.]
8. [OPEN: game stutter limits from S-P7 for filters and breakpoints.]
