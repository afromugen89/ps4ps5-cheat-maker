"""Pro step 9: per-entry AOB signatures (AC-9.1) and Bring them over (AC-9.2)."""

from __future__ import annotations

import struct

import iced_x86 as ix
import pytest

from cheatmaker.core import carry as cr
from cheatmaker.core import project as pj
from cheatmaker.core import updater as up
from cheatmaker.core.pro import refpoints as rp
from cheatmaker.core.pro.asm import FakeAssembler
from cheatmaker.core.pro.model import ProCheat, ProEntry, Variable
from cheatmaker.core.values import ValueType

CODE = 0x2000  # last 0x1000 bytes spare zeros
HOOK = 0x100
# movss [rax+0x10],xmm1 ; mov ecx,1 ; add rax,rbx ; mov [rbx+8],ecx ; ret
SITE_CODE = bytes.fromhex("f30f114810b9010000004801d8894b08c3")
NOP_SITE = 0x300
NOP_CODE = bytes.fromhex("894b08") + bytes.fromhex("8b4310") + bytes.fromhex("4883c308")
SHIFT = 0x40


def _image(shift: int = 0, twice: bool = False) -> up.CodeImage:
    code = bytearray(b"\xcc" * CODE)
    code[0x1000:] = bytes(0x1000)
    code[HOOK + shift : HOOK + shift + len(SITE_CODE)] = SITE_CODE
    code[NOP_SITE + shift : NOP_SITE + shift + len(NOP_CODE)] = NOP_CODE
    if twice:  # the same code (and what follows it) somewhere else too
        code[0x800 : 0x800 + len(SITE_CODE)] = SITE_CODE
    return up.CodeImage([(0, bytes(code))])


def _cheat(source: str = "original\njmp return\n", cave: int = 0x1F00) -> ProCheat:
    asm = FakeAssembler()
    off = SITE_CODE[:5]
    entry = ProEntry("cave", HOOK, off, b"\x90" * 5, source=source, cave=cave)
    pc = ProCheat("p1", "Health", [entry, ProEntry("nop", NOP_SITE, NOP_CODE[:3])])
    pc.variables.append(Variable("mult", ValueType.FLOAT, 2.0))
    from cheatmaker.core.pro import compile as pro_compile

    result = pro_compile.assemble(pc, entry, asm)
    assert result.ok, result.error
    entry.cave_bytes = result.code
    entry.on = b"\xe9" + struct.pack("<i", cave - (HOOK + 5))
    pro_compile.record_layout(pc, entry)
    return pc


# -- AC-9.1 -------------------------------------------------------------------------------------
def test_learn_gives_each_entry_a_unique_aob():
    pc = _cheat()
    image = _image()
    status = rp.learn([pc], image)
    assert status == {("p1", 0): rp.OK, ("p1", 1): rp.OK}
    for e in pc.entries:
        sig = up.Signature.from_text(e.aob)
        assert up.SIG_MIN <= len(sig) <= up.SIG_MAX
        assert image.find(sig, 2) == [e.offset]


def test_learn_works_with_the_cheat_on():
    pc = _cheat()
    entry = pc.entries[0]
    live = _image().overlay([(HOOK, entry.on), (entry.cave, entry.cave_bytes)])
    rp.learn([pc], live)
    sig = up.Signature.from_text(entry.aob)
    assert sig.data.startswith(SITE_CODE[:5])
    assert _image().find(sig, 2) == [HOOK]


def test_code_found_twice_is_not_unique():
    pc = _cheat()
    status = rp.learn([pc], _image(twice=True))
    assert status[("p1", 0)] == rp.NOT_UNIQUE and pc.entries[0].aob == ""
    assert rp.needs_learning([pc])


