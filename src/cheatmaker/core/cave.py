"""Lock it, "player only": a code cave that skips the damage write just for the player.

Games often write everyone's health with one instruction, e.g. at Returnal's
``vmovss [r12+0x2AC], xmm1``. Switching it off makes enemies immortal too. Instead:

1. **Collect.** Hook the instruction with a tiny routine that records the object
   register (r12 above) in a table, while the player fights for ~20 s. That gives the
   player's object and the enemies' objects.
2. **Compare.** Read the start of every object and find a field where the player's
   value differs from all the others (Returnal: +0x08 == 0x40048).
3. **Cave.** In spare zero bytes at the end of the game's code, write::

       cmp dword [r12+field], player_value
       jne normal
       mov [r12+0x2AC], <the value you want>   ; player: keep it
       <instructions after the writer that the hook covered>
       jmp back
     normal:
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
from collections.abc import Callable
from dataclasses import dataclass

import iced_x86 as ix

from cheatmaker.console.client import MemMap
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.locker import Writer
from cheatmaker.core.session import Session
from cheatmaker.errors import LockFailed, ScanCancelled

log = logging.getLogger(__name__)

JMP_LEN = 5
TABLE_SLOTS = 64
TABLE_SIZE = TABLE_SLOTS * 8
COMPARE_BYTES = 0x200  # how much of each object to compare
CAVE_ROOM = 256  # bytes reserved for one cave
SEARCH_TAIL = 0x10000  # look for spare bytes in the last 64 KiB of the code
RED_ZONE = 128  # SysV leaf functions may use 128 bytes below rsp

Read = Callable[[int, int], bytes]


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
    if disp >= 1 << 63:
        disp -= 1 << 64
    return Target(base, disp)


def hook_span(read: Read, site: int) -> list[ix.Instruction]:
    """Whole instructions starting at ``site`` covering at least the 5-byte jmp."""
    insns, total = [], 0
    for insn in decode_at(read(site, 32), site):
        if insn.code == ix.Code.INVALID:
            raise LockFailed("couldn't read the game code at the hook")
        insns.append(insn)
        total += insn.len
        if total >= JMP_LEN:
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


def player_only_code(
    span: list[ix.Instruction],
    target: Target,
    field: int,
    player_value: int,
    value: bytes,
    cave: int,
) -> bytes:
    """The cave: the player gets ``value`` instead of the game's write."""
    if len(value) not in (1, 2, 4, 8):
        raise LockFailed("can't write a value of that size")
    back = span[-1].next_ip
    C, Ins = ix.Code, ix.Instruction
    normal_ip = 0xFFFF_0000  # placeholder label, resolved by the encoder
    stores: list[ix.Instruction] = []
    if len(value) == 8:
        lo, hi = struct.unpack("<II", value)
        stores = [
            Ins.create_mem_u32(C.MOV_RM32_IMM32, _mem(target.base, target.disp), lo),
            Ins.create_mem_u32(C.MOV_RM32_IMM32, _mem(target.base, target.disp + 4), hi),
        ]
    elif len(value) == 4:
        stores = [
            Ins.create_mem_u32(
                C.MOV_RM32_IMM32, _mem(target.base, target.disp), struct.unpack("<I", value)[0]
            )
        ]
    elif len(value) == 2:
        stores = [
            Ins.create_mem_u32(
                C.MOV_RM16_IMM16, _mem(target.base, target.disp), struct.unpack("<H", value)[0]
            )
        ]
    else:
        stores = [Ins.create_mem_u32(C.MOV_RM8_IMM8, _mem(target.base, target.disp), value[0])]
    normal = [i.copy() for i in span]
    normal[0] = _label(normal[0], normal_ip)
    body = [
        Ins.create_mem_u32(C.CMP_RM32_IMM32, _mem(target.base, field), player_value & 0xFFFFFFFF),
        Ins.create_branch(C.JNE_REL32_64, normal_ip),
        *stores,
        *[  # what the hook covered after the writer (fresh labels: they're copies)
            _label(i.copy(), 0xFFFE_0000 + k) for k, i in enumerate(span[1:])
        ],
        Ins.create_branch(C.JMP_REL32_64, back),
        *normal,
        Ins.create_branch(C.JMP_REL32_64, back),
    ]
    return _encode(body, cave)


# -- where things go ------------------------------------------------------------
def _module_maps(session: Session) -> list[MemMap]:
    game = session.require_game()
    maps = session.require_client().maps(game.pid)
    return [m for m in maps if game.module_base <= m.start < game.module_end]


def find_cave(session: Session, size: int, skip: int = 0) -> int:
    """A run of zero bytes near the end of the game's code (16-byte aligned).
    ``skip`` reserves that many bytes at the very end (for another cave)."""
    game = session.require_game()
    client = session.require_client()
    code = [m for m in _module_maps(session) if m.executable]
    if not code:
        raise LockFailed("couldn't find the game's code")
    last = max(code, key=lambda m: m.end)
    start = max(last.start, last.end - SEARCH_TAIL)
    data = client.read(game.pid, start, last.end - start)
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
) -> list[int]:
    """Hook ``writer`` for ``seconds`` and return the object addresses it wrote to."""
    game = session.require_game()
    client = session.require_client()
    target = writer_target(writer)
    read = lambda a, n: client.read(game.pid, a, n)  # noqa: E731
    span = hook_span(read, writer.address)
    orig = b"".join(read(i.ip, i.len) for i in span)
    cave = find_cave(session, CAVE_ROOM)
    table = find_table(session)
    code = sampler_code(span, target, table, cave)
    cave_orig = read(cave, len(code))
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
        client.write(game.pid, writer.address, orig)  # unhook first
        sleep(0.5)  # let any thread inside the cave leave it
        raw = read(table, TABLE_SIZE)
        client.write(game.pid, table, bytes(TABLE_SIZE))
        client.write(game.pid, cave, cave_orig)
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
    session: Session, writer: Writer, field: FieldGuess, value: bytes, name: str
) -> Cheat:
    """Two CODE_PATCH locations: the cave, and the jump to it. Saveable."""
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
    )


__all__ = [
    "FieldGuess",
    "collect_objects",
    "compare_objects",
    "guess_fields",
    "make_player_only_cheat",
    "player_only_code",
    "sampler_code",
    "writer_target",
]
