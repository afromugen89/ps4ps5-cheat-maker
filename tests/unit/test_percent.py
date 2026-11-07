"""Percent search (spec-health-finding AC-P1 to AC-P3): values, PC checks, FakeConsole
and the scanner."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.console.fake import FakeConsole
from cheatmaker.console.local_scan import way_parts
from cheatmaker.core import percent_filter as pf
from cheatmaker.core.scanner import TOO_MANY, Scanner
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


def test_full_over_limit_after_a_manual_compare_uses_changed(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.pc_limit = 1
    sc.mark_full()
    show_hp(fake, 200)
    sc.next_scan(Compare.DECREASED)
    sc.count = 5  # pretend still more than the limit
    show_hp(fake, 100)
    sc.percent_scan(40, 1.0)
    assert sc.history[-1] == "40% (as changed)"


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
    assert sc.recorded == 0 and sc.groups() == [], "too many to keep values"
    show_hp(fake, 100)
    sc.percent_scan(40, 1.0)
    assert sc.recorded == 1 and sc.groups() == [], "one scan isn't enough"
    show_hp(fake, 50)
    sc.percent_scan(20, 1.0)
    [g] = sc.groups()
    assert {m.address for m in g.members} == {MAX_HP, HP_FRAC} and g.head_likely
    assert sc.values[(MAX_HP, F)] == [100.0, 50.0]
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
