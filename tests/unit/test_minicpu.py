"""tests/minicpu.py itself: flags, conditions, stack and SSE match the Intel manual, and
anything outside its closed set refuses to run."""

from __future__ import annotations

import struct

import pytest

from tests.minicpu import (
    AF,
    CF,
    OF,
    PF,
    SF,
    STATUS,
    ZF,
    Cpu,
    CpuFault,
    FlatMemory,
    Unsupported,
    bits_f32,
    f32_bits,
)

CODE, DATA, STACK = 0x1000, 0x8000, 0x7_0000


def run(hexcode: str, **regs: int) -> Cpu:
    code = bytes.fromhex(hexcode)
    mem = FlatMemory()
    mem.map(CODE, code)
    mem.map(DATA, 0x100)
    mem.map(STACK - 0x1000, 0x2000)
    cpu = Cpu(mem)
    cpu["rsp"] = STACK
    for name, value in regs.items():
        if name == "rflags":
            cpu.rflags = value
        else:
            cpu[name] = value
    return cpu.run(CODE, CODE + len(code))


@pytest.mark.parametrize(
    ("hexcode", "regs", "result", "flags"),
    [
        ("29c8", {"eax": 5, "ecx": 7}, 0xFFFFFFFE, CF | SF | AF),  # sub eax,ecx
        ("29c8", {"eax": 0x80000000, "ecx": 1}, 0x7FFFFFFF, OF | AF | PF),
        ("29c8", {"eax": 7, "ecx": 7}, 0, ZF | PF),
        ("01c8", {"eax": 0x7FFFFFFF, "ecx": 1}, 0x80000000, OF | SF | AF | PF),  # add
        ("01c8", {"eax": 0xFFFFFFFF, "ecx": 1}, 0, CF | ZF | PF | AF),
        ("4801c8", {"rax": 1 << 63, "rcx": 1 << 63}, 0, CF | OF | ZF | PF),  # add rax,rcx
        ("39c8", {"eax": 3, "ecx": 3}, 3, ZF | PF),  # cmp leaves eax
        ("f7d8", {"eax": 0}, 0, ZF | PF),  # neg eax
        ("f7d8", {"eax": 5}, 0xFFFFFFFB, CF | SF | AF),
        ("d1e8", {"eax": 3}, 1, CF),  # shr eax,1
        ("c1e804", {"eax": 0x1F}, 1, CF),  # shr eax,4
        ("21c8", {"eax": 0xF0, "ecx": 0x3C}, 0x30, PF),  # and eax,ecx
    ],
)
def test_arithmetic_flags(hexcode, regs, result, flags):
    cpu = run(hexcode, **regs)
    assert cpu["rax"] == result and cpu.status() == flags


def test_inc_and_dec_keep_the_carry():
    cpu = run("ffc0", eax=0xFFFFFFFF, rflags=0x202 | CF)  # inc eax
    assert cpu["eax"] == 0 and cpu.status() == CF | ZF | PF | AF
    cpu = run("ffc8", eax=0x80000000)  # dec eax
    assert cpu["eax"] == 0x7FFFFFFF and cpu.status() == OF | AF | PF


def test_register_widths():
    cpu = run("89c8", rax=0xFFFF_FFFF_FFFF_FFFF, ecx=5)  # mov eax,ecx clears the top half
    assert cpu["rax"] == 5
    cpu = run("88c8", rax=0x1122_3344, ecx=0xAB)  # mov al,cl keeps the rest
    assert cpu["rax"] == 0x1122_33AB
    cpu = run("48c7c0ffffffff")  # mov rax,-1 (imm32 sign-extended)
    assert cpu["rax"] == (1 << 64) - 1


