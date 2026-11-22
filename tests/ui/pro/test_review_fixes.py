"""PR #39 review: Pro status line, reopen, drafts, search ownership, My cheats toggle,
per-row freezes, pause while the game is gone, close confirmation, no save on failure."""

from __future__ import annotations

import struct

import pytest
from PySide6.QtCore import Qt

from cheatmaker.core.pro.asm import AssemblerNotFound
from cheatmaker.core.pro.compile import SiteChanged
from cheatmaker.core.scanner import LOST_PRO, REPLACED
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.errors import ReadFailed, WriteFailed
from cheatmaker.ui import hints as guided_hints
from cheatmaker.ui.pro import hints
from tests import fakegame as fg
from tests.ui.pro.test_cheats_tab import new_cheat

HP_ADDR = fg.PLAYER_AT[1] + fg.HP32


# -- M1: Pro's own status line ----------------------------------------------------------
def test_pro_messages_show_in_the_pro_window(win, tab):
    win.status.setText("main")
    cheat = new_cheat(tab)
    tab.editor.source.setPlainText("bogus\n")
    tab.editor.build_btn.click()
    assert win.pro.status.text().startswith("Line 1") and cheat.pro is not None
    assert win.status.text() == "main", "the main window's status line is left alone"


def test_site_changed_details_in_a_dialog(win, tab, game, warnings):
    new_cheat(tab)
    game.fake.poke(fg.PID, fg.WRITER, b"\x90" * 5)
    tab.on_box.click()
    ((title, text),) = warnings
    assert title == hints.SITE_CHANGED_TITLE
    assert f"eboot.bin+{fg.WRITER - fg.BASE:#x}" in text
    assert "expected: C4 C1 7A 11 8C" in text and "found:    90 90 90 90 90" in text


def test_pro_wording_for_write_and_assembler_errors(win, pro):
    pro.msgs.fail(WriteFailed("0x400100 didn't take the new bytes"), "t")
    assert pro.status.text() == hints.WRITE_FAILED.format(
        reason="0x400100 didn't take the new bytes"
    )
    pro.msgs.fail(AssemblerNotFound("Assembler not found"), "t")
    assert pro.status.text() == hints.ASSEMBLER_MISSING
    pro.msgs.fail(SiteChanged([(0x400100, b"\x01", b"\x02")]), "t")
    assert "Nothing was changed" in pro.status.text()


# -- M2: close and reopen ------------------------------------------------------------------
def test_reopen_restarts_the_poll_and_polls_at_once(win, pro, game):
    pro.close()
    assert not pro.timer.isActive()
    game.fake.remove_process(fg.PID)
    win.open_pro()
    assert win.pro is pro and pro.timer.isActive()
    assert all(not t.banner.isHidden() for t in pro.pro_tabs)
    assert hints.GAME_NOT_RUNNING in pro.header.text()


# -- M3: drafts ------------------------------------------------------------------------------
def test_build_takes_the_draft_and_a_failed_build_keeps_it(win, tab):
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    saves = []
    tab.save = lambda: saves.append(True)
    tab.editor.source.setPlainText("bogus\n")
    tab.editor.build_btn.click()
    assert entry.source == "original\njmp return\n" and saves == [], "nothing saved"
    assert tab.editor.draft_of(entry) == "bogus\n"
    tab.editor.source.setPlainText("nop\noriginal\njmp return\n")
    tab.editor.build_btn.click()
    assert entry.source == "nop\noriginal\njmp return\n" and not tab.editor.has_draft(entry)
    assert entry.cave_bytes[0] == 0x90 and saves == [True]


def test_variable_edit_never_uses_an_unbuilt_draft(win, tab, game):
    cheat = new_cheat(tab)
    tab.add_var_btn.click()
    tab.editor.build_btn.click()
    entry = cheat.pro.entries[0]
    before = entry.cave_bytes
    tab.editor.source.setPlainText("nop\nnop\noriginal\njmp return\n")  # a draft
    calls = len(tab.assembler.calls)
    tab.entries.setCurrentRow(0)
    tab.vars.item(0, 2).setText("9")
    assert entry.source == "original\njmp return\n"
    ((_n, _t, at),) = entry.layout
    assert entry.cave_bytes[:at] == before[:at], "only the variable's bytes changed"
    assert entry.cave_bytes[at : at + 4] == struct.pack("<i", 9)
    assert all("nop\nnop" not in src for src, *_ in tab.assembler.calls[calls:])


def test_editor_is_read_only_while_on(tab):
    new_cheat(tab)
    tab.on_box.click()
    assert tab.editor.source.isReadOnly()
    tab.on_box.click()
    assert not tab.editor.source.isReadOnly()


