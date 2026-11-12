"""Linked values, the pure part (spec-linked-values L2 to L5, L8).

Shaped like Returnal (console tests 2026-11-08): place 1 is health / 100 (3.0, a
decimal), the health fraction (1.0) is a second place the game keeps, the percent
(0-100) is worked out from the fraction, and the HUD copies are worked out from place 1.
"""

from __future__ import annotations

import math

import pytest

from cheatmaker.core import linked as lk
from cheatmaker.core import real_check as rc
from cheatmaker.core.grouping import Group, Member
from cheatmaker.core.values import ValueType

F = ValueType.FLOAT
I32 = ValueType.INT32
V = rc.Verdict

PLACE1 = 0x1381150AAC
FRACTION = 0x14A146B834
PERCENT = 0x20D67E0C08
COPY_A, COPY_B = 0x12E0EA5C10, 0x12E0EA5C00
STEP = 0.75  # the pool watch reads each span this often


def member(addr, factor, vtype=F, likely=False, mark=None) -> lk.PoolMember:
    return lk.PoolMember(addr, vtype, factor, likely, mark)


FRAC_M = member(FRACTION, 1 / 3, likely=True)
PCT_M = member(PERCENT, 100 / 3)
COPY_M = member(COPY_A, 1.0, mark=V.COPY)


def series(refs, values, t0=0.0, step=STEP) -> list[lk.Reading]:
    return [
        lk.Reading(t0 + i * step, r, v) for i, (r, v) in enumerate(zip(refs, values, strict=True))
    ]


def returnal(effect: str, n: int = 27, hits=(5, 12, 20)):
    """(place 1, fraction, percent, a copy of place 1) per reading over a 20 s test.

    Each hit takes 30 health (0.3 of place 1, 0.1 of the fraction). Never goes down:
    place 1 holds. Always full: place 1 is back to full when read. Half damage: place 1
    loses half (0.15), the fraction the game's full amount."""
    p1, frac = [], []
    hp, fr = 3.0, 1.0
    for i in range(n):
        if i in hits:
            fr -= 0.1
            if effect == "half":
                hp -= 0.15
        p1.append(hp)
        frac.append(fr)
    pct = [f * 100 for f in frac]
    return p1, frac, pct, list(p1)


# -- from_group / pool_for ------------------------------------------------------------------
def group(*addrs_types_factors, likely=()) -> Group:
    members = [Member(a, vt, None, (1.0, 2.0, 3.0)) for a, vt, _f in addrs_types_factors]
    return Group(members, set(likely), [f for _a, _vt, f in addrs_types_factors])


def test_from_group_snapshots_members_factors_and_marks():
    g = group((PLACE1, F, 1.0), (FRACTION, F, 1 / 3), (COPY_A, F, 1.0), likely=(0, 1))
    snap = lk.from_group(g, {(COPY_A, F): rc.Result(V.COPY), (PLACE1, F): rc.Result(V.REAL, 2)})
    assert snap is not None
    assert [m.address for m in snap.members] == [PLACE1, FRACTION, COPY_A]
    assert [m.mark for m in snap.members] == [V.REAL, None, V.COPY]
    assert [m.likely for m in snap.members] == [True, True, False]
    assert snap.members[1].factor == pytest.approx(1 / 3)
    assert lk.from_group(group((PLACE1, F, 1.0)), {}) is None, "a single member is no pool"


def test_pool_for_rebases_on_place1_and_splits_copies():
    snap = lk.LinkedGroup(
        (
            member(FRACTION, 1.0, likely=True),  # the group head is the fraction
            member(PLACE1, 3.0),
            member(PERCENT, 100.0),
            member(COPY_A, 3.0, mark=V.COPY),
        )
    )
    watched, copies = lk.pool_for(snap, (PLACE1, F), {})
    assert [m.address for m in watched] == [FRACTION, PERCENT]
    assert [m.factor for m in watched] == pytest.approx([1 / 3, 100 / 3])
    assert [m.address for m in copies] == [COPY_A] and copies[0].factor == pytest.approx(1.0)


