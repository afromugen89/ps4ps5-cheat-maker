"""Lock it, "player only": a code cave that skips the damage write just for the player.

Games often write everyone's health with one instruction, e.g. at Returnal's
``vmovss [r12+0x2AC], xmm1``. Switching it off makes enemies immortal too. Instead:

1. **Collect.** Hook the instruction with a tiny routine that records the object
   register (r12 above) in a table, while the player fights for ~20 s. That gives the
   player's object and the enemies' objects.
2. **Compare.** Read the start of every object and find a field where the player's
   value differs from all the others (Returnal: +0x08 == 0x40048).
3. **Cave.** In spare zero bytes at the end of the game's code, write::

       lea rsp,[rsp-128] ; pushfq              ; keep the red zone and the flags
       cmp dword [r12+field], player_value
       jne normal
       popfq ; lea rsp,[rsp+128]
       vmovss [r12+0x2AC], xmm1                ; player: the game's write runs first,
       mov [r12+0x2AC], <the value you want>   ;   then the kept value goes over it
       <instructions after the writer that the hook covered>
       jmp back
     normal:
       popfq ; lea rsp,[rsp+128]
       <the original instructions>             ; everyone else: as before
       jmp back

   and replace the writer with ``jmp cave`` + NOPs. Both are plain byte patches, so
   the cheat saves to GoldHEN / CheatRunner and works after a restart.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

import iced_x86 as ix

from cheatmaker.console.client import MemMap
from cheatmaker.core.cheats import Cheat, Location, LocationKind, value_positions
from cheatmaker.core.locker import Read, Writer, nop_unsafe, writes_memory
from cheatmaker.core.session import Session
from cheatmaker.core.values import ValueType
from cheatmaker.errors import LockFailed, ScanCancelled

log = logging.getLogger(__name__)

JMP_LEN = 5
TABLE_SLOTS = 64
TABLE_SIZE = TABLE_SLOTS * 8
COMPARE_BYTES = 0x200  # how much of each object to compare
CAVE_ROOM = 256  # bytes reserved for one cave
MAX_SLOTS = 64  # find_slot looks this far below the table for a free 16-byte slot
SEARCH_TAIL = 0x10000  # look for spare bytes in the last 64 KiB of the code
RED_ZONE = 128  # SysV leaf functions may use 128 bytes below rsp
_info = ix.InstructionInfoFactory()


@dataclass(frozen=True)
class Target:
    """What the writer instruction stores to: ``[base + disp]``."""

    base: int  # an iced_x86.Register value
    disp: int

    @property
    def base_name(self) -> str:
        return _reg_name(self.base)


def _reg_name(reg: int) -> str:
    for name in dir(ix.Register):
        if not name.startswith("_") and getattr(ix.Register, name) == reg:
            return name.lower()
    return str(reg)


def decode_at(code: bytes, ip: int) -> list[ix.Instruction]:
    return list(ix.Decoder(64, code, ip=ip))


def writer_target(writer: Writer) -> Target:
    """``[base + disp]`` of the writer. Refuses forms the cave can't handle."""
    insn = ix.Decoder(64, writer.code, ip=writer.address).decode()
    base, index = insn.memory_base, insn.memory_index
    if base in (ix.Register.NONE, ix.Register.RIP, ix.Register.RSP) or index != ix.Register.NONE:
        raise LockFailed(
            "This instruction doesn't write through one object register, so 'player only' "
            "can't tell characters apart. Try another option."
        )
    disp = insn.memory_displacement
    if disp == 0:
        # [r14] points at the value itself, not a character: comparing [r14+field]
        # reads whatever sits after the value, and the routine is usually shared by
        # many values. This crashed Returnal on Kein's PS5 (2026-11-08).
        raise LockFailed(
            "This instruction writes through a pointer to the value itself, so it's "
            "probably shared by many values and 'player only' could crash the game. "
            "Try another option."
        )
    if disp >= 1 << 63:
        disp -= 1 << 64
    return Target(base, disp)


def hook_span(read: Read, site: int, single: bool = False) -> list[ix.Instruction]:
    """Whole instructions starting at ``site`` covering at least the 5-byte jmp.

    Refuses spans that would break the game: a ``ret``/jump/branch before the last
    instruction (the jump would cover the next function or code reached from
    elsewhere), and any ``call`` (its return would land in the cave, which may be gone
    by then). ``single``: the hooked instruction alone must be 5+ bytes, so no other
    code can jump into the middle of the patch."""
    insns, total = [], 0
    for insn in decode_at(read(site, 32), site):
        if insn.code == ix.Code.INVALID:
            raise LockFailed("couldn't read the game code at the hook")
        if insn.flow_control in (ix.FlowControl.CALL, ix.FlowControl.INDIRECT_CALL):
            raise LockFailed("the game code there calls another part of the game")
        if insns and insns[-1].flow_control != ix.FlowControl.NEXT:
            raise LockFailed("the instruction is too short to patch safely there")
        insns.append(insn)
        total += insn.len
        if total >= JMP_LEN:
            if single and len(insns) > 1:
                raise LockFailed("the instruction is too short to patch safely there")
            return insns
    raise LockFailed("couldn't fit a jump at the hook")


