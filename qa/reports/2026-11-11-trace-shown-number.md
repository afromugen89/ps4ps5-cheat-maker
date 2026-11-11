# QA report: Find the real one behind it (spec-trace AC-T1 to AC-T12)

- Date: 2026-11-11
- Branch / commit under test: `claude/project-thread-y85u6o` @ `f3a987e`
- Environment: Linux 6.18 x86_64, Python 3.13.18, PySide6 6.12.0 (offscreen), pytest-qt 4.5.0,
  iced-x86 (project pin), unicorn 2.1.4 (cross-checks only)
- Automated tests: `tests/qa/test_trace_qa.py` (50 pass, 14 strict xfail = 12 open bugs)
- Full suite: `1981 passed, 24 skipped, 14 xfailed` (baseline before QA: 1931 passed,
  24 skipped). `ruff check src tests` and `ruff format --check src tests` are clean.

## Product owner summary

I tested the whole feature without a console. That covered the code reader (x86 patterns), the
number maths (the "way"), the hop driver (watch, memory check, write test, hops, limits,
cancel, lost connection) and the Find list and dialog.

The safety rules hold. Across 11 exit paths (Cancel at each of the 5 stages, lost connection
at 4 stages, a refused write, a failed memory-map read):
- nothing is written before the copy has been checked at two different shown values;
- every paused freeze is resumed;
- no test value is left in the game.

Wrong trap registers and mis-decoded code end in "no row", never in a wrong row. Way maths
round-trips on the common ways (x100 cut or round, double, int x0.01, the low half of an
8-byte number) for every screen value from -1000 to 1000. The forward maths agrees with a real
CPU (unicorn).

Bugs: 12 (0 Critical, 0 High, 5 Medium, 7 Low).
- BUG-01 (Medium): a copy written with an atomic store (`xchg`) is reported as "This is
  already the value the game uses". The player is told a screen copy is the real value.
- BUG-02 (Medium): x100 is not followed when the code loads the constant first and then
  multiplies by the value. The player gets "can't follow" instead of a result.
- BUG-04 (Medium): on a value kept as an 8-byte decimal and shown x100 through a 4-byte
  decimal, Set and Make a cheat write a number that shows 1 less than typed for about 6% of
  values. The player is also told "the nearest" without need.
- BUG-07 (Medium): Remove on a search row deletes the traced row at the same address.
- BUG-10 (Medium): a freeze on an 8-byte value that is no longer listed is either not paused
  during a trace, or is put back holding only 4 of its 8 bytes.
- Low:
  - BUG-03: `mov [m],ah` is read as the low byte.
  - BUG-05: a decimal screen copy gets a needless "can't show exactly" notice.
  - BUG-06: the message says "Followed it 1 steps".
  - BUG-08: the search row is mislabelled "(added, ...)".
  - BUG-09: one rare self-copy outvotes the real copier.
  - BUG-11: no trace suggestion when one checked row failed.
  - BUG-12: the exec-breakpoint fallback (off by default) works out a max with the wrong
    registers.

Release recommendation: **Ready with known issues.** Nothing found can write a wrong value
into the game or leave a test value behind. The feature already sits under Advanced "(being
tested)". Fix BUG-01, BUG-02, BUG-04 and BUG-07 before the button leaves Advanced (C20a).

## Scope

In scope:
- `core/trace_way.py`: forward and inverse maths, AC-T5 match rules, labels, `as_scale`.
- `core/trace_flow.py`: window decoding, `follow`, `candidates` (regs, replay, block), caller
  rule, `exec_address`.
- `core/tracer.py`: `watch_span`, hops, memory check, write test, circle, 4-hop limit, time
  limits, release and resume, cancel, lost connection, errors, logs.
- `ui/trace_dialog.py`: result texts, release and resume, cancel and close.
- The trace parts of `ui/find_wizard.py`: traced rows next to search rows, Set and Make
  conversion, Remove, Search for it that way, the Advanced gate.
- `core/guide.py`: `after_check` and `after_put_back`.

