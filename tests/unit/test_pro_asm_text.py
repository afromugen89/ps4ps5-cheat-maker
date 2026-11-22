"""Pro step 7: cave bytes to text, guided-cave adoption (AC-S1), imported caves (AC-14.1).

Every guided cave builder's output must come back byte for byte from the text."""

from __future__ import annotations

import itertools
import struct

import iced_x86 as ix
import pytest

from cheatmaker.core import cave as cv
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.locker import Writer
from cheatmaker.core.pro.asm import CaveContext, FakeAssembler
from cheatmaker.core.pro.asm_text import CantConvert, adopt, cave_entries, decode, to_source
from cheatmaker.core.pro.badge import badge
from tests import fakegame as fg

CAVE = 0x3F00  # module offsets: sources are assembled as if the module sat at 0
TABLE, SLOT = 0x4800, 0x4900
VT = fg.PLAYER_VT - fg.BASE


def _span(form: fg.Form) -> tuple[list[ix.Instruction], cv.Target]:
    site = form.site - fg.BASE
    span = cv.decode_at(form.code, site)
    return span, cv.writer_target(Writer(site, form.code, "", 1))


def _ctx(form: fg.Form) -> CaveContext:
    return CaveContext(CAVE, form.site - fg.BASE, form.code)


def _round_trip(nasm, code: bytes, ctx: CaveContext) -> str:
    text = to_source(code, ctx, nasm)
    assert nasm.assemble(text, ctx).code == code
    return text


# -- AC-S1: every guided builder -------------------------------------------------------
def _sampler_cases():
    for name, form in fg.FORMS.items():
        span, target = _span(form)
        yield f"sampler-{name}", form, cv.sampler_code(span, target, TABLE, CAVE)


def _player_only_cases():
    for (name, form), value in itertools.product(fg.FORMS.items(), (4, 8)):
        if form.vtype.size != value:
            continue
        span, target = _span(form)
        kept = form.vtype.encode(999)
        code = cv.player_only_code(span, target, 8, 0x40048, kept, CAVE)
        yield f"player-{name}", form, code


def _capture_cases():
    form = fg.FORMS["float"]
    span, target = _span(form)
    for kind, value in itertools.product(("vtable", "field", "none"), (None, b"\0\0\x7a\x44")):
        if kind == "none" and value is not None:
            continue  # refused: keeping a value needs a filter
        code = cv.capture_code(
            span, target, SLOT, CAVE, kind, vtable=VT, field=8, field_value=0x40048, value=value
        )
        yield f"capture-{kind}-{value is not None}", form, code


def _effect_cases():
    filters = (None, cv.Filter("vtable", vtable=VT), cv.Filter("field", field=8, field_value=7))
    for (name, form), effect, only_me, counter, flags_fix in itertools.product(
        fg.FORMS.items(), cv.Effect, filters, (None, SLOT), (False, True)
    ):
        span, target = _span(form)
        code = cv.effect_code(
            span,
            target,
            form.vtype,
            effect,
            CAVE,
            full=form.vtype.encode(250),
            counter=counter,
            only_me=only_me,
            flags_fix=flags_fix,
        )
        kind = only_me.kind if only_me else "all"
        yield f"effect-{name}-{effect.name}-{kind}-{counter}-{flags_fix}", form, code


CASES = [*_sampler_cases(), *_player_only_cases(), *_capture_cases(), *_effect_cases()]


