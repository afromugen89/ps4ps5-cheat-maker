"""core/trace_flow: the code window before a writer and following the value back
(AC-T3), one rule per test, on code assembled with tests/asm.py."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core.trace_flow import (
    End,
    MemRef,
    decode_window,
    follow,
    text,
    writer_at,
)
from cheatmaker.core.trace_way import Kind
from cheatmaker.core.values import ValueType
from tests.asm import asm, call, ends, ins, jcc, listing, mem, rep_movsb, ret, rip

F, D = ValueType.FLOAT, ValueType.DOUBLE
I8, I16, I32, I64 = ValueType.INT8, ValueType.INT16, ValueType.INT32, ValueType.INT64
CODE = 0x400100
RODATA = 0x404000
DATA = 0x405000
K100 = RODATA + 0x10
KEY = RODATA + 0x20
SIGN = RODATA + 0x30  # 0x80000000: the sign bit of a float
GMAX = DATA + 0x30  # a writable global


class Memory:
    """Regions for ``read`` and ``is_const`` (read-only data only is constant)."""

    def __init__(self) -> None:
        self.regions: dict[int, bytearray] = {
            RODATA: bytearray(0x100),
            DATA: bytearray(0x100),
        }
        self.put(K100, struct.pack("<f", 100.0))
        self.put(KEY, struct.pack("<I", 0x5A5A5A5A))
        self.put(SIGN, struct.pack("<I", 0x80000000))

    def put(self, addr: int, data: bytes) -> None:
        for start, buf in self.regions.items():
            if start <= addr < start + len(buf):
                buf[addr - start : addr - start + len(data)] = data
                return
        raise KeyError(hex(addr))

    def read(self, addr: int, n: int) -> bytes:
        for start, buf in self.regions.items():
            if start <= addr and addr + n <= start + len(buf):
                return bytes(buf[addr - start : addr - start + n])
        raise KeyError(hex(addr))

    @staticmethod
    def is_const(addr: int) -> bool:
        return RODATA <= addr < RODATA + 0x100


def window(*insns, pad: bytes = b"\xcc" * 16):
    """Code laid out at CODE with int3 padding before it, as a game's functions are."""
    code = asm(CODE, *insns)
    writer_ip = [CODE, *ends(code, CODE)][-2]
    return decode_window(pad + code, writer_ip, CODE + len(code) - writer_ip), code


def trace(*insns, shown=F, lane=0, mem_=None):
    win, _code = window(*insns)
    m = mem_ or Memory()
    return follow(win, lane, m.read, m.is_const, shown)


def test_window_starts_after_padding_and_ends_with_the_writer():
    win, code = window(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm0"),
    )
    assert listing(code, CODE) == ["movss xmm0,[rbx+10h]", "movss [rdi+40h],xmm0"]
    assert [i.ip for i in win.insns] == [CODE, ends(code, CODE)[0]]
    assert win.marker.startswith("int3")


def test_window_resynchronises_after_a_misleading_start():
    body = [
        ins("MOV_R64_IMM64", "rax", 0x0F10C3_8B48C389),  # bytes that decode as other code
        ins("MOV_R64_RM64", "rbx", mem("rax", 8)),
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm0"),
    ]
    win, code = window(*body, pad=bytes.fromhex("4889c3") * 40)  # mov rbx,rax (no marker)
    assert [text(i) for i in win.insns[-4:]] == listing(code, CODE)
    assert all(text(i) == "mov rbx,rax" for i in win.insns[:-4])
    assert win.marker == "window start"


