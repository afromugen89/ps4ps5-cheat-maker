"""What does this instruction access? (spec PR-3, AC-3.4/3.5, architecture-pro step 18).

The game never stops: the instruction is hooked with ``cave.sampler_code``, which
stores its base register into a 64-slot table in spare module data on every pass and
then runs the original code. Every ``POLL_S`` the table is read and zeroed, so each
address seen in a poll counts once; an address's count is how many polls saw it (a
measure of how often, not an exact number of accesses). At the end the hook comes
out, and after ``DISABLE_WAIT`` (threads finish their last pass, as when a Pro cheat
is turned off) the table and the sampler cave are zeroed again.

Rows are the accessed address (base + displacement) and the object (the base
register). When there is no spare room for the cave or the table, ``NoRoomForSampler``
is raised; the UI then offers an execute watch on the instruction instead, and
``accesses_from_hits`` turns its hits (registers before the instruction runs) into
the same rows.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

import iced_x86 as ix

from cheatmaker.console.client import WatchHit
from cheatmaker.core import cave as cv
from cheatmaker.core.cheats import Cheat
from cheatmaker.core.locker import MAX_INSN, touches_memory
from cheatmaker.core.pro import sites
from cheatmaker.core.pro.asm import insn_text
from cheatmaker.core.pro.compile import DISABLE_WAIT, ProWriter
from cheatmaker.core.session import Session
from cheatmaker.errors import LockFailed

log = logging.getLogger(__name__)

SAMPLE_SECONDS = 20
POLL_S = 0.25
NO_ROOM = "No room for the sampler: the game's code or data has no spare space for it ({reason})."
NOT_MEMORY = "This instruction doesn't read or write memory, so there is nothing to sample."
NO_BASE = (
    "This instruction's address isn't one register plus a number ({text}), so the "
    "sampler can't record it."
)


class NoRoomForSampler(LockFailed):
    """No spare code or data for the sampler (AC-3.5)."""


@dataclass(frozen=True)
class Operand:
    """The memory operand ``[base + disp]`` of the sampled instruction."""

    address: int  # the instruction
    text: str
    base: int  # an iced_x86.Register
    disp: int
    length: int  # the instruction's length

    @property
    def base_name(self) -> str:
        return cv.Target(self.base, self.disp).base_name


@dataclass(frozen=True)
class AccessRow:
    address: int  # base + disp: what the instruction touched
    object: int  # the base register's value
    count: int


@dataclass(frozen=True)
class SampleResult:
    operand: Operand
    rows: list[AccessRow]
    polls: int
    cancelled: bool
    site_changed: bool = False
    """The site held other bytes at the end (a cheat wrote there): left alone (m7)."""


def operand_at(code: bytes, address: int) -> Operand:
    """The instruction at ``address`` and its ``[base + disp]`` operand. LockFailed
    when it doesn't touch memory through one general register (no index, not RIP or
    RSP: the sampler stores that register)."""
    insn = ix.Decoder(64, code, ip=address).decode()
    if insn.code == ix.Code.INVALID:
        raise LockFailed("couldn't read the game code at that address")
    text = insn_text(insn)
    if not touches_memory(insn):
        raise LockFailed(NOT_MEMORY)
    base, index = insn.memory_base, insn.memory_index
    if base in (ix.Register.NONE, ix.Register.RIP, ix.Register.RSP) or index != ix.Register.NONE:
        raise LockFailed(NO_BASE.format(text=text))
    if ix.RegisterInfo(base).size != 8:  # [eax+...]: a 32-bit address
        raise LockFailed(NO_BASE.format(text=text))
    disp = insn.memory_displacement
    if disp >= 1 << 63:
        disp -= 1 << 64
    return Operand(address, text, base, disp, insn.len)


def rows_from(counts: Counter[int], operand: Operand) -> list[AccessRow]:
    rows = [AccessRow((obj + operand.disp) & (1 << 64) - 1, obj, n) for obj, n in counts.items()]
    return sorted(rows, key=lambda r: (-r.count, r.address))


def accesses_from_hits(hits: Iterable[WatchHit], operand: Operand) -> list[AccessRow]:
    """The watch offered instead (AC-3.5): an execute watch's hits carry the registers
    before the instruction runs, so base + disp is what it is about to touch."""
    name = operand.base_name
    counts: Counter[int] = Counter(h.regs[name] for h in hits if name in h.regs)
    return rows_from(counts, operand)


def _table_values(raw: bytes) -> set[int]:
    return {v for (v,) in struct.iter_unpack("<Q", raw) if v}


def refuse_patched(site: int, length: int, enabled: Iterable[Cheat], module_base: int) -> None:
    """Overlap naming an enabled cheat that patches the instruction: the hook would
    replace the cheat's bytes (and turning it off later would remove the hook)."""
    start, end = site - module_base, site - module_base + length
    for other in enabled:
        if any(s < end and start < e for s, e in sites.cheat_ranges(other, module_base)):
            raise sites.Overlap(f"“{other.name}” patches this instruction. Turn it off first.")


