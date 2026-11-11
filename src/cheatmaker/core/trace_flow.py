"""Read the game's code before a writer and follow the written value back to where it
was loaded from (spec-trace AC-T3, AC-T4; docs/architecture-trace.md "The analyser").

Pure: it takes decoded iced-x86 instructions, the trap registers and an injected
``read(addr, n)``, and never talks to a console.

- ``decode_window``: the straight-line code block that ends with the writer.
- ``writer_at``: the writing instruction for a data trap (``rep movs`` at RIP too).
- ``follow``: walks backwards from the writer's source register over the
  instructions that write it, recording the steps (``trace_way.Step``) until a load
  (a source), a constant, a call result, the start of the block, or a step it doesn't
  follow.
"""

from __future__ import annotations

import logging
import math
import struct
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Literal

import iced_x86 as ix

from cheatmaker.console.client import WatchHit
from cheatmaker.core.locker import instruction_before
from cheatmaker.core.trace_way import Kind, Step, Way
from cheatmaker.core.values import ValueType

log = logging.getLogger(__name__)

WINDOW = 256  # bytes decoded before the writer (AC-T3)
REPLAY_DEPTH = 3  # pointers read back from memory to work out one address (AC-T4 rule 2)
MAX_INSN = 15

Read = Callable[[int, int], bytes]
"""``read(address, size)``: the game's memory (same as ``locker.Read``)."""
IsConst = Callable[[int], bool]
"""``is_const(address)``: the address is in a mapped region that isn't writable."""

_FMT = ix.Formatter(ix.FormatterSyntax.INTEL)
_INFO = ix.InstructionInfoFactory()
_REG_NAMES = {getattr(ix.Register, n): n.lower() for n in dir(ix.Register) if n.isupper()}
_WRITE = (
    ix.OpAccess.WRITE,
    ix.OpAccess.READ_WRITE,
    ix.OpAccess.COND_WRITE,
    ix.OpAccess.READ_COND_WRITE,
)
M = ix.Mnemonic
_FULL_MOVES = {
    M.MOVAPS,
    M.MOVUPS,
    M.MOVAPD,
    M.MOVUPD,
    M.MOVDQA,
    M.MOVDQU,
    M.LDDQU,
    M.VMOVAPS,
    M.VMOVUPS,
    M.VMOVAPD,
    M.VMOVUPD,
    M.VMOVDQA,
    M.VMOVDQU,
}
_SCALAR_MOVES = {M.MOVSS: 4, M.VMOVSS: 4, M.MOVD: 4, M.VMOVD: 4}
_SCALAR_MOVES |= {M.MOVSD: 8, M.VMOVSD: 8, M.MOVQ: 8, M.VMOVQ: 8}
_STORES = {M.MOV, *_FULL_MOVES, *_SCALAR_MOVES}
_EXTENDS = {M.MOVZX: False, M.MOVSX: True, M.MOVSXD: True}
_TO_WHOLE = {
    M.CVTTSS2SI: (ValueType.FLOAT, "cut"),
    M.VCVTTSS2SI: (ValueType.FLOAT, "cut"),
    M.CVTTSD2SI: (ValueType.DOUBLE, "cut"),
    M.VCVTTSD2SI: (ValueType.DOUBLE, "cut"),
    M.CVTSS2SI: (ValueType.FLOAT, "round"),
    M.VCVTSS2SI: (ValueType.FLOAT, "round"),
    M.CVTSD2SI: (ValueType.DOUBLE, "round"),
    M.VCVTSD2SI: (ValueType.DOUBLE, "round"),
}
_TO_DECIMAL = {
    M.CVTSI2SS: ValueType.FLOAT,
    M.VCVTSI2SS: ValueType.FLOAT,
    M.CVTSI2SD: ValueType.DOUBLE,
    M.VCVTSI2SD: ValueType.DOUBLE,
}
_RESIZE = {  # (kind, from, to)
    M.CVTSS2SD: (Kind.WIDEN, ValueType.FLOAT, ValueType.DOUBLE),
    M.VCVTSS2SD: (Kind.WIDEN, ValueType.FLOAT, ValueType.DOUBLE),
    M.CVTSD2SS: (Kind.NARROW, ValueType.DOUBLE, ValueType.FLOAT),
    M.VCVTSD2SS: (Kind.NARROW, ValueType.DOUBLE, ValueType.FLOAT),
}
_MULDIV = {  # (kind, number type)
    M.MULSS: (Kind.MUL, ValueType.FLOAT),
    M.VMULSS: (Kind.MUL, ValueType.FLOAT),
    M.MULSD: (Kind.MUL, ValueType.DOUBLE),
    M.VMULSD: (Kind.MUL, ValueType.DOUBLE),
    M.DIVSS: (Kind.DIV, ValueType.FLOAT),
    M.VDIVSS: (Kind.DIV, ValueType.FLOAT),
    M.DIVSD: (Kind.DIV, ValueType.DOUBLE),
    M.VDIVSD: (Kind.DIV, ValueType.DOUBLE),
}
_ZEROING = {M.XOR, M.PXOR, M.XORPS, M.XORPD, M.VPXOR, M.VXORPS, M.VXORPD, M.SUB}
_SCRAMBLE = {M.XOR, M.PXOR, M.XORPS, M.XORPD, M.VPXOR, M.VXORPS, M.VXORPD}
_SCRAMBLE |= {M.ROL, M.ROR, M.NOT, M.BSWAP}
_FLOAT_XORS = {M.XORPS, M.XORPD, M.VXORPS, M.VXORPD}  # with a sign mask: a negation
_OFFSETS = {M.ADD, M.SUB, M.INC, M.DEC, M.NEG, M.ADDSS, M.SUBSS, M.ADDSD, M.SUBSD}
_OFFSETS |= {M.VADDSS, M.VSUBSS, M.VADDSD, M.VSUBSD}
CALLEE_SAVED = frozenset({"rbx", "rbp", "r12", "r13", "r14", "r15"})
"""Registers a call keeps (System V, as PS4/PS5 games use)."""
_INT_TYPES = {1: ValueType.INT8, 2: ValueType.INT16, 4: ValueType.INT32, 8: ValueType.INT64}


