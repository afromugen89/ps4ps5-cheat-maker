"""Bring them over in the app (spec-game-update AC-U1, U3..U8, AC-U11): the offer bar,
Not now, the run with each step, Cancel, a dropped link, the summary and what is saved."""

from __future__ import annotations

import dataclasses
import struct

import pytest
from PySide6.QtCore import Qt

from cheatmaker.core import carry as cr
from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.core.session import GameInfo
from cheatmaker.settings import Settings
from cheatmaker.ui import hints
from cheatmaker.ui import main_window as mw
from cheatmaker.ui.cheats_view import COL_KEEPS, COL_ON
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests import fakegame as fg
from tests.unit import test_carry as tc

NEW = fg.V2.version


class _NoDialog:
    def exec(self) -> int:
        return 0


class Console:
    def __init__(self, run: int = 1, layout: fg.Layout = fg.V2, **kw) -> None:
        self.run, self.layout, self.kw = run, layout, kw
        self.game = fg.build(run, connect=False, layout=layout, **kw)

    def restart(self) -> fg.Game:
        self.game = fg.build(self.run, connect=False, layout=self.layout, **self.kw)
        return self.game


class App:
    """One app run (MainWindow) against the console; ``picks`` answer pick dialogs."""

    def __init__(self, qtbot, console: Console) -> None:
        self.console = console
        self.ctl = Controller(
            lambda ip, port: console.game.fake, Settings.load(), TaskRunner(sync=True)
        )
        self.w = MainWindow(self.ctl)
        qtbot.addWidget(self.w)
        self.asked: list[str] = []
        self.picks: list[str] = []
        finder = self.w.cheats.finder
        finder.ask_confirm = lambda text: self.asked.append(text) or True
        finder.ask_pick = self._pick
        finder.sleep = lambda _s: None
        clock = tc.Clock()
        self.clock = clock
        self.rounds = 3
        self.w.carry.clock = clock
        self.w.carry.sleep = self._sleep
        self._fight = None

    def _sleep(self, s: float) -> None:
        self.clock.sleep(s)
        if self._fight is None:
            self._fight = self.console.game.fight(self.rounds)
        self._fight(s)

    def _pick(self, obj, options) -> int | None:
        self.picks.append(obj.label)
        return self.console.game.player

    def connect(self) -> None:
        self.w.connect_view.ip.setText("192.168.1.50")
        self.w.connect_view.connect_btn.click()

    @property
    def bar(self):
        return self.w.carry_bar

    def bring(self) -> None:
        self._fight = None
        self.bar.bring_btn.click()


@pytest.fixture
def make(qtbot, monkeypatch):
    monkeypatch.setattr(rf.ah, "WATCH_SECONDS", 1)
    monkeypatch.setattr(rf.ah, "PROVE_SECONDS", 1)
    monkeypatch.setattr(rf, "SLOT_WAIT_S", 0.5)
    apps: list[App] = []

    def _make(console: Console) -> App:
        app = App(qtbot, console)
        apps.append(app)
        return app

    yield _make
    for a in apps:
        a.ctl.shutdown()


def new_path():
    return pj.project_path(fg.TITLE, NEW)


def old_files() -> dict:
    """Old notes and old fingerprints, byte for byte (AC-U7)."""
    out = {}
    for p in (pj.project_path(fg.TITLE, fg.VERSION), up.signatures_path(fg.TITLE, fg.VERSION)):
        out[p] = p.read_bytes() if p.exists() else None
    return out


# == AC-U1 the offer =======================================================================
def test_offer_at_connect_names_both_versions_and_writes_nothing(make, caplog):
    tc.learned_v1(1)
    caplog.set_level("INFO")
    app = make(Console())
    app.console.game.fake.writes.clear()
    app.connect()
    assert app.bar.mode == "offer" and app.bar.isVisibleTo(app.w)
    assert app.bar.label.text() == (
        "Your notes for Returnal are for version 03.000.005; the game is now 03.001.000. "
        "Press Bring them over and keep playing for about 2 minutes."
    )
    assert (
        "action=carry_offer title=PPSA01285 from=03.000.005 to=03.001.000 objects=1 cheats=1"
        in (caplog.text)
    )
    assert app.console.game.fake.writes == [] and not new_path().exists()


def test_newer_notes_only_are_worded_newer(make):
    p = tc.notes("03.002.000")
    tc.save(p)
    app = make(Console())
    app.connect()
    assert "made for a newer version, 03.002.000" in app.bar.label.text()


