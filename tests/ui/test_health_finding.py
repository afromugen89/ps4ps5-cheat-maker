"""Find view: percent search, It's full now, About X%, and grouped copies
(spec-health-finding), on the FakeConsole (pytest-qt, offscreen)."""

from __future__ import annotations

import struct

import pytest
from PySide6.QtWidgets import QMessageBox

from cheatmaker.core.guide import Action, Shows
from cheatmaker.core.values import ValueType
from cheatmaker.settings import Settings
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.find_wizard import ROW
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests.conftest import HEAP_BASE, PID, build_fake

HP = HEAP_BASE + 0x400  # health out of 250
HP_FRAC = HEAP_BASE + 0x410  # the same health as a fraction, right next to it
COPIES = [0x3000_0000 + k * 0x20000 for k in range(3)]  # HUD copies of the fraction


@pytest.fixture
def fake():
    fake = build_fake()
    for a in COPIES:
        fake.add_region(PID, "[anon:hud]", a, 0x10)
    return fake


@pytest.fixture
def win(qtbot, fake):
    ctl = Controller(lambda ip, port: fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()
    yield w
    ctl.shutdown()


def show_hp(fake, hp: float) -> None:
    fake.poke(PID, HP, struct.pack("<f", hp))
    for a in [HP_FRAC, *COPIES]:
        fake.poke(PID, a, struct.pack("<f", hp / 250))


def search(w, text: str) -> None:
    w.find.number.setText(text)
    w.find.search_btn.click()


def test_typing_a_percent_searches_every_way(win, fake):  # AC-P1
    fake.poke(PID, HEAP_BASE + 0x300, struct.pack("<f", 40.0))
    fake.poke(PID, HEAP_BASE + 0x310, struct.pack("<f", 0.4))
    fake.poke(PID, HEAP_BASE + 0x320, struct.pack("<i", 400))
    f = win.find
    search(win, "40%")
    sc = win.ctl.scanner
    assert sc.history == ["40%"] and sc.count == 3
    assert f.count.text().startswith("3 matches")
    assert f.table.rowCount() == 3 and not f.tree.isVisibleTo(f)
    f.advanced.setChecked(True)  # never asked for: the percent ignores "stores it as"
    search(win, "140%")
    assert "0 to 100" in win.status.text() and sc.count == 3


def test_plain_number_search_is_unchanged(win):
    search(win, "100")
    assert win.find.count.text() == "2 matches" and win.ctl.scanner.history == ["100"]
    assert not win.ctl.scanner.percent_search


def test_full_then_percent_then_groups(win, fake):  # AC-P2, AC-G4
    f = win.find
    show_hp(fake, 250)
    assert not any(b.isVisibleTo(f) for b in f.about_btns.values())
    f.full_btn.click()
    sc = win.ctl.scanner
    assert sc.full_mode and sc.history == ["full"]
    assert all(b.isVisibleTo(f) for b in f.about_btns.values())
    show_hp(fake, 100)
    search(win, "40%")
    assert sc.count == 5 and f.table.isVisibleTo(f), "one change: no groups yet"
    show_hp(fake, 50)
    search(win, "20%")
    show_hp(fake, 25)
    search(win, "10%")
    assert sc.count == 5 and f.grouped
    assert f.tree.isVisibleTo(f) and not f.table.isVisibleTo(f)
    assert f.tree.topLevelItemCount() == 1
    top = f.tree.topLevelItem(0)
    assert top.childCount() == 5
    assert "likely real" in top.text(2) and "+4 copies" in top.text(2)
    assert top.text(1) in ("of full", "fraction")
    f.tree.setCurrentItem(top)
    assert f.selected().address in (HP, HP_FRAC), "a group row acts on its likely real one"
    copy = next(
        top.child(c) for c in range(5) if f.rows[top.child(c).data(0, ROW)].address == COPIES[0]
    )
    f.tree.setCurrentItem(copy)
    assert f.selected().address == COPIES[0], "an expanded row acts on that address"
    f.freeze_btn.click()
    assert COPIES[0] in win.ctl.frozen_addresses
    f.refresh_live()
    assert f.selected().address == COPIES[0], "the pick survives a live refresh"


def test_group_filter_hides_groups_with_no_match(win, fake):
    f = win.find
    show_hp(fake, 250)
    f.full_btn.click()
    show_hp(fake, 100)
    search(win, "40%")
    show_hp(fake, 50)
    search(win, "20%")
    show_hp(fake, 25)
    search(win, "10%")
    f.filter.setText("24-26")
    assert not f.tree.topLevelItem(0).isHidden() and f.filter_count.text() == "1 of 1 shown"
    f.filter.setText("1000")
    assert f.tree.topLevelItem(0).isHidden()


def full_and_three_hits(win, fake) -> None:
    """It's full now, then 40%, 20%, 10%: health, its fraction and 3 HUD copies, grouped."""
    show_hp(fake, 250)
    win.find.full_btn.click()
    for hp, pct in ((100, "40%"), (50, "20%"), (25, "10%")):
        show_hp(fake, hp)
        search(win, pct)
    assert win.find.grouped


def test_test_the_hits_on_one_group_tests_members_stored_one_way(win, fake):
    """Health is kept "of full", its copies as fractions: one test value can't mean the
    same to both, so the fractions (the most common way) are tested, asked in %."""
    from cheatmaker.core.values import FRACTION

    f = win.find
    full_and_three_hits(win, fake)
    addrs, scale = f._test_candidates()
    assert sorted(addrs) == sorted([HP_FRAC, *COPIES]) and scale == FRACTION


def test_test_the_hits_refuses_when_every_candidate_is_stored_differently(win, fake):
    from cheatmaker.core.grouping import Group, Member
    from cheatmaker.core.values import FRACTION, OF_FULL

    f = win.find
    full_and_three_hits(win, fake)
    f.groups = [
        Group([Member(HP, ValueType.FLOAT, OF_FULL, ())]),
        Group([Member(HP_FRAC, ValueType.FLOAT, FRACTION, ())]),
    ]
    with pytest.raises(ValueError, match="different ways"):
        f._test_candidates()


def test_make_a_cheat_on_group_row_and_on_expanded_row(win, fake):  # AC-G4
    f = win.find
    full_and_three_hits(win, fake)
    f.template.setCurrentIndex(0)
    top = f.tree.topLevelItem(0)
    f.tree.setCurrentItem(top)
    head = f.selected().address
    assert head in (HP, HP_FRAC)
    f.make_btn.click()
    copy = next(
        top.child(c)
        for c in range(top.childCount())
        if f.rows[top.child(c).data(0, ROW)].address == COPIES[1]
    )
    f.tree.setCurrentItem(copy)
    f.make_btn.click()
    cheats = win.ctl.cheat_file.cheats
    assert [c.locations[0].offset for c in cheats[-2:]] == [head, COPIES[1]]


def test_search_lost_clears_the_groups(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    win.ctl.search_lost.emit()
    assert not f.grouped and f.tree.topLevelItemCount() == 0
    assert not f.tree.isVisibleTo(f) and f.table.isVisibleTo(f)
    assert f.hint.text() == hints.SEARCH_LOST


def test_about_buttons(win, fake):  # AC-P3
    f = win.find
    show_hp(fake, 250)
    f.full_btn.click()
    show_hp(fake, 130)  # 52%
    f.about_btns[50].click()
    sc = win.ctl.scanner
    assert sc.history[-1] == "about 50%" and sc.count == 5


def test_full_again_asks_first(win, fake, monkeypatch):
    f = win.find
    show_hp(fake, 250)
    f.full_btn.click()
    sc = win.ctl.scanner
    answers = []

    def ask(*a):
        answers.append(a[2])
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", ask)
    f.full_btn.click()
    assert answers == [hints.FIND_FULL_CONFIRM] and sc.history == ["full"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    f.full_btn.click()
    assert sc.history[-1] == "full again (same)"


def test_too_many_for_percent_says_so(win, fake):
    f = win.find
    win.ctl.scanner.pc_limit = 10
    show_hp(fake, 250)
    f.full_btn.click()
    show_hp(fake, 100)
    search(win, "40%")
    assert f.hint.text() == hints.FIND_PERCENT_TOO_MANY
    assert win.ctl.scanner.history[-1] == "40% (as went down)"


def test_guide_bar_starts_from_full(win):  # AC-P4
    f = win.find
    f.shows_btns[Shows.BAR].click()
    assert f.suggestion.action is Action.FULL
    assert f.hint.text() == "Fill it up, then press It's full now."
    assert "font-weight: bold" in f.full_btn.styleSheet()
    f.full_btn.click()
    assert f.suggestion.action is Action.PERCENT
    assert f.hint.text().startswith("Make it go down, then type the %")


def test_start_over_leaves_full_mode(win, fake):
    f = win.find
    f.full_btn.click()
    f.new_btn.click()
    assert not win.ctl.scanner.full_mode and not f.grouped
    assert not any(b.isVisibleTo(f) for b in f.about_btns.values())


def test_kein_scan_12_shows_as_a_few_rows(qtbot):  # measurable target, in tests
    from tests.unit.test_grouping import kein_fake, poke_scan

    fake, scans = kein_fake()
    ctl = Controller(lambda ip, port: fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()
    f = w.find
    poke_scan(fake, scans, 0)
    f.advanced.setChecked(True)
    f.vtype.setCurrentIndex(f.vtype.findData(ValueType.FLOAT))
    f.unknown_btn.click()
    for i in (1, 2, 3):
        poke_scan(fake, scans, i)
        f.down_btn.click()
    assert ctl.scanner.count == 59 and f.grouped
    tops = [f.tree.topLevelItem(t) for t in range(f.tree.topLevelItemCount())]
    assert len(tops) <= 8
    health = next(t for t in tops if t.childCount() > 10)
    assert f.rows[health.data(0, ROW)].address in (0x12E0EA40B0, 0x12E0EA40A0)
    assert health.text(2).startswith("likely real")
    assert f.tree.isColumnHidden(3) is False, "Advanced is on: addresses are shown"
    ctl.shutdown()


TWO_A, TWO_A_COPY = HEAP_BASE + 0x600, 0x3100_0000  # one value and its HUD copy
TWO_B, TWO_B_COPY = HEAP_BASE + 0x640, 0x3200_0000  # another value, moving its own way


def test_test_the_hits_with_several_groups_uses_the_expanded_pick(qtbot):
    fake = build_fake()
    fake.add_region(PID, "[anon:hud]", TWO_A_COPY, 0x10)
    fake.add_region(PID, "[anon:hud]", TWO_B_COPY, 0x10)
    ctl = Controller(lambda ip, port: fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()
    f = w.find
    series_a, series_b = (100.0, 80.0, 60.0, 40.0), (50.0, 45.0, 30.0, 10.0)

    def show(i):
        for a in (TWO_A, TWO_A_COPY):
            fake.poke(PID, a, struct.pack("<f", series_a[i]))
        for a in (TWO_B, TWO_B_COPY):
            fake.poke(PID, a, struct.pack("<f", series_b[i]))

    show(0)
    f.advanced.setChecked(True)
    f.vtype.setCurrentIndex(f.vtype.findData(ValueType.FLOAT))
    f.unknown_btn.click()
    for i in (1, 2, 3):
        show(i)
        f.down_btn.click()
    assert ctl.scanner.count == 4 and f.grouped and len(f.groups) == 2
    heads = [g.head.address for g in f.groups]
    assert sorted(f._test_candidates()[0]) == sorted(heads)
    b_top = next(
        f.tree.topLevelItem(t)
        for t in range(2)
        if TWO_B in {m.address for m in f.groups[t].members}
    )
    pick = next(
        b_top.child(c)
        for c in range(b_top.childCount())
        if f.rows[b_top.child(c).data(0, ROW)].address == TWO_B_COPY
    )
    f.tree.setCurrentItem(pick)
    addrs = f._test_candidates()[0]
    assert TWO_B_COPY in addrs and len(addrs) == 2
    [other] = [a for a in addrs if a != TWO_B_COPY]
    assert other in (TWO_A, TWO_A_COPY), "group A still gives its head"
    ctl.shutdown()


def test_tree_pick_survives_a_new_scan_and_found_rows_are_selected(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    top = f.tree.topLevelItem(0)
    copy = next(
        top.child(c)
        for c in range(top.childCount())
        if f.rows[top.child(c).data(0, ROW)].address == COPIES[2]
    )
    f.tree.setCurrentItem(copy)
    show_hp(fake, 12.5)
    search(win, "5%")
    assert f.grouped and f.selected().address == COPIES[2]
    assert f.tree.selectedItems()[0].parent() is not None, "still the expanded row"
    f.tree.clearSelection()
    f.use_found(COPIES[1], ValueType.FLOAT)  # e.g. Test the hits picked a copy
    assert f.selected().address == COPIES[1]
