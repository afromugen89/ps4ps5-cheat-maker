"""End-to-end UI flows on the FakeConsole (pytest-qt, offscreen)."""

from __future__ import annotations

import json
import struct

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from cheatmaker.console.uploader import Uploader
from cheatmaker.settings import Settings
from cheatmaker.ui import hints, messages
from cheatmaker.ui.cheats_view import COL_KEEPS, COL_NAME, COL_ON, COL_VALUE
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.lock_wizard import LockWizard
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.save_dialog import SaveDialog
from cheatmaker.ui.tasks import TaskRunner
from tests.conftest import GOLD_ADDR, HEALTH_ADDR, PID, STATIC_ADDR, WRITER_ADDR, build_fake
from tests.unit.test_uploader import FakeFTP


@pytest.fixture
def fake():
    return build_fake()


@pytest.fixture
def win(qtbot, fake):
    FakeFTP.files, FakeFTP.dirs, FakeFTP.fail_connect = {}, {"/", "/data"}, False
    ctl = Controller(
        lambda ip, port: fake,
        Settings.load(),
        TaskRunner(sync=True),
        uploader_factory=lambda: Uploader("1.2.3.4", ftp_factory=FakeFTP),
    )
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    yield w
    ctl.shutdown()


def connect(w, ip="192.168.1.50"):
    w.connect_view.ip.setText(ip)
    w.connect_view.connect_btn.click()


def search(w, text):
    w.find.number.setText(text)
    w.find.search_btn.click()


def i32(fake, addr):
    return struct.unpack("<i", fake.peek(PID, addr, 4))[0]


def test_connect_shows_game_and_remembers_ip(win):  # AC-1.1, AC-1.3, AC-2.1
    connect(win)
    assert win.connect_view.status.text().startswith("Connected")
    assert "PPSA01234" in win.connect_view.game.text()
    assert "version 01.000" in win.connect_view.game.text()
    assert Settings.load().last_ip == "192.168.1.50"
    w2 = MainWindow(win.ctl)
    assert w2.connect_view.ip.text() == "192.168.1.50"
    w2.deleteLater()


def test_connect_cleans_pasted_url(win):
    connect(win, "http://192.168.0.193/")
    assert win.connect_view.ip.text() == "192.168.0.193"
    assert Settings.load().last_ip == "192.168.0.193"


def test_connect_unreachable_message(qtbot):  # AC-1.2
    from cheatmaker.errors import ConsoleUnreachable

    def factory(ip, port):
        raise ConsoleUnreachable("refused")

    ctl = Controller(factory, Settings(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    connect(w)
    assert w.status.text() == messages.UNREACHABLE
    assert w.connect_view.status.text() == "Not connected"


def test_no_game_then_refresh(win, fake):  # AC-2.2, AC-2.3
    proc = fake._procs.pop(PID)
    fake.foreground_pid = None
    connect(win)
    assert win.status.text() == messages.NO_GAME
    fake._procs[PID] = proc
    fake.foreground_pid = PID
    win.connect_view.refresh_btn.click()
    assert "PPSA01234" in win.connect_view.game.text()


def test_find_set_freeze_make_cheat(win, fake):  # AC-3, AC-4, AC-5
    connect(win)
    search(win, "100")
    assert win.find.count.text() == "2 matches"
    assert win.find.hint.text() == hints.FIND_FEW and win.find.table.rowCount() == 2
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 95))
    search(win, "95")
    assert win.find.count.text() == "1 matches" and win.find.table.rowCount() == 1
    assert win.find.table.isColumnHidden(1), "no addresses unless Advanced"
    win.find.table.selectRow(0)
    win.find.set_value.setText("250")
    win.find.set_btn.click()
    assert i32(fake, HEALTH_ADDR) == 250
    win.find.freeze_btn.click()
    assert win.find.freeze_btn.text() == "Unfreeze"
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 10))
    win.ctl.freezer.tick()
    assert i32(fake, HEALTH_ADDR) == 250
    win.find.freeze_btn.click()
    assert not win.ctl.frozen_addresses
    win.find.set_value.clear()
    win.find.template.setCurrentIndex(0)  # Infinite Health
    win.find.make_btn.click()
    assert win.tabs.currentWidget() is win.cheats
    t = win.cheats.table
    assert t.item(0, COL_NAME).text() == "Infinite Health"
    assert t.item(0, COL_KEEPS).text() == "This session only"
    assert win.cheats.hint.text() == hints.CHEATS_SESSION


