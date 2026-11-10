"""Percent search (spec-health-finding AC-P1 to AC-P3): values, PC checks, FakeConsole
and the scanner."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.console.client import ScanHit
from cheatmaker.console.fake import FakeConsole
from cheatmaker.console.local_scan import way_parts
from cheatmaker.core import percent_filter as pf
from cheatmaker.core.scanner import NOTED, RECORD_LIMIT, TOO_MANY, Scanner
from cheatmaker.core.session import Session
from cheatmaker.core.values import (
    FRACTION,
    OF_FULL,
    PERCENT,
    X10,
    Compare,
    ValueType,
    parse_percent,
    percent_parts,
    scale_target,
)
from cheatmaker.settings import Settings
from tests.conftest import HEAP_BASE, PID, build_fake

F, W = ValueType.FLOAT, ValueType.INT32  # decimal, whole number
PCT_F = HEAP_BASE + 0x300  # health as a percent, decimal
FRAC = HEAP_BASE + 0x310  # health as a fraction
X10_I = HEAP_BASE + 0x320  # health x10, whole number
PCT_I = HEAP_BASE + 0x330  # health as a percent, whole number


def f32(fake: FakeConsole, addr: int, v: float) -> None:
    fake.poke(PID, addr, struct.pack("<f", v))


def i32(fake: FakeConsole, addr: int, v: int) -> None:
    fake.poke(PID, addr, struct.pack("<i", v))


def show_percent(fake: FakeConsole, pct: float) -> None:
    """The game shows ``pct`` % health, kept four ways."""
    f32(fake, PCT_F, pct)
    f32(fake, FRAC, pct / 100)
    i32(fake, X10_I, round(pct * 10))
    i32(fake, PCT_I, round(pct))


def new_session(fake: FakeConsole) -> Session:
    s = Session(lambda ip, port: fake)
    s.connect("192.168.1.50")
    s.refresh_game()
    return s


# -- values ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "want"),
    [("40%", (40, 1.0)), (" 40.5 % ", (40.5, 0.1)), ("0%", (0, 1.0)), ("100%", (100, 1.0))],
)
def test_parse_percent(text, want):
    value, unit = parse_percent(text)
    assert value == want[0] and abs(unit - want[1]) < 1e-12


def test_parse_percent_plain_number_is_none_and_bad_text_says_why():
    assert parse_percent("40") is None
    with pytest.raises(ValueError, match="0 to 100"):
        parse_percent("120%")
    with pytest.raises(ValueError, match="0 to 100"):
        parse_percent("-5%")
    with pytest.raises(ValueError, match="isn't a number"):
        parse_percent("abc%")
    with pytest.raises(ValueError, match="before the %"):
        parse_percent("%")


def test_percent_parts_by_type():
    assert percent_parts(40) == [(F, PERCENT), (F, FRACTION), (F, X10), (W, PERCENT), (W, X10)]
    assert percent_parts(40.5) == [(F, PERCENT), (F, FRACTION), (F, X10), (W, X10)]
    assert percent_parts(40.25) == [(F, PERCENT), (F, FRACTION), (F, X10)]
    assert percent_parts(40, W) == [(W, PERCENT), (W, X10)]
    assert percent_parts(40, ValueType.DOUBLE) == [
        (ValueType.DOUBLE, w) for w in (PERCENT, FRACTION, X10)
    ]


def test_scale_target_per_compare():
    assert scale_target(FRACTION, Compare.BETWEEN, (39, 41)) == pytest.approx((0.39, 0.41))
    assert scale_target(X10, Compare.DECREASED_BY, (5, 1)) == (50, 10)
    assert scale_target(X10, Compare.EXACT, 4) == 40
    assert scale_target(X10, Compare.DECREASED, None) is None
    assert scale_target(None, Compare.BETWEEN, (1, 2)) == (1, 2)


def test_way_parts_pairs_types_and_ways():
    assert way_parts((F, W), (PERCENT, X10)) == [(F, PERCENT), (W, X10)]
    assert way_parts(F, (PERCENT, FRACTION)) == [(F, PERCENT), (F, FRACTION)]
    with pytest.raises(ValueError):
        way_parts((F,), (PERCENT, X10))


# -- percent_filter -------------------------------------------------------------------
def test_filter_windows_follow_the_typed_unit():
    t = pf.Target(40, 1.0)
    assert pf.check(F, None, 40.6, None, t) is PERCENT
    assert pf.check(F, None, 0.405, None, t) is FRACTION
    assert pf.check(F, None, 395.0, None, t) is X10
    assert pf.check(F, None, 41.0, None, t) is None, "one unit away is out"
    fine = pf.Target(40.5, 0.1)
    assert pf.check(F, None, 40.45, None, fine) is PERCENT
    assert pf.check(F, None, 40.7, None, fine) is None


def test_filter_of_full_and_tied_ways():
    t = pf.Target(40, 1.0)
    full = pf.Full(250.0)
    assert pf.check(F, None, 100.0, full, t) == OF_FULL
    assert pf.check(F, None, 102.5, full, t) == OF_FULL, "41% of full is in"
    assert pf.check(F, None, 105.0, full, t) is None
    zero = pf.Target(0, 1.0)
    assert not pf.matches_way(0.0, OF_FULL, pf.Full(0.0), zero), "0 at full never matches"
    assert pf.check(F, OF_FULL, 0.0, pf.Full(0.0), zero) is None
    assert pf.check(F, PERCENT, 0.4, None, t) is None, "a percent match stays a percent"
    assert pf.check(W, None, 100, full, t) == OF_FULL
    assert pf.check(W, None, 40, full, t) is PERCENT, "the plain way first"
    assert pf.check(F, None, float("nan"), full, t) is None


def test_filter_about_uses_fraction_and_of_full_only():
    t = pf.Target(50, pf.ABOUT_TOL, about=True)
    assert pf.check(F, None, 0.6, None, t) is FRACTION
    assert pf.check(F, None, 50.0, None, t) is None, "no plain percent for About"
    assert pf.check(F, None, 150.0, pf.Full(250.0), t) == OF_FULL  # 60%
    assert pf.check(F, None, 200.0, pf.Full(250.0), t) is None  # 80%


def test_full_estimate_and_its_error():
    est = pf.full_estimate(100.0, 40, 1.0)
    assert est.value == 250.0 and est.rel_err == 1 / 40
    assert pf.full_estimate(100.0, None, 1.0) is None
    assert pf.full_estimate(100.0, 0, 1.0) is None
    # 20% typed, value 50 of an estimated 250 +-2.5%: tolerance 1 + 20/40 points
    assert pf.check(F, None, 53.5, est, pf.Target(20, 1.0)) == OF_FULL


# -- FakeConsole ----------------------------------------------------------------------
def test_fake_percent_parts_scan_and_narrow():
    fake = build_fake()
    show_percent(fake, 40)
    regions = [m for m in fake.maps(PID) if m.writable]
    parts = percent_parts(40)
    n = fake.scan_start(
        PID,
        regions,
        tuple(t for t, _ in parts),
        Compare.BETWEEN,
        (39.5, 40.5),
        ways=tuple(w for _, w in parts),
    )
    assert n == 4
    counts = fake.scan_way_counts()
    assert counts == {(F, PERCENT): 1, (F, FRACTION): 1, (W, PERCENT): 1, (W, X10): 1}
    assert fake.scan_counts() == {F: 2, W: 2}
    by_addr = {h.address: h.way for h in fake.scan_results(10)}
    assert by_addr == {PCT_F: PERCENT, FRAC: FRACTION, X10_I: X10, PCT_I: PERCENT}
    show_percent(fake, 30)
    assert fake.scan_next(Compare.BETWEEN, (29.5, 30.5)) == 4
    with pytest.raises(ValueError, match="range search"):
        fake.scan_start(PID, regions, F, Compare.EXACT, 40, ways=(PERCENT,))


def test_fake_restrict_keeps_hits_on_the_pc():
    fake = build_fake()
    regions = [m for m in fake.maps(PID) if m.writable]
    fake.scan_start(PID, regions, (F, W), Compare.UNKNOWN, None)
    from cheatmaker.console.client import ScanHit

    keep = [
        ScanHit(FRAC, struct.pack("<f", 0.5), F, FRACTION),
        ScanHit(PCT_I, struct.pack("<i", 50), W, PERCENT),
    ]
    show_percent(fake, 50)
    assert fake.scan_restrict(keep) == 2
    assert fake.scan_way_counts() == {(F, FRACTION): 1, (W, PERCENT): 1}
    show_percent(fake, 40)
    assert fake.scan_next(Compare.DECREASED, None) == 2
    assert fake.scan_restrict([]) == 0 and fake.scan_counts() == {F: 0}


# -- Scanner: AC-P1 -------------------------------------------------------------------
def test_scanner_percent_search_finds_every_way(fake, session):
    show_percent(fake, 40)
    sc = Scanner(session)
    assert sc.percent_scan(40, 1.0) == 4
    assert sc.history == ["40%"] and sc.percent_search and sc.has_ways
    assert sc.mixed and set(sc.types) == {F, W}
    show_percent(fake, 30)
    assert sc.percent_scan(30, 1.0) == 4
    show_percent(fake, 20)
    assert sc.next_scan(Compare.EXACT, 20) == 4, "a plain number counts as a percent"
    assert sc.history == ["40%", "30%", "20%"]
    show_percent(fake, 25)
    assert sc.next_scan(Compare.INCREASED) == 4
    ways = {m.address: m.way for m in sc.matches(10)}
    assert ways == {PCT_F: PERCENT, FRAC: FRACTION, X10_I: X10, PCT_I: PERCENT}


def test_scanner_percent_picked_type_and_bad_input(fake, session):
    show_percent(fake, 40)
    sc = Scanner(session)
    assert sc.percent_scan(40, 1.0, W) == 2
    assert {m.address for m in sc.matches(10)} == {X10_I, PCT_I}
    with pytest.raises(ValueError, match="0 to 100"):
        Scanner(session).percent_scan(140, 1.0)
    with pytest.raises(ValueError, match="can't hold"):
        Scanner(session).percent_scan(40.25, 0.01, W)


def test_scanner_percent_on_a_plain_search_checks_on_the_pc(fake, session):
    show_percent(fake, 40)
    sc = Scanner(session)
    sc.first_scan(None, F)
    show_percent(fake, 30)
    sc.next_scan(Compare.DECREASED)
    assert sc.count == 4  # the decimals, and the whole numbers read as decimals
    show_percent(fake, 20)
    assert sc.percent_scan(20, 1.0) == 2
    assert {m.address for m in sc.matches(10)} == {PCT_F, FRAC}
    assert sc.percent_search and sc.history[-1] == "20%"
    sc2 = Scanner(session)
    sc2.first_scan(None, F)
    sc2.pc_limit = 10
    with pytest.raises(ValueError, match="too many to check by percent"):
        sc2.percent_scan(20, 1.0)


# -- Scanner: AC-P2 / AC-P3 -----------------------------------------------------------
MAX_HP = HEAP_BASE + 0x400  # health out of 250 (not out of 100)
HP_FRAC = HEAP_BASE + 0x410


def show_hp(fake: FakeConsole, hp: float) -> None:
    f32(fake, MAX_HP, hp)
    f32(fake, HP_FRAC, hp / 250)


def test_full_then_percent_of_full(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    assert sc.mark_full() > 100 and sc.full_mode and sc.history == ["full"]
    show_hp(fake, 100)  # the pause screen says 40%
    assert sc.percent_scan(40, 1.0) == 2
    ways = {m.address: m.way for m in sc.matches(10)}
    assert ways == {MAX_HP: OF_FULL, HP_FRAC: FRACTION}
    assert sc.full[(MAX_HP, F)].value == 250.0
    show_hp(fake, 50)
    assert sc.percent_scan(20, 1.0) == 2 and sc.history == ["full", "40%", "20%"]
    show_hp(fake, 60)
    sc.next_scan(Compare.INCREASED)
    assert sc.last_pct is None, "a manual compare forgets the shown percent"
    assert sc.percent_scan(24, 1.0) == 2, "the value when full is still known"


def test_full_with_too_many_runs_plain_compares_first(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.pc_limit = 10
    sc.mark_full()
    show_hp(fake, 100)
    n = sc.percent_scan(40, 1.0)
    assert sc.note == TOO_MANY and sc.history[-1] == "40% (as went down)"
    assert 2 <= n <= 10  # the decimals and their whole-number views went down
    show_hp(fake, 50)
    assert sc.percent_scan(20, 1.0) == 2 and sc.note is None
    assert {m.address for m in sc.matches(10)} == {MAX_HP, HP_FRAC}


def test_full_then_manual_compare_then_percent_keeps_the_value(fake, session):
    """Review blocker: a Went down between Full and the first % leaves no value when
    full to check against; the % is noted (nothing dropped) and the next one narrows."""
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.mark_full()
    show_hp(fake, 100)
    sc.next_scan(Compare.DECREASED)
    assert sc.last_pct is None
    before = sc.count
    assert sc.percent_scan(40, 1.0) == before and sc.note == NOTED
    assert sc.history[-1] == "40% (noted)" and sc.last_pct == 40
    assert MAX_HP in {m.address for m in sc.matches(500)}
    show_hp(fake, 50)
    assert sc.percent_scan(20, 1.0) == 2
    assert {m.address for m in sc.matches(10)} == {MAX_HP, HP_FRAC}


def test_full_then_manual_compare_then_about_keeps_the_value(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.mark_full()
    show_hp(fake, 140)
    sc.next_scan(Compare.DECREASED)
    sc.about_scan(50)
    assert sc.note == NOTED and MAX_HP in {m.address for m in sc.matches(500)}
    show_hp(fake, 60)
    sc.about_scan(25)
    assert MAX_HP in {m.address for m in sc.matches(500)}


def test_full_over_limit_after_a_manual_compare_notes_the_percent(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.pc_limit = 1
    sc.mark_full()
    show_hp(fake, 200)
    sc.next_scan(Compare.DECREASED)
    sc.count = 5  # pretend still more than the limit
    show_hp(fake, 100)
    sc.percent_scan(40, 1.0)
    assert sc.history[-1] == "40% (noted)" and sc.note == NOTED


@pytest.mark.parametrize(
    ("first", "second", "about"),
    [(190, 170, True), (100, 99, False)],  # About 75% twice; 40% twice while 100 -> 99
)
def test_same_percent_again_never_runs_same(fake, session, first, second, about):
    """Review: an unchanged percent mustn't drop a value that moved a little."""
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.pc_limit = 1
    sc.mark_full()
    pct = 75 if about else 40
    run = (lambda: sc.about_scan(pct)) if about else (lambda: sc.percent_scan(pct, 1.0))
    show_hp(fake, first)
    run()
    assert "(as went down)" in sc.history[-1]
    show_hp(fake, second)
    run()
    assert sc.history[-1].endswith("(noted)") and sc.note == NOTED
    kept = {h.address for h in session.client.scan_results(10**6)}
    assert MAX_HP in kept