def test_a_working_aob_is_kept_and_outside_code_is_reported():
    pc = _cheat()
    mine = "F3 0F 11 48 10 B9 01 00 00 00 48 01 D8 89 4B 08 C3"
    pc.entries[0].aob = mine
    pc.entries.append(ProEntry("bytes", CODE + 0x10, b"\0", b"\1"))
    status = rp.learn([pc], _image())
    assert pc.entries[0].aob == mine and status[("p1", 2)] == rp.NOT_CODE


def test_copy_aobs():
    pc, learned = _cheat(), _cheat()
    rp.learn([learned], _image())
    assert rp.copy_aobs([pc], [learned]) == 2
    assert [e.aob for e in pc.entries] == [e.aob for e in learned.entries]
    assert rp.copy_aobs([pc], [learned]) == 0


# -- AC-9.2 -------------------------------------------------------------------------------------
def test_bring_over_refinds_and_reassembles():
    pc = _cheat()
    rp.learn([pc], _image())
    (new,), report = rp.bring_over([pc], _image(SHIFT), FakeAssembler())
    assert report.moved == ["Health"] and report.failed == {} and report.guessed == []
    cave, nop = new.entries
    assert (cave.offset, nop.offset) == (HOOK + SHIFT, NOP_SITE + SHIFT)
    assert cave.cave is not None and cave.cave >= 0x1000
    insns = list(ix.Decoder(64, cave.cave_bytes, ip=cave.cave))
    assert insns[1].near_branch_target == cave.offset + 5, "jmp return: the new return"
    assert struct.unpack_from("<i", cave.on, 1)[0] == cave.cave - (cave.offset + 5)
    assert cave.cave_bytes[-4:] == struct.pack("<f", 2.0)
    assert nop.on == b"\x90" * 3
    assert pc.entries[0].offset == HOOK, "the old cheat is only read"
    for e in new.entries:  # signatures for the new version
        assert _image(SHIFT).find(up.Signature.from_text(e.aob), 2) == [e.offset]


def test_without_aob_found_by_original_bytes_is_guessed():
    pc = _cheat()
    pc.entries = [ProEntry("bytes", NOP_SITE, NOP_CODE, b"\x90" * len(NOP_CODE))]
    (new,), report = rp.bring_over([pc], _image(SHIFT), FakeAssembler())
    assert report.guessed == ["Health"] and new.entries[0].offset == NOP_SITE + SHIFT


def test_cant_find_is_listed():
    pc = _cheat()
    rp.learn([pc], _image())
    gone = up.CodeImage([(0, b"\xcc" * 0x1000 + bytes(0x1000))])
    moved, report = rp.bring_over([pc], gone, FakeAssembler())
    assert moved == [] and report.failed["Health"].startswith("can't find the code at +100")
    short = ProCheat("p2", "Short", [ProEntry("bytes", NOP_SITE, b"\x89\x4b", b"\x90\x90")])
    assert "no signature" in rp.bring_over([short], gone, None)[1].failed["Short"]


def test_cave_reaching_game_data_fails():
    pc = _cheat("db 0x90\njmp 0x50\n")
    rp.learn([pc], _image())
    rooms = [(0, 0x10)]
    moved, report = rp.bring_over([pc], _image(SHIFT), FakeAssembler(), reserved=rooms)
    assert moved == [] and "+50" in report.failed["Health"]


def test_no_assembler_fails_caves():
    pc = _cheat()
    rp.learn([pc], _image())
    _moved, report = rp.bring_over([pc], _image(SHIFT), None)
    assert report.failed == {"Health": "Assembler not found"}


def test_assemble_error_is_reported():
    pc = _cheat()
    rp.learn([pc], _image())
    asm = FakeAssembler()
    asm.fail = (3, "bad")
    _moved, report = rp.bring_over([pc], _image(SHIFT), asm)
    assert "line 3: bad" in report.failed["Health"]


