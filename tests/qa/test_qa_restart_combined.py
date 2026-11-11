"""QA for "combined cheats that survive restarts" (docs/spec-restart-combined.md AC-1..AC-8),
2026-11-11. Test plan (TC-xx) and bugs (BUG-xx): qa/reports/2026-11-11-restart-combined.md.

Tests marked ``xfail(strict=True)`` reproduce an open bug: they fail today and must start
passing (then drop the marker) once the bug is fixed.
"""

from __future__ import annotations

import json
import logging
import struct

import pytest
from PySide6.QtCore import Qt

from cheatmaker import formats
from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind, combine
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ConnectionLost
from cheatmaker.ui import hints
from cheatmaker.ui.cheats_view import COL_KEEPS, COL_ON, FOUND_AGAIN
from cheatmaker.ui.find_again import LearnDialog
from cheatmaker.ui.save_dialog import SaveDialog
from tests import fakegame as fg
from tests.ui.test_find_again import (
    add_found_fraction,
    add_health_cheat,
    connect,
    fraction_in_player,
    learn,
    money_in_enemy,
    session_part,
    tick,
)

SESSION = "This session only"
GOD_OFF = fg.DATA - fg.BASE + 0x80  # a module byte: a fixed part
GOD_AT = fg.DATA + 0x80


# -- helpers -----------------------------------------------------------------------------
def untick(w, row=0):
    w.cheats.table.item(row, COL_ON).setCheckState(Qt.CheckState.Unchecked)


def fixed_cheat(w, name="god", off=GOD_OFF, on=b"\x01", old=b"\x00") -> Cheat:
    c = Cheat(name, [Location(LocationKind.MODULE_VALUE, off, on, old)], freeze=True)
    w.ctl.ensure_file().cheats.append(c)
    w.ctl.cheats_changed.emit()
    return c


def combine_picked(w) -> Cheat:
    w.cheats.table.selectAll()
    w.cheats.combine_btn.click()
    cheats = w.ctl.cheat_file.cheats
    assert len(cheats) == 1, w.status.text()
    w.cheats.table.selectRow(0)
    return cheats[0]


def saved() -> pj.Project:
    return pj.load(pj.project_path(fg.TITLE, fg.VERSION))


def sess(name, addr, vt=ValueType.INT32, value=1) -> Cheat:
    raw = vt.encode(value)
    return Cheat(name, [Location(LocationKind.SESSION_ADDRESS, addr, raw, raw)], value_type=vt)


def learned(name, obj="obj1", vt=ValueType.FLOAT) -> Cheat:
    raw = vt.encode(1)
    return Cheat(
        name,
        [Location(LocationKind.SESSION_ADDRESS, 0, raw, raw)],
        value_type=vt,
        anchor=(obj, name),
    )


def mixed_three(w, game) -> Cheat:
    """Found-again health (player) + session money (enemy 0) + fixed god byte."""
    learn(w, add_health_cheat(w, game))
    session_part(w, "money", money_in_enemy(game), ValueType.INT64, 77)
    fixed_cheat(w)
    return combine_picked(w)


def pick_first(obj, options):
    return options[0].address


# == AC-1 Combine ==========================================================================
def test_tc01_combine_fixed_moving_learned_keeps_each_part(win, console):  # TC-01
    connect(win)
    c = mixed_three(win, console.game)
    assert c.name == "Infinite Health + money + god"
    assert [loc.part for loc in c.locations] == ["Infinite Health", "money", "god"]
    assert [loc.vtype for loc in c.locations] == [ValueType.FLOAT, ValueType.INT64, None]
    assert [loc.anchor for loc in c.locations] == [("obj1", "Infinite Health"), None, None]
    assert [loc.kind for loc in c.locations] == [
        LocationKind.SESSION_ADDRESS,
        LocationKind.SESSION_ADDRESS,
        LocationKind.MODULE_VALUE,
    ]
    assert c.anchor is None and c.value_type is None and c.value is None and c.freeze
    assert win.cheats.table.item(0, COL_KEEPS).text() == SESSION
    assert win.cheats.learn_btn.isEnabled()
    p = saved()
    assert p.cheats == [], "the found-again entry is gone and nothing moving is saved"
    assert [o.id for o in p.objects] == ["obj1"], "objects stay"


