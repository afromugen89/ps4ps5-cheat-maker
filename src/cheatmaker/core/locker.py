""" "Lock it" (spec D8): find the instruction that changes a value and switch it off.

Flow: watch the found address for writes -> for each reported RIP, find the
instruction that ends at that RIP and writes memory -> replace it with NOPs ->
the result is a CODE_PATCH location, which CheatRunner can load after a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import iced_x86 as ix

from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.session import Session
from cheatmaker.errors import LockFailed, ReadFailed

log = logging.getLogger(__name__)

MAX_INSN = 15  # longest x86 instruction
CONTEXT = 64  # bytes read before a watchpoint RIP to find where instructions start
NOP = 0x90
_formatter = ix.Formatter(ix.FormatterSyntax.INTEL)
_info = ix.InstructionInfoFactory()


@dataclass(frozen=True)
class Writer:
    address: int  # where the instruction starts
    code: bytes
    text: str  # "mov [rax],ecx" (shown only under Advanced)
    hits: int  # how many times it wrote during the watch
    unsafe: str | None = None  # why NOPs here could hang the game; None = safe

    @property
    def length(self) -> int:
        return len(self.code)


def writes_memory(insn: ix.Instruction) -> bool:
    return any(
        m.access
        in (
            ix.OpAccess.WRITE,
            ix.OpAccess.READ_WRITE,
            ix.OpAccess.COND_WRITE,
            ix.OpAccess.READ_COND_WRITE,
        )
        for m in _info.info(insn).used_memory()
    )


def touches_memory(insn: ix.Instruction) -> bool:
    """Has an explicit memory operand that it reads or writes (``lea`` and ``nop [..]``
    only name an address; push/pop/call touch the stack implicitly and don't count)."""
    if insn.mnemonic in (ix.Mnemonic.LEA, ix.Mnemonic.NOP):
        return False
    return any(insn.op_kind(i) == ix.OpKind.MEMORY for i in range(insn.op_count))


_REG_WRITE = (
    ix.OpAccess.WRITE,
    ix.OpAccess.READ_WRITE,
    ix.OpAccess.COND_WRITE,
    ix.OpAccess.READ_COND_WRITE,
)
_ATOMIC = {
    ix.Mnemonic.XCHG,
    ix.Mnemonic.CMPXCHG,
    ix.Mnemonic.CMPXCHG8B,
    ix.Mnemonic.CMPXCHG16B,
    ix.Mnemonic.XADD,
}


def nop_unsafe(insn: ix.Instruction) -> str | None:
    """Why switching ``insn`` off with NOPs could hang or break the game, or None.

    Safe means the instruction's only effect is the memory write. Atomic updates
    (``lock``, ``cmpxchg``, ``xchg``, ``xadd``) sit in retry loops: without them the
    loop never sees success and the game freezes. Instructions that also change a
    register (``rep stos``/``movs`` move rdi/rsi/rcx, ``push``/``call`` move rsp,
    ``cmpxchg`` sets rax) leave the code after them working on wrong values."""
    if insn.has_lock_prefix or insn.mnemonic in _ATOMIC:
        return "it's an atomic update, which the game retries until it works"
    if insn.has_rep_prefix or insn.has_repe_prefix or insn.has_repne_prefix:
        return "it's a block copy that the code after it depends on"
    if insn.flow_control != ix.FlowControl.NEXT:
        return "it also jumps or calls"
    if any(r.access in _REG_WRITE for r in _info.info(insn).used_registers()):
        return "it also changes a register the game uses next"
    return None


def _chain_end(code: bytes, start: int, ip0: int) -> int | None:
    """Decode from ``start`` straight through; the offset where the last instruction
    that ends exactly at ``len(code)`` begins, or None if the chain doesn't land there."""
    dec = ix.Decoder(64, code[start:], ip=ip0 + start)
    pos = start
    last = None
    for insn in dec:
        if insn.code == ix.Code.INVALID:
            return None
        last = pos
        pos += insn.len
    return last if pos == len(code) else None


def instruction_before(
    code_before: bytes,
    rip: int,
    want: Callable[[ix.Instruction], bool] = writes_memory,
) -> tuple[int, bytes, str] | None:
    """Find the instruction that ends exactly at ``rip`` and ``want`` accepts
    (default: one that writes memory).

    ``code_before`` holds the bytes just before ``rip`` (best: ``CONTEXT`` of them).
    Several instructions can end at ``rip``: the real one, a shorter tail of it, and
    a longer one that starts inside the instruction before it. Picking that longer
    one would NOP part of the previous instruction and corrupt the game's code. So
    decode straight through from every start offset (x86 decoding re-synchronises
    after a few instructions) and let each chain vote for where the last
    instruction starts, weighted by how much code it decoded. Ties go to the
    longest, since shorter tails of a long instruction can also look valid.
    """
    n = len(code_before)
    ip0 = rip - n
    valid: dict[int, ix.Instruction] = {}
    for back in range(1, min(MAX_INSN, n) + 1):
        insn = ix.Decoder(64, code_before[-back:], ip=rip - back).decode()
        if insn.code != ix.Code.INVALID and insn.len == back and want(insn):
            valid[n - back] = insn
    if not valid:
        return None
    votes = dict.fromkeys(valid, 0)
    for start in range(n):
        end = _chain_end(code_before, start, ip0)
        if end in votes:
            votes[end] += n - start
    best = max(valid, key=lambda off: (votes[off], -off))
    return ip0 + best, bytes(code_before[best:]), _formatter.format(valid[best])


def decode_one(code: bytes, ip: int) -> ix.Instruction:
    return ix.Decoder(64, code, ip=ip).decode()


def find_writers(
    session: Session, address: int, size: int, timeout_s: float = 30.0
) -> list[Writer]:
    """Watch ``address`` while the player makes the value change. Most hits first."""
    client = session.require_client()
    game = session.require_game()
    rips = client.watch_writes(game.pid, address, size, timeout_s)
    log.info("action=lock_watch address=%#x rips=%d", address, len(rips))
    if not rips:
        raise LockFailed("nothing changed the value while we were watching")
    counts: dict[int, int] = {}
    for r in rips:
        counts[r] = counts.get(r, 0) + 1
    writers: list[Writer] = []
    for rip, hits in sorted(counts.items(), key=lambda kv: -kv[1]):
        try:
            before = client.read(game.pid, rip - CONTEXT, CONTEXT)
        except ReadFailed:
            try:
                before = client.read(game.pid, rip - MAX_INSN, MAX_INSN)
            except ReadFailed:
                log.warning("action=lock_watch can't read code before rip=%#x", rip)
                continue
        found = instruction_before(before, rip)
        if found is None:
            log.warning("action=lock_watch no writing instruction ends at rip=%#x", rip)
            continue
        start, code, text = found
        unsafe = nop_unsafe(decode_one(code, start))
        log.info(
            "action=lock_watch writer=%#x code=%s text=%r hits=%d unsafe=%s",
            start,
            code.hex(),
            text,
            hits,
            unsafe,
        )
        writers.append(Writer(start, code, text, hits, unsafe))
    if not writers:
        raise LockFailed("couldn't work out which instruction changes the value")
    return writers


def make_lock_cheat(session: Session, writer: Writer, name: str) -> Cheat:
    """A CODE_PATCH cheat that NOPs ``writer``. Off = the original bytes."""
    game = session.require_game()
    if writer.unsafe:
        raise LockFailed(
            f"switching this one off could freeze the game: {writer.unsafe}. "
            "Try another option, or Player only."
        )
    if not game.module_base <= writer.address < game.module_end:
        raise LockFailed("the instruction is outside the game's own code, so it can't be saved")
    loc = Location(
        LocationKind.CODE_PATCH,
        writer.address - game.module_base,
        bytes([NOP]) * writer.length,
        writer.code,
    )
    return Cheat(
        name=name, locations=[loc], template="lock", hint="Stops the game from changing this value."
    )


def verify_original(session: Session, cheat: Cheat) -> None:
    """Before patching, check the code still holds the bytes we expect."""
    client = session.require_client()
    game = session.require_game()
    for loc in cheat.locations:
        now = client.read(game.pid, loc.address(game.module_base), len(loc.off))
        if now not in (loc.off, loc.on):
            raise LockFailed("the game's code isn't what we expected; refusing to patch")


def patch_landed(session: Session, cheat: Cheat) -> bool:
    """After turning a lock on: read the code back. Some writes to code pages are
    acknowledged without changing anything."""
    client = session.require_client()
    game = session.require_game()
    ok = all(
        client.read(game.pid, loc.address(game.module_base), len(loc.on)) == loc.on
        for loc in cheat.locations
    )
    log.info("action=lock_test name=%r landed=%s", cheat.name, ok)
    return ok


__all__ = [
    "Writer",
    "find_writers",
    "instruction_before",
    "make_lock_cheat",
    "nop_unsafe",
    "patch_landed",
    "touches_memory",
    "verify_original",
    "writes_memory",
]
