"""Pro step 15: the Scanner tab and the address list (AC-2.1 to AC-2.3)."""

from __future__ import annotations

import struct

from PySide6.QtCore import Qt

from cheatmaker.core.pro import scan as sc
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.ui.pro import hints
from tests import fakegame as fg

HP_ADDR = fg.PLAYER_AT[1] + fg.HP32  # whole-number health of the player


def pick(box, data):
    box.setCurrentIndex(box.findData(data))


def first(scanner, value, cmp=Compare.EXACT, vtype=ValueType.INT32, region=sc.GAME_DATA):
    scanner.region.setCurrentText(region)
    pick(scanner.vtype, vtype)
    pick(scanner.compare, cmp)
    scanner.value.setText(str(value))
    scanner.first_btn.click()


def test_first_and_next_scan_narrow_to_the_value(pro, game):
    """AC-2.2 through the tab: greater than."""
    s = pro.scanner
    hp = struct.unpack("<i", game.fake.peek(fg.PID, HP_ADDR, 4))[0]
    first(s, hp)
    n = s.scan.count
    assert n >= 1 and s.count.text() == hints.SCAN_COUNT.format(n=n)
    assert s.table.rowCount() == min(n, sc.PAGE_SIZE)
    game.fake.poke(fg.PID, HP_ADDR, struct.pack("<i", hp + 1000))
    pick(s.compare, Compare.GREATER)
    s.value.setText(str(hp + 500))
    s.next_btn.click()
    assert s.scan.count == 1 and s.hits[0].address == HP_ADDR
    assert s.table.item(0, 1).text() == str(hp + 1000)


def test_progress_shows_time_left(pro):
    """AC-2.1: progress with time left."""
    s = pro.scanner
    now = [100.0]
    s.clock = lambda: now[0]
    s._started = 100.0
    now[0] = 130.0
    s._show_progress(0.25)
    assert s.progress.value() == 25
    assert s.left.text() == hints.SCAN_PROGRESS.format(pct=25, left="about 1 min 30 s left")


def test_results_are_paged_a_thousand_at_a_time(pro):
    s = pro.scanner
    first(s, 0, vtype=ValueType.INT8)
    assert s.scan.count > sc.PAGE_SIZE
    assert s.table.rowCount() == sc.PAGE_SIZE and s.next_page_btn.isEnabled()
    first_page_last = s.hits[-1].address
    s.next_page_btn.click()
    assert s.page == 1 and s.hits[0].address > first_page_last
    s.prev_page_btn.click()
    assert s.page == 0


def test_cancelled_scan(win, pro, monkeypatch):
    s = pro.scanner
    from cheatmaker.errors import ScanCancelled

    def cancelled(*a, **kw):
        raise ScanCancelled("stop")

    monkeypatch.setattr(s.scan, "first", cancelled)
    first(s, 5)
    assert win.status.text() == hints.SCAN_CANCELLED and s.first_btn.isEnabled()


def test_bad_value_is_refused(win, pro):
    s = pro.scanner
    first(s, "lots")
    assert "isn't a" in win.status.text() and s.scan.count is None


def test_new_scan_and_compares(pro):
    s = pro.scanner
    assert s.compare.findData(Compare.CHANGED) == -1, "a first scan has nothing to compare"
    first(s, 1)
    assert s.compare.findData(Compare.CHANGED) >= 0 and s.compare.findData(Compare.UNKNOWN) == -1
    s.new_btn.click()
    assert s.scan.count is None and s.table.rowCount() == 0


def test_add_results_to_the_address_list(win, pro, game):
    s = pro.scanner
    hp = struct.unpack("<i", game.fake.peek(fg.PID, HP_ADDR, 4))[0]
    first(s, hp)
    row = next(k for k, h in enumerate(s.hits) if h.address == HP_ADDR)
    s.table.cellDoubleClicked.emit(row, 0)
    (added,) = pro.addresses.rows
    assert added.address == HP_ADDR and added.vtype is ValueType.INT32 and added.value == hp
    assert win.status.text() == hints.ADDED.format(n=1)


