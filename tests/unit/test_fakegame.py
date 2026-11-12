"""tests/fakegame.py's game: writers run on minicpu, hits can be queued, and code
writes can be dropped like on some consoles."""

from __future__ import annotations

import iced_x86 as ix
import pytest

from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.locker import patch_landed
from cheatmaker.core.values import ValueType
from tests import fakegame as fg


@pytest.mark.parametrize(
    ("form", "start", "new"),
    [("float", 100.0, 37.5), ("int32", 100, 61), ("int64", 100_000, 99_000)],
)
def test_hit_runs_the_games_writer(form, start, new):
    g = fg.build()
    assert g.value(g.player, form) == start
    cpu = g.hit(g.player, new, form)
    assert g.value(g.player, form) == new and g.last_cpu is cpu
    assert cpu.rip == fg.FORMS[form].end and cpu["rsp"] == fg.RSP
    assert g.value(g.enemies[0], form) != new, "only that object"


@pytest.mark.parametrize(("new", "dead"), [(1, False), (0, True), (-5, True)])
def test_sub_writer_leaves_flags_for_the_games_jle(new, dead):
    g = fg.build()
    cpu = g.hit(g.player, new, "int32")
    assert cpu.condition(ix.ConditionCode.LE) is dead
    assert fg.FORMS["int32"].kind == "sub" and cpu["ecx"] == 100 - new


def test_play_queues_one_hit_per_read():
    g = fg.build()
    g.play((g.player, 90.0, "float"), (g.player, 80.0, "float"))
    assert g.fake.read(fg.PID, g.health_addr, 4) and g.health() == 90.0
    g.fake.read(fg.PID, g.health_addr, 4)
    assert g.health() == 80.0 and not g.script
    g.fake.read(fg.PID, g.health_addr, 4)
    assert g.health() == 80.0


def test_drop_code_writes_is_noticed_by_patch_landed():
    g = fg.build()
    g.fake.drop_code_writes = True
    cheat = Cheat(
        "x", [Location(LocationKind.CODE_PATCH, fg.SUB_SITE - fg.BASE, b"\x90" * 6, fg.SUB_W)]
    )
    g.fake.write(fg.PID, fg.SUB_SITE, b"\x90" * 6)
    assert g.fake.peek(fg.PID, fg.SUB_SITE, 6) == fg.SUB_W, "acknowledged, nothing changed"
    assert g.fake.writes[-1] == (fg.PID, fg.SUB_SITE, b"\x90" * 6)
    assert not patch_landed(g.session, cheat)
    g.fake.write(fg.PID, g.health_addr, b"\0\0\0\0")
    assert g.health() == 0.0, "data writes still land"
    g.fake.drop_code_writes = False
    g.fake.write(fg.PID, fg.SUB_SITE, b"\x90" * 6)
    assert patch_landed(g.session, cheat)


# -- linked values (spec-linked-values): a second place, its copies, slow reads --------------
def test_linked_game_keeps_health_in_two_places_with_copies():
    g = fg.build(capture_ticks=False, linked=True)
    assert g.fraction() == 1.0 and g.fraction_addr == fg.FRAC_OBJ + fg.FRAC
    assert g.fake.peek(fg.PID, fg.FRAC_SITE, len(fg.VMOVSS_FRAC)) == fg.VMOVSS_FRAC
    g.damage(30.0)
    assert g.health() == 70.0 and g.fraction() == pytest.approx(0.7)
    g.fake.read(fg.PID, g.health_addr, 4)  # the game runs: copies worked out again
    pct = ValueType.FLOAT.decode(g.fake.peek(fg.PID, fg.PERCENT, 4))
    assert pct == pytest.approx(70.0)
    for addr in fg.HUD_COPIES:
        assert ValueType.FLOAT.decode(g.fake.peek(fg.PID, addr, 4)) == 70.0


def test_fraction_writer_is_one_the_builder_can_hook():
    from cheatmaker.core import builder as b
    from cheatmaker.core.cave import Effect
    from cheatmaker.core.locker import find_writers

    g = fg.build(capture_ticks=False, linked=True)
    writers = find_writers(g.session, g.fraction_addr, 4, 1.0)
    assert [w.address for w in writers] == [fg.FRAC_SITE]
    eligible, skipped = b.classify(writers, ValueType.FLOAT, Effect.NEVER_DOWN, False)
    assert skipped == [] and eligible[0].methods == (b.Method.HOOK,)


def test_without_linked_nothing_of_it_is_there():
    g = fg.build(capture_ticks=False)
    assert g.fake.peek(fg.PID, fg.FRAC_SITE, 8) == b"\xcc" * 8, "filler, no writer"
    assert g.fake.peek(fg.PID, fg.PERCENT, 4) == bytes(4)
    assert g.fraction_addr not in g.fake.write_hits


def test_slow_reads_advance_the_clock_per_read():
    g = fg.build(capture_ticks=False)
    now = [0.0]
    g.slow_reads(lambda s: now.__setitem__(0, now[0] + s))
    g.fake.read(fg.PID, g.health_addr, 4)
    g.fake.read(fg.PID, g.health_addr, 4)
    assert now[0] == pytest.approx(2 * fg.READ_LATENCY_S)