def reg_name(reg: int) -> str:
    return _REG_NAMES.get(reg, f"reg{reg}")


def full_name(reg: int) -> str:
    """The register as a whole: ``eax`` -> ``rax``, ``xmm1`` -> ``xmm1``."""
    if ix.RegisterExt.is_vector_register(reg):
        return f"xmm{ix.RegisterExt.number(reg)}"
    return reg_name(ix.RegisterExt.full_register(reg))


_HIGH_BYTES = frozenset({ix.Register.AH, ix.Register.BH, ix.Register.CH, ix.Register.DH})


def _reg_size(reg: int) -> int:
    return 16 if ix.RegisterExt.is_vector_register(reg) else ix.RegisterExt.size(reg)


def text(insn: ix.Instruction) -> str:
    return _FMT.format(insn)


def written_registers(insn: ix.Instruction) -> set[str]:
    """Whole registers ``insn`` writes (``rsp`` for push/pop/call too)."""
    return {
        full_name(r.register)
        for r in _INFO.info(insn).used_registers()
        if r.access in _WRITE and r.register != ix.Register.NONE
    }


# -- data types --------------------------------------------------------------------------
@dataclass(frozen=True)
class MemRef:
    """A memory operand, by register name."""

    base: str | None
    index: str | None
    scale: int
    disp: int
    rip_target: int | None
    size: int

    @classmethod
    def of(cls, insn: ix.Instruction) -> MemRef:
        size = ix.MemorySizeInfo(insn.memory_size).size
        if insn.memory_base == ix.Register.RIP:
            return cls(None, None, 1, 0, insn.ip_rel_memory_address, size)
        base = None if insn.memory_base == ix.Register.NONE else full_name(insn.memory_base)
        index = None if insn.memory_index == ix.Register.NONE else full_name(insn.memory_index)
        disp = insn.memory_displacement
        if disp >= 1 << 63:
            disp -= 1 << 64
        return cls(base, index, insn.memory_index_scale, disp, None, size)

    @property
    def regs(self) -> tuple[str, ...]:
        return tuple(r for r in (self.base, self.index) if r)

    def text(self) -> str:
        if self.rip_target is not None:
            return f"[{self.rip_target:X}h]"
        parts = []
        if self.base:
            parts.append(self.base)
        if self.index:
            parts.append(self.index if self.scale == 1 else f"{self.index}*{self.scale}")
        out = "+".join(parts)
        if self.disp or not out:
            sign = "-" if self.disp < 0 else ("+" if out else "")
            out += f"{sign}{abs(self.disp):X}h"
        return f"[{out}]"

    def same_place(self, other: MemRef) -> bool:
        return (self.base, self.index, self.scale, self.disp, self.rip_target) == (
            other.base,
            other.index,
            other.scale,
            other.disp,
            other.rip_target,
        )


@dataclass(frozen=True)
class Load:
    ip: int
    text: str
    mem: MemRef
    lane: int  # byte offset inside the loaded bytes (vector lanes)
    at: int  # index in Window.insns


class End(Enum):
    SOURCE = "source"
    BLOCK_COPY = "block copy"
    CONSTANT = "fixed number"
    CALL_RESULT = "result of a call"
    BLOCK_START = "start of the code block"
    BRANCH = "a branch inside the path"
    NOT_FOLLOWED = "a step it doesn't follow"
    SCRAMBLED = "scrambled"
    ALREADY_REAL = "read-modify-write"


@dataclass(frozen=True)
class Window:
    insns: tuple[ix.Instruction, ...]  # straight line, the last one is the writer
    marker: str = ""  # what ends the block before it ("ret at 0x...", "window start")

    @property
    def writer(self) -> ix.Instruction:
        return self.insns[-1]


@dataclass(frozen=True)
class Path:
    end: End
    way: Way | None  # steps followed so far (SOURCE, BLOCK_COPY, BLOCK_START)
    load: Load | None = None  # SOURCE
    max_load: Load | None = None  # SHARE
    reg: str | None = None  # BLOCK_START: the register the value arrives in
    followed: tuple[str, ...] = ()  # instruction texts, writer first (Advanced)
    reason: str = ""  # e.g. "add eax,ecx at 0x40123a"
    lane: int = 0  # BLOCK_START: the byte of ``reg`` the value is in
    width: int = 16  # BLOCK_START: the low bytes of ``reg`` the value was carried in


@dataclass(frozen=True)
class Candidate:
    address: int
    method: Literal["regs", "replay", "block", "caller", "exec"]
    expr: str  # the address: "rbx+10h", "[[rbx+30h]+8h]+10h", "watched+(rsi-rdi)"
    max_address: int | None = None
    writer: int = 0


# -- the window ----------------------------------------------------------------------------
def _chain(code: bytes, start: int, ip0: int, writer_ip: int) -> list[ix.Instruction] | None:
    """Decode from ``start`` straight through; the instructions before the writer if
    the chain lands exactly on it, else None."""
    out = []
    for insn in ix.Decoder(64, code[start:], ip=ip0 + start):
        if insn.ip == writer_ip:
            return out
        if insn.ip > writer_ip or insn.code == ix.Code.INVALID:
            return None
        out.append(insn)
    return None


