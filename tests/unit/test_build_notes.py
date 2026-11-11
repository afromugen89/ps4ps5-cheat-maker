"""Crash notes (spec-cheat-builder B6, S7): a JSON file in the app folder."""

from __future__ import annotations

import json

import pytest

from cheatmaker.core import build_notes as bn
from tests import fakegame as fg

JMP = "e9fb2f0000"


def _note(game: fg.Game, pid: int = fg.PID, **kw) -> bn.OpenNote:
    site_off = fg.SUB_SITE - fg.BASE
    room = 0x3000
    return bn.OpenNote(
        title_id=kw.get("title_id", fg.TITLE),
        version=kw.get("version", fg.VERSION),
        pid=pid,
        site_off=site_off,
        method="hook",
        n=2,
        patches=(bn.Patch(site_off, fg.SUB_W.hex(), JMP + "90"),),
        rooms=((room, 0x20),),
    )


def test_open_clear_and_round_trip():
    g = fg.build(capture_ticks=False)
    assert bn.current_note() is None
    note = _note(g)
    bn.open_note(note)
    assert bn.current_note() == note
    raw = json.loads(bn.notes_path().read_text(encoding="utf-8"))
    assert raw["open"]["patches"][0]["orig_hex"] == fg.SUB_W.hex() and raw["crashed"] == []
    assert not bn.notes_path().with_suffix(".tmp").exists(), "written by rename"
    bn.clear_note()
    assert bn.current_note() is None
    bn.clear_note()  # nothing open: still fine


def test_crash_marks():
    assert not bn.is_crashed("PPSA01285", "03.000.005", 0x300, "hook")
    bn.mark_crashed("PPSA01285", "03.000.005", 0x300, "hook")
    bn.mark_crashed("PPSA01285", "03.000.005", 0x300, "hook")  # once is enough
    assert bn.is_crashed("ppsa01285", "03.000.005", 0x300, "hook")
    assert not bn.is_crashed("PPSA01285", "03.000.005", 0x300, "nop"), "per method"
    assert not bn.is_crashed("PPSA01285", "03.000.006", 0x300, "hook"), "per version"
    assert not bn.is_crashed("PPSA01285", "03.000.005", 0x304, "hook"), "per writer"
    assert len(json.loads(bn.notes_path().read_text(encoding="utf-8"))["crashed"]) == 1


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", ""])
def test_damaged_file_is_ignored(text):
    bn.notes_path().parent.mkdir(parents=True, exist_ok=True)
    bn.notes_path().write_text(text, encoding="utf-8")
    assert bn.current_note() is None and not bn.is_crashed("A", "1", 0, "hook")
    bn.mark_crashed("A", "1", 0, "hook")
    assert bn.is_crashed("A", "1", 0, "hook")


def test_bad_open_note_reads_as_none():
    bn.notes_path().parent.mkdir(parents=True, exist_ok=True)
    bn.notes_path().write_text(json.dumps({"open": {"pid": 1}, "crashed": []}), encoding="utf-8")
    assert bn.current_note() is None


def test_nothing_open_on_connect():
    g = fg.build(capture_ticks=False)
    assert bn.check_on_connect(g.session, sleep=lambda _s: None) is None


def test_same_game_still_running_is_put_back_not_marked():
    g = fg.build(capture_ticks=False)
    note = _note(g)
    jmp = bytes.fromhex(note.patches[0].new_hex)
    g.fake.poke(fg.PID, fg.SUB_SITE, jmp)
    g.fake.poke(fg.PID, fg.BASE + 0x3000, b"\x55" * 0x20)
    bn.open_note(note)
    waits = []
    hint = bn.check_on_connect(g.session, sleep=waits.append)
    assert "put back" in hint and waits == [bn.PUT_BACK_WAIT_S]
    assert g.fake.peek(fg.PID, fg.SUB_SITE, 6) == fg.SUB_W
    assert g.fake.peek(fg.PID, fg.BASE + 0x3000, 0x20) == bytes(0x20)
    assert bn.current_note() is None and not bn.is_crashed(fg.TITLE, fg.VERSION, 0x300, "hook")


def test_same_game_but_no_answer_says_restart():
    g = fg.build(capture_ticks=False)
    note = _note(g)
    g.fake.poke(fg.PID, fg.SUB_SITE, bytes.fromhex(note.patches[0].new_hex))
    bn.open_note(note)
    g.fake.fail_writes = True
    assert "Restart the game" in bn.check_on_connect(g.session, sleep=lambda _s: None)
    assert bn.current_note() == note, "kept for the next connect"


@pytest.mark.parametrize("how", ["restarted", "other version", "no game"])
def test_game_restarted_marks_the_method_crashed(how):
    g = fg.build(capture_ticks=False)
    if how == "restarted":
        note = _note(g, pid=fg.PID + 1)
    elif how == "other version":
        note = _note(g, version="01.000.000")
    else:
        note = _note(g)
        g.session.game = None
    bn.open_note(note)
    hint = bn.check_on_connect(g.session, sleep=lambda _s: None)
    assert hint == "Fix 2 crashed the game last time. Your game is unchanged after a restart."
    assert bn.is_crashed(note.title_id, note.version, note.site_off, "hook")
    assert bn.current_note() is None
    assert g.fake.writes == [], "a new game's code is never touched"
