"""Pro step 12: Cheats tab, cave editor, on/off, variables, export, import, refusals."""

from __future__ import annotations

import json

import pytest

from cheatmaker import formats
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.core.pro import compile as pc_compile
from cheatmaker.core.pro.asm import AssemblerNotFound
from cheatmaker.core.pro.badge import HEAP_ADDRESS
from cheatmaker.core.pro.model import JMP_REL32_SIZE
from cheatmaker.core.values import ValueType
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.cave_editor import DEBOUNCE_MS
from cheatmaker.ui.pro.pro_window import ProWindow
from tests import fakegame as fg

HOOK = f"eboot.bin+{fg.WRITER - fg.BASE:#x}"


def answers(tab, *texts):
    queue = list(texts)
    tab.ask_text = lambda *_a: queue.pop(0)


def new_cheat(tab, name="Infinite health", hook=HOOK) -> Cheat:
    answers(tab, name, hook)
    tab.new_btn.click()
    cheat = tab.selected()
    assert cheat is not None and cheat.pro is not None, tab.ctl.cheat_file
    return cheat


def game_bytes(game, addr, n):
    return game.fake.peek(fg.PID, addr, n)


def test_new_cheat_is_built_listed_and_kept_in_the_notes(win, tab):
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    assert entry.kind == "cave" and entry.cave is not None and entry.cave_bytes
    assert cheat in win.ctl.cheat_file.cheats
    assert [c.id for c in win.ctl.project.pro_cheats] == [cheat.pro.id]
    assert tab.entries.count() == 1 and "Cave hooked" in tab.entries.item(0).text()
    assert tab.editor.source.toPlainText() == pc_compile.DEFAULT_SOURCE
    assert "Badge: CheatRunner" in tab.badge.text()
    assert "room for" in tab.editor.budget.text()


def test_typing_assembles_after_a_pause(qtbot, tab):
    """AC-8.1: bytes or the first error with its line, soon after typing stops."""
    assert DEBOUNCE_MS < 500
    cheat = new_cheat(tab)
    tab.editor.source.setPlainText("nop\noriginal\njmp return\n")
    assert cheat.pro.entries[0].source.startswith("nop")
    qtbot.waitUntil(lambda: tab.editor.bytes_view.toPlainText().startswith("90 "), timeout=2000)
    assert tab.editor.error.text() == ""
    tab.editor.source.setPlainText("nop\nbogus here\n")
    qtbot.waitUntil(lambda: tab.editor.error.text().startswith("Line 2:"), timeout=2000)


def test_results_for_older_text_are_dropped(tab):
    new_cheat(tab)
    ed = tab.editor
    ed.source.setPlainText("nop\noriginal\njmp return\n")
    stale = ed._gen
    ed.source.setPlainText("bogus\n")
    from cheatmaker.core.pro.asm import AsmResult

    ed._assembled(stale, AsmResult(b"\x90", {}))
    assert ed.bytes_view.toPlainText() != "90"


def test_too_big_a_cave_says_so(tab):
    new_cheat(tab)
    tab.editor.room = 4
    tab.editor.size = 9
    tab.editor._show_budget()
    assert tab.editor.error.text() == hints.CAVE_TOO_BIG.format(size=9, room=4)


def test_build_keeps_the_new_bytes(win, tab):
    cheat = new_cheat(tab)
    tab.editor.source.setPlainText("nop\noriginal\njmp return\n")
    tab.editor.build_btn.click()
    entry = cheat.pro.entries[0]
    assert entry.cave_bytes[0] == 0x90
    assert cheat.locations[0].on == entry.cave_bytes, "compiled again"
    assert "Built" in win.status.text()


def test_build_error_changes_nothing(win, tab):
    cheat = new_cheat(tab)
    before = cheat.pro.entries[0].cave_bytes
    tab.editor.source.setPlainText("bogus\n")
    tab.editor.build_btn.click()
    assert cheat.pro.entries[0].cave_bytes == before
    assert "Line 1" in win.status.text()