@pytest.mark.parametrize(
    ("op", "flags", "taken"),
    [
        ("70", OF, True),
        ("71", OF, False),
        ("72", CF, True),
        ("73", CF, False),
        ("74", ZF, True),
        ("75", ZF, False),
        ("76", ZF, True),
        ("77", 0, True),
        ("78", SF, True),
        ("79", SF, False),
        ("7a", PF, True),
        ("7b", PF, False),
        ("7c", SF, True),  # jl: SF != OF
        ("7c", SF | OF, False),
        ("7d", SF | OF, True),  # jge
        ("7e", ZF, True),  # jle
        ("7e", OF, True),
        ("7f", 0, True),  # jg
        ("7f", ZF, False),
    ],
)
def test_conditional_jumps(op, flags, taken):
    code = bytes.fromhex(op + "02") + b"\x90\x90"  # jcc +2 ; nop ; nop
    mem = FlatMemory()
    mem.map(CODE, code)
    cpu = Cpu(mem, rflags=0x202 | flags)
    cpu.rip = CODE
    cpu.step()
    assert (cpu.rip == CODE + 4) is taken


def test_stack_and_flags_round_trip():
    # lea rsp,[rsp-80h] ; pushfq ; push rax ; mov eax,1 ; add eax,eax ; pop rax ; popfq ;
    # lea rsp,[rsp+80h]
    code = "".join(["488d642480", "9c", "50", "b801000000", "01c0", "58", "9d", "488da42480000000"])
    cpu = run(code, rax=0x1234, rflags=0x202 | CF | ZF)
    assert cpu["rax"] == 0x1234 and cpu["rsp"] == STACK and cpu.status() == CF | ZF
    with pytest.raises(CpuFault):
        run("ff3425" + struct.pack("<I", 0x9000_0000).hex())  # push qword [9000_0000h]


def test_push_memory_and_pop():
    mem = FlatMemory()
    code = bytes.fromhex("ff3425" + struct.pack("<I", DATA).hex() + "59")  # push [DATA]; pop rcx
    mem.map(CODE, code)
    mem.map(DATA, struct.pack("<Q", 0xDEAD_BEEF_0000_0001))
    mem.map(STACK - 0x100, 0x200)
    cpu = Cpu(mem)
    cpu["rsp"] = STACK
    cpu.run(CODE, CODE + len(code))
    assert cpu["rcx"] == 0xDEAD_BEEF_0000_0001 and cpu["rsp"] == STACK


def test_sse_scalar_ops():
    one, two = f32_bits(1.5), f32_bits(2.25)
    cpu = run("f30f58c1", xmm0=(7 << 64) | one, xmm1=two)  # addss xmm0,xmm1
    assert bits_f32(cpu["xmm0"] & 0xFFFFFFFF) == 3.75 and cpu["xmm0"] >> 64 == 7
    cpu = run("f30f59c1", xmm0=one, xmm1=two)  # mulss
    assert bits_f32(cpu["xmm0"]) == 3.375
    cpu = run("f30f59c1", xmm0=f32_bits(3e38), xmm1=f32_bits(10.0))
    assert bits_f32(cpu["xmm0"]) == float("inf"), "float32 overflow"
    cpu = run("66 0f 6e c8".replace(" ", ""), eax=two, xmm1=(1 << 100))  # movd xmm1,eax
    assert cpu["xmm1"] == two
    cpu = run("660f7ec8", xmm1=(5 << 32) | two)  # movd eax,xmm1
    assert cpu["rax"] == two


@pytest.mark.parametrize(
    ("a", "b", "flags"),
    [
        (1.0, 2.0, CF),
        (2.0, 1.0, 0),
        (2.0, 2.0, ZF),
        (float("nan"), 2.0, ZF | PF | CF),
        (-0.0, 0.0, ZF),
    ],
)
def test_ucomiss(a, b, flags):
    cpu = run("0f2ec1", xmm0=f32_bits(a), xmm1=f32_bits(b), rflags=0x202 | OF | SF | AF)
    assert cpu.status() == flags


