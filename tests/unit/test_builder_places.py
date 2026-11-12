"""Make it work with linked places (spec-linked-values L4 to L13) on the fake game.

The game is tests/fakegame.py with ``linked=True`` (health and its fraction in another
object, a percent worked out from the fraction, two HUD copies of health) and, with
``third=True``, a third place (twice health, in a third object). Time is the builder
flow tests' fake clock; its sleeps let the game act.

The rollback invariant of the flow tests holds for the whole build: every way a build
with kept places ends without a cheat leaves the module bytes as they were before
place 1's fix went in, with one 2 s wait for all of them on Cancel.
"""

from __future__ import annotations

import itertools
import logging
import struct
import threading
from dataclasses import replace

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import build_notes as notes
from cheatmaker.core import builder as b
from cheatmaker.core import cave as cv
from cheatmaker.core import linked as lk
from cheatmaker.core import real_check as rc
from cheatmaker.core.cave import Effect
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.values import ValueType
from cheatmaker.errors import BuildFailed, ConnectionLost, ReadFailed, ScanCancelled
from tests import fakegame as fg
from tests.unit.test_builder_flow import Clock, module
from tests.unit.test_builder_linked import fight

F = ValueType.FLOAT
I32 = ValueType.INT32
TR = b.TryResult
V = rc.Verdict


def pool_of(g: fg.Game, *, third: bool = False, **marks) -> lk.LinkedGroup:
    """Health's Find group as Find hands it over (group order, best first)."""
    m = {"fraction": V.KEPT, "percent": None, "copy": V.COPY, "third": V.KEPT} | marks
    members = [
        lk.PoolMember(g.health_addr, F, 1.0, True, V.REAL),
        lk.PoolMember(g.fraction_addr, F, 0.01, True, m["fraction"]),
        lk.PoolMember(fg.PERCENT, F, 1.0, False, m["percent"]),
        lk.PoolMember(fg.HUD_COPIES[0], F, 1.0, False, m["copy"]),
        lk.PoolMember(fg.HUD_COPIES[1], F, 1.0, False, m["copy"]),
    ]
    if third:
        members.append(lk.PoolMember(g.third_addr, F, 2.0, False, m["third"]))
    return lk.LinkedGroup(tuple(members))


def setup(effect=Effect.NEVER_DOWN, *, third=False, pool=None, **kw):
    g = fg.build(capture_ticks=False, linked=True, third=third)
    g.fake.write_hits[g.health_addr] = [WatchHit(fg.WRITER + 10)] * 3
    clock = Clock()
    bl = b.Builder(
        g.session,
        g.health_addr,
        F,
        effect,
        name="Health",
        sleep=clock.sleep,
        clock=clock,
        pool=pool(g) if pool else pool_of(g, third=third),
        **kw,
    )
    return g, clock, bl


def pass_place(bl: b.Builder, clock: Clock, g: fg.Game, progress=None) -> b.LinkedPlan:
    """The current place's watch and a passing test, judged."""
    bl.watch()
    clock.events.extend(fight(g))
    out = bl.try_candidate(bl.next_candidate(), progress=progress)
    assert out.result is TR.PASS, out
    return bl.after_pass()


def to_place(bl: b.Builder, clock: Clock, g: fg.Game, n: int) -> None:
    """Pass places 1 to n - 1 and start place n (its watch done)."""
    for _ in range(n - 1):
        plan = pass_place(bl, clock, g)
        assert not plan.done
        np = bl.next_place()
        assert np.place == bl.place, np
    bl.watch()


def sleeps(bl: b.Builder) -> list[float]:
    """Every wait the builder makes from now on."""
    seen: list[float] = []
    real = bl.sleep

    def sleep(s: float) -> None:
        seen.append(s)
        real(s)

    bl.sleep = sleep
    return seen


