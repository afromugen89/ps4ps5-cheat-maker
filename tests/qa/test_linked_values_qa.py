"""QA for linked values in Make it work (docs/spec-linked-values.md L1-L17).

Report: qa/reports/2026-11-12-linked-values.md. Test case IDs (TC-xx) match the report.
Each test names its test case. Open bugs are strict xfails named ``bugNN``: they say
what the spec expects and fail today; remove the marker once the bug is fixed.

The game is tests/fakegame.py with ``linked=True`` (health, its fraction in another
object, a percent worked out from the fraction, two HUD copies of health) and with
``third=True`` a third place. Time is the builder flow tests' fake clock; its sleeps
let the game act. Nothing here touches a real console.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import logging
import math
import re
import struct
import threading

import pytest
from PySide6.QtCore import Qt

from cheatmaker.console.client import WatchHit
from cheatmaker.core import build_notes as notes
from cheatmaker.core import builder as b
from cheatmaker.core import linked as lk
from cheatmaker.core import real_check as rc
from cheatmaker.core.cave import Effect
from cheatmaker.core.cheats import Cheat, Location, LocationKind, combine
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.values import ValueType
from cheatmaker.errors import BuildFailed, ReadFailed, ScanCancelled, WriteFailed
from cheatmaker.formats import goldhen_json, shn
from cheatmaker.ui import hints
from cheatmaker.ui.cheats_view import COL_VALUE
from tests import fakegame as fg
from tests.ui.test_build_linked_ui import clock, game, h, health_row, play, win  # noqa: F401
from tests.ui.test_build_ui import add_and_select
from tests.unit.test_builder_flow import Clock, module
from tests.unit.test_builder_linked import fight
from tests.unit.test_builder_places import (
    _both_hit,
    _frac_only_me,
    _hp32_pool,
    pass_place,
    pool_of,
    setup,
    to_place,
)

F, I32 = ValueType.FLOAT, ValueType.INT32
TR = b.TryResult
V = rc.Verdict


def original_module(third: bool = False) -> tuple[bytes, bytes, bytes]:
    """The module bytes of a fresh fake game: what every put back must return to."""
    return module(fg.build(capture_ticks=False, linked=True, third=third))


def heap_writes(g: fg.Game) -> set[int]:
    """Addresses the app wrote outside the game's module (FakeConsole.writes holds
    (pid, address, data))."""
    return {addr for _pid, addr, _data in g.fake.writes if addr >= fg.HEAP}


def M(addr=0x1000, vtype=F, k=1.0, likely=False, mark=V.KEPT) -> lk.PoolMember:
    return lk.PoolMember(addr, vtype, k, likely, mark)


def Rs(values, ref=100.0, every=0.75, start=0.0) -> list[lk.Reading]:
    return [lk.Reading(start + i * every, ref, v) for i, v in enumerate(values)]


# == TC-01 / TC-02: L1, L17 - when it runs; nothing changes without a pool ===============
def test_tc01_no_pool_build_logs_linked_none_and_no_linked_lines(caplog):
    caplog.set_level(logging.INFO)
    g, clk, bl = setup(pool=lambda g: None)
    bl.linked = None
    bl.watch()
    clk.events.extend(fight(g))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    assert bl.after_pass().done and bl.pool_rows == {}
    res = bl.finalize()
    assert (res.places, res.linked) == (1, "none")
    assert "action=build_pool" not in caplog.text and "action=build_linked" not in caplog.text
    line = next(r.getMessage() for r in caplog.records if "action=build_result" in r.getMessage())
    assert line.endswith("places=1 linked=none")


def test_tc02_pool_that_keeps_up_is_one_test_and_done():
    """A HUD copy that follows place 1 (not checked yet): one test, Done, linked=none."""
    g = fg.build(capture_ticks=False, linked=True)
    g.fake.write_hits[g.health_addr] = [WatchHit(fg.WRITER + 10)] * 3
    clk = Clock()
    pool = lk.LinkedGroup(
        (
            lk.PoolMember(g.health_addr, F, 1.0, True, V.REAL),
            lk.PoolMember(fg.HUD_COPIES[0], F, 1.0, False, None),  # the game's own copy
        )
    )
    bl = b.Builder(
        g.session, g.health_addr, F, Effect.NEVER_DOWN, name="Health",
        sleep=clk.sleep, clock=clk, pool=pool,
    )  # fmt: skip
    bl.watch()
    clk.events.extend(fight(g))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    assert bl.after_pass() == b.LinkedPlan(True)
    assert len(bl.tried) == 1 and bl.finalize().linked == "none"


# == TC-03 / TC-04: L2 - the pool and the Start check ======================================
def test_tc03_pool_caps_16_watched_not_counting_place1_and_copies():
    head = lk.PoolMember(0x100, F, 2.0, True, V.REAL)
    members = [head]
    for i in range(20):
        members.append(lk.PoolMember(0x1000 + 0x10 * i, F, 4.0, False, None))
    for i in range(5):
        members.append(lk.PoolMember(0x9000 + 0x10 * i, F, 2.0, False, V.COPY))
    watched, copies = lk.pool_for(lk.LinkedGroup(tuple(members)), head.key, {})
    assert len(watched) == lk.POOL_MAX == 16 and len(copies) == 5
    assert [m.address for m in watched] == [0x1000 + 0x10 * i for i in range(16)], "group order"
    assert all(m.factor == pytest.approx(2.0) for m in watched), "rebased on place 1"


def test_tc03b_overlap_bad_factor_and_fresher_marks():
    head = lk.PoolMember(0x100, F, 1.0, True, None)
    group = lk.LinkedGroup(
        (
            head,
            lk.PoolMember(0x102, F, 1.0, False, None),  # shares 2 bytes with place 1
            lk.PoolMember(0x200, F, 1.0, False, None),
            lk.PoolMember(0x203, I32, 1.0, False, None),  # shares a byte with 0x200
            lk.PoolMember(0x300, F, -1.0, False, None),  # negative factor
            lk.PoolMember(0x400, F, math.nan, False, None),
            lk.PoolMember(0x500, F, 0.5, False, None),  # Check which is real in B1: copy
        )
    )
    marks = {(0x500, F): rc.Result(V.COPY, 3)}
    watched, copies = lk.pool_for(group, head.key, marks)
    assert [m.address for m in watched] == [0x200]
    assert [m.address for m in copies] == [0x500] and copies[0].mark is V.COPY


def test_tc04_start_check_edges():
    p1 = rc.Target(0x100, F)
    kept, dropped = lk.check_start(
        p1,
        100.0,
        [M(0x10, k=0.01), M(0x20, k=0.01), M(0x30, I32, 0.5), M(0x40, k=1.0), M(0x50, k=1.0)],
        {(0x10, F): 1.019, (0x20, F): 1.03, (0x30, I32): 49, (0x40, F): math.nan, (0x50, F): None},
    )
    assert [m.address for m in kept] == [0x10, 0x30]
    assert kept[0].factor == pytest.approx(0.01019), "the Start ratio"
    assert dict((m.address, why) for m, why in dropped) == {
        0x20: lk.NOT_LINKED, 0x40: lk.NOT_LINKED, 0x50: lk.NOT_READ,
    }  # fmt: skip
    kept, dropped = lk.check_start(p1, 0.0, [M(0x10, k=0.01)], {(0x10, F): 0.0})
    assert kept[0].factor == 0.01 and not dropped, "place 1 at 0: the group's factor"


def test_tc05_find_freezes_on_the_pool_are_released_at_start(win, game, h, clock):  # noqa: F811
    add_and_select(win, health_row(game, pool_of(game)))
    ctl = win.ctl
    for a in (game.fraction_addr, fg.HUD_COPIES[0]):
        ctl.freezer.freeze(f"find:{a}", [(fg.PID, a, game.fake.peek(fg.PID, a, 4))])
        ctl.frozen_addresses.add(a)
    play(clock, game)
    try:
        win.cheats.build_btn.click()
        assert ctl.frozen_addresses == set() and h.last.builder.pool_released == 2
        assert h.last.result_cheat is not None and h.last.result_cheat.places == 2
    finally:
        ctl.freezer.clear()


# == TC-06 / TC-07 / TC-08: L3 - the fell-behind judge ======================================
@pytest.mark.parametrize(
    ("member", "values", "ref", "want"),
    [
        pytest.param(M(), [100.0] * 27, 100.0, lk.Status.KEPT_UP, id="equal"),
        pytest.param(M(), [100.0] * 26 + [97.9], 100.0, lk.Status.BEHIND, id="last-2.1pct"),
        pytest.param(M(), [100.0] * 26 + [98.0], 100.0, lk.Status.KEPT_UP, id="last-2.0pct"),
        pytest.param(M(), [0.0] * 27, 0.0, lk.Status.KEPT_UP, id="zero-both"),
        pytest.param(M(), [-50.0] * 20 + [-55.0] * 7, -50.0, lk.Status.BEHIND, id="negative"),
        pytest.param(M(), [-50.0] * 27, -50.0, lk.Status.KEPT_UP, id="negative-held"),
        pytest.param(M(vtype=I32), [99] * 27, 100, lk.Status.KEPT_UP, id="int-gap-1"),
        pytest.param(M(vtype=I32), [98] * 27, 100, lk.Status.BEHIND, id="int-gap-2"),
        pytest.param(M(vtype=I32, k=1 / 3), [33] * 27, 100.0, lk.Status.KEPT_UP, id="int-trunc"),
        pytest.param(M(), [math.nan] * 27, 100.0, lk.Status.UNKNOWN, id="nan"),
        pytest.param(M(), [None] * 27, 100.0, lk.Status.UNKNOWN, id="unreadable"),
        pytest.param(M(), [100.0] * 20 + [None] * 7, 100.0, lk.Status.UNKNOWN, id="gone-at-end"),
        pytest.param(M(), [100.0], 100.0, lk.Status.UNKNOWN, id="one-reading"),
        pytest.param(
            M(), [100.0] * 10 + [90.0] * 3 + [100.0] * 14, 100.0, lk.Status.BEHIND, id="recovers"
        ),
        pytest.param(
            M(),
            [100.0] * 10 + [90.0, None, 90.0] + [100.0] * 14,
            100.0,
            lk.Status.KEPT_UP,
            id="run-broken-by-unreadable",
        ),
        pytest.param(
            M(), [100.0] * 10 + [90.0] + [100.0] * 16, 100.0, lk.Status.KEPT_UP, id="one-blip"
        ),
        pytest.param(M(), [101.0] * 27, 100.0, lk.Status.KEPT_UP, id="above-is-fine"),
    ],
)
def test_tc06_judge_edges(member, values, ref, want):
    rs = Rs(values, ref)
    end_t = rs[-1].t + 0.1
    assert lk.judge_member(member, rs, end_t).status is want


def test_tc06b_nan_reference_makes_readings_unusable():
    rs = [lk.Reading(i * 0.75, math.nan, 50.0) for i in range(27)]
    assert lk.judge_member(M(), rs, 20.0).status is lk.Status.UNKNOWN


def test_tc07_half_damage_small_hits_add_up():
    """Half damage, hits of 2% of health: place 1 loses 1% per hit, the fraction 2%.
    One hit is within the 2% rule; two are behind (spec Words: more than 2%)."""
    m = M(k=0.01, likely=True)
    one = [
        lk.Reading(i * 0.75, 100.0 if i < 10 else 99.0, 1.0 if i < 10 else 0.98) for i in range(27)
    ]
    two = [
        lk.Reading(i * 0.75, 100.0 - (i >= 10) - (i >= 20), 1.0 - 0.02 * ((i >= 10) + (i >= 20)))
        for i in range(27)
    ]
    assert lk.judge_member(m, one, 20.0).status is lk.Status.KEPT_UP
    assert lk.judge_member(m, two, 20.0).status is lk.Status.BEHIND


def test_tc07b_half_damage_whole_numbers():
    m = M(vtype=I32)
    rs = [lk.Reading(i * 0.75, 100 - (i >= 10) - (i >= 20), 100 - 2 * ((i >= 10) + (i >= 20)))
          for i in range(27)]  # fmt: skip
    assert lk.judge_member(m, rs, 20.0).status is lk.Status.BEHIND


def test_tc08_bug05_behind_for_less_than_a_second_is_kept_up():
    """Spec Words: 'at the end of the test or for at least 1 s in a row'. The pool watch
    reads a span again after 0.65 s (POOL_EVERY_S - POOL_EARLY_S), and judge_member
    counts any 2 consecutive behind readings without looking at their times."""
    rs = [lk.Reading(t, 100.0, 100.0) for t in (0, 1, 2, 3, 4)]
    rs += [lk.Reading(4.65, 100.0, 90.0), lk.Reading(5.3, 100.0, 90.0)]  # 0.65 s behind
    rs += [lk.Reading(6.0 + t, 100.0, 100.0) for t in range(15)]
    assert lk.judge_member(M(), rs, 20.5).status is lk.Status.KEPT_UP


# == TC-09 / TC-10: L3 - pool reads during a test =============================================
def _sixteen(slow: bool, pool: bool = True, latency: float = fg.READ_LATENCY_S):
    """16 members, each in its own 4 KiB span; with ``slow`` every console read costs
    ``latency`` (36 ms, a real PS5, by default)."""
    g = fg.build(capture_ticks=False, linked=True)
    g.fake.write_hits[g.health_addr] = [WatchHit(fg.WRITER + 10)] * 3
    clk = Clock()
    if slow:
        g.slow_reads(lambda s: setattr(clk, "now", clk.now + s), latency)
    members = [lk.PoolMember(g.health_addr, F, 1.0, True, V.REAL)]
    for i in range(16):
        addr = fg.HEAP + i * 0x1000 + 0xF00  # free bytes in every heap page
        g.fake.poke(fg.PID, addr, struct.pack("<f", 100.0 * (1 + i)))
        members.append(lk.PoolMember(addr, F, float(1 + i), False, V.KEPT))
    bl = b.Builder(
        g.session, g.health_addr, F, Effect.NEVER_DOWN, name="Health",
        sleep=clk.sleep, clock=clk, pool=lk.LinkedGroup(tuple(members)) if pool else None,
    )  # fmt: skip
    bl.watch()
    clk.events.extend(fight(g))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    return bl


def _worst_gap(data: lk.PoolData) -> float:
    t0 = data.end_t - b.TEST_S
    worst = 0.0
    for rs in data.readings.values():
        ts = [t0, *(r.t for r in rs), data.end_t]
        worst = max(worst, max(y - x for x, y in itertools.pairwise(ts)))
    return worst


def test_tc09a_sixteen_spans_without_latency_are_read_once_a_second():
    data = _sixteen(slow=False)._passed.pool
    assert data.spans == 16 and data.late == 0
    assert _worst_gap(data) <= lk.ONCE_A_SECOND_S + 1e-9


def test_tc09b_bug02_sixteen_spans_at_36ms_are_read_once_a_second():
    """36 ms (a real PS5) is the slowest latency where 16 spans still fit in the time
    the value's sampling leaves (L3)."""
    data = _sixteen(slow=True)._passed.pool
    assert data.late == 0, f"{data.late} late reads"
    assert _worst_gap(data) <= lk.ONCE_A_SECOND_S, _worst_gap(data)