def test_movss_forms():
    mem = FlatMemory()
    # movss xmm0,[DATA] ; movss [DATA+4],xmm1 ; movss xmm2,xmm1 ; movups [DATA+16],xmm2
    code = bytes.fromhex(
        "f30f100425"
        + struct.pack("<I", DATA).hex()
        + "f30f110c25"
        + struct.pack("<I", DATA + 4).hex()
        + "f30f10d1"
        + "0f111425"
        + struct.pack("<I", DATA + 16).hex()
    )
    mem.map(CODE, code)
    mem.map(DATA, struct.pack("<f", 9.0) + bytes(60))
    cpu = Cpu(mem)
    cpu["xmm0"] = (1 << 127) | 5
    cpu["xmm1"] = f32_bits(4.0)
    cpu["xmm2"] = 0xAAAA << 64
    cpu.run(CODE, CODE + len(code))
    assert cpu["xmm0"] == f32_bits(9.0), "a load clears the rest"
    assert struct.unpack("<f", mem.read(DATA + 4, 4))[0] == 4.0
    assert cpu["xmm2"] == (0xAAAA << 64) | f32_bits(4.0), "register move keeps the rest"
    assert mem.read(DATA + 16, 16) == cpu["xmm2"].to_bytes(16, "little")


def test_vex_store_of_the_returnal_writer():
    mem = FlatMemory()
    code = bytes.fromhex("C4C17A118C24AC020000")  # vmovss [r12+2ACh],xmm1
    mem.map(CODE, code)
    mem.map(DATA, 0x400)
    cpu = Cpu(mem)
    cpu["r12"], cpu["xmm1"] = DATA, f32_bits(42.0)
    cpu.run(CODE, CODE + len(code))
    assert struct.unpack("<f", mem.read(DATA + 0x2AC, 4))[0] == 42.0


@pytest.mark.parametrize(
    "hexcode",
    [
        "0fa2",  # cpuid
        "ffe0",  # jmp rax
        "f00fc108",  # lock xadd
        "f3aa",  # rep stosb
        "88e0",  # mov al,ah: high byte registers
        "c5fa10c1",  # vmovss xmm0,xmm0,xmm1 (three operands)
    ],
)
def test_unknown_instructions_raise(hexcode):
    with pytest.raises(Unsupported):
        run(hexcode, rax=DATA)


def test_faults():
    with pytest.raises(CpuFault, match="still running"):
        run("ebfe")  # jmp $
    with pytest.raises(CpuFault, match="unmapped"):
        run("8908", rax=0x10)  # mov [rax],ecx
    mem = FlatMemory()
    mem.map(CODE, b"\x06")  # not valid in 64-bit mode
    with pytest.raises(CpuFault, match="no valid"):
        Cpu(mem).run(CODE, CODE + 1)


def test_snapshot_and_trace():
    cpu = run("89c8", ecx=3)
    snap = cpu.snapshot()
    assert snap["rax"] == 3 and snap["rsp"] == STACK and "xmm15" in snap
    assert cpu.trace == ["mov eax,ecx"] and cpu.flag("ZF") is False and STATUS & ZF


# -- the trace fixtures' game code (closed-set extension) -----------------------------
def _code(*insns) -> str:
    from tests.asm import asm

    return asm(CODE, *insns).hex()


def test_extensions():
    from tests.asm import ins

    cpu = run(_code(ins("MOVZX_R32_RM8", "eax", "cl")), rax=-1, ecx=0x80)
    assert cpu["rax"] == 0x80
    cpu = run(_code(ins("MOVSX_R32_RM16", "eax", "cx")), ecx=0x8001)
    assert cpu["rax"] == 0xFFFF8001
    cpu = run(_code(ins("MOVSXD_R64_RM32", "rax", "ecx")), ecx=0xFFFFFFFE)
    assert cpu["rax"] == (1 << 64) - 2


def test_imul_by_an_immediate():
    from tests.asm import ins

    cpu = run(_code(ins("IMUL_R32_RM32_IMM32", "eax", "ecx", 100)), ecx=87)
    assert cpu["rax"] == 8700 and cpu.status() == 0
    cpu = run(_code(ins("IMUL_R32_RM32_IMM32", "eax", "ecx", -2)), ecx=0x40000000)
    assert cpu["eax"] == 0x80000000 and cpu.status() == 0  # -2**31 fits
    cpu = run(_code(ins("IMUL_R32_RM32_IMM32", "eax", "ecx", 4)), ecx=0x40000000)
    assert cpu["eax"] == 0 and cpu.status() == CF | OF


