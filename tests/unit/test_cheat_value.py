"""A cheat's value survives Save and Open, and stays editable (value inside game code too)."""

from __future__ import annotations

import json
import struct

import iced_x86 as ix
import pytest

from cheatmaker import formats
from cheatmaker.core import cave as cv
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind, value_positions
from cheatmaker.core.locker import Writer
from cheatmaker.core.values import ValueType
from cheatmaker.formats import goldhen_json, shn

VMOVSS = bytes.fromhex("C4C17A118C24AC020000")  # vmovss [r12+0x2AC], xmm1
WRITER = Writer(0x2B7B43F, VMOVSS, "vmovss [r12+2ACh],xmm1", 1)
CAVE = 0x65D7DB0


def _code(value: bytes) -> bytes:
    span = cv.decode_at(VMOVSS, WRITER.address)
    return cv.player_only_code(span, cv.writer_target(WRITER), 8, 0x40048, value, CAVE)


def _code_cheat(vtype: ValueType, value: float | int) -> Cheat:
    at = value_positions(lambda v: [_code(v), b"\xe9\0\0\0\0"], vtype.size)
    assert at is not None
    code = _code(vtype.encode(value))
    return Cheat(
        "Infinite Health",
        [
            Location(LocationKind.CODE_PATCH, CAVE - 0x400000, code, bytes(len(code))),
            Location(LocationKind.CODE_PATCH, 0x100, b"\xe9\1\2\3\4", VMOVSS[:5]),
        ],
        value_type=vtype,
        value_at=at,
    )


def _file(*cheats: Cheat) -> CheatFile:
    return CheatFile("Returnal", "PPSA01285", "03.000.005", list(cheats))


def _stores(code: bytes) -> list[str]:
    f = ix.Formatter(ix.FormatterSyntax.INTEL)
    return [f.format(i) for i in ix.Decoder(64, code, ip=CAVE) if i.mnemonic == ix.Mnemonic.MOV]


# -- where the value sits ------------------------------------------------------------
@pytest.mark.parametrize("vtype", [ValueType.FLOAT, ValueType.INT32, ValueType.INT64])
def test_value_positions_find_the_immediate_in_the_routine(vtype):
    at = value_positions(lambda v: [_code(v)], vtype.size)
    assert at is not None and len(at) == vtype.size
    code = _code(vtype.encode(300))
    assert vtype.decode(bytes(code[b] for _i, b in at)) == 300


def test_value_positions_give_up_when_the_shape_changes():
    assert value_positions(lambda v: [v * (1 + v[0] % 2)], 4) is None  # length moves
    assert value_positions(lambda v: [b"\0" * 8], 4) is None  # value not in it
    assert value_positions(lambda v: [v + v], 4) is None  # value twice: ambiguous

    def refuses(_v):
        raise ValueError("no")

    assert value_positions(refuses, 4) is None


# -- editing a value inside code -------------------------------------------------------
def test_value_inside_code_reads_and_edits_only_its_bytes():
    c = _code_cheat(ValueType.FLOAT, 300.0)
    cave_off, jmp = c.locations[0].off, c.locations[1].on
    assert c.value == 300.0
    c.set_value(999)
    assert c.value == 999.0
    assert _stores(c.locations[0].on) == ["mov dword ptr [r12+2ACh],4479C000h"]  # 999.0
    assert c.locations[0].on == _code(struct.pack("<f", 999.0)), "only the value changed"
    assert c.locations[0].off == cave_off and c.locations[1].on == jmp


def test_routine_left_in_place_when_off_keeps_off_in_step():
    c = _code_cheat(ValueType.INT32, 5)
    c.locations[0].off = c.locations[0].on  # the PC-off routine: off leaves it in place
    c.set_value(7)
    assert c.locations[0].off == c.locations[0].on and c.value == 7


def test_value_that_does_not_fit_is_refused():
    c = _code_cheat(ValueType.INT32, 5)
    with pytest.raises(ValueError):
        c.set_value(1 << 40)
    assert c.value == 5


def test_cheat_without_value_still_refuses_edit():
    c = Cheat("Lock", [Location(LocationKind.CODE_PATCH, 0x100, b"\x90\x90", b"\x89\x08")])
    assert c.value is None
    with pytest.raises(ValueError):
        c.set_value(1)