def test_tc02_learn_mixed_then_restart_writes_every_part(win, console, monkeypatch):  # TC-02
    connect(win)
    c = mixed_three(win, console.game)
    calls = []
    real = rf.learn
    monkeypatch.setattr(rf, "learn", lambda *a, **k: calls.append(a[3]) or real(*a, **k))
    learn(win, c)
    assert calls == ["money"], "fixed and learned parts are skipped"
    assert c.found_again and win.cheats.table.item(0, COL_KEEPS).text() == FOUND_AGAIN
    [entry] = saved().cheats
    assert [p.name for p in entry.parts] == ["Infinite Health", "money", "god"]
    assert [p.learned for p in entry.parts] == [True, True, False]
    assert entry.parts[2].kind == "module_value" and entry.parts[2].offset == GOD_OFF
    assert entry.parts[2].on == b"\x01" and entry.parts[2].off == b"\x00"

    win.ctl.shutdown()
    run2 = console.restart(2)
    win.cheats.finder.ask_pick = pick_first  # two enemies of the same class in run 2
    connect(win)
    [c2] = win.ctl.cheat_file.cheats
    assert [loc.part for loc in c2.locations] == ["Infinite Health", "money", "god"]
    assert win.status.text().startswith("Ready: Infinite Health + money + god found again (2 parts")
    tick(win)
    assert c2.enabled
    e = win.ctl.live.trackers["obj2"].address
    assert run2.health() == 999.0 and run2.value(e, "int64") == 77
    assert run2.fake.peek(fg.PID, GOD_AT, 1) == b"\x01"
    untick(win)
    assert run2.fake.peek(fg.PID, GOD_AT, 1) == b"\x00", "fixed part restored"
    assert run2.health() == 999.0 and run2.value(e, "int64") == 77, "learned parts left"


def test_tc03_all_fixed_parts_says_yes_and_is_not_in_the_notes(win, console):  # TC-03
    connect(win)
    fixed_cheat(win, "a", GOD_OFF)
    fixed_cheat(win, "b", GOD_OFF + 1)
    c = combine_picked(win)
    assert win.cheats.table.item(0, COL_KEEPS).text() == "Yes"
    assert c.saveable and not c.found_again and not win.cheats.learn_btn.isEnabled()
    assert win.ctl.project is None or win.ctl.project.cheats == []


def test_tc04_at_most_four_parts_that_move(win, console):  # TC-04
    h = fg.HEAP + 0xA000
    five = [sess(f"v{i}", h + 8 * i) for i in range(5)]
    with pytest.raises(ValueError) as e:
        combine(five)
    assert str(e.value) == "Combine up to 4 values that move; these have 5."
    fixed = [
        Cheat(f"f{i}", [Location(LocationKind.CODE_PATCH, 0x10 * i, b"\x90", b"\xcc")])
        for i in range(6)
    ]
    assert len(combine(five[:4] + fixed).locations) == 10, "fixed parts have no limit"
    # learned parts count as moving
    with pytest.raises(ValueError, match="these have 5"):
        combine([*five[:3], learned("a"), learned("b")])
    # a source cheat with two session lines counts twice
    two = Cheat(
        "pair",
        [
            Location(LocationKind.SESSION_ADDRESS, h + 0x100, b"\x01", b"\x01"),
            Location(LocationKind.SESSION_ADDRESS, h + 0x200, b"\x01", b"\x01"),
        ],
    )
    with pytest.raises(ValueError, match="these have 5"):
        combine([*five[:3], two])


def test_tc04b_too_many_in_the_ui_changes_nothing(win, console):  # TC-04
    connect(win)
    for i in range(5):
        session_part(win, f"v{i}", fg.HEAP + 0xA000 + 8 * i, ValueType.INT32, 1)
    win.ctl.cheats_changed.emit()
    win.cheats.table.selectAll()
    assert win.cheats.combine_btn.isEnabled()
    win.cheats.combine_btn.click()
    assert len(win.ctl.cheat_file.cheats) == 5
    assert "Combine up to 4 values that move; these have 5." in win.status.text()


@pytest.mark.parametrize(
    ("a", "b", "clash"),
    [
        (sess("health", 0x1000, ValueType.FLOAT), sess("fraction", 0x1002), True),
        (sess("health", 0x1000, ValueType.FLOAT), sess("fraction", 0x1004), False),
        (sess("health", 0x1004), sess("fraction", 0x1000, ValueType.FLOAT), False),
        (sess("health", 0x1003), sess("fraction", 0x1000, ValueType.FLOAT), True),
    ],
)
def test_tc05_overlap_session_parts(a, b, clash):  # TC-05
    if clash:
        with pytest.raises(ValueError) as e:
            combine([a, b])
        assert str(e.value) == "health and fraction overlap in memory."
    else:
        assert len(combine([a, b]).locations) == 2


def test_tc05b_overlap_fixed_and_learned_parts():  # TC-05
    f1 = Cheat("f1", [Location(LocationKind.CODE_PATCH, 0x100, b"\x90\x90", b"\xcc\xcc")])
    f2 = Cheat("f2", [Location(LocationKind.MODULE_VALUE, 0x101, b"\x01", b"\x00")])
    with pytest.raises(ValueError, match=r"f1 and f2 overlap in memory\."):
        combine([f1, f2])
    f3 = Cheat("f3", [Location(LocationKind.MODULE_VALUE, 0x101, b"\x01", b"\x00", "other.prx")])
    assert len(combine([f1, f3]).locations) == 2, "another module is another space"
    with pytest.raises(ValueError, match="overlap in memory"):
        combine([learned("hp"), Cheat("hp2", learned("hp").locations, anchor=("obj1", "hp"))])
    # a learned part right now at 0x5000 vs a session value at 0x5002
    addr = {("obj1", "hp"): 0x5000, ("obj2", "mp"): 0x5004}
    with pytest.raises(ValueError, match=r"hp and x overlap in memory\."):
        combine([learned("hp"), sess("x", 0x5002)], addr_of=lambda loc: addr[loc.anchor])
    assert (
        len(
            combine(
                [learned("hp"), learned("mp", "obj2")], addr_of=lambda loc: addr[loc.anchor]
            ).locations
        )
        == 2
    )


