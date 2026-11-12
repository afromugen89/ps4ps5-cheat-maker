"""Crash notes (spec-cheat-builder B6, S7): a JSON file in the app folder."""

from __future__ import annotations

import json
from dataclasses import replace

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


def test_damaged_marks_are_dropped_and_good_ones_kept():
    bn.notes_path().parent.mkdir(parents=True, exist_ok=True)
    good = {"title_id": "A", "version": "1", "writer_off": 16, "method": "hook"}
    bad = [None, 5, "x", {"writer_off": "x"}, {**good, "writer_off": "16"}, {**good, "title_id": 1}]
    bn.notes_path().write_text(json.dumps({"open": 7, "crashed": [*bad, good]}), "utf-8")
    assert bn.current_note() is None
    assert bn.is_crashed("A", "1", 16, "hook")
    bn.mark_crashed("B", "1", 0, "nop")
    assert len(json.loads(bn.notes_path().read_text(encoding="utf-8"))["crashed"]) == 2


def test_a_site_with_other_bytes_keeps_the_routines_and_says_restart():
    """The jump was re-aimed (finalize) or replaced: zeroing the rooms could leave a
    live jump into zeros, so they stay."""
    g = fg.build(capture_ticks=False)
    note = _note(g)
    other = b"\xe9\x10\x20\x00\x00\x90"
    g.fake.poke(fg.PID, fg.SUB_SITE, other)
    g.fake.poke(fg.PID, fg.BASE + 0x3000, b"\x55" * 0x20)
    bn.open_note(note)
    hint = bn.check_on_connect(g.session, sleep=lambda _s: None)
    assert hint == bn.RESTART
    assert g.fake.peek(fg.PID, fg.SUB_SITE, 6) == other
    assert g.fake.peek(fg.PID, fg.BASE + 0x3000, 0x20) == b"\x55" * 0x20, "routines kept"
    assert bn.current_note() is None and not bn.is_crashed(fg.TITLE, fg.VERSION, 0x300, "hook")


# -- linked values: places kept while the next one is built (spec-linked-values L12) ---------
def _place2(g: fg.Game, pid: int = fg.PID) -> bn.OpenNote:
    site_off = fg.WRITER - fg.BASE
    return replace(
        _note(g, pid),
        site_off=site_off,
        patches=(bn.Patch(site_off, fg.VMOVSS_W.hex(), JMP + "9090909090"),),
        rooms=((0x3100, 0x20),),
        place=2,
    )


def _in_game(g: fg.Game, *ns: bn.OpenNote) -> None:
    for n in ns:
        for p in n.patches:
            g.fake.poke(fg.PID, fg.BASE + p.site_off, bytes.fromhex(p.new_hex))
        for off, size in n.rooms:
            g.fake.poke(fg.PID, fg.BASE + off, b"\x55" * size)


def test_place_is_written_only_when_above_one():
    g = fg.build(capture_ticks=False)
    bn.open_note(_note(g))
    one = bn.notes_path().read_text(encoding="utf-8")
    assert '"place"' not in one and '"kept"' not in one, "as an older app writes it"
    bn.open_note(_place2(g))
    assert json.loads(bn.notes_path().read_text(encoding="utf-8"))["open"]["place"] == 2
    assert bn.current_note().place == 2


def test_keep_note_moves_open_to_kept_and_clear():
    g = fg.build(capture_ticks=False)
    bn.keep_note()  # nothing open: nothing to do
    assert not bn.notes_path().exists()
    first, second = _note(g), _place2(g)
    bn.open_note(first)
    bn.keep_note()
    assert bn.current_note() is None and bn.kept_notes() == [first]
    bn.open_note(second)
    bn.keep_note()
    assert bn.kept_notes() == [first, second]
    bn.clear_kept([2])
    assert bn.kept_notes() == [first]
    bn.open_note(second)
    bn.clear_all()
    assert bn.current_note() is None and bn.kept_notes() == []
    raw = json.loads(bn.notes_path().read_text(encoding="utf-8"))
    assert raw == {"open": None, "crashed": []}, "an empty kept list isn't written"