def jmp_patch(site: int, target: int, length: int) -> bytes:
    rel = target - (site + JMP_LEN)
    if not -(1 << 31) <= rel < 1 << 31:
        raise LockFailed("the spare space is too far from the instruction")
    return b"\xe9" + struct.pack("<i", rel) + b"\x90" * (length - JMP_LEN)


def _encode(insns: list[ix.Instruction], ip: int) -> bytes:
    enc = ix.BlockEncoder(64)
    enc.add_many(insns)
    return bytes(enc.encode(ip))


def _mem(base, disp: int = 0, index=ix.Register.NONE, scale: int = 1) -> ix.MemoryOperand:
    return ix.MemoryOperand(base, index, scale, disp, 8 if disp else 0)


def _label(insn: ix.Instruction, ip: int) -> ix.Instruction:
    insn.ip = ip
    return insn


def _scratch(avoid) -> tuple:
    regs = [r for r in (ix.Register.RAX, ix.Register.RBX, ix.Register.RCX) if r != avoid]
    return regs[0], regs[1]


def sampler_code(span: list[ix.Instruction], target: Target, table: int, cave: int) -> bytes:
    """Record the object register into a 64-slot table, then run the original code."""
    back = span[-1].next_ip
    a, b = _scratch(target.base)
    C, Ins = ix.Code, ix.Instruction
    body = [
        Ins.create_reg_mem(C.LEA_R64_M, ix.Register.RSP, _mem(ix.Register.RSP, -RED_ZONE)),
        Ins.create(C.PUSHFQ),
        Ins.create_reg(C.PUSH_R64, a),
        Ins.create_reg(C.PUSH_R64, b),
        Ins.create_reg_reg(C.MOV_R64_RM64, a, target.base),
        Ins.create_reg_i32(C.SHR_RM64_IMM8, a, 4),
        Ins.create_reg_i32(C.AND_RM64_IMM8, a, TABLE_SLOTS - 1),
        Ins.create_reg_mem(C.LEA_R64_M, b, _mem(ix.Register.RIP, table)),
        Ins.create_mem_reg(C.MOV_RM64_R64, _mem(b, 0, a, 8), target.base),
        Ins.create_reg(C.POP_R64, b),
        Ins.create_reg(C.POP_R64, a),
        Ins.create(C.POPFQ),
        Ins.create_reg_mem(C.LEA_R64_M, ix.Register.RSP, _mem(ix.Register.RSP, RED_ZONE)),
        *[i.copy() for i in span],
        Ins.create_branch(C.JMP_REL32_64, back),
    ]
    return _encode(body, cave)


def _stores(target: Target, value: bytes) -> list[ix.Instruction]:
    """``mov [base+disp], value`` as immediate stores (two dwords for 8 bytes)."""
    C, Ins = ix.Code, ix.Instruction
    mem = _mem(target.base, target.disp)
    if len(value) == 8:
        lo, hi = struct.unpack("<II", value)
        return [
            Ins.create_mem_u32(C.MOV_RM32_IMM32, mem, lo),
            Ins.create_mem_u32(C.MOV_RM32_IMM32, _mem(target.base, target.disp + 4), hi),
        ]
    if len(value) == 4:
        return [Ins.create_mem_u32(C.MOV_RM32_IMM32, mem, struct.unpack("<I", value)[0])]
    if len(value) == 2:
        return [Ins.create_mem_u32(C.MOV_RM16_IMM16, mem, struct.unpack("<H", value)[0])]
    if len(value) == 1:
        return [Ins.create_mem_u32(C.MOV_RM8_IMM8, mem, value[0])]
    raise LockFailed("can't write a value of that size")


