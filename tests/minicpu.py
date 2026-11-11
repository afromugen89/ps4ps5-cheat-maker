"""A tiny x86-64 interpreter for tests: runs our generated routines (and the game
instructions they hook) one instruction at a time, using iced-x86 to decode.

It knows a closed set of instructions: what ``cave.py`` emits (``lea``, ``push``,
``pop``, ``pushfq``, ``popfq``, ``mov``, ``cmp``, ``sub``, ``add``, ``inc``, ``neg``,
``shr``, ``and``, jumps, ``movss``, ``movups``, ``movd``, ``ucomiss``, ``addss``,
``mulss``) plus the game writer forms the fakes use (``vmovss``, ``sub [mem],reg``,
``mov [mem],reg``) and the game code the trace fixtures run (``movzx``, ``movsx``,
``movsxd``, ``imul r,r/m,imm``, the ``cvt*`` conversions between whole numbers,
float and double, ``divss``, ``mulsd``, ``divsd``, SSE ``movsd``, ``movq``,
``movaps``, ``movdqu``/``vmovups`` (128-bit), ``unpcklps``, direct ``call``, ``ret``).
Anything else raises ``Unsupported`` (``rep movs`` too), so a routine can't pass a
test by using an instruction the interpreter would get wrong. Flags follow the Intel
manual for the six status flags (undefined ones are left at 0).

Decimal maths runs in Python (doubles) and is rounded to float32 once at the end: for
``addss``/``mulss``/``divss`` of two float32 values that is the same result as the
CPU's single rounding (a double holds more than twice float32's precision). MXCSR
isn't modelled: round-to-nearest only, and no FTZ/DAZ (a game that sets them would
see denormals flushed where minicpu keeps them). ``cvtsi2ss`` of a 64-bit whole number
above 2**53 rounds twice (through a double) and can be one float32 step off.

unicorn (tests/qa/test_capture_emulated.py) is the slower, optional cross-check.
"""

from __future__ import annotations

import ctypes
import math
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import iced_x86 as ix

CF, PF, AF, ZF, SF, OF = 0x1, 0x4, 0x10, 0x40, 0x80, 0x800
STATUS = CF | PF | AF | ZF | SF | OF
FLAG_BITS = {"CF": CF, "PF": PF, "AF": AF, "ZF": ZF, "SF": SF, "OF": OF}
_FMT = ix.Formatter(ix.FormatterSyntax.INTEL)
_HIGH8 = {ix.Register.AH, ix.Register.CH, ix.Register.DH, ix.Register.BH}
_IMM = {
    ix.OpKind.IMMEDIATE8,
    ix.OpKind.IMMEDIATE16,
    ix.OpKind.IMMEDIATE32,
    ix.OpKind.IMMEDIATE64,
    ix.OpKind.IMMEDIATE8TO16,
    ix.OpKind.IMMEDIATE8TO32,
    ix.OpKind.IMMEDIATE8TO64,
    ix.OpKind.IMMEDIATE32TO64,
}
M = ix.Mnemonic


class CpuFault(Exception):
    """The routine did something a real CPU would fault on (or ran away)."""


class Unsupported(CpuFault):
    """An instruction minicpu doesn't know."""


class Memory(Protocol):
    def read(self, addr: int, size: int) -> bytes: ...

    def write(self, addr: int, data: bytes) -> None: ...


class FlatMemory:
    """Separate mapped ranges; touching anything else faults."""

    def __init__(self) -> None:
        self._regions: list[tuple[int, bytearray]] = []

    def map(self, start: int, data: bytes | int) -> None:
        self._regions.append((start, bytearray(data)))

    def _find(self, addr: int, size: int) -> tuple[bytearray, int]:
        for start, buf in self._regions:
            if start <= addr and addr + size <= start + len(buf):
                return buf, addr - start
        raise CpuFault(f"unmapped memory {addr:#x}+{size}")

    def read(self, addr: int, size: int) -> bytes:
        buf, off = self._find(addr, size)
        return bytes(buf[off : off + size])

    def write(self, addr: int, data: bytes) -> None:
        buf, off = self._find(addr, len(data))
        buf[off : off + len(data)] = data


def _mask(size: int) -> int:
    return (1 << (8 * size)) - 1


def _sign(value: int, size: int) -> bool:
    return bool(value >> (8 * size - 1) & 1)


