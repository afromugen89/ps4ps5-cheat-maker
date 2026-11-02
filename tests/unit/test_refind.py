"""Find it next time / Find it again on the fake two-run game (AC-11, AC-12, AC-14.3)."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import refind as rf
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.project import ChainAnchor, HookAnchor, StaticAnchor, VtableAnchor
from cheatmaker.core.values import ValueType
from cheatmaker.errors import FindFailed
from tests import fakegame as fg
from tests.conftest import STATIC_ADDR, build_fake


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _learn(game: fg.Game, **kw) -> rf.Learned:
    return rf.learn(
        game.session,
        game.health_addr,
        ValueType.FLOAT,
        "Health",
        "obj1",
        watch_seconds=1,
        prove_seconds=1,
        **kw,
    )


def _again(game: fg.Game, obj, **kw) -> rf.Outcome:
    c = Clock()
    return rf.find_again(game.session, obj, sleep=c.sleep, clock=c, **kw)


# -- learn -------------------------------------------------------------------------------
def test_learn_moving_value_records_hook_and_vtable():  # AC-11.1
    run1 = fg.build(1)
    learned = _learn(run1)
    obj = learned.obj
    assert learned.methods == ["hook_slot", "vtable_scan"] and not learned.notes
    assert obj.vtable == fg.PLAYER_VT - fg.BASE
    assert obj.field("Health").off == fg.HP and obj.field("Health").range == (0, 10_000)
    hook = obj.hook
    assert isinstance(hook, HookAnchor) and hook.role == "reader"
    assert isinstance(obj.anchors[-1], VtableAnchor)
    assert (fg.BASE + hook.slot, 8, "write", 1) in run1.fake.watched, "slot proven"
    assert run1.fake.peek(fg.PID, fg.READER, 10) == fg.VMOVSS_R, "learning patches nothing"


def test_learn_static_value_needs_nothing_else():
    fake = build_fake()
    from cheatmaker.core.session import Session

    s = Session(lambda ip, port: fake)
    s.connect("1.2.3.4")
    s.refresh_game()
    learned = rf.learn(s, STATIC_ADDR, ValueType.INT32, "Skill points", "obj1")
    assert learned.methods == ["static"] and fake.watched == []
    assert learned.obj.anchors == [StaticAnchor(STATIC_ADDR - s.game.module_base)]
    out = rf.find_again(s, learned.obj)
    assert out.found and out.method == "static" and out.address == STATIC_ADDR


def test_learn_nothing_holds_says_so():  # AC-11.3
    game = fg.build()
    game.fake.read_hits.clear()
    game.fake.write_hits.clear()
    with pytest.raises(FindFailed, match="still works for this session"):
        _learn(game)


def test_learn_slot_in_use_still_keeps_the_vtable():
    game = fg.build()
    from cheatmaker.console.client import WatchHit
    from cheatmaker.core import cave as cv

    game.fake.write_hits[cv.find_slot(game.session)] = [WatchHit(fg.BASE + 0x500)]
    learned = _learn(game)
    assert learned.methods == ["vtable_scan"] and "spare memory" in learned.notes[0]


def test_learn_without_vtable_uses_the_player_only_field(monkeypatch):
    game = fg.build()
    for o in (game.player, *game.enemies):  # objects without a class pointer
        game.fake.poke(fg.PID, o, bytes(8))
    seen = [game.player, *game.enemies]  # what the Player-only collect would record
    monkeypatch.setattr(rf.cv, "collect_objects", lambda s, w, secs, cancel=None: seen)
    learned = _learn(game)
    assert learned.obj.vtable is None and learned.methods == ["hook_slot"]
    f = learned.obj.hook.filter
    assert (f.kind, f.off, f.value) == ("field", 8, 0x40048)


# -- find again ----------------------------------------------------------------------------
def test_restart_found_by_the_game_code_within_10_s():  # AC-12.1, C10c
    obj = _learn(fg.build(1)).obj
    run2 = fg.build(2)
    assert ah.install(run2.session, obj) == "installed"
    out = _again(run2, obj)
    assert out.found and out.method == "hook_slot" and out.address == run2.player
    assert out.seconds <= rf.SLOT_WAIT_S


def test_slot_not_filled_falls_back_to_vtable_scan():  # C10e
    obj = _learn(fg.build(1)).obj
    run2 = fg.build(2)
    out = _again(run2, obj, use_hook=False)
    assert out.found and out.method == "vtable_scan" and out.address == run2.player
    run3 = fg.build(3)  # hook installed but the game never runs it: wait, then scan
    run3.touched = []
    ah.install(run3.session, obj)
    out = _again(run3, obj)
    assert out.found and out.method == "vtable_scan" and out.seconds >= rf.SLOT_WAIT_S


def test_two_objects_of_the_same_class_disagree():  # AC-12.3
    obj = _learn(fg.build(1)).obj
    run2 = fg.build(2)
    twin = fg.HEAP + 0x9000
    run2.fake.poke(fg.PID, twin, run2.fake.peek(fg.PID, run2.player, 0x300))
    out = _again(run2, obj, use_hook=False)
    assert out.status == "disagree" and {f.address for f in out.options} == {run2.player, twin}
    picked = rf.confirm_pick(obj, twin)
    assert picked.found and picked.address == twin


def test_everything_fails():  # AC-12.4
    obj = _learn(fg.build(1)).obj
    run2 = fg.build(2)
    run2.fake.poke(fg.PID, run2.player, bytes(0x300))  # no player object at all
    out = _again(run2, obj)
    assert out.status == "failed" and out.address is None


def test_chain_proven_on_first_restart_makes_the_hook_unneeded():
    learned = _learn(fg.build(1, chain=True), pointer_scan=True)
    obj = learned.obj
    assert "chain" in learned.methods and obj.anchors_of(ChainAnchor)
    assert not obj.has_proven_chain
    run2 = fg.build(2, chain=True)  # hook not installed: the chain finds it
    out = _again(run2, obj, use_scan=False, slot_wait=0)
    assert out.found and out.method == "chain" and out.address == run2.player
    assert out.changed and obj.has_proven_chain, "classified: short chain"
    run3 = fg.build(3, chain=True)
    out = _again(run3, obj, use_scan=False, slot_wait=0)
    assert out.found and out.method == "chain"


def test_proven_chain_and_slot_disagree_writes_nothing():  # AC-12.3
    learned = _learn(fg.build(1, chain=True), pointer_scan=True)
    obj = learned.obj
    for a in obj.anchors_of(ChainAnchor):
        a.runs_seen = 2
    run2 = fg.build(2, chain=True)
    twin = fg.HEAP + 0x9000
    run2.fake.poke(fg.PID, twin, run2.fake.peek(fg.PID, run2.player, 0x300))
    run2.touched = [twin]  # the hooked code saw another object of the player's class
    ah.install(run2.session, obj)
    out = _again(run2, obj)
    assert out.status == "disagree" and {f.method for f in out.options} == {"chain", "hook_slot"}


def test_verify_rules():
    run1 = fg.build(1)
    obj = _learn(run1).obj
    s = run1.session
    assert rf.verify(s, obj, run1.player) is None
    assert rf.verify(s, obj, fg.BASE + 0x100) == "not in the game's writable memory"
    assert rf.verify(s, obj, 0x10) == "not in the game's writable memory"
    assert rf.verify(s, obj, run1.enemies[0]) == "different kind of object"
    assert rf.verify(s, obj, run1.player + 2) == "not an object address"
    run1.set_health(float("nan"))
    assert rf.verify(s, obj, run1.player) == "Health doesn't look right"
    run1.set_health(-5.0)
    assert rf.verify(s, obj, run1.player) == "Health doesn't look right"


def test_default_range_and_widen():
    assert rf.default_range(ValueType.FLOAT, 87.5) == (0, 10_000)
    assert rf.default_range(ValueType.INT32, 2450) == (0, 24_500)
    assert rf.default_range(ValueType.INT8, 100) == (0, 127)
    obj = _learn(fg.build(1)).obj
    f = obj.field("Health")
    rf.widen(f, 99_999.0)
    assert f.range == (0, 99_999.0)


# -- freeze follows the object (AC-12.5, C10f) -------------------------------------------------
def _anchored_cheat(value: float = 999.0) -> Cheat:
    raw = struct.pack("<f", value)
    return Cheat(
        "Infinite Health",
        [Location(LocationKind.SESSION_ADDRESS, 0, raw, raw)],
        freeze=True,
        value_type=ValueType.FLOAT,
        anchor=("obj1", "Health"),
    )


def test_freeze_pauses_on_a_new_biome_and_resumes_at_the_new_object():
    obj = _learn(fg.build(1)).obj
    run2 = fg.build(2)
    ah.install(run2.session, obj)
    out = _again(run2, obj)
    fz = Freezer(run2.session)
    live = LiveCheats(run2.session, fz)
    live.trackers["obj1"] = rf.Tracker(run2.session, obj, out.address)
    cheat = _anchored_cheat()
    live.turn_on(cheat)
    assert run2.health() == 999.0
    run2.set_health(10.0)
    fz.tick()
    assert run2.health() == 999.0
    # new biome: the slot still names the freed object until the game code runs again
    old = run2.player
    run2.touched = []
    run2.move_player(fg.HEAP + 0x6000)
    run2.fake.poke(fg.PID, old, bytes(0x300))
    writes = len(run2.fake.writes)
    fz.tick()
    assert fz.paused and len(run2.fake.writes) == writes, "nothing written while unsure"
    assert run2.fake.peek(fg.PID, old + fg.HP, 4) == bytes(4)
    run2.touched = [run2.player]  # the game's code runs on the new object
    run2.set_health(50.0)
    fz.tick()
    assert not fz.paused and run2.health() == 999.0
    assert cheat.locations[0].offset == run2.health_addr
    live.turn_off(cheat)


def test_anchored_cheat_is_never_written_blind():
    obj = _learn(fg.build(1)).obj
    run2 = fg.build(2)
    live = LiveCheats(run2.session, Freezer(run2.session))
    cheat = _anchored_cheat()
    with pytest.raises(FindFailed, match="nothing was changed"):
        live.turn_on(cheat)  # no tracker yet
    live.trackers["obj1"] = rf.Tracker(run2.session, obj)  # slot empty, hook not installed
    with pytest.raises(FindFailed):
        live.set_once(cheat)
    assert run2.fake.writes == []
    live.turn_off(cheat)  # nothing to restore, no error
    assert run2.fake.writes == []


def test_remember_history():
    from cheatmaker.core.project import Project

    p = Project("PPSA01285", "03.000.005")
    rf.remember(p, rf.Outcome("found", 1, "hook_slot", 0.4))
    rf.remember(p, rf.Outcome("disagree"))
    rf.remember(p, rf.Outcome("failed", seconds=3))
    assert [(h.method, h.verified) for h in p.history] == [("hook_slot", True), ("none", False)]