def player_only_code(
    span: list[ix.Instruction],
    target: Target,
    field: int,
    player_value: int,
    value: bytes,
    cave: int,
) -> bytes:
    """The cave: the player gets ``value`` instead of the game's write.

    The ``cmp`` runs between ``pushfq`` and ``popfq`` (past the red zone), so the game
    never sees flags from our check. On the player's path the game's writer still runs
    first, then ``value`` goes over it, so the flags it leaves are the game's own (an
    older version skipped the writer and left our ``cmp`` flags behind)."""
    back = span[-1].next_ip
    C, Ins = ix.Code, ix.Instruction
    normal_ip = 0xFFFF_0000  # placeholder label, resolved by the encoder
    normal = [*_unstack(), *[i.copy() for i in span]]
    normal[0] = _label(normal[0], normal_ip)
    copies = [_label(i.copy(), 0xFFFE_0000 + k) for k, i in enumerate(span)]
    body = [
        *_stack(),
        Ins.create_mem_u32(C.CMP_RM32_IMM32, _mem(target.base, field), player_value & 0xFFFFFFFF),
        Ins.create_branch(C.JNE_REL32_64, normal_ip),
        *_unstack(),
        copies[0],  # the game's write: its flags, its other effects
        *_stores(target, value),
        *copies[1:],  # what the hook covered after the writer
        Ins.create_branch(C.JMP_REL32_64, back),
        *normal,
        Ins.create_branch(C.JMP_REL32_64, back),
    ]
    return _encode(body, cave)


def _stack() -> list[ix.Instruction]:
    """Step past the red zone and keep the game's flags."""
    C, Ins = ix.Code, ix.Instruction
    return [
        Ins.create_reg_mem(C.LEA_R64_M, ix.Register.RSP, _mem(ix.Register.RSP, -RED_ZONE)),
        Ins.create(C.PUSHFQ),
    ]


def _unstack() -> list[ix.Instruction]:
    """Undo ``_stack``: the game's flags and stack as they were."""
    C, Ins = ix.Code, ix.Instruction
    return [
        Ins.create(C.POPFQ),
        Ins.create_reg_mem(C.LEA_R64_M, ix.Register.RSP, _mem(ix.Register.RSP, RED_ZONE)),
    ]


SLOT_SIZE = 16
"""Capture slot: the object address (8 bytes), then a counter of captures (8 bytes)."""
IMM32_MAX = (1 << 31) - 1
"""A vtable up to here fits ``cmp qword [obj], imm32``; above it the routine loads it
into a scratch register first."""


@dataclass(frozen=True)
class Filter:
    """How a routine tells the wanted object from the others: ``kind`` "vtable"
    (``cmp qword [obj], vtable``) or "field" (``cmp dword [obj+field], field_value``)."""

    kind: str
    vtable: int | None = None
    field: int = 0
    field_value: int = 0


def _filter_check(obj, flt: Filter) -> list[ix.Instruction]:
    """The compare that sets ZF when ``obj`` is the wanted object (jne skips it)."""
    C, Ins = ix.Code, ix.Instruction
    if flt.kind == "vtable":
        vtable = flt.vtable
        if vtable is None or not 0 < vtable < 1 << 64:
            raise LockFailed("the character's type can't be checked from this place in memory")
        if vtable <= IMM32_MAX:
            return [Ins.create_mem_i32(C.CMP_RM64_IMM32, _mem(obj), vtable)]
        tmp, _ = _scratch(obj)  # push/mov/pop don't touch the flags, so jne sees the cmp
        return [
            Ins.create_reg(C.PUSH_R64, tmp),
            Ins.create_reg_u64(C.MOV_R64_IMM64, tmp, vtable),
            Ins.create_mem_reg(C.CMP_RM64_R64, _mem(obj), tmp),
            Ins.create_reg(C.POP_R64, tmp),
        ]
    if flt.kind == "field":
        value = flt.field_value & 0xFFFFFFFF
        return [Ins.create_mem_u32(C.CMP_RM32_IMM32, _mem(obj, flt.field), value)]
    raise ValueError(f"unknown filter {flt.kind!r}")


