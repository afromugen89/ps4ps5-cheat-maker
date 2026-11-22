"""Find what writes / accesses an address (spec PR-3, architecture-pro step 17).

``WatchSession`` runs one debugger session (``console.client.Debugger``) with up to four
hardware watches, one per slot, until Stop, ``seconds`` or ``max_hits``:

- Each stop is resumed at once (the game waits only one round trip), then grouped
  by instruction: one ``InstructionRow`` per (instruction, slot), with its hit count
  and the registers of its first and last hit.
- Data watchpoints trap *after* the instruction, so the row names the instruction
  that ends at the reported RIP (AC-3.1, ``locker.instruction_before``); an execute
  watch names the instruction at RIP.
- DR6 says which slot fired. When it names none that is set (UNVERIFIED on NG until
  C-P3d) and only one watch runs, the hit is that watch's.
- Every end path clears the slots and detaches (``finally``); the rows stay
  (AC-3.2). A lost session or a closed game ends it with ``LOST`` and a message,
  never writing anything (AC-1.2).

``REGISTERS_VERIFIED`` is False until spike S-P1 proves NG's registers other than RIP;
the Watch tab then marks the register columns "unverified" (AC-3.3).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

import iced_x86 as ix

from cheatmaker.console.client import Debugger, StopEvent, WatchHit, WatchKind
from cheatmaker.core.cave import Target
from cheatmaker.core.cheats import Cheat, LocationKind
from cheatmaker.core.locker import CONTEXT, MAX_INSN, Read, instruction_before, touches_memory
from cheatmaker.core.pro.asm import insn_text
from cheatmaker.errors import (
    CheatMakerError,
    ConnectionLost,
    ConsoleBusy,
    DebuggerLost,
    ReadFailed,
)

log = logging.getLogger(__name__)

MAX_WATCH_SLOTS = 4
"""Watches at once: x86 has four debug slots (DR0-DR3). That NG gives us all four, each
reporting its own hits through DR6, is console spike S-P2 (C-P3d). If it fails, set
this to 1 and the Watch tab takes one address at a time."""
REPEAT_HITS = 16
"""The same thread at the same RIP with the same registers this many times in a row:
the breakpoint fires again and again without the game moving on (the tracer's
``EXEC_HITS`` rule; an execute breakpoint without RF, console test C20d)."""
WATCH_SECONDS = 60
WATCH_HITS = 500
REGISTERS_VERIFIED = False
"""Flip to True once console spike S-P1 shows every general register is right."""
POLL_S = 0.25  # how often Stop and the time limit are checked while nothing happens
UPDATE_S = 0.2  # live counts reach the UI at most this often
UNKNOWN_TEXT = "?"
WATCH_KINDS: tuple[WatchKind, ...] = ("write", "access")
WATCH_SIZES = (1, 2, 4, 8)
REG_ORDER = (
    "rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp",
    "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15", "rip", "rflags",
)  # fmt: skip

STOPPED, TIME, HITS, LOST, FAILED, REPEATS = (
    "stopped",
    "time",
    "hits",
    "lost",
    "failed",
    "repeats",
)
MISALIGNED = (
    "The PS5 can only watch a value at an address divisible by its size: {addr:#x} isn't "
    "a multiple of {size}. Watch {size} byte(s) at {down:#x}, or a smaller size."
)
REPEATING = (
    "The watch fired {n} times in a row at the same place without the game moving on, "
    "so it was stopped."
)


@dataclass(frozen=True)
class WatchTarget:
    addr: int
    size: int
    kind: WatchKind

    def __post_init__(self) -> None:
        if self.size not in WATCH_SIZES:
            raise ValueError(f"A watch covers 1, 2, 4 or 8 bytes, not {self.size}.")
        if self.kind not in (*WATCH_KINDS, "execute"):
            raise ValueError(f"Unknown watch kind {self.kind!r}.")
        if self.kind == "execute" and self.size != 1:
            raise ValueError("An instruction watch covers 1 byte.")
        if self.addr % self.size:  # x86 debug registers ignore the low address bits
            down = self.addr - self.addr % self.size
            raise ValueError(MISALIGNED.format(addr=self.addr, size=self.size, down=down))

    def covers(self, start: int, size: int) -> bool:
        return start < self.addr + self.size and self.addr < start + size


@dataclass
class InstructionRow:
    address: int  # where the instruction starts (the RIP when it couldn't be found)
    text: str
    slot: int | None
    count: int
    first: WatchHit
    last: WatchHit


@dataclass(frozen=True)
class WatchResult:
    rows: list[InstructionRow]
    hits: int
    ended: str  # STOPPED, TIME, HITS, LOST, FAILED or REPEATS
    seconds: float
    message: str = ""  # why it was LOST, FAILED or REPEATS


@dataclass(frozen=True)
class _Found:
    address: int  # where the instruction starts (the RIP when it couldn't be found)
    text: str
    insn: ix.Instruction | None = None


@dataclass
class _Lookup:
    """Instruction start and text per reported RIP (each read once per session)."""

    read: Read
    found: dict[tuple[int, str], _Found] = field(default_factory=dict)

    def __call__(self, rip: int, kind: WatchKind) -> _Found:
        key = (rip, "execute" if kind == "execute" else "data")
        if key not in self.found:
            try:
                self.found[key] = self._find(rip, kind)
            except ConsoleBusy:  # the main connection is busy: "?" now, ask next time
                log.info("action=pro_watch_decode rip=%#x result=busy", rip)
                return _Found(rip, UNKNOWN_TEXT)
        return self.found[key]

    def _find(self, rip: int, kind: WatchKind) -> _Found:
        try:
            if kind == "execute":
                insn = ix.Decoder(64, self.read(rip, MAX_INSN), ip=rip).decode()
                if insn.code == ix.Code.INVALID:
                    return _Found(rip, UNKNOWN_TEXT)
                return _Found(rip, insn_text(insn), insn)
            before = instruction_before(self.read(rip - CONTEXT, CONTEXT), rip, touches_memory)
        except ConsoleBusy:
            raise
        except ReadFailed as exc:
            log.info("action=pro_watch_decode rip=%#x reason=%s", rip, exc)
            return _Found(rip, UNKNOWN_TEXT)
        if before is None:
            return _Found(rip, UNKNOWN_TEXT)
        start, raw, _text = before
        insn = ix.Decoder(64, raw, ip=start).decode()
        return _Found(start, insn_text(insn), insn)


def _reg_value(regs: dict[str, int], reg: int) -> int | None:
    if reg == ix.Register.NONE:
        return 0
    return regs.get(Target(reg, 0).base_name)


def operand_touches(insn: ix.Instruction, regs: dict[str, int], target: WatchTarget) -> bool:
    """Does ``insn``'s memory operand, worked out from ``regs``, overlap ``target``?

    Best effort, used only to split a stop whose DR6 names several watches (m3):

    - ``regs`` are the registers *after* the instruction ran (a data watch traps after
      it). An instruction that changes its own base or index register (``mov rax,
      [rax+8]``, ``pop``, string ops) gives the wrong address.
    - The fs/gs segment bases aren't in the stop packet, so an ``fs:``/``gs:``
      operand never matches.

    A wrong or missing match only means the hit's watch shows as "?" (no single
    match), never the wrong watch for a correct operand."""
    if not any(insn.op_kind(i) == ix.OpKind.MEMORY for i in range(insn.op_count)):
        return False
    if insn.memory_segment in (ix.Register.FS, ix.Register.GS):
        return False
    base = _reg_value(regs, insn.memory_base)
    index = _reg_value(regs, insn.memory_index)
    if base is None or index is None or insn.memory_base == ix.Register.RIP:
        return False
    ea = (base + index * insn.memory_index_scale + insn.memory_displacement) & (1 << 64) - 1
    size = max(1, ix.MemorySizeInfo(insn.memory_size).size)
    return target.covers(ea, size)


class WatchSession:
    def __init__(
        self,
        dbg: Debugger,
        read: Read,
        targets: Sequence[WatchTarget],
        *,
        seconds: float = WATCH_SECONDS,
        max_hits: int = WATCH_HITS,
        clock: Callable[[], float] = time.monotonic,
        on_update: Callable[[list[InstructionRow], int, float], None] | None = None,
        on_hit: Callable[[StopEvent], None] | None = None,
    ) -> None:
        """``on_update``: live rows (throttled), and once at the end. ``on_hit``: every
        stop (the access sampler's fallback counts addresses from the registers)."""
        if not 1 <= len(targets) <= MAX_WATCH_SLOTS:
            raise ValueError(f"Watch 1 to {MAX_WATCH_SLOTS} addresses at a time.")
        self.dbg = dbg
        self.targets = list(targets)
        self.seconds, self.max_hits = seconds, max_hits
        self.clock = clock
        self.on_update = on_update
        self.on_hit = on_hit
        self._lookup = _Lookup(read)
        self._rows: dict[tuple[int, int | None], InstructionRow] = {}
        self._same: tuple[int, int, tuple] | None = None  # (lwpid, rip, regs) of the last
        self._same_count = 0
        self.hits = 0

    def rows(self) -> list[InstructionRow]:
        """Copies, most hits first (safe to hand to another thread)."""
        out = [replace(r) for r in self._rows.values()]
        return sorted(out, key=lambda r: (-r.count, r.address))

    def run(self, cancel: threading.Event) -> WatchResult:
        started = self.clock()
        ended, message = STOPPED, ""
        try:
            self.dbg.attach()
            for slot, t in enumerate(self.targets):
                self.dbg.set_watch(slot, t.addr, t.size, t.kind)
            log.info(
                "action=pro_watch_start targets=%s seconds=%s max_hits=%d",
                ",".join(f"{t.kind}:{t.addr:#x}/{t.size}" for t in self.targets),
                self.seconds,
                self.max_hits,
            )
            ended = self._loop(cancel, started)
            if ended == REPEATS:
                message = REPEATING.format(n=REPEAT_HITS)
        except DebuggerLost as exc:
            ended, message = LOST, str(exc)
        except ConnectionLost:  # the main link: the Controller must hear it (m-B)
            raise  # after the detach below
        except CheatMakerError as exc:  # e.g. the firewall: the rows so far still show
            log.warning("action=pro_watch_end reason=%s: %s", type(exc).__name__, exc)
            ended, message = FAILED, str(exc)
        finally:
            self.dbg.detach()  # clears every slot first (AC-3.2)
        took = self.clock() - started
        log.info(
            "action=pro_watch_end ended=%s hits=%d rows=%d seconds=%.1f",
            ended,
            self.hits,
            len(self._rows),
            took,
        )
        rows = self.rows()
        if self.on_update is not None:
            self.on_update(rows, self.hits, took)
        return WatchResult(rows, self.hits, ended, took, message)

    def _loop(self, cancel: threading.Event, started: float) -> str:
        deadline = started + self.seconds
        shown = started
        while True:
            if cancel.is_set():
                return STOPPED
            if self.hits >= self.max_hits:
                return HITS
            left = deadline - self.clock()
            if left <= 0:
                return TIME
            stop = self.dbg.next_stop(min(POLL_S, left))
            if stop is not None:
                self.dbg.resume()  # the game runs on before anything is decoded
                self._add(stop)
                if self.on_hit is not None:
                    self.on_hit(stop)
                if self._repeating(stop):
                    return REPEATS
            now = self.clock()
            if self.on_update is not None and (stop is None or now - shown >= UPDATE_S):
                shown = now
                self.on_update(self.rows(), self.hits, now - started)

    def _repeating(self, stop: StopEvent) -> bool:
        """An execute breakpoint firing again and again with the very same registers
        (NG not stepping past it). Only execute stops count: a data watch traps after
        the instruction, so a game writing the same value from the same place every
        frame is normal there."""
        if self._kind(self._fired(stop)) != "execute":
            self._same, self._same_count = None, 0
            return False
        key = (stop.lwpid, stop.hit.rip, tuple(sorted(stop.hit.regs.items())))
        self._same_count = self._same_count + 1 if key == self._same else 1
        self._same = key
        if self._same_count >= REPEAT_HITS:
            log.warning("action=pro_watch_end reason=repeats rip=%#x", stop.hit.rip)
            return True
        return False

    def _fired(self, stop: StopEvent) -> list[int]:
        """The slots DR6 names (bits 0-3)."""
        return [k for k in range(len(self.targets)) if stop.dr6 >> k & 1]

    def _kind(self, fired: list[int]) -> WatchKind:
        """``execute`` when every watch that may have fired is an execute watch (all of
        them when DR6 names none), else ``access``."""
        kinds = {self.targets[k].kind for k in fired or range(len(self.targets))}
        return "execute" if kinds == {"execute"} else "access"

    def _slot(self, stop: StopEvent) -> tuple[int | None, _Found]:
        """Which watch fired, and the instruction. One DR6 bit names it. Several bits:
        the watch the instruction's operand points at (m3), else none. No bit (DR6
        UNVERIFIED on NG): the only watch, else none."""
        fired = self._fired(stop)
        found = self._lookup(stop.hit.rip, self._kind(fired))
        if len(fired) == 1:
            return fired[0], found
        if not fired:
            return (0 if len(self.targets) == 1 else None), found
        if found.insn is not None:
            hit = [k for k in fired if operand_touches(found.insn, stop.hit.regs, self.targets[k])]
            if len(hit) == 1:
                return hit[0], found
        return None, found

    def _add(self, stop: StopEvent) -> None:
        self.hits += 1
        slot, found = self._slot(stop)
        address, text = found.address, found.text
        row = self._rows.get((address, slot))
        if row is None:
            self._rows[(address, slot)] = InstructionRow(address, text, slot, 1, stop.hit, stop.hit)
            log.info(
                "action=pro_watch_hit slot=%s insn=%#x rip=%#x dr6=%#x text=%s",
                slot,
                address,
                stop.hit.rip,
                stop.dr6,
                text,
            )
            return
        row.count += 1
        row.last = stop.hit


def registers_text(hit: WatchHit) -> str:
    """The general registers of one hit, one ``name=value`` per register."""
    return (
        " ".join(f"{name}={hit.regs[name]:#x}" for name in REG_ORDER if name in hit.regs)
        or UNKNOWN_TEXT
    )


def value_target(cheat: Cheat, module_base: int) -> WatchTarget | None:
    """A write watch on the value a cheat holds ("Make a cave at its writer"): its
    first value line at a known address. None for a code cheat, or a value whose place
    is found again each run (it isn't known until then). ValueError (``MISALIGNED``)
    for a value the PS5 can't watch."""
    for loc in cheat.locations:
        if loc.kind is LocationKind.CODE_PATCH or loc.anchor is not None:
            continue
        if len(loc.on) in WATCH_SIZES:
            return WatchTarget(loc.address(module_base), len(loc.on), "write")
    return None