@pytest.mark.parametrize("latency", [0.05, 0.08])
def test_tc09c_a_console_too_slow_for_both_reads_the_pool_late(caplog, latency):
    """Beyond 36 ms 16 spans don't fit: the value's own readings come first (B5), late
    pool reads are logged, and every member is read again as the test ends."""
    caplog.set_level(logging.INFO)
    bl = _sixteen(slow=True, latency=latency)
    data = bl._passed.pool
    assert data.late > 0
    assert f"late={data.late}" in caplog.text
    for rs in data.readings.values():
        assert data.end_t - rs[-1].t <= lk.ONCE_A_SECOND_S + latency * data.spans


@pytest.mark.parametrize("latency", [0.036, 0.05, 0.06, 0.08])
def test_tc10_sixteen_spans_keep_the_target_sample_budget(latency):
    with_pool = len(_sixteen(slow=True, latency=latency)._pending[1].samples)
    without = len(_sixteen(slow=True, pool=False, latency=latency)._pending[1].samples)
    assert with_pool >= 0.9 * without, (with_pool, without)


# == TC-11: L4 - copies are never written ======================================================
def test_tc11_copies_never_written_through_two_places():
    g, clk, bl = setup()
    pass_place(bl, clk, g)
    bl.next_place()
    assert heap_writes(g) == {fg.PERCENT}, "only the L4 check, on the unmarked percent"
    assert g.fake.peek(fg.PID, fg.PERCENT, 4) == struct.pack("<f", g.fraction() * 100.0)
    assert pass_place(bl, clk, g).done
    bl.finalize()
    for _ in range(3):
        g.damage(10.0)
    assert not heap_writes(g) & set(fg.HUD_COPIES)
    assert heap_writes(g) == {fg.PERCENT}