def test_close_percent_runs_no_compare(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.pc_limit = 1
    sc.mark_full()
    show_hp(fake, 100)
    sc.percent_scan(40, 1.0)
    show_hp(fake, 102)
    sc.percent_scan(41, 1.0)  # 40 -> 41 typed to whole numbers: could be either way
    assert sc.history[-1] == "41% (noted)"
    show_hp(fake, 50)
    sc.percent_scan(20, 1.0)
    assert sc.history[-1] in ("20% (as went down)", "20%")


def test_plain_number_in_a_full_search_counts_as_a_percent(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.mark_full()
    show_hp(fake, 100)
    assert sc.next_scan(Compare.EXACT, 40) == 2 and sc.history[-1] == "40%"
    with pytest.raises(ValueError, match=r"In a Full search, type the % the game shows"):
        sc.next_scan(Compare.EXACT, 250)
    with pytest.raises(ValueError, match="press Start over"):
        sc.next_scan(Compare.EXACT, 180)


def test_about_buttons_keep_a_wide_range(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    with pytest.raises(ValueError, match="full now"):
        sc.about_scan(50)
    sc.mark_full()
    show_hp(fake, 140)  # 56%: "about 50%"
    assert sc.about_scan(50) == 2 and sc.history[-1] == "about 50%"
    show_hp(fake, 60)  # 24%
    assert sc.about_scan(25) == 2


def test_full_again_runs_same_and_restarts_the_full_point(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.mark_full()
    show_hp(fake, 100)
    sc.percent_scan(40, 1.0)
    show_hp(fake, 250)
    sc.next_scan(Compare.INCREASED)
    assert sc.mark_full() == 2 and sc.history[-1] == "full again (same)"
    assert sc.last_pct == 100 and sc.full == {}
    sc.reset()
    assert not sc.full_mode and not sc.percent_search and sc.values == {}


def test_full_value_zero_never_matches(fake, session):
    show_hp(fake, 0)
    sc = Scanner(session)
    sc.mark_full()
    assert sc.percent_scan(0, 1.0) >= 0
    assert MAX_HP not in {m.address for m in sc.matches(500) if m.way == OF_FULL}


# -- Scanner: grouping (AC-G1) --------------------------------------------------------
def test_scanner_keeps_values_and_groups_linked_matches(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.mark_full()
    assert sc.count <= RECORD_LIMIT and sc.recorded == 1, "few enough: values kept at once"
    assert sc.groups() == []
    show_hp(fake, 100)
    sc.percent_scan(40, 1.0)
    assert sc.recorded == 2 and sc.groups() == [], "two values are one change: not enough"
    assert sc.changes_to_group == 1
    show_hp(fake, 25)
    sc.percent_scan(10, 1.0)
    [g] = sc.groups()
    assert sc.changes_to_group == 0
    assert {m.address for m in g.members} == {MAX_HP, HP_FRAC} and g.head_likely
    assert sc.values[(MAX_HP, F)] == [250.0, 100.0, 25.0]
    assert sc.tags[(HP_FRAC, F)] == FRACTION


def test_scan_file_lists_how_each_match_is_stored(fake, session):
    show_percent(fake, 40)
    sc = Scanner(session)
    sc.percent_scan(40, 1.0)
    text = sc.scan_log.last_file.read_text(encoding="utf-8")
    assert "# Stored as: percent, fraction, x10" in text
    assert "Address\tValue\tType\tStored as" in text
    assert f"0x{FRAC:010X}\t0.4\tDecimal (4 bytes)\tfraction" in text


def test_percent_pc_limit_setting_is_clamped(tmp_path):
    import json

    assert Settings().percent_pc_limit == 20_000
    Settings.path().parent.mkdir(parents=True, exist_ok=True)
    Settings.path().write_text(json.dumps({"percent_pc_limit": 5}))
    assert Settings.load().percent_pc_limit == 1_000
    Settings.path().write_text(json.dumps({"percent_pc_limit": "lots"}))
    assert Settings.load().percent_pc_limit == 20_000
    Settings.path().write_text(json.dumps({"percent_pc_limit": 10**9}))
    assert Settings.load().percent_pc_limit == 100_000


def test_new_session_helper_is_usable():
    assert new_session(build_fake()).game is not None


# -- review: 4-byte decimal bounds, all-or-nothing narrowing ---------------------------
def test_fraction_one_unit_away_is_out_in_four_byte_decimals(fake, session):
    """0.41 stored as a 4-byte decimal reads 0.4099999964: still one unit from 40%."""
    from cheatmaker.core.values import f32 as to32

    edge = HEAP_BASE + 0x500
    for stored, kept in ((0.41, False), (0.39, False), (0.405, True), (0.3951, True)):
        f32(fake, edge, stored)
        sc = Scanner(session)
        sc.percent_scan(40, 1.0, F)
        assert (edge in {m.address for m in sc.matches(500)}) is kept, stored
        assert (pf.check(F, None, to32(stored), None, pf.Target(40, 1.0)) is not None) is kept
    assert pf.check(ValueType.DOUBLE, None, 0.4099999, None, pf.Target(40, 1.0)) is FRACTION


def _two_parts():
    from cheatmaker.console.client import ScanHit
    from cheatmaker.console.local_scan import LocalScanSession, MultiScan

    mem = {0x100: struct.pack("<f", 30.0), 0x200: struct.pack("<f", 0.3)}
    a = LocalScanSession(lambda ad, n: mem.get(ad, bytes(n)))
    a.set_results(F, [ScanHit(0x100, struct.pack("<f", 40.0))], PERCENT)
    b = LocalScanSession(lambda ad, n: mem.get(ad, bytes(n)))
    b.set_results(F, [ScanHit(0x200, struct.pack("<f", 0.4))], FRACTION)
    return a, b, MultiScan([a, b])


@pytest.mark.parametrize("error", [ValueError("too many to compare"), "cancel"])
def test_multiscan_narrows_all_parts_or_none(error):
    import threading

    from cheatmaker.errors import ScanCancelled

    a, b, ms = _two_parts()
    cancel = threading.Event()

    def narrow(s, cmp, v, progress, c):
        if s is b:
            if error == "cancel":
                c.set()
                raise ScanCancelled("cancelled")
            raise error
        return s.narrowed(cmp, v, progress, c)

    with pytest.raises((ValueError, ScanCancelled)):
        ms.next(Compare.DECREASED, None, None, cancel, narrow=narrow)
    assert struct.unpack("<f", a.hits[0].value)[0] == 40.0, "part a kept its baseline"
    assert ms.next(Compare.DECREASED, None) == 2, "a retry still sees both go down"


def test_multiscan_check_runs_on_every_part_first():
    _a, b, ms = _two_parts()
    narrowed = []

    def check(s, cmp, v):
        if s is b:
            raise ValueError("too many to compare")

    def narrow(s, cmp, v, progress, c):
        narrowed.append(s)
        return s.narrowed(cmp, v, progress, c)

    with pytest.raises(ValueError):
        ms.next(Compare.DECREASED, None, narrow=narrow, check=check)
    assert narrowed == [], "nothing narrowed when one part can't be"


def test_multiscan_reports_progress_per_part():
    _a, _b, ms = _two_parts()
    seen = []
    ms.next(Compare.DECREASED, None, seen.append)
    assert seen[-1] == 1.0 and seen == sorted(seen) and any(0 < p < 1 for p in seen)


def test_percent_check_can_be_cancelled_while_reading(fake, session):
    import threading

    from cheatmaker.errors import ScanCancelled

    show_hp(fake, 250)
    sc = Scanner(session)
    sc.mark_full()
    before = sc.count
    ev = threading.Event()
    ev.set()
    show_hp(fake, 100)
    with pytest.raises(ScanCancelled):
        sc.percent_scan(40, 1.0, cancel=ev)
    assert sc.count == before and sc.full_mode


# -- QA round (BUG-02, BUG-03, BUG-04) ---------------------------------------------------
def test_one_way_each_keeps_the_narrowest_way_of_a_type():
    from cheatmaker.console.local_scan import LocalScanSession, one_way_each

    def part(vt, way, addrs):
        s = LocalScanSession(lambda a, n: b"")
        s.set_results(vt, [ScanHit(a, b"\0" * 4) for a in addrs], way)
        return s

    x10, pct, frac, whole = (
        part(F, X10, [1, 2, 3]),
        part(F, PERCENT, [2, 3]),
        part(F, FRACTION, [3]),
        part(W, X10, [1, 2]),
    )
    one_way_each([x10, pct, frac, whole])
    assert [h.address for h in frac.hits] == [3]
    assert [h.address for h in pct.hits] == [2]
    assert [h.address for h in x10.hits] == [1]
    assert [h.address for h in whole.hits] == [1, 2], "another type is another match"


def test_noted_percent_under_the_limit_keeps_nan_and_inf(fake, session):
    f32(fake, HEAP_BASE + 0x500, float("nan"))
    f32(fake, HEAP_BASE + 0x508, float("inf"))
    sc = Scanner(session)
    sc.mark_full(F)
    sc.last_pct = None  # as after a manual compare: no value when full is known
    sc.pc_limit = 100_000
    before = sc.count
    assert sc.percent_scan(40, 1.0) == before and sc.note == NOTED
    assert {HEAP_BASE + 0x500, HEAP_BASE + 0x508} <= {m.address for m in sc.matches(before)}


def test_zero_percent_then_went_up_over_the_limit_compares(fake, session):
    """Over the limit, 0% then 30% is a plain Went up (it can tell the direction)."""
    f32(fake, HEAP_BASE + 0x500, 1700.0)
    sc = Scanner(session)
    sc.pc_limit = 0  # always over the limit
    sc.mark_full(F)
    f32(fake, HEAP_BASE + 0x500, 0.0)
    sc.percent_scan(0, 1.0)
    f32(fake, HEAP_BASE + 0x500, 510.0)
    sc.percent_scan(30, 1.0)
    assert sc.history[-1] == "30% (as went up)" and sc.note == TOO_MANY
    assert HEAP_BASE + 0x500 in {m.address for m in sc.matches(5000)}


# -- QA BUG-05: a value that fits several ways at a low percent ---------------------------
def test_tie_prefers_the_way_that_agrees_with_full():
    every = [PERCENT, FRACTION, X10, OF_FULL]
    assert pf.tie(every, pf.Full(1700.0)) == OF_FULL, "0 at 0%: a shield out of 1,700"
    assert pf.tie([PERCENT, X10, OF_FULL], pf.Full(120.0)) == OF_FULL, "1.2 at 1% of 120"
    assert pf.tie([PERCENT, OF_FULL], pf.Full(100.0)) is PERCENT, "full is 100: a percent"
    assert pf.tie([FRACTION, OF_FULL], pf.Full(1.0)) is FRACTION
    assert pf.tie([X10], pf.Full(250.0)) is X10, "the only way it fits"
    assert pf.tie([PERCENT, X10]) is None, "no Full: left untied"
    assert pf.tie([X10]) is X10


def test_low_percent_fits_every_way_and_stays_untied_until_one_is_left(fake, session):
    """0 at 0% (no Full known) fits percent, fraction and x10: the match isn't tied to
    percent, so 300 at 30% (x10) is still found."""
    edge = HEAP_BASE + 0x500
    f32(fake, edge, 0.0)
    sc = Scanner(session)
    sc.first_scan(None, F)
    sc.percent_scan(0, 1.0)
    assert sc.note is None
    [m] = [m for m in sc.matches(5000) if m.address == edge]
    assert m.way is None
    f32(fake, edge, 300.0)
    sc.percent_scan(30, 1.0)
    [m] = [m for m in sc.matches(5000) if m.address == edge]
    assert m.way == X10


def test_zero_percent_in_a_full_search_ties_of_full(fake, session):
    edge = HEAP_BASE + 0x500
    f32(fake, edge, 1700.0)
    sc = Scanner(session)
    sc.mark_full(F)
    f32(fake, edge, 0.0)
    sc.percent_scan(0, 1.0)
    [m] = [m for m in sc.matches(5000) if m.address == edge]
    assert m.way == OF_FULL