# -- L4 to L9: whole builds -------------------------------------------------------------
def test_never_goes_down_fixes_two_places_and_leaves_copies_alone(caplog):
    caplog.set_level(logging.INFO)
    g, clock, bl = setup()
    before = module(g)
    plan = pass_place(bl, clock, g)
    assert [m.address for m in plan.behind] == [g.fraction_addr, fg.PERCENT]
    np = bl.next_place()
    assert np == b.NextPlace(2, 2, bl.place_member) and np.member.address == g.fraction_addr
    assert bl.address == g.fraction_addr and bl.place == 2 and bl.has_kept
    assert bl.pool_rows[fg.PERCENT].done is lk.Done.COPY, "L4: the game put the percent back"
    assert bl.real_results[(fg.PERCENT, F)].verdict is V.COPY
    hints: list[str] = []
    plan = pass_place(bl, clock, g, progress=lambda _f, h: hints.append(h))
    assert hints[0] == "Testing fix 1 of 1 for place 2: keep playing and take a hit."
    assert plan.done
    res = bl.finalize()
    assert (res.places, res.linked, res.after_restart) == (2, "fixed", "Yes")
    cheat = res.cheat
    assert cheat.name == "Health: Never goes down" and cheat.places == 2 and cheat.enabled
    assert cheat.saveable and not cheat.combined and cheat.value is None
    assert b.full_fixed_in_places(replace(cheat, places=1)), "told from the lines on reload"
    offs = [loc.offset for loc in cheat.locations]
    assert offs[1] == fg.WRITER - fg.BASE and offs[3] == fg.FRAC_SITE - fg.BASE, "places in order"
    for loc in cheat.locations:
        assert g.fake.peek(fg.PID, fg.BASE + loc.offset, len(loc.on)) == loc.on
    assert notes.current_note() is None and notes.kept_notes() == []
    for _ in range(10):  # C22a: 10 hits, nothing lost
        g.damage(10.0)
    assert g.health() == 100.0 and g.fraction() == pytest.approx(0.7)
    hud = {addr for _pid, addr, _data in g.fake.writes} & set(fg.HUD_COPIES)
    assert not hud, "the copies are never written"
    rows = bl.pool_rows
    assert rows[g.fraction_addr].done is lk.Done.FIXED
    assert all(rows[a].done is lk.Done.NOTHING for a in fg.HUD_COPIES)
    assert module(g)[1:] == before[1:], "module data untouched"
    text = caplog.text
    pick = f"action=build_linked_pick place=2 member={g.fraction_addr:#x} mark=kept cluster=2"
    assert pick in text
    assert "action=build_linked_result place=2 result=pass" in text
    assert "places=2 linked=fixed" in text


def test_a_member_that_keeps_up_after_place_2_is_not_needed():
    g, clock, bl = setup(pool=lambda g: pool_of(g, percent=V.KEPT))
    pass_place(bl, clock, g)
    np = bl.next_place()
    assert np.member.address == g.fraction_addr, "likely real first"
    assert pass_place(bl, clock, g).done
    row = bl.pool_rows[fg.PERCENT]
    assert row.status is lk.Status.KEPT_UP
    assert (row.done, row.after_place) == (lk.Done.NOT_NEEDED, 2)


def test_always_full_scales_full_for_the_second_place():
    g, clock, bl = setup(Effect.ALWAYS_FULL)
    pass_place(bl, clock, g)
    bl.next_place()
    assert bl.full == pytest.approx(1.0, rel=0.005)
    assert pass_place(bl, clock, g).done
    res = bl.finalize()
    g.damage(30.0)
    assert g.health() == 100.0 and g.fraction() == pytest.approx(1.0, rel=0.005)
    assert res.cheat.value is None and res.cheat.places == 2
    assert b.full_fixed_in_places(res.cheat)
    reloaded = replace(res.cheat, places=1)  # places isn't saved in cheat files
    assert b.full_fixed_in_places(reloaded), "told from the lines"


def test_full_fixed_in_places_is_a_make_it_work_result_with_two_routines():
    code = bytes([0x90] * 5)
    routine = Location(LocationKind.CODE_PATCH, 0x100, code, code)
    jump = Location(LocationKind.CODE_PATCH, 0x200, code, bytes(5))
    two = Cheat("Health: Always full", [routine, jump, replace(routine, offset=0x300), jump])
    assert b.full_fixed_in_places(two), "a loaded file keeps only the name"
    assert b.full_fixed_in_places(replace(two, name="Ammo: Half damage (only me)"))
    assert b.full_fixed_in_places(replace(two, name="Mine", hint="Never goes down.")), "hint"
    assert not b.full_fixed_in_places(replace(two, locations=[routine, jump])), "one place"
    assert not b.full_fixed_in_places(replace(two, name="Mine")), "not Make it work"
    assert not b.full_fixed_in_places(replace(two, name="Mine", hint="Found again each run."))
    held = replace(two, value_type=ValueType.INT32, value_at=[(0, 0), (0, 1), (0, 2), (0, 3)])
    assert held.value is not None
    assert not b.full_fixed_in_places(held), "Full can be typed"
    assert b.full_fixed_in_places(replace(two, name="Mine", places=2)), "in memory: places"


