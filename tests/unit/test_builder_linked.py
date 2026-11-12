"""Make it work with a pool (spec-linked-values L1 to L3, L16, L17) on the fake game.

The game is tests/fakegame.py with ``linked=True``: health (place 1) and its fraction in
another object, each with its own writer; a percent worked out from the fraction and two
HUD copies worked out from health. Time is the builder flow tests' fake clock, and with
``slow`` every console read costs 36 ms of it, as on a real PS5.
"""

from __future__ import annotations

import itertools
import logging

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import builder as b
from cheatmaker.core import linked as lk
from cheatmaker.core import real_check as rc
from cheatmaker.core.cave import Effect
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ReadFailed
from tests import fakegame as fg
from tests.unit.test_builder_flow import Clock

F = ValueType.FLOAT
TR = b.TryResult
V = rc.Verdict


def returnal_pool(g: fg.Game, **marks) -> lk.LinkedGroup:
    """The Find group of health, in group order (best first), as Find hands it over."""
    m = {"health": V.REAL, "fraction": V.KEPT, "percent": None, "copy": V.COPY} | marks
    return lk.LinkedGroup(
        (
            lk.PoolMember(g.health_addr, F, 1.0, True, m["health"]),
            lk.PoolMember(g.fraction_addr, F, 0.01, True, m["fraction"]),
            lk.PoolMember(fg.PERCENT, F, 1.0, False, m["percent"]),
            lk.PoolMember(fg.HUD_COPIES[0], F, 1.0, False, m["copy"]),
            lk.PoolMember(fg.HUD_COPIES[1], F, 1.0, False, m["copy"]),
        )
    )


def setup(effect=Effect.NEVER_DOWN, *, pool="returnal", slow=False, **kw):
    g = fg.build(capture_ticks=False, linked=True)
    g.fake.write_hits[g.health_addr] = [WatchHit(fg.WRITER + 10)] * 3
    clock = Clock()
    if slow:
        g.slow_reads(lambda s: setattr(clock, "now", clock.now + s))
    bl = b.Builder(
        g.session,
        g.health_addr,
        F,
        effect,
        name="Health",
        sleep=clock.sleep,
        clock=clock,
        pool=returnal_pool(g) if pool == "returnal" else pool,
        **kw,
    )
    return g, clock, bl


def fight(g: fg.Game, hits: int = 3, every: int = 20) -> list:
    """Hits spread over the 20 s test, the first after ``every`` ticks (a tick is at
    least 0.25 s)."""
    out: list = []
    for _ in range(hits):
        out += [None] * (every - 1) + [lambda: g.damage(10.0)]
    return out


def test_build_pool_rebases_on_place1_and_splits_copies(caplog):
    caplog.set_level(logging.INFO)
    _g, _clock, bl = setup()
    bl.watch()
    assert [m.address for m in bl.pool] == [bl.linked.members[1].address, fg.PERCENT]
    assert [m.factor for m in bl.pool] == pytest.approx([0.01, 1.0])
    assert [m.address for m in bl.copies] == list(fg.HUD_COPIES)
    assert "action=build_pool members=2 dropped=0 released=0 copies=2" in caplog.text


def test_members_the_game_moved_on_from_are_dropped_and_logged(caplog):
    caplog.set_level(logging.INFO)
    g, _clock, bl = setup()
    g.set_value(fg.FRAC_OBJ, 0.5, "fraction")  # since the search, the fraction changed
    g.fake.read(fg.PID, g.health_addr, 4)  # the game works out the percent again
    bl.watch()
    assert bl.pool == [] and bl.copies == [], "nothing to fix: copies aren't watched alone"
    assert "action=build_pool members=0 dropped=2 released=0 copies=0" in caplog.text
    assert f"action=build_pool_drop member={g.fraction_addr:#x} reason='{lk.NOT_LINKED}'" in (
        caplog.text
    )


def test_released_freezes_are_logged(caplog):
    caplog.set_level(logging.INFO)
    # The dialog releases the Find freezes on the pool in ``release`` (before Start's read).
    _g, _clock, bl = setup(release=lambda: setattr(bl, "pool_released", 3))
    bl.watch()
    assert "action=build_pool members=2 dropped=0 released=3" in caplog.text


def test_always_full_gives_each_member_its_own_full():
    _g, _clock, bl = setup(Effect.ALWAYS_FULL)
    bl.watch()
    assert bl.full == 100.0
    assert bl.pool_full[bl.pool[0].address] == pytest.approx(1.0, rel=0.005)
    assert bl.pool_full[fg.PERCENT] == pytest.approx(100.0)


