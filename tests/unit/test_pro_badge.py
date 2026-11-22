"""Pro step 4: the CheatRunner / PC only badge and its reasons (spec-pro §5, AC-S2)."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.core import project as pj
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind, combine
from cheatmaker.core.pro.badge import (
    CHEATRUNNER,
    FOUND_AGAIN,
    FREEZE,
    HEAP_ADDRESS,
    NOTHING,
    PC_ONLY,
    POINTER_PATH,
    Badge,
    badge,
    pc_only,
    runner_cheats,
)
from cheatmaker.core.templates import BY_KEY, make_cheat
from cheatmaker.core.values import ValueType

RAW = struct.pack("<i", 100)


def _patch(name="nop", off=0x100) -> Cheat:
    return Cheat(name, [Location(LocationKind.CODE_PATCH, off, b"\x90\x90", b"\x89\x08")])


def _heap(template="set_number") -> Cheat:
    return make_cheat(
        BY_KEY[template], LocationKind.SESSION_ADDRESS, 0x2000_0040, ValueType.INT32, RAW
    )


def _static_freeze() -> Cheat:
    return make_cheat(
        BY_KEY["infinite_health"], LocationKind.MODULE_VALUE, 0x1010, ValueType.INT32, RAW
    )


def _found(obj="obj1", field="Health") -> Cheat:
    loc = Location(LocationKind.SESSION_ADDRESS, 0, RAW, RAW)
    return Cheat(field, [loc], value_type=ValueType.INT32, anchor=(obj, field))


def _project(*anchors) -> pj.Project:
    obj = pj.ObjectSpec("obj1", "you", None, [], list(anchors))
    return pj.Project("PPSA01234", "01.000", objects=[obj])


def test_code_patch_runs_on_cheatrunner():
    b = badge(_patch())
    assert b == Badge(CHEATRUNNER) and b.runs_on_console and b.reasons == ()
    assert "PC off" in b.tooltip


def test_static_value_without_freeze_runs_on_cheatrunner():
    c = make_cheat(
        BY_KEY["max_skill_points"], LocationKind.MODULE_VALUE, 0x1010, ValueType.INT32, RAW
    )
    assert badge(c).runs_on_console


def test_heap_address_is_pc_only():  # AC-S2
    b = badge(_heap())
    assert b.target == PC_ONLY and b.reasons == (HEAP_ADDRESS,)
    assert b.tooltip == "Needs Cheat Maker running: heap address."


def test_freeze_is_pc_only_even_in_the_module():  # AC-S2
    assert badge(_static_freeze()).reasons == (FREEZE,)
    assert badge(_heap("infinite_health")).reasons == (HEAP_ADDRESS, FREEZE)


def test_found_again_each_run():  # AC-S2
    assert badge(_found()).reasons == (FOUND_AGAIN,), "no heap address: it is found again"
    assert badge(_found(), _project(pj.StaticAnchor(0x40))).reasons == (FOUND_AGAIN,)
    chain = pj.ChainAnchor(0x50, (0x10, 0x2AC), "Health")
    mixed = _project(chain, pj.StaticAnchor(0x40))
    assert badge(_found(), mixed).reasons == (FOUND_AGAIN,)


def test_pointer_path_when_only_memory_paths_find_it():  # AC-S2
    chain = pj.ChainAnchor(0x50, (0x10, 0x2AC), "Health")
    assert badge(_found(), _project(chain)).reasons == (POINTER_PATH,)
    assert badge(_found(obj="gone"), _project(chain)).reasons == (FOUND_AGAIN,)
    assert badge(_found(), _project()).reasons == (FOUND_AGAIN,), "no anchors yet"


def test_combined_cheats():
    learned = combine([_found(field="a"), _found(field="b")])
    assert badge(learned).reasons == (FOUND_AGAIN,)
    moving = combine([_heap(), _patch()])
    assert badge(moving).reasons == (HEAP_ADDRESS,)
    fixed = combine([_patch("a", 0x100), _patch("b", 0x200)])
    assert badge(fixed).runs_on_console


def test_nothing_to_write():
    assert badge(Cheat("empty", [])).reasons == (NOTHING,)


@pytest.mark.parametrize(
    "make",
    [
        _patch,
        _heap,
        _static_freeze,
        _found,
        lambda: _heap("infinite_money"),
        lambda: Cheat("e", []),
    ],
)
def test_badge_follows_the_architecture_rule(make):
    """CheatRunner = saveable and not freeze and not found again (architecture-pro)."""
    c = make()
    assert badge(c).runs_on_console == (c.saveable and not c.freeze and not c.found_again)


def test_runner_and_pc_only_lists_keep_order():
    a, h, s, f = _patch("a"), _heap(), _static_freeze(), _found()
    assert runner_cheats([a, h, s, f]) == [a]
    assert [(c.name, b.reasons) for c, b in pc_only([a, h, s, f])] == [
        (h.name, (HEAP_ADDRESS,)),
        (s.name, (FREEZE,)),
        (f.name, (FOUND_AGAIN,)),
    ]


def test_export_still_holds_frozen_module_values():
    """Guided behaviour kept (spec-restart-combined TC-60): a frozen value in the module is
    written once by CheatRunner. Its badge says PC only; whether export should drop it is
    an open architecture question, so export is unchanged for now."""
    s = _static_freeze()
    cf = CheatFile("Game", "PPSA01234", "01.000", [_patch(), s, _heap()])
    assert cf.saveable_cheats() == [cf.cheats[0], s]
    assert not badge(s).runs_on_console