def test_half_damage_halves_both_places():
    g, clock, bl = setup(Effect.HALF)
    pass_place(bl, clock, g)
    bl.next_place()
    assert pass_place(bl, clock, g).done
    bl.finalize()
    h, f = g.health(), g.fraction()
    g.damage(10.0)
    assert g.health() == pytest.approx(h - 5.0) and g.fraction() == pytest.approx(f - 0.05)


def test_three_places():
    g, clock, bl = setup(third=True)
    to_place(bl, clock, g, 3)
    assert bl.address == g.third_addr and bl.places_total == 3
    assert pass_place(bl, clock, g).done
    res = bl.finalize()
    assert res.places == 3 and len(res.cheat.locations) == 6
    third = g.value(fg.THIRD_OBJ, "third")
    g.damage(10.0)
    assert g.value(fg.THIRD_OBJ, "third") == third and g.health() == 100.0


def test_nothing_fixable_left_after_the_check_finishes_with_one_place(caplog):
    caplog.set_level(logging.INFO)
    g, clock, bl = setup(pool=lambda g: pool_of(g, fraction=V.COPY))
    plan = pass_place(bl, clock, g)
    assert [m.address for m in plan.behind] == [fg.PERCENT], "copies are never fixed (L4)"
    np = bl.next_place()
    assert np.finish and bl.place == 1
    res = bl.finalize()
    assert (res.places, res.linked) == (1, "none") and len(res.cheat.locations) == 2
    assert res.cheat.name == "Health: Never goes down"
    assert bl.pool_rows[fg.PERCENT].done is lk.Done.COPY


# -- L6: same code, limits (L10), a place that can't be fixed --------------------------------
def test_a_writer_that_is_an_earlier_fix_is_skipped_as_the_same_code():
    g, clock, bl = setup()
    pass_place(bl, clock, g)
    bl.next_place()
    bl._fixed_sites.add(fg.FRAC_SITE)  # as if place 1's fix were this instruction
    with pytest.raises(b.PlaceFailed, match="None of the safe fixes"):
        bl.watch()
    assert [s.reason for s in bl.skipped] == [b.SAME_CODE]


def test_nothing_changed_at_place_2_is_place_failed():
    g, clock, bl = setup()
    pass_place(bl, clock, g)
    bl.next_place()
    g.fake.write_hits[g.fraction_addr] = []
    with pytest.raises(b.PlaceFailed, match="Nothing changed it"):
        bl.watch()
    assert bl.has_kept, "place 1 stays in for the fallback"


def test_an_unsupported_type_goes_to_the_fallback():
    g, clock, bl = setup()
    pass_place(bl, clock, g)
    # As if Find had listed the fraction as an 8-byte decimal, which fixes can't patch.
    bl._behind = [lk.PoolMember(g.fraction_addr, ValueType.DOUBLE, 0.01, True, V.KEPT)]
    np = bl.next_place()
    assert np.fallback_reason == cv.UNSUPPORTED_TYPE and bl.place == 2


FOURTH = fg.HEAP + 0xC800  # a fourth place: three times health, lowered by each hit


def test_limit_places_never_adds_a_place(caplog):
    """MAX_PLACES (3) are fixed and a fourth value still falls: it isn't held by a
    freeze (that would be a fourth place); the result is the three code fixes,
    linked=partial, and the member is listed as not fixed with the reason."""
    caplog.set_level(logging.INFO)
    g, clock, bl = setup(third=True)
    g.fake.poke(fg.PID, FOURTH, struct.pack("<f", 300.0))
    four = lk.PoolMember(FOURTH, F, 3.0, False, V.KEPT)
    bl.linked = lk.LinkedGroup((*bl.linked.members, four))

    def hit() -> None:
        g.damage(10.0)
        g.fake.poke(
            fg.PID, FOURTH, struct.pack("<f", F.decode(g.fake.peek(fg.PID, FOURTH, 4)) - 30)
        )

    def place() -> b.LinkedPlan:
        bl.watch()
        clock.events.extend(([None] * 19 + [hit]) * 3)
        assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
        return bl.after_pass()

    for n in (2, 3):
        assert not place().done
        assert bl.next_place().place == n
    assert [m.address for m in place().behind] == [FOURTH], "the fourth still falls"
    np = bl.next_place()
    assert np.fallback_reason == b.LIMIT
    res = bl.fallback(reason=np.fallback_reason)
    assert res.places == 3 <= lk.MAX_PLACES and res.linked == "partial" and res.reason == b.LIMIT
    assert not res.cheat.freeze and not res.cheat.combined and len(res.cheat.locations) == 6
    row = bl.pool_rows[FOURTH]
    assert (row.done, row.reason) == (lk.Done.NOT_FIXED, b.LIMIT)
    lines = [
        r.getMessage() for r in caplog.records if "action=build_linked_result" in r.getMessage()
    ]
    assert [ln.split()[1] for ln in lines] == ["place=2", "place=3", "place=4"], lines
    assert "place=4 result=limit" in lines[-1]
    assert "places=3 linked=partial" in caplog.text