def test_place1_not_in_the_group_means_no_pool(caplog):
    caplog.set_level(logging.INFO)
    _g, _clock, bl = setup()
    bl.linked = lk.LinkedGroup(bl.linked.members[1:])
    bl.watch()
    assert bl.pool == [] and "action=build_pool members=0 dropped=0" in caplog.text


# -- the pool watch during a test (L3) ---------------------------------------------------------
def _run_test(bl: b.Builder, clock: Clock, g: fg.Game) -> b.TestOutcome:
    bl.watch()
    clock.events.extend(fight(g))
    return bl.try_candidate(bl.next_candidate())


def _gaps(readings: list[lk.Reading], start: float, end: float) -> list[float]:
    ts = [start, *(r.t for r in readings), end]
    return [b - a for a, b in itertools.pairwise(ts)]


def test_every_member_is_read_at_least_once_a_second_under_latency(caplog):
    caplog.set_level(logging.INFO)
    g, clock, bl = setup(slow=True)
    out = _run_test(bl, clock, g)
    assert out.result is TR.PASS, out
    inst = bl._passed
    data = inst.pool
    assert data is not None and data.not_watched == []
    assert set(data.readings) == {m.key for m in (*bl.pool, *bl.copies)}
    t0 = data.end_t - b.TEST_S
    for key, rs in data.readings.items():
        assert max(_gaps(rs, t0, data.end_t)) <= lk.ONCE_A_SECOND_S, key
        spaced = _gaps(rs, rs[0].t, rs[-1].t)[1:-1]
        assert min(spaced) >= lk.POOL_EVERY_S - lk.POOL_EARLY_S, "not read more than needed"
    assert data.reads >= 3 * 20 and data.late == 0
    assert "action=build_linked_reads place=1 spans=3" in caplog.text
    assert "avg_ms=36 late=0" in caplog.text


def test_target_samples_stay_within_ten_percent_of_a_build_without_pool():
    counts = {}
    for pool in ("returnal", None):
        g, clock, bl = setup(slow=True, pool=pool)
        assert _run_test(bl, clock, g).result is TR.PASS
        counts[pool] = len(bl._pending[1].samples)
    assert counts["returnal"] >= 0.9 * counts[None], counts


def test_readings_follow_place1_and_the_fraction_falls_behind():
    g, clock, bl = setup()
    assert _run_test(bl, clock, g).result is TR.PASS
    assert g.health() == 100.0 and g.fraction() == pytest.approx(0.7), "place 1 held"
    rs = bl._passed.pool.readings
    frac = rs[(g.fraction_addr, F)]
    assert {r.ref for r in frac} == {100.0}, "place 1's value of the same tick"
    assert frac[0].value == 1.0 and frac[-1].value == pytest.approx(0.7)
    assert rs[(fg.HUD_COPIES[0], F)][-1].value == 100.0, "the copies follow place 1"


def test_a_member_that_cant_be_read_any_more_is_not_lost(monkeypatch):
    g, clock, bl = setup()
    bl.watch()
    real_read = g.fake.read
    gone_at = clock.now + 5.0

    def read(pid, addr, size):
        if addr <= fg.PERCENT < addr + size and clock.now >= gone_at:
            raise ReadFailed("area unloaded")
        return real_read(pid, addr, size)

    monkeypatch.setattr(g.fake, "read", read)
    clock.events.extend(fight(g))
    out = bl.try_candidate(bl.next_candidate())
    assert out.result is TR.PASS, out
    pct = bl._passed.pool.readings[(fg.PERCENT, F)]
    assert pct[0].value == 100.0
    assert [r.value for r in pct if r.t >= gone_at] == [None, None], "not read after 2"
    assert all(r.value is not None for r in pct if r.t < gone_at)


def test_no_member_left_reads_exactly_as_without_a_pool(monkeypatch):
    def requests(pool) -> list[tuple[int, int]]:
        g, clock, bl = setup(pool=pool)
        g.set_value(fg.FRAC_OBJ, 0.5, "fraction")  # everyone in the pool is dropped
        bl.watch()
        seen: list[tuple[int, int]] = []
        real = g.fake.read
        monkeypatch.setattr(g.fake, "read", lambda p, a, n: (seen.append((a, n)), real(p, a, n))[1])
        clock.events.extend(fight(g))
        bl.try_candidate(bl.next_candidate())
        return seen

    assert requests("returnal") == requests(None)