# -- M4: one search at a time -------------------------------------------------------------------
def test_pro_first_scan_waits_for_the_main_window(win, pro):
    s = pro.scanner
    win.ctl.tasks._running = 1  # a guided search runs
    s.value.setText("5")
    s.first_btn.click()
    win.ctl.tasks._running = 0
    assert pro.status.text() == hints.GUIDED_BUSY and s.scan.count is None


def test_find_says_the_pro_scanner_took_its_search(win, pro):
    win.ctl.scanner.first_scan(100)
    assert win.ctl.scanner.active
    s = pro.scanner
    s.value.setText("5")
    s.first_btn.click()
    assert win.ctl.scanner.lost_why == LOST_PRO
    assert win.find.hint.text() == guided_hints.SEARCH_LOST_PRO


def test_guided_next_scan_checks_it_still_owns_the_search(win):
    sc = win.ctl.scanner
    sc.first_scan(100)
    win.ctl.session.search_owner = object()  # someone else's search, unannounced
    with pytest.raises(ValueError, match=REPLACED[:20]):
        sc.next_scan(Compare.CHANGED)
    assert not sc.active


# -- M5: My cheats toggles a Pro cheat through Pro's rules -------------------------
def test_my_cheats_toggle_refuses_an_overlap(win, tab, game):
    first = new_cheat(tab, "First")
    tab.on_box.click()
    second = new_cheat(tab, "Second")
    writes = len(game.fake.writes)
    row = win.ctl.cheat_file.cheats.index(second)
    win.cheats.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
    assert first.enabled and not second.enabled
    assert "First" in win.status.text() and len(game.fake.writes) == writes


def test_my_cheats_toggle_uses_pro_on_and_off(win, tab, game):
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    row = win.ctl.cheat_file.cheats.index(cheat)
    win.cheats.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
    assert cheat.enabled and game.fake.peek(fg.PID, fg.WRITER, 1) == b"\xe9"
    win.cheats.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
    assert not cheat.enabled
    assert game.fake.peek(fg.PID, fg.WRITER, len(entry.off)) == entry.off
    assert game.fake.peek(fg.PID, fg.BASE + entry.cave, 1) == b"\x00"


def test_my_cheats_site_changed_writes_nothing(win, tab, game):
    cheat = new_cheat(tab)
    game.fake.poke(fg.PID, fg.WRITER, b"\x90" * 5)
    writes = len(game.fake.writes)
    row = win.ctl.cheat_file.cheats.index(cheat)
    win.cheats.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
    assert not cheat.enabled and len(game.fake.writes) == writes
    assert "Nothing was changed" in win.status.text()


# -- minors ---------------------------------------------------------------------------------------
def test_two_rows_on_one_address_freeze_apart(win, pro):
    lst = pro.addresses
    a = lst.add(HP_ADDR, ValueType.INT32, "a")
    b = lst.add(HP_ADDR, ValueType.INT32, "b")
    assert a is b, "one row per address in the list"
    from cheatmaker.core.pro.addresses import AddressRow, freeze, unfreeze

    c = AddressRow(HP_ADDR, ValueType.INT16)
    freeze(win.ctl.session, win.ctl.freezer, a, 5)
    freeze(win.ctl.session, win.ctl.freezer, c, 6)
    assert len(win.ctl.freezer.keys) == 2
    unfreeze(win.ctl.freezer, c)
    assert win.ctl.freezer.keys == {a.key}


def test_freezes_pause_while_the_game_is_gone(win, pro, game):
    lst = pro.addresses
    row = lst.add(HP_ADDR, ValueType.INT32)
    lst.table.item(0, 4).setCheckState(Qt.CheckState.Checked)
    assert win.ctl.freezer.keys == {row.key}
    game.fake.foreground_pid = None
    real = game.fake.maps

    def gone(pid):
        raise ReadFailed("gone")

    game.fake.maps = gone
    pro.poll()
    assert win.ctl.freezer.keys == set() and row.frozen is not None
    game.fake.maps = real
    pro.poll()
    assert win.ctl.freezer.keys == {row.key}


def test_close_asks_when_rows_are_frozen(win, pro, questions):
    lst = pro.addresses
    lst.add(HP_ADDR, ValueType.INT32)
    lst.table.item(0, 4).setCheckState(Qt.CheckState.Checked)
    pro.confirm_close = lambda n: False
    pro.close()
    assert pro.isVisible() and win.ctl.freezer.keys
    del pro.confirm_close
    pro.close()
    assert questions == [hints.CLOSE_FROZEN_TITLE] and not win.ctl.freezer.keys
