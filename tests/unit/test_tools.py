"""Smoke test for the console spike script (C10a) on the fake game."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from tests import fakegame as fg

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_watch_regs_prints_and_saves_candidates(monkeypatch, tmp_path, capsys):
    game = fg.build(connect=False)
    tool = _load("watch_regs")
    monkeypatch.setattr(tool.Ps5DebugNgClient, "connect", lambda ip, port: game.fake)
    out = tmp_path / "c10a.json"
    argv = ["watch_regs", "1.2.3.4", hex(game.health_addr), "--seconds", "1", "--out", str(out)]
    monkeypatch.setattr(sys, "argv", argv)
    assert tool.main() == 0
    text = capsys.readouterr().out
    assert "vmovss xmm0,[r12+2ACh]" in text and "reader" in text and ": yes" in text
    doc = json.loads(out.read_text())
    assert doc["candidates"][0]["offset"] == hex(fg.READER - fg.BASE)
    assert doc["candidates"][0]["confirmed"] is True and doc["kind"] == "access"


# -- make_old_notes (console tests C21b-C21g) ----------------------------------------------
def _notes_files(version: str):
    from cheatmaker.core import project as pj
    from cheatmaker.core import updater as up

    return pj.project_path(fg.TITLE, version), up.signatures_path(fg.TITLE, version)


def _setup_cur():
    from cheatmaker.core import project as pj
    from cheatmaker.core import updater as up

    notes, sigs = _notes_files(fg.VERSION)
    pj.save(notes, pj.Project(fg.TITLE, fg.VERSION, objects=[pj.ObjectSpec("o", "Gold", None)]))
    up.save_signatures(sigs, fg.TITLE, fg.VERSION, {0x10: up.Signature.from_text("AA BB")})
    return notes.read_bytes(), sigs.read_bytes()


def _run(monkeypatch, *args) -> int:
    tool = _load("make_old_notes")
    monkeypatch.setattr(sys, "argv", ["make_old_notes", *args])
    return tool.main()


def test_make_old_notes_forward_and_undo_are_byte_identical(monkeypatch, capsys):
    notes_b, sigs_b = _setup_cur()
    cur_notes, cur_sigs = _notes_files(fg.VERSION)
    old_notes, old_sigs = _notes_files("03.000.004")
    old_notes.write_text("my real old notes")  # a real older file is set aside, then back
    assert _run(monkeypatch, fg.TITLE, fg.VERSION, "03.000.004") == 0
    out = capsys.readouterr().out
    assert "Close Cheat Maker" in out and "Set aside" in out and "Wrote" in out
    assert not cur_notes.exists() and not cur_sigs.exists()
    assert json.loads(old_notes.read_text())["version"] == "03.000.004"
    assert json.loads(old_sigs.read_text())["version"] == "03.000.004"
    assert json.loads(old_sigs.read_text())["signatures"] == {"0x10": "AA BB"}
    # the app's dry run makes new CUR notes; then --undo
    cur_notes.write_text("made by the dry run")
    assert _run(monkeypatch, fg.TITLE, fg.VERSION, "03.000.004", "--undo") == 0
    assert cur_notes.read_bytes() == notes_b and cur_sigs.read_bytes() == sigs_b
    assert old_notes.read_text() == "my real old notes" and not old_sigs.exists()
    carried = cur_notes.with_name(cur_notes.name + ".carried")
    assert carried.read_text() == "made by the dry run", "kept, never deleted"
    assert not list(cur_notes.parent.glob("*.keep")) and not list(cur_sigs.parent.glob("*.keep"))


def test_make_old_notes_refuses_plainly(monkeypatch, capsys):
    assert _run(monkeypatch, fg.TITLE, fg.VERSION, "03.000.004") == 1
    assert "There are no notes" in capsys.readouterr().out
    _setup_cur()
    assert _run(monkeypatch, fg.TITLE, fg.VERSION, "3.0.5") == 1
    assert "the same version" in capsys.readouterr().out
    cur_notes, _ = _notes_files(fg.VERSION)
    keep = cur_notes.with_name(cur_notes.name + ".keep")
    keep.write_text("x")
    assert _run(monkeypatch, fg.TITLE, fg.VERSION, "03.000.004") == 1
    assert "already exists" in capsys.readouterr().out
    keep.unlink()
    assert _run(monkeypatch, fg.TITLE, fg.VERSION, "03.000.004", "--undo") == 1
    assert "is missing" in capsys.readouterr().out
    cur_notes.write_text("{")
    assert _run(monkeypatch, fg.TITLE, fg.VERSION, "03.000.004") == 1
    assert "can't be read" in capsys.readouterr().out
