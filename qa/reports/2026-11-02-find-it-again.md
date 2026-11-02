# QA report: v3 "Find it again" (AC-11 to AC-14)

- Date: 2026-11-02. QA: qa-engineer agent
- Build under test: branch `claude/project-thread-4ppl6l` at `7508b78` (diff vs `origin/main`, 39 files, +4336 lines)
- Environment: Linux, Python 3.12.3, PySide6 offscreen, FakeConsole + `tests/fakegame.py`. The x86 emulator check used unicorn 2.1.4 in a separate scratch venv. unicorn is not a project dependency.
- Inputs: spec v3 §AC-11..14, architecture v3 (§5 flow, §9 risks, §11 C10), implementation notes, the diff, and the existing tests.

## 1. Summary for the product owner

I tested "Find it next time" and "Find it again" end to end on the fake PS5 (two game runs, a moving player object, enemies of another class, a game that runs our encoded hook bytes), plus the `.cmproj` file and the PS4 (CUSA) differences. I also ran the hook code on a real x86-64 CPU emulator.

**Verdict: ⚠️ Ready for the console tests (C10) with known issues; not ready for release.**

What works well:
- The hook code leaves the game's registers, flags and stack untouched. This held on 6 instruction shapes, 3 filters, 2 module bases (including above 4 GiB, the PS4 case) and player vs enemy, on a real CPU emulator.
- After a restart the value is found again, checked, confirmed once and then written.
- When two methods disagree, nothing is written. When all methods fail, the guided search opens pre-filled.

Bugs: **9** (High 3, Medium 5, Low 1).
- **BUG-01 (High)**: after pressing Connect again, a dropped link, or Refresh from the PS home screen, the game-code hook stays in the game. From then on the app treats it as "not ours" and never removes it, which breaks AC-11.2.
- **BUG-02 (High)**: after a new biome, while the old character object still exists (Unreal keeps it until garbage collection), the freeze keeps writing to the old, dead object. It doesn't move to the new one, which breaks AC-12.5 on Returnal-like games.
- **BUG-03 (High)**: turning the PC-off cheat off wipes its routine instantly. A game thread that is inside the routine at that moment can crash the game.
- **BUG-04 (Medium)**: a PC-off cheat built on a "subtract from health" instruction skips the subtraction. The game's next decision then sees the wrong flags.
- **BUG-05 (Medium)**: a damaged `.cmproj` (wrong data types, or a bad saved value) raises a Python error during connect. Only fully corrupt JSON shows the plain message.
- **BUG-06 (Medium)**: an unreadable or newer `.cmproj` is overwritten with no backup on the next "Find it next time".
- **BUG-07 (Medium)**: a simple game with a short memory path still gets a game-code hook, during learning and on the first restart.
- **BUG-08 (Medium)**: a value that grows past 10,000 (or 10× its learned value), such as money, is no longer found again.
- **BUG-09 (Low)**: "Find it again" with the memory search silently ends a search the player had open.

## 2. Scope

**In scope:**
- `core/{anchor_hook,refind,project,vtable_find,cave(capture_code, hook_span, find_slot),freezer(tracked),locker(touches_memory)}`
- `ui/{find_again,cheats_view,save_dialog}`
- `console/fake.py` watch hits

**Out of scope:**
- The real PS5Debug-NG network path (`watch` register parsing). The contract tests already exist, and this path is console-only (C10a).
- updater re-aim (D17, v3.1).
- CheatRunner's own patch order.

**Risk areas, by priority:**
1. Game crash from the routine: span, registers/flags, removal.
2. Writing the wrong or an unverified object.
3. Hook lifetime: disconnect, close, last cheat off.
4. A hook CheatRunner already loaded.
5. PC-off cheat vs the live hook.
6. Freezer following moves.
7. `.cmproj` robustness.
8. PS4 vs PS5: module base, vtable above 2 GiB.
9. Genericity: simple games must not get hooks.

## 3. Test results

The baseline was the existing suite: 300 passed. Once, `tests/unit/test_ps5debug_ng.py::test_watch_writes` failed with `OSError: [Errno 98] Address in use` while the pr-reviewer's run used the fixed port 755 in parallel. It passed on rerun. This is a test-infra flake, not a product bug.

