"""Pro step 13: the Code view (AC-6.1, AC-6.2)."""

from __future__ import annotations

import time

from PySide6.QtGui import QGuiApplication

from cheatmaker.ui.pro import hints
from tests import fakegame as fg
from tests.ui.pro.test_cheats_tab import answers, new_cheat

WRITER = f"eboot.bin+{fg.WRITER - fg.BASE:#x}"


def go(code, text):
    code.place.setText(text)
    code.go_btn.click()


def test_address_loads_and_its_instruction_is_selected(pro):
    """AC-6.1."""
    code = pro.code
    t0 = time.monotonic()
    go(code, WRITER)
    assert time.monotonic() - t0 < 1.0
    line = code.selected_line()
    assert line.ip == fg.WRITER and line.text.startswith("vmovss")
    assert code.table.item(code.table.currentRow(), 0).text() == WRITER
    assert code.place.text() == WRITER


def test_active_cheat_site_shows_its_name_and_original(pro):
    """AC-6.2."""
    tab = pro.cheats
    tab.sleep = lambda _s: None
    cheat = new_cheat(tab, "Infinite health")
    tab.on_box.click()
    assert cheat.enabled
    code = pro.code
    go(code, WRITER)
    line = code.selected_line()
    assert line.text.startswith("jmp") and line.mark == "Infinite health (on)"
    code.original.setChecked(True)
    assert code.selected_line().text.startswith("vmovss")


def test_follow_back_and_forward(pro):
    tab = pro.cheats
    cheat = new_cheat(tab)
    tab.sleep = lambda _s: None
    tab.on_box.click()
    code = pro.code
    go(code, WRITER)
    code.follow(code.table.currentRow())
    cave = fg.BASE + cheat.pro.entries[0].cave
    assert code.page.target == cave and code.selected_line().ip == cave
    code.back_btn.click()
    assert code.page.target == fg.WRITER
    code.forward_btn.click()
    assert code.page.target == cave


def test_copy_aob(win, pro):
    code = pro.code
    go(code, WRITER)
    text = code.copy_aob()
    assert text.startswith("C4 C1 7A 11 8C 24 AC 02 00 00")
    assert QGuiApplication.clipboard().text() == text
    assert win.pro.status.text() == hints.AOB_COPIED.format(aob=text)


def test_make_nop_entry_here_makes_a_cheat(win, pro):
    code = pro.code
    go(code, f"eboot.bin+{fg.SUB_SITE - fg.BASE:#x}")
    answers(pro.cheats, "No damage")
    code.nop_btn.click()
    (cheat,) = win.ctl.cheat_file.cheats
    assert cheat.pro.entries[0].kind == "nop" and cheat.name == "No damage"
    assert pro.tabs.currentWidget() is pro.cheats


def test_make_cave_entry_adds_to_the_picked_cheat(win, pro):
    cheat = new_cheat(pro.cheats)
    code = pro.code
    go(code, f"eboot.bin+{fg.SUB_SITE - fg.BASE:#x}")
    code.cave_btn.click()
    assert [e.kind for e in cheat.pro.entries] == ["cave", "cave"]


def test_unmapped_address_is_said_and_keeps_the_link(win, pro):
    go(pro.code, "0x30000000")
    assert win.ctl.session.connected
    assert "Couldn't read" in win.pro.status.text()


def test_bad_text(win, pro):
    go(pro.code, "nowhere")
    assert win.pro.status.text().startswith("Type an address")