# -- carry integration ----------------------------------------------------------------------------
def test_carry_brings_pro_cheats_into_the_new_notes():
    old = pj.Project("PPSA01234", "01.000", "eboot.bin")
    old.pro_cheats.append(_cheat())
    rp.learn(old.pro_cheats, _image())
    image = _image(SHIFT)
    plan = cr.Plan(old, pj.Project("PPSA01234", "01.001", "eboot.bin"), image)
    cr._carry_pro(plan, image, FakeAssembler())
    new, report = cr.assemble(plan, {}, {})
    assert [p.entries[0].offset for p in new.pro_cheats] == [HOOK + SHIFT]
    assert "Health" in report.carried and "Health" in report.check
    cave = new.pro_cheats[0].entries[0]
    assert (cave.cave, cave.cave + len(cave.cave_bytes)) in plan.reserved


def test_learn_note_signatures_learns_pro_aobs():
    p = pj.Project("PPSA01234", "01.000", "eboot.bin")
    p.pro_cheats.append(_cheat())
    changed, _parts = cr.learn_note_signatures(p, _image(), have={})
    assert changed == 2 and all(e.aob for e in p.pro_cheats[0].entries)


def test_merge_keeps_new_names_and_fresh_ids():
    source = pj.Project("PPSA01234", "01.000", "eboot.bin")
    source.pro_cheats += [_cheat(), ProCheat("p2", "Other", [ProEntry("nop", 1, b"\x90")])]
    into = pj.Project("PPSA01234", "01.001", "eboot.bin")
    into.pro_cheats.append(ProCheat("p1", "Other", [ProEntry("nop", 2, b"\x90")]))
    merged = cr.for_merge(source, into)
    assert [(p.id, p.name) for p in merged.pro_cheats] == [("p2", "Health")]


@pytest.mark.needs_nasm
def test_bring_over_with_nasm(nasm):
    pc = _cheat()
    entry = pc.entries[0]
    entry.source = "movss xmm1, [mult]\noriginal\njmp return\n"
    from cheatmaker.core.pro import compile as pro_compile

    entry.cave_bytes = pro_compile.assemble(pc, entry, nasm).code
    pro_compile.record_layout(pc, entry)
    rp.learn([pc], _image())
    (new,), report = rp.bring_over([pc], _image(SHIFT), nasm)
    assert report.moved == ["Health"]
    cave = new.entries[0]
    first = next(iter(ix.Decoder(64, cave.cave_bytes, ip=cave.cave)))
    assert first.ip_rel_memory_address == cave.cave + len(cave.cave_bytes) - 4


def test_unconverted_imported_cave_is_refused_with_a_reason():
    cave = ProEntry("bytes", 0x1F00, bytes(2), b"\x90\xc3")
    pc = ProCheat("p3", "Imported", [cave, ProEntry("nop", NOP_SITE, NOP_CODE[:3])])
    _moved, report = rp.bring_over([pc], _image(SHIFT), FakeAssembler())
    assert "isn't converted to assembly" in report.failed["Imported"]


def test_update_file_moves_a_pro_source_with_its_lines_and_refuses_caves():
    """QA BUG-08."""
    from cheatmaker.core.cheats import CheatFile
    from cheatmaker.core.pro import compile as pro_compile

    nop_only = _cheat()
    nop_only.entries, nop_only.variables, nop_only.editable = nop_only.entries[1:], [], None
    nop_only.entries[0].aob = "89 4B 08"
    with_cave = _cheat()
    with_cave.id, with_cave.name = "p2", "Cave"
    cf = CheatFile(
        "T",
        "PPSA01234",
        "01.000",
        [pro_compile.compile_cheat(nop_only), pro_compile.compile_cheat(with_cave)],
    )
    new, report = up.update_file(cf, up.learn(cf, _image()), _image(SHIFT), "01.001")
    assert report.moved == ["Health"] and report.failed["Cave"] == up.PRO_CAVE
    (moved,) = new.cheats
    assert moved.pro is not nop_only and moved.pro.entries[0].offset == NOP_SITE + SHIFT
    assert moved.pro.entries[0].aob == "" and nop_only.entries[0].offset == NOP_SITE