def test_pool_for_fresher_marks_win():
    snap = lk.LinkedGroup((member(PLACE1, 1.0), member(FRACTION, 1 / 3), member(COPY_A, 1.0)))
    fresh = {(COPY_A, F): rc.Result(V.COPY), (FRACTION, F): rc.Result(V.KEPT)}
    watched, copies = lk.pool_for(snap, (PLACE1, F), fresh)
    assert [(m.address, m.mark) for m in watched] == [(FRACTION, V.KEPT)]
    assert [m.address for m in copies] == [COPY_A]


def test_pool_for_drops_overlaps_bad_factors_and_caps_at_16():
    members = [member(PLACE1, 1.0)]
    members.append(member(PLACE1 + 2, 1.0, I32))  # shares bytes with place 1
    members.append(member(0x5000, 2.0))
    members.append(member(0x5002, 2.0, I32))  # shares bytes with the one taken before
    members.append(member(0x6000, math.nan))
    members.append(member(0x6100, -1.0))
    members.append(member(0x6200, 0.0))
    members += [member(0x7000 + 0x10 * i, 1.0 + i) for i in range(20)]
    members.append(member(0x9000, 1.0, mark=V.COPY))  # copies don't count to the 16
    watched, copies = lk.pool_for(lk.LinkedGroup(tuple(members)), (PLACE1, F), {})
    assert len(watched) == lk.POOL_MAX
    assert watched[0].address == 0x5000 and watched[1].address == 0x7000, "group order kept"
    assert [m.address for m in copies] == [0x9000]


def test_pool_for_place1_not_in_group_is_no_pool():
    snap = lk.LinkedGroup((member(FRACTION, 1.0), member(PERCENT, 100.0)))
    assert lk.pool_for(snap, (PLACE1, F), {}) == ([], [])


# -- check_start ----------------------------------------------------------------------------
def test_check_start_two_percent_rule_and_start_factor():
    p1 = rc.Target(PLACE1, F)
    pool = [member(FRACTION, 1 / 3), member(PERCENT, 100 / 3), member(COPY_A, 1.0)]
    values = {(FRACTION, F): 0.995, (PERCENT, F): 90.0, (COPY_A, F): None}
    kept, dropped = lk.check_start(p1, 3.0, pool, values)
    assert [m.address for m in kept] == [FRACTION]
    assert kept[0].factor == pytest.approx(0.995 / 3.0), "the ratio read at Start"
    assert [(m.address, why) for m, why in dropped] == [
        (PERCENT, lk.NOT_LINKED),
        (COPY_A, lk.NOT_READ),
    ]


def test_check_start_whole_numbers_get_a_slack_of_one():
    p1 = rc.Target(PLACE1, I32)
    pool = [member(0x10, 0.5, I32), member(0x20, 0.5, I32)]
    kept, dropped = lk.check_start(p1, 7, pool, {(0x10, I32): 4, (0x20, I32): 5})
    assert [m.address for m in kept] == [0x10] and [m.address for m, _w in dropped] == [0x20]


def test_check_start_place1_zero_keeps_the_group_factor():
    pool = [member(FRACTION, 1 / 3), member(PERCENT, 100 / 3)]
    kept, _ = lk.check_start(
        rc.Target(PLACE1, F), 0.0, pool, {(FRACTION, F): 0.0, (PERCENT, F): 5.0}
    )
    assert [(m.address, m.factor) for m in kept] == [(FRACTION, pytest.approx(1 / 3))]


def test_check_start_unreadable_place1_drops_everyone():
    kept, dropped = lk.check_start(rc.Target(PLACE1, F), math.nan, [FRAC_M], {FRAC_M.key: 1.0})
    assert kept == [] and dropped == [(FRAC_M, lk.NOT_LINKED)]


# -- plan_spans -------------------------------------------------------------------------------
def test_plan_spans_watched_first_copies_within_the_budget():
    watched = [member(0x10000, 1.0), member(0x10010, 2.0)]
    copies = [member(0x20000 + 0x2000 * i, 1.0, mark=V.COPY) for i in range(20)]
    copies.insert(0, member(0x10020, 1.0, mark=V.COPY))  # shares the watched span
    spans = lk.plan_spans(watched, copies)
    assert len(spans) == lk.MAX_SPANS
    assert [t.address for t in spans[0]] == [0x10000, 0x10010, 0x10020]
    assert spans[1][0].address == 0x20000
    with_ref = lk.plan_spans(watched, (), ref=rc.Target(0x10F00, F))
    assert [[t.address for t in s] for s in with_ref] == [[0x10000, 0x10010, 0x10F00]]


