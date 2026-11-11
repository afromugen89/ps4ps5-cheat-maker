# pr-reviewer findings: smart cheat builder (builder-wip @ 8fa17a0)

Verdict: Request changes.

1. [Blocker] Cancel/Disconnect while a fix is being written leaves a jump into zeroed memory (builder.py ~756-773, 928-946): _install_and_test publishes _installed, writes routine then jump without the lock; a concurrent rollback restores and zeroes, then the worker writes the jump. Fix: serialise install and rollback (install lock held across writes, rollback waits; or UI only sets cancel and waits for the worker); re-check _taken_back after the writes and put back if taken.
2. [Blocker] Cancel/Disconnect during finalize leaves a jump into zeroed memory while the cheat says on (builder.py ~968-1001). Fix: same serialisation; if taken mid-finalize, abort with put-back, never return a cheat.
3. [Blocker] UI says "Your game is unchanged" after a failed put-back (build_dialog.py ~361-370, 390-400, 424-433; BUILD_CANCELLED, BUILD_NONE_WORKED). _tried only checks LOST; CANCELLED always "unchanged"; reject/_failed ignore close(); try_candidate finally drops rollback result on exception; _taken_back returns RESTORED blindly. Fix: sticky Builder.not_restored, stop trying after NOT_RESTORED, show RESTART hint, check close() results, share the other thread's rollback result.
4. [Major] Always full uses the value from dialog open, not Start, and the wrong member after Check which is real (build_dialog.py ~134, 155-163, 234-236). Fix: full=None unless the player edited the field (textEdited) so Builder reads it after real_check, just before the watch.
5. [Major] S3 misses flags the writer doesn't change: `dec [m] ; jbe` reads CF from before; flags_fix's cmp forces CF=0. Fix: when flags_fix, second analysis with pending = all six status flags; skip if CF/PF/AF read; add matrix case dec [m]; jbe with CF=1.
6. [Major] Game merely out of front (PS button) is marked crashed forever (builder.py ~1107-1109, 900-918). Fix: gone only when the pid is missing (processes()); not in front -> put back without crash mark; assert in test.
7. [Major] B7 not built; TODO comment claims the dialog offers Lock it (it doesn't). Fix: record the cut in the spec "Not in this version", fix the comment.
8. [Major] Only me filter applied to every hooked writer though learned from one writer's base (builder.py ~590-618, 718). Fix: only allow Only me hooks on writers whose writer_target(w).disp equals the collected writer's; skip others with a reason.
9. [Major] Collect's capture hook outside the rollback record and crash note (builder.py ~596, cave.collect_objects). Fix: _close_builder sets cancel and before_disconnect waits for the step; or route the collect hook through _installed and the note.
10. [Minor] check_on_connect zeroes rooms even when a site holds bytes other than ours (build_notes.py ~201-212). Fix: update the note before re-aiming the jump; only zero rooms if every site reads orig.
11. [Minor] live.turn_on failing after finalize leaves an unrecorded live patch (builder.py ~995-1003). Fix: mark the cheat on/active without rewriting the bytes.
12. [Minor] Data slot not proven unused (find_slot). Fix: re-read slot before install and after the put-back wait; refuse if it changed unexplained.
13. [Minor] push qword [obj+disp] reads 8 bytes for a 4-byte value (page-end fault). Fix: read 4 bytes via scratch without changing flags.
14. [Minor] Never goes down skips NOP fallback when the hook fails at prepare (builder.py ~167-172). Fix: run _check_effect_writer and hook_span in classify.
15. [Minor] Failed/cancelled build leaves the source cheat switched off (build_dialog.py ~282-289). Fix: re-enable it on failure.
16. [Minor] Always full on Int64 stores two dwords (torn). Fix: mov rax, imm64 ; mov [m], rax.
17. [Nit] minicpu: addss double rounding, FTZ/DAZ not modelled (note in docstring). rollback sleeps 2 s on the UI thread from reject().
