# QA report: the window freezing during long PS5 searches (scan link)

- Date: 2026-11-21
- Branch / commit under test: `claude/project-thread-ayylgc` (PR #36) @ `f4ecc0a`. The production code is the same as in `226c6ad`; `f4ecc0a` only changes a test.
- Design: docs/architecture-scan-link.md. Notes: docs/implementation-notes.md ("Searches on their own connection" and the PR #36 review fixes). Console tests: C24 in docs/console-tests.md.
- Code: console/ps5debug_ng.py (scan link, `_locked`, `batch`, cancel and discard), core/freezer.py (turn_off, the freezer hold), core/scanner.py (ScanLinkLost), the `ConsoleBusy` re-raises in core/*, ui/find_wizard.py, ui/find_again.py, ui/cheats_view.py, ui/watchdog.py, logs.py (crash recorder), app.py
- Environment: Linux 6.18, Python 3.13.18, PySide6 6.12.0, Qt offscreen. Uses the real `Ps5DebugNgClient` against `FakeNgServer` (with gated snapshot, narrow-down, value scan and auth), and `FakeConsole` for UI flows.
- Automated tests: `tests/qa/test_search_freeze_qa.py` (first round: 13 pass, 8 strict xfail; re-test: 23 pass, 1 strict xfail; final re-test: 25 pass, 1 strict xfail)
- Full suite: baseline 2421 passed, 3 skipped. First round: 2434 passed, 3 skipped, 8 xfailed. Re-test on 16b8499: 2450 passed, 3 skipped, 1 xfailed. **Final re-test on 35b6113: 2460 passed, 3 skipped, 1 xfailed**

> **Final re-test on `35b6113`:** BUG-09 is fixed and the Start over flow works. One new Low bug was found (BUG-10). Final recommendation: **Ready with known issues** (one Low). See "Final re-test of 35b6113" at the end.
>
> **Re-test 2026-11-21 on `16b8499`:** all 8 bugs are fixed and verified. One new bug was found in the fix (BUG-09, Medium). See "Re-test of 16b8499" at the end.

## Product owner summary

**What I tested:**
- long "I don't know the number" searches, one type and two types, and narrowing down, all on the second ("scan link") connection
- pointer passes
- reads, writes and the freezer while those run
- the "PS5 is busy" limit on the window
- turning cheats off, deleting them, editing their values and Lock it tests while the PS5 is busy
- Start over and Cancel, disconnect and Connect again, Refresh after a game restart
- the search connection dropping, and the connection count against the PS5's 12-slot limit
- the crash recorder: crash.log, the "last run ended unexpectedly" check and the freeze watchdog

**Verdict:** the main fix works.
- The window and frozen cheats keep working while a gated "unknown" snapshot or narrow-down runs. Reads and writes on the main connection answer in well under 0.5 s.
- No search command ever goes over the main connection.
- A disconnect during a search ends it at once and closes every socket.
- A dropped search connection doesn't drop the session.
- The connection count stays within the PS5's slots.

The weak spot is the new **"PS5 is busy"** answer. A few screens don't expect it, and two of them can leave a cheat running in the game that the player can no longer see or turn off.

**Bugs:** 0 Critical, 2 High, 2 Medium, 4 Low.

- BUG-01 (High): if you delete a cheat that is on while the PS5 is busy, its row disappears, but the cheat stays frozen or patched in the game. You can't turn it off any more.
- BUG-02 (High): in Lock it and Player only, the test patch ("Test it") is forgotten when taking it out fails because the PS5 is busy. The NOP stays in the game's code.
- BUG-03 (Medium): if you type a new value into a cheat that is on while the PS5 is busy, the table shows the new value, but the game stays frozen at the old one.
- BUG-04 (Medium): Start over is greyed out while a search runs. Console test C24b and the PR description ("Start over cancels a running search at once") can't be carried out. Only Cancel works.
- BUG-05 (Low): if a run writes more than 256 KB of log and then crashes, it is reported as a clean exit. Its crash.log dump is then ignored too.
- BUG-06 (Low): after the PC sleeps during a window freeze, the watchdog logs "ui_unfrozen" although the window is still frozen.
- BUG-07 (Low): after the search connection drops while idle, the next new search fails once with "The PS5 dropped the search". Pressing Search again works.
- BUG-08 (Low): a race. A Start over held back during a pointer pass can wipe a search started at the moment the pass ends.

**Release recommendation (first round): Ready with known issues.** Superseded by the re-test below. The freeze fix itself is sound and much better than before. BUG-01 and BUG-02 should be fixed before release. They only happen while the main connection is busy for more than 1.5 s, which is now rare: the fallback when the scan link can't open, a Lock it attach, or a large read. But when they happen, a cheat is left on in the game.

## Scope

In scope:
- The scan link: lazy open, falling back to the main connection, forwarding, `scan_counts` without a lock, a link drop becoming ScanLinkLost, close and reconnect.
- Cancelling and discarding a link.
- The pool cap.
- The UI-thread wait limit (ConsoleBusy, BUSY_HOLD).
- Busy is never treated as gone.
- turn_off writes before marking the cheat off.
- The freezer hold (`batch`).
- The live list not asking for matches during a task.
- The crash recorder: crash.log with its start and exit markers, the previous-run check, the Qt message handler and the watchdog.

Out of scope:
- The real PS5: timings, NG's behaviour when a socket closes during a snapshot, real slot accounting. These are the console tests in C24.
- Windows-only faulthandler behaviour.

Assumptions:
- The UI thread is Python's main thread (true in the app and in pytest).
- A FakeConsole `batch()` that raises ConsoleBusy reproduces what the real client does on the UI thread while busy. `_locked` raises ConsoleBusy before any write.

## Test plan and results

| TC-ID | Title | Priority | Result | Notes |
|---|---|---|---|---|
| TC-01 | Reads, writes and version on main stay fast during a 2-type snapshot and the narrow-down after it | High | Pass | `test_tc01_...`; no TURBOSCAN_* on main |
| TC-02 | Freezer keeps a value during a snapshot (UI level) | High | Pass | existing `test_window_and_freezer_keep_working_during_a_snapshot` |
| TC-03 | Freezer writes during a pointer pass | High | Pass | existing `test_frozen_value_is_written_during_a_pointer_pass` |
| TC-04 | UI thread gets ConsoleBusy within about 1.5 s; other threads wait | High | Pass | existing tests 8 and `test_busy_ui_thread_gives_up_at_once_for_a_moment` |
| TC-05 | Connection count with 8 scan connections, an exact search and then a 2-type unknown search | High | Pass | peak 10 open, at most 11 allowed (+1 Cancel) |
| TC-06 | Disconnect during a snapshot ends the search at once and closes every socket | High | Pass | `test_tc06_...` |
| TC-07 | ConsoleBusy message is the busy text, never "lost" | Medium | Pass | |
| TC-08 | Unticking a frozen cheat while busy keeps it on and ticked; the next try works | High | Pass | |
| TC-09 | Freezer thread waits through a busy main lock (more than 1.5 s) and is never turned off | High | Pass | |
| TC-10 | Refresh after a game restart during a first search answers at once; the old search is not kept | Medium | Pass | see risk R3 |
| TC-11 | Deleting an ON cheat while busy | High | **Fail** | BUG-01 |
| TC-12 | Lock it / Player only "untest" while busy | High | **Fail** | BUG-02 |
| TC-13 | Editing an ON cheat's value while busy | Medium | **Fail** | BUG-03 |
| TC-14 | Start over during a running search (C24b) | Medium | **Fail** | BUG-04 |
| TC-15 | Cancel during a snapshot / close during a snapshot returns at once | High | Pass | existing tests 4 and 7 |
| TC-16 | Link drop in a first or next search: session stays, message, new link next time | High | Pass | existing tests 5 |
| TC-17 | Main reconnect during a search leaves the link alone | Medium | Pass | existing test 6 |
| TC-18 | close() while the link opens | Medium | Pass | existing |
| TC-19 | New search after the idle link dropped | Low | **Fail** | BUG-07 |
| TC-20 | Crash recorder: a clean previous run is not reported | High | Pass | |
| TC-21 | A short crashed run is reported | High | Pass | |
| TC-22 | Handled dumps before the exit marker are ignored | Medium | Pass | |
| TC-23 | A crash without an exit marker counts only when the run ended unexpectedly | Medium | Pass | |
| TC-24 | A teardown crash after the exit marker is reported | Medium | Pass | |
| TC-25 | Watchdog: one freeze report and one recovery | Medium | Pass | |
| TC-26 | A long crashed run (more than 256 KB of log) is reported | Medium | **Fail** | BUG-05 |
| TC-27 | Watchdog with the PC asleep during a freeze | Low | **Fail** | BUG-06 |
| TC-28 | A held-back Start over after a pointer pass vs a new search (race) | Low | **Fail** | BUG-08 |
| TC-29 | Existing suite regression | High | Pass | 2434 passed, 3 skipped, 8 xfailed |

## Bugs

### BUG-01: Deleting an ON cheat while the PS5 is busy removes the row but leaves the cheat running
- Severity: High
- Priority: P1
- Related test case: TC-11 | Automated test: `tests/qa/test_search_freeze_qa.py::test_bug01_delete_while_busy_does_not_leave_an_invisible_frozen_cheat`
- Environment: f4ecc0a, Linux, Python 3.13.18, PySide6 6.12.0 offscreen
- Steps to reproduce:
  1. Connect. Add a frozen Health cheat (999) and tick it.
  2. Make the PS5 busy for the UI thread. On the real client, the main lock must be held for more than 1.5 s: the fallback when the link can't open, a Lock it attach, or a long read. In the test, `fake.batch` raises ConsoleBusy.
  3. Select the row and press Delete.
- Expected result: the busy rule in implementation-notes says busy "changes nothing: the cheat stays on, in the game and in the list". So either the row stays, or the cheat is really turned off.
- Actual result: the row is removed. `cheat.enabled` is still True, it is still in `live.active`, and the freezer still holds its key. The game value stays frozen at 999 and no row exists to untick it. For a code patch, the original bytes are never written back.
- Evidence: `AssertionError: row gone=True, still on=True, frozen={'140094656756176'}`; log `action=toggle reason=ConsoleBusy: The PS5 is busy right now.`
- Suspected cause: `src/cheatmaker/ui/cheats_view.py` `do_delete` (around lines 357-369) reports any `turn_off` error and then removes the cheat anyway. Before this PR, `turn_off` always marked the cheat off first. Now ConsoleBusy deliberately leaves it on, so `do_delete` must keep the row (or stop) on ConsoleBusy.
- Impact for the product owner: a player who deletes a cheat at the wrong moment ends up with a cheat stuck on in the game, and no way to turn it off short of disconnecting.

### BUG-02: Lock it / Player only forget a test patch they couldn't take out (busy)
- Severity: High
- Priority: P1
- Related test case: TC-12 | Automated test: `tests/qa/test_search_freeze_qa.py::test_bug02_lock_wizard_untest_while_busy_keeps_track_of_the_test_patch`
- Environment: as above
- Steps to reproduce:
  1. Open Lock it on a cheat and "Test it" one option. A NOP patch goes in over the writer.
  2. While the PS5 is busy, pick another option, press Test again, or close the dialog. Any of these calls `_untest()`.
- Expected result: busy changes nothing. The wizard must still know its candidate, so it can take it out on the next try or on close.
- Actual result: `turn_off` raises ConsoleBusy and the message is shown, but `_untest` then sets `self.candidate = None`. The NOP stays in the game's code (`90 90`) and is still listed in `live.active`, but nothing refers to it any more. The next Test puts a second patch in.
- Evidence: `assert (not True or None is Cheat(name='Health lock', ... enabled=True ...))`; log `action=lock reason=ConsoleBusy`
- Suspected cause: `src/cheatmaker/ui/lock_wizard.py` `_untest` (around lines 128-134) and `src/cheatmaker/ui/player_only_dialog.py` `_untest` (around lines 88-94) clear `self.candidate` after a failed turn_off.
- Impact for the product owner: a Lock it test can leave a game-code patch in place that the app has forgotten. The game behaves strangely until it is restarted.

### BUG-03: A value typed into an ON cheat while busy is shown but not applied
- Severity: Medium
- Priority: P2
- Related test case: TC-13 | Automated test: `tests/qa/test_search_freeze_qa.py::test_bug03_value_edit_while_busy_does_not_show_a_value_that_is_not_frozen`
- Steps to reproduce:
  1. A frozen Health cheat at 999 is ticked.
  2. While busy, type 500 in its Value cell.
  3. Let the freezer run.
- Expected result: the table shows the value that is held in the game. Either the edit is refused (and the cell goes back to 999), or the freezer takes 500.
- Actual result: the busy message is shown, the cell shows 500, and the game is held at 999. Typing 500 again doesn't fire a change, because the text is the same. The player must type something else first, or untick and tick again.
- Evidence: `AssertionError: table shows 500, the game is held at 999`
- Suspected cause: `src/cheatmaker/ui/cheats_view.py` `_item_changed` COL_VALUE branch calls `c.set_value(...)` before `live.refresh(c)`. On ConsoleBusy the new value is kept on the Cheat, but `turn_on` never re-registered the freezer entry.
- Impact for the product owner: the cheat list says 500 while the game keeps 999, with no hint about why.

### BUG-04: Start over is disabled while a search runs (C24b can't be done)
- Severity: Medium
- Priority: P2
- Related test case: TC-14 | Automated test: `tests/qa/test_search_freeze_qa.py::test_bug04_start_over_can_be_pressed_during_a_running_search`
- Steps to reproduce:
  1. Run an "I don't know the number" search to the end.
  2. Press Went down. The narrow-down is held on the console.
  3. Look at Start over.
- Expected result: C24b says "press Start over while the snapshot runs (also try it during a Went down scan)... The button answers at once". The PR claims Start over cancels a running search at once.
- Actual result: `new_btn` is disabled, because `_update_buttons` sets `ready = self._ready() and not busy`. It is also disabled during a first search (`active` is False). The discard path in `scan_cancel` is only reached through a game change, a disconnect or a class search.
- Suspected cause: `src/cheatmaker/ui/find_wizard.py` `_update_buttons` (around line 427). Alternatively, C24b and the PR text are wrong and should say **Cancel**. The product owner should decide which. If Start over gets enabled, `do_reset` also has to handle the result callback of the cancelled task.
- Impact for the product owner: the console tester can't run C24b as written. A player can only stop a search with Cancel, which on the PS5 waits for the console's next progress tick.

### BUG-05: A crashed run with more than 256 KB of log after its start is reported as a clean exit
- Severity: Low
- Priority: P2
- Related test case: TC-26 | Automated test: `tests/qa/test_search_freeze_qa.py::test_bug05_long_crashed_run_is_still_reported`
- Steps to reproduce: write a log with `action=start ...`, then more than 256 KB of lines and no `action=exit`. Call `previous_run_ended_unexpectedly(path)`.
- Expected result: True. The run has a start and no exit.
- Actual result: False. Only the last `RUN_CHECK_BYTES` (256 KB) are read, and the start line is outside that tail. `install_native_crash_log(ended_unexpectedly=False)` then also ignores the crash.log section, which has no exit marker. So a native crash dump from that run is never reported.
- Suspected cause: `src/cheatmaker/logs.py` `previous_run_ended_unexpectedly`. When the tail has no start line, it should still count the run as crashed if the tail has no `action=exit`. Another fix is a small run-state marker file.
- Impact for the product owner: the long sessions are the ones most likely to hit the freeze or crash, and for those "Copy log" won't say the last run ended unexpectedly.

### BUG-06: Watchdog logs ui_unfrozen after a sleep gap while the window is still frozen
- Severity: Low
- Priority: P3
- Related test case: TC-27 | Automated test: `tests/qa/test_search_freeze_qa.py::test_bug06_watchdog_no_false_unfrozen_after_sleep_gap`
- Steps to reproduce:
  1. Use a fake clock and no beats for 18 s. `ui_frozen` is logged.
  2. Advance the clock by 600 s and call `check()`. `watchdog_skip` is logged.
  3. Advance by 2 s and call `check()`.
- Expected result: no `ui_unfrozen`, because the UI never beat again.
- Actual result: `action=ui_unfrozen seconds=618`.
- Suspected cause: `src/cheatmaker/ui/watchdog.py` `check()`. The sleep-gap branch sets `_heartbeat = max(_heartbeat, now)`, and the next check reads that as a real beat that is newer than `_frozen_since`.
- Impact for the product owner: a misleading log line when a freeze is being diagnosed after the PC slept.

### BUG-07: A new search fails once after the idle scan link was dropped
- Severity: Low
- Priority: P3
- Related test case: TC-19 | Automated test: `tests/qa/test_search_freeze_qa.py::test_bug07_new_search_after_the_idle_link_dropped_opens_a_new_link`
- Steps to reproduce:
  1. Run a search. The link opens and stays idle.
  2. The PS5 drops the link.
  3. Press Search to start a new search.
- Expected result: the new search opens a fresh link and works.
- Actual result: `ScanLinkLost: the search connection dropped: the PS5 closed the connection after TURBOSCAN_START`. The player sees "The PS5 dropped the search. Press Search to start it again." The second try works.
- Suspected cause: `src/cheatmaker/console/ps5debug_ng.py` `_run_search` / `scan_start`. A dead idle link is only noticed when the new search uses it. `scan_start` (the `open_link=True` path) could retry once on a new link when the link fails before any of the new search's data has come back.
- Impact for the product owner: a confusing one-off error on a search that never really started.

### BUG-08: A held-back Start over after a pointer pass can wipe a search started at that moment
- Severity: Low
- Priority: P3
- Related test case: TC-28 | Automated test: `tests/qa/test_search_freeze_qa.py::test_bug08_deferred_cancel_after_a_pointer_pass_does_not_wipe_a_new_search`
- Steps to reproduce:
  1. Keep an unknown search.
  2. Start a pointer pass (gated).
  3. Call `scan_cancel()`. It is held back.
  4. Release the pass. While its catch-up `_cancel_here()` runs (widened in the test), start an exact search on another thread.
- Expected result: the new search waits for the cancel (`_cancelling`), as it does for a direct cancel, and then keeps its 2 matches.
- Actual result: `scan_start` returns 2, then the catch-up wipes it: `scan_counts() == {}` and `scan_results() == []`.
- Suspected cause: `src/cheatmaker/console/ps5debug_ng.py` `_end_job`. It decrements `_jobs`, then calls `link._cancel_here()` without setting `link._cancelling` (or counting a job), so `_run_search` lets a new call in. The catch-up in `_run_search` (`catch_up` for a SEARCH) has the same pattern but runs inside the counted job, so it is safe.
- Impact for the product owner: rare. A search that just finished can show no matches. It needs a game change or class search during a pointer pass.

## Automated tests added

`tests/qa/test_search_freeze_qa.py` (13 pass, 8 strict xfail):

- TC-01: main connection free during a 2-type snapshot and the narrow-down
- TC-05: connection-count peak at most 11
- TC-06: disconnect during a snapshot in the UI
- TC-07: busy message
- TC-08: untick while busy
- TC-09: the freezer thread rides out a busy lock
- TC-10: Refresh after a game restart during a first search
- TC-20 to TC-24: crash.log and previous-run checks
- TC-25: watchdog freeze and recovery
- BUG-01 to BUG-08: strict xfail tests

Full run: `QT_QPA_PLATFORM=offscreen pytest -q -p no:cacheprovider` gave **2434 passed, 3 skipped, 8 xfailed**. `ruff check` and `ruff format --check` are clean on the new file.

## Not tested / risks

- R1. **Real NG slot accounting after a discard.** `_discard` closes the link's sockets, but NG's thread for that client may stay inside the snapshot for up to 156 s before it sees the close. Two quick discards followed by a new search could go over 12 slots. The client then degrades: fewer pool connections, or the link falls back to main, and the window hangs as before. C24b/C24c should note the `scan_pool` / `scan_link` warnings after several restarts in a row. Not testable on the fake.
- R2. **Disconnect or close while main is busy.** `run_before_disconnect` runs on the UI thread. ConsoleBusy there is only reported, so a found-again hook stays in the game (it remains recorded and is retried on the next close). This was not a hang before, and it is rare now that searches don't hold main. Documented, not filed.
- R3. **A game change during a first search** doesn't call `scan_cancel`, because `Scanner.reset` only cancels when `active`. On the fake, the old process disappears and the snapshot fails, so no stale search is kept (TC-10 passes). If a real NG snapshot of a just-exited process still completes, the old run's matches would become the new run's search. Worth a look in C24.
- R4. **Cancel button during a snapshot** only reacts on the next progress word from the console (unchanged behaviour). The fake's gated snapshot sends none, so this was not timed.
- R5. Windows faulthandler "handled exception" dumps and Qt message volume: only covered by unit tests, not tested on Windows.
- R6. Class search (Find it again) during a player's search: they share the link. The "taken" semantics are the same as before; there is no new defect, but the timing changed.

## Re-test of 16b8499 (2026-11-21)

Commit `16b8499` ("Fix QA BUG-01..08 and re-review leftovers") was re-tested.
- The implementor only removed the xfail markers from my tests. The test bodies are unchanged, so each test now checks its bug's fix exactly as it was written.
- The new production code was also reviewed:
  - `busy.py` `held()` on the Tracker locks
  - `_end_pending` / `_send_end`
  - `search_running()`
  - `take_out(raise_busy=True)`
  - the BUG-07 retry (`ScanLinkLost.unanswered`)
  - the BUG-05 whole-file read
  - the watchdog `_woke_at`
  - Start over during a search (`_scan_gen`, `_late_scan`, `Scanner.stop`)

### Fix verification

| Bug | Fix | Verified by | Result |
|---|---|---|---|
| BUG-01 | `do_delete` keeps the row on ConsoleBusy | `test_bug01_...` | Fixed |
| BUG-02 | `_untest` returns False on busy and keeps `candidate`. Test, Test all, Player only and close stop there. | `test_bug02_...` | Fixed. The dialog can't be closed while the PS5 stays busy; the busy message explains why. |
| BUG-03 | the value bytes are rolled back on ConsoleBusy | `test_bug03_...` | Fixed |
| BUG-04 | Start over is enabled during a running Find search, but not during Check which is real. It cancels, calls `scan_cancel` (discarding the link) and drops the late answer (`_scan_gen`). | `test_bug04_...`, RT-01, RT-02 | Fixed |
| BUG-05 | the whole file is read when the tail has neither marker | `test_bug05_...` | Fixed |
| BUG-06 | after a sleep, only the wake time moves; the heartbeat is untouched | `test_bug06_...`, TC-25 | Fixed |
| BUG-07 | one retry on a new link when nothing came back on the dead one | `test_bug07_...`; the existing mid-search drop tests still get ScanLinkLost with no retry | Fixed |
| BUG-08 | the catch-up cancel in `_end_job` sets `_cancelling` | `test_bug08_...` | Fixed |

### Regression tests added (re-test)

| TC-ID | Title | Priority | Result |
|---|---|---|---|
| RT-01 | Start over during a running first unknown search: the window answers in under 0.5 s, no error message, the scanner is not active; the next search keeps its 2 matches | High | Pass |
| RT-02 | Start over is greyed out while Check which is real runs, and enabled again after it | Medium | Pass |
| RT-03 | Live list during a pointer pass on the scan link | Medium | **Fail: BUG-09** |
| RT-04 | Full suite, and every earlier TC | High | Pass (2450 passed, 3 skipped, 1 xfailed) |

### BUG-09: The live list waits on the scan link during a pointer pass (window hangs about 1.5 s per tick)
- Severity: Medium
- Priority: P2
- Related test case: RT-03 | Automated test: `tests/qa/test_search_freeze_qa.py::test_bug09_live_list_does_not_wait_on_the_link_during_a_pointer_pass`
- Environment: `16b8499`, Linux, Python 3.13.18, PySide6 6.12.0 offscreen
- Steps to reproduce:
  1. Run an unknown search with few matches (FakeNgServer), so the live list shows the matches.
  2. Start a pointer pass on the link and hold its slice with `scan_gate`.
  3. Run one live-list tick (`refresh_live()`).
- Expected result: under 0.5 s. Review MAJOR 3 said the UI thread doesn't ask for matches while the link is busy. Before 16b8499, `tasks.busy` skipped this, and a pointer pass runs on `ctl.tasks`.
- Actual result: `the window waited 1.5 s`. The tick waits `MAIN_THREAD_WAIT` on the link's lock and then gets ConsoleBusy, which is caught, so the last values stay. Later ticks wait 0.1 s during `BUSY_HOLD_S` and then 1.5 s again, so the window stutters for as long as the pointer pass runs.
- Suspected cause: `ui/find_wizard.py` `refresh_live` now uses `client.search_running()`. `ps5debug_ng.py` `search_running()` only counts `_jobs[SEARCH]`. A pointer pass is a `POINTERS` job.
  - By code reading, the same applies when no link could be opened (`_link is None`, so the search runs on main and `search_running()` returns False). Each tick then waits 1.5 s behind the search on the main connection. The old `tasks.busy` check covered that case too.
  - Possible fix: count any job on the link, plus a search kept on main.
- Impact for the product owner: while "Find a memory path" runs, the window briefly freezes again every couple of seconds if the Find view is showing live matches.

### Observations (not filed)
- **R7.** `Tracker.accept_pending` / `reject_pending` now give up after 1.5 s on the UI thread. If that happens after the player answers the "it moved" question, `check_pending` catches ConsoleBusy without showing a message, and the same question is asked again on the next tick. This is rare: the Tracker lock is only held long while main is busy. It is acceptable "try again" behaviour, but a short busy message would be clearer.
- **R8.** `_send_end` suppresses ConnectionLost, which includes a 4 s receive timeout. If a big snapshot takes longer than 4 s to free, the late END status could stay queued ahead of the next answer on that socket. The old `_end_resident` already suppressed every error, so this risk is not new. Worth watching in C24 (`action=snapshot_end`).
- **R9.** BUG-02's fix means Lock it / Player only can't be closed while the PS5 stays busy (fallback mode, a long attach). The busy message is shown each time. That was the intended trade-off.

### Recommendation after the 16b8499 re-test (superseded below)

**Ready with known issues.** All eight first-round bugs are fixed and stay covered by tests, and the two High bugs (cheats left on with no row or reference) are gone. BUG-09 is a Medium performance regression that only shows during pointer passes, or when the link can't be opened. It should be fixed soon, but it doesn't change any game state.

## Final re-test of 35b6113 (2026-11-21)

Commit `35b6113` ("Scan link review round 4") was reviewed with `git diff 72054a2..35b6113`. It changes:
- `search_running()`: now counts every job, and also jobs on the main connection when there is no link
- `_send_end`: now raises its errors
- the Find view's "stopping" gate after Start over, with progress tagged by `_scan_gen`
- the find-again "it moved" answers: kept in `_answers` until applied, with a busy message
- the watchdog's freeze length after a sleep

**Change to my RT-01 test:** right after Start over, Search is now disabled until the stopped search's worker has ended. The test asserts that, then that Search is enabled again and the next search works. It still checks what it was written to check: the window answers at once, no error is shown, and a new search works. I accept the change.

### Results

| ID | Title | Priority | Result |
|---|---|---|---|
| BUG-09 | Live list during a pointer pass | Medium | Fixed: `test_bug09_...` passes and the tick takes under 0.5 s |
| RT-01 | Start over during a running first search: the window answers at once, no error, Search is gated until the stopped search's worker ends, then the next search works | High | Pass |
| RT-02 | Start over is greyed out during Check which is real | Medium | Pass |
| RT-05 | Start over twice, then Connect again: the gate opens again and a search works | Medium | Pass (new) |
| RT-06 | A snapshot END answered after the read timeout, then a new search | Low | **Fail: BUG-10** (new) |
| RT-07 | Every earlier test case and BUG-01 to BUG-08 | High | Pass |
| RT-08 | Full suite | High | Pass: 2460 passed, 3 skipped, 1 xfailed (BUG-10) |

I also fixed my own TC-10. The fake server now hangs up cleanly when the game process is gone, instead of raising in its thread. Five runs in a row produced no unhandled-thread warning.

### BUG-10: A slow snapshot END leaves its answer on the socket, so the next search hangs for 60 s and fails
- Severity: Low
- Priority: P3
- Related test case: RT-06 | Automated test: `tests/qa/test_search_freeze_qa.py::test_bug10_slow_snapshot_end_does_not_desync_the_next_search`
- Environment: `35b6113`, Linux, Python 3.13.18. The test shortens the timeouts: `IO_TIMEOUT` is 0.3 s instead of 4 s, and `SCAN_TIMEOUT` is 2 s instead of 60 s.
- Steps to reproduce:
  1. Keep an unknown search on the link.
  2. The fake answers TURBOSCAN_END 0.6 s late, which is longer than the read timeout.
  3. Call `scan_cancel()`, the same call Start over makes.
  4. Start an exact search.
- Expected result: the new search works (2 matches) in under 1 s. If the END fails, that link must not be used again; then the BUG-07 retry opens a new link.
- Actual result: the late END status is read as the TURBOSCAN_START answer, the stream is out of step, and the search waits `SCAN_TIMEOUT` (60 s on the real client). It then fails with `ScanLinkLost: receive failed after TURBOSCAN_START: timed out`. The BUG-07 retry doesn't apply, because an answer did come back. The search after that works.
- Suspected cause: `ps5debug_ng.py` `_end_resident`. 35b6113 made `_send_end` raise ConnectionLost "so its late answer isn't read as the next command's", but `_end_resident`'s `except Exception` swallows it and keeps the socket. The deferred path (`_locked` → `_send_end`) does raise, and that is fine. Possible fix: on a non-busy failure in `_end_resident`, close or abort that connection (`self._w.close()`, or `_abort()` for a link), so the next call fails at once and is retried.
- Impact for the product owner: only if the PS5 needs more than 4 s to free a big snapshot (not verified on the console). The next search after Start over then hangs for about a minute and fails once.

### Observations
- **R10.** With no scan link (fallback mode), a search on the main connection can't be stopped at once. Start over sets Cancel, and the snapshot notices it at the console's next progress update. Until then, the Start-over gate keeps Search disabled. This is the expected fallback behaviour (logged as `action=scan_link` warnings).
- R7 is resolved: a busy moment after answering the "it moved" question now keeps the answer, applies it on the next tick and shows the busy message.
- R8 is partly resolved: the deferred END now raises, but the direct END path still swallows errors (BUG-10).

### Final recommendation

**Ready with known issues.**
- BUG-01 to BUG-09 are fixed and covered by tests that now pass.
- The only open bug is BUG-10 (Low). It needs a console that takes more than 4 s to answer TURBOSCAN_END. It costs one failed search and loses no game state.
- Before release, run console tests C24a to C24c, and note in them any `action=snapshot_end` or `scan_link_lost` lines.