Out of scope or not testable here:
- The real console: NG watch timing, register correctness (C10a/C20a), detach, RF on execute
  breakpoints (C20d), PS4 CUSA titles (C20g).
- Unchanged Lock it, Make it work and Check which is real behaviour.

Risk areas (tested first): writes before the check; put-back and resume on every path;
analyser misreads; Way inverse used by Set and Make a cheat; time and hop limits; rows that
share an address.

Assumptions:
- An atomic `xchg` store and a commutative multiply are valid encodings of the copy and x100
  patterns the spec lists.
- "Every checked row copy" (AC-T1) leaves out rows that couldn't be checked (FAILED).

## Test plan and results

| TC-ID | Title | Priority | Result | Notes |
|---|---|---|---|---|
| TC-01 | No write before the second memory check (x100, share of max, 2 hops) | High | Pass | Every write happens in Stage.TEST and only to a checked source |
| TC-01b | A decoy holding the same number once is never written | High | Pass | |
| TC-02 | Release and resume balanced, no test value, on 11 exit paths | High | Pass | Cancel at watch, read, steady, change and test; lost connection at watch, read, steady and test; refused write; maps() lost |
| TC-03 | Two hops: both copies released, resumed last-first | High | Pass | |
| TC-04 | Release raising at hop 2 still resumes hop 1 | High | Pass | Outcome FAILED |
| TC-05 | 8-byte copy at a 4-aligned address: watched (addr, 4), traced | High | Pass | |
| TC-05b | 4-byte decimal at a 2-aligned address: watched (addr, 2), traced | Medium | Pass | |
| TC-05c | Odd-address 4-byte value: can't watch | Low | Pass (design) | See observation O-1 |
| TC-06 | 5 copies deep: TOO_FAR after 4 hops, 4th source "not sure" | High | Pass | |
| TC-06b | Slow player: the trace ends at TOTAL_S | Medium | Pass | See observation O-3 |
| TC-07a | Atomic store `xchg [m],eax` of a copy | High | **Fail** | BUG-01 |
| TC-07b | `movss x0,[K100]; mulss x0,[rbx+10h]` (SSE and VEX) | High | **Fail** | BUG-02 |
| TC-07c | `mov [rdi],ah` lane | Low | **Fail** | BUG-03 |
| TC-07d | Zero-extension by 32-bit mov; partial `mov al`; `movd`; `vmovss` 3-operand lane; `cvttsd2si r64`; `x*x`; constant register reused; `cmov`; callee-saved copy | High | Pass | 9 cases |
| TC-07e | Calls: xmm6 is clobbered, r12 survives | High | Pass | |
| TC-07f | Mis-synced window (200 random prefixes) never gives a wrong source | High | Pass | Some lose the path (safe) |
| TC-08 | RIP constant in the R+X code map counts as constant; a writable global x100 isn't followed | High | Pass | |
| TC-09 | Typed screen numbers round-trip, 7 ways, -1000..1000 | High | Pass | |
| TC-09b | Double narrowed to float, x100 cut, round trip | High | **Fail** | BUG-04 |
| TC-09c | Share of max with max 0, -0.0 or NaN: clean refusal | Medium | Pass | |
| TC-09d | Negative values cut toward zero | Medium | Pass | |
| TC-09e | Decimal screen copy (x1000) settable without a notice | Low | **Fail** | BUG-05 |
| TC-09f | Narrow way forward maths equals unicorn | Medium | Pass | |
| TC-10 | TOO_FAR text after 1 hop | Low | **Fail** | BUG-06 |
| TC-10b | First hit's raw packet logged in hex at debug | Low | Pass | AC-T11 |
| TC-12a | Remove on a search row at a traced address | Medium | **Fail** | BUG-07 |
| TC-12b | Search row at a traced address isn't labelled "added" | Low | **Fail** | BUG-08 |
| TC-12c | Set on a share-of-max row with max 0: message, nothing written | Medium | Pass | |
| TC-12d | TOO_FAR row is added marked "not sure" | Medium | Pass | |
| TC-12e | Whole UI flow: FOUND, no test value left | High | Pass | |
| TC-12f | Cancel during the watch: no row, nothing written | High | Pass | |
| TC-13 | A one-off self copy doesn't outvote the busy copier | Low | **Fail** | BUG-09 |
| TC-14 | An unlisted 8-byte freeze over the copy is paused and put back whole | Medium | **Fail** | BUG-10 |
| TC-15 | Guide: copies plus one unchecked row suggests the trace | Low | **Fail** | BUG-11 |
| TC-15b | Guide: all copies suggests it, a real one doesn't | Medium | Pass | |
| TC-16 | Exec fallback: max address from the max load's registers | Low | **Fail** | BUG-12 (setting off by default) |
| TC-M1 | A group row traces its head, a member that member | Medium | Pass (inspection) | `do_trace` uses `selected()`/`_row_type()` like Set, which has existing UI tests |
| TC-M2 | Advanced gate, busy rule, notice once | High | Pass | Existing `tests/ui/test_trace_ui.py`, re-run |
| TC-M3 | Console tests C20a-i | High | Not run | Need a PS5/PS4 with NG |