@pytest.mark.parametrize(
    ("code", "x", "want"),
    [
        ("CVTTSS2SI_R32_XMMM32", 87.9, 87),
        ("CVTTSS2SI_R32_XMMM32", -87.9, -87 & 0xFFFFFFFF),
        ("CVTSS2SI_R32_XMMM32", 87.5, 88),
        ("CVTSS2SI_R32_XMMM32", 86.5, 86),  # halves to even
        ("CVTTSS2SI_R32_XMMM32", 3e9, 0x80000000),  # out of range: indefinite
        ("CVTTSS2SI_R32_XMMM32", float("nan"), 0x80000000),
        ("CVTTSS2SI_R64_XMMM32", 3e9, 3000000000),
    ],
)
def test_float_to_whole(code, x, want):
    from tests.asm import ins

    dst = "rax" if "R64" in code else "eax"
    cpu = run(_code(ins(code, dst, "xmm0")), xmm0=f32_bits(x))
    assert cpu["rax"] == want


def test_double_conversions_and_maths():
    from tests.asm import ins
    from tests.minicpu import bits_f64, f64_bits

    cpu = run(_code(ins("CVTTSD2SI_R32_XMMM64", "eax", "xmm0")), xmm0=f64_bits(51.99))
    assert cpu["rax"] == 51
    cpu = run(_code(ins("CVTSD2SI_R64_XMMM64", "rax", "xmm0")), xmm0=f64_bits(-2.5))
    assert cpu["rax"] == (1 << 64) - 2
    cpu = run(_code(ins("CVTSI2SS_XMM_RM32", "xmm0", "ecx")), ecx=-3 & 0xFFFFFFFF, xmm0=7 << 64)
    assert bits_f32(cpu["xmm0"]) == -3.0 and cpu["xmm0"] >> 64 == 7, "the rest stays"
    cpu = run(_code(ins("CVTSI2SD_XMM_RM64", "xmm0", "rcx")), rcx=1 << 40)
    assert bits_f64(cpu["xmm0"]) == float(1 << 40)
    cpu = run(_code(ins("CVTSS2SD_XMM_XMMM32", "xmm0", "xmm1")), xmm1=f32_bits(0.1))
    assert bits_f64(cpu["xmm0"]) == bits_f32(f32_bits(0.1))
    cpu = run(_code(ins("CVTSD2SS_XMM_XMMM64", "xmm0", "xmm1")), xmm1=f64_bits(0.1))
    assert bits_f32(cpu["xmm0"]) == bits_f32(f32_bits(0.1))
    cpu = run(
        _code(ins("MULSD_XMM_XMMM64", "xmm0", "xmm1")), xmm0=f64_bits(0.87), xmm1=f64_bits(100)
    )
    assert bits_f64(cpu["xmm0"]) == 0.87 * 100
    cpu = run(_code(ins("DIVSD_XMM_XMMM64", "xmm0", "xmm1")), xmm0=f64_bits(1), xmm1=f64_bits(0))
    assert bits_f64(cpu["xmm0"]) == float("inf")
    cpu = run(_code(ins("DIVSS_XMM_XMMM32", "xmm0", "xmm1")), xmm0=f32_bits(87), xmm1=f32_bits(100))
    assert bits_f32(cpu["xmm0"]) == bits_f32(f32_bits(0.87))
    cpu = run(_code(ins("DIVSS_XMM_XMMM32", "xmm0", "xmm1")), xmm0=0, xmm1=0)
    assert bits_f32(cpu["xmm0"]) != bits_f32(cpu["xmm0"]), "0/0 is NaN"