def capture_code(
    span: list[ix.Instruction],
    target: Target,
    slot: int,
    cave: int,
    filter_kind: str = "vtable",
    vtable: int | None = None,
    field: int = 0,
    field_value: int = 0,
    value: bytes | None = None,
) -> bytes:
    """The "find it again" capture routine (architecture v3 §5)::

        lea rsp,[rsp-128] ; pushfq          ; keep the red zone and the flags
        cmp qword [obj], vtable             ; filter: only the object we want
        jne other                           ;   (or cmp dword [obj+field], value)
        mov [slot], obj                     ; remember where it is now
        inc qword [slot+8]                  ; and that the routine ran
      [ popfq ; lea rsp,[rsp+128]          ; optional "keep at value" (player only):
        [the game's writer]                 ;   writer: runs, then our value over it
        mov [obj+disp], value               ;   reader: our value before it reads
        <rest of the hooked code> ; jmp back ]
      other:
        popfq ; lea rsp,[rsp+128]
        <the original instructions> ; jmp back

    Only the flags change before the original code runs, and the object register is
    read before the hooked instruction, so it works on readers that overwrite it
    (``mov rax,[rax+2ACh]``). ``vtable`` must fit a sign-extended imm32 (the game's
    module sits below 2 GiB on the PS5, base 0x400000 in C1)."""
    C, Ins = ix.Code, ix.Instruction
    back = span[-1].next_ip
    other_ip = 0xFFFF_0000  # placeholder label, resolved by the encoder
    obj = target.base
    if filter_kind == "none":
        if value is not None:
            raise LockFailed("keeping a value needs a way to tell you apart from the others")
        check = []
    else:
        check = _filter_check(obj, Filter(filter_kind, vtable, field, field_value))
    capture = [
        *check,
        *([Ins.create_branch(C.JNE_REL32_64, other_ip)] if check else []),
        Ins.create_mem_reg(C.MOV_RM64_R64, _mem(ix.Register.RIP, slot), obj),
        Ins.create_mem(C.INC_RM64, _mem(ix.Register.RIP, slot + 8)),
    ]
    keep: list[ix.Instruction] = []
    if value is not None:
        # A writer still runs first (``sub [rax+2ACh],ecx ; jle dead`` must leave the
        # flags the game's next branch expects, from our kept value), then our value
        # goes over its write. A reader gets our value before it reads.
        n = 1 if writes_memory(span[0]) else 0
        copies = [_label(i.copy(), 0xFFFE_0000 + k) for k, i in enumerate(span)]
        keep = [
            *_unstack(),
            *copies[:n],
            *_stores(target, value),
            *copies[n:],
            Ins.create_branch(C.JMP_REL32_64, back),
        ]
    other = _unstack()
    other[0] = _label(other[0], other_ip)
    body = [
        *_stack(),
        *capture,
        *keep,
        *other,
        *[i.copy() for i in span],
        Ins.create_branch(C.JMP_REL32_64, back),
    ]
    return _encode(body, cave)


# -- "Make it work" routines (docs/architecture-cheat-builder.md §1) -------------------
class Effect(Enum):
    NEVER_DOWN = "Never goes down"
    ALWAYS_FULL = "Always full"
    HALF = "Half damage"


EFFECT_TYPES = (ValueType.FLOAT, ValueType.INT32, ValueType.INT64)
UNSUPPORTED_TYPE = "This kind of value isn't supported yet. Use Lock it."
BUILD_CAVE_SKIP = 5 * CAVE_ROOM
"""Builder rooms go below this much of the code's spare end (sampler, Player only and
"find it again" caves use the rooms above)."""
_HALF_F32 = 0x3F00_0000  # 0.5 as a float
_LOW32 = {ix.Register.RAX: ix.Register.EAX, ix.Register.RBX: ix.Register.EBX}


