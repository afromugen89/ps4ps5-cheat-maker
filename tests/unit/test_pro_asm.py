"""Pro step 6: the cave assembler (wrapper, symbols, errors, shim, fake)."""

from __future__ import annotations

import struct
import subprocess
import time

import iced_x86 as ix
import pytest

from cheatmaker.core.pro import asm
from cheatmaker.core.pro.asm import (
    AssemblerNotFound,
    CaveContext,
    FakeAssembler,
    NasmAssembler,
    parse_error,
    parse_map,
    shim,
    shim_line,
    wrap,
)
from cheatmaker.core.pro.model import Variable
from cheatmaker.core.values import ValueType

CAVE = 0x407000
HOOK = 0x400100
# mov eax, [rip+0x10] (reads 0x400116), 6 bytes: what the jump at HOOK covers
ORIGINAL = bytes.fromhex("8b0510000000")
CTX = CaveContext(CAVE, HOOK, ORIGINAL)
MULT = Variable("mult", ValueType.FLOAT, 2.0)


def _decode(code: bytes, ip: int = CAVE) -> list[ix.Instruction]:
    return list(ix.Decoder(64, code, ip=ip))


# -- shim ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("movss xmm0, dword ptr [rax+10h]", "movss xmm0, dword [rax+10h]"),
        ("mov BYTE PTR [rcx], 1", "mov BYTE [rcx], 1"),
        ("movaps xmm1, xmmword ptr [rsp]", "movaps xmm1, oword [rsp]"),
        ("vmovaps ymm1, ymmword ptr [rsp]", "vmovaps ymm1, yword [rsp]"),
        ("jmp OriginReturn", "jmp return"),
        ("mov eax, [0x407010]", "mov eax, [rel $$+(0x407010-0x407000)]"),
        ("mov eax, [rel 407010h]", "mov eax, [rel $$+(0x407010-0x407000)]"),
        ("mov rax, [abs 0x10]", "mov rax, [abs 0x10]"),
        ("mov eax, [rax+0x10]", "mov eax, [rax+0x10]"),
        ("nop ; dword ptr [0x10] OriginReturn", "nop ; dword ptr [0x10] OriginReturn"),
    ],
)
def test_shim_line(line, want):
    assert shim_line(line, CAVE) == want


def test_shim_keeps_every_line():
    src = "a:\n\n  dword ptr [0x1]\n; c\n"
    assert shim(src, CAVE).count("\n") == src.count("\n")


# -- wrapper -------------------------------------------------------------------------
def test_wrap_defines_hook_return_original_and_variables():
    text = wrap("nop", CaveContext(CAVE, HOOK, ORIGINAL, {"other": 0x407100}), [MULT])
    assert "org 0x407000" in text
    assert "hook equ 0x400100" in text
    assert "return equ 0x400106" in text
    assert "other equ 0x407100" in text
    # the covered instruction keeps reading 0x400116 from wherever it is assembled
    assert "mov eax, [rel $$+(0x400116-0x407000)]" in text
    assert "mult: db 0x00, 0x00, 0x00, 0x40" in text
    assert text.index("%line 0+1 user") < text.index("nop") < text.index("mult:")


def test_wrap_without_hook_has_no_original():
    text = wrap("nop", CaveContext(CAVE))
    assert "hook" not in text and "original" not in text and "align" not in text


def test_insn_text_marks_plain_addresses_absolute():
    (insn,) = _decode(bytes.fromhex("488b042510000000"))
    assert asm.insn_text(insn) == "mov rax, [abs 0x10]"


# -- NASM output parsing -------------------------------------------------------------
def test_parse_error_maps_user_lines():
    err = "user:3: warning: x\nuser:4: error: parser: instruction expected\nuser:9: error: b"
    assert parse_error(err) == (4, "parser: instruction expected")
    assert parse_error("cheatmaker-variables:2: error: bad") == (0, "bad")
    assert parse_error("nasm: fatal: unable to open input file") == (
        0,
        "unable to open input file",
    )
    assert parse_error("user:1: warning: only a warning") is None


def test_parse_map_reads_text_section_only():
    text = (
        "---- No Section ----\n\nValue     Name\n00400100  hook\n\n"
        "---- Section .text ----\n\nReal              Virtual           Name\n"
        "          407000            407000  start\n"
        "          407018            407018  mult\n"
    )
    assert parse_map(text) == {"start": 0x407000, "mult": 0x407018}


# -- the real assembler ----------------------------------------------------------------
@pytest.mark.needs_nasm
def test_nasm_assembles_with_symbols_original_and_variables(nasm):
    src = "start:\n  mulss xmm1, dword ptr [mult]\n  original\n  jmp OriginReturn\n"
    r = nasm.assemble(src, CTX, [MULT])
    assert r.ok, r.error
    insns = _decode(r.code)
    mulss, mov, jmp = insns[0], insns[1], insns[2]
    assert mulss.mnemonic == ix.Mnemonic.MULSS
    assert mulss.ip_rel_memory_address == r.labels["mult"]
    assert mov.ip_rel_memory_address == 0x400116  # same data as at the hook
    assert jmp.near_branch_target == HOOK + len(ORIGINAL)
    assert r.labels["start"] == CAVE
    assert r.labels["mult"] % 4 == 0
    off = r.labels["mult"] - CAVE
    assert r.code[off : off + 4] == struct.pack("<f", 2.0)


@pytest.mark.needs_nasm
def test_nasm_error_names_the_user_line(nasm):
    r = nasm.assemble("nop\n\n  frobnicate eax\nnop\n", CTX)
    assert r.code == b""
    assert r.error is not None and r.error[0] == 3
    assert r.error[1]