def test_moves_of_8_and_16_bytes():
    from tests.asm import ins, mem

    mem_ = FlatMemory()
    code = bytes.fromhex(
        _code(
            ins("MOVSD_XMM_XMMM64", "xmm0", mem("rax")),
            ins("MOVSD_XMM_XMMM64", "xmm1", "xmm0"),
            ins("MOVSD_XMMM64_XMM", mem("rax", 8), "xmm2"),
            ins("MOVQ_RM64_XMM", "rcx", "xmm2"),
            ins("MOVQ_XMM_RM64", "xmm3", "rcx"),
            ins("MOVAPS_XMM_XMMM128", "xmm4", mem("rax", 0x10)),
            ins("MOVDQU_XMMM128_XMM", mem("rax", 0x20), "xmm4"),
            ins("VEX_VMOVUPS_XMM_XMMM128", "xmm5", mem("rax", 0x18)),
            ins("UNPCKLPS_XMM_XMMM128", "xmm6", "xmm7"),
        )
    )
    mem_.map(CODE, code)
    data = struct.pack("<Q", 0x1111) + bytes(8) + bytes(range(16)) + bytes(0x20)
    mem_.map(DATA, data)
    cpu = Cpu(mem_)
    cpu["rax"] = DATA
    cpu["xmm0"] = (9 << 100) | 5
    cpu["xmm1"] = (3 << 64) | 4
    cpu["xmm2"] = (8 << 64) | 0x2222
    cpu["xmm3"] = 1 << 120
    cpu["xmm6"] = (4 << 32) | 3
    cpu["xmm7"] = (2 << 32) | 1
    cpu.run(CODE, CODE + len(code))
    assert cpu["xmm0"] == 0x1111, "a load clears the rest"
    assert cpu["xmm1"] == (3 << 64) | 0x1111, "register move keeps the rest"
    assert mem_.read(DATA + 8, 8) == struct.pack("<Q", 0x2222)
    assert cpu["rcx"] == 0x2222 and cpu["xmm3"] == 0x2222
    assert cpu["xmm4"] == int.from_bytes(bytes(range(16)), "little")
    assert mem_.read(DATA + 0x20, 16) == bytes(range(16))
    assert cpu["xmm5"] == int.from_bytes(mem_.read(DATA + 0x18, 16), "little")
    assert cpu["xmm6"] == 3 | 1 << 32 | 4 << 64 | 2 << 96


def test_movaps_faults_on_misaligned_memory():
    from tests.asm import ins, mem

    with pytest.raises(CpuFault, match="misaligned"):
        run(_code(ins("MOVAPS_XMM_XMMM128", "xmm0", mem("rax", 4))), rax=DATA)


def test_call_and_ret():
    from tests.asm import asm, call, ins, ret

    callee = CODE + 0x40
    main = asm(CODE, ins("MOV_R32_IMM32", "eax", 1), call(callee), ins("ADD_RM32_IMM8", "eax", 2))
    body = asm(callee, ins("MOV_R32_IMM32", "ecx", 5), ret())
    mem_ = FlatMemory()
    mem_.map(CODE, main + bytes(0x40 - len(main)) + body)
    mem_.map(STACK - 0x1000, 0x2000)
    cpu = Cpu(mem_)
    cpu["rsp"] = STACK
    cpu.run(CODE, CODE + len(main))
    assert cpu["eax"] == 3 and cpu["ecx"] == 5 and cpu["rsp"] == STACK
    assert cpu.trace == ["mov eax,1", f"call {callee:016X}h", "mov ecx,5", "ret", "add eax,2"]


@pytest.mark.parametrize(
    "hexcode",
    [
        "f3a4",  # rep movsb
        "a5",  # movsd (the string one)
        "f7e9",  # imul ecx (one operand)
        "0fafc1",  # imul eax,ecx (two operands)
        "c20800",  # ret 8
    ],
)
def test_unknown_trace_forms_raise(hexcode):
    with pytest.raises(Unsupported):
        run(hexcode, rax=DATA, rsp=STACK)