def test_block_ends_at_ret_and_jmp():
    code = asm(CODE, ret(), ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm1"))
    win = decode_window(b"\x90" * 8 + code, CODE + 1, len(code) - 1)
    assert len(win.insns) == 1 and win.marker == f"ret at {CODE:#x}"


def test_float_copy():
    p = trace(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm0"),
    )
    assert p.end is End.SOURCE and p.load.mem.text() == "[rbx+10h]" and p.load.lane == 0
    assert p.way.real is F and p.way.steps == () and p.way.label() == "as shown"
    assert p.followed == ("movss [rdi+40h],xmm0", "movss xmm0,[rbx+10h]")


def test_int_copy_through_another_register():
    p = trace(
        ins("MOV_R32_RM32", "eax", mem("rcx", 8)),
        ins("MOV_R32_RM32", "esi", "eax"),
        ins("MOV_RM32_R32", mem("rdx"), "esi"),
        shown=I32,
    )
    assert p.end is End.SOURCE and p.load.mem == MemRef("rcx", None, 1, 8, None, 4)
    assert p.way.real is I32 and p.load.at == 0


def test_cut_conversion():
    p = trace(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        shown=I32,
    )
    assert p.end is End.SOURCE and p.way.real is F
    [step] = p.way.steps
    assert step.kind is Kind.TO_WHOLE and step.rounding == "cut" and step.to is I32
    assert p.way.matches(87.9, 87)


def test_round_conversion_from_memory():
    p = trace(
        ins("CVTSS2SI_R32_XMMM32", "eax", mem("rbx", 0x10)),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        shown=I32,
    )
    assert p.end is End.SOURCE and p.way.rounding == "round" and p.load.mem.disp == 0x10


def test_x100_by_a_rip_relative_constant():
    p = trace(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(K100)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        shown=I32,
    )
    assert p.end is End.SOURCE
    assert [s.kind for s in p.way.steps] == [Kind.MUL, Kind.TO_WHOLE]
    assert p.way.factor() == 100 and p.way.label() == "x0.01 of the screen"


def test_constant_from_an_immediate_moved_into_xmm():
    p = trace(
        ins("MOV_R32_IMM32", "eax", struct.unpack("<I", struct.pack("<f", 100.0))[0]),
        ins("MOVD_XMM_RM32", "xmm1", "eax"),
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MULSS_XMM_XMMM32", "xmm0", "xmm1"),
        ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
    )
    assert p.end is End.SOURCE and p.way.factor() == 100


def test_vex_multiply_three_operands():
    p = trace(
        ins("VEX_VMOVSS_XMM_M32", "xmm1", mem("rbx", 0x10)),
        ins("VEX_VMULSS_XMM_XMM_XMMM32", "xmm0", "xmm1", rip(K100)),
        ins("VEX_VMOVSS_M32_XMM", mem("rdi"), "xmm0"),
    )
    assert p.end is End.SOURCE and p.way.factor() == 100 and p.load.mem.base == "rbx"


def test_share_of_a_loaded_max():
    p = trace(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("DIVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x14)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(K100)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        shown=I32,
    )
    assert p.end is End.SOURCE and p.load.mem.disp == 0x10
    assert [s.kind for s in p.way.steps] == [Kind.SHARE, Kind.MUL, Kind.TO_WHOLE]
    assert p.max_load.mem.text() == "[rbx+14h]"


def test_divide_by_a_writable_global_is_a_share_and_by_a_constant_a_div():
    p = trace(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("DIVSS_XMM_XMMM32", "xmm0", rip(GMAX)),
        ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
    )
    assert p.way.steps[0].kind is Kind.SHARE and p.max_load.mem.rip_target == GMAX
    p = trace(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("DIVSS_XMM_XMMM32", "xmm0", rip(K100)),
        ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
    )
    assert p.way.steps[0].kind is Kind.DIV and p.way.factor() == pytest.approx(0.01)


def test_two_loaded_values_multiplied_is_not_followed():
    p = trace(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MULSS_XMM_XMMM32", "xmm0", mem("rbx", 0x18)),
        ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
    )
    assert p.end is End.NOT_FOLLOWED and "two loaded values" in p.reason and p.way is None


def test_imul_by_an_immediate_and_int_to_decimal():
    p = trace(
        ins("IMUL_R32_RM32_IMM32", "eax", mem("rbx", 8), 100),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        shown=I32,
    )
    assert p.end is End.SOURCE and p.way.factor() == 100 and p.way.real is I32
    p = trace(
        ins("CVTSI2SS_XMM_RM32", "xmm0", mem("rbx", 8)), ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0")
    )
    assert p.end is End.SOURCE and p.way.real is I32 and p.way.steps[0].kind is Kind.TO_DECIMAL


def test_widen_and_extend():
    p = trace(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx")),
        ins("CVTSS2SD_XMM_XMMM32", "xmm0", "xmm0"),
        ins("MOVSD_XMMM64_XMM", mem("rdi"), "xmm0"),
        shown=D,
    )
    assert p.end is End.SOURCE and p.way.real is F and p.way.steps[0].kind is Kind.WIDEN
    p = trace(
        ins("MOVZX_R32_RM16", "eax", mem("rbx")), ins("MOV_RM32_R32", mem("rdi"), "eax"), shown=I32
    )
    assert p.way.real is I16 and not p.way.steps[0].signed and p.load.mem.size == 2
    p = trace(
        ins("MOV_R32_RM32", "eax", mem("rbx")), ins("MOV_RM64_R64", mem("rdi"), "rax"), shown=I64
    )
    assert p.way.real is I32 and p.way.steps[0].kind is Kind.EXTEND


def test_wider_whole_number_load_is_the_source_and_the_copy_its_low_bytes():
    p = trace(
        ins("MOV_R64_RM64", "rax", mem("rbx", 0x10)),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        shown=I32,
    )
    assert p.end is End.SOURCE and p.load.mem.size == 8
    assert p.way.real is I64 and [s.kind for s in p.way.steps] == [Kind.TRUNCATE]
    assert p.way.to_screen(2**32 + 87) == 87 and p.way.to_real(87) == 87
    assert p.way.label() == "as shown" and p.way.rounding == "exact"
    p = trace(
        ins("MOVZX_R32_RM16", "eax", mem("rbx")), ins("MOV_RM8_R8", mem("rdi"), "al"), shown=I8
    )
    assert p.way.real is I16 and [s.kind for s in p.way.steps] == [Kind.TRUNCATE]
    p = trace(  # a register move doesn't decide it: the load further back does
        ins("MOV_R64_RM64", "rcx", mem("rbx")),
        ins("MOV_R64_RM64", "rax", "rcx"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        shown=I32,
    )
    assert p.way.real is I64 and len(p.way.steps) == 1


def test_an_8_byte_copy_of_two_fields_keeps_the_watched_field_its_own_size():
    """``mov rax,[m] ; mov [d],rax`` copies all 8 bytes: the low 4 watched are just the
    low 4 of the source (hp next to maxhp), not an 8-byte number."""
    p = trace(
        ins("MOV_R64_RM64", "rax", mem("rbx", 0x10)),
        ins("MOV_RM64_R64", mem("rdi"), "rax"),
        shown=I32,
    )
    assert p.end is End.SOURCE and p.way.real is I32 and p.way.steps == ()
    p = trace(  # a narrower register move on the way: the copy got 4 bytes
        ins("MOV_R64_RM64", "rax", mem("rbx", 0x10)),
        ins("MOV_R32_RM32", "ecx", "eax"),
        ins("MOV_RM64_R64", mem("rdi"), "rcx"),
        shown=I32,
    )
    assert p.way.real is I64 and [s.kind for s in p.way.steps] == [Kind.TRUNCATE]
    p = trace(  # movzx from 2 bytes into a 4-byte store, the low byte watched
        ins("MOVZX_R32_RM16", "eax", mem("rbx")), ins("MOV_RM32_R32", mem("rdi"), "eax"), shown=I8
    )
    assert p.way.real is I8 and p.way.steps == ()


def test_store_of_a_high_byte_register_is_byte_1():
    p = trace(
        ins("MOV_R32_RM32", "eax", mem("rbx", 0x10)), ins("MOV_RM8_R8", mem("rdi"), "ah"), shown=I8
    )
    assert p.end is End.SOURCE and p.load.lane == 1 and p.way.real is I8


def test_conversion_that_doesnt_fit_the_copy_type():
    p = trace(
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        shown=F,
    )
    assert p.end is End.NOT_FOLLOWED and "FLOAT" in p.reason


def test_vector_lane():
    p = trace(
        ins("VEX_VMOVUPS_XMM_XMMM128", "xmm0", mem("rsi")),
        ins("VEX_VMOVUPS_XMMM128_XMM", mem("rdi"), "xmm0"),
        lane=8,
    )
    assert p.end is End.SOURCE and p.load.lane == 8 and p.load.mem.base == "rsi"


def test_movss_register_move_keeps_the_upper_lanes():
    p = trace(
        ins("MOVUPS_XMM_XMMM128", "xmm0", mem("rsi")),
        ins("MOVSS_XMM_XMMM32", "xmm0", "xmm3"),
        ins("MOVUPS_XMMM128_XMM", mem("rdi"), "xmm0"),
        lane=4,
    )
    assert p.end is End.SOURCE and p.load.lane == 4 and p.load.mem.base == "rsi"


@pytest.mark.parametrize(
    ("insns", "end"),
    [
        ([ins("MOV_RM32_IMM32", mem("rdi"), 5)], End.CONSTANT),
        ([ins("MOV_R32_IMM32", "eax", 5), ins("MOV_RM32_R32", mem("rdi"), "eax")], End.CONSTANT),
        ([ins("XOR_R32_RM32", "eax", "eax"), ins("MOV_RM32_R32", mem("rdi"), "eax")], End.CONSTANT),
        (
            [
                ins("MOV_R32_RM32", "eax", mem("rbx", 0x10)),
                ins("XOR_R32_RM32", "eax", rip(KEY)),
                ins("MOV_RM32_R32", mem("rdi"), "eax"),
            ],
            End.SCRAMBLED,
        ),
        ([ins("NOT_RM32", "eax"), ins("MOV_RM32_R32", mem("rdi"), "eax")], End.SCRAMBLED),
        ([ins("SUB_RM32_R32", mem("rax", 0x2B0), "ecx")], End.ALREADY_REAL),
        (
            [
                ins("MOV_R32_RM32", "eax", mem("rdi", 8)),
                ins("ADD_R32_RM32", "eax", "ecx"),
                ins("MOV_RM32_R32", mem("rdi", 8), "eax"),
            ],
            End.ALREADY_REAL,
        ),
        (
            [
                ins("MOV_R32_RM32", "eax", mem("rsi", 8)),
                ins("ADD_R32_RM32", "eax", "ecx"),
                ins("MOV_RM32_R32", mem("rdi", 8), "eax"),
            ],
            End.NOT_FOLLOWED,
        ),
        (
            [ins("LEA_R32_M", "eax", mem("rcx", 4)), ins("MOV_RM32_R32", mem("rdi"), "eax")],
            End.NOT_FOLLOWED,
        ),
    ],
)
def test_ends(insns, end):
    p = trace(*insns, shown=I32)
    assert p.end is end, p.reason
    assert p.way is None


def test_float_xor_with_a_sign_mask_is_a_negation_not_scrambling():
    p = trace(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx")),
        ins("XORPS_XMM_XMMM128", "xmm0", rip(SIGN)),
        ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
    )
    assert p.end is End.NOT_FOLLOWED and p.reason.endswith("flips its sign")
    p = trace(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx")),
        ins("XORPS_XMM_XMMM128", "xmm0", rip(KEY)),
        ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
    )
    assert p.end is End.SCRAMBLED


def test_not_followed_names_the_instruction():
    p = trace(
        ins("MOV_R32_RM32", "eax", mem("rsi", 8)),
        ins("ADD_R32_RM32", "eax", "ecx"),
        ins("MOV_RM32_R32", mem("rdi", 8), "eax"),
        shown=I32,
    )
    assert p.reason.startswith("add eax,ecx at 0x")


def test_calls():
    p = trace(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx")),
        call(0x400800),
        ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
    )
    assert p.end is End.CALL_RESULT
    p = trace(
        ins("MOV_R32_RM32", "ebx", mem("rcx")),
        call(0x400800),
        ins("MOV_RM32_R32", mem("rdi"), "ebx"),
        shown=I32,
    )
    assert p.end is End.SOURCE, "rbx survives a call"
    p = trace(
        ins("MOV_R32_RM32", "ecx", mem("rbx")),
        call(0x400800),
        ins("MOV_RM32_R32", mem("rdi"), "ecx"),
        shown=I32,
    )
    assert p.end is End.NOT_FOLLOWED and "a call changes it" in p.reason


def test_branch_inside_the_path():
    p = trace(
        ins("MOV_R32_RM32", "eax", mem("rbx")),
        jcc("JE_REL32_64", 0x400800),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        shown=I32,
    )
    assert p.end is End.BRANCH


def test_block_start_names_the_argument_register():
    p = trace(ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm1"))
    assert p.end is End.BLOCK_START and p.reg == "xmm1" and p.way.real is F
    p = trace(ins("MOV_R32_RM32", "eax", "esi"), ins("MOV_RM32_R32", mem("rdi"), "eax"), shown=I32)
    assert p.end is End.BLOCK_START and p.reg == "rsi"


def test_block_copy_and_writer_at():
    code = asm(CODE, ins("MOV_R32_RM32", "ecx", "edx"), rep_movsb())
    rep_ip = ends(code, CODE)[0]
    before = b"\xcc" * 16 + code[: rep_ip - CODE]
    # a trap mid-copy leaves RIP at the rep instruction
    assert writer_at(before, code[rep_ip - CODE :], rep_ip) == (
        rep_ip,
        b"\xf3\xa4",
        "rep movsb [rdi],[rsi]",
    )
    # an ordinary writer ends at RIP
    plain = asm(CODE, ins("MOV_RM32_R32", mem("rdi"), "eax"))
    assert writer_at(b"\xcc" * 8 + plain, b"\xcc" * 8, CODE + len(plain))[0] == CODE
    # a store right before the rep that wrote the watched address: the trap is its own
    store = asm(CODE, ins("MOV_RM32_R32", mem("rdi"), "eax"), rep_movsb())
    rep2 = ends(store, CODE)[0]
    head, tail = b"\xcc" * 16 + store[: rep2 - CODE], store[rep2 - CODE :]
    hit = WatchHit(rep2, {"rdi": 0x5000, "rip": rep2})
    assert writer_at(head, tail, rep2, hit, 0x5000)[0] == CODE
    assert writer_at(head, tail, rep2, hit, 0x6000)[0] == rep2, "stored elsewhere"
    assert writer_at(head, tail, rep2)[0] == rep2, "no trap registers: the rep"
    win = decode_window(b"\xcc" * 8 + code, rep_ip, 2)
    p = follow(win, 0, Memory().read, Memory.is_const, I32)
    assert p.end is End.BLOCK_COPY and p.way.real is I32
