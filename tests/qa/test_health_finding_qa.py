"""QA round for percent search, It's full now, About X% and grouped copies
(docs/spec-health-finding.md). Report: qa/reports/2026-11-07-health-finding.md.

Bugs are strict xfail tests named after their BUG id; remove the marker once fixed.
FakeConsole for the scanner, the NG fake server for the PS5Debug-NG client, and
pytest-qt (offscreen) for the Find view.
"""

from __future__ import annotations

import struct

import pytest

from cheatmaker.console.fake import PROT_R, PROT_X, FakeConsole
from cheatmaker.core.grouping import COPIES, LIKELY, STILL, group_matches
from cheatmaker.core.scanner import NOTED, TOO_MANY, Scanner
from cheatmaker.core.session import Session
from cheatmaker.core.values import FRACTION, OF_FULL, PERCENT, X10, Compare, ValueType
from cheatmaker.settings import Settings
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests.conftest import HEAP_BASE, PID, build_fake

F, W = ValueType.FLOAT, ValueType.INT32
HP = HEAP_BASE + 0x400  # health, not out of 100
HP_FRAC = HEAP_BASE + 0x410  # the same health as a fraction, next to it
EDGE = HEAP_BASE + 0x500  # a lone value for boundary checks


def f32(fake: FakeConsole, addr: int, v: float) -> None:
    fake.poke(PID, addr, struct.pack("<f", v))


def i32(fake: FakeConsole, addr: int, v: int) -> None:
    fake.poke(PID, addr, struct.pack("<i", v))


def connect(fake: FakeConsole) -> Session:
    s = Session(lambda ip, port: fake)
    s.connect("192.168.1.50")
    s.refresh_game()
    return s


def addrs(sc: Scanner, n: int = 5000) -> list[int]:
    return [m.address for m in sc.matches(n)]


def show_hp(fake: FakeConsole, hp: float, max_hp: float = 250) -> None:
    f32(fake, HP, hp)
    f32(fake, HP_FRAC, hp / max_hp)


