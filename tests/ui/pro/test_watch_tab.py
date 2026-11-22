"""The Pro Watch tab (spec PR-3) on the fake game and its FakeDebugger."""

from __future__ import annotations

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.console.fake import FakeStop
from cheatmaker.core.pro import watch_session as ws
from cheatmaker.core.session import GUIDED_DEBUGGER
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.watch_tab import COL_COUNT, COL_FIRST, COL_TEXT, COL_WATCH
from tests import fakegame as fg
from tests.ui.pro.test_cheats_tab import new_cheat

HP_ADDR = fg.PLAYER_AT[1] + fg.HP
AFTER_WRITER = fg.WRITER + len(fg.VMOVSS_W)


@pytest.fixture
def watch(pro, game):
    tab = pro.watch
    tab.clock = game.fake.debugger.clock  # nothing waits for real
    return tab


def _hit(rip: int = AFTER_WRITER) -> WatchHit:
    return WatchHit(rip, {"rip": rip, "r12": fg.PLAYER_AT[1]})


def _add(tab, place: str = hex(HP_ADDR), kind: str = "write") -> None:
    tab.place.setText(place)
    tab.kind.setCurrentIndex(ws.WATCH_KINDS.index(kind))
    tab.add_btn.click()


def test_writer_listed_with_count_and_registers(watch, game, pro):  # AC-3.1
    game.fake.debugger.stops = [FakeStop(0, _hit()) for _ in range(3)]
    _add(watch)
    watch.start_btn.click()
    assert watch.table.rowCount() == 1
    assert watch.table.item(0, COL_TEXT).text() == "vmovss [r12+0x2ac], xmm1"
    assert watch.table.item(0, COL_COUNT).text() == "3"
    assert "r12=0x20000100" in watch.table.item(0, COL_FIRST).text()
    assert watch.table.item(0, COL_WATCH).text() == f"Writes {HP_ADDR:#x}"
    assert pro.status.text() == hints.WATCH_ENDED["time"].format(hits=3)


def test_end_clears_detaches_and_keeps_the_list(watch, game):  # AC-3.2
    game.fake.debugger.stops = [FakeStop(0, _hit())]
    _add(watch)
    watch.start_btn.click()
    d = game.fake.debugger
    assert d.state == "detached" and d.calls[-1] == ("detach",)
    assert watch.table.rowCount() == 1 and not watch.watching
    assert watch.ctl.session.debug_owner is None


def test_the_main_link_dropping_mid_watch_reaches_the_controller(watch, game, win, pro):  # m-B
    from cheatmaker.ui.messages import LOST

    game.fake.debugger.stops = [FakeStop(0, _hit(AFTER_WRITER)), FakeStop(0, _hit(fg.WRITER))]
    game.fake.game_ticks.append(game.fake.disconnect)  # the first read is the last
    _add(watch)
    watch.start_btn.click()
    assert not win.ctl.session.connected  # handled as a lost link (AC-1.4)
    assert pro.status.text() == LOST
    assert not watch.watching and game.fake.debugger.state == "detached"


def _as_if_watching(tab) -> None:
    import threading

    tab.targets = [ws.WatchTarget(HP_ADDR, 4, "write")]
    tab._fill_targets()
    tab._cancel = threading.Event()
    tab.running_targets = list(tab.targets)


def test_a_new_game_then_a_failed_watch_clears_the_old_list(watch):  # BUG-05
    from cheatmaker.errors import DebuggerLost

    _as_if_watching(watch)
    watch.game_state_changed(True)
    watch._failed(DebuggerLost("gone"))
    assert watch.targets == [] and watch.target_table.rowCount() == 0


def test_the_game_going_away_keeps_the_list(watch):  # BUG-05: only a new game clears
    _as_if_watching(watch)
    watch.alive = False  # the game stopped answering (same game)
    watch.game_state_changed(False)
    watch._ended(ws.WatchResult([], 0, ws.STOPPED, 1.0))
    assert len(watch.targets) == 1 and watch.target_table.rowCount() == 1


def test_registers_marked_unverified(watch):  # AC-3.3
    assert not ws.REGISTERS_VERIFIED
    header = watch.table.horizontalHeaderItem(COL_FIRST).text()
    assert header.endswith(hints.REGS_UNVERIFIED)


def test_limits_come_from_the_settings(watch, game, win):
    win.ctl.settings.pro_watch_hits = 2
    game.fake.debugger.flood(5, 0, _hit())
    _add(watch)
    watch.start_btn.click()
    assert watch.table.item(0, COL_COUNT).text() == "2"
    assert watch.state.text() == hints.WATCH_ENDED["hits"].format(hits=2)


