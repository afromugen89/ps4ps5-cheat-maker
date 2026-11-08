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
    with pytest.raises(ValueError):
        combine([a, Cheat("C", b.locations, anchor=("o", "f"))])