def test_tc11b_fallback_freezes_only_the_failing_place():
    g, clk, bl = setup()
    pass_place(bl, clk, g)
    bl.next_place()
    g.fake.write_hits[g.fraction_addr] = []
    with pytest.raises(b.PlaceFailed):
        bl.watch()
    live = bl.live = LiveCheats(g.session, Freezer(g.session))
    try:
        res = bl.fallback()
    finally:
        live.freezer.stop()
    assert res.linked == "freeze"
    assert heap_writes(g) <= {fg.PERCENT, g.fraction_addr}
    assert not heap_writes(g) & set(fg.HUD_COPIES)


# == TC-12 / TC-13 / TC-14: L5 - clusters and which place next ================================
def _two_series(hits, offset):
    """The fraction (read at 0.75 s steps) and the percent (100 x fraction, read
    ``offset`` s later, in another 4 KiB span): one value and its copy."""

    def frac(t: float) -> float:
        return 1.0 - 0.1 * sum(t >= hit for hit in hits)

    a = [lk.Reading(0.75 * i, 100.0, frac(0.75 * i)) for i in range(27)]
    p = [lk.Reading(0.75 * i + offset, 100.0, 100 * frac(0.75 * i + offset)) for i in range(27)]
    return a, p


def test_tc13_readings_at_different_times_still_cluster():
    fr, pc = M(0x1000, mark=V.KEPT, likely=True), M(0x9000, mark=None)
    a, p = _two_series((5.1, 10.1, 15.1), offset=0.4)
    cs = lk.clusters([fr, pc], {fr.key: a, pc.key: p})
    assert [[m.address for m in c] for c in cs] == [[0x1000, 0x9000]]


