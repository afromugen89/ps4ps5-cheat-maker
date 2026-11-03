"""QA for "finding hard values" (AC-H1..AC-H6), 2026-11-03. See
qa/reports/2026-11-03-hard-values.md for the test plan (TC-Hxx) and bugs (BUG-Hn).

Tests marked ``xfail(strict=True)`` reproduce an open bug: they fail today and must
start passing (then drop the marker) once the bug is fixed.
"""

from __future__ import annotations

import math
import struct
import threading

import pytest

from cheatmaker.console import ps5debug_ng
from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient
from cheatmaker.console.uploader import Uploader
from cheatmaker.core.guide import FALLBACKS, Action, Guide, Scan, Shows
from cheatmaker.core.hit_tester import TEST_LIMIT, HitTester, suggest_value
from cheatmaker.core.scanner import Scanner, default_unit
from cheatmaker.core.session import Session
from cheatmaker.core.values import (
    SHOWN,
    Compare,
    ValueType,
    make_scale,
    matches,
    parse_filter,
    unit_of,
)
from cheatmaker.errors import ConnectionLost, ReadFailed, WriteFailed
from cheatmaker.settings import Settings
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.hit_test_dialog import HitTestDialog
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests.conftest import BAR_ADDR, GOLD_ADDR, HEALTH_ADDR, HEAP_BASE, PID, build_fake
from tests.ui.test_ui_flow import FakeFTP
from tests.unit.test_ps5debug_ng import FakeNgServer

F, D, I32, I8 = ValueType.FLOAT, ValueType.DOUBLE, ValueType.INT32, ValueType.INT8
SPARE = HEAP_BASE + 0x300  # free heap space in build_fake()


def f32(v: float) -> bytes:
    return struct.pack("<f", v)


def i32b(v: int) -> bytes:
    return struct.pack("<i", v)


def peek_i32(fake, addr: int) -> int:
    return struct.unpack("<i", fake.peek(PID, addr, 4))[0]


# -- fixtures ----------------------------------------------------------------------------
@pytest.fixture
def scan_fake():
    fake = build_fake()
    s = Session(lambda ip, port: fake)
    s.connect("192.168.1.50")
    s.refresh_game()
    return fake, Scanner(s)


def ng_scanner(**kw):
    srv = FakeNgServer(**kw)
    s = Session(lambda ip, port: Ps5DebugNgClient.connect("127.0.0.1", srv.port, timeout=3))
    s.connect("127.0.0.1")
    s.refresh_game()
    return srv, Scanner(s)


@pytest.fixture
def hv_fake():
    return build_fake()


@pytest.fixture
def hv_win(qtbot, hv_fake):
    FakeFTP.files, FakeFTP.dirs, FakeFTP.fail_connect = {}, {"/", "/data"}, False
    ctl = Controller(
        lambda ip, port: hv_fake,
        Settings.load(),
        TaskRunner(sync=True),
        uploader_factory=lambda: Uploader("1.2.3.4", ftp_factory=FakeFTP),
    )
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    yield w
    ctl.shutdown()


def connect(w) -> None:
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()


def search(w, text: str) -> None:
    w.find.number.setText(text)
    w.find.search_btn.click()


# == AC-H2 tolerance ======================================================================
@pytest.mark.parametrize(
    "text,unit", [("87", 1.0), ("87.5", 0.1), ("0.87", 0.01), ("-0.87", 0.01), ("87.50", 0.01),
                  ("1,000.5", 0.1), (".5", 0.1), ("87.", 1.0), ("1e3", 1.0), ("0", 1.0)]
)  # fmt: skip
def test_tc_h01_unit_of_last_typed_digit(text, unit):
    assert unit_of(text) == pytest.approx(unit)


@pytest.mark.parametrize("typed,mem,ok", [(87, 86.01, True), (87, 87.99, True), (-87, -86.5, True),
                                          (0, 0.99, True), (0, -0.99, True)])  # fmt: skip
def test_tc_h02_exact_float_inside_one_unit(typed, mem, ok):
    assert matches(F, Compare.EXACT, f32(mem), target=typed) is ok


# Regression: fixed in f08a14e (was open on 42d24b4).
@pytest.mark.parametrize("typed,mem", [(87, 88.0), (87, 86.0), (-87, -88.0), (0, 1.0)])
def test_tc_h03_exact_float_one_unit_away_is_not_a_match(typed, mem):
    # AC-H2: "A match is |memory - typed| < unit." NG FuzzyValue is < 1.0 too.
    assert not matches(F, Compare.EXACT, f32(mem), target=typed)


