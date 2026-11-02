"""v3 UI flows on the fake two-run game: Find it next time, restart, found again (AC-11..13)."""

from __future__ import annotations

import json
import struct

import pytest
from PySide6.QtCore import Qt

from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.values import ValueType
from cheatmaker.settings import Settings
from cheatmaker.ui import hints
from cheatmaker.ui.cheats_view import COL_KEEPS, COL_ON, FOUND_AGAIN
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.find_again import LearnDialog, PickDialog
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.save_dialog import SaveDialog
from cheatmaker.ui.tasks import TaskRunner
from tests import fakegame as fg


class Console:
    """Which run of the game the 'PS5' is in right now."""

    def __init__(self) -> None:
        self.game = fg.build(1, connect=False)

    def restart(self, run: int, **kw) -> fg.Game:
        self.game = fg.build(run, connect=False, **kw)
        return self.game


@pytest.fixture
def console():
    return Console()


@pytest.fixture
def win(qtbot, console, monkeypatch):
    monkeypatch.setattr(rf.ah, "WATCH_SECONDS", 1)
    monkeypatch.setattr(rf.ah, "PROVE_SECONDS", 1)
    monkeypatch.setattr(rf, "SLOT_WAIT_S", 0.5)
    ctl = Controller(lambda ip, port: console.game.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.cheats.finder.ask_confirm = lambda text: True
    w.cheats.finder.sleep = lambda _s: None
    yield w
    ctl.shutdown()


def connect(w):
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()


def add_health_cheat(w, game: fg.Game) -> Cheat:
    raw = struct.pack("<f", 999.0)
    c = Cheat(
        "Infinite Health",
        [Location(LocationKind.SESSION_ADDRESS, game.health_addr, raw, raw)],
        freeze=True,
        value_type=ValueType.FLOAT,
    )
    w.ctl.ensure_file().cheats.append(c)
    w.ctl.cheats_changed.emit()
    w.cheats.table.selectRow(len(w.cheats.cheats) - 1)
    return c


def learn(w, cheat) -> LearnDialog:
    dlg = LearnDialog(w.ctl, w.cheats.finder, cheat, w)
    dlg.start_btn.click()
    return dlg


def tick(w, row=0):
    item = w.cheats.table.item(row, COL_ON)
    item.setCheckState(Qt.CheckState.Checked)


def test_find_it_next_time_then_found_again_after_restart(win, console):  # AC-11.1, AC-12.1
    connect(win)
    cheat = add_health_cheat(win, console.game)
    assert win.cheats.learn_btn.isEnabled()
    dlg = learn(win, cheat)
    assert dlg.hint.text().startswith(hints.READY["hook_slot"].format(name="Infinite Health"))
    assert "small hook to the game's code" in dlg.hint.text()  # AC-11.2
    assert cheat.anchor == ("obj1", "Infinite Health")
    assert console.game.fake.peek(fg.PID, fg.READER, 1) == b"\xe9", "hook in while connected"
    path = pj.project_path(fg.TITLE, fg.VERSION)
    saved = pj.load(path)
    assert saved.cheats[0].name == "Infinite Health" and saved.objects[0].hook is not None
    assert win.cheats.table.item(0, COL_KEEPS).text() == FOUND_AGAIN

    win.ctl.shutdown()  # close the app: the hook comes out (AC-11.2)
    assert console.game.fake.peek(fg.PID, fg.READER, 10) == fg.VMOVSS_R

    run2 = console.restart(2)  # restart the game, start Cheat Maker, connect
    connect(win)
    assert "Infinite Health found again by the game's code" in win.status.text()
    tracker = win.ctl.live.trackers["obj1"]
    assert tracker.address == run2.player and not tracker.confirmed
    tick(win)  # click 1 (+ "Yes" in the confirm: click 2)
    assert run2.health() == 999.0, "set without typing a number"
    assert tracker.confirmed


def test_restart_with_a_fresh_app_brings_the_cheat_back(win, console, qtbot):
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    win.ctl.shutdown()
    run2 = console.restart(2)
    ctl = Controller(lambda ip, port: run2.fake, Settings.load(), TaskRunner(sync=True))
    w2 = MainWindow(ctl)
    qtbot.addWidget(w2)
    w2.cheats.finder.ask_confirm = lambda text: True
    w2.cheats.finder.sleep = lambda _s: None
    connect(w2)
    [c] = w2.ctl.cheat_file.cheats
    assert c.name == "Infinite Health" and c.anchor == ("obj1", "Infinite Health") and c.freeze
    tick(w2)
    assert run2.health() == 999.0
    run2.set_health(3.0)
    w2.ctl.freezer.tick()
    assert run2.health() == 999.0
    ctl.shutdown()


def test_confirm_no_writes_nothing(win, console):  # AC-12.2
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    win.ctl.shutdown()
    run2 = console.restart(2)
    connect(win)
    asked = []
    win.cheats.finder.ask_confirm = lambda text: asked.append(text) or False
    tick(win)
    assert "Is this your Infinite Health? It shows 100.00" in asked[0]
    assert run2.fake.writes == [] or all(a != run2.health_addr for _p, a, _d in run2.fake.writes)
    assert not win.ctl.cheat_file.cheats[0].enabled
    assert win.status.text().startswith("OK, nothing was changed")


def test_not_found_opens_the_search_prefilled(win, console):  # AC-12.4
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    win.ctl.shutdown()
    run2 = console.restart(2)
    run2.fake.poke(fg.PID, run2.player, bytes(0x300))  # the player isn't there at all
    connect(win)
    assert "Couldn't find Infinite Health this time" in win.status.text()
    assert win.tabs.currentWidget() is win.find
    assert win.find.number.text() == "100" and win.find.vtype.currentData() is ValueType.FLOAT
    tick(win)  # never writes blind
    assert all(a != run2.health_addr for _p, a, _d in run2.fake.writes)
    assert not win.ctl.cheat_file.cheats[0].enabled


def test_disagree_asks_to_pick(win, console):  # AC-12.3
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    win.ctl.shutdown()
    run2 = console.restart(2)
    twin = fg.HEAP + 0x9000
    run2.fake.poke(fg.PID, twin, run2.fake.peek(fg.PID, run2.player, 0x300))
    run2.touched = []  # the hook never runs, so the memory search finds both
    seen = []

    def pick(obj, options):
        seen.extend(options)
        return twin

    win.cheats.finder.ask_pick = pick
    connect(win)
    assert {o.address for o in seen} == {run2.player, twin}
    tracker = win.ctl.live.trackers["obj1"]
    assert tracker.address == twin and tracker.confirmed, "picked by watching it"


def test_pick_dialog_lists_live_values(qtbot, console):
    game = fg.build(2)
    ctl = Controller(lambda ip, port: game.fake, Settings.load(), TaskRunner(sync=True))
    ctl.session.connect("1.2.3.4")
    ctl.session.refresh_game()
    obj = pj.ObjectSpec(
        "obj1", "Health", fg.PLAYER_VT - fg.BASE, [pj.Field("Health", fg.HP, ValueType.FLOAT)]
    )
    twin = fg.HEAP + 0x9000
    game.fake.poke(fg.PID, twin, game.fake.peek(fg.PID, game.player, 0x300))
    game.set_health(42.0, twin)
    dlg = PickDialog(ctl, obj, [rf.Found("hook_slot", game.player), rf.Found("vtable_scan", twin)])
    qtbot.addWidget(dlg)
    assert dlg.list.count() == 2 and dlg.list.item(1).text().startswith("Place 2: 42.00")
    assert "found by a memory search" in dlg.list.item(1).text()
    dlg.list.setCurrentRow(1)
    dlg.use_btn.click()
    assert dlg.picked == twin
    ctl.shutdown()


def test_save_explains_app_only_and_can_add_the_hook(win, console, tmp_path):  # AC-13
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    dlg = SaveDialog(win.ctl, win)
    dlg.folder.setText(str(tmp_path))
    assert "work only while Cheat Maker is connected" in dlg.skipped.text()
    assert not dlg.with_hook.isChecked() and not dlg.with_hook.isHidden()  # D14: off
    dlg.with_hook.setChecked(True)
    path = dlg.do_save()
    doc = json.loads(path.read_text())
    [hook] = doc["mods"]
    assert hook["name"] == "Cheat Maker: find Infinite Health again"
    assert len(hook["memory"]) == 2  # the routine, then the jump into it
    offsets = {int(m["offset"], 16) for m in hook["memory"]}
    assert fg.READER - fg.BASE in offsets


def test_simple_static_value_is_ready_without_a_hook(qtbot, monkeypatch):
    from tests.conftest import STATIC_ADDR, build_fake

    fake = build_fake()
    ctl = Controller(lambda ip, port: fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    connect(w)
    raw = struct.pack("<i", 5)
    c = Cheat(
        "Skill points",
        [Location(LocationKind.SESSION_ADDRESS, STATIC_ADDR, raw, raw)],
        value_type=ValueType.INT32,
    )
    ctl.ensure_file().cheats.append(c)
    dlg = LearnDialog(ctl, w.cheats.finder, c, w)
    dlg.start_btn.click()
    assert dlg.hint.text() == hints.READY["static"].format(name="Skill points")
    assert fake.watched == [] and fake.writes == []
    ctl.shutdown()


def test_find_it_again_button_and_value_edit_is_remembered(win, console):
    from cheatmaker.ui.cheats_view import COL_VALUE

    connect(win)
    cheat = add_health_cheat(win, console.game)
    learn(win, cheat)
    assert win.cheats.again_btn.isEnabled()
    win.ctl.live.trackers.clear()
    win.cheats.again_btn.click()
    assert "obj1" in win.ctl.live.trackers
    win.cheats.table.item(0, COL_VALUE).setText("20000")
    saved = pj.load(pj.project_path(fg.TITLE, fg.VERSION))
    assert saved.cheats[0].value == 20000.0
    assert saved.objects[0].field("Infinite Health").range[1] >= 20000


def test_pc_off_cheat_keeps_the_value_after_a_restart(win, console):  # AC-13.1
    connect(win)
    dlg = learn(win, add_health_cheat(win, console.game))
    assert not dlg.pc_off_btn.isHidden()
    dlg.pc_off_btn.click()
    pc_off = win.ctl.cheat_file.cheats[-1]
    assert pc_off.saveable and pc_off.name == "Infinite Health (works with the PC off)"
    win.ctl.shutdown()
    run2 = console.restart(2)  # CheatRunner loads the saved lines into the new run
    for loc in pc_off.locations:
        run2.fake.poke(fg.PID, fg.BASE + loc.offset, loc.on)
    run2.set_health(12.0)
    run2.fake.read(fg.PID, fg.BASE, 1)  # a frame runs
    assert run2.health() == 999.0