def test_tc12_bug04_last_readings_straddling_a_hit_still_cluster():
    """The fraction's last reading is at 19.5 s, the percent's at 19.9 s; a hit lands at
    19.7 s. Every other pair has the same ratio, but _same_ratio compares each pair with
    the last pair only (which is mid-hit and isn't dropped: it has no next reading)."""
    fr, pc = M(0x1000, mark=V.KEPT, likely=True), M(0x9000, mark=None)
    a, p = _two_series((5.1, 10.1, 19.7), offset=0.4)
    cs = lk.clusters([fr, pc], {fr.key: a, pc.key: p})
    assert len(cs) == 1, [[hex(m.address) for m in c] for c in cs]


def test_tc14_pick_order_real_likely_size_address():
    real = M(0x500, mark=V.REAL)
    kept_likely = M(0x400, mark=V.KEPT, likely=True)
    kept_big = M(0x300, mark=V.KEPT)
    kept_low = M(0x100, mark=V.KEPT)
    copy = M(0x50, mark=V.COPY, likely=True)
    unchecked = M(0x60, mark=rc.Verdict.FAILED)
    cs = [[kept_low], [kept_big, M(0x310, mark=V.COPY)], [kept_likely], [real], [copy], [unchecked]]
    order = [rep.address for rep, _c in lk.pick(cs)]
    assert order == [0x500, 0x400, 0x300, 0x100], "copies and unchecked are never picked"