## Bugs

Environment for all bugs: `claude/project-thread-y85u6o` @ `f3a987e`, Linux, Python 3.13.18,
FakeConsole + tests/tracegame.py. Reproduce any of them with:

```
QT_QPA_PLATFORM=offscreen /tmp/claude-0/cmvenv/bin/pytest -q -p no:cacheprovider tests/qa/test_trace_qa.py --runxfail -k <test>
```

### BUG-01: an atomic store (`xchg [m],reg`) of a screen copy is taken as read-modify-write: "already the value the game uses"
- Severity: Medium. Priority: P2.
- Related test case: TC-07a.
- Automated tests:
  - `tests/qa/test_trace_qa.py::test_tc07a_xchg_store_of_a_copy_is_not_already_real`
  - `tests/qa/test_trace_qa.py::test_tc07a2_tracer_on_an_xchg_copy_does_not_say_already_real`
- Steps to reproduce:
  1. A HUD copy written by `mov eax,[rbx+10h] ; xchg [rdi],eax`. This is how compilers emit a
     sequentially consistent `std::atomic` store.
  2. Run Find the real one behind it on the copy.
- Expected (AC-T3): only a value read from the watched address means it is already real. Here
  the value comes from `[rbx+10h]`, so the trace should find `[rbx+10h]`.
- Actual: `follow` returns `End.ALREADY_REAL` ("xchg [rdi],eax at 0x400103"). The trace ends
  `Outcome.ALREADY_REAL` with "read-modify-write: it's the real one", and no row is added.
- Suspected cause: `src/cheatmaker/core/trace_flow.py:395`. Any memory operand with
  READ_WRITE access counts as read-modify-write. `xchg` reads the old value only to return it
  in the register; the stored value is the register.
- Impact: the player is told a screen copy is the game's real value, then freezes it and
  nothing happens.

### BUG-02: x100 isn't followed when the constant is loaded first and the value is the memory operand
- Severity: Medium. Priority: P2.
- Related test case: TC-07b.
- Automated test: `tests/qa/test_trace_qa.py::test_tc07b_constant_first_multiply_is_followed[sse|vex]`
- Steps to reproduce: run `follow` on `movss xmm0,[rip+K100] ; mulss xmm0,[rbx+10h] ;
  cvttss2si eax,xmm0 ; mov [rdi],eax`. Also on the VEX form `vmovss xmm1,[rip+K100] ;
  vmulss xmm0,xmm1,[rbx+10h] ; ...`. K100 is read-only.
- Expected (AC-T3): multiply by a constant read from a RIP-relative address is followed.
  Multiplication commutes, so the source is `[rbx+10h]` with factor 100.
- Actual: `NOT_FOLLOWED`, "two loaded values multiplied: mulss xmm0,[rbx+10h]". The trace
  answers "The game works this number out in a way the app can't follow yet."