def test_limit_tests(monkeypatch):
    monkeypatch.setattr(lk, "MAX_TESTS", 2)
    g, clock, bl = setup()
    to_place(bl, clock, g, 2)
    out = bl.try_candidate(bl.next_candidate())  # nobody hits: test 2
    assert out.result is TR.NO_HIT and bl.tests_total == 2
    assert bl.next_candidate() is None and bl.limit_hit, "test 3 is refused"
    res = bl.fallback()  # within MAX_PLACES: the place is held
    assert (res.linked, res.places, res.reason) == ("freeze", 2, b.LIMIT) and res.cheat.freeze


def test_limit_fixes_per_linked_place(monkeypatch):
    limits: list[int] = []
    real = b.candidates
    monkeypatch.setattr(
        b, "candidates", lambda e, limit=6: (limits.append(limit), real(e, limit))[1]
    )
    g, clock, bl = setup()
    to_place(bl, clock, g, 2)
    assert limits == [b.MAX_CANDIDATES, lk.MAX_LINKED_FIXES] and lk.MAX_LINKED_FIXES == 4


# -- L12: putting back every place ---------------------------------------------------------
def test_kept_place_note_rooms_and_slot(caplog):
    caplog.set_level(logging.INFO)
    open_room = (0x3000, 0x3100)  # an open cheat's code line
    g, clock, bl = setup(extra_reserved=[open_room])
    pass_place(bl, clock, g)
    first = bl._passed
    bl.next_place()
    [kept] = notes.kept_notes()
    assert kept.place == 1 and notes.current_note() is None
    assert kept.patches[0].new_hex != first.sites[0][2].hex(), "the re-aimed jump is noted"
    assert bl._fixed_sites == {fg.WRITER}
    bl.watch()
    inst = bl._prepare(bl.next_candidate(), 1)
    base = fg.BASE
    mine = [(a - base, a - base + s) for a, s in inst.rooms]
    for lo, hi in [*bl._kept_reserved, open_room]:
        assert all(hi <= a or b_ <= lo for a, b_ in mine), (mine, lo, hi)
    assert inst.slot != first.slot and inst.place == 2
    assert bl._note(inst).place == 2


@pytest.mark.parametrize("places", [2, 3])
@pytest.mark.parametrize("path", ["fail", "cancel", "close", "close thread"])
def test_every_way_out_puts_every_place_back(places, path):
    g, clock, bl = setup(third=places == 3)
    before = module(g)
    to_place(bl, clock, g, places)
    assert module(g) != before
    cancel = threading.Event()
    waits = sleeps(bl)
    want = {"fail": TR.FAIL, "cancel": TR.CANCELLED, "close": TR.CANCELLED}
    if path == "fail":  # another code path lowers it: the fix didn't hold
        obj, form = (fg.FRAC_OBJ, "fraction") if places == 2 else (fg.THIRD_OBJ, "third")
        clock.events.extend([None, lambda: g.set_value(obj, 0.01, form)])
    elif path == "cancel":
        clock.events.extend([None, None, cancel.set])
    elif path == "close":
        clock.events.extend([None, lambda: bl.close()])
    if path == "close thread":
        out = _close_on_another_thread(bl, g)
    else:
        out = bl.try_candidate(bl.next_candidate(), cancel)
        assert out.result is want[path], out
    if path == "fail":
        assert out.rollback is b.Rollback.RESTORED and bl.has_kept, "the kept places stay in"
        assert len(notes.kept_notes()) == places - 1
        assert bl.close() is b.Rollback.RESTORED
    else:
        assert out.rollback is b.Rollback.RESTORED and not bl.has_kept
    if path in ("cancel", "close"):
        assert waits.count(b.PUT_BACK_WAIT_S) == 1, "one wait for every place"
    assert module(g) == before, path
    assert notes.current_note() is None and notes.kept_notes() == []
    assert bl.close() is b.Rollback.NOTHING