def test_build_passes_my_cheats_code_lines(tab, monkeypatch):
    cheat = new_cheat(tab)
    guided = Cheat("Guided", [Location(LocationKind.CODE_PATCH, 0x3F00, b"\x90", b"\x00")])
    tab.ctl.cheat_file.cheats.append(guided)
    seen = {}
    real = pc_compile.build

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(pc_compile, "build", spy)
    tab.select_cheat(cheat)
    tab.editor.build_btn.click()
    assert (0x3F00, 0x3F01) in seen["open_rooms"]


def test_on_and_off_follow_the_safety_rules(win, tab, game):
    """AC-8.4 / AC-8.5 through compile.enable and disable; My cheats follows."""
    cheat = new_cheat(tab)
    entry = cheat.pro.entries[0]
    tab.on_box.click()
    assert cheat.enabled and id(cheat) in win.ctl.live.active
    assert game_bytes(game, fg.WRITER, 1) == b"\xe9"
    assert game_bytes(game, fg.BASE + entry.cave, len(entry.cave_bytes)) == entry.cave_bytes
    assert win.cheats.table.item(win.ctl.cheat_file.cheats.index(cheat), 0).checkState().value
    tab.on_box.click()
    assert not cheat.enabled
    assert game_bytes(game, fg.WRITER, len(entry.off)) == entry.off
    assert game_bytes(game, fg.BASE + entry.cave, len(entry.cave_bytes)) == bytes(
        len(entry.cave_bytes)
    )


def test_changed_site_writes_nothing(win, tab, game):
    cheat = new_cheat(tab)
    game.fake.poke(fg.PID, fg.WRITER, b"\x90" * JMP_REL32_SIZE)
    writes = len(game.fake.writes)
    tab.on_box.click()
    assert not cheat.enabled and len(game.fake.writes) == writes
    assert "Nothing was changed" in win.status.text()


def test_overlap_names_the_other_cheat(win, tab):
    first = new_cheat(tab, "First")
    tab.on_box.click()
    assert first.enabled
    second = new_cheat(tab, "Second")
    tab.on_box.click()
    assert not second.enabled and "First" in win.status.text()


def test_build_and_entry_changes_wait_until_off(win, tab):
    cheat = new_cheat(tab)
    tab.on_box.click()
    tab.editor.build_btn.click()
    assert win.status.text() == hints.TURN_OFF_FIRST
    assert not tab.add_cave_btn.isEnabled() and not tab.remove_entry_btn.isEnabled()
    assert cheat.enabled


def test_add_and_remove_entries(win, tab):
    cheat = new_cheat(tab)
    answers(tab, f"eboot.bin+{fg.SUB_SITE - fg.BASE:#x}")
    tab.add_nop_btn.click()
    assert [e.kind for e in cheat.pro.entries] == ["cave", "nop"]
    assert len(cheat.locations) == 3
    tab.entries.setCurrentRow(1)
    tab.remove_entry_btn.click()
    assert [e.kind for e in cheat.pro.entries] == ["cave"]
    tab.entries.setCurrentRow(0)
    tab.remove_entry_btn.click()
    assert win.status.text() == hints.LAST_ENTRY


def test_bad_place_is_refused(win, tab):
    answers(tab, "x", "here")
    tab.new_btn.click()
    assert len(win.ctl.cheat_file.cheats) == 0
    assert "eboot.bin+0x1234" in win.status.text()


def test_variable_value_goes_live_when_on(win, tab, game):
    """AC-8.7 through the variables table."""
    cheat = new_cheat(tab)
    tab.add_var_btn.click()
    assert [v.name for v in cheat.pro.variables] == ["value1"]
    tab.editor.build_btn.click()
    tab.on_box.click()
    assert cheat.enabled
    tab.vars.item(0, 2).setText("1234")
    entry = cheat.pro.entries[0]
    ((_name, _t, at),) = entry.layout
    assert game_bytes(game, fg.BASE + entry.cave + at, 4) == (1234).to_bytes(4, "little")
    assert win.status.text() == hints.VAR_WRITTEN.format(name="value1", value="1234")


def test_variable_waits_for_build_when_the_layout_changed(win, tab):
    cheat = new_cheat(tab)
    tab.add_var_btn.click()
    tab.vars.item(0, 2).setText("7")
    assert cheat.pro.variables[0].value == 7
    assert win.status.text() == hints.VAR_NEEDS_BUILD.format(name="value1")