# == TC-15 / TC-16: L6 - rooms, slots, notes per place ========================================
def test_tc15_three_places_get_their_own_rooms_and_slots():
    g, clk, bl = setup(third=True, extra_reserved=[(0x3000, 0x3100)])
    to_place(bl, clk, g, 3)
    inst = bl._prepare(bl.next_candidate(), 1)
    base = fg.BASE
    mine = [(a - base, a - base + s) for a, s in inst.rooms]
    kept = [(a - base, a - base + s) for p in bl._kept for a, s in p.inst.rooms]
    assert len(bl._kept) == 2
    for lo, hi in [*kept, (0x3000, 0x3100)]:
        assert all(hi <= x or y <= lo for x, y in mine), (mine, lo, hi)
    slots = {p.inst.slot for p in bl._kept} | {inst.slot}
    assert len(slots) == 3, "three counters, none shared"
    a, b_ = (p.inst.rooms for p in bl._kept)
    assert all(y <= x2 or y2 <= x for x, s in a for x2, s2 in b_ for y, y2 in [(x + s, x2 + s2)])


def test_tc16_bug07_collect_at_place_2_is_noted_as_place_2(monkeypatch):
    g, clk, bl = _frac_only_me()
    seen: list[notes.OpenNote] = []
    real = notes.open_note
    monkeypatch.setattr(notes, "open_note", lambda n: (seen.append(n), real(n))[1])
    objs = (fg.FRAC_OBJ, fg.HEAP + 0xB800)
    clk.events.extend(
        [lambda o=o: g.hit(o, g.value(o, "fraction"), "fraction") for _ in range(3) for o in objs]
    )
    bl.collect()
    collect_notes = [n for n in seen if n.method == "collect"]
    assert collect_notes and all(n.place == 2 for n in collect_notes), [
        n.place for n in collect_notes
    ]


# == TC-17 / TC-18: L7, L8 - Only me and Full per place ========================================
@pytest.mark.parametrize("effect", [Effect.ALWAYS_FULL, Effect.NEVER_DOWN, Effect.HALF])
def test_tc18_whole_number_second_place_every_effect(effect):
    """Place 2 is a whole number (sub [rax+2B0h],ecx) in place 1's object."""
    g = fg.build(capture_ticks=False)
    g.fake.write_hits[g.health_addr] = [WatchHit(fg.WRITER + 10)] * 3
    g.fake.write_hits[g.player + fg.HP32] = [WatchHit(fg.SUB_SITE + 6)] * 3
    clk = Clock()
    bl = b.Builder(
        g.session, g.health_addr, F, effect, name="Health",
        sleep=clk.sleep, clock=clk, pool=_hp32_pool(g),
    )  # fmt: skip
    hit = _both_hit(g, enemies=False)
    for place in (1, 2):
        bl.watch()
        clk.events.extend([None] * 19 + [hit] + [None] * 19 + [hit])
        out = bl.try_candidate(bl.next_candidate())
        assert out.result is TR.PASS, (place, out)
        plan = bl.after_pass()
        if place == 1:
            assert not plan.done
            assert bl.next_place().member.vtype is I32
            if effect is Effect.ALWAYS_FULL:
                assert bl.full == 100 and isinstance(bl.full, int), "exact after rounding"
    assert plan.done
    res = bl.finalize()
    assert (res.places, res.linked) == (2, "fixed")
    h0, i0 = g.health(), g.value(g.player, "int32")
    hit()
    if effect is Effect.HALF:
        assert (g.health(), g.value(g.player, "int32")) == (h0 - 5.0, i0 - 5)
    else:
        assert (g.health(), g.value(g.player, "int32")) == (h0, i0)