def _close_on_another_thread(bl: b.Builder, g: fg.Game) -> b.TestOutcome:
    inside, go = threading.Event(), threading.Event()
    real = g.fake.write
    n = [0]

    def write(pid, addr, data):
        real(pid, addr, data)
        n[0] += 1
        if n[0] == 1:  # the routine is in, the jump not yet
            inside.set()
            go.wait(5)

    g.fake.write = write  # type: ignore[method-assign]
    out: dict = {}
    worker = threading.Thread(
        target=lambda: out.setdefault("v", bl.try_candidate(bl.next_candidate())), daemon=True
    )
    worker.start()
    assert inside.wait(5)
    closer = threading.Thread(target=lambda: out.setdefault("rb", bl.close()), daemon=True)
    closer.start()
    closer.join(0.3)
    assert closer.is_alive(), "close waits for the writes in progress"
    go.set()
    closer.join(5)
    worker.join(5)
    g.fake.write = real  # type: ignore[method-assign]
    assert out["rb"] is b.Rollback.RESTORED
    return out["v"]


def test_cancel_between_steps_puts_every_place_back():
    g, clock, bl = setup()
    before = module(g)
    pass_place(bl, clock, g)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        bl.next_place(cancel)  # Cancel landed as the next step started
    assert module(g) == before and notes.kept_notes() == [] and notes.current_note() is None
    g2, clock2, bl2 = setup()
    pass_place(bl2, clock2, g2)
    bl2.next_place()
    with pytest.raises(ScanCancelled):
        bl2.watch(cancel)  # place 2's watch sees Cancel
    assert not bl2.has_kept and notes.kept_notes() == []


def test_game_gone_blames_only_the_fix_under_test():
    g, clock, bl = setup()
    to_place(bl, clock, g, 2)
    clock.events.extend([None, lambda: g.fake.remove_process(fg.PID)])
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.LOST and out.hint.startswith("The game stopped with fix 1.")
    assert notes.is_crashed(fg.TITLE, fg.VERSION, fg.FRAC_SITE - fg.BASE, "hook")
    assert not notes.is_crashed(fg.TITLE, fg.VERSION, fg.WRITER - fg.BASE, "hook")
    assert notes.kept_notes() == [] and notes.current_note() is None and not bl.has_kept


def test_lost_link_notes_place_2_and_the_next_connect_blames_only_it():
    g, clock, bl = setup()
    to_place(bl, clock, g, 2)
    clock.events.extend([None, g.fake.disconnect])
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.LOST and out.rollback is b.Rollback.NOT_RESTORED
    note = notes.current_note()
    assert note is not None and note.place == 2 and note.site_off == fg.FRAC_SITE - fg.BASE
    assert [k.place for k in notes.kept_notes()] == [1]
    assert bl.close() is b.Rollback.NOT_RESTORED, "nothing more is tried"
    g.session.game = None  # the game restarted: the next connect finds no such process
    hint = notes.check_on_connect(g.session, sleep=lambda _s: None)
    assert hint.startswith("Fix 1 crashed the game last time")
    assert notes.is_crashed(fg.TITLE, fg.VERSION, fg.FRAC_SITE - fg.BASE, "hook")
    assert not notes.is_crashed(fg.TITLE, fg.VERSION, fg.WRITER - fg.BASE, "hook")
    assert notes.kept_notes() == []


def test_a_put_back_not_taken_leaves_the_kept_places_alone():
    g, clock, bl = setup()
    to_place(bl, clock, g, 2)
    clock.events.extend([lambda: setattr(g.fake, "fail_writes", True)])
    out = bl.try_candidate(bl.next_candidate())
    assert out.rollback is b.Rollback.NOT_RESTORED and bl.not_restored
    g.fake.fail_writes = False
    with pytest.raises(BuildFailed, match="Restart the game"):
        bl.next_place()
    writes = len(g.fake.writes)
    assert bl.close() is b.Rollback.NOT_RESTORED
    assert len(g.fake.writes) == writes, "nothing more is written"
    assert bl.has_kept and [k.place for k in notes.kept_notes()] == [1]


# -- L7: Only me per place ------------------------------------------------------------------
def _hp32_pool(g: fg.Game) -> lk.LinkedGroup:
    """Health (decimal) and the whole-number health in the same object (another field)."""
    return lk.LinkedGroup(
        (
            lk.PoolMember(g.health_addr, F, 1.0, True, V.REAL),
            lk.PoolMember(g.player + fg.HP32, I32, 1.0, True, V.KEPT),
        )
    )


def _both_hit(g: fg.Game, enemies: bool = True):
    def hit() -> None:
        for obj in (g.player, *(g.enemies if enemies else ())):
            g.hit(obj, float(g.value(obj)) - 10.0)
            g.hit(obj, int(g.value(obj, "int32")) - 10, "int32")

    return hit