def _parity(value: int) -> bool:
    return bin(value & 0xFF).count("1") % 2 == 0


def bits_f32(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits & 0xFFFFFFFF))[0]


def bits_f64(bits: int) -> float:
    return struct.unpack("<d", struct.pack("<Q", bits & _mask(8)))[0]


def f64_bits(value: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", value))[0]


def f32_bits(value: float) -> int:
    """Round to the nearest float32 (overflow gives inf, as the CPU does)."""
    return struct.unpack("<I", struct.pack("<f", ctypes.c_float(value).value))[0]


@dataclass
class Cpu:
    mem: Memory
    gpr: list[int] = field(default_factory=lambda: [0] * 16)
    xmm: list[int] = field(default_factory=lambda: [0] * 16)
    rflags: int = 0x202
    rip: int = 0
    steps: int = 0
    trace: list[str] = field(default_factory=list)

    # -- registers by name -------------------------------------------------------
    def __getitem__(self, name: str) -> int:
        return self._get_reg(getattr(ix.Register, name.upper()))

    def __setitem__(self, name: str, value: int) -> None:
        reg = getattr(ix.Register, name.upper())
        size = 16 if ix.RegisterExt.is_xmm(reg) else ix.RegisterExt.size(reg)
        self._set_reg(reg, value & _mask(size))

    def flag(self, name: str) -> bool:
        return bool(self.rflags & FLAG_BITS[name])

    def status(self) -> int:
        """The six status flags only."""
        return self.rflags & STATUS

    def snapshot(self) -> dict[str, int]:
        names = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp"]
        out = {n: self[n] for n in [*names, *(f"r{i}" for i in range(8, 16))]}
        out.update({f"xmm{i}": self.xmm[i] for i in range(16)})
        return out

    def _get_reg(self, reg: int) -> int:
        if ix.RegisterExt.is_xmm(reg):
            return self.xmm[ix.RegisterExt.number(reg)]
        if not ix.RegisterExt.is_gpr(reg) or reg in _HIGH8:
            raise Unsupported(f"register {reg}")
        n = ix.RegisterExt.full_register(reg) - ix.Register.RAX
        return self.gpr[n] & _mask(ix.RegisterExt.size(reg))

    def _set_reg(self, reg: int, value: int) -> None:
        if ix.RegisterExt.is_xmm(reg):
            self.xmm[ix.RegisterExt.number(reg)] = value & _mask(16)
            return
        if not ix.RegisterExt.is_gpr(reg) or reg in _HIGH8:
            raise Unsupported(f"register {reg}")
        n = ix.RegisterExt.full_register(reg) - ix.Register.RAX
        size = ix.RegisterExt.size(reg)
        if size >= 4:  # a 32-bit write clears the upper half
            self.gpr[n] = value & _mask(size)
        else:
            self.gpr[n] = (self.gpr[n] & ~_mask(size)) | (value & _mask(size))

    # -- memory ----------------------------------------------------------------
    def address(self, insn: ix.Instruction) -> int:
        if insn.memory_base == ix.Register.RIP:
            return insn.ip_rel_memory_address
        addr = insn.memory_displacement
        if insn.memory_base != ix.Register.NONE:
            addr += self._get_reg(insn.memory_base)
        if insn.memory_index != ix.Register.NONE:
            addr += self._get_reg(insn.memory_index) * insn.memory_index_scale
        return addr & _mask(8)

    def load(self, addr: int, size: int) -> int:
        return int.from_bytes(self.mem.read(addr, size), "little")

    def store(self, addr: int, value: int, size: int) -> None:
        self.mem.write(addr, (value & _mask(size)).to_bytes(size, "little"))

    def push(self, value: int) -> None:
        self.gpr[4] = (self.gpr[4] - 8) & _mask(8)
        self.store(self.gpr[4], value, 8)

    def pop(self) -> int:
        value = self.load(self.gpr[4], 8)
        self.gpr[4] = (self.gpr[4] + 8) & _mask(8)
        return value

    # -- operands ----------------------------------------------------------------
    def _size(self, insn: ix.Instruction, i: int) -> int:
        kind = insn.op_kind(i)
        if kind == ix.OpKind.REGISTER:
            reg = insn.op_register(i)
            return 16 if ix.RegisterExt.is_xmm(reg) else ix.RegisterExt.size(reg)
        if kind == ix.OpKind.MEMORY:
            return ix.MemorySizeInfo(insn.memory_size).size
        raise Unsupported(f"operand {i} of {_FMT.format(insn)}")

    def get(self, insn: ix.Instruction, i: int, size: int) -> int:
        """Operand ``i`` as an unsigned number of ``size`` bytes (immediates are
        sign-extended to it, as the CPU does)."""
        kind = insn.op_kind(i)
        if kind == ix.OpKind.REGISTER:
            return self._get_reg(insn.op_register(i)) & _mask(size)
        if kind == ix.OpKind.MEMORY:
            return self.load(self.address(insn), size)
        if kind in _IMM:
            return insn.immediate(i) & _mask(size)
        raise Unsupported(f"operand {i} of {_FMT.format(insn)}")

    def put(self, insn: ix.Instruction, i: int, value: int, size: int) -> None:
        kind = insn.op_kind(i)
        if kind == ix.OpKind.REGISTER:
            self._set_reg(insn.op_register(i), value & _mask(size))
        elif kind == ix.OpKind.MEMORY:
            self.store(self.address(insn), value, size)
        else:
            raise Unsupported(f"operand {i} of {_FMT.format(insn)}")

    # -- flags -------------------------------------------------------------------
    def _set_flags(self, bits: int, which: int = STATUS) -> None:
        self.rflags = (self.rflags & ~which) | (bits & which)

    def _szp(self, r: int, size: int) -> int:
        out = PF if _parity(r) else 0
        out |= ZF if r & _mask(size) == 0 else 0
        out |= SF if _sign(r, size) else 0
        return out

    def _add(self, a: int, b: int, size: int, keep_cf: bool = False) -> int:
        full = a + b
        r = full & _mask(size)
        bits = self._szp(r, size) | (AF if (a ^ b ^ r) & 0x10 else 0)
        bits |= CF if full >> (8 * size) else 0
        bits |= OF if _sign((a ^ r) & (b ^ r), size) else 0
        self._set_flags(bits, STATUS & ~CF if keep_cf else STATUS)
        return r

    def _sub(self, a: int, b: int, size: int, keep_cf: bool = False) -> int:
        r = (a - b) & _mask(size)
        bits = self._szp(r, size) | (AF if (a ^ b ^ r) & 0x10 else 0)
        bits |= CF if a < b else 0
        bits |= OF if _sign((a ^ b) & (a ^ r), size) else 0
        self._set_flags(bits, STATUS & ~CF if keep_cf else STATUS)
        return r

    def _logic(self, r: int, size: int) -> int:
        self._set_flags(self._szp(r, size))
        return r

    def condition(self, cc: int) -> bool:
        f = self.flag
        C = ix.ConditionCode
        table: dict[int, Callable[[], bool]] = {
            C.O: lambda: f("OF"),
            C.NO: lambda: not f("OF"),
            C.B: lambda: f("CF"),
            C.AE: lambda: not f("CF"),
            C.E: lambda: f("ZF"),
            C.NE: lambda: not f("ZF"),
            C.BE: lambda: f("CF") or f("ZF"),
            C.A: lambda: not (f("CF") or f("ZF")),
            C.S: lambda: f("SF"),
            C.NS: lambda: not f("SF"),
            C.P: lambda: f("PF"),
            C.NP: lambda: not f("PF"),
            C.L: lambda: f("SF") != f("OF"),
            C.GE: lambda: f("SF") == f("OF"),
            C.LE: lambda: f("ZF") or f("SF") != f("OF"),
            C.G: lambda: not f("ZF") and f("SF") == f("OF"),
        }
        if cc not in table:
            raise Unsupported(f"condition {cc}")
        return table[cc]()

    # -- running -----------------------------------------------------------------
    def fetch(self, ip: int) -> ix.Instruction:
        for n in range(15, 0, -1):  # the last instruction may sit at a region's end
            try:
                raw = self.mem.read(ip, n)
            except CpuFault:
                continue
            insn = ix.Decoder(64, raw, ip=ip).decode()
            if insn.code != ix.Code.INVALID:
                return insn
        raise CpuFault(f"no valid instruction at {ip:#x}")

    def run(self, start: int, stop: int, limit: int = 1000) -> Cpu:
        """Run from ``start`` until ``rip`` reaches ``stop``."""
        self.rip = start
        while self.rip != stop:
            if self.steps >= limit:
                raise CpuFault(f"still running after {limit} instructions")
            self.step()
        return self

    def step(self) -> None:
        insn = self.fetch(self.rip)
        self.trace.append(_FMT.format(insn))
        self.steps += 1
        if insn.has_lock_prefix or insn.has_rep_prefix or insn.has_repne_prefix:
            raise Unsupported(_FMT.format(insn))
        handler = _HANDLERS.get(insn.mnemonic)
        if handler is None:
            raise Unsupported(f"minicpu doesn't know {_FMT.format(insn)}")
        self.rip = insn.next_ip
        handler(self, insn)


# -- instruction handlers -------------------------------------------------------------
def _mov(cpu: Cpu, insn: ix.Instruction) -> None:
    size = cpu._size(insn, 0)
    cpu.put(insn, 0, cpu.get(insn, 1, size), size)


def _lea(cpu: Cpu, insn: ix.Instruction) -> None:
    size = cpu._size(insn, 0)
    cpu.put(insn, 0, cpu.address(insn), size)


def _push(cpu: Cpu, insn: ix.Instruction) -> None:
    if insn.op_kind(0) not in _IMM and cpu._size(insn, 0) != 8:
        raise Unsupported(_FMT.format(insn))
    cpu.push(cpu.get(insn, 0, 8))


def _pop(cpu: Cpu, insn: ix.Instruction) -> None:
    if cpu._size(insn, 0) != 8:
        raise Unsupported(_FMT.format(insn))
    cpu.put(insn, 0, cpu.pop(), 8)


def _pushfq(cpu: Cpu, _insn: ix.Instruction) -> None:
    cpu.push(cpu.rflags | 0x2)


def _popfq(cpu: Cpu, _insn: ix.Instruction) -> None:
    cpu.rflags = cpu.pop() | 0x2


def _binary(op: str) -> Callable[[Cpu, ix.Instruction], None]:
    def run(cpu: Cpu, insn: ix.Instruction) -> None:
        size = cpu._size(insn, 0)
        a, b = cpu.get(insn, 0, size), cpu.get(insn, 1, size)
        if op == "add":
            cpu.put(insn, 0, cpu._add(a, b, size), size)
        elif op == "sub":
            cpu.put(insn, 0, cpu._sub(a, b, size), size)
        elif op == "cmp":
            cpu._sub(a, b, size)
        elif op == "and":
            cpu.put(insn, 0, cpu._logic(a & b, size), size)
        elif op == "or":
            cpu.put(insn, 0, cpu._logic(a | b, size), size)
        elif op == "xor":
            cpu.put(insn, 0, cpu._logic(a ^ b, size), size)
        elif op == "test":
            cpu._logic(a & b, size)

    return run


def _inc(cpu: Cpu, insn: ix.Instruction) -> None:
    size = cpu._size(insn, 0)
    cpu.put(insn, 0, cpu._add(cpu.get(insn, 0, size), 1, size, keep_cf=True), size)


def _dec(cpu: Cpu, insn: ix.Instruction) -> None:
    size = cpu._size(insn, 0)
    cpu.put(insn, 0, cpu._sub(cpu.get(insn, 0, size), 1, size, keep_cf=True), size)


def _neg(cpu: Cpu, insn: ix.Instruction) -> None:
    size = cpu._size(insn, 0)
    cpu.put(insn, 0, cpu._sub(0, cpu.get(insn, 0, size), size), size)


def _shift(kind: str) -> Callable[[Cpu, ix.Instruction], None]:
    def run(cpu: Cpu, insn: ix.Instruction) -> None:
        size = cpu._size(insn, 0)
        bits = 8 * size
        a = cpu.get(insn, 0, size)
        count = cpu.get(insn, 1, 1) & (0x3F if size == 8 else 0x1F)
        if count == 0:
            return  # flags unchanged
        if kind == "shr":
            r, cf = a >> count, (a >> (count - 1)) & 1
            of = _sign(a, size)
        elif kind == "sar":
            signed = a - (1 << bits) if _sign(a, size) else a
            r, cf, of = (signed >> count) & _mask(size), (signed >> (count - 1)) & 1, False
        else:  # shl
            r, cf = (a << count) & _mask(size), (a >> (bits - count)) & 1
            of = _sign(r, size) != bool(cf)
        flags = cpu._szp(r, size) | (CF if cf else 0) | (OF if of and count == 1 else 0)
        cpu._set_flags(flags)
        cpu.put(insn, 0, r, size)

    return run


def _jmp(cpu: Cpu, insn: ix.Instruction) -> None:
    if insn.op_kind(0) not in (ix.OpKind.NEAR_BRANCH64, ix.OpKind.NEAR_BRANCH32):
        raise Unsupported(f"indirect {_FMT.format(insn)}")
    cpu.rip = insn.near_branch_target


def _jcc(cpu: Cpu, insn: ix.Instruction) -> None:
    if cpu.condition(insn.condition_code):
        cpu.rip = insn.near_branch_target


def _nop(_cpu: Cpu, _insn: ix.Instruction) -> None:
    return


# SSE: legacy forms keep bits 128+ (we only keep 128 bits); VEX forms clear them.
def _movss(cpu: Cpu, insn: ix.Instruction) -> None:
    dst, src = insn.op_kind(0), insn.op_kind(1)
    if insn.op_count != 2:
        raise Unsupported(_FMT.format(insn))
    low = cpu.get(insn, 1, 4)
    if dst == ix.OpKind.MEMORY:
        cpu.put(insn, 0, low, 4)
    elif src == ix.OpKind.MEMORY:  # load: the rest of the register is cleared
        cpu.put(insn, 0, low, 16)
    else:  # register to register: only the low float changes
        old = cpu.get(insn, 0, 16)
        cpu.put(insn, 0, (old & ~0xFFFFFFFF) | low, 16)


def _movups(cpu: Cpu, insn: ix.Instruction) -> None:
    cpu.put(insn, 0, cpu.get(insn, 1, 16), 16)


def _movd(cpu: Cpu, insn: ix.Instruction) -> None:
    if insn.op_kind(0) == ix.OpKind.REGISTER and ix.RegisterExt.is_xmm(insn.op0_register):
        cpu.put(insn, 0, cpu.get(insn, 1, 4), 16)  # zero-extended into the xmm register
    else:
        cpu.put(insn, 0, cpu.get(insn, 1, 4), 4)


def _ucomiss(cpu: Cpu, insn: ix.Instruction) -> None:
    a, b = bits_f32(cpu.get(insn, 0, 4)), bits_f32(cpu.get(insn, 1, 4))
    if a != a or b != b:  # NaN: unordered
        bits = ZF | PF | CF
    elif a > b:
        bits = 0
    elif a < b:
        bits = CF
    else:
        bits = ZF
    cpu._set_flags(bits)


def _scalar(op: Callable[[float, float], float]) -> Callable[[Cpu, ix.Instruction], None]:
    def run(cpu: Cpu, insn: ix.Instruction) -> None:
        if insn.op_count != 2:
            raise Unsupported(_FMT.format(insn))
        old = cpu.get(insn, 0, 16)
        r = f32_bits(op(bits_f32(old), bits_f32(cpu.get(insn, 1, 4))))
        cpu.put(insn, 0, (old & ~0xFFFFFFFF) | r, 16)

    return run


def _double(op: Callable[[float, float], float]) -> Callable[[Cpu, ix.Instruction], None]:
    def run(cpu: Cpu, insn: ix.Instruction) -> None:
        if insn.op_count != 2:
            raise Unsupported(_FMT.format(insn))
        old = cpu.get(insn, 0, 16)
        r = f64_bits(op(bits_f64(old), bits_f64(cpu.get(insn, 1, 8))))
        cpu.put(insn, 0, (old & ~_mask(8)) | r, 16)

    return run


def _divide(a: float, b: float) -> float:
    """IEEE division (Python raises on x/0). 0/0 gives x86's default NaN (sign set)."""
    if b == 0:
        if a != a:
            return a
        if a == 0:
            return -math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def _extend(signed: bool) -> Callable[[Cpu, ix.Instruction], None]:
    """movzx / movsx / movsxd."""

    def run(cpu: Cpu, insn: ix.Instruction) -> None:
        dst, src = cpu._size(insn, 0), cpu._size(insn, 1)
        v = cpu.get(insn, 1, src)
        if signed and _sign(v, src):
            v -= 1 << (8 * src)
        cpu.put(insn, 0, v & _mask(dst), dst)

    return run


def _imul3(cpu: Cpu, insn: ix.Instruction) -> None:
    """imul r, r/m, imm: signed, truncated; CF = OF = the result didn't fit."""
    if insn.op_count != 3:
        raise Unsupported(_FMT.format(insn))
    size = cpu._size(insn, 0)
    a, b = cpu.get(insn, 1, size), insn.immediate(2) & _mask(size)
    sa = a - (1 << (8 * size)) if _sign(a, size) else a
    sb = b - (1 << (8 * size)) if _sign(b, size) else b
    full = sa * sb
    r = full & _mask(size)
    sr = r - (1 << (8 * size)) if _sign(r, size) else r
    cpu._set_flags(CF | OF if sr != full else 0)
    cpu.put(insn, 0, r, size)


def _float_in(cpu: Cpu, insn: ix.Instruction, double: bool) -> float:
    return bits_f64(cpu.get(insn, 1, 8)) if double else bits_f32(cpu.get(insn, 1, 4))


def _to_whole(double: bool, cut: bool) -> Callable[[Cpu, ix.Instruction], None]:
    """cvt(t)ss2si / cvt(t)sd2si: out of range or NaN gives the "indefinite" value."""

    def run(cpu: Cpu, insn: ix.Instruction) -> None:
        size = cpu._size(insn, 0)
        x = _float_in(cpu, insn, double)
        indefinite = 1 << (8 * size - 1)
        if not math.isfinite(x):
            cpu.put(insn, 0, indefinite, size)
            return
        n = math.trunc(x) if cut else round(x)  # round(): nearest, halves to even
        lo, hi = -(1 << (8 * size - 1)), (1 << (8 * size - 1)) - 1
        cpu.put(insn, 0, n & _mask(size) if lo <= n <= hi else indefinite, size)

    return run


def _from_whole(double: bool) -> Callable[[Cpu, ix.Instruction], None]:
    """cvtsi2ss / cvtsi2sd: the rest of the xmm register stays."""

    def run(cpu: Cpu, insn: ix.Instruction) -> None:
        if insn.op_count != 2:
            raise Unsupported(_FMT.format(insn))
        src = cpu._size(insn, 1)
        v = cpu.get(insn, 1, src)
        if _sign(v, src):
            v -= 1 << (8 * src)
        old = cpu.get(insn, 0, 16)
        if double:
            cpu.put(insn, 0, (old & ~_mask(8)) | f64_bits(float(v)), 16)
        else:
            cpu.put(insn, 0, (old & ~_mask(4)) | f32_bits(float(v)), 16)

    return run


def _cvtss2sd(cpu: Cpu, insn: ix.Instruction) -> None:
    old = cpu.get(insn, 0, 16)
    cpu.put(insn, 0, (old & ~_mask(8)) | f64_bits(bits_f32(cpu.get(insn, 1, 4))), 16)


def _cvtsd2ss(cpu: Cpu, insn: ix.Instruction) -> None:
    old = cpu.get(insn, 0, 16)
    cpu.put(insn, 0, (old & ~_mask(4)) | f32_bits(bits_f64(cpu.get(insn, 1, 8))), 16)


def _movsd(cpu: Cpu, insn: ix.Instruction) -> None:
    """SSE movsd (the string ``movsd`` has memory-only operands and is refused)."""
    if insn.op_count != 2 or (
        insn.op_kind(0) != ix.OpKind.REGISTER and insn.op_kind(1) != ix.OpKind.REGISTER
    ):
        raise Unsupported(_FMT.format(insn))
    low = cpu.get(insn, 1, 8)
    if insn.op_kind(0) == ix.OpKind.MEMORY:
        cpu.put(insn, 0, low, 8)
    elif insn.op_kind(1) == ix.OpKind.MEMORY:
        cpu.put(insn, 0, low, 16)
    else:
        old = cpu.get(insn, 0, 16)
        cpu.put(insn, 0, (old & ~_mask(8)) | low, 16)


def _movq(cpu: Cpu, insn: ix.Instruction) -> None:
    """movq: into an xmm register the rest is cleared; to r64 or memory, 8 bytes."""
    if insn.op_kind(0) == ix.OpKind.REGISTER and ix.RegisterExt.is_xmm(insn.op0_register):
        cpu.put(insn, 0, cpu.get(insn, 1, 8), 16)
    else:
        cpu.put(insn, 0, cpu.get(insn, 1, 8), 8)


def _move128(aligned: bool) -> Callable[[Cpu, ix.Instruction], None]:
    """movaps (memory must be 16-aligned), movdqu, vmovups xmm (128-bit only)."""

    def run(cpu: Cpu, insn: ix.Instruction) -> None:
        if insn.op_count != 2 or any(
            insn.op_kind(i) == ix.OpKind.REGISTER and ix.RegisterExt.is_ymm(insn.op_register(i))
            for i in range(2)
        ):
            raise Unsupported(_FMT.format(insn))
        touches = any(insn.op_kind(i) == ix.OpKind.MEMORY for i in range(2))
        if aligned and touches and cpu.address(insn) % 16:
            raise CpuFault(f"misaligned {_FMT.format(insn)}")
        cpu.put(insn, 0, cpu.get(insn, 1, 16), 16)

    return run


def _unpcklps(cpu: Cpu, insn: ix.Instruction) -> None:
    """dst = [dst0, src0, dst1, src1] (32-bit lanes)."""
    d, s = cpu.get(insn, 0, 16), cpu.get(insn, 1, 16)
    lane = lambda v, i: (v >> (32 * i)) & 0xFFFFFFFF  # noqa: E731
    out = lane(d, 0) | lane(s, 0) << 32 | lane(d, 1) << 64 | lane(s, 1) << 96
    cpu.put(insn, 0, out, 16)


def _call(cpu: Cpu, insn: ix.Instruction) -> None:
    if insn.op_kind(0) not in (ix.OpKind.NEAR_BRANCH64, ix.OpKind.NEAR_BRANCH32):
        raise Unsupported(f"indirect {_FMT.format(insn)}")
    cpu.push(insn.next_ip)
    cpu.rip = insn.near_branch_target


def _ret(cpu: Cpu, insn: ix.Instruction) -> None:
    if insn.op_count:
        raise Unsupported(_FMT.format(insn))  # ret imm16
    cpu.rip = cpu.pop()


_HANDLERS: dict[int, Callable[[Cpu, ix.Instruction], None]] = {
    M.MOV: _mov,
    M.LEA: _lea,
    M.PUSH: _push,
    M.POP: _pop,
    M.PUSHFQ: _pushfq,
    M.POPFQ: _popfq,
    M.ADD: _binary("add"),
    M.SUB: _binary("sub"),
    M.CMP: _binary("cmp"),
    M.AND: _binary("and"),
    M.OR: _binary("or"),
    M.XOR: _binary("xor"),
    M.TEST: _binary("test"),
    M.INC: _inc,
    M.DEC: _dec,
    M.NEG: _neg,
    M.SHR: _shift("shr"),
    M.SAR: _shift("sar"),
    M.SHL: _shift("shl"),
    M.JMP: _jmp,
    M.NOP: _nop,
    M.MOVSS: _movss,
    M.VMOVSS: _movss,
    M.MOVUPS: _movups,
    M.MOVD: _movd,
    M.UCOMISS: _ucomiss,
    M.ADDSS: _scalar(lambda a, b: a + b),
    M.MULSS: _scalar(lambda a, b: a * b),
    M.DIVSS: _scalar(_divide),
    M.MULSD: _double(lambda a, b: a * b),
    M.DIVSD: _double(_divide),
    M.MOVZX: _extend(False),
    M.MOVSX: _extend(True),
    M.MOVSXD: _extend(True),
    M.IMUL: _imul3,
    M.CVTTSS2SI: _to_whole(False, True),
    M.CVTSS2SI: _to_whole(False, False),
    M.CVTTSD2SI: _to_whole(True, True),
    M.CVTSD2SI: _to_whole(True, False),
    M.CVTSI2SS: _from_whole(False),
    M.CVTSI2SD: _from_whole(True),
    M.CVTSS2SD: _cvtss2sd,
    M.CVTSD2SS: _cvtsd2ss,
    M.MOVSD: _movsd,
    M.MOVQ: _movq,
    M.MOVAPS: _move128(True),
    M.MOVDQU: _move128(False),
    M.VMOVUPS: _move128(False),
    M.UNPCKLPS: _unpcklps,
    M.CALL: _call,
    M.RET: _ret,
}
for _m in (
    M.JO,
    M.JNO,
    M.JB,
    M.JAE,
    M.JE,
    M.JNE,
    M.JBE,
    M.JA,
    M.JS,
    M.JNS,
    M.JP,
    M.JNP,
    M.JL,
    M.JGE,
    M.JLE,
    M.JG,
):
    _HANDLERS[_m] = _jcc