def _unhook(session: Session, writer: ProWriter, site: int, hook: bytes, orig: bytes) -> bool:
    """Put the game's bytes back at ``site`` (AC-3.4). True when no jump leads into the
    sampler cave any more. The site is read first: when it holds neither the hook nor
    the game's own bytes, something else wrote there meanwhile (a cheat turned on), so
    it isn't overwritten and the cave stays (m7)."""
    game = session.require_game()
    now = session.require_client().read(game.pid, site, len(hook))
    if now == orig:  # the hook never landed
        return True
    if now != hook:
        log.warning(
            "action=pro_sample site=%#x result=site_changed found=%s; left alone",
            site,
            now.hex(" "),
        )
        return False
    writer.write_checked(site, orig, "sampler:unhook")
    return True


def sample(
    session: Session,
    site: int,
    writer: ProWriter,
    *,
    seconds: float = SAMPLE_SECONDS,
    cancel: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
    reserved: Sequence[sites.Range] = (),
    enabled: Iterable[Cheat] = (),
    on_update: Callable[[list[AccessRow], int], None] | None = None,
) -> SampleResult:
    """Sample what the instruction at ``site`` accesses for ``seconds`` (AC-3.4).
    ``reserved``: module-offset rooms the sampler cave must keep out of (Pro caves,
    code lines of open cheats). NoRoomForSampler when there is no spare space."""
    game = session.require_game()
    client = session.require_client()
    read = lambda a, n: client.read(game.pid, a, n)  # noqa: E731
    enabled = list(enabled)
    refuse_patched(site, 1, enabled, game.module_base)  # a cheat's bytes aren't the game's
    operand = operand_at(read(site, MAX_INSN), site)
    span = cv.hook_span(read, site)
    orig = b"".join(read(i.ip, i.len) for i in span)
    refuse_patched(site, len(orig), enabled, game.module_base)  # the whole jump
    try:
        cave = cv.find_cave(session, cv.CAVE_ROOM, extra_reserved=reserved)
        table = cv.find_table(session)
    except LockFailed as exc:
        log.info("action=pro_sample site=%#x result=no_room reason=%s", site, exc)
        raise NoRoomForSampler(NO_ROOM.format(reason=exc)) from exc
    code = cv.sampler_code(span, cv.Target(operand.base, operand.disp), table, cave)
    counts: Counter[int] = Counter()
    polls, cancelled = 0, False
    hook = cv.jmp_patch(site, cave, len(orig))
    writer.write_checked(cave, code, "sampler:cave")
    try:
        writer.write_checked(site, hook, "sampler:hook")  # inside: a bad read-back unhooks
        log.info("action=pro_sample site=%#x cave=%#x table=%#x", site, cave, table)
        waited = 0.0
        while waited < seconds:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            sleep(POLL_S)
            waited += POLL_S
            counts.update(_table_values(read(table, cv.TABLE_SIZE)))
            client.write(game.pid, table, bytes(cv.TABLE_SIZE))
            polls += 1
            if on_update is not None:
                on_update(rows_from(counts, operand), polls)
    finally:
        unhooked = _unhook(session, writer, site, hook, orig)
        if unhooked:
            sleep(DISABLE_WAIT)  # threads finish their last pass through the cave
        counts.update(_table_values(read(table, cv.TABLE_SIZE)))
        client.write(game.pid, table, bytes(cv.TABLE_SIZE))
        if unhooked:  # only once nothing can jump into it any more
            writer.write(cave, bytes(len(code)), "sampler:cave_clear")
    rows = rows_from(counts, operand)
    log.info("action=pro_sample site=%#x polls=%d addresses=%d", site, polls, len(rows))
    return SampleResult(operand, rows, polls, cancelled, site_changed=not unhooked)