def test_variable_type_and_in_file(tab):
    cheat = new_cheat(tab)
    tab.add_var_btn.click()
    tab.vars.cellWidget(0, 1).setCurrentIndex(list(ValueType).index(ValueType.FLOAT))
    assert cheat.pro.variables[0].vtype is ValueType.FLOAT
    tab.vars.item(0, 4).setCheckState(tab.vars.item(0, 4).checkState().Checked)
    assert cheat.pro.editable == "value1"
    tab.editor.build_btn.click()
    assert cheat.value_type is ValueType.FLOAT, "My cheats can edit it"


def test_bad_variable_name_is_refused(win, tab):
    cheat = new_cheat(tab)
    tab.add_var_btn.click()
    tab.vars.item(0, 0).setText("rax")
    assert cheat.pro.variables[0].name == "value1"
    assert "register" in win.status.text()


def _guided(win) -> Cheat:
    cf = win.ctl.ensure_file()
    sub = fg.SUB_SITE - fg.BASE
    c = Cheat("No damage", [Location(LocationKind.CODE_PATCH, sub, b"\x90" * 6, fg.SUB_W)])
    cf.cheats.append(c)
    win.ctl.cheats_changed.emit()
    return c


def test_guided_cheat_is_previewed_then_adopted(win, tab):
    c = _guided(win)
    tab.select_cheat(c)
    assert tab.cheat_list.currentItem().text() == hints.GUIDED.format(name="No damage")
    assert tab.entries.count() == 1 and not tab.on_box.isEnabled()
    assert tab.adopt_btn.isEnabled()
    tab.adopt_btn.click()
    assert c.pro is not None and c.pro.origin == "guided"
    assert c.pro.entries[0].kind == "nop"
    assert [p.id for p in win.ctl.project.pro_cheats] == [c.pro.id]


def test_guided_wizards_refuse_a_cheat_edited_in_pro(win, tab, monkeypatch):
    c = _guided(win)
    tab.select_cheat(c)
    tab.adopt_btn.click()
    opened = []
    monkeypatch.setattr(win, "make_build_dialog", lambda **kw: opened.append(kw))
    win.open_lock(c)
    assert win.status.text() == hints.EDITED_IN_PRO.format(name="No damage")
    win.status.setText("")
    win.cheats.do_build(c)
    assert win.status.text() == hints.EDITED_IN_PRO.format(name="No damage")
    assert opened == []


def test_assembler_not_found(win, qtbot):
    def missing():
        raise AssemblerNotFound("Assembler not found")

    pro = ProWindow(win.ctl, assembler_factory=missing)
    qtbot.addWidget(pro)
    t = pro.cheats
    assert t.assembler is None and not t.asm_missing.isHidden()
    assert not t.new_btn.isEnabled()
    pro.close()


def _export_project(win) -> None:
    cf = win.ctl.ensure_file()
    for k in range(3):
        cf.cheats.append(
            Cheat(
                f"Fix {k}", [Location(LocationKind.MODULE_VALUE, 0x4000 + k * 4, b"\x01", b"\x00")]
            )
        )
    cf.cheats.append(
        Cheat("Heap gold", [Location(LocationKind.SESSION_ADDRESS, fg.HEAP, b"\x01", b"\x00")])
    )
    win.ctl.cheats_changed.emit()


def test_export_holds_runner_cheats_and_lists_the_pc_only_one(win, tab, tmp_path):
    """AC-12.1."""
    _export_project(win)
    shown = []

    def run(dlg):
        shown.append(dlg)
        dlg.folder.setText(str(tmp_path))
        dlg.do_export()

    tab.exec_dialog = run
    tab.export_btn.click()
    (dlg,) = shown
    assert dlg.count.text() == hints.EXPORT_COUNT.format(n=3)
    assert dlg.left_out.count() == 1
    assert dlg.left_out.item(0).text() == f"Heap gold: {HEAP_ADDRESS}"
    assert not dlg.make_cave_btn.isEnabled()
    data = json.loads(dlg.saved.read_text(encoding="utf-8"))
    assert len(data["mods"]) == 3