def _marker(insn: ix.Instruction) -> bool:
    """Ends a code block: ret, int3/padding, an unconditional or indirect jump."""
    if insn.code == ix.Code.INVALID:
        return True
    return insn.flow_control in (
        ix.FlowControl.RETURN,
        ix.FlowControl.INTERRUPT,
        ix.FlowControl.UNCONDITIONAL_BRANCH,
        ix.FlowControl.INDIRECT_BRANCH,
        ix.FlowControl.EXCEPTION,
    )


def decode_window(code: bytes, writer_ip: int, writer_len: int) -> Window:
    """The straight-line block that ends with the writer. ``code`` ends with the
    writer's bytes (up to ``WINDOW`` bytes before them).

    Decoding straight through from every start offset, each chain that lands exactly
    on the writer votes (weighted by how much it decoded, as ``locker`` does) for the
    instruction that ends where the walk back is: x86 re-synchronises after a few
    instructions, so the earliest ones of a mis-synced start lose. The walk then cuts
    at the last ret, int3, unconditional jump or invalid byte before the writer."""
    ip0 = writer_ip + writer_len - len(code)
    writer = ix.Decoder(64, code[len(code) - writer_len :], ip=writer_ip).decode()
    n = writer_ip - ip0
    ends: list[tuple[int, dict[int, ix.Instruction]]] = []
    for start in range(n):
        chain = _chain(code, start, ip0, writer_ip)
        if chain:
            ends.append((n - start, {i.next_ip: i for i in chain}))
    back: list[ix.Instruction] = []
    cur, marker = writer_ip, "window start"
    while True:
        votes: dict[tuple[int, int], tuple[int, ix.Instruction]] = {}
        for weight, by_end in ends:
            insn = by_end.get(cur)
            if insn is not None:
                key = (insn.ip, insn.len)
                votes[key] = (votes.get(key, (0, insn))[0] + weight, insn)
        if not votes:
            break
        (_ip, _len), (_w, best) = max(votes.items(), key=lambda kv: (kv[1][0], kv[0][1]))
        if _marker(best):
            marker = f"{text(best)} at {best.ip:#x}"
            break
        back.append(best)
        cur = best.ip
    return Window((*reversed(back), writer), marker)


def writer_at(
    code_before: bytes,
    code_at: bytes,
    rip: int,
    hit: WatchHit | None = None,
    watched: int | None = None,
) -> tuple[int, bytes, str] | None:
    """The writing instruction for a data trap at ``rip``. Normally the one that ends
    at ``rip`` (``locker.instruction_before``). A ``rep movs``/``rep stos`` trapped
    mid-copy can leave RIP **at** itself, so that one is preferred when it is there
    (UNVERIFIED on the console: the caller logs which reading was used), unless the
    instruction ending at ``rip`` stores over ``watched`` by the trap's registers: then
    the trap is that store's, and the ``rep`` after it hasn't run yet."""
    at = ix.Decoder(64, code_at, ip=rip).decode()
    before = instruction_before(code_before, rip)
    if (
        at.code != ix.Code.INVALID
        and (at.has_rep_prefix or at.has_repe_prefix)
        and at.mnemonic in (M.MOVSB, M.MOVSW, M.MOVSD, M.MOVSQ, M.STOSB, M.STOSW, M.STOSD, M.STOSQ)
        and not _stores_over(before, hit, watched)
    ):
        return rip, bytes(code_at[: at.len]), text(at)
    return before


def _stores_over(
    found: tuple[int, bytes, str] | None, hit: WatchHit | None, watched: int | None
) -> bool:
    """Does the instruction ``found`` store over ``watched``, by ``hit``'s registers?"""
    if found is None or hit is None or watched is None:
        return False
    insn = ix.Decoder(64, found[1], ip=found[0]).decode()
    addr = store_address(insn, hit)
    if addr is None:
        return False
    return addr <= watched < addr + max(1, ix.MemorySizeInfo(insn.memory_size).size)


# -- following the value -------------------------------------------------------------------
class _Stop(Exception):
    """A control-flow signal inside ``follow``: the path ends here."""

    def __init__(self, end: End, reason: str = "") -> None:
        super().__init__(reason)
        self.end, self.reason = end, reason


@dataclass
class _Walk:
    win: Window
    read: Read
    is_const: IsConst
    shown: ValueType
    tracked: str  # whole register name
    lane: int
    cur: ValueType  # the tracked value's number type here
    steps: list[Step]
    followed: list[str]
    max_load: Load | None = None
    width: int = 16
    """The narrowest the value passed through (bytes from the register's start): the
    writer's store, narrower register moves and extends. Only a load wider than this
    holds more than the copy got (TRUNCATE)."""

    def way(self) -> Way:
        return Way(self.cur, self.shown, tuple(reversed(self.steps)))

    def path(self, end: End, **kw) -> Path:
        way = None
        if end in (End.SOURCE, End.BLOCK_COPY, End.BLOCK_START):
            way = self.way()
        kw.setdefault("width", self.width)
        return Path(end, way, max_load=self.max_load, followed=tuple(self.followed), **kw)


def _int_type(size: int) -> ValueType:
    return _INT_TYPES[size]


def _where(insn: ix.Instruction) -> str:
    return f"{text(insn)} at {insn.ip:#x}"