| TC | Title | Prio | Result | Notes / test |
|---|---|---|---|---|
| TC-01 | AC-11.1 learn a moving value: hook + class, saved, plain "Ready" words | H | Pass | existing `test_refind::test_learn_moving_value…`, `test_find_again::test_find_it_next_time…` |
| TC-02 | Static value (PS4 CUSA id): no watch, no patch, found as "doesn't move" | H | Pass | `qa::test_tc02_static_value_learns_without_watching_or_patching` |
| TC-03 | AC-11.2 hook out on app close | H | Pass | existing |
| TC-04 | AC-11.2 hook out on last cheat off, back on next on | H | Pass | existing |
| TC-05 | AC-11.2 hook out after Connect pressed again | H | **Fail** | BUG-01 `qa::test_tc05…` |
| TC-06 | AC-11.2 hook out after Refresh from home screen | H | **Fail** | BUG-01 `qa::test_tc06…` |
| TC-07 | AC-11.2 hook out after lost link + reconnect | H | **Fail** | BUG-01 `qa::test_tc07…` |
| TC-08 | AC-11.3 nothing holds: says so, suggests Lock it | M | Pass | existing |
| TC-09 | AC-12.1 found by the slot within 10 s after restart, ≤ 2 clicks | H | Pass | existing (fake timing only) |
| TC-10 | AC-12.2 confirm before first write; No = nothing written | H | Pass | existing |
| TC-11 | AC-12.3 disagreement: nothing written, pick dialog | H | Pass | existing |
| TC-12 | AC-12.4 all fail: guided search pre-filled | H | Pass | existing |
| TC-13 | AC-12.5 freeze follows a move (old object wiped) | H | Pass | `qa::test_tc13…` + existing |
| TC-14 | AC-12.5 freeze follows a move (old object still alive, UE4 pending-kill) | H | **Fail** | BUG-02 `qa::test_tc14…` |
| TC-15 | AC-13.1 PC-off cheat keeps value after restart | H | Pass | existing |
| TC-16 | AC-13.2 Save explains "only while connected" | M | Pass | existing |
| TC-17 | Capture routine invisible to the game: all GPRs, flags, red zone, object memory; slot filled for the player only | H | Pass | `qa::test_capture_emulated::test_tc17…` (120 emulated cases, unicorn) + `qa::test_tc17…` (static check, 36 cases) |
| TC-18 | PC-off keep routine on a read-modify-write writer keeps flags | M | **Fail** | BUG-04 `qa::test_tc18…` (both files) |
| TC-19 | PC-off cheat off leaves its routine for threads inside | H | **Fail** | BUG-03 `qa::test_tc19…` |
| TC-20 | hook_span refuses ret/branch/call/short spans | H | Pass | existing `test_cave::test_hook_span_refuses…` |
| TC-21 | install refuses foreign patch / used spare space / used slot | H | Pass | existing |
| TC-22 | CheatRunner-loaded capture hook: "already", not patched, not removed | H | Pass | existing |
| TC-23 | CheatRunner-loaded PC-off routine: no patch, found by slot, left alone on disconnect | H | Pass | `qa::test_tc23…` |
| TC-24 | Live PC-off cheat vs live capture hook (re-aim, put back) | H | Pass | existing M2 |
| TC-25 | Malformed `.cmproj` structure gives a plain ProjectFileError | M | **Fail** | BUG-05 `qa::test_tc25…` (3 cases) |
| TC-26 | Bad saved cheat value doesn't break connect | M | **Fail** | BUG-05 `qa::test_tc26…` |
| TC-27 | Empty / truncated / non-UTF-8 / newer-format file gives a plain error | M | Pass | `qa::test_tc27…` (4 cases) |
| TC-28 | Unreadable / newer file is kept before a new save | M | **Fail** | BUG-06 `qa::test_tc28…` |
| TC-29 | Old minimal file (no format_version, optional keys missing) loads | M | Pass | `qa::test_tc29…` |
| TC-30 | Short-chain game never gets a code hook | M | **Fail** | BUG-07 `qa::test_tc30…` |
| TC-31 | Value growing past 10,000 still found | M | **Fail** | BUG-08 `qa::test_tc31…` |
| TC-32 | PS4 CUSA project file name | L | Pass | `qa::test_tc33…` (asserts `CUSA00001_01.00.cmproj`) |
| TC-33 | Module above 4 GiB (vtable > 2 GiB): learn, wide compare, jump and slot in range | H | Pass | `qa::test_tc33…` + emulator |
| TC-34 | Find again's memory search vs the player's open search | L | **Fail** | BUG-09 `qa::test_tc34…` |
| TC-35 | Regression: full suite + ruff | H | Pass | 345 passed, 14 xfailed; ruff clean |

**Totals:** 35 TCs, 23 pass, 12 fail (14 xfail tests across 9 bugs). The emulator file adds 120 passing cases and 2 xfail where unicorn is installed.

## 4. Bugs

### BUG-01: Hook left in the game after a reconnect, a lost link or Refresh from the home screen; never removed afterwards
- Severity: High. Priority: P1.
- Related test cases: TC-05, TC-06, TC-07.
- Automated tests: `tests/qa/test_find_again_qa.py::test_tc05_reconnect_still_takes_the_hook_out_on_close`, `::test_tc06_refresh_from_home_screen_keeps_the_hook_ours`, `::test_tc07_lost_link_then_reconnect_takes_the_hook_out_on_close`
- Environment: `7508b78`, Linux, Python 3.12.3, FakeConsole.
- Steps to reproduce (A):
  1. Connect.
  2. Find it next time on health. The hook is in: `jmp` at READER.
  3. Press **Connect** again.
  4. Close the app.
- Steps to reproduce (B):
  1. Do steps A1 and A2.
  2. Press the PS button (foreground = home screen) and press **Refresh**.
  3. Go back to the game and press **Refresh**.
  4. Close the app.
- Steps to reproduce (C):
  1. Do steps A1 and A2.
  2. The link drops (`handle_lost`).
  3. Connect again.
  4. Close the app.