def test_tc05c_overlap_module_value_and_session_same_byte():  # TC-05, BUG-03 fixed
    a = Cheat("a", [Location(LocationKind.MODULE_VALUE, 0x4040, b"\x01", b"\x00")])
    b = sess("b", fg.BASE + 0x4040, ValueType.INT8, 2)  # the very same byte in this run
    with pytest.raises(ValueError, match=r"a and b overlap in memory\."):
        combine([a, b], addr_of=lambda _loc: None, module_base=fg.BASE)
    assert combine([a, b]), "module base not known: can't tell"


def test_tc05d_overlap_in_the_ui_with_a_found_again_part(win, console):  # TC-05
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    session_part(win, "copy", console.game.health_addr + 2, ValueType.INT16, 1)
    win.ctl.cheats_changed.emit()
    win.cheats.table.selectAll()
    win.cheats.combine_btn.click()
    assert len(win.ctl.cheat_file.cheats) == 2, "nothing combined"
    assert "Infinite Health and copy overlap in memory." in win.status.text()
    assert [c.name for c in saved().cheats] == ["Infinite Health"], "notes unchanged"


def test_tc06_part_names_several_lines_and_clashes():  # TC-06
    multi = Cheat(
        "health",
        [
            Location(LocationKind.SESSION_ADDRESS, 0x100, b"\x01", b"\x01"),
            Location(LocationKind.SESSION_ADDRESS, 0x200, b"\x01", b"\x01"),
        ],
    )
    c = combine([multi, sess("fraction", 0x300)])
    assert [loc.part for loc in c.locations] == ["health (1)", "health (2)", "fraction"]
    c = combine([sess("x", 0x100), sess("x", 0x200), sess("x", 0x300)])
    assert [loc.part for loc in c.locations] == ["x", "x (2)", "x (3)"]
    again = combine([c, sess("x", 0x400)])  # combine a combined cheat: names kept
    assert [loc.part for loc in again.locations] == ["x", "x (2)", "x (3)", "x (4)"]
    assert len({loc.part for loc in again.locations}) == 4


def test_tc07_combine_on_for_two_found_again_off_for_one(win, console):  # TC-07
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    add_found_fraction(win)
    win.cheats.table.selectRow(0)
    assert not win.cheats.combine_btn.isEnabled()
    win.cheats.table.selectAll()
    assert win.cheats.combine_btn.isEnabled()


def test_tc09_combine_found_again_while_disconnected(win, console):  # TC-09, BUG-02 fixed
    # Fix chosen: Combine is refused (button off, plain message) for found-again cheats
    # while the game's notes aren't loaded, so nothing is lost next run. Connect, then
    # combine.
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    add_found_fraction(win)
    win.ctl.shutdown()  # the game is closed: no connection, no notes loaded
    win.cheats.table.selectAll()
    assert not win.cheats.combine_btn.isEnabled()
    assert win.cheats.combine_btn.toolTip() == hints.COMBINE_NEEDS_NOTES
    win.cheats.do_combine()  # even if pressed some other way
    assert win.status.text() == hints.COMBINE_NEEDS_NOTES
    assert [c.name for c in win.ctl.cheat_file.cheats] == ["Infinite Health", "fraction"]
    console.restart(2)
    connect(win)
    win.cheats.table.selectAll()
    win.cheats.combine_btn.click()  # connected: it works and is kept
    assert [c.name for c in win.ctl.cheat_file.cheats] == ["Infinite Health + fraction"]
    assert [c.name for c in saved().cheats] == ["Infinite Health + fraction"]


# == AC-2 Find it next time on a combined cheat ============================================
def two_moving(w, game, second=fraction_in_player, vt=ValueType.INT32, name2="fraction"):
    a = session_part(w, "health", game.health_addr, ValueType.FLOAT, 999.0)
    b = session_part(w, name2, second(game), vt, 77)
    w.ctl.cheat_file.cheats[:] = [combine([a, b], name="Health")]
    w.ctl.cheats_changed.emit()
    w.cheats.table.selectRow(0)
    return w.ctl.cheat_file.cheats[0]