@pytest.mark.parametrize("case", ["unknown", "other title", "new notes", "unreadable"])
def test_no_offer(make, case):
    tc.learned_v1(1)
    layout = fg.V2
    if case == "unknown":
        layout = dataclasses.replace(fg.V2, version="unknown")
    elif case == "other title":
        p = tc.notes("03.000.004", title="PPSA99999")
        pj.project_path(fg.TITLE, fg.VERSION).unlink()
        tc.save(p)
    elif case == "new notes":
        tc.save(tc.notes(NEW, objects=[tc.obolites()]))
    elif case == "unreadable":
        new_path().write_text("{", encoding="utf-8")
    app = make(Console(layout=layout))
    app.connect()
    assert app.bar.mode == "hidden" and not app.bar.isVisibleTo(app.w)


def test_not_now_is_remembered_after_a_restart_and_advanced_stays(make):
    tc.learned_v1(1)
    app = make(Console())
    app.connect()
    app.bar.not_now_btn.click()
    assert app.bar.mode == "hidden"
    assert Settings.load().carry_declined == ["PPSA01285 03.000.005>03.001.000"]
    app.ctl.shutdown()
    again = make(Console())
    again.connect()
    assert again.bar.mode == "hidden"
    again.w.cheats.show_details.setChecked(True)
    assert not again.w.cheats.bring_btn.isHidden() and again.w.cheats.bring_btn.isEnabled()


def test_an_old_cheat_file_still_gets_the_v2_warning(make):
    tc.learned_v1(1)
    app = make(Console())
    app.connect()
    loc = Location(LocationKind.CODE_PATCH, 0x300, b"\x90" * 6, fg.SUB_W)
    app.ctl.cheat_file = CheatFile("Returnal", fg.TITLE, fg.VERSION, [Cheat("No death", [loc])])
    app.w.cheats._check_version()
    assert "made for version 03.000.005" in app.w.cheats.warning.text()
    assert not app.w.cheats.update_btn.isHidden()


# == AC-U3..U8 the run =========================================================================
def test_full_bring_over_v1_to_v2(make, caplog):
    tc.learned_v1(1)
    before = old_files()
    caplog.set_level("INFO")
    app = make(Console())
    app.connect()
    texts = []
    app.ctl.message.connect(texts.append)
    app.bring()
    assert app.picks == ["Health"], "player and enemies: two classes, the player picks"
    summary = "Brought over 1 of 1 cheats for version 03.001.000. Check them: Infinite health."
    assert app.w.status.text() == summary and app.bar.mode == "done"
    assert app.bar.label.text() == summary
    for step in (
        "Reading the game's code (reads only)...",
        "Finding Health in the new version: play normally, take a hit if you can.",
        "Checking the spare memory (about a minute, keep playing)...",
    ):
        assert step in texts
    saved = pj.load(new_path())
    obj = saved.objects[0]
    assert saved.carried_from == fg.VERSION and obj.check and obj.field("Health").off == 0x2B4
    assert obj.hook.sig_unique, "the new version's fingerprints, learned from the same read"
    assert old_files() == before, "old notes and fingerprints byte for byte"
    for line in (
        "action=carry_hook object=obj1",
        "how=loose",
        "field_shift=0x8",
        "action=carry_cheat name=Infinite health result=carried",
        "action=carry_done from=03.000.005 to=03.001.000 objects=1 found=1",
    ):
        assert line in caplog.text
    assert caplog.text.count("action=read_code") == 1, "the code is read once"
    assert not app.w.cheats.finder.carrying
    assert app.w.cheats.table.item(0, COL_KEEPS).text() == hints.KEEPS_CHECK
    app.w.cheats.table.item(0, COL_ON).setCheckState(Qt.CheckState.Checked)
    assert "brought over from version 03.000.005" in app.asked[0]
    assert app.console.game.health() == 999.0, "written at the new place after the Yes"
    assert not pj.load(new_path()).objects[0].check


def test_one_class_needs_no_pick_and_the_bar_progress_ends_full(make):
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    seen = []
    app.bar.progress.valueChanged.connect(seen.append)
    app.bring()
    assert app.picks == [] and seen and seen == sorted(seen) and seen[-1] == 100


def test_buttons_wait_while_bringing_over(make, monkeypatch):
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    states = []
    real = app._sleep

    def sleep(s):
        cv = app.w.cheats
        states.append(
            (
                cv.finder.carrying,
                cv.again_btn.isEnabled(),
                cv.learn_btn.isEnabled(),
                cv.bring_btn.isEnabled(),
            )
        )
        real(s)

    app.w.carry.sleep = sleep
    app.bring()
    assert states and all(s == (True, False, False, False) for s in states)
    opened = []
    monkeypatch.setattr(mw, "LockWizard", lambda *a: opened.append(a) or _NoDialog())
    app.w.open_lock(Cheat("x", [Location(LocationKind.SESSION_ADDRESS, 1, b"\0", b"\0")]))
    assert opened, "free again afterwards"


