import struct

import pytest

from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind, versions_differ
from cheatmaker.core.templates import BY_KEY, TEMPLATES, make_cheat
from cheatmaker.core.values import ValueType


def test_required_templates_exist():  # AC-5.1
    names = {t.name for t in TEMPLATES}
    assert {
        "Infinite Health",
        "Infinite Money",
        "Infinite Ammo",
        "Max Skill Points",
        "Set to a Number",
    } <= names


def test_make_cheat_infinite_money_defaults_high_and_freezes():  # AC-5.2
    c = make_cheat(
        BY_KEY["infinite_money"],
        LocationKind.SESSION_ADDRESS,
        0x2000_0080,
        ValueType.INT32,
        struct.pack("<i", 2450),
    )
    assert c.name == "Infinite Money" and c.freeze and c.value == 999_999
    assert c.locations[0].off == struct.pack("<i", 2450)
    assert c.session_only and not c.saveable


def test_make_cheat_clamps_to_type():
    c = make_cheat(
        BY_KEY["infinite_money"],
        LocationKind.MODULE_VALUE,
        0x10,
        ValueType.INT16,
        struct.pack("<h", 50),
    )
    assert c.value == 32767 and c.saveable


def test_make_cheat_health_keeps_current_and_custom_value():
    c = make_cheat(
        BY_KEY["infinite_health"],
        LocationKind.SESSION_ADDRESS,
        1,
        ValueType.FLOAT,
        struct.pack("<f", 87.5),
    )
    assert c.value == 87.5
    c2 = make_cheat(
        BY_KEY["set_number"],
        LocationKind.SESSION_ADDRESS,
        1,
        ValueType.INT32,
        struct.pack("<i", 1),
        value=42,
    )
    assert c2.value == 42 and not c2.freeze


def test_make_cheat_rejects_code_patch():
    with pytest.raises(ValueError):
        make_cheat(TEMPLATES[0], LocationKind.CODE_PATCH, 0, ValueType.INT32, b"\0" * 4)


def test_set_value_validates():
    c = make_cheat(BY_KEY["set_number"], LocationKind.SESSION_ADDRESS, 1, ValueType.INT8, b"\x01")
    c.set_value(100)
    assert c.value == 100
    with pytest.raises(ValueError):
        c.set_value(1000)
    lock = Cheat("x", [Location(LocationKind.CODE_PATCH, 0, b"\x90", b"\x01")])
    with pytest.raises(ValueError):
        lock.set_value(1)


def test_location_validation():
    with pytest.raises(ValueError):
        Location(LocationKind.CODE_PATCH, 0, b"\x90", b"\x01\x02")
    with pytest.raises(ValueError):
        Location(LocationKind.CODE_PATCH, -1, b"\x90", b"\x01")
    with pytest.raises(ValueError):
        Location(LocationKind.CODE_PATCH, 0, b"", b"")
    loc = Location(LocationKind.MODULE_VALUE, 0x10, b"\1", b"\0")
    assert loc.address(0x400000) == 0x400010
    assert Location(LocationKind.SESSION_ADDRESS, 0x99, b"\1", b"\0").address(0x400000) == 0x99


def test_file_name_ps5_and_ps4():  # AC-6.1
    assert (
        CheatFile("G", "ppsa01234", "01.000.000").file_name("json") == "PPSA01234_01.000.000.json"
    )
    cf = CheatFile("G", "CUSA05347", "01.00")
    assert cf.file_name("shn") == "CUSA05347_01.00.shn" and cf.is_ps4
    with pytest.raises(ValueError):
        cf.file_name("mc4")
    with pytest.raises(ValueError):
        CheatFile("G", "CUSA05347", "").file_name("json")


def test_versions_differ():  # AC-7.2
    assert not versions_differ("01.05", "1.05")
    assert versions_differ("01.05", "01.06")


def test_combine_cheats():
    from cheatmaker.core.cheats import Cheat, Location, LocationKind, combine

    a = Cheat("A", [Location(LocationKind.SESSION_ADDRESS, 0x10, b"\x01", b"\x00")], freeze=True)
    b = Cheat("B", [Location(LocationKind.CODE_PATCH, 0x20, b"\x90", b"\xcc")])
    c = combine([a, b])
    assert c.name == "A + B" and c.freeze and c.value is None and c.session_only
    assert [loc.offset for loc in c.locations] == [0x10, 0x20]
    assert c.locations[0] is not a.locations[0]  # copies: editing one won't change the other
    with pytest.raises(ValueError):
        combine([a])
    # Found-again cheats can be combined now (spec-restart-combined AC-1).
    found = combine([a, Cheat("C", b.locations, anchor=("o", "f"))])
    assert found.locations[1].anchor == ("o", "f") and found.anchor is None


# -- combined cheats that survive restarts (spec-restart-combined) ---------------------
def _session(name: str, addr: int, vt: ValueType = ValueType.FLOAT, v: float = 1.0) -> Cheat:
    raw = vt.encode(v)
    return Cheat(name, [Location(LocationKind.SESSION_ADDRESS, addr, raw, raw)], value_type=vt)


def _found(name: str, obj: str = "obj1", vt: ValueType = ValueType.FLOAT, v=3.0) -> Cheat:
    raw = vt.encode(v)
    return Cheat(
        name,
        [Location(LocationKind.SESSION_ADDRESS, 0, raw, raw)],
        freeze=True,
        value_type=vt,
        anchor=(obj, name),
    )


