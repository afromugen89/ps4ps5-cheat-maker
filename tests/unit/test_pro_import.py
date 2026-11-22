"""Pro step 10: import json/shn/mc4 into Pro (PR-14, AC-14.1)."""

from __future__ import annotations

import pytest

from cheatmaker import formats
from cheatmaker.core import cave as cv
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.core.pro import importer
from cheatmaker.core.pro.asm import CaveContext, FakeAssembler
from cheatmaker.core.pro.asm_text import CantConvert
from cheatmaker.core.pro.model import ProCheat

HOOK, CAVE = 0x100, 0x1F00
HOOK_OFF = bytes.fromhex("f30f114810")
# cmp dword [rax+8], 7 ; jne +7 ; mov dword [rax+0x10], 0 ; movss [rax+0x10], xmm1 ; jmp back
CAVE_CODE = (
    bytes.fromhex("837808 07")
    + bytes.fromhex("7507")
    + bytes.fromhex("c7401000000000")
    + HOOK_OFF
    + b"\xe9"
    + (HOOK + 5 - (CAVE + 4 + 2 + 7 + 5 + 5)).to_bytes(4, "little", signed=True)
)


def _file() -> CheatFile:
    cave = Location(LocationKind.CODE_PATCH, CAVE, CAVE_CODE, bytes(len(CAVE_CODE)))
    jump = Location(LocationKind.CODE_PATCH, HOOK, cv.jmp_patch(HOOK, CAVE, 5), HOOK_OFF)
    nop = Location(LocationKind.CODE_PATCH, 0x300, b"\x90" * 3, bytes.fromhex("894b08"))
    other = Location(LocationKind.CODE_PATCH, 0x10, b"\1", b"\0", section=2)
    return CheatFile(
        "Game",
        "PPSA01234",
        "01.000",
        [Cheat("God mode", [cave, jump, nop]), Cheat("Libc thing", [other])],
    )


def test_import_makes_bytes_entries_and_refuses_other_sections():
    result = importer.import_cheats(_file(), [ProCheat("p1", "Mine", [])])
    (pc,) = result.cheats
    assert pc.id == "p2" and pc.origin == "import" and pc.name == "God mode"
    assert [(e.kind, e.offset) for e in pc.entries] == [
        ("bytes", CAVE),
        ("bytes", HOOK),
        ("bytes", 0x300),
    ]
    assert pc.entries[0].on == CAVE_CODE and pc.entries[0].off == bytes(len(CAVE_CODE))
    assert result.refused == {"Libc thing": importer.OTHER_SECTION.format(section=2)}


def test_other_module_is_refused():
    loc = Location(LocationKind.CODE_PATCH, 0x10, b"\1", b"\0", module="libc.prx")
    result = importer.import_cheats(CheatFile("G", "PPSA01234", "1", [Cheat("x", [loc])]))
    assert result.refused == {"x": importer.OTHER_MODULE.format(module="libc.prx")}


@pytest.mark.parametrize("fmt", ["json", "shn", "mc4"])
def test_import_from_every_format(tmp_path, fmt):
    cf = _file()
    cf.cheats = cf.cheats[:1]
    path = tmp_path / f"PPSA01234_01.000.{fmt}"
    path.write_bytes(formats.dumps(cf, fmt))
    (pc,) = importer.import_path(path).cheats
    assert [e.on for e in pc.entries] == [loc.on for loc in cf.cheats[0].locations]


def test_caves_are_detected_and_shown_disassembled():
    pc = importer.import_cheats(_file()).cheats[0]
    assert [importer.is_cave(e) for e in pc.entries] == [True, False, False]
    text = importer.disassembly(pc.entries[0])
    assert text[0] == "cmp dword [rax+8], 7" and text[-1].startswith("jmp ")
    odd = pc.entries[0].__class__("bytes", 0x10, b"\0\0", b"\x0f\xff")
    assert importer.disassembly(odd)[-1].startswith("db ")


def test_convert_with_fake_assembler_keeps_bytes():
    pc = importer.import_cheats(_file()).cheats[0]
    assert importer.convert(pc, FakeAssembler()) == 1
    cave, nop = pc.entries
    assert cave.kind == "cave" and cave.offset == HOOK and cave.cave == CAVE
    assert cave.cave_bytes == CAVE_CODE and cave.off == HOOK_OFF
    assert nop.kind == "bytes"
    ctx = CaveContext(CAVE, HOOK, HOOK_OFF)
    assert FakeAssembler().assemble(cave.source, ctx).code == CAVE_CODE
    with pytest.raises(CantConvert, match="already"):
        importer.convert(pc, FakeAssembler())


@pytest.mark.needs_nasm
def test_convert_with_nasm_reassembles_the_same_bytes(nasm):
    """AC-14.1."""
    pc = importer.import_cheats(_file()).cheats[0]
    assert importer.convert(pc, nasm) == 1
    cave = pc.entries[0]
    assert "jne short L1" in cave.source and "jmp near return" in cave.source
    assert nasm.assemble(cave.source, CaveContext(CAVE, HOOK, HOOK_OFF)).code == CAVE_CODE


def test_undecodable_cave_is_refused_naming_the_offset():
    cf = _file()
    cave = cf.cheats[0].locations[0]
    cave.on = cave.on[:-3]  # the jmp back cut short
    cave.off = cave.off[:-3]
    pc = importer.import_cheats(cf).cheats[0]
    before = [e.to_json() for e in pc.entries]
    with pytest.raises(CantConvert, match=r"God mode: the bytes at \+0x12 \(e9 ee\)"):
        importer.convert(pc, FakeAssembler())
    assert [e.to_json() for e in pc.entries] == before


def test_convert_without_a_jump_changes_nothing():
    loc = Location(LocationKind.CODE_PATCH, CAVE, b"\x90\xc3", b"\0\0")
    pc = importer.import_cheats(CheatFile("G", "PPSA01234", "1", [Cheat("c", [loc])])).cheats[0]
    assert importer.convert(pc, FakeAssembler()) == 0
    assert pc.entries[0].kind == "bytes"


def test_convert_leaves_a_jump_to_real_code_as_bytes():
    """Review B1: the target patches game code (off 55 48 89 e5), it isn't a cave."""
    real = Location(
        LocationKind.CODE_PATCH, 0x3000, bytes.fromhex("9090c390"), bytes.fromhex("554889e5")
    )
    jump = Location(LocationKind.CODE_PATCH, HOOK, cv.jmp_patch(HOOK, 0x3000, 5), HOOK_OFF)
    cf = CheatFile("G", "PPSA01234", "1", [Cheat("j", [real, jump])])
    pc = importer.import_cheats(cf).cheats[0]
    assert importer.convert(pc, FakeAssembler()) == 0
    assert [(e.kind, e.off) for e in pc.entries] == [("bytes", real.off), ("bytes", HOOK_OFF)]