def follow(win: Window, lane: int, read: Read, is_const: IsConst, shown: ValueType) -> Path:
    """Follow the value the writer stores (byte ``lane`` of its store holds the
    watched ``shown`` number) back to where it came from."""
    w = win.writer
    first = text(w)
    if any(
        m.access in (ix.OpAccess.READ_WRITE, ix.OpAccess.READ_COND_WRITE)
        for m in _INFO.info(w).used_memory()
    ):
        return Path(End.ALREADY_REAL, None, followed=(first,), reason=_where(w))
    if w.has_rep_prefix or w.has_repe_prefix:
        if w.mnemonic in (M.STOSB, M.STOSW, M.STOSD, M.STOSQ):
            return Path(
                End.NOT_FOLLOWED, None, followed=(first,), reason=f"a block fill: {_where(w)}"
            )
        return Path(End.BLOCK_COPY, Way(shown, shown), followed=(first,))
    if w.op_count < 2 or w.op0_kind != ix.OpKind.MEMORY:
        return Path(End.NOT_FOLLOWED, None, followed=(first,), reason=_where(w))
    src_kind = w.op_kind(w.op_count - 1)
    if src_kind != ix.OpKind.REGISTER:
        end = End.CONSTANT if src_kind != ix.OpKind.MEMORY else End.NOT_FOLLOWED
        return Path(end, None, followed=(first,), reason=_where(w))
    if w.mnemonic not in _STORES:
        return Path(End.NOT_FOLLOWED, None, followed=(first,), reason=_where(w))
    src = w.op_register(w.op_count - 1)
    store_size = ix.MemorySizeInfo(w.memory_size).size
    if lane + shown.size > store_size:
        return Path(
            End.NOT_FOLLOWED, None, followed=(first,), reason=f"the copy isn't all in {_where(w)}"
        )
    high = 1 if src in _HIGH_BYTES else 0  # mov [m],ah stores byte 1 of rax
    return follow_reg(
        win, full_name(src), lane + high, read, is_const, shown, width=store_size + high
    )


def follow_reg(
    win: Window,
    reg: str,
    lane: int,
    read: Read,
    is_const: IsConst,
    shown: ValueType,
    *,
    width: int = 16,
) -> Path:
    """Like ``follow``, but the value is in ``reg`` (byte ``lane``) when the window's
    last instruction runs (a call that passes it as an argument). ``width``: the low
    bytes of ``reg`` that carry it on (the writer's store size)."""
    walk = _Walk(win, read, is_const, shown, reg, lane, shown, [], [text(win.writer)], width=width)
    try:
        return _walk_back(walk, len(win.insns) - 2)
    except _Stop as stop:
        return walk.path(stop.end, reason=stop.reason)


def _walk_back(walk: _Walk, i: int) -> Path:
    insns = walk.win.insns
    while i >= 0:
        insn = insns[i]
        flow = insn.flow_control
        if flow == ix.FlowControl.CONDITIONAL_BRANCH:
            raise _Stop(End.BRANCH, _where(insn))
        if flow in (ix.FlowControl.CALL, ix.FlowControl.INDIRECT_CALL):
            if walk.tracked in ("rax", "xmm0"):
                raise _Stop(End.CALL_RESULT, _where(insn))
            if walk.tracked not in CALLEE_SAVED:
                raise _Stop(End.NOT_FOLLOWED, f"a call changes it: {_where(insn)}")
            i -= 1
            continue
        if walk.tracked not in written_registers(insn):
            i -= 1
            continue
        walk.followed.append(text(insn))
        done = _step(walk, insn, i)
        if done is not None:
            return done
        i -= 1
    return walk.path(End.BLOCK_START, reg=walk.tracked, lane=walk.lane)


def _dst_src(insn: ix.Instruction) -> tuple[int, int]:
    """(dst operand, the operand the tracked value comes from) for 2- and 3-operand
    (VEX) forms."""
    return 0, insn.op_count - 1


def _need_lane0(walk: _Walk, insn: ix.Instruction) -> None:
    if walk.lane:
        raise _Stop(End.NOT_FOLLOWED, f"a vector lane through {_where(insn)}")


def _check_type(walk: _Walk, want: ValueType, insn: ix.Instruction) -> None:
    """The tracked value's type here must be what ``insn`` makes (a whole number may
    be the low part of a wider register)."""
    if walk.cur is want:
        return
    if not want.is_float and not walk.cur.is_float and walk.cur.size < want.size:
        return
    raise _Stop(
        End.NOT_FOLLOWED, f"{_where(insn)} makes a {want.name}, the copy is {walk.cur.name}"
    )


def _load(walk: _Walk, insn: ix.Instruction, i: int, lane: int, size: int) -> Path:
    if lane + walk.cur.size > size:
        raise _Stop(End.NOT_FOLLOWED, f"the value isn't all in {_where(insn)}")
    load = Load(insn.ip, text(insn), MemRef.of(insn), lane, i)
    return walk.path(End.SOURCE, load=load)