# -- judge_member: all three effects ------------------------------------------------------------
@pytest.mark.parametrize("effect", ["never", "full", "half"])
def test_returnal_fraction_and_percent_fall_behind_copies_keep_up(effect):
    p1, frac, pct, copy = returnal(effect)
    end = (len(p1) - 1) * STEP
    j = {
        name: lk.judge_member(m, series(p1, vals), end)
        for name, m, vals in (("frac", FRAC_M, frac), ("pct", PCT_M, pct), ("copy", COPY_M, copy))
    }
    assert j["frac"].status is lk.Status.BEHIND
    assert j["frac"].worst == pytest.approx(0.7)
    assert j["pct"].status is lk.Status.BEHIND, "the percent follows the fraction"
    assert j["copy"].status is lk.Status.KEPT_UP


def test_half_damage_member_that_halves_too_keeps_up():
    p1, _frac, _pct, _copy = returnal("half")
    halved = [p / 3 for p in p1]  # loses half of its amount, like place 1
    j = lk.judge_member(FRAC_M, series(p1, halved), (len(p1) - 1) * STEP)
    assert j.status is lk.Status.KEPT_UP


def test_unknown_when_too_few_or_nothing_lately():
    one = [lk.Reading(19.5, 3.0, 1.0), lk.Reading(19.9, 3.0, None)]
    assert lk.judge_member(FRAC_M, one, 20.0).status is lk.Status.UNKNOWN
    early = series([3.0] * 5, [0.5] * 5)  # behind, but read only in the first 3 s
    j = lk.judge_member(FRAC_M, early, 20.0)
    assert j.status is lk.Status.UNKNOWN and j.worst == 0.5
    assert lk.judge_member(FRAC_M, [], 20.0) == lk.Judged(FRAC_M, lk.Status.UNKNOWN, None)
    no_ref = [lk.Reading(t, None, 0.5) for t in (18.0, 19.0, 20.0)]
    assert lk.judge_member(FRAC_M, no_ref, 20.0).status is lk.Status.UNKNOWN


def test_behind_at_the_last_reading_only():
    vals = [1.0] * 6 + [0.9]
    assert lk.judge_member(FRAC_M, series([3.0] * 7, vals), 6 * STEP).status is lk.Status.BEHIND


def test_one_reading_behind_in_the_middle_is_not_enough():
    vals = [1.0, 1.0, 0.9, 1.0, 1.0, 1.0]
    j = lk.judge_member(FRAC_M, series([3.0] * 6, vals), 5 * STEP)
    assert j.status is lk.Status.KEPT_UP and j.worst == pytest.approx(0.9)


def test_two_readings_in_a_row_behind_then_back_up():
    vals = [1.0, 0.9, 0.9, 1.0, 1.0]
    j = lk.judge_member(FRAC_M, series([3.0] * 5, vals), 4 * STEP)
    assert j.status is lk.Status.BEHIND


def test_an_unreadable_reading_breaks_the_run():
    vals = [1.0, 0.9, None, 0.9, 1.0, 1.0]
    j = lk.judge_member(FRAC_M, series([3.0] * 6, vals), 5 * STEP)
    assert j.status is lk.Status.KEPT_UP


def test_within_two_percent_is_kept_up():
    vals = [1.0, 0.985, 0.985, 0.985]
    assert lk.judge_member(FRAC_M, series([3.0] * 4, vals), 3 * STEP).status is lk.Status.KEPT_UP


def test_whole_numbers_behind_only_by_more_than_one():
    m = member(0x40, 1.0, I32)
    assert lk.judge_member(m, series([100] * 4, [99] * 4), 3).status is lk.Status.KEPT_UP
    assert lk.judge_member(m, series([100] * 4, [98] * 4), 3).status is lk.Status.BEHIND


