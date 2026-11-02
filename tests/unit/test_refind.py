"""Find it next time / Find it again on the fake two-run game (AC-11, AC-12, AC-14.3)."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import refind as rf
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.project import ChainAnchor, Field, HookAnchor, StaticAnchor, VtableAnchor
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
    assert obj.field("Health").off == fg.HP
    assert obj.field("Health").range == (0, rf.CLASS_FLOAT_LIMIT), "class check: wide"
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
    f = Field("Health", 0, ValueType.FLOAT, rf.default_range(ValueType.FLOAT, 87.5))
    rf.widen(f, 99_999.0)
    assert f.range == (0, 99_999.0)


def test_class_range_lets_a_counter_grow():  # BUG-08
    assert rf.class_range(ValueType.FLOAT, 100.0) == (0, rf.CLASS_FLOAT_LIMIT)
    assert rf.class_range(ValueType.INT32, 2450) == (0, 2**31 - 1)
    assert rf.class_range(ValueType.INT32, -20) == (-200, 2**31 - 1)


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
    clock = Clock()
    live.trackers["obj1"] = rf.Tracker(run2.session, obj, out.address, clock=clock)
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
    assert fz.paused and run2.health() == 50.0, "re-finds at most once a second"
    clock.sleep(rf.TRACK_RETRY_S)
    fz.tick()
    assert not fz.paused and run2.health() == 999.0
    assert live.trackers["obj1"].field_address("Health") == run2.health_addr
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


# -- review fixes ------------------------------------------------------------------------
def test_learn_uses_only_hooks_the_registers_confirm():
    from cheatmaker.console.client import WatchHit

    game = fg.build()
    for d in (game.fake.read_hits, game.fake.write_hits):  # no registers in the hits
        d[game.health_addr] = [WatchHit(h.rip) for h in d[game.health_addr]]
    with pytest.raises(FindFailed, match="still works for this session"):
        _learn(game)


def test_learn_skips_short_instructions_but_keeps_the_class_search():  # review blocker
    from cheatmaker.console.client import WatchHit

    game = fg.build()
    short = fg.BASE + 0x300
    game.fake.poke(fg.PID, short, bytes.fromhex("894808c34883ec28"))  # +8; ret; sub
    addr = game.player + 8  # the 4-byte id field, written by the short instruction
    game.fake.write_hits[addr] = [WatchHit(short + 3, {"rax": game.player})]
    learned = rf.learn(
        game.session, addr, ValueType.INT32, "Id", "obj1", watch_seconds=1, prove_seconds=1
    )
    assert learned.methods == ["vtable_scan"] and learned.obj.hook is None
    assert "safe to hook" in learned.notes[0]
    assert game.fake.peek(fg.PID, short, 4) == bytes.fromhex("894808c3"), "nothing patched"


def test_enemies_through_an_unfiltered_hook_never_ask_while_the_player_verifies():
    """Filter "none": the slot names whoever passed last (enemies in a fight). While the
    player's object still verifies the slot is not asked, so no "Is this it?" each hit."""
    game = fg.build(2)
    for o in (game.player, *game.enemies):
        game.fake.poke(fg.PID, o, bytes(8))
    game.touched = [game.player]
    obj = _learn(game, collect_seconds=0.25).obj
    assert obj.hook.filter.kind == "none"
    ah.install(game.session, obj)
    tracker = rf.Tracker(game.session, obj, game.player, clock=Clock())
    for enemy in game.enemies:  # a fight: each enemy goes through the hooked code
        game.touched = [enemy]
        game.fake.read(fg.PID, fg.BASE, 1)
        assert ah.read_slot(game.session, obj.hook)[0] == enemy
        assert tracker.current() == game.player and tracker.pending is None


def test_weak_object_that_moves_waits_for_the_player():  # review M4
    game = fg.build(2)
    for o in (game.player, *game.enemies):  # no class pointer: verify is weak
        game.fake.poke(fg.PID, o, bytes(8))
    game.touched = [game.player]
    obj = _learn(game, collect_seconds=0.25).obj
    assert obj.vtable is None and obj.hook.filter.kind == "none"
    ah.install(game.session, obj)
    clock = Clock()
    tracker = rf.Tracker(game.session, obj, game.player, clock=clock)
    fz = Freezer(game.session)
    live = LiveCheats(game.session, fz)
    live.trackers["obj1"] = tracker
    live.turn_on(_anchored_cheat())
    assert game.health() == 999.0
    # new area: the player's old memory is freed (no longer plausible) and the game's
    # code now handles another object (here an enemy): never followed by itself
    old, enemy = game.player, game.enemies[0]
    game.touched = [enemy]
    game.set_health(10.0, enemy)
    game.fake.poke(fg.PID, old + fg.HP, b"\xff" * 4)  # NaN: freed memory
    fz.tick()  # the freezer's own write would make it look fine again
    writes = len(game.fake.writes)
    for _ in range(3):
        fz.tick()
        clock.sleep(rf.TRACK_RETRY_S)
    assert len(game.fake.writes) == writes, "nothing written while unsure"
    assert tracker.pending == enemy and not tracker.confirmed and fz.paused
    tracker.reject_pending()
    assert tracker.address is None and tracker.pending is None
    fz.tick()
    assert len(game.fake.writes) == writes
    tracker.address, tracker.pending = old, enemy  # asked again; this time "yes"
    tracker.accept_pending()
    fz.tick()
    assert tracker.confirmed and tracker.address == enemy
    assert struct.unpack("<f", game.fake.peek(fg.PID, enemy + fg.HP, 4))[0] == 999.0