def test_firewall_message_is_shown(watch, game, pro):  # AC-1.3
    game.fake.debugger.back_channel = False
    _add(watch)
    watch.start_btn.click()
    assert "port 755" in pro.status.text() and not watch.watching


def test_guided_watch_holds_the_debugger(watch, pro):
    _add(watch)
    with watch.ctl.session.claim_debugger(GUIDED_DEBUGGER):
        watch.start_btn.click()
    assert "in use by the main window" in pro.status.text()


def test_four_watches_at_most(watch, pro):
    for k in range(4):
        _add(watch, hex(HP_ADDR + 8 * k))
    assert len(watch.targets) == 4 and not watch.add_btn.isEnabled()
    assert not watch.add_target(ws.WatchTarget(HP_ADDR + 64, 4, "write"))
    assert pro.status.text() == hints.WATCH_FULL
    watch.target_table.selectRow(0)
    watch.remove_btn.click()
    assert len(watch.targets) == 3


def test_game_gone_stops_the_watch(watch, pro):  # AC-1.2
    import threading

    watch._cancel = cancel = threading.Event()  # as if a watch were running
    pro.alive = False
    pro._show_state()
    assert cancel.is_set() and pro.status.text() == hints.WATCH_GAME_GONE


def test_make_cave_entry_at_the_writer(watch, game, pro, win):
    cheat = new_cheat(pro.cheats)
    game.fake.debugger.stops = [FakeStop(0, _hit())]
    _add(watch)
    watch.start_btn.click()
    watch.table.selectRow(0)
    pro.cheats.select_cheat(cheat)
    n = len(cheat.pro.entries)
    watch.nop_btn.click()
    assert pro.tabs.currentWidget() is pro.cheats
    assert len(cheat.pro.entries) == n + 1
    assert cheat.pro.entries[-1].offset == fg.WRITER - fg.BASE


def test_show_in_code_and_a_row_outside_the_game(watch, game, pro):
    game.fake.debugger.stops = [FakeStop(0, _hit()), FakeStop(0, _hit()), FakeStop(0, _hit(0x10))]
    _add(watch)
    watch.start_btn.click()
    watch.table.selectRow(0)
    watch.code_btn.click()
    assert pro.tabs.currentWidget() is pro.code
    pro.tabs.setCurrentWidget(watch)
    watch.table.selectRow(1)
    assert watch.selected_row().text == ws.UNKNOWN_TEXT
    watch.cave_btn.click()
    assert pro.status.text() == hints.WATCH_PICK_ROW  # "?" rows can't be patched


def test_close_stops_a_running_watch(watch, pro):
    import threading

    watch._cancel = cancel = threading.Event()
    pro.force_close = True
    pro.close()
    assert cancel.is_set()


def test_export_make_cave_watches_the_pc_only_value(win, pro, game):
    from cheatmaker.core.cheats import Cheat, Location, LocationKind

    heap = Cheat(
        "Heap gold", [Location(LocationKind.SESSION_ADDRESS, HP_ADDR, b"\1\0\0\0", b"\0\0\0\0")]
    )
    win.ctl.ensure_file().cheats.append(heap)
    win.ctl.cheats_changed.emit()
    pro.watch.clock = game.fake.debugger.clock
    shown = []

    def run(dlg):
        shown.append(dlg)
        dlg.left_out.setCurrentRow(0)
        assert dlg.make_cave_btn.isEnabled()
        dlg.make_cave_btn.click()

    pro.cheats.exec_dialog = run
    pro.cheats.export_btn.click()
    assert shown and pro.tabs.currentWidget() is pro.watch
    assert pro.watch.running_targets == [ws.WatchTarget(HP_ADDR, 4, "write")]


def test_export_make_cave_says_why_a_misaligned_value_cant_be_watched(win, pro):  # M6
    from cheatmaker.core.cheats import Cheat, Location, LocationKind

    odd = Cheat(
        "Odd gold",
        [Location(LocationKind.SESSION_ADDRESS, HP_ADDR + 2, b"\1\0\0\0", b"\0\0\0\0")],
    )
    win.ctl.ensure_file().cheats.append(odd)
    win.ctl.cheats_changed.emit()
    tips = []

    def run(dlg):
        dlg.left_out.setCurrentRow(0)
        tips.append((dlg.make_cave_btn.isEnabled(), dlg.make_cave_btn.toolTip()))

    pro.cheats.exec_dialog = run
    pro.cheats.export_btn.click()
    ((enabled, tip),) = tips
    assert not enabled and "divisible by its size" in tip