- Suspected cause: `src/cheatmaker/core/trace_flow.py:643`. `_muldiv` tries the other order
  only for the 3-operand VEX register form. When the memory operand is not a constant and the
  tracked register's own last write is a constant load, it should swap.
- Impact: a common way of showing a value x100 gets no result.

### BUG-03: a store of `ah` (byte 1 of rax) is followed as the low byte
- Severity: Low. Priority: P3.
- Related test case: TC-07c.
- Automated test: `tests/qa/test_trace_qa.py::test_tc07c_store_of_ah_is_byte_1_of_the_register`
- Steps to reproduce: run `follow` on `mov eax,[rbx+10h] ; mov [rdi],ah` with shown INT8.
- Expected: source byte 1 of `[rbx+10h]` (lane 1), or not followed.
- Actual: SOURCE `[rbx+10h]` lane 0, way "INT32 -> low 1 bytes -> INT8".
- Suspected cause: `src/cheatmaker/core/trace_flow.py:419`. `full_name(src)` drops the
  high-byte offset of ah/bh/ch/dh.
- Impact: the memory check rejects it, so the player gets "no row" instead of the value.
  Advanced shows a wrong path.

### BUG-04: a double narrowed to float, then x100 and cut, can't be Set exactly
- Severity: Medium. Priority: P2.
- Related test cases: TC-09b, TC-09f (the forward maths is right).
- Automated test: `tests/qa/test_trace_qa.py::test_tc09b_double_narrowed_to_float_round_trips`
- Steps to reproduce: `Way(DOUBLE, INT32, (NARROW->FLOAT, MUL 100, TO_WHOLE cut)).real_for(237)`.
- Expected (AC-T7): the real value written shows 237. The value 2.3700001239776611 does, as
  checked on unicorn.
- Actual: returns `(2.37, False)`. 2.37 shows 236 on the CPU (unicorn confirms). 124 of the
  screens from -1000 to 1000, and 260 of the screens from -2000 to 2000, aren't settable. The
  UI then writes the nearest value and says "The game can't show exactly 237: wrote the
  nearest it can, 236."
- Suspected cause: `src/cheatmaker/core/trace_way.py:129-139, 254-259`. `_next` nudges in
  the real type's ULPs (double). After `cvtsd2ss`, 8 double ULPs never change the float, so
  the nudge can't reach the next float32.
- Impact: Set or a cheat made on such a traced row shows one less than the number typed.

### BUG-05: a decimal screen copy gets "can't show exactly" for plain numbers
- Severity: Low. Priority: P3.
- Related test case: TC-09e.
- Automated test: `tests/qa/test_trace_qa.py::test_tc09e_decimal_screen_value_is_settable_without_a_nearest_notice`
- Steps to reproduce: `Way(FLOAT, FLOAT, (MUL 1000,)).real_for(127.0)`.
- Expected: exact, since 127.0000076 is shown as 127.
- Actual: `exact=False`. The UI says "The game can't show exactly 127: wrote the nearest it
  can, 127.00."
- Suspected cause: `src/cheatmaker/core/trace_way.py:227` (`_same_shown`). For a decimal
  shown type, exact is decided by identical bytes. It should use the AC-T5 decimal tolerance,
  or the screen's digits.
- Impact: a confusing notice after a Set that worked.

### BUG-06: "Followed it 1 steps without reaching the real value."
- Severity: Low. Priority: P3.
- Related test case: TC-10.
- Automated test: `tests/qa/test_trace_qa.py::test_tc10_too_far_text_after_one_hop_is_grammatical`
- Steps to reproduce: a TOO_FAR result with one hop (time ran out at hop 1), shown with
  `result_text`.
- Expected: plain English. The spec text for this outcome is the 4-hops message. When time
  ran out, saying so would be clearer.
- Actual: "Followed it 1 steps without reaching the real value."
- Suspected cause: `src/cheatmaker/ui/trace_dialog.py:76`. `TRACE_TOO_FAR` is formatted with
  `len(r.hops)` for every TOO_FAR reason.
- Impact: cosmetic.

