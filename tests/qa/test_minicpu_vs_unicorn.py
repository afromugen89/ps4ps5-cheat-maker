"""Cross-check tests/minicpu.py against a real x86-64 emulator (unicorn): the same
instruction with random inputs must leave the same registers and status flags (flags
the Intel manual calls undefined are not compared).

unicorn isn't a project dependency: this file is skipped unless it is installed
(``pip install unicorn``), like test_capture_emulated.py.
"""

from __future__ import annotations

import random
import struct

import pytest

from tests.minicpu import AF, OF, PF, SF, STATUS, ZF, Cpu, FlatMemory, f32_bits, f64_bits

uc = pytest.importorskip("unicorn")
x86 = pytest.importorskip("unicorn.x86_const")

CODE, STACK = 0x10000, 0x80000
ARITH = {  # hex -> flags left undefined
    "01c8": 0,  # add eax,ecx
    "4801c8": 0,
    "29c8": 0,  # sub eax,ecx
    "4829c8": 0,
    "39c8": 0,  # cmp
    "4839c8": 0,
    "21c8": AF,  # and
    "09c8": AF,  # or
    "31c8": AF,  # xor
    "4885c8": AF,  # test rax,rcx
    "ffc0": 0,  # inc eax
    "48ffc8": 0,  # dec rax
    "f7d8": 0,  # neg eax
    "48f7d8": 0,
    "d1e8": AF,  # shr eax,1
    "48d1f8": AF,  # sar rax,1
    "d1e0": AF,  # shl eax,1
    "c1e805": AF | OF,  # shr eax,5
    "48c1e83f": AF | OF,  # shr rax,63
}
JCC = [f"{op:02x}02" for op in range(0x70, 0x80)]  # jcc +2
SSE = ["0f2ec1", "f30f58c1", "f30f59c1"]  # ucomiss, addss, mulss xmm0,xmm1
EDGE = [0, 1, 0x7F, 0x80, 0xFF, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF, 1 << 63, (1 << 64) - 1]
FLOATS = [0.0, -0.0, 1.0, -1.5, 3.4e38, -3.4e38, 1e-45, float("inf"), float("nan"), 0.1]


def _unicorn(code: bytes, regs: dict[str, int], rflags: int) -> tuple[dict[str, int], int]:
    mu = uc.Uc(uc.UC_ARCH_X86, uc.UC_MODE_64)
    mu.mem_map(CODE, 0x1000)
    mu.mem_map(STACK - 0x1000, 0x2000)
    mu.mem_write(CODE, code + b"\x90\x90\x90")
    mu.reg_write(x86.UC_X86_REG_RSP, STACK)
    for name, value in regs.items():
        mu.reg_write(getattr(x86, f"UC_X86_REG_{name.upper()}"), value)
    mu.reg_write(x86.UC_X86_REG_EFLAGS, rflags)
    mu.emu_start(CODE, CODE + len(code), count=1)
    out = {n: mu.reg_read(getattr(x86, f"UC_X86_REG_{n.upper()}")) for n in regs}
    out["rip"] = mu.reg_read(x86.UC_X86_REG_RIP)
    return out, mu.reg_read(x86.UC_X86_REG_EFLAGS)


def _mini(code: bytes, regs: dict[str, int], rflags: int) -> tuple[dict[str, int], int]:
    mem = FlatMemory()
    mem.map(CODE, code + b"\x90\x90\x90")
    mem.map(STACK - 0x1000, 0x2000)
    cpu = Cpu(mem, rflags=rflags)
    cpu["rsp"] = STACK
    for name, value in regs.items():
        cpu[name] = value
    cpu.rip = CODE
    cpu.step()
    out = {n: cpu[n] for n in regs}
    out["rip"] = cpu.rip
    return out, cpu.rflags


def _same(hexcode: str, regs: dict[str, int], rflags: int, undefined: int = 0) -> None:
    code = bytes.fromhex(hexcode)
    want, wf = _unicorn(code, regs, rflags)
    got, gf = _mini(code, regs, rflags)
    assert got == want, hexcode
    mask = STATUS & ~undefined
    assert gf & mask == wf & mask, f"{hexcode} {regs} flags {gf:#x} != {wf:#x}"


@pytest.mark.parametrize("hexcode", ARITH)
def test_integer_flags_match(hexcode):
    rnd = random.Random(hexcode)
    for _ in range(300):
        a = rnd.choice(EDGE) if rnd.random() < 0.3 else rnd.getrandbits(64)
        b = rnd.choice(EDGE) if rnd.random() < 0.3 else rnd.getrandbits(64)
        _same(hexcode, {"rax": a, "rcx": b}, 0x202 | rnd.getrandbits(12) & STATUS, ARITH[hexcode])


@pytest.mark.parametrize("hexcode", JCC)
def test_conditions_match(hexcode):
    for flags in range(0, 0x1000, 0x11):
        _same(hexcode, {"rax": 0}, 0x202 | (flags & STATUS))