def test_tc10_learn_button_rules(win, console):  # TC-10
    c = Cheat(
        "x",
        [
            Location(LocationKind.SESSION_ADDRESS, fg.HEAP + 0xA000, b"\x01\x02", b"\x00\x00"),
            Location(LocationKind.SESSION_ADDRESS, fg.HEAP + 0xA010, b"\x01", b"\x00"),
        ],
    )
    connect(win)
    # a moving part without a number type (raw bytes): off
    win.ctl.cheat_file.cheats[:] = [combine([c, sess("y", fg.HEAP + 0xA020)])]
    win.ctl.cheats_changed.emit()
    win.cheats.table.selectRow(0)
    assert not win.cheats.learn_btn.isEnabled()
    # every part has a type: on; disconnected: off
    two_moving(win, console.game)
    assert win.cheats.learn_btn.isEnabled()
    win.ctl.handle_lost()
    win.cheats.table.selectRow(0)
    assert not win.cheats.learn_btn.isEnabled()


def test_tc10b_learn_button_off_when_nothing_moves(win, console):  # TC-10
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    fixed_cheat(win)
    combine_picked(win)
    assert not win.cheats.learn_btn.isEnabled(), "nothing left to learn"


@pytest.mark.xfail(
    reason="BUG-01: a moving part in the same object as an already-learned part gets a "
    "second object and a second hook",
    strict=True,
)
def test_tc11_same_object_as_an_already_learned_part_shares_it(win, console):  # TC-11
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    session_part(win, "fraction", fraction_in_player(console.game), ValueType.INT32, 77)
    win.ctl.cheats_changed.emit()
    c = combine_picked(win)
    learn(win, c)
    assert c.found_again
    objs = win.ctl.project.objects
    assert len(objs) == 1, [(o.id, [f.name for f in o.fields]) for o in objs]
    assert [f.name for f in objs[0].fields] == ["Infinite Health", "fraction"]
    assert len(win.cheats.finder.installed) == 1, "one hook"


def test_tc11b_same_object_merge_gives_one_tracker_after_restart(win, console):  # TC-11
    connect(win)
    learn(win, two_moving(win, console.game))
    [obj] = win.ctl.project.objects
    assert obj.label == "health" and [f.off for f in obj.fields] == [fg.HP, fg.HP32]
    win.ctl.shutdown()
    console.restart(3)
    connect(win)
    assert list(win.ctl.live.trackers) == [obj.id]
    assert win.status.text().startswith("Ready: Health found again (2 parts")


def test_tc12_first_part_fails_second_never_starts(win, console, monkeypatch):  # TC-12
    connect(win)
    game = console.game
    nowhere = fg.HEAP + 0xE000
    game.fake.poke(fg.PID, nowhere, struct.pack("<f", 5.0))
    a = session_part(win, "health", nowhere, ValueType.FLOAT, 999.0)
    b = session_part(win, "fraction", fraction_in_player(game), ValueType.INT32, 77)
    win.ctl.cheat_file.cheats[:] = [c := combine([a, b], name="Health")]
    win.ctl.cheats_changed.emit()
    rooms = list(win.ctl.session.reserved_rooms)
    calls = []
    real = rf.learn
    monkeypatch.setattr(rf, "learn", lambda *a, **k: calls.append(a[3]) or real(*a, **k))
    dlg = learn(win, c)
    assert calls == ["health"]
    assert dlg.hint.text() == (
        "Couldn't learn health (part 1 of 2): nothing in the game touched the value while "
        "we watched. Nothing was changed."
    )
    assert win.ctl.session.reserved_rooms == rooms
    assert [loc.anchor for loc in c.locations] == [None, None]
    assert win.ctl.project is None or win.ctl.project.objects == []


def cancel_on_watch(monkeypatch, fake, dlg, nth: int, kind: str):
    """Press Cancel while the ``nth`` watch of ``kind`` is running (the fake asks
    ``until`` once, with no time passed: it must say stop)."""
    real = fake.watch
    seen = []

    def watch(pid, addr, size, k, timeout_s, *, until=None):
        if k == kind:
            seen.append(addr)
            if len(seen) == nth:
                dlg.do_cancel()
        return real(pid, addr, size, k, timeout_s, until=until)

    monkeypatch.setattr(fake, "watch", watch)
    return seen


def test_tc13_cancel_during_the_first_watch(win, console, monkeypatch):  # TC-13
    connect(win)
    c = two_moving(win, console.game, money_in_enemy, ValueType.INT64, "money")
    rooms = list(win.ctl.session.reserved_rooms)
    dlg = LearnDialog(win.ctl, win.cheats.finder, c, win)
    fake = console.game.fake
    cancel_on_watch(monkeypatch, fake, dlg, 1, "access")
    dlg.start_btn.click()
    assert fake.until_answers[-1] is True, "the watch was told to stop"
    assert dlg.hint.text() == (
        "Couldn't learn health (part 1 of 2): you pressed Cancel. Nothing was changed."
    )
    assert win.ctl.session.reserved_rooms == rooms
    assert win.cheats.finder.installed == {} and win.cheats.finder.by_chain == set()
    assert dlg.start_btn.isEnabled() and not dlg.cancel_btn.isEnabled()
    assert len(c.moving_parts) == 2


