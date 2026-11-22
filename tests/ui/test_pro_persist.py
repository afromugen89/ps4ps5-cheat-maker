"""Pro step 8: Cheat.pro is saved in the .cmproj (format 5) and comes back in My cheats."""

from __future__ import annotations

import dataclasses

import pytest

from cheatmaker.core import project as pj
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.pro import compile as pro_compile
from cheatmaker.core.pro.model import ProCheat, ProEntry
from cheatmaker.settings import Settings
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests import fakegame as fg

CAVE = 0x3F00


@pytest.fixture
def win(qtbot):
    game = fg.build(1, connect=False)
    ctl = Controller(lambda ip, port: game.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()
    yield w
    ctl.shutdown()


def _pro() -> ProCheat:
    patch = ProEntry("bytes", fg.SUB_SITE - fg.BASE, fg.SUB_W, b"\x90" * len(fg.SUB_W))
    cave = ProEntry(
        "cave",
        fg.WRITER - fg.BASE,
        fg.VMOVSS_W,
        b"\xe9" + bytes(4) + b"\x90" * 5,
        source="ret\n",
        cave=CAVE,
        cave_bytes=b"\xc3",
    )
    return ProCheat("p1", "No damage", [patch, cave])


def test_pro_cheat_is_saved_and_restored(win):
    finder = win.cheats.finder
    cf = win.ctl.ensure_file()
    cf.cheats.append(pro_compile.compile_cheat(_pro()))
    finder.save()
    p = win.ctl.project
    path = pj.project_path(p.title_id, p.version)
    assert [c.id for c in pj.load(path).pro_cheats] == ["p1"]
    assert (CAVE, CAVE + 1) in win.ctl.session.reserved_rooms, "guided caves keep out"

    cf.cheats.clear()
    game = win.ctl.session.require_game()
    finder.on_game(dataclasses.replace(game, pid=game.pid + 1))  # the game restarted
    (again,) = [c for c in cf.cheats if c.pro is not None]
    assert again.name == "No damage" and again.pro.entries[1].cave == CAVE


def _saved_ids(win) -> list[str]:
    p = win.ctl.project
    return [c.id for c in pj.load(pj.project_path(p.title_id, p.version)).pro_cheats]


def _with_pro(win):
    cf = win.ctl.ensure_file()
    cheat = pro_compile.compile_cheat(_pro())
    cf.cheats.append(cheat)
    win.cheats.finder.save()
    win.ctl.cheats_changed.emit()
    return cheat


def test_deleting_a_pro_cheat_forgets_its_source(win):
    _with_pro(win)
    assert _saved_ids(win) == ["p1"]
    win.cheats.table.selectRow(len(win.cheats.cheats) - 1)
    win.cheats.do_delete()
    assert win.ctl.project.pro_cheats == [] and _saved_ids(win) == []


def test_combining_a_pro_cheat_forgets_its_source(win):
    _with_pro(win)
    other = Cheat("Ammo", [Location(LocationKind.CODE_PATCH, 0x500, b"\x90", b"\x01")])
    win.ctl.ensure_file().cheats.append(other)
    win.ctl.cheats_changed.emit()
    win.cheats.table.selectAll()
    win.cheats.combine_btn.click()
    assert [c.name for c in win.cheats.cheats] == ["No damage + Ammo"]
    assert _saved_ids(win) == []


def test_renaming_a_pro_cheat_renames_its_source(win):
    from cheatmaker.ui.cheats_view import COL_NAME

    cheat = _with_pro(win)
    row = win.cheats.cheats.index(cheat)
    win.cheats.table.item(row, COL_NAME).setText("Never hurt")
    assert cheat.pro is not None and cheat.pro.name == "Never hurt"
    p = win.ctl.project
    assert pj.load(pj.project_path(p.title_id, p.version)).pro_cheats[0].name == "Never hurt"


def test_merged_bring_over_drops_the_old_rows_and_saves_only_moved_offsets(win):
    """QA BUG-07, merge case: the notes already hold a Pro cheat with the same id, so
    the brought-over one is re-id'd; the old version's row must still go."""
    from cheatmaker.core import carry as cr
    from cheatmaker.core.pro import refpoints as rp
    from cheatmaker.core.pro.asm import FakeAssembler
    from tests.unit.test_pro_refpoints import SHIFT, _image
    from tests.unit.test_pro_refpoints import _cheat as carry_cheat

    finder, cf = win.cheats.finder, win.ctl.ensure_file()
    own = ProCheat("p1", "Ammo", [ProEntry("bytes", 0x500, b"\x01", b"\x90")])
    cf.cheats.append(pro_compile.compile_cheat(own))
    finder.save()  # these notes: the new version's, holding their own p1
    into = win.ctl.project

    old_pc = carry_cheat()  # the old version's p1 "Health", still listed
    rp.learn([old_pc], _image())
    cf.cheats.append(pro_compile.compile_cheat(old_pc))
    source = pj.Project(into.title_id, "00.900", "eboot.bin", pro_cheats=[old_pc])

    ids: dict[str, str] = {}
    merged = cr.for_merge(source, into, pro_ids=ids)
    assert ids == {"p1": "p2"}
    (moved,), _report = rp.bring_over(merged.pro_cheats, _image(SHIFT), FakeAssembler())
    carried = pj.Project(into.title_id, into.version, "eboot.bin", pro_cheats=[moved])
    finder.adopt_carried(carried, {}, merge_into=into, old_pro_ids=ids)

    saved = {pc.id: pc for pc in _load(win).pro_cheats}
    assert sorted(saved) == ["p1", "p2"] and saved["p1"].name == "Ammo"
    assert [e.offset for e in saved["p2"].entries] == [e.offset for e in moved.entries]
    rows = [(c.pro.id, c.name) for c in cf.cheats if c.pro is not None]
    assert sorted(rows) == [("p1", "Ammo"), ("p2", "Health")]


def _load(win) -> pj.Project:
    p = win.ctl.project
    return pj.load(pj.project_path(p.title_id, p.version))