@pytest.mark.parametrize("kind", ["static", "chain"])
def test_static_or_chain_only_needs_no_hook_and_no_slot_check(make, caplog, kind):
    if kind == "static":
        p = tc.static_notes()
        console = Console(static=True)
    else:
        p = tc.chain_notes()
        p.cheats = p.cheats[:1]
        console = Console(layout=dataclasses.replace(fg.V1, version=NEW), chain=True)
    tc.save(p)
    caplog.set_level("INFO")
    app = make(console)
    app.connect()
    app.bring()
    assert "slot_prove" not in caplog.text and "hook_install" not in caplog.text
    assert "action=read_code" not in caplog.text
    assert app.w.status.text().startswith("Brought over 1 of 1 cheats")
    saved = pj.load(new_path())
    assert saved.objects[0].anchors and saved.objects[0].check
    assert f"kind={kind} result=kept" in caplog.text


# -- one pass and one fail per row of the spec's table --------------------------------------
def test_hook_fails_plainly_when_the_code_changed_and_the_value_is_kept(make):
    tc.learned_v1(1)
    app = make(Console(layout=dataclasses.replace(fg.V2, changed=True)))
    app.connect()
    app.bring()
    assert app.w.status.text() == (
        "Brought over 0 of 1 cheats for version 03.001.000. Not brought over: "
        "Infinite health (the game's code changed where it was found)."
    )
    saved = pj.load(new_path())
    assert saved.objects[0].anchors == [] and [c.name for c in saved.cheats] == ["Infinite health"]
    assert app.console.game.fake.peek(fg.PID, fg.V2.reader, 10) == app.console.game.layout.vmovss_r


def test_static_that_moved_fails_with_a_reason(make):
    tc.save(tc.static_notes())
    console = Console(layout=dataclasses.replace(fg.V2, static_off=fg.DATA + 0x60), static=True)
    console.game.fake.poke(fg.PID, fg.DATA + 0x40, struct.pack("<i", -5))
    app = make(console)
    app.connect()
    app.bring()
    assert "Not brought over: Rich (it couldn't be found in this version)." in app.w.status.text()


def test_code_part_moves_and_says_test_it(make):
    tc.parts_notes()
    app = make(Console(run=3, layout=dataclasses.replace(fg.V2, field_shift=0)))
    app.connect()
    app.bring()
    assert app.w.status.text().startswith("Brought over 2 of 2 cheats")
    god = next(c for c in app.w.cheats.cheats if c.name == "God")
    assert god.carried == "test"
    assert pj.load(new_path()).cheats[1].test


def test_code_part_that_changed_names_the_part(make):
    tc.parts_notes()
    app = make(Console(run=3))
    app.connect()
    app.bring()
    assert "God (part jump couldn't be found (code at +" in app.w.status.text()
    assert [c.name for c in pj.load(new_path()).cheats] == ["Infinite health"]


# -- Cancel at each step, a dropped link, another game (AC-U3) --------------------------------
def _cancel_with(monkeypatch, app: App, where: str) -> None:
    carry = app.w.carry
    if where == "read":
        real = up.read_code_image

        def read(session, progress=None, cancel=None):
            carry.cancel()
            return real(session, progress, cancel)

        monkeypatch.setattr(cr.up, "read_code_image", read)
    elif where == "collect":
        carry.sleep = lambda _s: carry.cancel()
    elif where == "prove":
        real_prove = cr.ah.prove_slot

        def prove(*a, **kw):
            carry.cancel()
            return real_prove(*a, **kw)

        monkeypatch.setattr(cr.ah, "prove_slot", prove)
    elif where == "locate":
        real_find = cr.rf.find_again

        def find(*a, **kw):
            carry.cancel()
            return real_find(*a, **kw)

        monkeypatch.setattr(cr.rf, "find_again", find)


@pytest.mark.parametrize("where", ["read", "collect", "prove", "locate"])
def test_cancel_takes_every_hook_out_saves_nothing_and_keeps_the_offer(
    make, monkeypatch, where, caplog
):
    tc.learned_v1(3)
    before = old_files()
    caplog.set_level("INFO")
    app = make(Console(run=3))
    app.connect()
    _cancel_with(monkeypatch, app, where)
    app.bring()
    game = app.console.game
    assert game.fake.peek(fg.PID, fg.V2.reader, 10) == fg.V2.vmovss_r, "no jump left"
    assert not new_path().exists() and old_files() == before
    assert app.w.status.text() == hints.CARRY_CANCELLED
    assert app.bar.mode == "offer" and not app.w.cheats.finder.carrying
    assert app.w.cheats.finder.installed == {}
    if where == "locate":
        assert "action=hook_remove" in caplog.text