@pytest.mark.parametrize("fmt", ["shn", "mc4"])
def test_export_other_formats(win, tab, tmp_path, fmt):
    _export_project(win)
    from cheatmaker.ui.pro.export_dialog import ExportDialog

    dlg = ExportDialog(win.ctl)
    dlg.fmt.setCurrentIndex(dlg.fmt.findData(fmt))
    dlg.folder.setText(str(tmp_path))
    path = dlg.do_export()
    assert path.suffix == f".{fmt}"
    assert len(formats.load_path(path).cheats) == 3


def _cave_file(tmp_path):
    cave_at, hook = 0x3F00, fg.WRITER - fg.BASE
    cave = b"\x90" + b"\xe9" + (hook + 10 - (cave_at + 6)).to_bytes(4, "little", signed=True)
    jump = b"\xe9" + (cave_at - (hook + 5)).to_bytes(4, "little", signed=True) + b"\x90" * 5
    cf = CheatFile("Game", fg.TITLE, fg.VERSION)
    cf.cheats.append(
        Cheat(
            "Imported",
            [
                Location(LocationKind.CODE_PATCH, cave_at, cave, bytes(len(cave))),
                Location(LocationKind.CODE_PATCH, hook, jump, fg.VMOVSS_W),
            ],
        )
    )
    path = tmp_path / f"{fg.TITLE}_{fg.VERSION}.json"
    path.write_bytes(formats.dumps(cf, "json"))
    return path


def test_import_and_convert_to_assembly(win, tab, tmp_path):
    path = _cave_file(tmp_path)
    tab.pick_file = lambda: str(path)
    seen = []

    def run(dlg):
        seen.append(dlg.preview.toPlainText())
        dlg.do_convert()
        seen.append(dlg.message.text())
        dlg.do_add()

    tab.exec_dialog = run
    tab.import_btn.click()
    assert "(a cave)" in seen[0] and "nop" in seen[0]
    assert seen[1] == hints.CONVERTED.format(name="Imported", n=1)
    (cheat,) = [c for c in win.ctl.cheat_file.cheats if c.pro is not None]
    assert [e.kind for e in cheat.pro.entries] == ["cave"]
    assert cheat.pro.origin == "import"
    assert win.status.text() == hints.IMPORTED.format(n=1)


def test_import_cancelled_adds_nothing(win, tab, tmp_path):
    path = _cave_file(tmp_path)
    tab.pick_file = lambda: str(path)
    tab.exec_dialog = lambda dlg: dlg.reject()
    tab.import_btn.click()
    assert not win.ctl.cheat_file or not win.ctl.cheat_file.cheats


def test_import_lists_refused_cheats(win, tab, tmp_path):
    cf = CheatFile("Game", fg.TITLE, fg.VERSION)
    loc = Location(LocationKind.CODE_PATCH, 0x10, b"\x90", b"\x00", section=1)
    cf.cheats.append(Cheat("Other section", [loc]))
    path = tmp_path / "x.shn"
    path.write_bytes(formats.dumps(cf, "shn"))
    tab.pick_file = lambda: str(path)
    seen = []
    tab.exec_dialog = lambda dlg: seen.append((dlg.refused.text(), dlg.message.text()))
    tab.import_btn.click()
    assert "Other section: a line is in section 1" in seen[0][0]
    assert seen[0][1] == hints.IMPORT_NOTHING


@pytest.mark.needs_nasm
def test_real_assembler_from_the_settings(win, qtbot):
    """The default factory: NASM found from settings / bundled / PATH."""
    pro = ProWindow(win.ctl)
    qtbot.addWidget(pro)
    t = pro.cheats
    assert t.assembler is not None and t.asm_missing.isHidden()
    cheat = new_cheat(t)
    t.editor.source.setPlainText("mov eax, 1\noriginal\njmp return\n")
    qtbot.waitUntil(lambda: t.editor.bytes_view.toPlainText().startswith("B8 01"), timeout=3000)
    assert cheat.pro.entries[0].source.startswith("mov")
    pro.close()