def test_tc_h04_filter_box_open_ends_and_negatives():
    lo, hi = parse_filter("87")
    assert lo > 86 and hi < 88 and lo <= 86.01 and hi >= 87.99
    lo, hi = parse_filter("-0.5")
    assert -0.6 < lo < -0.59 and -0.41 < hi < -0.4
    lo, hi = parse_filter("0.87")
    assert lo <= 0.865 <= hi and not lo <= 0.37 <= hi


def test_tc_h05_typed_decimal_first_scan_keeps_the_window(scan_fake):
    fake, sc = scan_fake
    fake.poke(PID, SPARE, f32(0.8749))
    fake.poke(PID, SPARE + 4, f32(0.37))
    assert sc.first_scan(0.87, unit=0.01) == 1
    [hit] = sc.session.require_client().scan_results(5)
    assert hit.address == SPARE and sc.vtype is F


def test_tc_h06_typed_decimal_still_tries_double(scan_fake):
    # AC-3.6 ("tries the likely types"); before this change 87.5 found the double.
    fake, sc = scan_fake
    fake.poke(PID, BAR_ADDR, f32(1.0))
    fake.poke(PID, SPARE, struct.pack("<d", 87.5))
    assert sc.first_scan(87.5, unit=0.1) == 1
    assert sc.vtype is D


def test_tc_h06b_typed_87_point_0_still_finds_whole_number(scan_fake):
    fake, sc = scan_fake
    fake.poke(PID, BAR_ADDR, f32(1.0))
    fake.poke(PID, HEALTH_ADDR, i32b(87))
    assert sc.first_scan(87.0, unit=0.1) >= 1
    assert HEALTH_ADDR in {h.address for h in sc.session.require_client().scan_results(50)}


# Regression: fixed in f08a14e (was open on 42d24b4).
def test_tc_h07_typed_decimal_on_ng_without_snapshot_engine():
    # AC-H2: "Typed decimals use NG ValueBetween [typed - unit, typed + unit]". Before this
    # change a typed 87.4 worked on every NG (FuzzyValue); 42d24b4 said "Update it".
    _srv, sc = ng_scanner(snapshot=False)
    assert sc.first_scan(87.4, unit=0.1) == 1


def test_tc_h08_typed_decimal_on_ng_with_snapshot_engine():
    srv, sc = ng_scanner(snapshot=True)
    assert sc.first_scan(87.4, unit=0.1) == 1
    assert srv.scan_compares[-1] == 4  # ValueBetween


# Regression: fixed in f08a14e (was open on 42d24b4).
def test_tc_h09_typed_decimal_next_scan_on_classic_ng_with_many_matches(monkeypatch):
    monkeypatch.setattr(ps5debug_ng, "MAX_READ_SPANS", 0)
    _srv, sc = ng_scanner(snapshot=False)
    assert sc.first_scan(87, F, unit=1) == 1
    # The player typed the number: "Type the number you see now" must not come back.
    assert sc.next_scan(Compare.EXACT, 87.4, unit=0.1) == 1


def test_tc_h10_default_unit_for_plain_numbers():
    assert default_unit(87) == 1 and default_unit(87.0) == 1
    assert default_unit(87.5) == pytest.approx(0.1)


def test_tc_h10b_default_unit_small_float():
    assert default_unit(1e-5) == pytest.approx(1e-5)


# == AC-H1 changed by =====================================================================
def test_tc_h11_float_changed_by_tolerance():
    prev, amt = f32(100.0), (15, 1.0)
    assert matches(F, Compare.DECREASED_BY, f32(85.0), prev, amt)
    assert matches(F, Compare.DECREASED_BY, f32(85.5), prev, amt)
    assert not matches(F, Compare.DECREASED_BY, f32(84.0), prev, amt)
    assert not matches(F, Compare.INCREASED_BY, f32(85.0), prev, amt), "wrong direction"
    assert matches(F, Compare.INCREASED_BY, f32(-85.0), f32(-100.0), amt), "negatives"
    assert not matches(F, Compare.INCREASED_BY, f32(1.0), f32(float("nan")), amt)
    assert not matches(F, Compare.INCREASED_BY, f32(1.0), f32(float("inf")), amt)
    assert matches(F, Compare.DECREASED_BY, f32(0.72), f32(0.87), (0.15, 0.01))


def test_tc_h12_int_changed_by_is_exact():
    p = i32b(100)
    assert matches(I32, Compare.DECREASED_BY, i32b(85), p, (15, 1.0))
    assert not matches(I32, Compare.DECREASED_BY, i32b(84), p, (15, 1.0))
    assert not matches(I32, Compare.DECREASED_BY, i32b(86), p, (15, 1.0))
    big = struct.pack("<q", 2**62)
    assert matches(ValueType.INT64, Compare.INCREASED_BY, big, struct.pack("<q", 2**62 - 10**12),
                   (10**12, 1.0))  # fmt: skip