def test_aob_search_and_show_in_code(pro):
    s = pro.scanner
    s.aob.setText(fg.VMOVSS_W.hex(" "))
    s.aob_btn.click()
    assert s.aob_hits == [fg.WRITER] and s.count.text() == hints.AOB_FOUND.format(n=1)
    assert not s.add_btn.isEnabled()
    s.table.cellDoubleClicked.emit(0, 0)
    assert pro.tabs.currentWidget() is pro.code and pro.code.page.target == fg.WRITER


# -- address list -------------------------------------------------------------------------
def _row(pro, addr=HP_ADDR):
    lst = pro.addresses
    row = lst.add(addr, ValueType.INT32, "Health")
    lst.table.selectRow(lst.rows.index(row))
    return lst, row


def test_set_value_once(pro, game):
    lst, _ = _row(pro)
    lst.table.item(0, 3).setText("777")
    assert game.fake.peek(fg.PID, HP_ADDR, 4) == struct.pack("<i", 777)
    assert lst.table.item(0, 3).text() == "777"


def test_freeze_keeps_writing_until_off(win, pro, game):
    """AC-2.3: frozen in the shared Freezer (10 a second) under a pro: key."""
    lst, row = _row(pro)
    freezer = win.ctl.freezer
    lst.table.item(0, 4).setCheckState(Qt.CheckState.Checked)
    assert freezer.keys == {f"pro:{HP_ADDR:x}"} and freezer.interval <= 0.1
    assert row.frozen == row.value
    lst.table.item(0, 3).setText("555")
    assert freezer.fixed_writes("pro:")[row.key][0][2] == struct.pack("<i", 555)
    lst.table.item(0, 4).setCheckState(Qt.CheckState.Unchecked)
    assert freezer.keys == set()


def test_badges_heap_and_static(pro):
    """AC-2.3: heap PC only; a frozen static CheatRunner, written once."""
    lst, _ = _row(pro)
    assert lst.table.item(0, 7).text() == "PC only"
    static = lst.add(fg.DATA + 0x40, ValueType.INT32, "Static")
    k = lst.rows.index(static)
    lst.table.item(k, 4).setCheckState(Qt.CheckState.Checked)
    assert lst.table.item(k, 7).text() == "CheatRunner"
    assert "written once" in lst.table.item(k, 7).toolTip()


def test_description_group_comment_and_type(pro, game):
    lst, row = _row(pro)
    lst.table.item(0, 5).setText("Player")
    lst.table.item(0, 6).setText("drops on hit")
    assert (row.group, row.comment) == ("Player", "drops on hit")
    lst.table.cellWidget(0, 2).setCurrentIndex(list(ValueType).index(ValueType.INT16))
    assert row.vtype is ValueType.INT16


def test_show_in_memory(pro):
    lst, _ = _row(pro)
    lst.memory_btn.click()
    assert pro.tabs.currentWidget() is pro.memory
    assert pro.memory.selected_address() == HP_ADDR


def test_add_typed_address_and_remove(pro):
    lst = pro.addresses
    lst.ask_text = lambda *_a: f"{fg.HEAP:#x}"
    lst.add_btn.click()
    assert [r.address for r in lst.rows] == [fg.HEAP]
    lst.table.selectRow(0)
    lst.remove_btn.click()
    assert lst.rows == []


def test_remove_and_close_drop_freezes(win, pro):
    lst, _ = _row(pro)
    lst.table.item(0, 4).setCheckState(Qt.CheckState.Checked)
    pro.close()
    assert win.ctl.freezer.keys == set()


def test_watch_actions_wait_for_the_watch_tab(pro):
    lst, _ = _row(pro)
    assert not lst.writes_btn.isEnabled() and not lst.accesses_btn.isEnabled()


def test_pointer_scan_opens_with_the_address(pro, monkeypatch):
    lst, _ = _row(pro)
    seen = []
    lst.exec_dialog = lambda dlg: seen.append((dlg.target, dlg.vtype))
    lst.pointer_btn.click()
    assert seen == [(HP_ADDR, ValueType.INT32)]