# == TC-19: L10 - limits ==========================================================
def test_tc19_bug01_place_limit_counts_the_freeze(monkeypatch):
    """With MAX_PLACES = 2 (the same code path as 3 places + a 4th value): two places
    pass, the third value still falls. L10: at most MAX_PLACES places per build; the
    design: 'Freeze places count toward MAX_PLACES'. Today the result has 3 places."""
    monkeypatch.setattr(lk, "MAX_PLACES", 2)
    g, clk, bl = setup(third=True)
    to_place(bl, clk, g, 2)
    clk.events.extend(fight(g))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    assert not bl.after_pass().done
    np = bl.next_place()
    assert np.fallback_reason == b.LIMIT
    live = bl.live = LiveCheats(g.session, Freezer(g.session))
    try:
        res = bl.fallback(reason=np.fallback_reason)
    finally:
        live.freezer.stop()
    assert res.places <= lk.MAX_PLACES, (res.places, [loc.part for loc in res.cheat.locations])


def test_tc19b_twelve_tests_limit_does_not_fire_without_a_pool():
    _g, _clk, bl = setup(pool=lambda g: None)
    bl.linked = None
    bl.tests_total = lk.MAX_TESTS - 1
    bl.watch()
    assert bl.next_candidate() is not None and not bl.limit_hit


# == TC-21: L11, L13, L14 - the freeze fallback in My cheats ===================================
def test_tc21_freeze_result_is_session_only_and_find_it_next_time_is_on(win, game, h, clock):  # noqa: F811
    game.fake.write_hits[game.fraction_addr] = []
    add_and_select(win, health_row(game, pool_of(game)))
    play(clock, game)
    try:
        win.cheats.build_btn.click()
        made = h.last.result_cheat
        assert made is not None and made.combined and made.freeze
        assert made.session_only and not made.saveable and made.name == "Health: Never goes down"
        i = win.ctl.cheat_file.cheats.index(made)
        win.cheats.table.selectRow(i)
        assert win.cheats.learn_btn.isEnabled(), "L14: Find it next time"
        assert not win.cheats.save_btn.isEnabled(), "not written to cheat files"
        item = win.cheats.table.item(i, COL_VALUE)
        assert not item.flags() & Qt.ItemFlag.ItemIsEditable
        assert notes.current_note() is None and notes.kept_notes() == []
    finally:
        win.ctl.freezer.clear()


# == TC-22: L12 - every way out puts every place back ===========================================
def test_tc22a_cancel_during_place_2_collect():
    g, clk, bl = _frac_only_me()
    cancel = threading.Event()
    clk.events.extend([None, None, cancel.set])
    with pytest.raises(ScanCancelled):
        bl.collect(cancel)
    assert module(g) == original_module()
    assert not bl.has_kept and notes.kept_notes() == [] and notes.current_note() is None


def test_tc22b_cancel_during_the_l4_check(monkeypatch):
    g, clk, bl = setup()
    pass_place(bl, clk, g)
    cancel = threading.Event()
    real = rc.RealChecker.run

    def run(self, cancel=None):
        cancel.set()
        return real(self, cancel=cancel)

    monkeypatch.setattr(rc.RealChecker, "run", run)
    with pytest.raises(ScanCancelled):
        bl.next_place(cancel)
    assert module(g) == original_module() and not bl.has_kept
    assert g.fake.peek(fg.PID, fg.PERCENT, 4) == struct.pack("<f", g.fraction() * 100.0)


def test_tc22c_finalize_that_cant_keep_place_2_then_close():
    g, clk, bl = setup()
    pass_place(bl, clk, g)
    bl.next_place()
    assert pass_place(bl, clk, g).done
    real = g.fake.write
    calls = [0]

    def write(pid, addr, data):
        calls[0] += 1
        if calls[0] == 1:
            raise WriteFailed("refused")
        return real(pid, addr, data)

    g.fake.write = write  # type: ignore[method-assign]
    with pytest.raises(BuildFailed, match="Couldn't keep the fix"):
        bl.finalize()
    g.fake.write = real  # type: ignore[method-assign]
    assert bl.has_kept, "place 1 is still in until the dialog closes the builder"
    assert bl.close() is b.Rollback.RESTORED
    assert module(g) == original_module()
    assert notes.current_note() is None and notes.kept_notes() == []


def test_tc22d_fallback_that_cant_read_the_member_then_close(monkeypatch):
    monkeypatch.setattr(lk, "MAX_PLACES", 2)
    g, clk, bl = setup(third=True)
    to_place(bl, clk, g, 2)
    clk.events.extend(fight(g))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    bl.after_pass()
    assert bl.next_place().fallback_reason == b.LIMIT
    real = g.fake.read

    def read(pid, addr, size):
        if addr == g.third_addr:
            raise ReadFailed("unloaded")
        return real(pid, addr, size)

    monkeypatch.setattr(g.fake, "read", read)
    live = bl.live = LiveCheats(g.session, Freezer(g.session))
    try:
        res = bl.fallback(reason=b.LIMIT)  # nothing more is held: the code fixes only
    finally:
        live.freezer.stop()
    monkeypatch.setattr(g.fake, "read", real)
    assert (res.linked, res.places, res.cheat.freeze) == ("partial", 2, False)
    assert notes.kept_notes() == [] and bl.close() is b.Rollback.NOTHING, "the result stays"
    live.turn_off(res.cheat)  # switched off: the game's code is its own again
    assert g.fake.peek(fg.PID, fg.WRITER, len(fg.VMOVSS_W)) == fg.VMOVSS_W
    assert g.fake.peek(fg.PID, fg.FRAC_SITE, len(fg.VMOVSS_FRAC)) == fg.VMOVSS_FRAC
    assert module(g)[1:] == original_module(third=True)[1:], "module data untouched"