def effect_code(
    span: list[ix.Instruction],
    target: Target,
    vtype: ValueType,
    effect: Effect,
    cave: int,
    *,
    full: bytes | None = None,
    counter: int | None = None,
    only_me: Filter | None = None,
    flags_fix: bool = False,
) -> bytes:
    """The builder's routine for the writer ``span[0]``::

        lea rsp,[rsp-128]                   ; past the red zone
      [ pushfq ; cmp <only_me> ; jne other ; popfq ]
        push qword [obj+disp]               ; the old value (4-byte ones via the scratch)
        <the game's writer>                 ; runs first: the game's flags
        pushfq ; push rax [; save xmm0, xmm1]
      [ inc qword [counter] ; mov [counter+8], <the game's new value> ]  (test build)
        <effect: compare new with old ; jump to game_ok to keep the game's write>
        mov [obj+disp], <our value>
        restore ; popfq [; cmp [obj+disp],0 when flags_fix]
      tail:
        lea rsp,[rsp+136] ; <rest of the hooked code> ; jmp back
      game_ok:
        restore ; popfq ; jmp tail
      other:
        popfq ; lea rsp,[rsp+128] ; <the original code> ; jmp back

    ``flags_fix``: ZF and SF come from the value we stored and OF is 0 (for a writer
    like ``sub`` whose result the game checks next). ``full`` is the value Always full
    stores (an immediate, so ``value_positions`` keeps it editable). Only legacy SSE
    is used: VEX would clear the upper YMM bits the routine doesn't save. The only
    general register used is rax (rbx when the object is in rax), saved first."""
    if vtype not in EFFECT_TYPES:
        raise LockFailed(UNSUPPORTED_TYPE)
    if effect is Effect.ALWAYS_FULL and (full is None or len(full) != vtype.size):
        raise LockFailed("Always full needs the full value")
    check_effect_writer(span[0], target, vtype)
    C, Ins, R = ix.Code, ix.Instruction, ix.Register
    labels = iter(range(0xFFFF_0000, 0xFFFF_1000))
    back = span[-1].next_ip
    obj, size = target.base, vtype.size
    value = _mem(obj, target.disp)
    s64, _ = _scratch(obj)
    s = s64 if size == 8 else _LOW32[s64]
    floats = vtype.is_float and effect is not Effect.ALWAYS_FULL
    old = _mem(R.RSP, 48 if floats else 16)  # pushed before rflags, the scratch, xmm0/1
    mov_r_m = C.MOV_R64_RM64 if size == 8 else C.MOV_R32_RM32
    mov_m_r = C.MOV_RM64_R64 if size == 8 else C.MOV_RM32_R32

    def mark(insns: list[ix.Instruction]) -> tuple[int, list[ix.Instruction]]:
        ip = next(labels)
        insns[0] = _label(insns[0], ip)
        return ip, insns

    def copies(insns: list[ix.Instruction]) -> list[ix.Instruction]:
        return [_label(i.copy(), next(labels)) for i in insns]

    def restore() -> list[ix.Instruction]:
        out = []
        if floats:
            out += [
                Ins.create_reg_mem(C.MOVUPS_XMM_XMMM128, R.XMM1, _mem(R.RSP, 16)),
                Ins.create_reg_mem(C.MOVUPS_XMM_XMMM128, R.XMM0, _mem(R.RSP)),
                Ins.create_reg_mem(C.LEA_R64_M, R.RSP, _mem(R.RSP, 32)),
            ]
        return [*out, Ins.create_reg(C.POP_R64, s64), Ins.create(C.POPFQ)]

    other_ip, other = mark([*_unstack(), *copies(span), Ins.create_branch(C.JMP_REL32_64, back)])
    tail_ip, tail = mark(
        [
            Ins.create_reg_mem(C.LEA_R64_M, R.RSP, _mem(R.RSP, RED_ZONE + 8)),
            *copies(span[1:]),
            Ins.create_branch(C.JMP_REL32_64, back),
        ]
    )
    game_ok_ip, game_ok = mark([*restore(), Ins.create_branch(C.JMP_REL32_64, tail_ip)])

    body = [Ins.create_reg_mem(C.LEA_R64_M, R.RSP, _mem(R.RSP, -RED_ZONE))]
    if only_me is not None:
        body += [
            Ins.create(C.PUSHFQ),
            *_filter_check(obj, only_me),
            Ins.create_branch(C.JNE_REL32_64, other_ip),
            Ins.create(C.POPFQ),
        ]
    body += [
        *_push_old(value, s64, size),
        _label(span[0].copy(), next(labels)),  # the game's own write
        Ins.create(C.PUSHFQ),
        Ins.create_reg(C.PUSH_R64, s64),
    ]
    if floats:
        body += [
            Ins.create_reg_mem(C.LEA_R64_M, R.RSP, _mem(R.RSP, -32)),
            Ins.create_mem_reg(C.MOVUPS_XMMM128_XMM, _mem(R.RSP), R.XMM0),
            Ins.create_mem_reg(C.MOVUPS_XMMM128_XMM, _mem(R.RSP, 16), R.XMM1),
        ]
    if counter is not None:
        body += _counter_lines(counter, s, value, mov_r_m, mov_m_r)
    if effect is Effect.ALWAYS_FULL and size == 8:  # one store: never half written
        body += [
            Ins.create_reg_u64(C.MOV_R64_IMM64, s64, struct.unpack("<Q", full or b"")[0]),
            Ins.create_mem_reg(C.MOV_RM64_R64, value, s64),
        ]
    elif effect is Effect.ALWAYS_FULL:
        body += _stores(target, full or b"")
    elif vtype.is_float:
        body += _float_effect(effect, value, old, s, game_ok_ip)
    else:
        body += _int_effect(effect, value, old, s, size, game_ok_ip, mov_r_m, mov_m_r)
    body += restore()
    if flags_fix:
        cmp0 = C.CMP_RM64_IMM8 if size == 8 else C.CMP_RM32_IMM8
        body.append(Ins.create_mem_i32(cmp0, value, 0))
    body += tail
    if effect is not Effect.ALWAYS_FULL:
        body += game_ok
    if only_me is not None:
        body += other
    code = _encode(body, cave)
    if len(code) > CAVE_ROOM:
        raise LockFailed("the fix doesn't fit in the spare space")
    return code


def _push_old(value: ix.MemoryOperand, s64, size: int) -> list[ix.Instruction]:
    """Push the old value without changing a flag or a register. A 4-byte value is
    read as 4 bytes through the scratch register: ``push qword`` would read 4 more,
    past the end of the page when the value is its last bytes."""
    C, Ins, R = ix.Code, ix.Instruction, ix.Register
    if size == 8:
        return [Ins.create_mem(C.PUSH_RM64, value)]
    return [
        Ins.create_reg(C.PUSH_R64, s64),  # the old value's place (shorter than a lea)
        Ins.create_reg(C.PUSH_R64, s64),
        Ins.create_reg_mem(C.MOV_R32_RM32, _LOW32[s64], value),
        Ins.create_mem_reg(C.MOV_RM64_R64, _mem(R.RSP, 8), s64),
        Ins.create_reg(C.POP_R64, s64),
    ]