# Regression: fixed in f08a14e (was open on 42d24b4: int(0.5) == 0 kept unchanged values).
@pytest.mark.parametrize("amount,unit", [(0.5, 0.1), (1.5, 0.1)])
def test_tc_h13_int_went_up_by_a_decimal_is_refused(scan_fake, amount, unit):
    fake, sc = scan_fake
    sc.first_scan(100, I32, unit=1)  # health + decoy
    fake.poke(PID, HEALTH_ADDR, i32b(101))
    with pytest.raises(ValueError, match="whole numbers"):
        sc.next_scan(Compare.INCREASED_BY, amount, unit=unit)
    assert sc.count == 2, "no scan ran"


def test_tc_h13b_int_x10_decimal_amount_is_whole_after_scale(scan_fake):
    fake, sc = scan_fake
    fake.poke(PID, SPARE, i32b(1000))
    sc.first_scan(100, unit=1, scale=make_scale("x10"))
    fake.poke(PID, SPARE, i32b(985))
    assert sc.next_scan(Compare.DECREASED_BY, 1.5, unit=0.1) == 1


@pytest.mark.parametrize("amount", [0, -4])
def test_tc_h14_amount_must_be_positive(scan_fake, amount):
    _fake, sc = scan_fake
    sc.first_scan(100, I32, unit=1)
    with pytest.raises(ValueError, match="more than 0"):
        sc.next_scan(Compare.DECREASED_BY, amount, unit=1)
    with pytest.raises(ValueError, match="how much"):
        sc.next_scan(Compare.DECREASED_BY, None)
    assert sc.count == 2


def test_tc_h15_changed_by_on_classic_ng_many_says_too_many(monkeypatch):
    monkeypatch.setattr(ps5debug_ng, "MAX_READ_SPANS", 0)
    _srv, sc = ng_scanner(snapshot=False)
    sc.first_scan(100, I32, unit=1)
    with pytest.raises(ValueError, match="too many to compare"):
        sc.next_scan(Compare.DECREASED_BY, 10, unit=1)


def test_tc_h16_changed_by_on_ng_snapshot_sends_scaled_amount():
    srv, sc = ng_scanner(snapshot=True)
    srv.fake.poke(PID, BAR_ADDR, f32(0.87))
    sc.first_scan(87, unit=1, scale=make_scale("fraction"))
    srv.fake.poke(PID, BAR_ADDR, f32(0.72))
    assert sc.next_scan(Compare.DECREASED_BY, 15, unit=1) >= 1
    assert BAR_ADDR in {h.address for h in sc.session.require_client().scan_results(50)}
    assert srv.scan_compares and srv.scan_compares[0] == 4, "first scan was ValueBetween"


# == AC-H3 scale ==========================================================================
def test_tc_h17_x10_typed_decimal_finds_whole_number(scan_fake):
    fake, sc = scan_fake
    fake.poke(PID, SPARE, i32b(875))
    assert sc.first_scan(87.5, unit=0.1, scale=make_scale("x10")) == 1
    assert sc.vtype is I32


def test_tc_h18_x10_window_excludes_neighbours_on_pc(scan_fake):
    fake, sc = scan_fake
    for i, v in enumerate([860, 861, 870, 879, 880]):
        fake.poke(PID, SPARE + 4 * i, i32b(v))
    sc.first_scan(87, unit=1, scale=make_scale("x10"))
    vals = sorted(
        struct.unpack("<i", h.value)[0] for h in sc.session.require_client().scan_results(50)
    )
    assert vals == [861, 870, 879]


# Regression: fixed in f08a14e (was open on 42d24b4).
def test_tc_h18b_x10_window_on_ng_matches_pc():
    srv, sc = ng_scanner(snapshot=True)
    for i, v in enumerate([860, 870, 880]):
        srv.fake.poke(PID, SPARE + 4 * i, i32b(v))
    sc.first_scan(87, unit=1, scale=make_scale("x10"))
    vals = sorted(
        struct.unpack("<i", h.value)[0] for h in sc.session.require_client().scan_results(50)
    )
    assert vals == [870]


def test_tc_h19_scale_applies_to_next_scans_and_resets(scan_fake):
    fake, sc = scan_fake
    fake.poke(PID, BAR_ADDR, f32(0.874))
    assert sc.first_scan(87, unit=1, scale=make_scale("fraction")) == 1
    assert sc.history == ["87 (a fraction of 100)"] and sc.vtype is F
    fake.poke(PID, BAR_ADDR, f32(0.801))
    assert sc.next_scan(Compare.EXACT, 80, unit=1) == 1
    fake.poke(PID, BAR_ADDR, f32(0.651))
    assert sc.next_scan(Compare.DECREASED_BY, 15, unit=1) == 1
    sc.reset()
    assert sc.scale is SHOWN


