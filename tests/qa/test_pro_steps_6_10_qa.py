"""QA round for Cheat Maker Pro R1 steps 6-10 (PR #38): the NASM wrapper and shim,
bytes to text, compile and on/off, live variables, format 5 persistence through My
cheats, Bring them over for Pro cheats, and import / Convert to assembly.
Spec docs/spec-pro.md (PR-8, PR-9, PR-14, AC-S1, AC-8.x, AC-9.x, AC-14.1); design
docs/architecture-pro.md steps 6-10. Report: qa/reports/2026-11-22-pro-steps-6-10.md.

Bugs are strict xfail tests named after their BUG id; remove the marker once fixed.
FakeConsole stands in for the PS5 and the real NASM (``needs_nasm``) assembles.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import stat
import struct
from pathlib import Path

import iced_x86 as ix
import pytest

from cheatmaker import formats
from cheatmaker.core import cave as cv
from cheatmaker.core import project as pj
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.core.pro import compile as pc_
from cheatmaker.core.pro import importer, sites
from cheatmaker.core.pro import refpoints as rp
from cheatmaker.core.pro.asm import (
    AssemblerNotFound,
    CaveContext,
    FakeAssembler,
    NasmAssembler,
)
from cheatmaker.core.pro.asm_text import CantConvert, to_source
from cheatmaker.core.pro.model import ProCheat, ProEntry, Variable
from cheatmaker.core.session import Session
from cheatmaker.core.values import ValueType
from cheatmaker.errors import WriteFailed
from tests.unit.test_pro_compile import (
    BASE,
    HOOK,
    HOOK_CODE,
    NOP_SITE,
    PID,
    _FailOn,
    _two_caves,
)
from tests.unit.test_pro_compile import _cheat as built_cheat
from tests.unit.test_pro_compile import _fake as compile_fake
from tests.unit.test_pro_import import CAVE, CAVE_CODE, HOOK_OFF
from tests.unit.test_pro_import import _file as import_file
from tests.unit.test_pro_refpoints import SHIFT, _image
from tests.unit.test_pro_refpoints import _cheat as carry_cheat

MODULE = 0x10000
CTX = CaveContext(org=0x1000, hook=0x100, original=bytes.fromhex("8b4b08 89c8"), module_size=MODULE)


@pytest.fixture
def s():
    fake = compile_fake()
    session = Session(lambda ip, port: fake)
    session.connect("192.168.1.50")
    session.refresh_game()
    session.fake = fake  # type: ignore[attr-defined]
    return session


def _insn(code: bytes, ip: int) -> ix.Instruction:
    return next(iter(ix.Decoder(64, code, ip=ip)))


# == the assembler wrapper and shim (step 6) ===========================================
@pytest.mark.needs_nasm
@pytest.mark.parametrize(
    ("source", "line"),
    [
        ("nop\nnop\nmovv eax, 1", 3),
        ("nop\r\nnop\r\nmovv eax, 1\r\n", 3),  # Windows line ends
        ("original\nmov eax, [rbx\n", 2),
        ("%macro m 0\nmovv eax,1\n%endmacro\nnop\nm", 5),
        ("mov al, 0x1ff", 1),  # -Werror=number-overflow
        ("jmp 0x20000", 1),  # outside the module
        ("\tnop ; comment with ; and [0x99999]\nfoo bar", 2),
    ],
)
def test_error_lines_are_the_users(nasm, source, line):  # TC-01
    result = nasm.assemble(source, CTX)
    assert not result.ok and result.error[0] == line, result.error


@pytest.mark.needs_nasm
def test_plain_module_offsets_are_rip_relative(nasm):  # TC-02
    for src in ("mov eax, [0x2000]", "mov eax, [rel 0x2000]", "mov eax, [2000h]"):
        code = nasm.assemble(src, CTX).code
        insn = _insn(code, CTX.org)
        assert insn.is_ip_rel_memory_operand and insn.ip_rel_memory_address == 0x2000, src
    code = nasm.assemble("mov rax, fs:[0x28]", CTX).code
    assert not _insn(code, CTX.org).is_ip_rel_memory_operand, "segment operands left alone"


@pytest.mark.needs_nasm
@pytest.mark.parametrize(
    "src",
    [
        "mov eax, [0x2000+0x10]",
        "mov eax, [dword 0x2010]",
        "mov eax, [0b10000000010000]",
        "mov eax, [0x20_10]",
        "mov eax, [0x2010+rcx*4]",
        "mov eax, [0x10000+0x10000]",
    ],
)
@pytest.mark.xfail(
    reason="BUG-01: a bare-number address not written exactly as [N] is assembled as an "
    "absolute address (not a module offset) and not refused",
    strict=True,
)
def test_bug01_every_bare_number_address_is_a_module_offset_or_refused(nasm, src):  # TC-03
    result = nasm.assemble(src, CTX)
    if result.ok:
        insn = _insn(result.code, CTX.org)
        assert insn.is_ip_rel_memory_operand, f"{src} reads absolute {insn.memory_displacement:#x}"


@pytest.mark.needs_nasm
@pytest.mark.xfail(
    reason="BUG-02: the shim rewrites [N] after a spaced segment prefix and inside strings",
    strict=True,
)
@pytest.mark.parametrize(
    ("src", "want"),
    [("mov rax, fs: [0x28]", "64488b042528000000"), ('db "[5]"', b"[5]".hex())],
    ids=["fs-space", "string"],
)
def test_bug02_shim_leaves_segment_operands_and_strings(nasm, src, want):  # TC-04
    assert nasm.assemble(src, CTX).code.hex() == want


@pytest.mark.needs_nasm
@pytest.mark.xfail(
    reason="BUG-03: cave source can read any file on the PC into the cave (incbin, %include)",
    strict=True,
)
def test_bug03_cave_source_cant_read_pc_files(nasm, tmp_path):  # TC-05
    secret = tmp_path / "secret.txt"
    secret.write_text("password", encoding="utf-8")
    result = nasm.assemble(f'incbin "{secret.as_posix()}"', CTX)
    assert b"password" not in result.code


def _script(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fake-nasm"
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as the assembler")
def test_slow_assembler_times_out_with_a_message(tmp_path):  # TC-06
    asm = NasmAssembler(_script(tmp_path, "sleep 5\n"), timeout=0.3)
    result = asm.assemble("nop", CTX)
    assert result.error == (0, "the assembler took too long; try again")


def test_missing_or_broken_assembler_is_assembler_not_found(tmp_path):  # TC-07
    with pytest.raises(AssemblerNotFound):
        NasmAssembler(tmp_path / "nope.exe").assemble("nop", CTX)
    plain = tmp_path / "not-executable"
    plain.write_bytes(b"x")
    with pytest.raises(AssemblerNotFound):
        NasmAssembler(plain).assemble("nop", CTX)


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as the assembler")
@pytest.mark.xfail(
    reason="BUG-04: assembler output that isn't valid in the PC's text encoding raises "
    "UnicodeDecodeError instead of an error line",
    strict=True,
)
def test_bug04_undecodable_assembler_output_is_an_error_line(tmp_path):  # TC-08
    body = 'printf "user:1: error: symbol \\140\\377\\201\\047 not defined\\n" >&2\nexit 1\n'
    result = NasmAssembler(_script(tmp_path, body)).assemble("mov eax, x", CTX)
    assert result.error is not None and result.error[0] == 1


# == bytes to text (step 7) ============================================================
ODD = {
    "rep ret": "f3c3",
    "66 nop": "6690",
    "long nop": "0f1f440000",
    "cs prefix": "2e8b03",
    "notrack": "3effe0",
    "bnd jmp": "f2e900000000",
    "vex": "c5fa100508000000",
    "evex k1z": "62f17cc9580500000000",
    "xacquire": "f0f2ff00",
    "fs abs": "64488b042528000000",
    "fs rip": "64488b0500000000",
    "moffs": "48a10000000000000000",
    "jmp into": "eb0190c3",
    "call hook": "e8" + (0x100 - 0x1005).to_bytes(4, "little", signed=True).hex(),
    "loop": "e2fe",
    "jrcxz": "e300c3",
    "jcc rel32": "0f8400000000c3",
    "long disp": "8b8300000000",
    "sib no base": "8b04a500100000",
    "redundant rex": "408b03",
    "mov r,r other way": "498bc4",
    "imm32 for imm8": "81c001000000",
    "movabs": "48b88877665544332211",
    "far jmp": "ff2c2500100000",
}


@pytest.mark.needs_nasm
@pytest.mark.parametrize("name", list(ODD))
def test_odd_encodings_round_trip_byte_for_byte(nasm, name):  # TC-10
    code = bytes.fromhex(ODD[name])
    ctx = CaveContext(0x1000, 0x100, bytes.fromhex("8b4b0889c8"))
    source = to_source(code, ctx, nasm)
    assert nasm.assemble(source, ctx).code == code


@pytest.mark.needs_nasm
@pytest.mark.parametrize("hexcode", ["0f", "0000000000", "e9000000"])
def test_bytes_that_dont_decode_are_refused_with_the_offset(nasm, hexcode):  # TC-11
    with pytest.raises(CantConvert, match=r"bytes at \+0x"):
        to_source(bytes.fromhex(hexcode), CaveContext(0x1000), nasm)


# == compile, on/off, rollback, overlap (step 8) =======================================
def test_rollback_puts_back_the_jump_before_the_cave(s, monkeypatch):  # TC-20
    fake = s.fake
    pc = built_cheat(s)
    pc.entries.append(pc_.new_nop_entry(s, NOP_SITE))
    real = fake.write
    order: list[int] = []

    def write(pid, addr, data):
        order.append(addr - BASE)
        if addr - BASE == NOP_SITE and data[0] == 0x90:  # the 3rd write doesn't take
            return real(pid, addr, data[:-1])
        return real(pid, addr, data)

    monkeypatch.setattr(fake, "write", write)
    with pytest.raises(WriteFailed):
        pc_.enable(s, pc, pc_.ProWriter(s))
    entry = pc.entries[0]
    assert order[:3] == [entry.cave, HOOK, NOP_SITE]
    assert order[3:] == [NOP_SITE, HOOK, entry.cave], "undo: last write first"
    assert fake.peek(PID, BASE + HOOK, len(HOOK_CODE)) == HOOK_CODE
    assert fake.peek(PID, BASE + entry.cave, 4) == bytes(4)


def test_enable_twice_and_overlap_with_itself_is_allowed(s):  # TC-21
    pc = built_cheat(s)
    writer = pc_.ProWriter(s)
    pc_.enable(s, pc, writer)
    with pytest.raises(pc_.SiteChanged):  # already on: the hook holds the jump now
        pc_.enable(s, pc, writer, [pc_.compile_cheat(pc)])
    pc_.disable(s, pc, writer, sleep=lambda t: None)
    pc_.enable(s, pc, writer)


def test_overlap_names_a_guided_cheat_that_covers_the_cave(s):  # TC-22
    pc = built_cheat(s)
    cave = pc.entries[0].cave
    guided = Cheat("Ammo", [Location(LocationKind.CODE_PATCH, cave + 3, b"\x90", b"\x00")])
    with pytest.raises(sites.Overlap, match="Ammo"):
        pc_.enable(s, pc, pc_.ProWriter(s), [guided])


def test_compiled_cave_line_is_the_same_on_and_off_after_a_value_edit(s):  # TC-23
    cheat = pc_.compile_cheat(built_cheat(s))
    cheat.set_value(5.0)
    cave_line = cheat.locations[0]
    assert cave_line.on == cave_line.off
    pc_.take_value(cheat)
    assert cheat.pro.entries[0].cave_bytes[-4:] == struct.pack("<f", 5.0)


def test_live_variable_write_is_only_the_variable_and_off_is_stored(s):  # TC-24
    pc = built_cheat(s)
    writer = pc_.ProWriter(s)
    pc_.enable(s, pc, writer)
    s.fake.writes.clear()
    assert pc_.set_variable(pc, "mult", 0.5, FakeAssembler(), session=s, writer=writer, on=True)
    (write,) = s.fake.writes
    assert write[1] == BASE + pc_.variable_addresses(pc)["mult"]
    pc_.disable(s, pc, writer, sleep=lambda t: None)
    assert pc_.set_variable(pc, "mult", 7.0, FakeAssembler()) == pc_.SAVED


@pytest.mark.xfail(
    reason="BUG-05: a cave marked stale is still compiled into My cheats and cheat files",
    strict=True,
)
def test_bug05_stale_cave_isnt_exported(s):  # TC-25
    pc = _two_caves(s)
    home, second = pc.entries
    home.source = "nop\nnop\nnop\nnop\noriginal\njmp return\n"
    pc_.build(s, pc, home, _FailOn("jmp mult\n"))
    assert second.stale
    cf = CheatFile("T", "PPSA01234", "01.000", [pc_.compile_cheat(pc)])
    assert cf.saveable_cheats() == []


@pytest.mark.xfail(
    reason="BUG-06: set_variable assembles an unbuilt source and stores a cave bigger "
    "than its room (no NoRoom check)",
    strict=True,
)
def test_bug06_set_variable_never_grows_a_cave_past_its_room(s):  # TC-26
    pc = built_cheat(s, "nop\noriginal\njmp return\n")
    entry = pc.entries[0]
    s.fake.poke(PID, BASE + entry.cave + 0x40, b"\x01")  # something 64 bytes on
    room = sites.room_at(s, entry.cave, len(entry.cave_bytes))
    entry.source = "db " + ", ".join(["0x90"] * 0x60) + "\noriginal\njmp return\n"
    with pytest.raises(pc_.NoRoom):
        pc_.build(s, pc, entry, FakeAssembler())  # the editor's save is refused
    with contextlib.suppress(pc_.NoRoom, ValueError):
        pc_.set_variable(pc, "mult", 3.0, FakeAssembler(), session=s)
    assert len(entry.cave_bytes) <= room


# == persistence through My cheats (format 5) ==========================================
@pytest.fixture
def pwin(qtbot):
    from cheatmaker.settings import Settings
    from cheatmaker.ui.controller import Controller
    from cheatmaker.ui.main_window import MainWindow
    from cheatmaker.ui.tasks import TaskRunner
    from tests import fakegame as fg

    game = fg.build(1, connect=False)
    ctl = Controller(lambda ip, port: game.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()
    yield w
    ctl.shutdown()


def _saved(win) -> pj.Project:
    p = win.ctl.project
    return pj.load(pj.project_path(p.title_id, p.version))


def _restart(win) -> None:
    win.ctl.ensure_file().cheats.clear()
    game = win.ctl.session.require_game()
    win.cheats.finder.on_game(dataclasses.replace(game, pid=game.pid + 1))


def test_rename_then_restart_keeps_the_new_name(pwin):  # TC-30
    from cheatmaker.ui.cheats_view import COL_NAME
    from tests.ui.test_pro_persist import _pro

    cf = pwin.ctl.ensure_file()
    cheat = pc_.compile_cheat(_pro())
    cf.cheats.append(cheat)
    pwin.cheats.finder.save()
    pwin.ctl.cheats_changed.emit()
    pwin.cheats.table.item(pwin.cheats.cheats.index(cheat), COL_NAME).setText("Never hurt")
    _restart(pwin)
    assert [c.name for c in cf.cheats if c.pro is not None] == ["Never hurt"]
    assert [c.name for c in _saved(pwin).pro_cheats] == ["Never hurt"]


def test_two_pro_cheats_survive_a_restart_in_order(pwin):  # TC-31
    from tests.ui.test_pro_persist import _pro

    cf = pwin.ctl.ensure_file()
    a, b = _pro(), _pro()
    b.id, b.name = "p2", "Second"
    b.entries = b.entries[:1]
    cf.cheats += [pc_.compile_cheat(a), pc_.compile_cheat(b)]
    pwin.cheats.finder.save()
    _restart(pwin)
    assert [c.pro.id for c in cf.cheats if c.pro is not None] == ["p1", "p2"]


@pytest.mark.xfail(
    reason="BUG-07: after Bring them over, the old version's Pro cheat still in My cheats "
    "(same id) overwrites the moved one in the new notes",
    strict=True,
)
def test_bug07_bring_over_result_isnt_overwritten_by_the_old_row(pwin):  # TC-32
    old_pc = carry_cheat()
    rp.learn([old_pc], _image())
    pwin.ctl.ensure_file().cheats.append(pc_.compile_cheat(old_pc))  # still listed
    (moved,), _report = rp.bring_over([old_pc], _image(SHIFT), FakeAssembler())
    g = pwin.ctl.session.require_game()
    carried = pj.Project(g.title_id, g.version, "eboot.bin", pro_cheats=[moved])
    pwin.cheats.finder.adopt_carried(carried, {})
    saved = _saved(pwin).pro_cheats
    assert [e.offset for e in saved[0].entries] == [e.offset for e in moved.entries]


@pytest.mark.xfail(
    reason="BUG-08: Update for this version moves the cheat's lines but keeps its old Pro "
    "source, which is then saved and restored at the old offsets",
    strict=True,
)
def test_bug08_update_for_this_version_keeps_source_and_lines_together():  # TC-33
    pc = carry_cheat()
    pc.entries = pc.entries[1:]  # a NOP entry only (caves can't move by file update)
    pc.variables, pc.editable = [], None
    cf = CheatFile("T", "PPSA01234", "01.000", [pc_.compile_cheat(pc)])
    new, report = up.update_file(cf, up.learn(cf, _image()), _image(SHIFT), "01.001")
    assert report.moved == [pc.name]
    moved = new.cheats[0]
    project = pj.Project("PPSA01234", "01.001", "eboot.bin")
    pc_.store(project, [moved])
    (again,) = pc_.restore(project, [])
    assert [loc.offset for loc in again.locations] == [loc.offset for loc in moved.locations]


# == Bring them over (step 9) ==========================================================
def test_moved_cheat_compiles_and_turns_on_in_the_new_version():  # TC-40
    pc = carry_cheat()
    rp.learn([pc], _image())
    (new,), report = rp.bring_over([pc], _image(SHIFT), FakeAssembler())
    cheat = pc_.compile_cheat(new)
    assert report.moved == ["Health"]
    assert {loc.offset for loc in cheat.locations} >= {HOOK + SHIFT, NOP_SITE + SHIFT}
    assert all(e.aob for e in new.entries)


@pytest.mark.xfail(reason="BUG-09: a stale cave stays stale after Bring them over", strict=True)
def test_bug09_bring_over_clears_stale(s):  # TC-41
    pc = carry_cheat()
    pc.entries[0].stale = True
    rp.learn([pc], _image())
    (new,), _report = rp.bring_over([pc], _image(SHIFT), FakeAssembler())
    assert not new.entries[0].stale


def test_unconverted_import_is_refused_and_converted_one_moves(nasm_or_fake):  # TC-42
    imported = importer.import_cheats(import_file()).cheats[0]
    moved, report = rp.bring_over([imported], _image(), nasm_or_fake)
    assert moved == [] and "isn't converted" in report.failed["God mode"]


@pytest.fixture
def nasm_or_fake():
    from cheatmaker.paths import nasm_exe

    exe = nasm_exe()
    return NasmAssembler(exe) if exe is not None else FakeAssembler()


# == import and Convert (step 10) ======================================================
@pytest.mark.parametrize("fmt", ["json", "shn", "mc4"])
def test_import_every_format_keeps_bytes(tmp_path, fmt):  # TC-50
    cf = import_file()
    cf.cheats = cf.cheats[:1]
    path = tmp_path / cf.file_name(fmt)
    path.write_bytes(formats.dumps(cf, fmt))
    (pc,) = importer.import_path(path).cheats
    assert [(e.offset, e.on, e.off) for e in pc.entries] == [
        (loc.offset, loc.on, loc.off) for loc in cf.cheats[0].locations
    ]


@pytest.mark.needs_nasm
def test_converted_import_compiles_and_turns_on_over_zeros(nasm):  # TC-51
    pc = importer.import_cheats(import_file()).cheats[0]
    assert importer.convert(pc, nasm) == 1
    cave = next(e for e in pc.entries if e.kind == "cave")
    assert cave.cave == CAVE and cave.cave_bytes == CAVE_CODE and cave.off == HOOK_OFF
    cheat = pc_.compile_cheat(pc)
    by_offset = {loc.offset: loc for loc in cheat.locations}
    assert by_offset[CAVE].on == CAVE_CODE and by_offset[HOOK].on == cv.jmp_patch(HOOK, CAVE, 5)


def test_convert_two_jumps_into_one_cave_pairs_only_the_first():  # TC-52
    cf = import_file()
    cave, jump, _nop = cf.cheats[0].locations
    second = Location(LocationKind.CODE_PATCH, 0x400, cv.jmp_patch(0x400, CAVE, 5), HOOK_OFF)
    cf.cheats = [Cheat("Two", [cave, jump, second])]
    pc = importer.import_cheats(cf).cheats[0]
    assert importer.convert(pc, FakeAssembler()) == 1
    assert [e.kind for e in pc.entries] == ["cave", "bytes"]


def test_import_refuses_other_sections_by_name():  # TC-53
    result = importer.import_cheats(import_file())
    assert "section 2" in result.refused["Libc thing"]


def test_variables_are_placed_after_align_4(nasm_or_fake):  # TC-54
    pc = ProCheat(
        "p1",
        "V",
        [ProEntry("cave", HOOK, HOOK_CODE, source="nop\n", cave=0x1F00)],
        [Variable("a", ValueType.UINT8, 7), Variable("b", ValueType.DOUBLE, 1.5)],
    )
    result = pc_.assemble(pc, pc.entries[0], nasm_or_fake)
    assert result.ok, result.error
    offs = pc_.variable_offsets(pc.variables, len(result.code))
    assert result.code[offs["a"]] == 7
    assert result.code[offs["b"] : offs["b"] + 8] == struct.pack("<d", 1.5)
    assert offs["a"] % 4 == 0