# -- Save then Open --------------------------------------------------------------------
def _plain() -> Cheat:
    loc = Location(LocationKind.MODULE_VALUE, 0x1234, struct.pack("<i", 99), bytes(4))
    return Cheat("Max Skill Points", [loc], value_type=ValueType.INT32)


@pytest.mark.parametrize("fmt", ["json", "shn"])
def test_value_survives_save_and_open_and_can_be_edited(fmt):
    data = formats.dumps(_file(_plain(), _code_cheat(ValueType.FLOAT, 300.0)), fmt)
    plain, code = formats.FORMATS[fmt].loads(data).cheats
    assert (plain.value_type, plain.value, plain.value_at) == (ValueType.INT32, 99, None)
    assert (code.value_type, code.value) == (ValueType.FLOAT, 300.0)
    plain.set_value(50)
    code.set_value(999)
    assert plain.locations[0].on == struct.pack("<i", 50)
    assert code.locations[0].on == _code(struct.pack("<f", 999.0))
    again = formats.FORMATS[fmt].loads(formats.dumps(_file(plain, code), fmt)).cheats
    assert [c.value for c in again] == [50, 999.0]


def test_json_keeps_goldhen_shape_and_adds_one_field():
    doc = json.loads(goldhen_json.dumps(_file(_plain())))
    mod = doc["mods"][0]
    assert set(mod) == {"name", "type", "memory", goldhen_json.VALUE_KEY}
    assert mod[goldhen_json.VALUE_KEY] == {"type": "int32"}
    lock = Cheat("Lock", [Location(LocationKind.CODE_PATCH, 0x100, b"\x90\x90", b"\x89\x08")])
    assert set(json.loads(goldhen_json.dumps(_file(lock)))["mods"][0]) == {
        "name",
        "type",
        "memory",
    }, "no value: nothing extra"


def test_shn_attributes_do_not_clash_with_cheatrunner_lookups():
    text = shn.dumps(_file(_code_cheat(ValueType.FLOAT, 1.0))).decode()
    header = next(line for line in text.splitlines() if "<Cheat " in line)
    assert header.index('Text="Infinite Health"') < header.index(shn.TYPE_ATTR)
    # CheatRunner finds attributes with strstr('<attr>="'): ours must not contain those
    for attr in ("Text", "Name", "CheatName", "Description"):
        assert header.count(f'{attr}="') == (1 if attr == "Text" else 0)


@pytest.mark.parametrize(
    "extra",
    [
        {"type": "huge"},
        {"type": 5},
        {"type": "float", "at": [[0, 1]]},  # too few bytes
        {"type": "float", "at": [[9, 0], [9, 1], [9, 2], [9, 3]]},  # no such line
        {"type": "float", "at": "nope"},
        {"type": "int16"},  # on bytes are 4 long
    ],
)
def test_broken_value_field_still_loads_without_a_value(extra):
    doc = json.loads(goldhen_json.dumps(_file(_plain())))
    doc["mods"][0][goldhen_json.VALUE_KEY] = extra
    (c,) = goldhen_json.loads(json.dumps(doc)).cheats
    assert c.value is None and c.locations[0].on == struct.pack("<i", 99)


def test_files_from_other_tools_load_as_before():
    doc = json.loads(goldhen_json.dumps(_file(_plain())))
    del doc["mods"][0][goldhen_json.VALUE_KEY]
    (c,) = goldhen_json.loads(json.dumps(doc)).cheats
    assert c.value is None


@pytest.mark.parametrize(
    ("vtype", "value"),
    [
        (ValueType.INT8, -5),
        (ValueType.INT16, 0x1234),
        (ValueType.DOUBLE, 123.456),
        (ValueType.INT64, 0x0102030405060708),
    ],
)
def test_value_positions_keep_byte_order_for_every_size(vtype, value):
    c = _code_cheat(vtype, value)
    assert c.value == value  # 8 bytes: two dword stores, low one first
    c.set_value(value // 2 if isinstance(value, int) else value / 2)
    assert c.locations[0].on == _code(vtype.encode(c.value))


def test_shn_value_attributes_with_quotes_and_ampersand_in_the_name():
    c = _code_cheat(ValueType.FLOAT, 2.5)
    c.name = 'Health "max" & more'
    (back,) = shn.loads(shn.dumps(_file(c))).cheats
    assert back.name == c.name and back.value == 2.5 and back.value_at == c.value_at