def test_bad_number_message(win):
    connect(win)
    search(win, "lots")
    assert "isn't a number" in win.status.text()


def test_toggle_edit_value_and_rename(win, fake):  # AC-5.3, AC-7.1
    connect(win)
    search(win, "2450")
    win.find.table.selectRow(0)
    win.find.template.setCurrentIndex(1)  # Infinite Money
    win.find.make_btn.click()
    t = win.cheats.table
    t.item(0, COL_ON).setCheckState(Qt.CheckState.Checked)
    assert i32(fake, GOLD_ADDR) == 999_999
    t.item(0, COL_VALUE).setText("5,000")
    assert i32(fake, GOLD_ADDR) == 5000
    t.item(0, COL_NAME).setText("Rich")
    assert win.ctl.cheat_file.cheats[0].name == "Rich"
    t.item(0, COL_ON).setCheckState(Qt.CheckState.Unchecked)
    assert i32(fake, GOLD_ADDR) == 2450


def test_write_failure_message(win, fake):  # AC-4.3
    connect(win)
    search(win, "2450")
    win.find.table.selectRow(0)
    fake.fail_writes = True
    win.find.set_value.setText("1")
    win.find.set_btn.click()
    assert win.status.text().startswith("Couldn't change this value")


def test_lock_it_then_save_json_and_send(win, fake, tmp_path):  # D8, AC-6.1, AC-6.3
    connect(win)
    search(win, "100")
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 99))
    search(win, "99")
    win.find.table.selectRow(0)
    win.find.make_btn.click()
    win.cheats.table.selectRow(0)
    assert win.cheats.lock_btn.isEnabled()
    wiz = LockWizard(win.ctl, win.ctl.cheat_file.cheats[0], win)
    wiz.start_btn.click()
    assert wiz.options.count() == 1 and "changed it 2 times" in wiz.options.item(0).text()
    wiz.test_btn.click()
    assert fake.peek(PID, WRITER_ADDR, 2) == b"\x90\x90"
    assert wiz.hint.text().startswith("Option 1 is on")
    wiz.keep_btn.click()
    c = win.ctl.cheat_file.cheats[0]
    assert c.saveable and win.cheats.table.item(0, COL_KEEPS).text() == "Yes"

    dlg = SaveDialog(win.ctl, win)
    dlg.folder.setText(str(tmp_path))
    assert dlg.file_label.text() == "File: PPSA01234_01.000.json"
    path = dlg.do_save()
    doc = json.loads(path.read_text())
    assert doc["id"] == "PPSA01234" and doc["mods"][0]["memory"][0] == {
        "offset": f"{WRITER_ADDR - 0x400000:X}",
        "on": "9090",
        "off": "8908",
    }
    asked = []
    dlg.confirm = lambda name: asked.append(name) or True
    dlg.send_btn.click()
    assert asked == ["PPSA01234_01.000.json"], "overwrite prompt for the PC copy"
    assert "/data/cheatrunner/cheats/json/PPSA01234_01.000.json" in FakeFTP.files
    assert dlg.result.text().startswith("Sent to the PS5")


def test_save_shn_and_overwrite_declined(win, fake, tmp_path):  # AC-6.2, AC-6.5
    connect(win)
    search(win, "5")
    win.find.table.selectRow(
        next(i for i, m in enumerate(win.find.rows) if m.address == STATIC_ADDR)
    )
    win.find.template.setCurrentIndex(3)  # Max Skill Points
    win.find.make_btn.click()
    assert win.cheats.table.item(0, COL_KEEPS).text() == "Yes"
    dlg = SaveDialog(win.ctl, win)
    dlg.folder.setText(str(tmp_path))
    dlg.fmt.setCurrentIndex(1)
    p = dlg.do_save()
    assert p.name == "PPSA01234_01.000.shn" and b"<ValueOn>63-00-00-00</ValueOn>" in p.read_bytes()
    assert Settings.load().output_format == "shn"
    dlg.confirm = lambda name: False
    assert dlg.do_save() is None


def test_send_fails_falls_back_to_saved_path(win, fake, tmp_path):
    connect(win)
    search(win, "5")
    win.find.table.selectRow(
        next(i for i, m in enumerate(win.find.rows) if m.address == STATIC_ADDR)
    )
    win.find.make_btn.click()
    FakeFTP.fail_connect = True
    dlg = SaveDialog(win.ctl, win)
    dlg.folder.setText(str(tmp_path))
    dlg.do_send()
    assert win.status.text().startswith("Couldn't send the file")
    assert "Copy it to /data/cheatrunner/cheats/json/" in dlg.result.text()