def test_tc22e_ps_button_during_place_3_keeps_places_then_close_puts_all_back():
    g, clk, bl = setup(third=True)
    to_place(bl, clk, g, 3)
    clk.events.extend([None] * 10 + [lambda: setattr(g.fake, "foreground_pid", None)])
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.LOST and out.rollback is b.Rollback.RESTORED
    assert bl.has_kept and [k.place for k in notes.kept_notes()] == [1, 2]
    assert not notes.is_crashed(fg.TITLE, fg.VERSION, fg.THIRD_SITE - fg.BASE, "hook")
    g.fake.foreground_pid = fg.PID
    assert bl.close() is b.Rollback.RESTORED
    assert module(g) == original_module(third=True) and notes.kept_notes() == []


def test_tc22f_disconnect_between_steps_of_place_2(win, game, h, clock):  # noqa: F811
    add_and_select(win, health_row(game, pool_of(game)))
    before = module(game)
    fired: list[int] = []
    real_watch = b.Builder.watch

    def watch(self, cancel=None):
        if self.place == 2 and not fired:
            fired.append(1)
            win.ctl.run_before_disconnect()  # Disconnect pressed as place 2's watch starts
        return real_watch(self, cancel)

    b.Builder.watch = watch
    try:
        play(clock, game)
        win.cheats.build_btn.click()
    finally:
        b.Builder.watch = real_watch
    d = h.last
    assert fired and d.result_cheat is None
    assert module(game) == before
    assert notes.kept_notes() == [] and notes.current_note() is None
    assert d.hint.text() == hints.BUILD_CANCELLED


def test_tc22g_disconnect_during_place_2_test(win, game, h, clock):  # noqa: F811
    add_and_select(win, health_row(game, pool_of(game)))
    before = module(game)
    fired: list[int] = []

    def disconnect_in_place_2() -> None:
        bl = h.last.builder
        if not fired and bl is not None and bl.place == 2 and bl._installed is not None:
            fired.append(1)
            win.ctl.run_before_disconnect()

    play(clock, game, then=disconnect_in_place_2)
    win.cheats.build_btn.click()
    assert fired and h.last.result_cheat is None
    assert module(game) == before and h.last.hint.text() == hints.BUILD_CANCELLED
    assert notes.kept_notes() == [] and notes.current_note() is None


# == TC-23: L12 - notes across reconnect ===========================================================
def test_tc23a_app_died_between_places_same_game_puts_place_1_back():
    g, clk, bl = setup()
    pass_place(bl, clk, g)
    bl.next_place()  # place 1 kept; the app dies here
    assert [k.place for k in notes.kept_notes()] == [1] and notes.current_note() is None
    hint = notes.check_on_connect(g.session, sleep=lambda _s: None)
    assert hint == notes.KEPT_PUT_BACK
    assert module(g) == original_module() and notes.kept_notes() == []
    assert not notes.is_crashed(fg.TITLE, fg.VERSION, fg.WRITER - fg.BASE, "hook")


def test_tc23b_app_died_during_place_2_test_same_game_puts_both_back():
    g, clk, bl = setup()
    to_place(bl, clk, g, 2)
    inst = bl._prepare(bl.next_candidate(), 1)
    assert bl._install(inst, threading.Event()) is None  # place 2's fix in; the app dies
    assert notes.current_note().place == 2
    hint = notes.check_on_connect(g.session, sleep=lambda _s: None)
    assert hint.startswith("Fix 1 from last time was still in the game")
    assert module(g) == original_module()
    assert notes.current_note() is None and notes.kept_notes() == []
    assert not notes.is_crashed(fg.TITLE, fg.VERSION, fg.FRAC_SITE - fg.BASE, "hook")


def test_tc23c_other_process_marks_only_the_fix_under_test():
    g, clk, bl = setup(third=True)
    to_place(bl, clk, g, 3)
    inst = bl._prepare(bl.next_candidate(), 1)
    assert bl._install(inst, threading.Event()) is None
    # The game crashed and was started again: the same title, another process.
    g.session.game = dataclasses.replace(g.session.game, pid=fg.PID + 1)
    notes.check_on_connect(g.session, sleep=lambda _s: None)
    assert notes.is_crashed(fg.TITLE, fg.VERSION, fg.THIRD_SITE - fg.BASE, "hook")
    for site in (fg.WRITER, fg.FRAC_SITE):
        assert not notes.is_crashed(fg.TITLE, fg.VERSION, site - fg.BASE, "hook")
    assert notes.kept_notes() == [] and notes.current_note() is None