@pytest.mark.parametrize("hexcode", SSE)
def test_sse_matches(hexcode):
    rnd = random.Random(hexcode)
    for _ in range(300):
        a = rnd.choice(FLOATS) if rnd.random() < 0.5 else rnd.uniform(-1e6, 1e6)
        b = rnd.choice(FLOATS) if rnd.random() < 0.5 else rnd.uniform(-1e6, 1e6)
        hi = rnd.getrandbits(96) << 32
        regs = {"xmm0": hi | f32_bits(a), "xmm1": f32_bits(b)}
        if a != a or b != b:  # NaN payloads: only the flags are compared
            regs = {"xmm0": f32_bits(a), "xmm1": f32_bits(b)}
            if hexcode != "0f2ec1":
                continue
        _same(hexcode, regs, 0x202 | rnd.getrandbits(12) & STATUS)


def test_float_rounding_matches_for_halves():
    for bits in (0x3F800001, 0x3F800003, 0x00000001, 0x7F7FFFFF):
        half = struct.unpack("<I", struct.pack("<f", 0.5))[0]
        _same("f30f59c1", {"xmm0": bits, "xmm1": half}, 0x202)


# The trace fixtures' game code: hex -> (operands are float32 "s" / float64 "d", flags
# left undefined).
TRACE = {
    "f30f2cc0": ("s", 0),  # cvttss2si eax,xmm0
    "f3480f2cc0": ("s", 0),  # cvttss2si rax,xmm0
    "f30f2dc0": ("s", 0),  # cvtss2si eax,xmm0
    "f20f2cc0": ("d", 0),  # cvttsd2si eax,xmm0
    "f20f2dc0": ("d", 0),  # cvtsd2si eax,xmm0
    "f30f2ac1": ("s", 0),  # cvtsi2ss xmm0,ecx
    "f20f2ac1": ("d", 0),  # cvtsi2sd xmm0,ecx
    "f30f5ac1": ("s", 0),  # cvtss2sd xmm0,xmm1
    "f20f5ac1": ("d", 0),  # cvtsd2ss xmm0,xmm1
    "f30f5ec1": ("s", 0),  # divss xmm0,xmm1
    "f20f59c1": ("d", 0),  # mulsd xmm0,xmm1
    "f20f5ec1": ("d", 0),  # divsd xmm0,xmm1
    "f20f10c1": ("d", 0),  # movsd xmm0,xmm1
    "66480f7ec0": ("d", 0),  # movq rax,xmm0
    "66480f6ec1": ("d", 0),  # movq xmm0,rcx
    "0f14c1": ("s", 0),  # unpcklps xmm0,xmm1
    "0fb6c1": ("s", 0),  # movzx eax,cl
    "0fbfc1": ("s", 0),  # movsx eax,cx
    "4863c1": ("s", 0),  # movsxd rax,ecx
    "6bc164": ("s", SF | ZF | AF | PF),  # imul eax,ecx,100
    "4869c140420f00": ("s", SF | ZF | AF | PF),  # imul rax,rcx,1000000
}
DOUBLES = [0.0, -0.0, 1.0, -2.5, 1e308, -1e308, 5e-324, float("inf"), float("nan"), 0.87]


@pytest.mark.parametrize("hexcode", TRACE)
def test_trace_forms_match(hexcode):
    width, undefined = TRACE[hexcode]
    rnd = random.Random(hexcode)
    for _ in range(300):
        if width == "s":
            pick = lambda: rnd.choice(FLOATS) if rnd.random() < 0.5 else rnd.uniform(-1e10, 1e10)  # noqa: E731
            a, b = f32_bits(pick()), f32_bits(pick())
        else:
            pick = lambda: rnd.choice(DOUBLES) if rnd.random() < 0.5 else rnd.uniform(-1e19, 1e19)  # noqa: E731
            a, b = f64_bits(pick()), f64_bits(pick())
        hi = rnd.getrandbits(64) << 64
        regs = {
            "rax": rnd.getrandbits(64),
            "rcx": rnd.choice(EDGE) if rnd.random() < 0.3 else rnd.getrandbits(64),
            "xmm0": hi | a,
            "xmm1": (rnd.getrandbits(64) << 64) | b,
        }
        if _nan(regs, width) and hexcode not in NAN_SAFE:
            continue
        _same(hexcode, regs, 0x202 | rnd.getrandbits(12) & STATUS, undefined)


NAN_SAFE = {"f30f2cc0", "f3480f2cc0", "f30f2dc0", "f20f2cc0", "f20f2dc0", "f20f10c1"}
"""Forms whose result with a NaN input doesn't depend on the NaN's payload."""


def _nan(regs: dict[str, int], width: str) -> bool:
    import math
    import struct as st

    for name in ("xmm0", "xmm1"):
        v = regs[name]
        x = (
            st.unpack("<f", st.pack("<I", v & 0xFFFFFFFF))[0]
            if width == "s"
            else st.unpack("<d", st.pack("<Q", v & (1 << 64) - 1))[0]
        )
        if math.isnan(x):
            return True
    return False