def test_open_file_version_warning_and_broken_file(win, fake, tmp_path):  # AC-7.1-7.3
    connect(win)
    good = tmp_path / "PPSA01234_01.010.json"
    good.write_text(
        json.dumps(
            {
                "name": "T",
                "id": "PPSA01234",
                "version": "01.010",
                "process": "eboot.bin",
                "mods": [
                    {
                        "name": "Lock",
                        "type": "checkbox",
                        "memory": [{"offset": "100", "on": "9090", "off": "8908"}],
                    }
                ],
            }
        )
    )
    win.cheats.pick_file = lambda: str(good)
    win.cheats.open_btn.click()
    assert win.cheats.table.item(0, COL_NAME).text() == "Lock"
    assert "made for version 01.010; you're running 01.000" in win.cheats.warning.text()
    win.cheats.table.item(0, COL_ON).setCheckState(Qt.CheckState.Checked)
    assert fake.peek(PID, WRITER_ADDR, 2) == b"\x90\x90"
    win.cheats.table.selectRow(0)
    win.cheats.delete_btn.click()
    assert fake.peek(PID, WRITER_ADDR, 2) == b"\x89\x08" and win.cheats.table.rowCount() == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    win.cheats.pick_file = lambda: str(bad)
    win.cheats.open_btn.click()
    assert win.status.text() == messages.CANT_READ_FILE


def test_connection_lost_marks_cheats_off(win, fake):  # AC-1.4
    connect(win)
    search(win, "2450")
    win.find.table.selectRow(0)
    win.find.template.setCurrentIndex(1)
    win.find.make_btn.click()
    win.cheats.table.item(0, COL_ON).setCheckState(Qt.CheckState.Checked)
    fake.disconnect()
    win.ctl._lost.emit()  # what the freezer thread does
    assert win.status.text() == messages.LOST
    assert not win.ctl.cheat_file.cheats[0].enabled
    assert win.cheats.table.item(0, COL_ON).checkState() == Qt.CheckState.Unchecked
    assert win.connect_view.status.text() == "Not connected"


def test_first_run_warning_once(win, monkeypatch):  # AC-8.3
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: shown.append(a[2]))
    win.show_first_run_warning()
    win.show_first_run_warning()
    assert len(shown) == 1 and "Back up your saves" in shown[0]
    assert Settings.load().first_run_done


def test_copy_log(win, monkeypatch):  # AC-9.4
    from cheatmaker.ui import main_window

    monkeypatch.setattr(main_window, "read_latest_log", lambda: "LOG TEXT")
    win.copy_log_btn.click()
    from PySide6.QtGui import QGuiApplication

    assert QGuiApplication.clipboard().text() == "LOG TEXT"


def test_every_screen_has_a_hint(win):  # AC-8.2
    assert win.find.hint.text() and win.cheats.hint.text()
    assert win.connect_view.game.text() == hints.CONNECT


def test_type_in_address_then_set_freeze_remove(win, fake):
    from cheatmaker.core.values import ValueType

    connect(win)
    f = win.find
    f.add_type.setCurrentIndex(list(ValueType).index(ValueType.INT32))
    f.add_addr.setText(f"{HEALTH_ADDR:#x}")
    f.add_btn.click()
    assert f.table.rowCount() == 1 and f.table.item(0, 0).text().startswith("100")
    assert "added, " in f.table.item(0, 0).text() and f.selected().address == HEALTH_ADDR
    f.set_value.setText("555")
    f.set_btn.click()
    assert i32(fake, HEALTH_ADDR) == 555
    f.freeze_btn.click()
    assert HEALTH_ADDR in win.ctl.frozen_addresses
    f.remove_btn.click()
    assert f.table.rowCount() == 0 and HEALTH_ADDR not in win.ctl.frozen_addresses


def test_type_in_bad_or_unmapped_address(win):
    connect(win)
    win.find.add_addr.setText("hello")
    win.find.add_btn.click()
    assert "isn't an address" in win.status.text()
    win.find.add_addr.setText("0x10")
    win.find.add_btn.click()
    assert win.status.text().startswith("Nothing is at 0x10")
    assert win.find.table.rowCount() == 0