def _only_me_place1(g, clock, bl, *, by_field=False) -> None:
    bl.watch()
    if by_field:  # the player told apart by its ident (Only me by a field)
        bl.filter, bl.by_class = cv.Filter("field", field=8, field_value=0x40048), False
        bl._player_obj, bl._other_objs = g.player, list(g.enemies)
        bl.others = [e + fg.HP for e in g.enemies]
    else:
        clock.events.extend(
            [
                lambda o=o: g.hit(o, float(g.value(o)))
                for _ in range(3)
                for o in (g.player, *g.enemies)
            ]
        )
        assert bl.collect() is None and bl.by_class
        clock.events.clear()
    clock.events.extend([None] * 19 + [_both_hit(g)])
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.PASS, out


@pytest.mark.parametrize("by_field", [False, True])
def test_only_me_reuses_the_filter_on_the_same_object(by_field, monkeypatch):
    g = fg.build(capture_ticks=False)
    g.fake.write_hits[g.health_addr] = [WatchHit(fg.WRITER + 10)] * 3
    g.fake.write_hits[g.player + fg.HP32] = [WatchHit(fg.SUB_SITE + 6)] * 3
    clock = Clock()
    bl = b.Builder(
        g.session, g.health_addr, F, Effect.NEVER_DOWN, only_me=True, name="Health",
        sleep=clock.sleep, clock=clock, pool=_hp32_pool(g),
    )  # fmt: skip
    _only_me_place1(g, clock, bl, by_field=by_field)
    assert not bl.after_pass().done
    assert bl.next_place().member.vtype is I32
    bl.watch()
    monkeypatch.setattr(bl, "collect", lambda *_a: pytest.fail("no second Collect"))
    assert bl.reuse_filter() and bl.filter == bl._kept[0].filter
    others = bl._kept[0].other_objs
    assert others and bl.others == [e + fg.HP32 for e in others], "the enemies check, this place"
    clock.events.extend([None] * 19 + [_both_hit(g)])
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.PASS, out  # enemies went down: no question
    res = bl.finalize()
    assert res.cheat.name == "Health: Never goes down (only me)" and res.places == 2
    assert res.after_restart == ("Check after restart" if by_field else "Yes")
    hp32 = g.value(g.player, "int32")
    _both_hit(g)()
    assert g.health() == 100.0 and g.value(g.player, "int32") == hp32
    assert all(g.value(e, "int32") < 250 for e in g.enemies), "enemies still take damage"


ENEMY_FRACS = (fg.HEAP + 0xB800, fg.HEAP + 0xBC00)


def _frac_only_me():
    g, clock, bl = setup(only_me=True)
    g.fake.poke(fg.PID, fg.FRAC_OBJ, struct.pack("<Q", fg.PLAYER_VT))
    for e in ENEMY_FRACS:
        obj = bytearray(0x100)
        obj[0:8] = struct.pack("<Q", fg.ENEMY_VT)
        obj[fg.FRAC : fg.FRAC + 4] = struct.pack("<f", 1.0)
        g.fake.poke(fg.PID, e, bytes(obj))
    bl.watch()
    bl.filter, bl.by_class = cv.Filter("vtable", vtable=fg.PLAYER_VT), True
    bl._player_obj, bl._other_objs = g.player, []
    clock.events.extend(fight(g))
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.NEEDS_ANSWER
    assert bl.answer(b.Question.ENEMIES, b.Answer.YES).result is TR.PASS
    assert not bl.after_pass().done
    bl.next_place()
    bl.watch()
    assert not bl.reuse_filter(), "another object: Collect runs for this place"
    return g, clock, bl


def test_only_me_on_another_object_collects_once_for_that_place():
    g, clock, bl = _frac_only_me()
    objs = (fg.FRAC_OBJ, *ENEMY_FRACS)
    clock.events.extend(
        [lambda o=o: g.hit(o, g.value(o, "fraction"), "fraction") for _ in range(3) for o in objs]
    )
    seen: list[int] = []
    clock.events.appendleft(lambda: seen.append(notes.current_note().place))
    assert bl.collect() is None
    assert seen == [2], "Collect's note names its place"
    assert bl.filter == cv.Filter("vtable", vtable=fg.PLAYER_VT) and bl.by_class
    assert bl.others and set(bl.others) <= {e + fg.FRAC for e in ENEMY_FRACS}


