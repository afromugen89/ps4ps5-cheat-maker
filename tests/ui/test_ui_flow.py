"""End-to-end UI flows on the FakeConsole (pytest-qt, offscreen)."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from cheatmaker.console.uploader import Uploader
from cheatmaker.core.guide import Action, Shows
from cheatmaker.core.values import ValueType
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
    assert win.find.hint.text().startswith(hints.FIND_FEW) and win.find.table.rowCount() == 2
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 95))
    search(win, "95")
    assert win.find.count.text() == "1 matches" and win.find.table.rowCount() == 1
    assert not win.find.table.isColumnHidden(1), "addresses show without Advanced"
    assert win.find.table.item(0, 1).text() == f"{HEALTH_ADDR:#x}"
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


@pytest.mark.parametrize("fmt_index", [0, 1])
def test_saved_value_comes_back_on_open_and_can_be_edited(win, fake, tmp_path, fmt_index):
    connect(win)
    search(win, "5")
    win.find.table.selectRow(
        next(i for i, m in enumerate(win.find.rows) if m.address == STATIC_ADDR)
    )
    win.find.template.setCurrentIndex(3)  # Max Skill Points
    win.find.make_btn.click()
    t = win.cheats.table
    t.item(0, COL_VALUE).setText("250")
    dlg = SaveDialog(win.ctl, win)
    dlg.folder.setText(str(tmp_path))
    dlg.fmt.setCurrentIndex(fmt_index)
    path = dlg.do_save()
    win.cheats.pick_file = lambda: str(path)
    win.cheats.open_btn.click()
    item = t.item(0, COL_VALUE)
    assert item.text() == "250", "the value is still there after Save and Open"
    assert item.flags() & Qt.ItemFlag.ItemIsEditable
    item.setText("300")
    t.item(0, COL_ON).setCheckState(Qt.CheckState.Checked)
    assert i32(fake, STATIC_ADDR) == 300


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
    # "mov [rax+10h],ecx" twice: object + offset writers (bare [rax] ones are skipped)
    fake.poke(PID, WRITER_ADDR, b"\x89\x48\x10")
    fake.poke(PID, second, b"\x89\x48\x10")
    fake.writers[HEALTH_ADDR] = [WRITER_ADDR + 3, second + 3, second + 3]
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
    assert fake.peek(PID, WRITER_ADDR, 3) == b"\x90" * 3 == fake.peek(PID, second, 3)
    assert wiz.hint.text().startswith("All options are on")
    wiz.keep_btn.click()
    assert len(win.ctl.cheat_file.cheats[0].locations) == 2


def test_lock_it_test_all_skips_shared_writers(win, fake):
    second = WRITER_ADDR + 0x40
    fake.poke(PID, second, b"\x89\x08")  # two bare "mov [rax],ecx": maybe shared code
    fake.writers[HEALTH_ADDR] = [WRITER_ADDR + 2, second + 2]
    connect(win)
    search(win, "100")
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 99))
    search(win, "99")
    win.find.table.selectRow(0)
    win.find.make_btn.click()
    wiz = LockWizard(win.ctl, win.ctl.cheat_file.cheats[0], win)
    wiz.start_btn.click()
    assert wiz.options.count() == 2 and not wiz.test_all_btn.isEnabled()
    assert "may be shared" in wiz.options.item(0).text()
    assert wiz.test_btn.isEnabled()  # still testable one at a time


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


def test_went_down_by_and_stand_still(win, fake, monkeypatch):  # AC-H1, AC-H4
    from cheatmaker.core import scanner

    monkeypatch.setattr(scanner, "STILL_INTERVAL_S", 0)
    connect(win)
    search(win, "100")
    assert win.find.count.text() == "2 matches"
    assert win.find.down_by_btn.isEnabled() and win.find.still_btn.isEnabled()
    win.find.still_btn.click()
    assert win.find.count.text() == "2 matches"
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 85))
    win.find.by_amount.setText("15")
    win.find.down_by_btn.click()
    assert win.find.count.text() == "1 matches"
    assert win.find.rows[0].address == HEALTH_ADDR


def test_went_down_by_needs_an_amount(win, monkeypatch):
    shown = []
    win.ctl.message.connect(shown.append)
    connect(win)
    search(win, "100")
    win.find.by_amount.setText("-4")
    win.find.down_by_btn.click()
    assert shown and "more than 0" in shown[-1]
    assert win.find.count.text() == "2 matches"


def test_fraction_scale_from_advanced(win, fake):  # AC-H3
    from tests.conftest import BAR_ADDR

    fake.poke(PID, BAR_ADDR, struct.pack("<f", 0.874))
    connect(win)
    win.find.advanced.setChecked(True)
    win.find.scale.setCurrentIndex(1)
    assert win.find.scale_max.isEnabled()
    search(win, "87")
    assert win.find.count.text() == "1 matches"
    assert win.find.rows[0].address == BAR_ADDR


def test_guide_suggests_and_tries_the_next_way(win, fake):  # AC-H5
    from tests.conftest import BAR_ADDR

    fake.poke(PID, BAR_ADDR, struct.pack("<f", 0.874))  # 87 of 100, stored as 0-1
    connect(win)
    f = win.find
    f.shows_btns[Shows.NUMBER].click()
    assert f.suggestion.action is Action.SEARCH
    assert "font-weight: bold" in f.search_btn.styleSheet()
    search(win, "87")
    assert f.count.text() == "0 matches"
    assert f.suggestion.action is Action.TRY_NEXT and f.suggestion.fallback == "fraction"
    assert f.try_btn.isVisible() or not f.isVisible()
    f.try_btn.click()
    assert f.count.text() == "1 matches" and f.rows[0].address == BAR_ADDR
    assert f.suggestion.action is Action.MAKE_CHEAT


def test_test_the_hits_finds_the_real_one(win, fake, monkeypatch):  # AC-H6
    from cheatmaker.ui.hit_test_dialog import HitTestDialog

    connect(win)
    search(win, "100")  # health and a decoy copy
    f = win.find
    assert f.test_btn.isEnabled()
    seen = {}

    def play(dlg):
        seen["warned"] = dlg.warning.isVisibleTo(dlg)
        assert dlg.value.text() == "111"
        while not dlg.tester.done:
            dlg.write_btn.click()
            half = dlg.tester.testing if dlg.tester.waiting else []
            assert "change to 111" in dlg.hint.text()
            (dlg.yes_btn if HEALTH_ADDR in half else dlg.no_btn).click()
        assert i32(fake, HEALTH_ADDR) == 100, "put back"
        return 0

    monkeypatch.setattr(HitTestDialog, "exec", play)
    f.test_btn.click()
    assert seen["warned"] and win.ctl.hit_test_warned
    assert f.selected() is not None and f.selected().address == HEALTH_ADDR


def test_test_the_hits_stop_puts_values_back(win, fake, monkeypatch):
    from cheatmaker.ui.hit_test_dialog import HitTestDialog

    connect(win)
    search(win, "100")

    def stop(dlg):
        dlg.write_btn.click()
        assert any(i32(fake, a) == 111 for a in dlg.tester.testing)
        dlg.close_btn.click()
        return 0

    monkeypatch.setattr(HitTestDialog, "exec", stop)
    win.find.test_btn.click()
    assert i32(fake, HEALTH_ADDR) == 100


def test_test_the_hits_asks_in_shown_units(win, fake, monkeypatch):  # review finding 3
    from cheatmaker.ui.hit_test_dialog import HitTestDialog
    from tests.conftest import BAR_ADDR, HEAP_BASE

    copy = HEAP_BASE + 0x300
    fake.poke(PID, BAR_ADDR, struct.pack("<f", 0.874))
    fake.poke(PID, copy, struct.pack("<f", 0.874))
    connect(win)
    f = win.find
    f.advanced.setChecked(True)
    f.scale.setCurrentIndex(1)  # fraction of 100
    search(win, "87")
    assert f.count.text() == "2 matches"

    def play(dlg):
        assert float(dlg.value.text()) == pytest.approx(43.7, abs=0.01)
        dlg.write_btn.click()
        assert "43.7" in dlg.hint.text()
        written = [a for a in (BAR_ADDR, copy) if a in dlg.tester.testing]
        stored = struct.unpack("<f", fake.peek(PID, written[0], 4))[0]
        assert stored == pytest.approx(0.437, abs=1e-4), "written in stored units"
        dlg.yes_btn.click() if BAR_ADDR in written else dlg.no_btn.click()
        return 0

    monkeypatch.setattr(HitTestDialog, "exec", play)
    f.test_btn.click()
    assert f.selected().address == BAR_ADDR


def test_start_over_after_try_it_is_a_plain_search_again(win, fake):  # review finding 6
    from tests.conftest import BAR_ADDR

    fake.poke(PID, BAR_ADDR, struct.pack("<f", 0.874))
    connect(win)
    f = win.find
    f.shows_btns[Shows.NUMBER].click()
    search(win, "87")
    f.try_btn.click()  # fraction of 100
    assert f.advanced.isChecked() and f.scale.currentData() == "fraction"
    f.new_btn.click()
    assert not f.advanced.isChecked() and f.scale.currentData() == "shown"


def test_scan_files_button_opens_this_searchs_folder(win, monkeypatch):
    from cheatmaker.ui import find_wizard

    opened = []
    monkeypatch.setattr(find_wizard.QDesktopServices, "openUrl", lambda url: opened.append(url))
    connect(win)
    f = win.find
    assert f.scan_files_folder() == win.ctl.scanner.scan_log.root()
    search(win, "100")
    folder = win.ctl.scanner.scan_log.folder
    assert folder is not None and (folder / "scan-001.txt").exists()
    assert "scan-001.txt" in f.count.toolTip()
    f.files_btn.click()
    assert [Path(u.toLocalFile()) for u in opened] == [folder]


def test_unknown_search_shows_both_types_and_lists_whole_numbers(win, fake):
    connect(win)
    win.find.unknown_btn.click()
    assert "decimal" in win.find.count.text() and "whole number" in win.find.count.text()
    fake.poke(PID, GOLD_ADDR, struct.pack("<i", 2000))
    win.find.number.setText("2000")
    win.find.search_btn.click()
    assert win.find.count.text() == "1 matches"
    assert win.find.row_types == [ValueType.INT32]
    win.find.test_btn.click()  # one match: nothing to test, no crash


def test_combine_two_cheats_into_one(win, fake):
    """Returnal needs two health values kept at once: one tick should do both."""
    connect(win)
    search(win, "100")
    win.find.table.selectRow(0)
    win.find.set_value.setText("500")
    win.find.template.setCurrentIndex(0)  # Infinite Health (keeps writing)
    win.find.make_btn.click()
    win.tabs.setCurrentWidget(win.find)
    win.find.new_btn.click()  # Start over: a new search, not a narrowing one
    search(win, "2450")
    win.find.table.selectRow(0)
    win.find.set_value.setText("7000")
    win.find.template.setCurrentIndex(0)
    win.find.make_btn.click()
    view = win.cheats
    t = view.table
    assert t.rowCount() == 2 and not view.combine_btn.isEnabled()
    t.item(0, COL_ON).setCheckState(Qt.CheckState.Checked)  # one is on already
    t.selectAll()
    assert view.combine_btn.isEnabled() and not view.lock_btn.isEnabled()
    view.combine_btn.click()
    [c] = win.ctl.cheat_file.cheats
    assert c.name == "Infinite Health + Infinite Health" and len(c.locations) == 2
    assert c.enabled and c.freeze and c.value is None
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 1))
    fake.poke(PID, GOLD_ADDR, struct.pack("<i", 1))
    win.ctl.freezer.tick()
    assert i32(fake, HEALTH_ADDR) == 500 and i32(fake, GOLD_ADDR) == 7000
    t.item(0, COL_ON).setCheckState(Qt.CheckState.Unchecked)
    assert i32(fake, HEALTH_ADDR) == 100 and i32(fake, GOLD_ADDR) == 2450
    t.selectRow(0)
    assert not view.lock_btn.isEnabled()  # Lock it watches one address
    view.delete_btn.click()
    assert t.rowCount() == 0


def test_combine_is_on_with_a_found_again_cheat(win, fake):  # spec-restart-combined AC-1
    from cheatmaker.core import project as pj
    from cheatmaker.core import refind as rf
    from cheatmaker.core.cheats import Cheat, Location, LocationKind
    from cheatmaker.ui.cheats_view import FOUND_AGAIN

    connect(win)
    obj = pj.ObjectSpec("obj1", "Health", None, [pj.Field("health", 0x40, ValueType.INT32)])
    win.ctl.live.trackers["obj1"] = rf.Tracker(win.ctl.session, obj, HEALTH_ADDR - 0x40)
    win.cheats.finder.ensure_project().put_object(obj)  # the game's notes are loaded
    raw = struct.pack("<i", 500)
    found = Cheat(
        "health",
        [Location(LocationKind.SESSION_ADDRESS, 0, raw, raw)],
        freeze=True,
        value_type=ValueType.INT32,
        anchor=("obj1", "health"),
    )
    gold = struct.pack("<i", 7000)
    fraction = Cheat(
        "fraction",
        [Location(LocationKind.SESSION_ADDRESS, GOLD_ADDR, gold, gold)],
        value_type=ValueType.INT32,
    )
    win.ctl.ensure_file().cheats.extend([found, fraction])
    win.ctl.cheats_changed.emit()
    view, t = win.cheats, win.cheats.table
    t.selectAll()
    assert view.combine_btn.isEnabled()
    view.combine_btn.click()
    [c] = win.ctl.cheat_file.cheats
    assert c.name == "health + fraction" and [p.part for p in c.locations] == [
        "health",
        "fraction",
    ]
    assert t.item(0, COL_KEEPS).text() == "This session only"
    from cheatmaker.ui.cheats_view import details

    assert details(c) == (
        "health: found again obj1/health: F4 01 00 00 -> F4 01 00 00; "
        f"fraction: session_address +{GOLD_ADDR:X}: 58 1B 00 00 -> 58 1B 00 00"
    )
    t.selectRow(0)
    assert view.learn_btn.isEnabled(), "Find it next time is on for it"
    assert not view.lock_btn.isEnabled() and not view.build_btn.isEnabled()
    c.locations = c.learned_parts  # once every part is learned
    win.ctl.cheats_changed.emit()
    assert t.item(0, COL_KEEPS).text() == FOUND_AGAIN
    t.selectRow(0)
    assert not view.learn_btn.isEnabled(), "nothing left to learn"


def test_combine_refuses_parts_that_overlap(win, fake):  # AC-1
    connect(win)
    search(win, "100")
    win.find.table.selectRow(0)
    win.find.template.setCurrentIndex(0)
    win.find.make_btn.click()
    win.find.make_btn.click()  # the same value twice
    view = win.cheats
    view.table.selectAll()
    view.combine_btn.click()
    assert len(win.ctl.cheat_file.cheats) == 2, "nothing combined"
    assert "overlap in memory" in win.status.text()


def test_save_counts_a_found_again_combined_cheat_as_app_only(win, fake):  # AC-6
    from cheatmaker.core.cheats import Cheat, Location, LocationKind, combine

    connect(win)
    raw = struct.pack("<i", 1)

    def found(name):
        loc = Location(LocationKind.SESSION_ADDRESS, 0, raw, raw)
        return Cheat(name, [loc], value_type=ValueType.INT32, anchor=("obj1", name))

    patch = Cheat("nop", [Location(LocationKind.CODE_PATCH, 0x100, b"\x90\x90", b"\x89\x08")])
    win.ctl.ensure_file().cheats.extend([combine([found("a"), found("b")]), patch])
    dlg = SaveDialog(win.ctl, win)
    assert hints.SAVE_APP_ONLY.format(n=1) in dlg.skipped.text()
    assert "This session only" not in dlg.skipped.text()
    assert [c.name for c in win.ctl.cheat_file.saveable_cheats()] == ["nop"]