def test_lock_it_test_all_at_once(win, fake):
    second = WRITER_ADDR + 0x40
    fake.poke(PID, second, b"\x89\x08")  # another "mov [rax],ecx"
    fake.writers[HEALTH_ADDR] = [WRITER_ADDR + 2, second + 2, second + 2]
    connect(win)
    search(win, "100")
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 99))
    search(win, "99")
    win.find.table.selectRow(0)
    win.find.make_btn.click()
    wiz = LockWizard(win.ctl, win.ctl.cheat_file.cheats[0], win)
    wiz.start_btn.click()
    assert wiz.options.count() == 2 and wiz.test_all_btn.isEnabled()
    wiz.test_all_btn.click()
    assert fake.peek(PID, WRITER_ADDR, 2) == b"\x90\x90" == fake.peek(PID, second, 2)
    assert wiz.hint.text().startswith("All options are on")
    wiz.keep_btn.click()
    assert len(win.ctl.cheat_file.cheats[0].locations) == 2


def test_pointer_scan_use_and_recheck_after_restart(win, fake):
    from cheatmaker.core.values import ValueType
    from cheatmaker.ui.pointer_dialog import PointerDialog
    from tests.conftest import HEAP_BASE
    from tests.unit.test_pointers import plant_chain

    plant_chain(fake)
    connect(win)
    f = win.find
    f.add_type.setCurrentIndex(list(ValueType).index(ValueType.INT32))
    f.add_addr.setText(f"{HEALTH_ADDR:#x}")
    f.add_btn.click()
    dlg = PointerDialog(win.ctl, HEALTH_ADDR, ValueType.INT32, f.use_pointer, f)
    dlg.scan_btn.click()
    assert dlg.list.count() >= 1 and dlg.list.item(0).text() == "[[base+0x1020]+0x10]+0x40"
    # "restart": health moves; the saved list is re-checked against the new address
    moved = HEAP_BASE + 0x800
    fake.poke(PID, moved, struct.pack("<i", 77))
    plant_chain(fake, moved)
    dlg2 = PointerDialog(win.ctl, moved, ValueType.INT32, f.use_pointer, f)
    assert dlg2.list.count() >= 1, "loaded from the saved file"
    dlg2.recheck_btn.click()
    assert "still lead here" in dlg2.hint.text()
    dlg2.use_btn.click()
    row = f.table.rowCount() - 1
    assert f.table.item(row, 0).text().startswith("77") and "pointer" in f.table.item(row, 0).text()
    f.set_value.setText("999")
    f.set_btn.click()
    assert i32(fake, moved) == 999


def test_player_only_collect_test_keep(qtbot, monkeypatch):
    from cheatmaker.core import cave as cv
    from cheatmaker.core.cheats import Cheat, Location, LocationKind
    from cheatmaker.core.values import ValueType
    from cheatmaker.ui import player_only_dialog as pod
    from tests.unit import test_cave as tc

    fake, _s = tc.game()
    ctl = Controller(lambda ip, port: fake, Settings.load(), TaskRunner(sync=True))
    ctl.session.connect("1.2.3.4")
    ctl.session.refresh_game()
    health = Cheat(
        "Infinite Health",
        [Location(LocationKind.SESSION_ADDRESS, tc.PLAYER + tc.HP, b"\0\0zD", b"\0\0zD")],
        value_type=ValueType.FLOAT,
    )
    ctl.ensure_file().cheats.append(health)
    real = cv.collect_objects

    def fake_collect(session, writer, seconds, cancel=None):
        table = cv.find_table(session)

        def fight(_t):
            for obj in (tc.PLAYER, tc.ENEMY1, tc.ENEMY2):
                slot = (obj >> 4) & (cv.TABLE_SLOTS - 1)
                fake.poke(tc.PID, table + slot * 8, struct.pack("<Q", obj))

        return real(session, writer, 0.25, cancel, sleep=fight)

    monkeypatch.setattr(pod.cv, "collect_objects", fake_collect)
    dlg = pod.PlayerOnlyDialog(ctl, health, tc.WRITER)
    qtbot.addWidget(dlg)
    assert dlg.player_obj == tc.PLAYER and dlg.value.text() == "1000"
    dlg.collect_btn.click()
    assert dlg.fields.count() >= 1 and dlg.fields.item(0).text().startswith("+0x8: you 0x40048")
    dlg.value.setText("999")
    dlg.test_btn.click()
    assert fake.peek(tc.PID, tc.HOOK, 1) == b"\xe9"
    assert dlg.hint.text().startswith("On.")
    dlg.keep_btn.click()
    kept = ctl.cheat_file.cheats[0]
    assert kept.saveable and len(kept.locations) == 2
    ctl.shutdown()