def check_effect_writer(insn: ix.Instruction, target: Target, vtype: ValueType) -> None:
    """The routine runs the writer and then reads ``[obj+disp]`` back with the stack
    moved, so the writer must store exactly the value, through ``target``, and change
    no register (rsp included)."""
    if not writes_memory(insn) or (insn.memory_base, insn.memory_displacement) != (
        target.base,
        target.disp & ((1 << 64) - 1),
    ):
        raise LockFailed("the instruction doesn't write the value")
    if ix.MemorySizeInfo(insn.memory_size).size != vtype.size:
        raise LockFailed("the instruction writes more than this value")
    unsafe = nop_unsafe(insn)
    if unsafe is not None:
        raise LockFailed(f"the instruction can't be patched safely: {unsafe}")
    used = _info.info(insn).used_registers()
    if any(ix.RegisterExt.full_register(r.register) == ix.Register.RSP for r in used):
        raise LockFailed("the instruction uses the stack")


def _counter_lines(slot: int, s, value: ix.MemoryOperand, mov_r_m, mov_m_r) -> list:
    """Count the run and keep the game's new value (test build only)."""
    C, Ins = ix.Code, ix.Instruction
    return [
        Ins.create_mem(C.INC_RM64, _mem(ix.Register.RIP, slot)),
        Ins.create_reg_mem(mov_r_m, s, value),
        Ins.create_mem_reg(mov_m_r, _mem(ix.Register.RIP, slot + 8), s),
    ]


def _int_effect(effect, value, old, s, size, game_ok: int, mov_r_m, mov_m_r) -> list:
    C, Ins = ix.Code, ix.Instruction
    wide = size == 8
    if effect is Effect.NEVER_DOWN:
        return [
            Ins.create_reg_mem(mov_r_m, s, value),
            Ins.create_reg_mem(C.CMP_R64_RM64 if wide else C.CMP_R32_RM32, s, old),
            Ins.create_branch(C.JGE_REL32_64, game_ok),  # not lower: the game's write
            Ins.create_reg_mem(mov_r_m, s, old),
            Ins.create_mem_reg(mov_m_r, value, s),
        ]
    # Half damage: drop d = old - new; new = old - (d + 1) / 2, so a drop of 1 stays 1.
    # d + 1 fits unsigned (d is positive and didn't overflow), hence shr.
    return [
        Ins.create_reg_mem(mov_r_m, s, old),
        Ins.create_reg_mem(C.SUB_R64_RM64 if wide else C.SUB_R32_RM32, s, value),
        Ins.create_branch(C.JO_REL32_64, game_ok),  # too far apart to halve
        Ins.create_branch(C.JLE_REL32_64, game_ok),  # not lower: the game's write
        Ins.create_reg(C.INC_RM64 if wide else C.INC_RM32, s),
        Ins.create_reg_i32(C.SHR_RM64_1 if wide else C.SHR_RM32_1, s, 1),
        Ins.create_reg(C.NEG_RM64 if wide else C.NEG_RM32, s),
        Ins.create_reg_mem(C.ADD_R64_RM64 if wide else C.ADD_R32_RM32, s, old),
        Ins.create_mem_reg(mov_m_r, value, s),
    ]


def _float_effect(effect, value, old, s, game_ok: int) -> list:
    C, Ins, R = ix.Code, ix.Instruction, ix.Register
    compare = [
        Ins.create_reg_mem(C.MOVSS_XMM_XMMM32, R.XMM0, value),
        Ins.create_reg_mem(C.UCOMISS_XMM_XMMM32, R.XMM0, old),
        Ins.create_branch(C.JP_REL32_64, game_ok),  # NaN: the game's write
        Ins.create_branch(C.JAE_REL32_64, game_ok),  # not lower: the game's write
    ]
    if effect is Effect.NEVER_DOWN:
        return [
            *compare,
            Ins.create_reg_mem(C.MOV_R32_RM32, s, old),  # the old bits, exactly
            Ins.create_mem_reg(C.MOV_RM32_R32, value, s),
        ]
    # Half damage: new/2 + old/2 (the same as (old+new)/2, but can't overflow).
    return [
        *compare,
        Ins.create_reg_u32(C.MOV_R32_IMM32, s, _HALF_F32),
        Ins.create_reg_reg(C.MOVD_XMM_RM32, R.XMM1, s),
        Ins.create_reg_reg(C.MULSS_XMM_XMMM32, R.XMM0, R.XMM1),
        Ins.create_reg_mem(C.MULSS_XMM_XMMM32, R.XMM1, old),
        Ins.create_reg_reg(C.ADDSS_XMM_XMMM32, R.XMM0, R.XMM1),
        Ins.create_mem_reg(C.MOVSS_XMMM32_XMM, value, R.XMM0),
    ]