def test_tc_h20_lost_search_forgets_the_scale(scan_fake):
    _fake, sc = scan_fake
    sc.first_scan(87, unit=1, scale=make_scale("x10"))
    lost = []
    sc.on_lost.append(lambda: lost.append(1))
    sc.session.take_search()
    assert lost and sc.scale is SHOWN and not sc.active
    with pytest.raises(ValueError, match="Start a new search"):
        sc.next_scan(Compare.EXACT, 87, unit=1)


def test_tc_h21_fraction_max_validation():
    for bad in (0, -5):
        with pytest.raises(ValueError, match="more than 0"):
            make_scale("fraction", bad)
    assert make_scale("fraction", 250).factor == pytest.approx(1 / 250)
    with pytest.raises(ValueError):
        make_scale("x7")


def test_tc_h22_negative_values_with_scale(scan_fake):
    fake, sc = scan_fake
    fake.poke(PID, SPARE, i32b(-50))
    assert sc.first_scan(-5, unit=1, scale=make_scale("x10")) == 1


def test_tc_h23_huge_scaled_value_uses_8_bytes(scan_fake):
    fake, sc = scan_fake
    fake.poke(PID, SPARE, struct.pack("<q", 3_000_000_000))
    assert sc.first_scan(3_000_000, unit=1, scale=make_scale("x1000")) == 1


# == AC-H4 stand still ====================================================================
def test_tc_h24_stand_still_stops_after_two_quiet_scans(scan_fake):
    _fake, sc = scan_fake
    sc.first_scan(None, I32)
    sleeps = []
    sc.stand_still(sleep=sleeps.append)
    assert sc.history.count("unchanged") == 2 and sleeps == [2.0]


def test_tc_h25_stand_still_runs_5_when_counts_keep_dropping(scan_fake):
    fake, sc = scan_fake
    sc.first_scan(None, I32)
    slot = iter(range(64))

    def moving():  # one more value moves on its own before every scan
        fake.poke(PID, SPARE + 0x100 + 4 * next(slot), i32b(12345))

    moving()
    sleeps = []
    sc.stand_still(sleep=lambda s: (sleeps.append(s), moving()))
    assert sc.history.count("unchanged") == 5 and len(sleeps) == 4


def test_tc_h26_stand_still_cancel_and_one_left(scan_fake):
    fake, sc = scan_fake
    sc.first_scan(100, I32, unit=1)
    cancel = threading.Event()
    cancel.set()
    assert sc.stand_still(cancel=cancel, sleep=lambda s: None) == 2
    assert "unchanged" not in sc.history
    fake.poke(PID, HEALTH_ADDR, i32b(90))
    sc.next_scan(Compare.EXACT, 90, unit=1)
    sc.stand_still(sleep=lambda s: None)
    assert sc.history.count("unchanged") == 1, "stops at 1 left"
    with pytest.raises(ValueError):
        Scanner(sc.session).stand_still()


# == AC-H5 guide ==========================================================================
def test_tc_h27_fallbacks_skip_what_was_tried():
    g = Guide()
    g.start(Shows.NUMBER)
    g.note_tried("x10")  # the player tried x10 by hand first
    g.note_tried("shown")
    seen = []
    while (s := g.after_scan(Scan.FIRST_EXACT, 0)).action is Action.TRY_NEXT:
        seen.append(s.fallback)
        g.note_tried(s.fallback)
    assert seen == ["fraction", "x100", "unknown"]
    assert [k for k, _ in FALLBACKS] == ["shown", "fraction", "x10", "x100", "unknown"]
    g.note_tried("x1000")  # not a fallback: ignored
    assert "x1000" not in g.tried


def test_tc_h28_guide_counts_and_new_value():
    g = Guide()
    g.start(Shows.NOTHING)
    assert g.after_scan(Scan.FIRST_UNKNOWN, 10**8).action is Action.MOVE
    assert g.after_scan(Scan.COMPARE, 10**7).action is Action.MOVE
    assert g.after_scan(Scan.STAND_STILL, 10**6).action is Action.STAND_STILL
    assert g.after_scan(Scan.COMPARE, 2).action is Action.TEST_HITS
    assert "1 questions" in g.after_scan(Scan.COMPARE, 2).text
    assert g.after_scan(Scan.COMPARE, 1).action is Action.MAKE_CHEAT
    assert g.after_scan(Scan.COMPARE, TEST_LIMIT + 1).action is Action.STAND_STILL
    g.note_tried("shown")
    g.start(Shows.NUMBER)
    assert g.tried == [] and g.compares_in_row == 0


# Regression: fixed in f08a14e (was open on 42d24b4).
def test_tc_h29_many_after_first_search_suggests_narrowing():
    # AC-H5: "More than the show limit: for a number, Change it in the game, then type the
    # new number"; Test the hits only once "some still change together".
    g = Guide()
    g.start(Shows.NUMBER)
    assert g.after_scan(Scan.FIRST_EXACT, 300).action is Action.TYPE_NEW