def test_combine_keeps_each_parts_type_and_kind():  # AC-1
    from cheatmaker.core.cheats import combine

    fixed = Cheat("god", [Location(LocationKind.MODULE_VALUE, 0x40, b"\x01", b"\x00")])
    patch = Cheat("nop", [Location(LocationKind.CODE_PATCH, 0x80, b"\x90", b"\xcc")])
    c = combine([_found("health"), _session("fraction", 0x1000), fixed, patch])
    learned, moving, mod, code = c.locations
    assert [p.part for p in c.locations] == ["health", "fraction", "god", "nop"]
    assert learned.anchor == ("obj1", "health") and learned.vtype is ValueType.FLOAT
    assert learned.offset == 0 and not learned.moving
    assert moving.moving and moving.vtype is ValueType.FLOAT and moving.offset == 0x1000
    assert mod.vtype is None and code.vtype is None  # no number type on these cheats
    assert c.combined and c.anchor is None and c.value_type is None and c.freeze
    assert c.learned_parts == [learned] and c.moving_parts == [moving]
    assert c.objects == ["obj1"] and not c.found_again and c.session_only and not c.saveable


def test_combined_found_again_when_nothing_moves():  # AC-1
    from cheatmaker.core.cheats import combine

    fixed = Cheat("god", [Location(LocationKind.MODULE_VALUE, 0x40, b"\x01", b"\x00")])
    c = combine([_found("health"), _found("fraction", "obj2"), fixed])
    assert c.found_again and c.moving_parts == [] and c.objects == ["obj1", "obj2"]
    assert not c.saveable, "never written to a cheat file"
    plain = combine(
        [fixed, Cheat("b", [Location(LocationKind.CODE_PATCH, 0x90, b"\x90", b"\x90")])]
    )
    assert not plain.found_again and plain.saveable and plain.moving_parts == []


def test_combine_part_names():  # Words: "health (1)", "health (2)"
    from cheatmaker.core.cheats import combine

    two = Cheat(
        "health",
        [
            Location(LocationKind.SESSION_ADDRESS, 0x10, b"\x01", b"\x00"),
            Location(LocationKind.SESSION_ADDRESS, 0x20, b"\x01", b"\x00"),
        ],
    )
    c = combine([two, _session("fraction", 0x30)])
    assert [p.part for p in c.locations] == ["health (1)", "health (2)", "fraction"]
    again = combine([c, _session("fraction", 0x40)])  # named parts keep their names
    assert [p.part for p in again.locations] == [
        "health (1)",
        "health (2)",
        "fraction",
        "fraction (2)",
    ]


def test_combine_value_type_only_where_the_bytes_are_the_value():
    from cheatmaker.core.cheats import combine

    code = Cheat(
        "x",
        [Location(LocationKind.SESSION_ADDRESS, 0x10, b"\x01\x02", b"\x00\x00")],
        value_type=ValueType.FLOAT,  # 2 bytes can't be a float
    )
    inside = Cheat(
        "y",
        [Location(LocationKind.SESSION_ADDRESS, 0x20, b"\x01\x02\x03\x04", bytes(4))],
        value_type=ValueType.INT32,
        value_at=[(0, 0), (0, 1), (0, 2), (0, 3)],
    )
    c = combine([code, inside])
    assert [p.vtype for p in c.locations] == [None, None]


def test_combine_up_to_four_values_that_move():  # AC-1
    from cheatmaker.core.cheats import combine

    four = [_found("a", "o1"), _found("b", "o2"), _session("c", 0x10), _session("d", 0x20)]
    fixed = Cheat("f", [Location(LocationKind.CODE_PATCH, 0x80, b"\x90", b"\xcc")])
    assert len(combine([*four, fixed]).locations) == 5, "fixed parts have no limit"
    with pytest.raises(ValueError, match=r"^Combine up to 4 values that move; these have 5\.$"):
        combine([*four, _session("e", 0x30)])


def test_combine_refuses_parts_that_share_a_byte():  # AC-1
    from cheatmaker.core.cheats import combine

    def fixed(name, off, n=4):
        return Cheat(name, [Location(LocationKind.MODULE_VALUE, off, bytes(n), bytes(n))])

    with pytest.raises(ValueError, match=r"^a and b overlap in memory\.$"):
        combine([fixed("a", 0x100), fixed("b", 0x103)])
    assert combine([fixed("a", 0x100), fixed("b", 0x104)])  # side by side is fine
    with pytest.raises(ValueError, match="health and fraction overlap"):
        combine([_session("health", 0x1000), _session("fraction", 0x1002)])
    assert combine([_session("health", 0x1000), fixed("fraction", 0x1000)]), "other spaces"
    with pytest.raises(ValueError, match="overlap"):  # the same learned value twice
        combine([_found("health"), _found("health")])
    where = {("obj1", "health"): 0x1000}

    def addr_of(loc):
        return where.get(loc.anchor)

    with pytest.raises(ValueError, match="health and fraction overlap"):
        combine([_found("health"), _session("fraction", 0x1003)], addr_of=addr_of)
    assert combine([_found("health"), _session("fraction", 0x1004)], addr_of=addr_of)
    assert combine([_found("health"), _session("fraction", 0x1000)]), "not found: can't tell"