def test_tc23d_old_notes_file_without_kept_and_damaged_kept_entries():
    path = notes.notes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"open": None, "crashed": []}), encoding="utf-8")
    assert notes.kept_notes() == []
    path.write_text(json.dumps({"open": None, "kept": [{"pid": 1}, 7], "crashed": []}))
    assert notes.kept_notes() == []
    notes.clear_all()


# == TC-24 / TC-25: L13, L8 - the multi-place cheat in files ====================================
def _two_place_result(win, game, h, clock):  # noqa: F811
    add_and_select(win, health_row(game, pool_of(game)))
    play(clock, game)
    win.cheats.build_btn.click()
    made = h.last.result_cheat
    assert made is not None and made.places == 2
    return made


@pytest.mark.parametrize("fmt", [goldhen_json, shn])
def test_tc24_multi_place_cheat_round_trips_through_cheat_files(win, game, h, clock, fmt):  # noqa: F811
    made = _two_place_result(win, game, h, clock)
    assert made.saveable and not made.combined
    back = fmt.loads(fmt.dumps(win.ctl.cheat_file)).cheats[0]
    assert [(loc.offset, loc.on, loc.off) for loc in back.locations] == [
        (loc.offset, loc.on, loc.off) for loc in made.locations
    ]
    assert back.value is None


def test_tc25a_full_is_read_only_with_its_tooltip(win, game, h, clock):  # noqa: F811
    made = _two_place_result(win, game, h, clock)
    i = win.ctl.cheat_file.cheats.index(made)
    item = win.cheats.table.item(i, COL_VALUE)
    assert not item.flags() & Qt.ItemFlag.ItemIsEditable
    assert item.toolTip() == hints.BUILD_FULL_PLACES


def test_tc25b_bug06_full_tooltip_after_reload(win, game, h, clock):  # noqa: F811
    _two_place_result(win, game, h, clock)
    back = goldhen_json.loads(goldhen_json.dumps(win.ctl.cheat_file))
    win.ctl.cheat_file.cheats[:] = back.cheats
    win.ctl.cheats_changed.emit()
    item = win.cheats.table.item(0, COL_VALUE)
    assert not item.flags() & Qt.ItemFlag.ItemIsEditable, "still read-only"
    assert item.toolTip() == hints.BUILD_FULL_PLACES


# == TC-26: L15 - Advanced list ===================================================================
def test_tc26_bug03_unreadable_member_is_not_called_kept_up(monkeypatch):
    g, clk, bl = setup(pool=lambda g: pool_of(g, percent=V.KEPT))
    pass_place(bl, clk, g)
    assert bl.pool_rows[fg.PERCENT].status is lk.Status.BEHIND
    bl.next_place()
    bl.watch()
    real = g.fake.read

    def read(pid, addr, size):
        if addr <= fg.PERCENT < addr + size:
            raise ReadFailed("area unloaded")
        return real(pid, addr, size)

    monkeypatch.setattr(g.fake, "read", read)
    clk.events.extend(fight(g))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    bl.after_pass()
    row = bl.pool_rows[fg.PERCENT]
    assert row.status is lk.Status.UNKNOWN
    assert row.done is not lk.Done.NOT_NEEDED, hints.BUILD_POOL_DONE[row.done.value].format(
        n=row.after_place
    )


# == TC-27: L16 - log lines =======================================================
def test_tc27_log_lines_of_a_two_place_build(caplog):
    caplog.set_level(logging.INFO)
    g, clk, bl = setup()
    pass_place(bl, clk, g)
    bl.next_place()
    assert pass_place(bl, clk, g).done
    bl.finalize()
    text = caplog.text
    pats = [
        r"action=build_pool members=\d+ dropped=\d+ released=\d+",
        r"action=build_linked_check n=1 member=0x[0-9a-f]+ k=[\d.e+-]+ result=behind worst=[\d.]+",
        r"action=build_linked_check n=2 member=0x[0-9a-f]+ k=[\d.e+-]+ result=kept_up worst=",
        r"action=build_linked_pick place=2 member=0x[0-9a-f]+ mark=kept cluster=\d+ reason=",
        r"action=build_linked_result place=2 result=pass",
        r"action=build_result .* places=2 linked=fixed",
    ]
    for p in pats:
        assert re.search(p, text), p


# == TC-29: L2 - My cheats pool only for grouped rows; combine drops it ===========================
def test_tc29_combine_drops_the_pool_and_it_is_never_saved(game):  # noqa: F811
    a = health_row(game, pool_of(game))
    other = Cheat(
        "Gold", [Location(LocationKind.SESSION_ADDRESS, fg.HEAP + 8, b"\1\0\0\0", b"\0\0\0\0")]
    )
    c = combine([a, other], name="Both")
    assert c.linked is None
    assert a == health_row(game, None), "the pool never takes part in equality"