def _step(walk: _Walk, insn: ix.Instruction, i: int) -> Path | None:
    """One instruction that writes the tracked register. Returns the path when it
    ends here; otherwise updates ``walk`` (tracked register, lane, type, steps)."""
    m = insn.mnemonic
    dst, srci = _dst_src(insn)
    src_kind = insn.op_kind(srci) if insn.op_count else None
    if insn.op_count == 0 or insn.op_kind(dst) != ix.OpKind.REGISTER:
        raise _Stop(End.NOT_FOLLOWED, _where(insn))  # pop, push, cpuid...
    if full_name(insn.op_register(dst)) != walk.tracked:
        raise _Stop(End.NOT_FOLLOWED, f"{_where(insn)} also changes it")
    dst_size = _reg_size(insn.op_register(dst))

    # constants: mov r,imm ; xor r,r
    immediate = src_kind is not None and src_kind not in (ix.OpKind.REGISTER, ix.OpKind.MEMORY)
    if immediate and m == M.MOV:
        raise _Stop(End.CONSTANT, _where(insn))
    if m in _ZEROING and insn.op_count >= 2 and _same_regs(insn):
        raise _Stop(End.CONSTANT, _where(insn))
    if m in _FLOAT_XORS and _negates(walk, insn, i):
        raise _Stop(End.NOT_FOLLOWED, f"{_where(insn)} flips its sign")
    if m in _SCRAMBLE:
        raise _Stop(End.SCRAMBLED, _where(insn))

    if m == M.MOV or m in _FULL_MOVES or m in _SCALAR_MOVES:
        return _move(walk, insn, i, m, srci, dst_size)
    if m in _EXTENDS:
        return _extend(walk, insn, i, _EXTENDS[m], srci)
    if m in _TO_WHOLE:
        before, rounding = _TO_WHOLE[m]
        _need_lane0(walk, insn)
        _check_type(walk, _int_type(dst_size), insn)
        walk.steps.append(Step(Kind.TO_WHOLE, walk.cur, rounding=rounding))
        walk.cur = before
        return _from(walk, insn, i, srci, before.size)
    if m in _TO_DECIMAL:
        _need_lane0(walk, insn)
        _check_type(walk, _TO_DECIMAL[m], insn)
        size = (
            ix.MemorySizeInfo(insn.memory_size).size
            if insn.op_kind(srci) == ix.OpKind.MEMORY
            else _reg_size(insn.op_register(srci))
        )
        walk.steps.append(Step(Kind.TO_DECIMAL, walk.cur))
        walk.cur = _int_type(size)
        return _from(walk, insn, i, srci, size)
    if m in _RESIZE:
        kind, before, after = _RESIZE[m]
        _need_lane0(walk, insn)
        _check_type(walk, after, insn)
        walk.steps.append(Step(kind, walk.cur))
        walk.cur = before
        return _from(walk, insn, i, srci, before.size)
    if m in _MULDIV:
        kind, vt = _MULDIV[m]
        _need_lane0(walk, insn)
        _check_type(walk, vt, insn)
        return _muldiv(walk, insn, i, kind)
    if m == M.IMUL and insn.op_count == 3 and insn.op_kind(2) != ix.OpKind.REGISTER:
        _need_lane0(walk, insn)
        _check_type(walk, _int_type(dst_size), insn)
        walk.steps.append(Step(Kind.MUL, walk.cur, const=float(_signed_imm(insn, 2, dst_size))))
        return _from(walk, insn, i, 1, walk.cur.size)
    if m in _OFFSETS and _reads_back_same(walk, i):
        raise _Stop(End.ALREADY_REAL, _where(insn))
    raise _Stop(End.NOT_FOLLOWED, _where(insn))


def _same_regs(insn: ix.Instruction) -> bool:
    regs = [insn.op_register(k) for k in range(insn.op_count)]
    kinds = [insn.op_kind(k) for k in range(insn.op_count)]
    return all(k == ix.OpKind.REGISTER for k in kinds) and len({full_name(r) for r in regs}) == 1


def _signed_imm(insn: ix.Instruction, op: int, size: int) -> int:
    v = insn.immediate(op) & ((1 << (8 * size)) - 1)
    return v - (1 << (8 * size)) if v >> (8 * size - 1) else v


def _from(walk: _Walk, insn: ix.Instruction, i: int, srci: int, size: int) -> Path | None:
    """The value now comes from operand ``srci``: a load ends the path, a register is
    tracked on."""
    if insn.op_kind(srci) == ix.OpKind.MEMORY:
        return _load(walk, insn, i, walk.lane, ix.MemorySizeInfo(insn.memory_size).size)
    if insn.op_kind(srci) != ix.OpKind.REGISTER:
        raise _Stop(End.CONSTANT, _where(insn))
    walk.tracked = full_name(insn.op_register(srci))
    return None


def _move(
    walk: _Walk, insn: ix.Instruction, i: int, m: int, srci: int, dst_size: int
) -> Path | None:
    src_mem = insn.op_kind(srci) == ix.OpKind.MEMORY
    if m in _FULL_MOVES:
        if insn.op_count != 2:
            raise _Stop(End.NOT_FOLLOWED, _where(insn))
        return _from(walk, insn, i, srci, 16)
    if m in _SCALAR_MOVES:
        width = _SCALAR_MOVES[m]
        if walk.lane >= width:
            if src_mem or m in (M.MOVD, M.VMOVD, M.MOVQ, M.VMOVQ):
                raise _Stop(End.CONSTANT, f"{_where(insn)} clears the rest")
            if insn.op_count == 3:  # vmovss x,x1,x2: the rest comes from x1
                walk.tracked = full_name(insn.op_register(1))
            return None  # movss x,x keeps the rest of the register
        return _from(walk, insn, i, srci, width)
    # mov r, r/m
    if not src_mem and insn.op_register(srci) in _HIGH_BYTES:
        raise _Stop(End.NOT_FOLLOWED, f"a high byte register in {_where(insn)}")
    size = (
        ix.MemorySizeInfo(insn.memory_size).size if src_mem else _reg_size(insn.op_register(srci))
    )
    if walk.lane + walk.cur.size > size:
        if not walk.cur.is_float and walk.lane == 0 and size == 4 and walk.cur.size == 8:
            walk.steps.append(Step(Kind.EXTEND, walk.cur, signed=False))  # mov r32 zero-extends
            walk.cur = ValueType.INT32
            walk.width = min(walk.width, size)
            return _from(walk, insn, i, srci, size)
        raise _Stop(End.NOT_FOLLOWED, f"the value isn't all in {_where(insn)}")
    if src_mem:
        _truncate(walk, size)
    else:
        walk.width = min(walk.width, size)
    return _from(walk, insn, i, srci, size)