def test_tc14_cancel_while_proving_part_two_restores_rooms(win, console, monkeypatch):
    # TC-14: part 1 learned (its rooms reserved), Cancel during part 2's spare-memory check
    connect(win)
    c = two_moving(win, console.game, money_in_enemy, ValueType.INT64, "money")
    rooms = list(win.ctl.session.reserved_rooms)
    dlg = LearnDialog(win.ctl, win.cheats.finder, c, win)
    fake = console.game.fake
    seen_rooms = []
    real_learn = rf.learn

    def spy(*a, **k):
        seen_rooms.append(list(win.ctl.session.reserved_rooms))
        return real_learn(*a, **k)

    monkeypatch.setattr(rf, "learn", spy)
    cancel_on_watch(monkeypatch, fake, dlg, 2, "write")  # 1st write watch: part 1's prove
    dlg.start_btn.click()
    assert len(seen_rooms) == 2 and len(seen_rooms[1]) > len(rooms), "part 1's rooms held"
    assert dlg.hint.text() == (
        "Couldn't learn money (part 2 of 2): you pressed Cancel. Nothing was changed."
    )
    assert win.ctl.session.reserved_rooms == rooms
    assert fake.peek(fg.PID, fg.READER, 10) == fg.VMOVSS_R
    assert fake.peek(fg.PID, fg.MOV64_SITE, 7) == fg.MOV64_W
    assert win.ctl.project is None or win.ctl.project.objects == []


def test_tc15_cancel_after_first_install_restores_rooms_and_paths(win, console, monkeypatch):
    # TC-15: Cancel in the install stage: hooks out, rooms and memory-path list as before
    connect(win)
    c = two_moving(win, console.game, money_in_enemy, ValueType.INT64, "money")
    finder = win.cheats.finder
    finder.by_chain.add("objX")
    rooms = list(win.ctl.session.reserved_rooms)
    dlg = LearnDialog(win.ctl, finder, c, win)
    real = ah.install

    def install_then_cancel(session, obj):
        state = real(session, obj)
        dlg.do_cancel()
        return state

    monkeypatch.setattr(ah, "install", install_then_cancel)
    dlg.start_btn.click()
    fake = console.game.fake
    assert fake.peek(fg.PID, fg.READER, 10) == fg.VMOVSS_R
    assert fake.peek(fg.PID, fg.MOV64_SITE, 7) == fg.MOV64_W
    assert finder.installed == {} and finder.by_chain == {"objX"}
    assert win.ctl.session.reserved_rooms == rooms
    assert "you pressed Cancel" in dlg.hint.text()