# -- TC-01..03: percent_pc_limit boundary ----------------------------------------------
def test_tc01_full_search_at_exactly_the_limit_checks_on_the_pc(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    n = sc.mark_full()
    sc.pc_limit = n  # exactly at the limit: fits
    show_hp(fake, 100)
    assert sc.percent_scan(40, 1.0) == 2
    assert sc.note is None and sc.history[-1] == "40%"


def test_tc02_full_search_one_over_the_limit_runs_as_went_down(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    n = sc.mark_full()
    sc.pc_limit = n - 1
    show_hp(fake, 100)
    sc.percent_scan(40, 1.0)
    assert sc.note == TOO_MANY and sc.history[-1] == "40% (as went down)"
    assert HP in addrs(sc) and HP_FRAC in addrs(sc)


def test_tc03_plain_search_limit_boundary(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.first_scan(None, F)
    show_hp(fake, 100)
    n = sc.next_scan(Compare.DECREASED)
    sc.pc_limit = n - 1
    with pytest.raises(ValueError, match="too many to check by percent"):
        sc.percent_scan(40, 1.0)
    assert sc.count == n, "the refused percent left the search as it was"
    sc.pc_limit = n
    assert sc.percent_scan(40, 1.0) >= 1 and HP_FRAC in addrs(sc)
    assert sc.percent_search and not sc.full_mode


# -- TC-04..06: Full chain with heals and About buttons --------------------------------
def test_tc04_full_chain_over_the_limit_with_a_heal(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.pc_limit = 1  # always too many: every % runs as a plain compare
    sc.mark_full()
    show_hp(fake, 100)
    sc.percent_scan(40, 1.0)
    show_hp(fake, 162.5)
    sc.percent_scan(65, 1.0)
    show_hp(fake, 125)
    sc.about_scan(50)
    assert sc.history[1:] == [
        "40% (as went down)",
        "65% (as went up)",
        "about 50% (as went down)",
    ]
    assert {HP, HP_FRAC} <= set(addrs(sc))


def test_tc05_full_chain_on_the_pc_with_heals_and_about(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.mark_full()
    for hp, act in (
        (100, lambda: sc.percent_scan(40, 1.0)),
        (162.5, lambda: sc.percent_scan(65, 1.0)),  # heal
        (125, lambda: sc.about_scan(50)),
        (200, lambda: sc.about_scan(75)),  # heal
        (62.5, lambda: sc.about_scan(25)),
    ):
        show_hp(fake, hp)
        act()
        assert set(addrs(sc)) == {HP, HP_FRAC}, sc.history
    ways = {m.address: m.way for m in sc.matches(10)}
    assert ways == {HP: OF_FULL, HP_FRAC: FRACTION}


def test_tc06_about_never_keeps_plain_percent_or_x10_matches(fake, session):
    """AC-P3: About uses only of-full and fraction ways for untagged matches."""
    f32(fake, EDGE, 250.0)  # a value that is 50 now as a plain percent (but 20% of full)
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.mark_full()
    f32(fake, EDGE, 50.0)
    show_hp(fake, 125)
    sc.about_scan(50)
    assert EDGE not in addrs(sc) and {HP, HP_FRAC} <= set(addrs(sc))


# -- TC-07..08: "noted" scans ----------------------------------------------------------
def test_tc07_same_percent_twice_over_the_limit_is_noted_then_narrows(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.pc_limit = 1
    sc.mark_full()
    show_hp(fake, 100)
    sc.percent_scan(40, 1.0)
    before = sc.count
    sc.percent_scan(40, 1.0)  # same % again: no direction
    assert sc.note == NOTED and sc.count == before and sc.history[-1] == "40% (noted)"
    show_hp(fake, 50)
    sc.percent_scan(20, 1.0)
    assert sc.history[-1] == "20% (as went down)" and HP in addrs(sc)


def test_tc08_noted_after_manual_compare_keeps_the_real_value(fake, session):
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.mark_full()
    show_hp(fake, 200)
    sc.next_scan(Compare.DECREASED)
    show_hp(fake, 100)
    sc.next_scan(Compare.DECREASED)
    assert sc.last_pct is None
    sc.percent_scan(40, 1.0)
    assert sc.note == NOTED and HP in addrs(sc)
    show_hp(fake, 50)
    assert sc.percent_scan(20, 1.0) == 2


# -- TC-09..11: restricted (PC-side) search, then plain tools --------------------------
def _restricted(fake, session) -> Scanner:
    show_hp(fake, 250)
    sc = Scanner(session)
    sc.mark_full()
    show_hp(fake, 100)
    sc.percent_scan(40, 1.0)
    return sc


def test_tc09_plain_compares_after_scan_restrict_keep_the_ways(fake, session):
    sc = _restricted(fake, session)
    show_hp(fake, 90)
    assert sc.next_scan(Compare.DECREASED) == 2
    show_hp(fake, 120)
    assert sc.next_scan(Compare.INCREASED) == 2
    assert sc.next_scan(Compare.UNCHANGED) == 2
    show_hp(fake, 121)
    assert sc.next_scan(Compare.CHANGED) == 2
    ways = {m.address: m.way for m in sc.matches(10)}
    assert ways == {HP: OF_FULL, HP_FRAC: FRACTION}


def test_tc10_stand_still_after_scan_restrict(fake, session):
    sc = _restricted(fake, session)
    n = sc.stand_still(rounds=3, interval_s=0, sleep=lambda _s: None)
    assert n == 2 and sc.full_mode and sc.last_pct == 40, "Same keeps the shown percent"


def test_tc11_scan_file_has_stored_as_column_after_full_chain(fake, session):
    sc = _restricted(fake, session)
    text = sc.scan_log.last_file.read_text(encoding="utf-8")
    assert "# Stored as: of full, fraction" in text
    assert "\tStored as" in text.splitlines()[text.splitlines().index("") + 1]
    assert f"0x{HP:010X}\t100\tof full" in text or f"0x{HP:010X}\t100.0\tof full" in text


def test_tc12_plain_number_search_file_has_no_stored_as(fake, session):
    sc = Scanner(session)
    sc.first_scan(100)
    text = sc.scan_log.last_file.read_text(encoding="utf-8")
    assert "Stored as" not in text and sc.history == ["100"] and not sc.has_ways


# -- TC-13..14: Start over and game change ---------------------------------------------
def test_tc13_game_change_clears_full_mode_and_groups(fake, session):
    sc = _restricted(fake, session)
    show_hp(fake, 50)
    sc.percent_scan(20, 1.0)
    show_hp(fake, 25)
    sc.percent_scan(10, 1.0)
    assert sc.groups()
    fake.remove_process(PID)
    fake.add_process(88, "eboot.bin", "PPSA09999", "Other")
    fake.add_region(88, "eboot.bin", 0x400000, b"\0" * 16, PROT_R | PROT_X)
    session.refresh_game()
    assert not sc.active and not sc.full_mode and not sc.percent_search
    assert sc.full == {} and sc.values == {} and sc.groups() == [] and sc.last_pct is None
    assert sc.parts == {} and not sc.has_ways


def test_tc14_start_over_then_plain_number_is_a_plain_search(fake, session):
    sc = _restricted(fake, session)
    sc.reset()
    sc.first_scan(100)
    assert sc.history == ["100"] and not sc.percent_search and not sc.full_mode
    i32(fake, HEAP_BASE + 0x40, 90)
    assert sc.next_scan(Compare.EXACT, 90) == 1 and sc.history == ["100", "90"]


# -- TC-15..17: int vs float parts, 4-byte decimal edges -------------------------------
def test_tc15_decimal_percent_searches_whole_numbers_only_as_x10(fake, session):
    i32(fake, EDGE, 405)
    i32(fake, EDGE + 8, 40)
    sc = Scanner(session)
    sc.percent_scan(40.5, 0.1)
    assert (W, X10) in sc.parts and (W, PERCENT) not in sc.parts
    assert EDGE in addrs(sc) and EDGE + 8 not in addrs(sc)


@pytest.mark.parametrize(
    "stored,kept",
    [(410.0, False), (390.0, False), (409.99, True), (41.0, False), (39.0001, True)],
)
def test_tc16_x10_and_percent_edges_console_and_pc_agree(fake, session, stored, kept):
    f32(fake, EDGE, stored)
    sc = Scanner(session)
    sc.percent_scan(40, 1.0, F)  # console (FakeConsole) path
    assert (EDGE in addrs(sc)) is kept
    from cheatmaker.core import percent_filter as pf
    from cheatmaker.core.values import f32 as to32

    assert (pf.check(F, None, to32(stored), None, pf.Target(40, 1.0)) is not None) is kept


def test_tc17_of_full_tolerance_is_inclusive_one_point(fake, session):
    for now, kept in ((102.5, True), (97.5, True), (105.0, False), (95.0, False)):
        f32(fake, EDGE, 250.0)
        sc = Scanner(session)
        sc.mark_full(F)
        f32(fake, EDGE, now)
        sc.percent_scan(40, 1.0)
        assert (EDGE in addrs(sc)) is kept, now


# -- TC-18..19: PS4 game, NG fake server -----------------------------------------------
def test_tc18_ps4_game_full_chain_same_as_ps5():
    fake = build_fake("CUSA01234")
    s = connect(fake)
    assert s.game.is_ps4
    sc = Scanner(s)
    show_hp(fake, 250)
    sc.mark_full()
    for hp, pct in ((100, 40), (50, 20), (25, 10)):
        show_hp(fake, hp)
        sc.percent_scan(pct, 1.0)
    [g] = sc.groups()
    assert g.label == LIKELY and {m.address for m in g.members} == {HP, HP_FRAC}


@pytest.mark.parametrize("limit", [20_000, 5])
def test_tc19_ng_full_chain_with_heal_about_and_stand_still(limit):
    from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient
    from tests.unit.test_ps5debug_ng import FakeNgServer

    server = FakeNgServer(snapshot=True)
    s = Session(lambda ip, port: Ps5DebugNgClient.connect(ip, server.port, timeout=3))
    s.connect("127.0.0.1")
    s.refresh_game()
    fake = server.fake
    try:
        sc = Scanner(s)
        sc.pc_limit = limit
        show_hp(fake, 250)
        sc.mark_full()
        show_hp(fake, 200)
        sc.next_scan(Compare.DECREASED)
        show_hp(fake, 100)
        sc.percent_scan(40, 1.0)
        show_hp(fake, 162.5)
        sc.percent_scan(65, 1.0)
        show_hp(fake, 125)
        sc.about_scan(50)
        sc.stand_still(rounds=2, interval_s=0, sleep=lambda _s: None)
        show_hp(fake, 62.5)
        sc.about_scan(25)
        assert set(addrs(sc)) == {HP, HP_FRAC}, sc.history
        [g] = sc.groups()
        assert g.label == LIKELY
    finally:
        s.disconnect()


# -- TC-20..22: grouping order and labels ----------------------------------------------
def test_tc20_group_order_likely_then_smaller_then_still_last():
    h = {
        (0x1000, F): [100.0, 80.0, 60.0],  # real health
        (0x1010, F): [1.0, 0.8, 0.6],  # its fraction next to it: likely real
        (0x9000_0000, F): [5.0, 6.0, 9.0],  # a lone mover
        (0x5000_0000, F): [7.0, 3.0, 2.0],  # two far display copies
        (0x5002_0000, F): [7.0, 3.0, 2.0],
        (0x7000_0000, F): [4.0, 4.0, 4.0],  # never changed
    }
    gs = group_matches(h)
    assert gs[0].label == LIKELY and gs[0].head.address in (0x1000, 0x1010)
    assert [len(g.members) for g in gs[1:3]] == [1, 2], "then smaller groups first"
    assert gs[2].label == COPIES
    assert gs[-1].label == STILL and gs[-1].head.address == 0x7000_0000


def test_tc21_not_linked_when_ratio_drifts_over_one_percent():
    h = {(0x1000, F): [100.0, 80.0, 60.0], (0x9000, F): [200.0, 160.0, 118.0]}  # 1.7% off
    assert all(len(g.members) == 1 for g in group_matches(h))


def test_tc22_groups_need_two_to_500_matches(fake, session):
    sc = _restricted(fake, session)
    show_hp(fake, 50)
    sc.percent_scan(20, 1.0)
    show_hp(fake, 25)
    sc.percent_scan(10, 1.0)
    assert len(sc.groups()) == 1
    f32(fake, HP_FRAC, 0.5)  # only health keeps going down
    f32(fake, HP, 20.0)
    sc.next_scan(Compare.DECREASED)
    assert sc.count == 1 and sc.groups() == [], "one match: no groups"


# -- UI: tree vs table, Start over, game change ----------------------------------------
@pytest.fixture
def hwin(qtbot, fake):
    ctl = Controller(lambda ip, port: fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()
    yield w
    ctl.shutdown()


def search(w, text: str) -> None:
    w.find.number.setText(text)
    w.find.search_btn.click()


def full_chain(w, fake) -> None:
    show_hp(fake, 250)
    w.find.full_btn.click()
    for hp, pct in ((100, 40), (50, 20), (25, 10)):
        show_hp(fake, hp)
        search(w, f"{pct}%")


def test_tc23_tree_goes_back_to_table_when_one_match_is_left(hwin, fake):
    f = hwin.find
    full_chain(hwin, fake)
    assert f.grouped and f.tree.isVisibleTo(f)
    f.tree.setCurrentItem(f.tree.topLevelItem(0))
    f32(fake, HP, 20.0)  # only health moves
    f.down_btn.click()
    assert hwin.ctl.scanner.count == 1
    assert not f.grouped and f.table.isVisibleTo(f) and not f.tree.isVisibleTo(f)
    assert f.table.rowCount() == 1 and f.rows[0].address == HP


def test_tc24_start_over_and_game_change_reset_the_find_view(hwin, fake):
    f = hwin.find
    full_chain(hwin, fake)
    f.new_btn.click()
    assert not f.grouped and f.tree.topLevelItemCount() == 0 and f.table.isVisibleTo(f)
    assert not any(b.isVisibleTo(f) for b in f.about_btns.values())
    full_chain(hwin, fake)
    assert f.grouped
    fake.remove_process(PID)
    fake.add_process(88, "eboot.bin", "PPSA09999", "Other")
    fake.add_region(88, "eboot.bin", 0x400000, b"\0" * 16, PROT_R | PROT_X)
    hwin.ctl.session.refresh_game()
    assert not hwin.ctl.scanner.full_mode and not f.grouped
    assert not f.tree.isVisibleTo(f)


def test_tc25_typed_in_address_under_the_groups_acts_on_itself(hwin, fake):
    f = hwin.find
    full_chain(hwin, fake)
    f.advanced.setChecked(True)
    f.add_addr.setText(f"{HEAP_BASE + 0x80:#x}")  # gold, int32 2450
    f.add_btn.click()
    tops = [f.tree.topLevelItem(t) for t in range(f.tree.topLevelItemCount())]
    assert len(tops) == 2
    f.tree.setCurrentItem(tops[1])
    assert f.selected().address == HEAP_BASE + 0x80


def test_tc26_plain_number_search_ui_unchanged(hwin):
    search(hwin, "100")
    f = hwin.find
    assert f.count.text() == "2 matches" and f.table.isVisibleTo(f) and not f.grouped
    assert not any(b.isVisibleTo(f) for b in f.about_btns.values())


# -- Bugs -----------------------------------------------------------------------------
def _int_health_full_chain(sc: Scanner, fake: FakeConsole) -> None:
    """Health kept as a whole number (max 250), nothing else linked to it."""
    i32(fake, HP, 250)
    sc.mark_full()
    for hp, pct in ((100, 40), (50, 20), (25, 10)):
        i32(fake, HP, hp)
        sc.percent_scan(pct, 1.0)


def test_bug01_int_health_group_head_is_the_whole_number(fake, session):
    """Retest of BUG-01: the decimal reading of the whole number's bytes (1e-43) is not
    kept "of full", so only the whole number is left (no group: 1 match)."""
    sc = Scanner(session)
    _int_health_full_chain(sc, fake)
    assert sc.count == 1 and sc.types == {W: 1}
    [m] = sc.matches(10)
    assert (m.address, m.vtype, m.value, m.way) == (HP, W, 25, OF_FULL)
    assert sc.groups() == []


def test_bug01_int_health_with_fraction_groups_without_the_twin(fake, session):
    """With a real fraction next to it, the group is the whole number and the fraction;
    the decimal reading of the whole number is in no group."""
    sc = Scanner(session)
    i32(fake, HP, 250)
    f32(fake, HP_FRAC, 1.0)
    sc.mark_full()
    for hp, pct in ((100, 40), (50, 20), (25, 10)):
        i32(fake, HP, hp)
        f32(fake, HP_FRAC, hp / 250)
        sc.percent_scan(pct, 1.0)
    [g] = sc.groups()
    assert {(m.address, m.vtype) for m in g.members} == {(HP, W), (HP_FRAC, F)}
    assert g.label == LIKELY and g.copies == 1


def test_bug01_plain_unknown_search_group_head_is_the_whole_number(fake, session):
    sc = Scanner(session)
    i32(fake, HP, 250)
    sc.first_scan(None)
    for hp in (200, 150, 100):
        i32(fake, HP, hp)
        sc.next_scan(Compare.DECREASED)
    [g] = [g for g in sc.groups() if g.head.address == HP]
    assert g.head.vtype is W


def test_bug01_ui_set_on_the_one_row_writes_a_whole_number(hwin, fake):
    """Retest of BUG-01 in the Find view: one row, a whole number; Set and Make a cheat
    use the whole-number type."""
    f = hwin.find
    i32(fake, HP, 250)
    f.full_btn.click()
    for hp, pct in ((100, 40), (50, 20), (25, 10)):
        i32(fake, HP, hp)
        search(hwin, f"{pct}%")
    assert hwin.ctl.scanner.count == 1 and not f.grouped and f.table.isVisibleTo(f)
    assert [(m.address, m.vtype) for m in f.rows] == [(HP, W)]
    f.table.selectRow(0)
    f.set_value.setText("200")
    f.set_btn.click()
    assert struct.unpack("<i", fake.peek(PID, HP, 4))[0] == 200
    f.set_value.clear()
    f.template.setCurrentIndex(0)
    f.make_btn.click()
    assert hwin.ctl.cheat_file.cheats[-1].locations[0].offset == HP
    assert hwin.ctl.cheat_file.cheats[-1].value_type in (W, None)


def test_bug02_zero_percent_then_percent_keeps_the_real_value(fake, session):
    f32(fake, EDGE, 1700.0)  # a shield out of 1,700
    sc = Scanner(session)
    sc.pc_limit = 10
    sc.mark_full()
    f32(fake, EDGE, 0.0)  # shield broken: the game shows 0%
    sc.percent_scan(0, 1.0)
    assert sc.history[-1] == "0% (as went down)" and EDGE in addrs(sc)
    f32(fake, EDGE, 510.0)  # recharged to 30%
    sc.percent_scan(30, 1.0)
    assert EDGE in addrs(sc), f"count={sc.count} history={sc.history}"


def test_bug02_zero_percent_then_about_keeps_the_real_value(fake, session):
    f32(fake, EDGE, 1700.0)
    sc = Scanner(session)
    sc.pc_limit = 10
    sc.mark_full()
    f32(fake, EDGE, 0.0)
    sc.percent_scan(0, 1.0)
    f32(fake, EDGE, 850.0)
    sc.about_scan(50)
    assert EDGE in addrs(sc)


@pytest.mark.parametrize("pct,stored", [(1, 1.5), (0, 0.5)])
def test_bug03_low_percent_lists_each_address_once(fake, session, pct, stored):
    f32(fake, EDGE, stored)
    sc = Scanner(session)
    sc.percent_scan(pct, 1.0, F)
    found = addrs(sc)
    assert found.count(EDGE) == 1
    assert sc.count == len(set(found)), "the match count counts each address once"


def test_bug04_noted_scan_drops_nothing(fake, session):
    f32(fake, EDGE, float("inf"))
    f32(fake, EDGE + 8, 3e38)
    sc = Scanner(session)
    sc.pc_limit = 1
    sc.mark_full(F)
    before = sc.count
    sc.percent_scan(100, 1.0)  # same as Full: can only be noted
    assert sc.note == NOTED
    assert sc.count == before, "Noted, nothing dropped"


# -- Retest of the fixes (f9a10e7, 1c071b3) --------------------------------------------
def _ng_session():
    from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient
    from tests.unit.test_ps5debug_ng import FakeNgServer

    server = FakeNgServer(snapshot=True)
    s = Session(lambda ip, port: Ps5DebugNgClient.connect(ip, server.port, timeout=3))
    s.connect("127.0.0.1")
    s.refresh_game()
    return server, s


def test_rt01_ng_noted_on_the_console_snapshot_keeps_inf_and_huge():
    server, s = _ng_session()
    try:
        fake = server.fake
        f32(fake, EDGE, float("inf"))
        f32(fake, EDGE + 8, 3e38)
        f32(fake, EDGE + 16, -float("inf"))
        sc = Scanner(s)
        sc.pc_limit = 1
        show_hp(fake, 250)
        before = sc.mark_full()
        sc.percent_scan(100, 1.0)  # same as Full: noted
        assert sc.note == NOTED and sc.count == before
        assert {EDGE, EDGE + 8, EDGE + 16} <= set(addrs(sc))
        show_hp(fake, 100)
        sc.percent_scan(40, 1.0)
        assert sc.history[-1] == "40% (as went down)" and HP in addrs(sc)
    finally:
        s.disconnect()


def test_rt02_ng_noted_on_the_pc_too_many_to_read_is_refused_and_search_kept(monkeypatch):
    """Percent search on the PC (parts), then It's full now, then the same % over the
    limit: noting would read too many spans, so it is refused before anything changes."""
    from cheatmaker.console import ps5debug_ng

    server, s = _ng_session()
    try:
        fake = server.fake
        f32(fake, HP, 40.0)
        f32(fake, HP_FRAC, 0.4)
        sc = Scanner(s)
        sc.percent_scan(40, 1.0)
        n = sc.count
        sc.mark_full()
        assert sc.full_mode and sc.count == n
        history = list(sc.history)
        monkeypatch.setattr(ps5debug_ng, "MAX_READ_SPANS", 0)
        sc.pc_limit = 1
        with pytest.raises(ValueError, match="too many to note a percent"):
            sc.percent_scan(100, 1.0)
        assert sc.count == n and sc.history == history and sc.last_pct == 100
        monkeypatch.setattr(ps5debug_ng, "MAX_READ_SPANS", 4000)
        assert sc.percent_scan(100, 1.0) == n and sc.note == NOTED
    finally:
        s.disconnect()


def test_rt03_ng_one_percent_lists_each_address_once():
    server, s = _ng_session()
    try:
        f32(server.fake, EDGE, 1.5)
        sc = Scanner(s)
        sc.percent_scan(1, 1.0, F)
        found = addrs(sc)
        assert found.count(EDGE) == 1 and sc.count == len(set(found))
        assert {m.address: m.way for m in sc.matches(5000)}[EDGE] == PERCENT, "narrowest way"
    finally:
        s.disconnect()


@pytest.mark.xfail(
    reason="BUG-05: a 0% PC check ties every match to 'percent', so the next % drops it",
    strict=True,
)
@pytest.mark.parametrize("about", [False, True])
def test_bug05_zero_percent_on_the_pc_then_heal_keeps_the_value(fake, session, about):
    """Under the limit, 0% is checked on the PC (Full is known), then 30% / About 25%
    must still find the shield out of 1,700 (AC-P2)."""
    f32(fake, EDGE, 1700.0)
    sc = Scanner(session)
    sc.mark_full(F)
    f32(fake, EDGE, 0.0)
    sc.percent_scan(0, 1.0)
    assert sc.note is None and EDGE in addrs(sc)
    f32(fake, EDGE, 510.0)  # 30%
    if about:
        sc.about_scan(25)
    else:
        sc.percent_scan(30, 1.0)
    assert EDGE in addrs(sc), f"count={sc.count} history={sc.history}"


def test_rt05_plain_number_over_100_in_a_full_search_explains(hwin, fake):
    show_hp(fake, 250)
    hwin.find.full_btn.click()
    sc = hwin.ctl.scanner
    n = sc.count
    search(hwin, "250")
    assert "type the % the game shows" in hwin.status.text()
    assert "Start over" in hwin.status.text()
    assert sc.count == n and sc.history == ["full"]
    search(hwin, "100")  # 0..100 still counts as a percent
    assert sc.history[-1].startswith("100%")