def test_only_me_that_cant_tell_apart_at_place_2_is_place_failed():
    g, clock, bl = _frac_only_me()
    seen_alone = [lambda: g.hit(fg.FRAC_OBJ, g.fraction(), "fraction")] * 3
    clock.events.extend(seen_alone)
    assert bl.collect() == b.ONLY_YOU_SEEN
    clock.events.extend(seen_alone)
    with pytest.raises(b.PlaceFailed, match="Couldn't tell you apart"):
        bl.collect()
    assert bl.has_kept


# -- L11: the fallback -------------------------------------------------------------------
def _place2_fails(effect=Effect.NEVER_DOWN, **kw):
    g, clock, bl = setup(effect, **kw)
    pass_place(bl, clock, g)
    bl.next_place()
    g.fake.write_hits[g.fraction_addr] = []
    with pytest.raises(b.PlaceFailed):
        bl.watch()
    return g, clock, bl


@pytest.mark.parametrize("effect", [Effect.NEVER_DOWN, Effect.ALWAYS_FULL])
def test_fallback_holds_the_place_by_a_freeze(effect, caplog):
    caplog.set_level(logging.INFO)
    g, _clock, bl = _place2_fails(effect)
    live = bl.live = LiveCheats(g.session, Freezer(g.session))
    try:
        res = bl.fallback()
        cheat = res.cheat
        assert (res.linked, res.places, res.after_restart) == ("freeze", 2, b.SESSION_ONLY)
        assert cheat.name == f"Health: {effect.value}" and cheat.combined and cheat.freeze
        assert cheat.session_only and not cheat.saveable and cheat.enabled
        parts = [loc.part for loc in cheat.locations]
        assert parts == ["Health", "Health (2)", "Health (place 2)"]
        hold = cheat.locations[-1]
        assert hold.kind is LocationKind.SESSION_ADDRESS and hold.offset == g.fraction_addr
        want = 1.0 if effect is Effect.ALWAYS_FULL else 0.7
        assert F.decode(hold.on) == pytest.approx(want, rel=0.005) and hold.vtype is F
        assert str(id(cheat)) in live.freezer.keys, "the freeze runs"
        assert notes.current_note() is None and notes.kept_notes() == [] and not bl.has_kept
        assert bl.pool_rows[g.fraction_addr].done is lk.Done.FROZEN
        assert bl.close() is b.Rollback.NOTHING, "the result stays"
        assert "action=build_linked_result place=2 result=fallback_freeze" in caplog.text
        assert "places=2 linked=freeze" in caplog.text
    finally:
        live.freezer.stop()


def test_half_damage_fallback_keeps_the_passed_fix_only():
    g, _clock, bl = _place2_fails(Effect.HALF)
    res = bl.fallback()
    h = g.health()
    assert (res.linked, res.places, res.after_restart) == ("partial", 1, "Yes")
    assert not res.cheat.combined and not res.cheat.freeze and len(res.cheat.locations) == 2
    assert res.cheat.name == "Health: Half damage"
    assert bl.pool_rows[g.fraction_addr].done is lk.Done.NOT_FIXED
    g.damage(10.0)
    assert g.health() == h - 5.0, "place 1 still takes half"


def test_fallback_after_cancel_puts_everything_back():
    g, clock, bl = setup()
    before = module(g)
    pass_place(bl, clock, g)
    bl.next_place()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        bl.fallback(cancel)
    assert module(g) == before and notes.kept_notes() == []


# -- later places' tests: the baseline and the reference read (review) -----------------------
def test_a_hit_before_the_first_scheduled_read_still_counts_at_place_2():
    """The pool is read once as place 2's test starts: a member that falls in the very
    first tick, before any scheduled read, is still judged behind."""
    g, clock, bl = setup(third=True)
    to_place(bl, clock, g, 2)
    before = g.value(fg.THIRD_OBJ, "third")
    clock.events.extend([lambda: g.damage(10.0)])  # the first tick, then nothing
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    first = bl._passed.pool.readings[(g.third_addr, F)][0]
    assert first.value == before and first.ref == 100.0, "read as the test started"
    plan = bl.after_pass()
    assert [m.address for m in plan.behind] == [g.third_addr]


def _far_pool(g: fg.Game) -> lk.LinkedGroup:
    """Health, its fraction and 15 more members 4 KiB apart: 16 spans plus place 1's
    read of its own at place 2."""
    members = [
        lk.PoolMember(g.health_addr, F, 1.0, True, V.REAL),
        lk.PoolMember(g.fraction_addr, F, 0.01, True, V.KEPT),
    ]
    for i in range(1, 16):  # page 0 holds the player: place 1 gets a read of its own
        addr = fg.HEAP + i * 0x1000 + 0xF00  # free bytes in every heap page
        g.fake.poke(fg.PID, addr, struct.pack("<f", 100.0 * i))
        members.append(lk.PoolMember(addr, F, float(i), False, V.KEPT))
    return lk.LinkedGroup(tuple(members))