def test_old_file_without_kept_and_damaged_kept_load():
    bn.notes_path().parent.mkdir(parents=True, exist_ok=True)
    bn.notes_path().write_text(json.dumps({"open": None, "crashed": []}), "utf-8")
    assert bn.kept_notes() == []
    bn.notes_path().write_text(json.dumps({"open": None, "kept": 5, "crashed": []}), "utf-8")
    assert bn.kept_notes() == []
    bn.notes_path().write_text(
        json.dumps({"open": None, "kept": [7, {"pid": 1}], "crashed": []}), "utf-8"
    )
    assert bn.kept_notes() == []


def test_kept_places_in_the_same_game_are_put_back_newest_first_with_one_wait():
    g = fg.build(capture_ticks=False)
    first, second = _note(g), _place2(g)
    _in_game(g, first, second)
    for n in (first, second):
        bn.open_note(n)
        bn.keep_note()
    waits = []
    hint = bn.check_on_connect(g.session, sleep=waits.append)
    assert (
        hint
        == bn.KEPT_PUT_BACK
        == ("Fixes from last time were still in the game. They've been put back.")
    )
    assert waits == [bn.PUT_BACK_WAIT_S], "one wait for every place"
    sites = [a for _p, a, _d in g.fake.writes if a in (fg.WRITER, fg.SUB_SITE)]
    assert sites == [fg.WRITER, fg.SUB_SITE], "place 2 first"
    assert g.fake.peek(fg.PID, fg.SUB_SITE, 6) == fg.SUB_W
    assert g.fake.peek(fg.PID, fg.WRITER, 10) == fg.VMOVSS_W
    for off in (0x3000, 0x3100):
        assert g.fake.peek(fg.PID, fg.BASE + off, 0x20) == bytes(0x20)
    assert bn.kept_notes() == [] and not bn.is_crashed(fg.TITLE, fg.VERSION, 0x300, "hook")


def test_open_and_kept_together_the_open_hint_wins():
    g = fg.build(capture_ticks=False)
    first, second = _note(g), _place2(g)
    _in_game(g, first, second)
    bn.open_note(first)
    bn.keep_note()
    bn.open_note(second)
    hint = bn.check_on_connect(g.session, sleep=lambda _s: None)
    assert hint == "Fix 2 from last time was still in the game. It's been put back."
    assert g.fake.peek(fg.PID, fg.SUB_SITE, 6) == fg.SUB_W, "the kept place is put back too"
    assert bn.current_note() is None and bn.kept_notes() == []


@pytest.mark.parametrize("how", ["restarted", "no game"])
def test_kept_places_in_another_game_are_dropped_never_marked(how):
    g = fg.build(capture_ticks=False)
    first, second = _note(g, pid=fg.PID + 1), _place2(g, pid=fg.PID + 1)
    if how == "no game":
        first, second = _note(g), _place2(g)
        g.session.game = None
    bn.open_note(first)
    bn.keep_note()
    bn.open_note(second)  # the fix under test: this one is marked
    assert bn.check_on_connect(g.session, sleep=lambda _s: None).startswith("Fix 2 crashed")
    assert bn.kept_notes() == [] and g.fake.writes == []
    assert bn.is_crashed(fg.TITLE, fg.VERSION, fg.WRITER - fg.BASE, "hook")
    assert not bn.is_crashed(fg.TITLE, fg.VERSION, fg.SUB_SITE - fg.BASE, "hook"), "kept: never"


def test_kept_place_with_a_changed_site_keeps_its_rooms_and_says_restart():
    g = fg.build(capture_ticks=False)
    first, second = _note(g), _place2(g)
    _in_game(g, first, second)
    other = b"\xe9\x10\x20\x00\x00\x90"
    g.fake.poke(fg.PID, fg.SUB_SITE, other)
    for n in (first, second):
        bn.open_note(n)
        bn.keep_note()
    assert bn.check_on_connect(g.session, sleep=lambda _s: None) == bn.RESTART
    assert g.fake.peek(fg.PID, fg.BASE + 0x3000, 0x20) == b"\x55" * 0x20, "place 1 kept"
    assert g.fake.peek(fg.PID, fg.BASE + 0x3100, 0x20) == bytes(0x20), "place 2 clean"
    assert bn.kept_notes() == []


def test_kept_places_no_answer_says_restart_and_keeps_them():
    g = fg.build(capture_ticks=False)
    first = _note(g)
    _in_game(g, first)
    bn.open_note(first)
    bn.keep_note()
    g.fake.fail_writes = True
    assert bn.check_on_connect(g.session, sleep=lambda _s: None) == bn.RESTART
    assert bn.kept_notes() == [first], "kept for the next connect"