def _truncate(walk: _Walk, size: int) -> None:
    """A whole number loaded wider than the value ever was on its way to the copy
    (``mov rax,[m] ; mov [d],eax``): the source is the whole wide number, and the copy
    holds its low bytes. ``mov rax,[m] ; mov [d],rax`` copies all 8 bytes, so the
    watched part is just the same part of the source (no TRUNCATE)."""
    if walk.cur.is_float or walk.lane or size <= walk.width or size not in _INT_TYPES:
        return
    if size <= walk.cur.size:
        return
    walk.steps.append(Step(Kind.TRUNCATE, walk.cur))
    walk.cur = _int_type(size)


def _extend(walk: _Walk, insn: ix.Instruction, i: int, signed: bool, srci: int) -> Path | None:
    _need_lane0(walk, insn)
    if walk.cur.is_float:
        raise _Stop(End.NOT_FOLLOWED, f"{_where(insn)} makes a whole number")
    src_mem = insn.op_kind(srci) == ix.OpKind.MEMORY
    size = (
        ix.MemorySizeInfo(insn.memory_size).size if src_mem else _reg_size(insn.op_register(srci))
    )
    if not src_mem and insn.op_register(srci) in _HIGH_BYTES:
        raise _Stop(End.NOT_FOLLOWED, f"a high byte register in {_where(insn)}")
    if size < walk.cur.size:
        walk.steps.append(Step(Kind.EXTEND, walk.cur, signed=signed))
        walk.cur = _int_type(size)
    elif src_mem:
        _truncate(walk, size)  # movzx eax,word [m] ; mov [d],al
    walk.width = min(walk.width, size)
    return _from(walk, insn, i, srci, size)


def _muldiv(walk: _Walk, insn: ix.Instruction, i: int, kind: Kind) -> Path | None:
    """mulss/divss (and sd, VEX): the other operand must be a constant, or for a
    divide, a loaded value (a max)."""
    if insn.op_count == 3:  # vmulss x0,x1,x2/m: the value is in x1, the factor x2/m
        value_op, other = 1, 2
    else:
        value_op, other = 0, 1
    const, max_load = _constant(walk, insn, i, other)
    commutes = kind is Kind.MUL and insn.op_count == 3 and insn.op_kind(2) == ix.OpKind.REGISTER
    if const is None and max_load is None and commutes:
        # the value may be the other operand (multiplying is commutative)
        const, max_load = _constant(walk, insn, i, 1)
        if const is not None:
            value_op = 2
    if const is not None:
        walk.steps.append(Step(kind, walk.cur, const=const))
    elif kind is Kind.DIV and max_load is not None:
        walk.steps.append(Step(Kind.SHARE, walk.cur))
        walk.max_load = max_load
    elif max_load is not None:
        raise _Stop(End.NOT_FOLLOWED, f"two loaded values multiplied: {_where(insn)}")
    else:
        raise _Stop(End.NOT_FOLLOWED, f"not a fixed number: {_where(insn)}")
    if value_op != 0:
        walk.tracked = full_name(insn.op_register(value_op))
    return None


def _constant(
    walk: _Walk, insn: ix.Instruction, i: int, op: int
) -> tuple[float | None, Load | None]:
    """Operand ``op`` of a mul/div: (its constant value, or the load it comes from
    when that isn't a constant)."""
    double = walk.cur is ValueType.DOUBLE
    if insn.op_kind(op) == ix.OpKind.MEMORY:
        return _mem_constant(walk, insn, i, double)
    if insn.op_kind(op) != ix.OpKind.REGISTER:
        return None, None
    reg = full_name(insn.op_register(op))
    for j in range(i - 1, -1, -1):  # the last write of that register, same window
        prev = walk.win.insns[j]
        if prev.flow_control in (ix.FlowControl.CALL, ix.FlowControl.INDIRECT_CALL):
            return None, None
        if reg not in written_registers(prev):
            continue
        pm = prev.mnemonic
        if prev.op_count < 2 or prev.op_kind(0) != ix.OpKind.REGISTER:
            return None, None
        if pm in _SCALAR_MOVES or pm in _FULL_MOVES or pm == M.MOV:
            k = prev.op_kind(prev.op_count - 1)
            if k == ix.OpKind.MEMORY:
                return _mem_constant(walk, prev, j, double)
            if k == ix.OpKind.REGISTER:
                reg = full_name(prev.op_register(prev.op_count - 1))
                continue  # movd xmm1,eax: follow eax
            if pm == M.MOV:  # mov eax,42C80000h
                return _bits(prev.immediate(1), double), None
        return None, None
    return None, None


def _negates(walk: _Walk, insn: ix.Instruction, i: int) -> bool:
    """``xorps xmm0,[sign mask]``: the other operand is the constant -0.0 (only the sign
    bit set), so the value is negated, not scrambled."""
    for op in (2, 1) if insn.op_count == 3 else (1,):
        const, _load = _constant(walk, insn, i, op)
        if const == 0 and math.copysign(1.0, const) < 0:
            return True
    return False


def _bits(raw: int, double: bool) -> float:
    if double:
        return struct.unpack("<d", struct.pack("<Q", raw & ((1 << 64) - 1)))[0]
    return struct.unpack("<f", struct.pack("<I", raw & 0xFFFFFFFF))[0]