### BUG-07: Remove on a search row deletes the traced row at the same address
- Severity: Medium. Priority: P2.
- Related test case: TC-12a.
- Automated test: `tests/qa/test_trace_qa.py::test_tc12a_remove_is_not_offered_on_the_search_match_row`
- Steps to reproduce:
  1. Search 87 as a 4-byte whole number.
  2. A trace finds a decimal at the same address as one of the matches.
  3. Select the search match row (INT32). Remove is enabled; press it.
- Expected (AC-T7, review note "listed by (address, type)"): Remove isn't offered on a search
  match, and the traced decimal row stays.
- Actual: the traced row disappears (`manual` and `traced` entries deleted); the match stays.
- Suspected cause: `src/cheatmaker/ui/find_wizard.py:458` (enable) and `:1370` (delete).
  Both check `m.address in self.manual`, by address only, not by (address, type).
- Impact: the player loses the value the trace found and has to trace again.

### BUG-08: the search row at a traced address says "(added, Decimal (4 bytes))"
- Severity: Low. Priority: P3.
- Related test case: TC-12b.
- Automated test: `tests/qa/test_trace_qa.py::test_tc12b_search_match_row_doesnt_claim_to_be_the_added_row`
- Actual: the INT32 match row reads `87 (added, Decimal (4 bytes))`.
- Suspected cause: `src/cheatmaker/ui/find_wizard.py:882-883`. The label is added by address,
  not by (address, type). The same by-address keying makes "(frozen)" and Freeze/Unfreeze
  shared between the two rows.
- Impact: two rows claim to be the same added decimal.

### BUG-09: one self-copy hit makes a copy "already real" even though another writer copies it more often
- Severity: Low. Priority: P3.
- Related test case: TC-13.
- Automated test: `tests/qa/test_trace_qa.py::test_tc13_a_rare_self_copy_doesnt_outvote_the_copier`
- Steps to reproduce: the HUD is copied every frame from `[rcx+8]`, and once by a
  `movss xmm0,[rsi] ; movss [rsi],xmm0` self copy.
- Expected: the same rule as read-modify-write writers (review follow-up): decide by hits, or
  by whether another source passes the check. Here: FOUND at `[rcx+8]`.
- Actual: ALREADY_REAL, "read back from the same place: it's the real one".
- Suspected cause: `src/cheatmaker/core/tracer.py:361-363`. Any candidate equal to the
  watched address ends the hop, before hit counts or the check are considered.
- Impact: a rare relayout/memmove routine can make a copy look real.

### BUG-10: a freeze on an 8-byte value that isn't listed is missed, or re-frozen as 4 bytes
- Severity: Medium. Priority: P3.
- Related test case: TC-14.
- Automated test: `tests/qa/test_trace_qa.py::test_tc14_an_unlisted_8_byte_freeze_over_the_copy_is_paused_and_put_back_whole`
- Steps to reproduce:
  1. Freeze an 8-byte row at X. Then Start over or search again, so X isn't in the Find rows
     (the freeze stays on).
  2. Trace a copy at X+4. Or trace X itself.
- Expected (AC-T2): the freeze writing the watched bytes is paused, then resumed exactly as it
  was (8 bytes).
- Actual: watching X+4, the freeze isn't paused at all. Watching X, it is resumed holding 4
  bytes (`freezer._entries["find:X"]` data length 4).
- Suspected cause: `src/cheatmaker/ui/trace_dialog.py:388`, `self.frozen.get(a, 4)`. Rows that
  aren't listed get a 4-byte default for both the overlap test and the bytes re-frozen.
- Impact: after a trace, an 8-byte freeze (a double, a 64-bit count) silently holds only half
  its bytes and stops working.

### BUG-11: Check which is real says "these look like copies" but doesn't suggest the trace when one row couldn't be checked
- Severity: Low. Priority: P3.
- Related test case: TC-15.
- Automated test: `tests/qa/test_trace_qa.py::test_tc15_all_checked_rows_copies_with_one_unchecked_still_suggests_the_trace`
- Actual: `Guide().after_check([COPY, COPY, FAILED])` is None, while `_after_check` shows
  `CHECK_REAL_NONE` for the same mix.