def test_tc_h30_ui_guide_highlight_and_try_order(hv_win, hv_fake):
    hv_fake.poke(PID, BAR_ADDR, f32(5.0))
    hv_fake.poke(PID, SPARE, i32b(8700))  # 87 stored x100
    connect(hv_win)
    f = hv_win.find
    f.shows_btns[Shows.NUMBER].click()
    search(hv_win, "87")
    assert f.count.text() == "0 matches"
    tried = []
    while f.suggestion.action is Action.TRY_NEXT and f.count.text() == "0 matches":
        tried.append(f.suggestion.fallback)
        assert "font-weight: bold" in f.try_btn.styleSheet()
        f.try_btn.click()
    assert tried == ["fraction", "x10", "x100"]
    assert f.count.text() == "1 matches" and f.rows[0].address == SPARE
    assert f.ctl.scanner.history == ["87 (x100)"]


def test_tc_h31_ui_skipped_question_and_start_over_resets_guide(hv_win):
    connect(hv_win)
    f = hv_win.find
    search(hv_win, "4242")  # nothing has it
    assert f.guide.shows is Shows.NUMBER and f.suggestion.fallback == "fraction"
    f.new_btn.setEnabled(True)
    f.do_reset()
    assert f.guide.tried == [] and f.suggestion is None and not f.try_btn.isVisibleTo(f)


def test_tc_h32_try_it_with_empty_box_ends_search_and_buttons_follow(hv_win, hv_fake):
    hv_fake.poke(PID, BAR_ADDR, f32(5.0))
    connect(hv_win)
    f = hv_win.find
    f.shows_btns[Shows.NUMBER].click()
    search(hv_win, "100")
    f.do_compare(Compare.INCREASED)  # nothing went up: 0 matches, "Start over and try ..."
    assert f.suggestion.action is Action.START_OVER and f.suggestion.fallback == "fraction"
    f.number.clear()
    f.try_btn.click()  # asks for the number; the old search is gone
    assert not f.ctl.scanner.active and not f.up_btn.isEnabled()


def test_tc_h32b_second_try_it_with_empty_box_turns_compares_off(hv_win, hv_fake):
    hv_fake.poke(PID, BAR_ADDR, f32(5.0))
    connect(hv_win)
    f = hv_win.find
    f.shows_btns[Shows.NUMBER].click()
    search(hv_win, "4242")  # nothing as shown: Try it (fraction) turns Advanced on
    f.try_btn.click()
    assert f.advanced.isChecked() and f.ctl.scanner.active
    assert f.suggestion.fallback == "x10"
    f.number.clear()
    f.try_btn.click()
    assert not f.ctl.scanner.active
    assert not f.up_btn.isEnabled() and not f.still_btn.isEnabled()


def test_tc_h33_unknown_search_uses_the_scale(hv_win, hv_fake):
    # AC-H3: the scale applies to every typed number of that search.
    hv_fake.poke(PID, BAR_ADDR, f32(0.874))
    connect(hv_win)
    f = hv_win.find
    f.advanced.setChecked(True)
    f.scale.setCurrentIndex(1)  # fraction of 100
    f.unknown_btn.click()
    hv_fake.poke(PID, BAR_ADDR, f32(0.724))
    f.by_amount.setText("15")
    f.down_by_btn.click()
    assert BAR_ADDR in {h.address for h in f.ctl.session.require_client().scan_results(500)}


# == UI enablement ========================================================================
def test_tc_h34_new_buttons_enablement(hv_win, hv_fake):
    f = hv_win.find
    new = (f.up_by_btn, f.down_by_btn, f.still_btn, f.test_btn, *f.shows_btns.values())
    assert not any(b.isEnabled() for b in new), "not connected"
    connect(hv_win)
    assert all(b.isEnabled() for b in f.shows_btns.values())
    assert not any(b.isEnabled() for b in (f.up_by_btn, f.down_by_btn, f.still_btn, f.test_btn))
    search(hv_win, "100")  # 2
    assert all(b.isEnabled() for b in (f.up_by_btn, f.down_by_btn, f.still_btn, f.test_btn))
    hv_fake.poke(PID, HEALTH_ADDR, i32b(90))
    search(hv_win, "90")  # 1
    assert not f.test_btn.isEnabled() and f.still_btn.isEnabled()
    f.do_reset()
    assert not any(b.isEnabled() for b in (f.up_by_btn, f.down_by_btn, f.still_btn, f.test_btn))
    f.unknown_btn.click()  # thousands
    assert f.ctl.scanner.count > TEST_LIMIT and not f.test_btn.isEnabled()
    f.ctl.session.take_search()  # search lost
    assert not any(b.isEnabled() for b in (f.up_by_btn, f.down_by_btn, f.still_btn, f.test_btn))