def _mem_constant(
    walk: _Walk, insn: ix.Instruction, i: int, double: bool
) -> tuple[float | None, Load | None]:
    ref = MemRef.of(insn)
    load = Load(insn.ip, text(insn), ref, 0, i)
    if ref.rip_target is None or not walk.is_const(ref.rip_target):
        return None, load
    size = 8 if double else 4
    try:
        raw = walk.read(ref.rip_target, size)
    except Exception:  # unreadable constant: not followed (logged by the caller)
        return None, None
    return _bits(int.from_bytes(raw, "little"), double), None


def _reads_back_same(walk: _Walk, i: int) -> bool:
    """``mov eax,[rdi] ; add eax,ecx ; mov [rdi],eax``: the value is read from the very
    place it is written back to, so the watched address is the real one (AC-T3)."""
    insns = walk.win.insns
    writer_mem = MemRef.of(insns[-1])
    reg = walk.tracked
    for j in range(i - 1, -1, -1):
        insn = insns[j]
        if insn.flow_control != ix.FlowControl.NEXT:
            return False
        if reg not in written_registers(insn):
            continue
        if insn.mnemonic in _OFFSETS:
            continue
        if insn.op_count == 2 and insn.op_kind(1) == ix.OpKind.MEMORY:
            ref = MemRef.of(insn)
            changed = set().union(*(written_registers(x) for x in insns[j:-1]))
            return ref.same_place(writer_mem) and not (set(ref.regs) & changed)
        return False
    return False


# -- working out addresses (AC-T4) -------------------------------------------------------
_MASK64 = (1 << 64) - 1