@pytest.mark.xfail(
    reason="BUG-04: an unexpected (non-CheatMakerError) error while learning skips the "
    "rollback: reserved rooms stay changed",
    strict=True,
)
def test_tc16_unexpected_error_still_rolls_back(win, console, monkeypatch):  # TC-16
    connect(win)
    c = two_moving(win, console.game, money_in_enemy, ValueType.INT64, "money")
    rooms = list(win.ctl.session.reserved_rooms)
    real, calls = rf.learn, []

    def second_breaks(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("garbage from the console")
        return real(*a, **k)

    monkeypatch.setattr(rf, "learn", second_breaks)
    learn(win, c)
    assert win.ctl.session.reserved_rooms == rooms


def test_tc17_a_ticked_cheat_is_turned_off_first(win, console, monkeypatch):  # TC-17
    connect(win)
    c = two_moving(win, console.game)
    tick(win)
    assert c.enabled
    state = []
    real = rf.learn
    monkeypatch.setattr(rf, "learn", lambda *a, **k: state.append(c.enabled) or real(*a, **k))
    learn(win, c)
    assert state == [False, False] and not c.enabled
    assert str(id(c)) not in win.ctl.freezer._entries


def test_tc18_progress_covers_all_parts_and_pointers_apply_to_every_part(
    win, console, monkeypatch
):  # TC-18
    connect(win)
    c = two_moving(win, console.game, money_in_enemy, ValueType.INT64, "money")
    seen_ptr = []
    real = rf.learn
    monkeypatch.setattr(
        rf, "learn", lambda *a, **k: seen_ptr.append(k["pointer_scan"]) or real(*a, **k)
    )
    dlg = LearnDialog(win.ctl, win.cheats.finder, c, win)
    dlg.pointers.setChecked(True)
    prog = []
    dlg._progress.done.connect(prog.append)
    dlg.start_btn.click()
    assert seen_ptr == [True, True]
    assert prog == sorted(prog) and prog[0] == 0 and prog[-1] == 1
    assert any(0.5 <= p < 1 for p in prog), "part 2 is the second half"
    assert dlg.progress.value() == 100 and dlg.pc_off_btn.isHidden()
    assert not dlg.start_btn.isEnabled(), "done: Start off"


def test_tc19_connection_lost_in_part_two_rolls_back(win, console, monkeypatch):  # TC-19
    connect(win)
    c = two_moving(win, console.game, money_in_enemy, ValueType.INT64, "money")
    rooms = list(win.ctl.session.reserved_rooms)
    real, calls = rf.learn, []

    def drop(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise ConnectionLost("PS5 went away")
        return real(*a, **k)

    monkeypatch.setattr(rf, "learn", drop)
    dlg = learn(win, c)
    assert win.ctl.session.reserved_rooms == rooms
    assert not win.ctl.session.connected and len(c.moving_parts) == 2
    assert win.cheats.finder.installed == {}
    assert dlg._cancel is None and not dlg.cancel_btn.isEnabled(), "not stuck busy"


# == AC-3 Next run =========================================================================
def player_and_enemy(win, console) -> Cheat:
    """Health (player) + money (enemy 0), learned in one Start, then the app restarts
    into run 2 (two enemies: the player picks the first)."""
    connect(win)
    learn(win, two_moving(win, console.game, money_in_enemy, ValueType.INT64, "money"))
    win.ctl.shutdown()
    console.restart(2)
    win.cheats.finder.ask_pick = pick_first
    connect(win)
    [c] = win.ctl.cheat_file.cheats
    return c


def test_tc30_one_row_also_after_a_reconnect(win, console):  # TC-30
    c = player_and_enemy(win, console)
    assert c.name == "Health" and not c.enabled and len(c.learned_parts) == 2
    # connect again in the same run (Connect pressed again): still one row
    win.ctl.shutdown()
    connect(win)
    assert [x.name for x in win.ctl.cheat_file.cheats] == ["Health"]
    win.cheats.again_btn.click()
    assert [x.name for x in win.ctl.cheat_file.cheats] == ["Health"]
    assert win.status.text().count("Ready: Health found again") == 1


def test_tc31_one_question_two_objects_yes_confirms_all(win, console):  # TC-31
    c = player_and_enemy(win, console)
    trackers = [win.ctl.live.trackers[o] for o in c.objects]
    for t in trackers:
        t.confirmed = False
    asked = []
    win.cheats.finder.ask_confirm = lambda text: asked.append(text) or True
    tick(win)
    assert len(asked) == 1
    assert asked[0].startswith(
        "Is this your Health? health shows 100.00, money shows 250,000 right now"
    )
    assert all(t.confirmed for t in trackers) and c.enabled
    untick(win)
    tick(win)
    assert len(asked) == 1, "asked once per run"


def test_tc31b_question_skipped_only_when_every_object_confirmed(win, console):  # TC-31
    c = player_and_enemy(win, console)
    t1, t2 = (win.ctl.live.trackers[o] for o in c.objects)
    t1.confirmed, t2.confirmed = True, False
    asked = []
    win.cheats.finder.ask_confirm = lambda text: asked.append(text) or False
    run2 = console.game
    tick(win)
    assert len(asked) == 1 and not c.enabled
    assert run2.health() == 100.0, "No writes nothing, not even the confirmed part"
    assert win.status.text() == hints.CONFIRM_NO.format(name="Health")


def test_tc32_restart_with_one_part_missing(win, console):  # TC-32
    connect(win)
    learn(win, two_moving(win, console.game, money_in_enemy, ValueType.INT64, "money"))
    win.ctl.shutdown()
    run3 = console.restart(3)  # no enemies in this run: money is nowhere
    searched = []
    win.cheats.finder.open_search = lambda vt, v: searched.append((vt, v))
    connect(win)
    [c] = win.ctl.cheat_file.cheats
    assert win.status.text().startswith(
        hints.COMBINED_NOT_FOUND.format(part="money", name="Health")
    )
    assert searched == [(ValueType.INT64, 77)] or searched[0][0] is ValueType.INT64
    before = list(run3.fake.writes)
    tick(win)
    assert not c.enabled
    assert win.status.text() == (
        "Health: money isn't found right now, so nothing was changed. Press Find it again."
    )
    assert run3.health() == 100.0, "the part that is found isn't written either"
    assert all(a < fg.HEAP for _p, a, _d in run3.fake.writes[len(before) :])


def test_tc33_part_count_says_part_not_parts(win, console):  # TC-33, BUG-05 fixed
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    fixed_cheat(win)
    combine_picked(win)
    win.ctl.shutdown()
    console.restart(2)
    connect(win)
    assert "found again (1 part," in win.status.text(), win.status.text()


# == AC-4 While it is on ===================================================================
def test_tc40_freeze_pauses_and_resumes_one_part(win, console, caplog):  # TC-40
    c = player_and_enemy(win, console)
    run2 = console.game
    tick(win)
    money = win.ctl.live.trackers["obj2"]
    e = money.address
    saved_bytes = run2.fake.peek(fg.PID, e, 0x300)
    caplog.set_level(logging.INFO, logger="cheatmaker.core.freezer")
    run2.fake.poke(fg.PID, e, bytes(0x300))  # the enemy is freed
    run2.set_health(3.0)
    money._last_try = -10.0
    win.ctl.freezer.tick()
    assert run2.health() == 999.0, "the other part keeps being written"
    assert run2.value(e, "int64") == 0, "a lost part is never written"
    key = str(id(c))
    assert f"action=freeze key={key} part=money paused" in caplog.text
    win.ctl.freezer.tick()
    assert caplog.text.count("part=money paused") == 1, "logged once"
    run2.fake.poke(fg.PID, e, saved_bytes)  # back (e.g. after the biome loads)
    run2.set_value(e, 5, "int64")
    for _ in range(3):
        money._last_try = -10.0
        win.ctl.freezer.tick()
    assert f"action=freeze key={key} part=money resumed" in caplog.text
    assert run2.value(money.address, "int64") == 77
    assert c.enabled


def test_tc41_hook_stays_while_another_cheat_on_the_object_is_on(win, console):  # TC-41
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    fixed_cheat(win)
    combine_picked(win)
    single = add_found_fraction(win)  # a single found-again cheat on the same object
    fake = console.game.fake
    tick(win, 0)
    tick(win, 1)
    assert single.enabled
    untick(win, 0)
    assert fake.peek(fg.PID, fg.READER, 1) == b"\xe9", "fraction still on: hook stays"
    untick(win, 1)
    assert fake.peek(fg.PID, fg.READER, 10) == fg.VMOVSS_R, "last one off: hook out"


def test_tc42_lost_connection_while_on(win, console):  # TC-42
    c = player_and_enemy(win, console)
    tick(win)
    console.game.fake.poke(fg.PID, win.ctl.live.trackers["obj2"].address, bytes(0x300))
    win.ctl.live.trackers["obj2"]._last_try = -10.0
    win.ctl.freezer.tick()
    assert win.ctl.live._part_paused.get(id(c))
    win.ctl.handle_lost()
    assert not c.enabled and win.ctl.live._part_paused == {}


# == AC-5 Notes ============================================================================
def test_tc50_format_3_opened_by_an_older_app_is_kept_as_bak(win, console, monkeypatch, caplog):
    # TC-50: the older app's rule (format_version > its own) on a real format 3 file
    connect(win)
    learn(win, two_moving(win, console.game))
    win.ctl.shutdown()
    path = pj.project_path(fg.TITLE, fg.VERSION)
    original = path.read_text(encoding="utf-8")
    assert json.loads(original)["format_version"] == 3
    monkeypatch.setattr(pj, "FORMAT_VERSION", 2)  # "older" Cheat Maker
    win.ctl.cheat_file.cheats.clear()
    console.restart(2)
    connect(win)
    assert "notes for this game can't be read" in win.status.text()
    assert "made by a newer Cheat Maker (format 3)" in caplog.text
    assert win.ctl.project is None
    learn(win, add_health_cheat(win, console.game))  # saving notes: the old file is kept
    bak = path.with_name(path.name + ".bak")
    assert bak.read_text(encoding="utf-8") == original


def test_tc51_format_2_notes_come_back_the_same_and_are_saved_as_3(win, console):  # TC-51
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    win.ctl.shutdown()
    path = pj.project_path(fg.TITLE, fg.VERSION)
    doc = json.loads(path.read_text())
    doc["format_version"] = 2
    path.write_text(json.dumps(doc))
    before = pj.load(path)
    win.ctl.cheat_file.cheats.clear()
    console.restart(2)
    connect(win)
    [c] = win.ctl.cheat_file.cheats
    assert c.anchor == ("obj1", "Infinite Health") and not c.combined
    assert win.cheats.table.item(0, COL_KEEPS).text() == FOUND_AGAIN
    win.cheats.finder.save()
    doc3 = json.loads(path.read_text())
    assert doc3["format_version"] == 3
    after = pj.load(path)
    assert after.objects == before.objects and after.cheats == before.cheats
    assert "parts" not in doc3["cheats"][0], "a single cheat is saved as before"


@pytest.mark.parametrize(
    "part",
    [
        {"name": "x", "object": "obj1", "field": "Infinite Health", "value": 1e300},
        {"name": "x", "object": "nope", "field": "Infinite Health", "value": 1},
        {"name": "x", "object": "obj1", "field": "nope", "value": 1},
    ],
)
def test_tc52_bad_learned_part_leaves_only_that_cheat_out(win, console, part):  # TC-52
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    win.ctl.shutdown()
    path = pj.project_path(fg.TITLE, fg.VERSION)
    doc = json.loads(path.read_text())
    doc["cheats"].append({"name": "Bad", "action": "freeze", "parts": [part]})
    path.write_text(json.dumps(doc))
    win.ctl.cheat_file.cheats.clear()
    console.restart(2)
    connect(win)
    assert [c.name for c in win.ctl.cheat_file.cheats] == ["Infinite Health"]


def test_tc53_int_part_value_that_does_not_fit_is_skipped(win, console):  # TC-53
    connect(win)
    learn(win, two_moving(win, console.game))
    win.ctl.shutdown()
    path = pj.project_path(fg.TITLE, fg.VERSION)
    doc = json.loads(path.read_text())
    doc["cheats"][0]["parts"][1]["value"] = 2**40  # int32 field
    path.write_text(json.dumps(doc))
    win.ctl.cheat_file.cheats.clear()
    console.restart(2)
    connect(win)
    assert win.ctl.cheat_file.cheats == [], "left out, no crash"


def test_tc54_delete_one_combined_keeps_the_other(win, console):  # TC-54
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    fixed_cheat(win, "god")
    a = combine_picked(win)
    single = add_found_fraction(win)
    fixed_cheat(win, "flag", GOD_OFF + 4)
    win.cheats.table.selectAll()
    win.cheats.table.selectRow(1)
    # pick rows 1 and 2 (fraction + flag)
    from PySide6.QtCore import QItemSelectionModel

    sm = win.cheats.table.selectionModel()
    sm.select(
        win.cheats.table.model().index(2, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    win.cheats.combine_btn.click()
    assert single not in win.ctl.cheat_file.cheats
    assert [c.name for c in saved().cheats] == [a.name, "fraction + flag"]
    tick(win, 0)
    win.cheats.table.selectRow(0)
    win.cheats.delete_btn.click()
    assert not a.enabled
    assert [c.name for c in saved().cheats] == ["fraction + flag"]
    assert [o.id for o in saved().objects] == ["obj1"]


# == AC-6 Unchanged / exclusions ===========================================================
def test_tc60_combined_with_moving_or_learned_parts_never_in_cheat_files(win, console):
    # TC-60
    connect(win)
    learn(win, add_health_cheat(win, console.game))
    fixed_cheat(win, "god", GOD_OFF, b"\xab", b"\x00")
    learned_fixed = combine_picked(win)
    sess_c = session_part(win, "s", fg.HEAP + 0xA000, ValueType.INT32, 1)
    f2 = fixed_cheat(win, "f2", GOD_OFF + 8, b"\xcd", b"\x00")
    moving_fixed = combine([sess_c, f2], name="moving+fixed")
    a = fixed_cheat(win, "a", GOD_OFF + 16, b"\xef", b"\x00")
    b = fixed_cheat(win, "b", GOD_OFF + 17, b"\xef", b"\x00")
    all_fixed = combine([a, b], name="all fixed")
    cf = CheatFile("Returnal", fg.TITLE, fg.VERSION, [learned_fixed, moving_fixed, all_fixed])
    assert cf.saveable_cheats() == [all_fixed]
    only = CheatFile("Returnal", fg.TITLE, fg.VERSION, [all_fixed])
    for fmt in ("json", "shn"):
        out = formats.dumps(cf, fmt)
        assert out == formats.dumps(only, fmt), "none of their parts is written"
        assert b"all fixed" in out and learned_fixed.name.encode() not in out
    win.ctl.cheat_file.cheats[:] = [learned_fixed, moving_fixed, all_fixed]
    dlg = SaveDialog(win.ctl, win)
    text = dlg.skipped.text()
    assert '1 cheat(s) marked "This session only"' in text
    assert hints.SAVE_APP_ONLY.format(n=1) in text


def test_tc61_lock_it_and_make_it_work_off_for_combined(win, console):  # TC-61
    connect(win)
    c = two_moving(win, console.game)
    for case in (c, combine([fixed_cheat(win, "a"), fixed_cheat(win, "b", GOD_OFF + 1)])):
        win.ctl.cheat_file.cheats[:] = [case]
        win.ctl.cheats_changed.emit()
        win.cheats.table.selectRow(0)
        assert not win.cheats.lock_btn.isEnabled()
        assert not win.cheats.build_btn.isEnabled()
    learn(win, c)
    win.ctl.cheat_file.cheats[:] = [c]
    win.ctl.cheats_changed.emit()
    win.cheats.table.selectRow(0)
    assert c.found_again
    assert not win.cheats.lock_btn.isEnabled() and not win.cheats.build_btn.isEnabled()


def test_tc62_single_found_again_cheat_unchanged(win, console):  # TC-62
    connect(win)
    c = add_health_cheat(win, console.game)
    dlg = learn(win, c)
    assert not c.combined and c.anchor == ("obj1", "Infinite Health")
    assert not dlg.pc_off_btn.isHidden(), "PC-off still offered for a single cheat"
    [entry] = saved().cheats
    assert entry.parts == [] and entry.to_json() == {
        "name": "Infinite Health",
        "object": "obj1",
        "field": "Infinite Health",
        "action": "freeze",
        "value": 999.0,
    }
