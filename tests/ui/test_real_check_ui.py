"""Find view: Check which is real (spec-real-check AC-R1, AC-R5), on the FakeConsole."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.core import real_check
from cheatmaker.core.real_check import Verdict
from cheatmaker.settings import Settings
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests.conftest import PID, build_fake
from tests.ui.test_health_finding import COPIES, HP, HP_FRAC, full_and_three_hits, search, show_hp


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


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(real_check, "SETTLE_S", 0.0)


def f32(v: float) -> bytes:
    return struct.pack("<f", v)


def hp_now(fake) -> float:
    return struct.unpack("<f", fake.peek(PID, HP, 4))[0]


def game_copies_health(fake) -> None:
    """From now on the game works out the fraction and the HUD copies from HP."""

    def frame() -> None:
        for a in [HP_FRAC, *COPIES]:
            fake.poke(PID, a, f32(hp_now(fake) / 250))

    fake.game_ticks.append(frame)


def test_check_finds_the_real_one_in_a_group(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    game_copies_health(fake)
    assert f.check_btn.isEnabled()
    f.check_btn.click()
    assert f.verdicts[(HP, f.groups[0].head.vtype)].verdict is Verdict.REAL
    assert f.verdicts[(HP, f.groups[0].head.vtype)].followers == 4
    assert all(f.verdicts[(a, f.groups[0].head.vtype)].verdict is Verdict.COPY for a in COPIES)
    assert f.groups[0].head.address == HP, "the real one heads its group"
    top = f.tree.topLevelItem(0)
    assert "checked: real" in top.text(2) and "+4 copies" in top.text(2)
    assert f.selected().address == HP, "the real one is picked, ready for Make a cheat"
    assert f.hint.text().startswith("Found the real one at")
    assert hp_now(fake) == 25.0, "the test value was put back"
    assert f._cancel is None and f.check_btn.isEnabled()


def test_check_marks_copies_in_the_expanded_rows(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    game_copies_health(fake)
    f.check_btn.click()
    top = f.tree.topLevelItem(0)
    notes = {top.child(c).text(3): top.child(c).text(2) for c in range(top.childCount())}
    assert notes[f"{HP:#x}"] == "checked: real"
    assert notes[f"{COPIES[0]:#x}"] == "checked: copy"


def test_check_with_nothing_following(win, fake):
    """Nothing writes any of them (no game ticks): every one keeps its test value."""
    f = win.find
    full_and_three_hits(win, fake)
    f.check_btn.click()
    assert {r.verdict for r in f.verdicts.values()} == {Verdict.KEPT}
    assert f.hint.text() == hints.CHECK_REAL_KEPT
    assert hp_now(fake) == 25.0


def test_check_all_copies(win, fake):
    """The game rewrites all five from a value that isn't listed."""
    f = win.find
    full_and_three_hits(win, fake)
    fake.game_ticks.append(
        lambda: [fake.poke(PID, a, f32(25.0 if a == HP else 0.1)) for a in [HP, HP_FRAC, *COPIES]]
    )
    f.check_btn.click()
    assert {r.verdict for r in f.verdicts.values()} == {Verdict.COPY}
    assert f.hint.text() == hints.CHECK_REAL_NONE


def test_check_on_a_flat_list(win, fake):
    """Before groups show, the listed rows are checked one at a time."""
    f = win.find
    show_hp(fake, 250)
    f.full_btn.click()
    show_hp(fake, 100)
    search(win, "40%")
    assert not f.grouped and f._n_matches == 5
    game_copies_health(fake)
    f.check_btn.click()
    assert f.selected().address == HP
    assert "(checked: real)" in f.table.item(f._selected_row(), 0).text()


def test_reset_forgets_the_check(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    game_copies_health(fake)
    f.check_btn.click()
    f.do_reset()
    assert f.verdicts == {} and not f.check_btn.isEnabled()


def test_check_skips_frozen_addresses(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    win.ctl.frozen_addresses.add(COPIES[1])
    f.check_btn.click()
    assert (COPIES[1], f.groups[0].members[0].vtype) not in f.verdicts


def test_lost_link_during_the_check(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    fake.game_ticks.append(lambda: fake.disconnect() if fake.writes else None)
    f.check_btn.click()
    assert not win.ctl.session.connected
    assert f._cancel is None


def test_results_of_a_check_that_outlived_its_search_are_dropped(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    game_copies_health(fake)
    checker = real_check.RealChecker(
        fake, PID, real_check.targets_from_groups(f.groups), wait=lambda _s: None
    )
    gen = f._search_gen
    checker.run()
    f.do_reset()  # e.g. a new game started while it ran
    f._after_check(checker, False, gen)
    assert f.verdicts == {}


def test_hint_when_nothing_could_be_checked(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    fake.fail_writes = True
    f.check_btn.click()
    assert {r.verdict for r in f.verdicts.values()} == {Verdict.FAILED}
    assert f.hint.text() == hints.CHECK_REAL_FAILED


def test_live_list_runs_again_and_a_second_check_works(win, fake):
    f = win.find
    full_and_three_hits(win, fake)
    game_copies_health(fake)
    f.check_btn.click()
    assert f.timer.isActive()
    f.check_btn.click()
    assert f.groups[0].head.address == HP and hp_now(fake) == 25.0
