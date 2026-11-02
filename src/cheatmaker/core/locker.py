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
NOP = 0x90
_formatter = ix.Formatter(ix.FormatterSyntax.INTEL)
_info = ix.InstructionInfoFactory()


@dataclass(frozen=True)
class Writer:
    address: int  # where the instruction starts
    code: bytes
    text: str  # "mov [rax],ecx" (shown only under Advanced)
    hits: int  # how many times it wrote during the watch

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


def instruction_before(
    code_before: bytes,
    rip: int,
    want: Callable[[ix.Instruction], bool] = writes_memory,
) -> tuple[int, bytes, str] | None:
    """Find the instruction that ends exactly at ``rip`` and ``want`` accepts
    (default: one that writes memory).

    ``code_before`` holds the bytes just before ``rip``. Tries every start offset
    and prefers the longest valid decode, since shorter tails of a long
    instruction can also look valid.
    """
    best = None
    for back in range(1, min(MAX_INSN, len(code_before)) + 1):
        chunk = code_before[-back:]
        insn = ix.Decoder(64, chunk, ip=rip - back).decode()
        if insn.code == ix.Code.INVALID or insn.len != back or not want(insn):
            continue
        best = (rip - back, bytes(chunk), _formatter.format(insn))
    return best


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
            before = client.read(game.pid, rip - MAX_INSN, MAX_INSN)
        except ReadFailed:
            log.warning("action=lock_watch can't read code before rip=%#x", rip)
            continue
        found = instruction_before(before, rip)
        if found is None:
            log.warning("action=lock_watch no writing instruction ends at rip=%#x", rip)
            continue
        start, code, text = found
        writers.append(Writer(start, code, text, hits))
    if not writers:
        raise LockFailed("couldn't work out which instruction changes the value")
    return writers


def make_lock_cheat(session: Session, writer: Writer, name: str) -> Cheat:
    """A CODE_PATCH cheat that NOPs ``writer``. Off = the original bytes."""
    game = session.require_game()
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
    "patch_landed",
    "touches_memory",
    "verify_original",
    "writes_memory",
]
