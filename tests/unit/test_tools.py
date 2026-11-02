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
