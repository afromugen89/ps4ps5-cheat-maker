"""Pro step 14: the Memory view (read-only, AC-7.1)."""

from __future__ import annotations

from cheatmaker.core.pro import memory_page as mp
from tests import fakegame as fg


def go(mem, text):
    mem.place.setText(text)
    mem.go_btn.click()


def test_page_shows_bytes_and_selects_the_address(pro):
    mem = pro.memory
    go(mem, f"eboot.bin+{fg.WRITER - fg.BASE:#x}")
    assert mem.selected_address() == fg.WRITER
    row, col = divmod(fg.WRITER - fg.BASE, mp.ROW)
    assert mem.table.item(row, col).text() == "C4"
    assert mem.values["INT8"].text() == str(0xC4 - 0x100)
    assert mem.values["UINT8"].text() == str(0xC4)


def test_unmapped_bytes_show_question_marks(win, pro):
    """AC-7.1."""
    mem = pro.memory
    go(mem, f"{fg.HEAP + fg.HEAP_SIZE:#x}")
    assert mem.table.item(0, 0).text() == "??"
    assert mem.values["INT32"].text() == "??"
    assert win.ctl.session.connected


def test_next_and_previous_pages(pro):
    mem = pro.memory
    go(mem, f"{fg.HEAP:#x}")
    mem.next_btn.click()
    assert mem.page.start == fg.HEAP + mp.PAGE
    mem.prev_btn.click()
    mem.prev_btn.click()
    assert mem.page.start == fg.HEAP - mp.PAGE


def test_selecting_a_byte_inspects_it(pro, game):
    game.fake.poke(fg.PID, fg.HEAP + 0x10, (1234).to_bytes(4, "little"))
    mem = pro.memory
    go(mem, f"{fg.HEAP:#x}")
    mem.table.setCurrentCell(1, 0)
    assert mem.selected_address() == fg.HEAP + 0x10
    assert mem.values["INT32"].text() == "1234"