- Expected: AC-11.2, "the hook is removed when the app disconnects". At close, the hooked instruction is back to `C4C17A108424AC020000`.
- Actual: the site stays `e9eb390000909090…` in all three cases. `FindAgain.installed` is `{}` after the reconnect.
- Suspected cause:
  - `Session.connect()` calls `self.disconnect()` directly. That path skips `Controller.before_disconnect`, which only `shutdown()` runs.
  - `FindAgain.on_game` then does `installed.clear()` without removing anything.
  - Case B: with the home screen in front, `refresh_game` falls back to `processes()`. That gives a different `GameInfo` (version "unknown", title = title id), so `game_changed` fires for the same running process.
  - In every case, the next `install()` returns `"already"`, so the hook is treated as CheatRunner's and is never recorded as ours (`find_again.py` `start()`, `record` only on `"installed"`).
  - Suggested fix:
    - Run the before-disconnect hooks inside every disconnect or reconnect.
    - Don't clear `installed` when the pid is unchanged.
    - Remember "ours" per (title, pid) across reconnects, for example a marker in the slot counter or a session-scoped record keyed by pid.
- Impact for the product owner: after a Wi-Fi blip or a reconnect, the game keeps running Cheat Maker's code change until the game is restarted, even after the app is closed. The spec says it must be removed.

### BUG-02: Freeze keeps writing the old character after a new biome while the old object still exists
- Severity: High. Priority: P1.
- Related test case: TC-14.
- Automated test: `tests/qa/test_find_again_qa.py::test_tc14_freeze_follows_the_new_object_when_the_old_is_still_alive`
- Steps to reproduce:
  1. Learn health on run 1 and install the hook. Create a Tracker at the player.
  2. Build the new player at `HEAP+0x6000`. Leave the old object intact, as UE4 does: the destroyed pawn stays pending-kill until GC, with the same vtable and plausible health.
  3. Run one game frame. The slot now holds `0x20006000`.
  4. Call `tracker.current()`.
- Expected: AC-12.5 (and C10f), "within 5 s the app notices, stops writing, re-finds and resumes". The tracker returns the new object, or None while unsure.
- Actual: `tracker.current() == 0x20000100`, the old object. The freezer keeps writing there for as long as the old object survives, and never freezes the new character.
- Suspected cause: `refind.Tracker.current()` only consults the slot (`_slot_elsewhere`) for *weak* objects. For strong (vtable) objects, a passing `verify()` at the old address is trusted. Suggested fix: for any object with a hook, also read the slot each tick (8 bytes). If the slot names a different object that verifies, switch to it, because the game's code is handling the new one.
- Impact for the product owner: on Returnal-like games, "infinite health" can silently stop working after a new biome for up to about a minute. During that time the app writes into memory the game is about to free.

### BUG-03: Turning the PC-off cheat off wipes its routine the instant it is unhooked
- Severity: High. Priority: P1.
- Related test case: TC-19.
- Automated test: `tests/qa/test_find_again_qa.py::test_tc19_pc_off_cheat_off_leaves_its_routine_for_threads_inside`
- Steps to reproduce:
  1. Find it next time.
  2. Press **Also make a PC-off cheat**.
  3. Tick that cheat, then untick it.