@pytest.mark.needs_nasm
@pytest.mark.parametrize(("form", "code"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_guided_caves_round_trip(nasm, form, code):
    text = _round_trip(nasm, code, _ctx(form))
    assert "jmp near return" in text


@pytest.mark.needs_nasm
def test_text_uses_labels_and_return(nasm):
    form = fg.FORMS["float"]
    span, target = _span(form)
    code = cv.player_only_code(span, target, 8, 0x40048, form.vtype.encode(999), CAVE)
    text = _round_trip(nasm, code, _ctx(form))
    assert "jne short L1\n" in text and "\nL1:\n" in text
    assert text.count("jmp near return") == 2
    assert "lea rsp, [dword rsp-0x80]" in text  # the long form kept by a size hint
    assert "0x3f" not in text.split("L1:")[0], "no raw cave addresses"


@pytest.mark.needs_nasm
def test_lines_nasm_encodes_differently_become_db(nasm):
    # mov rax, r12 in its 49 8B C4 form; NASM writes 4C 89 E0
    code = bytes.fromhex("498bc4") + bytes.fromhex("c3")
    text = _round_trip(nasm, code, CaveContext(CAVE))
    assert "    db 0x49, 0x8b, 0xc4 ; mov rax, r12\n" in text
    assert text.endswith("    ret\n")


@pytest.mark.needs_nasm
def test_rip_relative_and_internal_data_targets(nasm):
    # lea rax,[rip+0] (points at the next instruction), mov eax,[rel 0x1234]
    code = bytes.fromhex("488d0500000000") + bytes.fromhex("8b05") + struct.pack("<i", -0x2D0D)
    text = _round_trip(nasm, code, CaveContext(CAVE))
    assert "lea rax, [rel L1]" in text
    assert f"[rel {CAVE + 7 + 6 - 0x2D0D:#x}]" in text


# -- AC-14.1 and refusals ---------------------------------------------------------------
@pytest.mark.needs_nasm
def test_imported_cave_converts(nasm):
    # a typical imported cave: compare, branch, store, the original, jump back
    hook = fg.WRITER - fg.BASE
    ctx = CaveContext(CAVE, hook, fg.VMOVSS_W)
    src = (
        "cmp dword [r12+8], 0x40048\njne .skip\nmov dword [r12+0x2ac], 0x447a0000\n"
        "jmp return\n.skip:\noriginal\njmp return\n"
    )
    code = nasm.assemble(src, ctx).code
    assert code
    text = _round_trip(nasm, code, ctx)
    assert "jne short L1" in text


def test_undecodable_bytes_are_refused_with_their_offset():
    with pytest.raises(CantConvert, match=r"\+0x3 \(ff\)"):
        decode(bytes.fromhex("900f0bff"), CAVE)  # ud2 decodes; a cut-off one doesn't
    with pytest.raises(CantConvert, match=r"\+0x2"):
        to_source(bytes.fromhex("9090ff"), CaveContext(CAVE), FakeAssembler())


def test_fake_assembler_falls_back_to_db_everywhere_it_must():
    code = bytes.fromhex("90c38b0500000000")
    text = to_source(code, CaveContext(CAVE), FakeAssembler())
    db_line = "    db 0x8b, 0x05, 0x00, 0x00, 0x00, 0x00 ; mov eax, [rel 0x3f08]\n"
    assert text == "    nop\n    ret\n" + db_line
    assert to_source(b"", CaveContext(CAVE), FakeAssembler()) == ""


# -- adopting guided cheats (AC-S1) ------------------------------------------------------
@pytest.mark.needs_nasm
def test_lock_it_player_only_is_adopted_byte_for_byte(nasm):
    g = fg.build()
    writer = Writer(fg.WRITER, fg.VMOVSS_W, "vmovss [r12+2ACh],xmm1", 1)
    field = cv.FieldGuess(8, 0x40048, (0x30010,))
    cheat = cv.make_player_only_cheat(g.session, writer, field, struct.pack("<f", 999.0), "HP")
    pc = adopt(cheat, "p1", nasm)
    assert pc.origin == "guided" and pc.name == "HP"
    (entry,) = pc.entries
    cave_loc, hook_loc = cheat.locations
    assert entry.kind == "cave"
    assert (entry.offset, entry.off, entry.on) == (hook_loc.offset, hook_loc.off, hook_loc.on)
    assert (entry.cave, entry.cave_bytes) == (cave_loc.offset, cave_loc.on)
    ctx = CaveContext(entry.cave, entry.offset, entry.off)
    assert nasm.assemble(entry.source, ctx).code == cave_loc.on
    assert badge(cheat).target == "CheatRunner"


def test_adopt_keeps_nop_and_byte_lines():
    lines = [
        Location(LocationKind.CODE_PATCH, 0x300, b"\x90" * 6, fg.SUB_W),
        Location(LocationKind.MODULE_VALUE, 0x4010, b"\x01\x00", b"\x05\x00"),
        # a jump to somewhere that isn't a line of this cheat: plain bytes
        Location(LocationKind.CODE_PATCH, 0x100, b"\xe9\0\0\0\0", b"\x89\x08\x90\x90\x90"),
    ]
    pc = adopt(Cheat("Mix", lines, hint="from guided"), "p2", FakeAssembler())
    assert [e.kind for e in pc.entries] == ["nop", "bytes", "bytes"]
    assert pc.entries[0].nop_default and pc.comment == "from guided"
    assert pc.entries[1].on == b"\x01\x00"


def test_adopt_refuses_pc_only_cheats():
    heap = Cheat("H", [Location(LocationKind.SESSION_ADDRESS, 0x2000_0040, b"\1", b"\0")])
    with pytest.raises(CantConvert, match="PC only"):
        adopt(heap, "p1", FakeAssembler())
    found = Cheat("F", [Location(LocationKind.CODE_PATCH, 1, b"\1", b"\0")], anchor=("o1", "hp"))
    with pytest.raises(CantConvert, match="PC only"):
        adopt(found, "p1", FakeAssembler())
    with pytest.raises(CantConvert, match="nothing"):
        adopt(Cheat("E", []), "p1", FakeAssembler())
    other = Location(LocationKind.CODE_PATCH, 1, b"\1", b"\0", module="libother.prx")
    with pytest.raises(CantConvert, match="outside"):
        adopt(Cheat("O", [other]), "p1", FakeAssembler())


def test_adopt_returns_an_existing_pro_source():
    line = Location(LocationKind.CODE_PATCH, 1, b"\1", b"\0")
    pc = adopt(Cheat("N", [line]), "p1", FakeAssembler())
    cheat = Cheat("N", [], pro=pc)
    assert adopt(cheat, "p9", FakeAssembler()) is pc


def test_cave_entries_pair_jump_and_cave_with_fake():
    jump = cv.jmp_patch(0x100, CAVE, 5)
    lines = [
        Location(LocationKind.CODE_PATCH, CAVE, b"\x90\xc3", b"\x90\xc3"),
        Location(LocationKind.CODE_PATCH, 0x100, jump, b"\x89\x08\x90\x90\x90"),
    ]
    (entry,) = cave_entries(lines, FakeAssembler(), comment="c")
    assert entry.source == "    nop\n    ret\n" and entry.comment == "c"