def test_tc_h35_scale_max_enabled_only_for_fraction_and_bad_max(hv_win):
    shown = []
    hv_win.ctl.message.connect(shown.append)
    connect(hv_win)
    f = hv_win.find
    f.advanced.setChecked(True)
    for i, (_l, kind) in enumerate([("", "shown"), ("", "fraction"), ("", "x10")]):
        f.scale.setCurrentIndex(i)
        assert f.scale_max.isEnabled() is (kind == "fraction")
    f.scale.setCurrentIndex(1)
    for bad in ("0", "-3", "abc"):
        f.scale_max.setText(bad)
        search(hv_win, "87")
        assert not f.ctl.scanner.active and shown


def test_tc_h36_stand_still_ui_hint(hv_win, monkeypatch):
    from cheatmaker.core import scanner

    monkeypatch.setattr(scanner, "STILL_INTERVAL_S", 0)
    connect(hv_win)
    search(hv_win, "100")
    hints_seen = []
    f = hv_win.find
    orig = f._run_scan
    monkeypatch.setattr(f, "_run_scan", lambda fn: (hints_seen.append(f.hint.text()), orig(fn)))
    f.still_btn.click()
    assert hints_seen == [hints.FIND_STILL]
    assert f.ctl.scanner.history[-1] == "unchanged"


# == AC-H6 Test the hits (core) ===========================================================
def _put(fake, addrs, v=100):
    for a in addrs:
        fake.poke(PID, a, i32b(v))


@pytest.mark.parametrize("answer", [True, False, None])
def test_tc_h37_every_answer_restores(fake, answer):
    addrs = [SPARE + 4 * i for i in range(5)]
    _put(fake, addrs)
    t = HitTester(fake, PID, I32, addrs)
    half = t.write_test(7)
    assert len(half) == 3 and all(peek_i32(fake, a) == 7 for a in half)
    t.answer(answer)
    assert all(peek_i32(fake, a) == 100 for a in addrs)
    assert len(t.candidates) == {True: 3, False: 2, None: 5}[answer]
    assert t.done is (answer is None) and t.result is None