def _samples_without_pool(latency: float) -> int:
    g, clock, bl = setup(pool=lambda g: None)
    g.slow_reads(lambda s: setattr(clock, "now", clock.now + s), latency)
    bl.watch()
    clock.events.extend(fight(g))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    return len(bl._pending[1].samples)


@pytest.mark.parametrize("latency", [0.036, 0.05, 0.06, 0.08])
def test_a_slow_console_keeps_the_value_sampled_at_place_2(latency):
    """B5 comes first (L3): with 16 spans, the baseline and place 1's own read, the
    place's value is read at least 90% as often as in a build without a pool; at 80 ms
    the build still fixes both places."""
    g, clock, bl = setup(pool=_far_pool)
    g.slow_reads(lambda s: setattr(clock, "now", clock.now + s), latency)
    to_place(bl, clock, g, 2)
    clock.events.extend(fight(g))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    data = bl._passed.pool
    assert data.spans == 16 and (g.health_addr, F) not in data.readings, "place 1 alone"
    with_pool = len(bl._pending[1].samples)
    without = _samples_without_pool(latency)
    assert with_pool >= 0.9 * without, (with_pool, without)
    assert bl.after_pass().done
    res = bl.finalize()
    assert (res.places, res.linked) == (2, "fixed")


def test_a_link_lost_in_the_closing_pool_reads_is_the_lost_path(monkeypatch):
    """The slow console's last pool reads come after the value's sampling: a lost link
    there ends the test as Lost, with the fix put back."""
    g, clock, bl = setup(pool=_far_pool)
    g.slow_reads(lambda s: setattr(clock, "now", clock.now + s), 0.08)
    before = module(g)

    def lost(self, ref):
        raise ConnectionLost("no answer")

    monkeypatch.setattr(b._PoolWatch, "stale", lost)
    bl.watch()
    clock.events.extend(fight(g))
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.LOST and out.rollback is b.Rollback.RESTORED, out
    assert module(g) == before


def test_place_2_reads_place_1_on_top_of_the_members(monkeypatch):
    """Place 1 is more than 4 KiB from every member, so it has a read of its own: that
    read doesn't take a member's turn. With no time for pool reads at all (only the
    one-member floor) and 36 ms reads, every member is still read at least once a second."""
    monkeypatch.setattr(lk, "POOL_SAMPLE_SHARE", -1.0)  # nothing fits: the floor only
    g, clock, bl = setup(
        pool=lambda g: lk.LinkedGroup(
            (
                lk.PoolMember(g.health_addr, F, 1.0, True, V.REAL),
                lk.PoolMember(g.fraction_addr, F, 0.01, True, V.KEPT),
                lk.PoolMember(fg.PERCENT, F, 1.0, False, V.KEPT),
            )
        )
    )
    g.slow_reads(lambda s: setattr(clock, "now", clock.now + s))
    to_place(bl, clock, g, 2)
    assert abs(fg.PERCENT - g.health_addr) > 0x1000
    clock.events.extend(fight(g))
    assert bl.try_candidate(bl.next_candidate()).result is TR.PASS
    data = bl._passed.pool
    rs = data.readings[(fg.PERCENT, F)]
    t0 = data.end_t - b.TEST_S
    ts = [t0, *(r.t for r in rs), data.end_t]
    assert max(b_ - a for a, b_ in itertools.pairwise(ts)) <= lk.ONCE_A_SECOND_S
    assert all(r.ref == 100.0 for r in rs) and data.late == 0


# -- a linked place that can't be read (review) -------------------------------------------------
def test_a_place_that_cant_be_read_at_its_watch_is_place_failed(monkeypatch):
    g, clock, bl = setup()
    pass_place(bl, clock, g)
    bl.next_place()
    real = g.fake.read

    def read(pid, addr, size):
        if addr == g.fraction_addr:
            raise ReadFailed("area unloaded")
        return real(pid, addr, size)

    monkeypatch.setattr(g.fake, "read", read)
    with pytest.raises(b.PlaceFailed, match="Couldn't watch it"):
        bl.watch()
    assert bl.has_kept and not bl.closed, "the fallback runs, not close()"
    res = bl.fallback()  # the hold can't be read either: the code fixes only
    assert (res.linked, res.places) == ("partial", 1) and not res.cheat.freeze
    assert bl.pool_rows[g.fraction_addr].done is lk.Done.NOT_FIXED