def test_dropped_link_saves_nothing_and_the_offer_comes_back(make, monkeypatch):
    tc.learned_v1(3)
    before = old_files()
    app = make(Console(run=3))
    app.connect()
    real_prove = cr.ah.prove_slot

    def prove(*a, **kw):
        app.console.game.fake.disconnect()
        return real_prove(*a, **kw)

    monkeypatch.setattr(cr.ah, "prove_slot", prove)
    app.bring()
    assert not new_path().exists() and old_files() == before
    assert app.bar.mode == "hidden" and not app.w.cheats.finder.carrying
    assert app.w.carry.run is None
    monkeypatch.setattr(cr.ah, "prove_slot", real_prove)
    app.console.restart()
    app.connect()
    assert app.bar.mode == "offer"


def test_another_game_process_cancels_the_run(make):
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    other = GameInfo(99, fg.TITLE, "Returnal", NEW, "eboot.bin", fg.BASE, fg.BASE + 0x6000)
    fired = []

    def sleep(_s):
        if not fired:
            fired.append(1)
            app.ctl.game_changed.emit(other)

    app.w.carry.sleep = sleep
    app.bring()
    assert app.w.carry.run is None and not new_path().exists()
    assert not app.w.cheats.finder.carrying


def test_needs_the_game_in_front(make):
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    app.console.game.fake.add_process(77, "eboot.bin", "PPSA00001", "Other", "1.0")
    app.console.game.fake.foreground_pid = 77
    app.bring()
    assert app.w.status.text() == hints.CARRY_NEEDS_GAME and not new_path().exists()


# -- new notes are never overwritten; Advanced adds new names only (AC-U7) ---------------------
def test_advanced_adds_only_new_names_to_notes_that_hold_values(make):
    old = tc.learned_v1(3)
    old.objects.append(tc.obolites())
    old.cheats.append(pj.ProjectCheat("Rich", "obj2", "Obolites", "set", 99999))
    tc.save(old)
    have = tc.health([], vtable=None)
    have.label = "Health"
    mine = pj.Project(
        fg.TITLE, NEW, objects=[have], cheats=[pj.ProjectCheat("Infinite health", "obj1", "Health")]
    )
    pj.save(new_path(), mine)
    app = make(Console(run=3, static=True))
    app.connect()
    assert app.bar.mode == "hidden", "notes that hold values: no offer"
    offered = []
    app.w.carry.pick_version = lambda versions: offered.append(versions) or versions[0]
    app.w.cheats.show_details.setChecked(True)
    app.w.cheats.bring_btn.click()
    assert offered == [[fg.VERSION]]
    saved = pj.load(new_path())
    assert [o.label for o in saved.objects] == ["Health", "Obolites"]
    assert [o.id for o in saved.objects] == ["obj1", "obj2"]
    assert saved.objects[0].anchors == [], "the value already here is left as it was"
    assert [c.name for c in saved.cheats] == ["Infinite health", "Rich"]
    assert saved.objects[1].check


def test_advanced_with_no_other_notes_says_so(make):
    app = make(Console())
    app.connect()
    app.w.carry.open_advanced()
    assert app.w.status.text() == "There are no notes for Returnal from another version."


# == AC-U9: Update for this version with the notes' hook in the file =========================
def test_update_rebuilds_hook_lines_after_a_bring_over(make):
    old = tc.learned_v1(1)
    cf, sigs = tc.hook_file(old)
    up.save_signatures(up.signatures_path(fg.TITLE, fg.VERSION), fg.TITLE, fg.VERSION, sigs)
    app = make(Console(layout=tc.SHIFTED))
    app.connect()

    def update(file):
        app.ctl.cheat_file = file
        app.w.cheats._check_version()
        assert not app.w.cheats.update_btn.isHidden()
        app.w.cheats.update_btn.click()
        return app.w.status.text()

    first, _ = tc.hook_file(old)
    text = update(first)
    assert text == (
        "Updated 1 of 3 cheats for version 03.001.000. Not moved: Find Health again (bring "
        "your notes over first (My cheats)); Health (PC off) (bring your notes over first "
        "(My cheats))"
    )
    app.ctl.cheat_file = None
    app.bring()
    assert app.w.status.text().startswith("Brought over 1 of 1 cheats")
    text = update(cf)
    assert text == "Updated 3 of 3 cheats for version 03.001.000."
    h = pj.load(new_path()).objects[0].hook
    find, _nop, keep = app.ctl.cheat_file.cheats
    assert find.locations[1].offset == h.hook and find.locations[0].offset == h.cave
    assert keep.value == 999.0