def find_slot(session: Session, extra_reserved: Iterable[tuple[int, int]] = ()) -> int:
    """16 zero bytes just below the Player-only table at the end of the game's writable
    data. The caller still has to prove the game never writes there (C10b). Never in
    ``session.reserved_rooms`` nor ``extra_reserved`` (module offsets, start and end)."""
    extra = list(extra_reserved)
    game = session.require_game()
    client = session.require_client()
    data_maps = [m for m in _module_maps(session) if m.writable and not m.executable]
    if not data_maps:
        raise LockFailed("couldn't find the game's data area")
    last = max(data_maps, key=lambda m: m.end)
    top = (last.end - TABLE_SIZE - SLOT_SIZE) & ~15
    for k in range(MAX_SLOTS):  # the first one not kept by another value's notes
        addr = top - k * SLOT_SIZE
        if addr < last.start:
            break
        if _reserved(session, addr, SLOT_SIZE, extra):
            continue
        if client.read(game.pid, addr, SLOT_SIZE) != bytes(SLOT_SIZE):
            break
        return addr
    raise LockFailed("no spare room in the game's data to remember where things are")


def _reserved(
    session: Session, addr: int, size: int, extra: Iterable[tuple[int, int]] = ()
) -> bool:
    base = session.require_game().module_base
    return any(
        addr < base + end and base + start < addr + size
        for start, end in [*session.reserved_rooms, *extra]
    )


# -- where things go ------------------------------------------------------------
def _module_maps(session: Session) -> list[MemMap]:
    game = session.require_game()
    maps = session.require_client().maps(game.pid)
    return [m for m in maps if game.module_base <= m.start < game.module_end]


def find_cave(
    session: Session,
    size: int,
    skip: int = 0,
    extra_reserved: Iterable[tuple[int, int]] = (),
) -> int:
    """A run of zero bytes near the end of the game's code (16-byte aligned).
    ``skip`` reserves that many bytes at the very end (for another cave), and the
    rooms in ``session.reserved_rooms`` are never used (an earlier sampler's code left
    in place pushes caves down, but never into a room a .cmproj keeps).
    ``extra_reserved`` adds more such rooms (module offsets, start and end), e.g. the
    code lines of cheats that are open."""
    game = session.require_game()
    client = session.require_client()
    code = [m for m in _module_maps(session) if m.executable]
    if not code:
        raise LockFailed("couldn't find the game's code")
    last = max(code, key=lambda m: m.end)
    start = max(last.start, last.end - SEARCH_TAIL)
    data = bytearray(client.read(game.pid, start, last.end - start))
    base = game.module_base
    for lo, hi in [*session.reserved_rooms, *extra_reserved]:  # never zero these
        a, b = max(base + lo, start), min(base + hi, last.end)
        if a < b:
            data[a - start : b - start] = b"\xff" * (b - a)
    end = len(data) - skip
    run = 0
    for i in range(end - 1, -1, -1):
        run = run + 1 if data[i] == 0 else 0
        if run >= size + 32:
            addr = (start + i + 16 + 15) & ~15  # keep a gap after the game's last code
            return addr
    raise LockFailed("no spare space at the end of the game's code for the patch")


def find_table(session: Session) -> int:
    """Zero bytes at the very end of the game's writable data for the sampler table."""
    game = session.require_game()
    client = session.require_client()
    data_maps = [m for m in _module_maps(session) if m.writable and not m.executable]
    if not data_maps:
        raise LockFailed("couldn't find the game's data area")
    last = max(data_maps, key=lambda m: m.end)
    addr = last.end - TABLE_SIZE
    if client.read(game.pid, addr, TABLE_SIZE) != bytes(TABLE_SIZE):
        raise LockFailed("no spare room in the game's data to collect characters")
    return addr


# -- collect + compare ------------------------------------------------------------
def collect_objects(
    session: Session,
    writer: Writer,
    seconds: float,
    cancel: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
    extra_reserved: Iterable[tuple[int, int]] = (),
) -> list[int]:
    """Hook ``writer`` for ``seconds`` and return the object addresses it wrote to. The
    sampler never goes in ``extra_reserved`` rooms (see find_cave)."""
    game = session.require_game()
    client = session.require_client()
    target = writer_target(writer)
    read = lambda a, n: client.read(game.pid, a, n)  # noqa: E731
    span = hook_span(read, writer.address)
    orig = b"".join(read(i.ip, i.len) for i in span)
    cave = find_cave(session, CAVE_ROOM, extra_reserved=extra_reserved)
    table = find_table(session)
    code = sampler_code(span, target, table, cave)
    client.write(game.pid, cave, code)
    client.write(game.pid, writer.address, jmp_patch(writer.address, cave, len(orig)))
    log.info("action=player_only_collect hook=%#x cave=%#x table=%#x", writer.address, cave, table)
    try:
        waited = 0.0
        while waited < seconds:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("collecting cancelled")
            sleep(0.25)
            waited += 0.25
    finally:
        client.write(game.pid, writer.address, orig)  # unhook
        sleep(0.5)  # let threads finish their last pass through the cave
        raw = read(table, TABLE_SIZE)
        client.write(game.pid, table, bytes(TABLE_SIZE))
        # The cave's code stays: a thread preempted inside it could still be running
        # it. Nothing jumps there any more, and the next cave goes below it.
    objs = sorted({v for (v,) in struct.iter_unpack("<Q", raw) if v})
    log.info("action=player_only_collect objects=%d", len(objs))
    return objs