def _clobbered(insns: tuple[ix.Instruction, ...] | list[ix.Instruction]) -> set[str]:
    """Registers written by ``insns``; a call counts as writing every register it
    doesn't keep."""
    out: set[str] = set()
    for insn in insns:
        out |= written_registers(insn)
        if insn.flow_control in (ix.FlowControl.CALL, ix.FlowControl.INDIRECT_CALL):
            out |= {"rax", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11"}
    return out


def _hex(n: int) -> str:
    return f"{n:X}h" if n >= 0 else f"-{-n:X}h"


@dataclass
class _Resolver:
    """Works out register values at a point in the window: from the trap registers
    when nothing overwrote them since (rule 1), else by replaying the loads that set
    them, reading the pointers from the game now (rule 2)."""

    insns: tuple[ix.Instruction, ...]
    regs: dict[str, int]
    read: Read
    allowed: frozenset[str] | None = None  # trap registers that may be used (caller rule)
    reads: int = 0  # pointers read from memory (more than 0: "replay")

    def reg(self, name: str, at: int, depth: int = 0) -> tuple[int, str] | None:
        """``name``'s value just before ``insns[at]`` and how it was found."""
        if name not in _clobbered(self.insns[at:]):
            if self.allowed is not None and name not in self.allowed:
                return None
            value = self.regs.get(name)
            return None if value is None else (value, name)
        for j in range(at - 1, -1, -1):
            insn = self.insns[j]
            if name not in _clobbered([insn]):
                continue
            return self._defined_by(insn, j, depth)
        return None

    def _defined_by(self, insn: ix.Instruction, j: int, depth: int) -> tuple[int, str] | None:
        m = insn.mnemonic
        if insn.op_count != 2 or insn.op_kind(0) != ix.OpKind.REGISTER:
            return None
        if ix.RegisterExt.size(insn.op0_register) != 8:
            return None  # only whole 64-bit pointers are replayed
        if m == M.MOV and insn.op_kind(1) == ix.OpKind.REGISTER:
            return self.reg(full_name(insn.op1_register), j, depth)
        if m == M.LEA:
            return self.address(MemRef.of(insn), j, depth)
        if m == M.MOV and insn.op_kind(1) == ix.OpKind.MEMORY and depth < REPLAY_DEPTH:
            where = self.address(MemRef.of(insn), j, depth + 1)
            if where is None:
                return None
            try:
                raw = self.read(where[0], 8)
            except Exception as exc:  # a pointer that can't be read: no candidate
                log.debug("action=trace_replay unreadable=%#x error=%s", where[0], exc)
                return None
            self.reads += 1
            return int.from_bytes(raw, "little"), f"[{where[1]}]"
        return None

    def address(self, ref: MemRef, at: int, depth: int = 0) -> tuple[int, str] | None:
        """The address ``ref`` names when ``insns[at]`` runs, and its expression
        (``rbx+10h``, ``[rbx+30h]+8h``; brackets are added by the caller)."""
        if ref.rip_target is not None:
            return ref.rip_target, _hex(ref.rip_target)
        total, parts = ref.disp, []
        for name, scale in ((ref.base, 1), (ref.index, ref.scale)):
            if name is None:
                continue
            got = self.reg(name, at, depth)
            if got is None:
                return None
            total += got[0] * scale
            parts.append(got[1] if scale == 1 else f"{got[1]}*{scale}")
        expr = "+".join(parts)
        if ref.disp or not expr:
            expr += ("+" if expr and ref.disp >= 0 else "") + _hex(ref.disp)
        return total & _MASK64, expr


def store_address(insn: ix.Instruction, hit: WatchHit) -> int | None:
    """Where the writer stored, from the trap registers (a store doesn't change its own
    base or index). None when a register is missing."""
    if insn.op_count == 0 or insn.op0_kind != ix.OpKind.MEMORY:
        return None
    got = _Resolver((insn,), hit.regs, lambda a, n: b"").address(MemRef.of(insn), 1)
    return None if got is None else got[0]


def _expr(got: tuple[int, str], lane: int) -> str:
    """The address as an expression: ``rbx+10h``, ``[[rbx+30h]+8h]+10h`` (a pointer read
    from memory in brackets), plus the lane."""
    return f"{got[1]}+{lane:X}h" if lane else got[1]


def candidates(
    win: Window,
    path: Path,
    hit: WatchHit,
    watched: int,
    read: Read,
    *,
    allowed: frozenset[str] | None = None,
    method: Literal["regs", "caller"] = "regs",
) -> list[Candidate]:
    """Source addresses for ``path`` from one trap (AC-T4 rules 1-3).

    1. regs: the load's registers weren't written between it and the writer:
       base + index x scale + disp (+ the lane). RIP-relative: the fixed address.
    2. replay: a register was overwritten by an earlier ``mov r,[m2]``: m2 is worked
       out the same way (up to REPLAY_DEPTH reads) and the pointer read from memory now.
    3. block: ``rep movs``: watched + (rsi - rdi).
    ``allowed`` limits rule 1 to some trap registers (``method="caller"``)."""
    writer = win.writer.ip
    if path.end is End.BLOCK_COPY:
        rsi, rdi = hit.regs.get("rsi"), hit.regs.get("rdi")
        if rsi is None or rdi is None:
            return []
        return [
            Candidate((watched + rsi - rdi) & _MASK64, "block", "watched+(rsi-rdi)", None, writer)
        ]
    if path.end is not End.SOURCE or path.load is None:
        return []
    res = _Resolver(win.insns, hit.regs, read, allowed)
    got = res.address(path.load.mem, path.load.at)
    if got is None:
        return []
    max_address = None
    if path.max_load is not None:
        mres = _Resolver(win.insns, hit.regs, read, allowed)
        mgot = mres.address(path.max_load.mem, path.max_load.at)
        if mgot is None:
            return []  # the max is part of the way: without it the way can't be checked
        max_address = mgot[0]
    kind: Literal["regs", "replay", "block", "caller", "exec"]
    kind = method if method == "caller" else ("replay" if res.reads else "regs")
    address = (got[0] + path.load.lane) & _MASK64
    return [Candidate(address, kind, _expr(got, path.load.lane), max_address, writer)]


# -- one caller up (AC-T4 rule 3) --------------------------------------------------------
STACK_SEARCH = 512  # bytes of stack searched for return addresses
ARGUMENT_REGS = frozenset({"rdi", "rsi", "rdx", "rcx", "r8", "r9"} | {f"xmm{i}" for i in range(8)})


def _is_call(insn: ix.Instruction) -> bool:
    return insn.flow_control in (ix.FlowControl.CALL, ix.FlowControl.INDIRECT_CALL)


def _call_before(read: Read, ret: int) -> tuple[int, bytes, str] | None:
    """The call instruction that ends at return address ``ret``, if there is one."""
    for n in (2 * MAX_INSN, MAX_INSN):
        try:
            before = read(ret - n, n)
        except Exception:  # near the start of a map: try fewer bytes
            continue
        return instruction_before(before, ret, want=_is_call)
    return None


def call_sites(stack: bytes, rsp: int, code_range: tuple[int, int], read: Read) -> list[int]:
    """Return addresses on the stack at the trap (nearest first): qwords inside the
    game's code whose preceding bytes decode to a call ending exactly there."""
    lo, hi = code_range
    out: list[int] = []
    for off in range(0, len(stack) - 7, 8):
        (v,) = struct.unpack_from("<Q", stack, off)
        if lo <= v < hi and v not in out and _call_before(read, v) is not None:
            out.append(v)
    log.debug("action=trace_callers rsp=%#x found=%s", rsp, ",".join(f"{a:#x}" for a in out))
    return out


def caller_window(read: Read, ret: int) -> Window | None:
    """The caller's code block that ends with the call returning to ``ret``."""
    found = _call_before(read, ret)
    if found is None:
        return None
    call_ip, call_code, _text = found
    for n in (WINDOW, 64, 16, 0):
        try:
            code = read(call_ip - n, n) + call_code
        except Exception:  # near the start of a map
            continue
        return decode_window(code, call_ip, len(call_code))
    return None


def follow_into_caller(
    read: Read,
    ret: int,
    reg: str,
    lane: int,
    hit: WatchHit,
    is_const: IsConst,
    shown: ValueType,
    callee: Window,
    watched: int,
    width: int = 16,
) -> tuple[Path, list[Candidate]]:
    """The value arrived in argument register ``reg`` at the start of the callee
    (``callee``): follow it back in the caller from the call that returns to ``ret``,
    then work out the address with only the registers a call keeps (rbx, rbp,
    r12-r15) from the trap, and only those the callee's own code doesn't write.
    ``shown`` is the type the callee's path starts from (its way's ``real``)."""
    win = caller_window(read, ret)
    if win is None:
        return Path(End.NOT_FOLLOWED, None, reason=f"no call before {ret:#x}"), []
    path = follow_reg(win, reg, lane, read, is_const, shown, width=width)
    written = set().union(*(written_registers(i) for i in callee.insns))
    allowed = frozenset(CALLEE_SAVED - written)
    cands = candidates(win, path, hit, watched, read, allowed=allowed, method="caller")
    return path, cands


def exec_address(load: Load, hit: WatchHit) -> int | None:
    """The address ``load`` reads, from an execute breakpoint's trap on it: the
    registers are those before the load runs, so this is exact (AC-T4 rule 4).
    None when a register is missing."""
    ref = load.mem
    if ref.rip_target is not None:
        return (ref.rip_target + load.lane) & _MASK64
    total = ref.disp + load.lane
    for name, scale in ((ref.base, 1), (ref.index, ref.scale)):
        if name is None:
            continue
        value = hit.regs.get(name)
        if value is None:
            return None
        total += value * scale
    return total & _MASK64