def test_show_up_to_n_matches_is_saved(win, fake):
    connect(win)
    search(win, "100")  # 2 matches: shown with the default 10
    assert win.find.table.rowCount() == 2
    win.find.show_n.setValue(1)
    assert win.find.table.rowCount() == 0, "2 matches > 1: no list"
    assert Settings.load().show_matches == 1
    win.find.show_n.setValue(50)
    assert win.find.table.rowCount() == 2


def test_filter_listed_rows_by_value(win, fake):
    connect(win)
    search(win, "100")
    f = win.find
    assert f.table.rowCount() == 2
    other = next(m.address for m in f.rows if m.address != HEALTH_ADDR)
    fake.poke(PID, other, struct.pack("<i", 250))
    f.refresh_live()
    f.filter.setText("200-300")
    hidden = [f.table.isRowHidden(i) for i in range(2)]
    assert sorted(hidden) == [False, True], "one row matches 250"
    assert f.filter_count.text() == "1 of 2 shown"
    shown = hidden.index(False)
    f.table.selectRow(shown)
    assert f.selected().address == other, "hidden rows keep the row -> match mapping"
    f.filter.setText("90-")  # half typed: show everything
    assert not any(f.table.isRowHidden(i) for i in range(2)) and f.filter_count.text() == ""
    f.filter.setText("100")
    assert f.rows[[f.table.isRowHidden(i) for i in range(2)].index(False)].address == HEALTH_ADDR
    f.filter.clear()
    assert not any(f.table.isRowHidden(i) for i in range(2))


def test_update_cheats_for_new_game_version(win, fake, tmp_path):
    """v2: learn signatures while 01.000 runs, then move the cheat after an update."""
    from tests.conftest import MODULE_BASE

    vmovss = bytes.fromhex("C4C17A118C24AC020000")

    def site(rip_disp: int) -> bytes:  # mov rax,[rip+disp]; vmovss [r12+2AC],xmm1; ret
        return bytes.fromhex("488B05") + struct.pack("<i", rip_disp) + vmovss + b"\xc3"

    fake.poke(PID, MODULE_BASE + 0x200, site(0x100))
    connect(win)
    old = tmp_path / "PPSA01234_01.000.json"
    old.write_text(
        json.dumps(
            {
                "name": "T",
                "id": "PPSA01234",
                "version": "01.000",
                "process": "eboot.bin",
                "mods": [
                    {
                        "name": "No damage",
                        "type": "checkbox",
                        "memory": [{"offset": "207", "on": "90" * 10, "off": vmovss.hex()}],
                    }
                ],
            }
        )
    )
    win.cheats.pick_file = lambda: str(old)
    win.cheats.open_btn.click()
    assert win.cheats.update_btn.isHidden()
    from cheatmaker.core import updater as up

    learned = up.load_signatures(up.signatures_path("PPSA01234", "01.000"))
    assert learned[0x207].text().startswith("C4 C1 7A 11 8C 24 AC 02 00 00 C3")

    # the game updates: code moves and the RIP-relative distance changes
    fake.poke(PID, MODULE_BASE + 0x200, b"\xcc" * 18)
    fake.poke(PID, MODULE_BASE + 0x340, site(0x80))
    fake._procs[PID].version = "01.010"
    win.ctl.session.refresh_game()
    assert "made for version 01.000" in win.cheats.warning.text()
    assert not win.cheats.update_btn.isHidden()
    win.cheats.update_btn.click()
    cf = win.ctl.cheat_file
    assert cf.version == "01.010" and cf.cheats[0].locations[0].offset == 0x347
    assert "Updated 1 of 1 cheats for version 01.010" in win.cheats.warning.text()
    assert win.cheats.update_btn.isHidden()
    win.cheats.table.item(0, COL_ON).setCheckState(Qt.CheckState.Checked)
    assert fake.peek(PID, MODULE_BASE + 0x347, 10) == b"\x90" * 10
    assert 0x347 in up.load_signatures(up.signatures_path("PPSA01234", "01.010"))