# -- clusters --------------------------------------------------------------------------------
def test_fraction_and_percent_are_one_cluster_a_far_copy_apart():
    p1, frac, pct, _ = returnal("never")
    lagging = [1.0] * 10 + [0.5] * (len(p1) - 10)  # behind, but on its own
    other = member(0x30000, 1 / 3, mark=V.KEPT)
    readings = {
        FRAC_M.key: series(p1, frac),
        PCT_M.key: series(p1, pct, t0=0.3),  # another span, read 0.3 s later
        other.key: series(p1, lagging),
    }
    assert lk.clusters([FRAC_M, PCT_M, other], readings) == [[FRAC_M, PCT_M], [other]]


def test_readings_taken_mid_hit_are_not_compared():
    # The percent is read 0.45 s after the fraction. A hit lands between the readings
    # at 1.2 s (percent still 100) and 1.5 s (fraction 0.9), so that pair disagrees;
    # both sit between neighbours that differ, so they are left out.
    t_frac = [0.0, 0.75, 1.5, 2.25, 3.0]
    frac = [1.0, 1.0, 0.9, 0.9, 0.9]
    pct = [100.0, 100.0, 100.0, 90.0, 90.0]  # at 1.2 s: still 100, the fraction 0.9
    rf = [lk.Reading(t, 3.0, v) for t, v in zip(t_frac, frac, strict=True)]
    rp = [lk.Reading(t + 0.45, 3.0, v) for t, v in zip(t_frac, pct, strict=True)]
    assert lk.clusters([FRAC_M, PCT_M], {FRAC_M.key: rf, PCT_M.key: rp}) == [[FRAC_M, PCT_M]]


def test_zero_on_one_side_breaks_the_link_both_zero_is_skipped():
    a, b = member(0x10, 1.0), member(0x20, 1.0)
    ra = series([1] * 3, [0.0, 0.0, 0.5])
    rb = series([1] * 3, [0.0, 0.0, 0.5])
    assert lk.clusters([a, b], {a.key: ra, b.key: rb}) == [[a, b]]
    rb = series([1] * 3, [0.0, 0.0, 0.0])
    assert lk.clusters([a, b], {a.key: ra, b.key: rb}) == [[a], [b]]
    assert lk.clusters([a, b], {}) == [[a], [b]], "no readings: no link"


# -- pick ----------------------------------------------------------------------------------
def test_pick_order_real_likely_size_address():
    real = member(0x500, 1.0, mark=V.REAL)
    kept_likely = member(0x400, 1.0, likely=True, mark=V.KEPT)
    kept_big = member(0x300, 1.0, mark=V.KEPT)
    kept_low = member(0x100, 1.0, mark=V.KEPT)
    kept_high = member(0x200, 1.0, mark=V.KEPT)
    copy = member(0x600, 1.0, mark=V.COPY)
    cs = [
        [kept_high],
        [kept_low],
        [kept_big, copy],
        [kept_likely],
        [real],
        [member(0x700, 1.0, mark=V.COPY), member(0x710, 1.0, mark=V.FAILED)],
    ]
    got = lk.pick(cs)
    assert [rep.address for rep, _c in got] == [0x500, 0x400, 0x300, 0x100, 0x200]
    assert got[2][1] == [kept_big, copy], "the cluster comes with its member"


def test_pick_within_a_cluster_and_unchecked_are_not_fixed():
    copy = member(0x100, 1.0, mark=V.COPY)
    kept = member(0x200, 1.0, mark=V.KEPT)
    real = member(0x300, 1.0, mark=V.REAL)
    unchecked = member(0x50, 1.0)
    [(rep, c)] = lk.pick([[copy, kept, real, unchecked]])
    assert rep is real and len(c) == 4
    assert lk.pick([[unchecked]]) == []


# -- scaled_full -------------------------------------------------------------------------------
def test_scaled_full():
    assert lk.scaled_full(3.0, 1 / 3, F) == pytest.approx(1.0, rel=0.005)
    assert lk.scaled_full(3.0, 1 / 3, F) == F.decode(F.encode(1.0))
    assert lk.scaled_full(300, 0.5, I32) == 150
    assert lk.scaled_full(7, 0.5, I32) == 4
    assert lk.scaled_full(2**31, 2.0, I32) == 2**31 - 1, "clamped to the type"