- Expected: per architecture §9 and the `cave.py` / `anchor_hook.remove` rule "leave routines in place" (a thread preempted inside must still find valid code), the routine bytes stay after the jump is restored.
- Actual: the write log is `0x400200 (10 bytes, original)`, then `0x403af0 (93 bytes of zeros)` with no delay, then `0x400200` (the app's hook put back). The routine reads `00 00 …`.
- Suspected cause: the PC-off cheat is a plain 2-line CODE_PATCH whose `off` for the routine is `bytes(len(code))` (`anchor_hook.hook_cheat`). `LiveCheats.turn_off` writes both in reverse order back to back. Suggested fix: give the routine line `off == on`, so the routine is left in place. The jump line alone turns it off. CheatRunner gets the same benefit.
- Impact for the product owner: on the hooked per-frame reader, untoggling the PC-off cheat can crash the game in rare cases.

### BUG-04: PC-off "keep value" routine skips a flag-setting writer, so the game's next branch sees stale flags
- Severity: Medium. Priority: P2.
- Related test case: TC-18.
- Automated tests: `tests/qa/test_find_again_qa.py::test_tc18_keep_routine_on_a_read_modify_write_keeps_the_games_flags`, `tests/qa/test_capture_emulated.py::test_tc18_keep_routine_on_sub_leaves_flags_as_the_game_expects` (needs unicorn)
- Steps to reproduce:
  1. Call `cave.capture_code(span(sub [rax+2ACh],ecx), …, value=999)`.
  2. Run it (emulator).
- Expected: the game's code after the hook (for example `sub [hp],dmg ; jle dead`) sees flags consistent with what the game wrote, or the hook is refused for such writers.
- Actual: for the player, `rest = span[1:]` drops the `sub`. The flags are the ones from before the instruction (emulator: EFLAGS differs from running the original).
- Suspected cause: `cave.capture_code`, the keep branch for writers. Suggested fix:
  - Run the original instruction, then store the value. `mov` doesn't touch the flags.
  - Or store before and after the original.
  - Or refuse PC-off on writers whose instruction sets flags. Readers are already preferred.
- Impact for the product owner: on some games, the PC-off cheat could trigger death or other logic even though health stays full.

### BUG-05: A damaged `.cmproj` raises a raw Python error during connect
- Severity: Medium. Priority: P2.
- Related test cases: TC-25, TC-26.
- Automated tests: `tests/qa/test_find_again_qa.py::test_tc25_malformed_project_is_a_plain_project_error[list|object-is-string|type-is-number]`, `::test_tc26_bad_cheat_value_in_project_does_not_break_connect`
- Steps to reproduce:
  1. Put `{"format":"cheatmaker-project",…,"objects":["x"]}` in `projects/PPSA01285_03.000.005.cmproj`. Other inputs that reproduce it: top level `[]`, or `"type": 5`.
  2. Connect.
- Expected: the plain message "notes … can't be read" (`ProjectFileError`) and the app carries on, as it does for invalid JSON.
- Actual:
  - `AttributeError: 'str' object has no attribute 'get'` reaches the Qt event loop from `FindAgain.on_game`.
  - With a valid file whose cheat `"value": "abc"`, `ValueError: could not convert string to float` comes from `ensure_cheats` (`values.py:33`).
  - The remaining `game_changed` handling for find-again is skipped, so no message and no find.
- Suspected cause: `project.load` catches `(OSError, ValueError, KeyError, TypeError, IndexError)` but not `AttributeError`. `ensure_cheats` encodes unchecked values. Suggested fix: validate types in `from_json` (dict/list/str/number), catch `AttributeError` in `load`, and validate `value` against `vtype` while loading.
- Impact for the product owner: a hand-edited or damaged notes file makes connecting misbehave silently instead of showing a clear message.

### BUG-06: An unreadable or newer `.cmproj` is overwritten without a backup
- Severity: Medium. Priority: P2.
- Related test case: TC-28.
- Automated test: `tests/qa/test_find_again_qa.py::test_tc28_unreadable_project_is_kept_before_a_new_one_is_saved`
- Steps to reproduce:
  1. Put in place a file with `format_version: 2`, as written by a newer Cheat Maker.
  2. Connect. The message "can't be read" shows, which is correct.
  3. Find it next time on health.
- Expected: the old file is kept, for example renamed to `.cmproj.bak` or `.broken`, before a new one is written. The original is user data.
- Actual: `save()` replaces it with a fresh format-1 project, and the original content is gone.
- Suspected cause: `FindAgain.on_game` sets `ctl.project = None` on `ProjectFileError`. `ensure_project` then creates a new one and `save` → `tmp.replace(path)` overwrites the file.
- Impact for the product owner: going back to an older app version, or one damaged byte, loses all learned values for that game.

### BUG-07: A simple game with a short memory path still gets a game-code hook
- Severity: Medium. Priority: P2.
- Related test case: TC-30.
- Automated test: `tests/qa/test_find_again_qa.py::test_tc30_short_chain_game_never_gets_a_code_hook`
- Steps to reproduce:
  1. Use the fake game with `chain=True`.
  2. Find it next time with "memory path" ticked. The chain is found (methods `hook_slot, vtable_scan, chain`).
- Expected: per architecture §5, "simple games stay simple": a short chain re-checked OK means a saved pointer chain and no hook. The session brief says the same.
- Actual: the hook is installed right after learning (`LearnDialog.do_start`) and again on the first restart (`start()` installs while the chain is still `runs_seen=1`). Only from the second restart on is it skipped. With the box unticked (the default), short-chain games always get a hook.
- Suspected cause: `refind.learn` always plans a hook when candidates exist. `LearnDialog.do_start` installs any hook. `FindAgain.start` installs before trying chains. Suggested fix:
  - When a chain exists, don't install during learning.
  - On connect, resolve chains first and install the hook only if they fail.
  - Optionally run a quick depth-3 chain check by default.
- Impact for the product owner: simple games get a code change they don't need, which adds crash risk for no benefit.

### BUG-08: A value that grows past 10,000 (or 10× its learned value) is no longer found again
- Severity: Medium. Priority: P2.
- Related test case: TC-31.
- Automated test: `tests/qa/test_find_again_qa.py::test_tc31_counter_that_grows_past_ten_thousand_is_still_found`
- Steps to reproduce:
  1. Learn at 100 (range `(0, 10000)`).
  2. The value becomes 25,000.
  3. Find it again.
- Expected: the same object with the same class is found. Ranges should catch garbage, not legitimate growth. This matters for currency and counters (Obolites, INT64).
- Actual: `Outcome(status="failed")`, with the log reason "Health doesn't look right". The freeze pauses for the same reason.
- Suspected cause: `refind.default_range` uses `max(10_000, value*10)` for every type, including integers. Suggested fix:
  - Integers: use the type's range (or ≥ 0) when the object has a class check.
  - Floats: keep only the finite check plus a wide bound.
  - Widen the range when the player confirms a value.
- Impact for the product owner: a money or counter cheat stops being found once the player earns a lot.

### BUG-09: "Find it again" with the memory search silently ends the player's open search
- Severity: Low. Priority: P3.
- Related test case: TC-34.
- Automated test: `tests/qa/test_find_again_qa.py::test_tc34_find_again_search_does_not_drop_an_open_search_silently`
- Steps to reproduce:
  1. Start a search in Find (100 found).
  2. In My cheats, press **Find it again**. The slot is empty, so the memory search runs.
  3. Go back to Find and press Next.
- Expected: either the Find screen is reset with a message, or the search still works.
- Actual: `vtable_find` calls `scan_cancel()`, but the `Scanner` still shows N matches, and Next raises "no scan to narrow down; start a new search".
- Suspected cause: `vtable_find.find_by_vtable` uses the console's single search session without telling `Scanner`. Suggested fix: call `ctl.scanner.reset()` (and refresh the Find view) before the memory search, or refuse it while a search is open.
- Impact for the product owner: a half-done search is lost with a confusing message.

## 5. Automated tests added

| File | What it covers |
|---|---|
| `tests/qa/__init__.py` | Package marker. |
| `tests/qa/test_find_again_qa.py` | 59 cases (45 pass, 14 xfail): hook lifetime across reconnect, refresh and lost link; tracker after a move with the old object wiped or still alive; PC-off routine removal; a static check of the capture routine (no register or memory change before the original instruction, 36 cases); keep-routine flags; PS4/high module base end to end (learn, install, wide compare, jump and slot); `.cmproj` corrupt, newer, old and overwrite cases; short-chain genericity; range growth; CheatRunner-loaded PC-off routine; search interaction. |
| `tests/qa/test_capture_emulated.py` | Runs the encoded routine on unicorn against the original instruction. 120 cases pass; 2 are xfail (BUG-04). Skipped when unicorn isn't installed, which is the case in the project venv. |

Full run:
- `QT_QPA_PLATFORM=offscreen pytest -q`: **345 passed, 14 xfailed** in 9.6 s.
- `ruff check .`: clean.
- With unicorn: `tests/qa/test_capture_emulated.py`: 120 passed, 24 skipped (no filter + enemy is not a valid case), 2 xfailed.

Each xfail is `strict=True`, so it turns red when the bug is fixed; then remove the marker.

## 6. Not tested / risks, and the TCs that only the console can verify

| Console-only | What the fake can't show | C10 test |
|---|---|---|
| Watch registers: r12 offset in the 0x4A0 packet; an RW watchpoint really reports a per-frame reader | NG register layout, breaktype 3 | **C10a** |
| Slot really unused by the game for 10 min plus a biome change (TC-21's prerequisite) | `.bss` use by the real game | **C10b** |
| AC-12.1 10 s on real hardware; hook install doesn't crash; torn 5-byte jump write (`install` notes it as UNVERIFIED) | real CPU and threads, NG write atomicity | **C10c** |
| CheatRunner-loaded hook / PC-off routine (TC-22, TC-23); CheatRunner's own on/off order for the routine line (BUG-03 also applies there) | real CheatRunner | **C10d** |
| Memory search ≤ 3 min | real scan speed | **C10e** |
| Freeze across biome and death; how long UE4 keeps the old pawn alive (sizes BUG-02); no crash from routine removal (BUG-03); keep routine on the real writer (BUG-04) | real GC and threads | **C10f** |
| PS4 CUSA: real module base, vtable above 2 GiB path, ps4debug differences (TC-32, TC-33) | real PS4 maps | **C10g** |
| AC-14.1 full proof: health + Obolites, 3 restarts / 2 biomes / 1 death, no wrong write (BUG-08 may show here with Obolites) | | **C10 (AC-14.1)** |

Other risks and gaps:
- `tests/fakegame.py` can't execute the wide-vtable compare (`cmp [obj],reg`), so the high-base path is covered by the emulator and static checks, not by `run_routines`.
- Cancel in Find it next time doesn't stop the 10 s watch or the 60 s slot proof. These are short, and I didn't file a bug.
- On connect the hook is installed even when no cheat is on (D14, by design). It is removed on close.
- `test_ps5debug_ng.py::test_watch_writes` binds the fixed port 755, so it can fail when two suites run at once. This is a test-infra issue only.

## 7. Release recommendation

Fix BUG-01, BUG-02 and BUG-03 before C10 if possible (C10c/f/d would otherwise run into them). The Medium bugs can come in the same round. Then run C10a-g.

## BUGS FOR FEATURE-IMPLEMENTOR

1. **BUG-01 (High)**: the hook is left in after Connect-again, a lost link plus reconnect, or Refresh from the home screen, and is never removed later.
   - Make pass: `tests/qa/test_find_again_qa.py::test_tc05…`, `::test_tc06…`, `::test_tc07…`
   - Expected: original bytes at the site after close. Actual: `e9…` stays.
   - Cause: `Session.connect()` → `disconnect()` skips `before_disconnect`; `FindAgain.on_game` clears `installed`; `refresh_game` fallback makes a different `GameInfo` for the same pid; `install()=="already"` is never recorded as ours.
2. **BUG-02 (High)**: a strong-object Tracker trusts the old address while the old object still verifies, and ignores the slot that already names the new object.
   - Make pass: `::test_tc14…`
   - Fix: in `Tracker.current()`, check `read_slot` for every object with a hook, not only weak ones.
3. **BUG-03 (High)**: the PC-off cheat's routine is zeroed right after unhooking.
   - Make pass: `::test_tc19…`
   - Fix: `hook_cheat` routine `Location.off = code` (leave it in place), so only the jump line toggles.
4. **BUG-04 (Medium)**: the keep routine drops a flag-setting writer (`sub [rax+2ACh],ecx`).
   - Make pass: `::test_tc18…` (and the emulated one if unicorn is installed)
   - Fix: run the original instruction and then store the value, or refuse such writers for PC-off.
5. **BUG-05 (Medium)**: a malformed `.cmproj` raises `AttributeError` / `ValueError` into the Qt loop.
   - Make pass: `::test_tc25…[3 cases]`, `::test_tc26…`
   - Fix: validate types in `from_json`, catch `AttributeError` in `load`, validate cheat values while loading.
6. **BUG-06 (Medium)**: an unreadable or newer `.cmproj` is overwritten without a backup.
   - Make pass: `::test_tc28…`
   - Fix: rename it to `.bak` before the first save, or refuse to save over it.
7. **BUG-07 (Medium)**: a short-chain game still gets the hook (at learn time and on the first restart).
   - Make pass: `::test_tc30…`
   - Fix: no install when a chain exists; on connect, try chains before installing.
8. **BUG-08 (Medium)**: `default_range` caps at 10× / 10,000, so a counter that grows is lost.
   - Make pass: `::test_tc31…`
   - Fix: wider integer range for objects that have a class check.
9. **BUG-09 (Low)**: the memory search in Find it again ends the player's open search without resetting `Scanner`.
   - Make pass: `::test_tc34…`
   - Fix: reset the scanner and the Find view first, or refuse while a search is open.

No GitHub issues were created (per session instructions); the main session files them.

---

## 8. Re-test after fixes (82aec57..59201ae), 2026-11-02

**Build:** `59201ae`, same environment. Unicorn is now installed in the project venv, so `tests/qa/test_capture_emulated.py` runs.

**How each fix was checked:**
- Diffing `8b6b875..59201ae -- tests/qa` shows only xfail markers removed. `test_tc28` now writes `pj.FORMAT_VERSION + 1` instead of `2`, which is still "newer than this app". Every QA test still exercises the original bug.
- I read each fix in `src/`.
- I probed each fix's side effects.

**Full run:** `pytest -q` gives **500 passed, 24 skipped, 4 xfailed**. `ruff check .` is clean.
- The 24 skips are emulator cases that are invalid (no filter + enemy).
- The 4 xfails are new or reopened bugs, listed below.
- The emulator suite runs 120 capture cases plus 2 keep-on-`sub` flag cases; all pass.

### Status of BUG-01..09

| Bug | Status | Evidence |
|---|---|---|
| BUG-01 hook left after reconnect / lost link / refresh | **Partly fixed, reopened** | TC-05, TC-06, TC-07 pass. Fix: `run_before_disconnect` before Connect, plus a `_mine` record so an "already" result for our own hook is recorded as ours. Still open: **Refresh while the PS home screen is in front, then close the app** leaves the hook in (`test_r02…`). Cause: `on_game` (with the "unknown"-version GameInfo) clears `installed` while the game process is still running, so `shutdown` has nothing to remove. A lost link followed by closing without reconnecting is documented as a known limit (C10); that is accepted. |
| BUG-02 freeze stays on the old object | Fixed | TC-14 passes. `Tracker` reads the slot every tick when the hook filters. **This fix introduced BUG-10.** |
| BUG-03 PC-off routine zeroed on off | Fixed | TC-19 passes. The routine line's `off == on`, so only the jump toggles. CheatRunner benefits too. |
| BUG-04 keep routine skips a flag-setting writer | Fixed | TC-18 passes in both the static and the emulated test. The writer runs first and the value is stored after it. Flags now come from `999 - dmg`; `mov` leaves them alone. |
| BUG-05 malformed `.cmproj` crashes | Fixed | TC-25 (3 cases) and TC-26 pass: type checks in `from_json`, and `AttributeError` is caught. |
| BUG-06 unreadable file overwritten | Fixed | TC-28 passes. The file is set aside as `.cmproj.bak` (`.bak2`, …) before the first save. |
| BUG-07 short-chain game gets a hook | Fixed | TC-30 passes. Chains are tried first on connect, and there is no install at learn time when a chain exists. **This fix introduced BUG-11.** |
| BUG-08 10,000 cap | Fixed for new notes | TC-31 passes: `class_range` gives float ≤ 1e9 and the full int range when a vtable is known. Files migrated from format 1 keep the old cap (BUG-12, Low). Objects without a class check keep the cap by design. |
| BUG-09 memory search drops the open search | Fixed | TC-34 passes. `Session.take_search` resets the Scanner, and the Find view shows `SEARCH_LOST`. |

### Regression probes (pass)

| Probe | Result | Test |
|---|---|---|
| Two values in one game: separate routine rooms (keep and capture) and slots, both hooks installed, both removed on close (`find_cave` / `find_slot` skip reserved rooms) | Pass | `test_find_again_retest.py::test_r05…` |
| Format-1 notes: hook dropped (never installed), found by memory search, "older Cheat Maker" message, re-saved as format 2 with only `vtable_scan` | Pass | `::test_r06…` |
| Connect again with a frozen cheat: hook taken out and put back once, still ours, removed on close | Pass | `::test_r07…` |
| Keep routine on all writer and reader forms (emulator) | Pass | `test_capture_emulated.py` |
| Scanner reset: Find view cleared with a message | Pass | implementor's `test_find_again_memory_search_tells_the_find_view` |

### New bugs

#### BUG-10: Freeze silently switches to another object of the same class (regression from the BUG-02 fix)
- Severity: High. Priority: P1.
- Automated test: `tests/qa/test_find_again_retest.py::test_r01_second_object_of_the_same_class_is_never_followed_silently`
- Steps to reproduce:
  1. Learn health and install the hook. Create a Tracker at the player.
  2. A second live object of the player's class (clone, second character, co-op partner) also runs through the hooked code.
  3. One frame runs, so the slot holds the twin.
  4. Call `tracker.current()`.
- Expected: AC-12.3, "two answers: write nothing, ask". The player's object still verifies, so the tracker keeps it or pauses and asks.
- Actual: `current()` returns the twin (`0x20009000`), and the freeze now writes the other object without asking.
- Suspected cause: `refind.Tracker.current()`. The BUG-02 fix moves to `_slot_elsewhere()` whenever the slot names a different verifying object, even though the old one still verifies.
- Suggested fix:
  - When both the old and the slot's object verify, treat it as a disagreement: set `pending` and ask, as for weak objects.
  - Or move only after the old object stops verifying, or after N slot reads in a row show only the new one. The pending-kill object is never handled again by the game code, while a twin keeps appearing alternately.
- Impact for the product owner: in a game with two characters of the same kind, "infinite health" can jump to the other character.

#### BUG-11: A value found by a memory path is never followed after a move; the freeze pauses for good with no message (regression from the BUG-07 fix)
- Severity: Medium. Priority: P2.
- Automated test: `::test_r03_chain_found_value_is_followed_after_a_move`
- Steps to reproduce:
  1. Use a short-chain game. Find it next time with a memory path.
  2. Restart and connect. The value is found by the memory path and no hook is put in.
  3. Tick the frozen cheat.
  4. Change biome: the object moves and the path still leads to the old place.
- Expected: AC-12.5, re-found within 5 s (the hook is available in the notes), or at least a message that the freeze stopped.
- Actual: `freezer.paused` stays set. The tracker stays at the old address. No hook is installed (`by_chain` blocks `before_on`). The memory search isn't used while tracking, and the status line still says "found again".
- Suspected cause:
  - `FindAgain.start` / `before_on` skip the hook for `by_chain` objects.
  - `Tracker.current()` re-finds only with `use_scan=False` and an empty slot.
- Suggested fix: when a `by_chain` object's tracker fails to re-find, drop it from `by_chain`, install the saved hook and retry. If nothing works, show "health was lost, press Find it again".
- Impact for the product owner: on a simple game, the freeze can quietly stop after a level change.

#### BUG-12: Notes migrated from format 1 keep the old 10,000 cap
- Severity: Low. Priority: P3.
- Automated test: `::test_r04_format1_notes_get_the_class_range_on_migration`
- Steps to reproduce:
  1. Use a format-1 file whose field has `range [0, 10000]` and a known vtable.
  2. Health is now 25,000.
  3. Connect.
- Expected: the class-checked range from the BUG-08 fix applies.
- Actual: not found; `trackers` is empty.
- Suspected cause: `Project.from_json` migration (ver < 2) drops hooks but leaves the stored `range`. Suggested fix: when `vtable` is set during migration, widen the range with `class_range`.
- Impact for the product owner: users upgrading from the previous build can still lose money or counter values above 10,000 until they press Find it next time again.

### Automated tests added in this round

| File | What it covers |
|---|---|
| `tests/qa/conftest.py` | Shares the `console` and `win` fixtures. |
| `tests/qa/test_find_again_retest.py` | 7 tests: r01-r04 are strict xfail for BUG-10, BUG-01 (reopened), BUG-11 and BUG-12; r05-r07 are passing regression checks. |

### Console-only (unchanged mapping)

| Bug | C10 test | What to check on the console |
|---|---|---|
| BUG-10 | **C10f** | A second same-class character, if the game has one. |
| BUG-11 | **C10f / AC-14.2** | A simple game across a level change. |
| BUG-02 fix | **C10f** | How long UE4 keeps the old pawn alive, and that the freeze moves within 5 s. |
| Lost-link known limit | **C10c** | If the link drops and the app is closed, the hook stays until the game restarts. |

### Recommendation (re-test)

⚠️ BUG-03..09 are closed. Fix BUG-10 (High) and the BUG-01 remainder before C10f/C10c; BUG-11 can come in the same round. After that, run C10.

### BUGS FOR FEATURE-IMPLEMENTOR (re-test)

1. **BUG-10 (High)**: the Tracker follows the slot to another same-class object while the old one still verifies.
   - Make pass: `tests/qa/test_find_again_retest.py::test_r01…`, while keeping `test_find_again_qa.py::test_tc14…` green.
   - Fix: both verify → pending/ask, or require that the old object stops being seen.
2. **BUG-01 (reopened, High)**: Refresh while the home screen is in front, then close → the hook stays.
   - Make pass: `::test_r02…`
   - Fix: in `on_game`, don't clear `installed` when the pid is unchanged, or remove our hooks before switching game.
3. **BUG-11 (Medium)**: a chain-found object is never re-found after a move, and the freeze is paused with no message.
   - Make pass: `::test_r03…`
4. **BUG-12 (Low)**: format-1 migration keeps `range [0, 10000]`.
   - Make pass: `::test_r04…`

---

## 9. Re-test 2 (497a988..d4f9702), 2026-11-02

**Build:** `d4f9702`. Full suite: **510 passed, 24 skipped, 1 xfailed**. `ruff check .` is clean.

The fix commits only removed the xfail markers from r01-r04 (`git diff 6d9d6d5.. -- tests/qa`), so those tests still exercise the bugs. r01 now passes because `current()` returns None (paused and asking), not the twin.

| Bug | Status | Notes |
|---|---|---|
| BUG-01 (rest) | Closed | `test_r02` passes. Our hooks are forgotten only when the pid changes. When the pid changes, the old process is gone, so that path is accepted. |
| BUG-10 | Closed | `test_r01` passes. When both objects verify, the tracker pauses and asks. If the player says No, the first object is kept and the other one is ignored: no repeated asks (`test_r08`, passes). |
| BUG-11 | Closed | `test_r03` passes. `Tracker.on_lost` → `path_lost` installs the hook, and the next ticks find the new place. |
| BUG-12 | Closed | `test_r04` passes. Migration widens the range for class-checked fields. |

### BUG-13 (new): An unanswered question blocks the AC-12.5 auto-resume after a UE4 move
- Severity: Medium. Priority: P2. This is a side effect of the BUG-10 fix.
- Automated test: `tests/qa/test_find_again_retest.py::test_r09_ue4_move_resumes_by_itself_once_the_old_object_is_gone`
- Steps to reproduce:
  1. Freeze health.
  2. The pawn moves, and the old pawn is still alive (pending kill). Both verify, so the tracker pauses and asks. This step is correct.
  3. The garbage collector frees the old pawn, so now only the new one verifies.
  4. Nobody answers yet: the player is playing.
- Expected: AC-12.5, "resumes the freeze without Kein's action". Once only one answer is left, follow it.
- Actual: `current()` stays None until the dialog is answered, because `if self.pending is not None: return None` runs before anything else.
- Suggested fix: when `pending_both` is set and the old address stops verifying while the pending one still does, accept the pending one automatically and close or ignore the dialog.
- Impact for the product owner: after a new biome in Returnal, the freeze can stay paused until he looks at the PC and answers.

### Risks (no bug filed)
- `path_lost` runs `ah.install` (console writes) on the freezer thread while it holds `Tracker._lock`. This works on FakeConsole. On the console, check in **C10f** that no stall or socket clash occurs when the GUI also talks to the PS5 at that moment.
- `Tracker.ignored` is never cleared. If the allocator later reuses an ignored address for the real new pawn, the slot path won't offer it while the old object still verifies. The re-find after the old one dies still works.

---

## 10. Final re-test (bbdd0f6), 2026-11-02

**Build:** `bbdd0f6`. Full suite: **519 passed, 24 skipped, 0 xfailed** (checked over 2 full runs). `ruff check .` is clean.

The fix commit only removed the xfail marker from `test_r09`, so it still exercises the bug.

- **BUG-13: closed.** `test_r09` passes. While a question is open, it is re-checked every tick. Once the old pawn is freed and only the new one verifies, the tracker follows the new one by itself (AC-12.5).

**Regression probes, all pass:**
- `test_r10`: an object with no class check (field filter only) is **never** auto-followed. When the old object dies, the new place stays pending until the player answers.
- `test_r11`: a late answer (No, after the question already answered itself) is ignored. The freeze stays on the new object and the cheat stays on.
- `test_r12`: the real `QMessageBox` closes by itself within 250 ms once the question no longer holds. The test is guarded so a regression can't hang the suite.
- `test_r13`: `path_lost` called from 8 threads at once installs the hook exactly once.
- `test_r08`: no repeated asks after No.

A first draft of `test_r12` left a `singleShot` timer that fired during a later test (`test_ps5debug_ng::test_next_scan_rescans…`) and made it fail. That was my test's fault, not a product bug; it is fixed with a timer owned by the window.

### All bugs

| Bug | Status |
|---|---|
| BUG-01..13 | Closed |

No bugs are open. `tests/qa` holds 59 + 120 emulated + 17 re-test cases, all green.

### Release readiness for console testing: ✅ Ready for C10

Every FakeConsole-testable acceptance criterion (AC-11.1-11.3, AC-12.1-12.5, AC-13.1-13.2, AC-14.3) passes. The capture and keep routines are proven register-, flag- and stack-safe on a real x86 emulator, at the PS5 low module base and above 4 GiB (PS4).

These items can only be settled on hardware:

| Item | Console test |
|---|---|
| NG register layout and whether a per-frame reader shows up | C10a |
| The slot really stays unused by the game | C10b |
| Real 10 s timing and whether the 5-byte jump write can tear | C10c |
| CheatRunner's own patch on/off order | C10d |
| Memory search speed (≤ 3 min) | C10e |
| UE4 GC timing, the ask/auto-resolve flow, and the freezer-thread hook install not clashing with GUI traffic | C10f |
| Real PS4 module base | C10g |
| The full proof on Returnal (health + Obolites, 3 restarts, 2 biomes, 1 death) | C10 / AC-14.1 |

Known accepted limit: if the link drops and the app is then closed without reconnecting, the hook stays in the game until the game restarts.

**Not ready for release** until C10 passes.