@pytest.mark.parametrize("n", [3, 5, 9, 17, 255, 499])
def test_tc_h38_odd_counts_find_any_position(fake, n):
    addrs = [SPARE + 4 * i for i in range(n)]
    for real in (addrs[0], addrs[-1], addrs[n // 2]):
        _put(fake, addrs)
        t = HitTester(fake, PID, I32, addrs)
        while not t.done:
            t.answer(real in t.write_test(1))
        assert t.result == real and t.rounds <= math.ceil(math.log2(n))
        assert all(peek_i32(fake, a) == 100 for a in addrs)


def test_tc_h39_limit_500(fake):
    HitTester(fake, PID, I32, [SPARE + 4 * i for i in range(TEST_LIMIT)])
    with pytest.raises(ValueError, match="2 to 500"):
        HitTester(fake, PID, I32, [SPARE + 4 * i for i in range(TEST_LIMIT + 1)])
    with pytest.raises(ValueError):
        HitTester(fake, PID, I32, [SPARE, SPARE])  # duplicates count once


def test_tc_h40_stop_while_waiting_restores_far_apart_addresses(fake):
    from tests.conftest import STATIC_ADDR

    t = HitTester(fake, PID, I32, [STATIC_ADDR, HEALTH_ADDR, GOLD_ADDR, SPARE])
    before = {a: peek_i32(fake, a) for a in t.candidates}
    t.write_test(999)
    t.stop()
    assert {a: peek_i32(fake, a) for a in before} == before
    assert t.done and t.result is None and not t.waiting


def test_tc_h41_failed_read_writes_nothing(fake, monkeypatch):
    t = HitTester(fake, PID, I32, [HEALTH_ADDR, GOLD_ADDR, SPARE])
    _put(fake, [SPARE])
    real_read = fake.read

    def bad(pid, addr, size):
        raise ReadFailed("gone")

    monkeypatch.setattr(fake, "read", bad)
    with pytest.raises(ReadFailed):
        t.write_test(5)
    assert peek_i32(fake, HEALTH_ADDR) == 100 and not t.waiting and t.rounds == 0
    monkeypatch.setattr(fake, "read", real_read)
    t.write_test(5)  # can try again
    assert t.waiting


def test_tc_h42_failed_restore_puts_back_the_rest(fake, monkeypatch):
    addrs = [SPARE + 4 * i for i in range(4)]
    _put(fake, addrs)
    t = HitTester(fake, PID, I32, addrs)
    half = t.write_test(7)
    real_write = fake.write

    def flaky(pid, addr, data):
        if addr == half[0]:
            raise ConnectionLost("link dropped")
        real_write(pid, addr, data)

    monkeypatch.setattr(fake, "write", flaky)
    with pytest.raises(ConnectionLost):
        t.answer(True)
    assert peek_i32(fake, half[1]) == 100, "the rest went back"
    assert peek_i32(fake, half[0]) == 7 and t.waiting, "the failed one stays noted"
    assert len(t.candidates) == 4, "answer not taken yet"
    monkeypatch.setattr(fake, "write", real_write)
    t.answer(True)  # retry once the link is back
    assert all(peek_i32(fake, a) == 100 for a in addrs) and t.candidates == half


# Regression: fixed in f08a14e (was open on 42d24b4).
def test_tc_h43_write_that_lands_but_errors_is_put_back(fake, monkeypatch):
    addrs = [SPARE + 4 * i for i in range(4)]
    _put(fake, addrs)
    t = HitTester(fake, PID, I32, addrs)
    real_write = fake.write
    n = []

    def lands_then_times_out(pid, addr, data):
        n.append(addr)
        real_write(pid, addr, data)
        if len(n) == 2:
            raise ConnectionLost("no reply after the write")

    monkeypatch.setattr(fake, "write", lands_then_times_out)
    with pytest.raises(ConnectionLost):
        t.write_test(7)
    assert all(peek_i32(fake, a) == 100 for a in addrs)


def test_tc_h44_suggest_value_bounds():
    assert suggest_value(I8, 127) == 116 and suggest_value(I8, -128) == -117
    assert suggest_value(ValueType.INT64, 2**63 - 1) == 2**63 - 12
    assert suggest_value(F, -0.0) == 11.0 and suggest_value(F, float("inf")) == 50.0
    assert suggest_value(D, -10.0) == -5.0


# == AC-H6 Test the hits (UI) =============================================================
def _open(win, monkeypatch, play):
    monkeypatch.setattr(HitTestDialog, "exec", lambda dlg: play(dlg) or 0)
    win.find.test_btn.click()


@pytest.mark.parametrize("how", ["not sure", "stop", "close"])
def test_tc_h45_dialog_ends_put_values_back(hv_win, hv_fake, monkeypatch, how):
    connect(hv_win)
    search(hv_win, "100")
    out = {}

    def play(dlg):
        dlg.write_btn.click()
        assert any(peek_i32(hv_fake, a) == 111 for a in (HEALTH_ADDR, HEAP_BASE + 0x200))
        assert not dlg.write_btn.isEnabled() and dlg.yes_btn.isEnabled()
        if how == "not sure":
            dlg.unsure_btn.click()
            out["hint"] = dlg.hint.text()
            assert not dlg.yes_btn.isEnabled() and dlg.close_btn.text() == "Close"
        elif how == "stop":
            dlg.close_btn.click()
        else:
            dlg.reject()  # window X / Escape

    _open(hv_win, monkeypatch, play)
    assert peek_i32(hv_fake, HEALTH_ADDR) == 100 and peek_i32(hv_fake, HEAP_BASE + 0x200) == 100
    if how == "not sure":
        assert out["hint"] == hints.TEST_HITS_STOPPED


@pytest.mark.parametrize("everywhere", [True, False])
def test_tc_h46_refused_write_does_not_ask(hv_win, hv_fake, monkeypatch, everywhere):
    shown = []
    hv_win.ctl.message.connect(shown.append)
    connect(hv_win)
    search(hv_win, "100")  # HEALTH_ADDR and the decoy; half A is HEALTH_ADDR
    real_write = hv_fake.write
    out = {}

    def refuse(pid, addr, data):
        if everywhere or addr == HEALTH_ADDR:
            raise WriteFailed("the console refused the write (fake)")
        real_write(pid, addr, data)

    def play(dlg):
        monkeypatch.setattr(hv_fake, "write", refuse)
        dlg.write_btn.click()
        out["asked"] = dlg.yes_btn.isEnabled() or dlg.no_btn.isEnabled()
        out["can_write"] = dlg.write_btn.isEnabled()
        dlg.no_btn.click()  # the player answers the question they see
        out["left"] = list(dlg.tester.candidates)

    _open(hv_win, monkeypatch, play)
    assert shown, "a plain message"
    assert not out["asked"] and out["can_write"], "nothing was written: no question"
    assert len(out["left"]) == 2, "a question never asked must not narrow"


def test_tc_h46b_dialog_bad_test_values(hv_win, hv_fake, monkeypatch):
    shown = []
    hv_win.ctl.message.connect(shown.append)
    connect(hv_win)
    search(hv_win, "100")

    def play(dlg):
        for bad in ("abc", "3000000000", ""):  # not a number / doesn't fit 4 bytes / empty
            n = len(shown)
            dlg.value.setText(bad)
            dlg.write_btn.click()
            assert len(shown) == n + 1 and not dlg.tester.waiting and dlg.write_btn.isEnabled()

    _open(hv_win, monkeypatch, play)
    assert peek_i32(hv_fake, HEALTH_ADDR) == 100


def test_tc_h47_warning_once_and_winner_selected(hv_win, hv_fake, monkeypatch):
    connect(hv_win)
    search(hv_win, "100")
    warned = []

    def play(dlg):
        warned.append(dlg.warning.isVisibleTo(dlg))
        while not dlg.tester.done:
            dlg.write_btn.click()
            (dlg.yes_btn if HEALTH_ADDR in dlg.tester.testing else dlg.no_btn).click()

    _open(hv_win, monkeypatch, play)
    _open(hv_win, monkeypatch, play)
    assert warned == [True, False]
    f = hv_win.find
    assert f.selected().address == HEALTH_ADDR and f.set_btn.isEnabled()
    assert sum(1 for m in f.rows if m.address == HEALTH_ADDR) >= 1


def test_tc_h48_decimal_test_value_on_whole_number(hv_win, hv_fake, monkeypatch):
    connect(hv_win)
    search(hv_win, "100")
    out = {}

    def play(dlg):
        dlg.value.setText("1.5")
        dlg.write_btn.click()
        out["waiting"] = dlg.tester.waiting
        out["hint"] = dlg.hint.text()
        out["mem"] = peek_i32(hv_fake, dlg.tester.testing[0])
        dlg.reject()

    _open(hv_win, monkeypatch, play)
    # Either refuse 1.5 for a whole number, or ask about the value really written.
    assert not out["waiting"] or f"change to {out['mem']}?" in out["hint"], out


def test_tc_h48b_test_the_hits_in_shown_units_with_x10(hv_win, hv_fake, monkeypatch):
    for a in (SPARE, SPARE + 0x40):
        hv_fake.poke(PID, a, i32b(1000))  # shown 100, stored x10
    connect(hv_win)
    f = hv_win.find
    f.advanced.setChecked(True)
    f.scale.setCurrentIndex(2)  # x10
    search(hv_win, "100")
    assert f.ctl.scanner.count == 2
    out = {}

    def play(dlg):
        out["prefill"] = dlg.value.text()
        dlg.write_btn.click()
        out["mem"] = peek_i32(hv_fake, dlg.tester.testing[0])
        out["hint"] = dlg.hint.text()
        dlg.yes_btn.click()

    _open(hv_win, monkeypatch, play)
    assert out["prefill"] == "111" and out["mem"] == 1110 and "change to 111?" in out["hint"]
    assert peek_i32(hv_fake, SPARE) == 1000 and f.selected().address == SPARE


def test_tc_h49_lost_link_during_test(hv_win, hv_fake, monkeypatch):
    connect(hv_win)
    search(hv_win, "100")

    def play(dlg):
        def drop(pid, addr, data):
            raise ConnectionLost("gone")

        monkeypatch.setattr(hv_fake, "write", drop)
        dlg.write_btn.click()
        dlg.reject()  # Stop: can't put back, must not raise out of the dialog

    _open(hv_win, monkeypatch, play)
    assert not hv_win.ctl.session.connected, "the usual lost-connection handling ran"
    assert peek_i32(hv_fake, HEALTH_ADDR) == 100


def test_tc_h50_guide_test_hits_once_narrowing_is_stuck():
    g = Guide(show_limit=10)
    g.start(Shows.NUMBER)
    assert g.after_scan(Scan.FIRST_EXACT, 300).action is Action.TYPE_NEW
    assert g.after_scan(Scan.NEXT_EXACT, 120).action is Action.TYPE_NEW
    assert g.after_scan(Scan.NEXT_EXACT, 120).action is Action.TEST_HITS, "dropped nothing"
    for _ in range(3):  # a number player is never sent to Stand still
        assert g.after_scan(Scan.COMPARE, 10**6).action is not Action.STAND_STILL
    assert g.after_scan(Scan.COMPARE, 8).action is Action.TEST_HITS, "short list"


def test_tc_h51_stop_mid_round_puts_back_what_was_written(fake, monkeypatch):
    addrs = [SPARE + 4 * i for i in range(10)]
    _put(fake, addrs)
    t = HitTester(fake, PID, I32, addrs)
    real_write = fake.write
    n = []

    def write(pid, addr, data):
        real_write(pid, addr, data)
        n.append(addr)
        if len(n) == 2:
            t.request_stop()  # the player pressed Stop while the round was writing

    monkeypatch.setattr(fake, "write", write)
    assert t.write_test(7) == []
    assert all(peek_i32(fake, a) == 100 for a in addrs)
    assert t.done and t.result is None and not t.waiting