- Suspected cause: `src/cheatmaker/core/guide.py:147`.
- Impact: the player isn't pointed at the feature in a common case (one match unloaded).

### BUG-12: execute-breakpoint fallback works out the max's address with the registers of the source load
- Severity: Low (the setting is off until C20d). Priority: P3.
- Related test case: TC-16.
- Automated test: `tests/qa/test_trace_qa.py::test_tc16_exec_fallback_max_address_uses_the_max_loads_registers`
- Steps to reproduce: `movss xmm0,[rbx+10h] ; mov rbx,[rbx+30h] ; divss xmm0,[rbx+14h] ;
  mulss [K100] ; cvttss2si ; mov [rdi],eax`, with the exec setting on.
- Expected: max = `[obj+30h]+14h`.
- Actual: max = `obj+14h`, so the check fails ("held 87, expected 21.5") and the result is
  SEARCH_THAT_WAY.
- Suspected cause: `src/cheatmaker/core/tracer.py:587`. `exec_address(path.max_load, h)`
  uses a hit taken at the source load's IP.
- Impact: no row when the max sits behind a pointer the code reloads.

## Observations (not filed as bugs)

- O-1: a 4-byte value at an odd address is "can't be watched". The spec's wording ("its
  aligned 2 bytes containing the address") would watch `(addr-1, 2)`; the design chose None.
  A 1-byte watch at the address itself would also work (DR7 length 1 needs no alignment).
  Product owner decision.
- O-2: with arbitrary non-code bytes before a routine, about 2% of windows lose the path
  (for example `mulss` read as `mulps`). It is always NOT_FOLLOWED, never a wrong source
  (TC-07f).
- O-3: TOTAL_S is checked between steps. With very slow reads (13 s each, not realistic) a
  trace ran 316 s. At 6 s per read it stops at 240 s.
- O-4: share of max with max 0 shows `(screen: -2,147,483,648)`. That is what the CPU's
  `cvttss2si` gives, but it looks odd.
- O-5: when a decimal copy shows 0 or a whole number, the first memory check allows +-1
  (`unit_of("0") == 1`). The second, different value usually narrows it.
- O-6: TRACE_LOST and TRACE_FAILED say "PS5" on a PS4 too. This matches the spec and the rest
  of the app.

## Automated tests added

`tests/qa/test_trace_qa.py` (deterministic; fake clock, FakeConsole, minicpu; unicorn only via
`importorskip`):
- TC-01/01b: write ordering against the Stage the progress callback saw last.
- TC-02 to TC-04: release and resume bookkeeping on 11 exit paths, two hops, and a failing
  release.
- TC-05/05b: misaligned spans traced end to end.
- TC-06/06b: the hop limit and the time cap.
- TC-07a-f: x86 patterns, call clobbering and mis-sync robustness.
- TC-08: read-only vs writable constants.
- TC-09 to TC-09f: Way round trips, max 0, negatives, a decimal shown type, and the unicorn
  cross-check.
- TC-10/10b: texts and the raw packet log.
- TC-12a-f: traced rows next to search rows; Set with max 0; the not-sure row; the whole UI
  flow; Cancel.
- TC-13 to TC-16: self copy, unlisted 8-byte freeze, guide, exec fallback max.

Run: `QT_QPA_PLATFORM=offscreen pytest -q -p no:cacheprovider` gives
`1981 passed, 24 skipped, 14 xfailed`. Ruff check and format check are clean.

## Not tested / risks

- No console: register correctness (C10a/C20a), `rep movs` RIP, NG execute breakpoints, the
  Cancel 2 s timing, detach, and PS4 titles (C20a-i stay manual). The code has no
  console-specific branches in the trace path.
- Qt modality and real threading (the tests use a synchronous TaskRunner).
- The Freeze put-back hint is best effort by design (the freezer races the game).

## BUGS FOR FEATURE-IMPLEMENTOR

See the final hand-off in the QA message. Each bug above names its failing test. Remove the
`xfail` marker when it passes.