@pytest.mark.needs_nasm
def test_nasm_undefined_symbol_line(nasm):
    r = nasm.assemble("nop\njmp nowhere\n", CTX)
    assert r.error == (2, "symbol `nowhere' not defined")


@pytest.mark.needs_nasm
def test_nasm_plain_address_is_rip_relative(nasm):
    r = nasm.assemble("mov eax, dword ptr [0x400200]", CaveContext(CAVE))
    assert r.ok, r.error
    assert _decode(r.code)[0].ip_rel_memory_address == 0x400200


@pytest.mark.needs_nasm
def test_nasm_within_half_a_second(nasm):
    """AC-8.1: bytes or the first error within 0.5 s (worst of five runs)."""
    worst = 0.0
    for _ in range(5):
        t = time.perf_counter()
        r = nasm.assemble("original\njmp return\n", CTX, [MULT])
        worst = max(worst, time.perf_counter() - t)
        assert r.ok
    assert worst < 0.5


def test_timeout_reports_an_error(monkeypatch, tmp_path):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("nasm", 2)

    monkeypatch.setattr(asm.subprocess, "run", slow)
    r = NasmAssembler(tmp_path / "nasm").assemble("nop", CTX)
    assert r.error is not None and "too long" in r.error[1]


def test_missing_exe_raises(tmp_path):
    with pytest.raises(AssemblerNotFound):
        NasmAssembler(tmp_path / "no-nasm").assemble("nop", CTX)


def test_default_assembler_needs_one(monkeypatch):
    monkeypatch.setattr("cheatmaker.paths.nasm_exe", lambda configured="": None)
    with pytest.raises(AssemblerNotFound, match="Assembler not found"):
        asm.default_assembler()


# -- the fake --------------------------------------------------------------------------
def test_fake_assembles_its_subset():
    fa = FakeAssembler()
    r = fa.assemble("top:\n  original\n  jmp return ; back\n  jmp top\nend: nop", CTX, [MULT])
    assert r.ok, r.error
    insns = _decode(r.code)
    assert insns[0].ip_rel_memory_address == 0x400116
    assert insns[1].near_branch_target == HOOK + len(ORIGINAL)
    assert insns[2].near_branch_target == CAVE
    assert r.labels["end"] == CAVE + 6 + 5 + 5
    assert r.labels["mult"] == CAVE + 20
    assert r.code[-4:] == struct.pack("<f", 2.0)
    assert fa.calls[0][1] is CTX


def test_fake_reports_errors_and_failures():
    fa = FakeAssembler()
    assert fa.assemble("nop\nmov eax, 1", CTX).error == (2, "fake assembler can't do 'mov eax, 1'")
    assert fa.assemble("jmp nowhere", CTX).error == (1, "symbol `nowhere' not defined")
    fa.fail = (1, "boom")
    assert fa.assemble("nop", CTX).error == (1, "boom")


@pytest.mark.needs_nasm
def test_fake_and_nasm_agree_on_the_subset(nasm):
    src = "top:\n  original\n  db 0x90, 0x90\n  jmp strict near return\n"
    fake_src = "top:\n  original\n  db 0x90, 0x90\n  jmp return\n"
    assert (
        nasm.assemble(src, CTX, [MULT]).code == FakeAssembler().assemble(fake_src, CTX, [MULT]).code
    )


# -- bare numbers are module offsets (review M1) ---------------------------------------
MODULE = CaveContext(0x1F00, 0x100, ORIGINAL, module_size=0x2000)


@pytest.mark.parametrize(
    ("line", "bad"),
    [
        ("mov eax, [0x5000]", "0x5000"),
        ("mov eax, dword ptr [rel 3000h]", "3000h"),
        ("jmp 0x3000", "0x3000"),
        ("  jne short 0x2000", "0x2000"),
        ("lbl: call 8192", "8192"),
    ],
)
def test_numbers_outside_the_module_are_refused(line, bad):
    err = asm.check_numbers(f"nop\n{line}\n", 0x2000)
    assert err is not None and err[0] == 2 and err[1].startswith(f"{bad} is outside")
    assert FakeAssembler().assemble(f"nop\n{line}\n", MODULE).error == err


def test_numbers_inside_the_module_and_segment_operands_pass():
    src = "mov eax, [0x100]\njmp 0x1fff\nmov rax, fs:[0x28]\nmov rax, [fs:0x28]\n; [0x9999]"
    assert asm.check_numbers(src, 0x2000) is None
    assert asm.check_numbers("jmp 0x5000", None) is None
    assert shim_line("mov rax, fs:[0x28]", 0x1F00) == "mov rax, fs:[0x28]"


@pytest.mark.needs_nasm
def test_nasm_at_a_module_offset_org(nasm):
    src = "mov eax, [0x100]\ncall 0x200\noriginal\njmp return\n"
    r = nasm.assemble(src, MODULE)
    assert r.ok, r.error
    mov, call, orig, jmp = _decode(r.code, 0x1F00)[:4]
    assert mov.ip_rel_memory_address == 0x100 and call.near_branch_target == 0x200
    assert orig.ip_rel_memory_address == 0x116 and jmp.near_branch_target == 0x106
    assert nasm.assemble("nop\nmov eax, [0x2000]\n", MODULE).error[0] == 2
    fs = nasm.assemble("mov rax, fs:[0x28]\n", MODULE)
    assert fs.ok and _decode(fs.code, 0x1F00)[0].memory_displacement == 0x28


@pytest.mark.needs_nasm
def test_nasm_number_overflow_is_an_error(nasm):
    r = nasm.assemble("nop\nmov eax, 0x1ffffffff\n", MODULE)
    assert r.error is not None and r.error[0] == 2 and "exceeds" in r.error[1]