@dataclass(frozen=True)
class FieldGuess:
    offset: int
    player_value: int
    others: tuple[int, ...]

    def label(self) -> str:
        shown = ", ".join(f"{v:#x}" for v in sorted(set(self.others))[:3])
        return f"+{self.offset:#x}: you {self.player_value:#x}, others {shown}"


def guess_fields(player: bytes, others: list[bytes], limit: int = 10) -> list[FieldGuess]:
    """Dword fields where the player's value differs from every other object.
    Best first: fewest distinct values among the others (a type/ID field), then the
    lowest offset."""
    out = []
    for off in range(0, len(player) - 3, 4):
        (pv,) = struct.unpack_from("<I", player, off)
        ov = [struct.unpack_from("<I", o, off)[0] for o in others if len(o) >= off + 4]
        if not ov or pv == 0 or pv in ov:
            continue
        out.append(FieldGuess(off, pv, tuple(ov)))
    out.sort(key=lambda g: (len(set(g.others)), g.offset))
    return out[:limit]


def compare_objects(session: Session, player_obj: int, objects: list[int]) -> list[FieldGuess]:
    game = session.require_game()
    client = session.require_client()
    player = client.read(game.pid, player_obj, COMPARE_BYTES)
    others = []
    for o in objects:
        if o == player_obj:
            continue
        try:
            others.append(client.read(game.pid, o, COMPARE_BYTES))
        except Exception:  # freed since: skip it
            continue
    if not others:
        raise LockFailed(
            "Only your character was seen. Collect again and hit an enemy (or get hit by "
            "several) during the 20 seconds."
        )
    return guess_fields(player, others)


# -- the cheat ---------------------------------------------------------------------
def make_player_only_cheat(
    session: Session,
    writer: Writer,
    field: FieldGuess,
    value: bytes,
    name: str,
    vtype: ValueType | None = None,
) -> Cheat:
    """Two CODE_PATCH locations: the cave, and the jump to it. Saveable. With ``vtype``
    the kept value stays editable in My cheats and in the file."""
    game = session.require_game()
    client = session.require_client()
    read = lambda a, n: client.read(game.pid, a, n)  # noqa: E731
    target = writer_target(writer)
    span = hook_span(read, writer.address)
    orig = b"".join(read(i.ip, i.len) for i in span)
    cave = find_cave(session, CAVE_ROOM, skip=CAVE_ROOM)  # not the sampler's spot
    code = player_only_code(span, target, field.offset, field.player_value, value, cave)
    cave_orig = read(cave, len(code))
    for addr in (cave, writer.address):
        if not game.module_base <= addr < game.module_end:
            raise LockFailed("the patch would land outside the game's own code")
    base = game.module_base
    value_at = None
    if vtype is not None and vtype.size == len(value):
        value_at = value_positions(
            lambda v: [player_only_code(span, target, field.offset, field.player_value, v, cave)],
            len(value),
        )
        if value_at is None:
            log.warning("action=value_positions found=0 name=%s: value won't be editable", name)
    return Cheat(
        name=name,
        locations=[
            Location(LocationKind.CODE_PATCH, cave - base, code, cave_orig),
            Location(
                LocationKind.CODE_PATCH,
                writer.address - base,
                jmp_patch(writer.address, cave, len(orig)),
                orig,
            ),
        ],
        template="lock",
        hint="Keeps your value; everyone else is unchanged. Turn on before playing.",
        value_type=vtype if value_at is not None else None,
        value_at=value_at,
    )


__all__ = [
    "BUILD_CAVE_SKIP",
    "EFFECT_TYPES",
    "SLOT_SIZE",
    "UNSUPPORTED_TYPE",
    "Effect",
    "FieldGuess",
    "Filter",
    "capture_code",
    "check_effect_writer",
    "collect_objects",
    "compare_objects",
    "effect_code",
    "find_cave",
    "find_slot",
    "guess_fields",
    "make_player_only_cheat",
    "player_only_code",
    "sampler_code",
    "writer_target",
]
