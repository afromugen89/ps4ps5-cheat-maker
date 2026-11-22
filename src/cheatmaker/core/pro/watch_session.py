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

from cheatmaker.console.client import DEBUG_SLOTS, Debugger, StopEvent, WatchHit, WatchKind
from cheatmaker.core.cheats import Cheat, LocationKind
from cheatmaker.core.locker import CONTEXT, MAX_INSN, Read, instruction_before, touches_memory
from cheatmaker.core.pro.asm import insn_text
from cheatmaker.errors import ConsoleBusy, DebuggerLost, ReadFailed

log = logging.getLogger(__name__)

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

STOPPED, TIME, HITS, LOST = "stopped", "time", "hits", "lost"


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
    ended: str  # STOPPED, TIME, HITS or LOST
    seconds: float
    message: str = ""  # why it was LOST


@dataclass
class _Lookup:
    """Instruction start and text per reported RIP (each read once per session)."""

    read: Read
    found: dict[tuple[int, str], tuple[int, str]] = field(default_factory=dict)

    def __call__(self, rip: int, kind: WatchKind) -> tuple[int, str]:
        key = (rip, "execute" if kind == "execute" else "data")
        if key not in self.found:
            self.found[key] = self._find(rip, kind)
        return self.found[key]

    def _find(self, rip: int, kind: WatchKind) -> tuple[int, str]:
        try:
            if kind == "execute":
                insn = ix.Decoder(64, self.read(rip, MAX_INSN), ip=rip).decode()
                if insn.code == ix.Code.INVALID:
                    return rip, UNKNOWN_TEXT
                return rip, insn_text(insn)
            before = instruction_before(self.read(rip - CONTEXT, CONTEXT), rip, touches_memory)
        except ConsoleBusy:
            raise
        except ReadFailed as exc:
            log.info("action=pro_watch_decode rip=%#x reason=%s", rip, exc)
            return rip, UNKNOWN_TEXT
        if before is None:
            return rip, UNKNOWN_TEXT
        start, raw, _text = before
        return start, insn_text(ix.Decoder(64, raw, ip=start).decode())


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
    ) -> None:
        if not 1 <= len(targets) <= DEBUG_SLOTS:
            raise ValueError(f"Watch 1 to {DEBUG_SLOTS} addresses at a time.")
        self.dbg = dbg
        self.targets = list(targets)
        self.seconds, self.max_hits = seconds, max_hits
        self.clock = clock
        self.on_update = on_update
        self._lookup = _Lookup(read)
        self._rows: dict[tuple[int, int | None], InstructionRow] = {}
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
        except DebuggerLost as exc:
            ended, message = LOST, str(exc)
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
            now = self.clock()
            if self.on_update is not None and (stop is None or now - shown >= UPDATE_S):
                shown = now
                self.on_update(self.rows(), self.hits, now - started)

    def _slot(self, stop: StopEvent) -> int | None:
        slot = stop.slot
        if slot is not None and slot < len(self.targets):
            return slot
        return 0 if len(self.targets) == 1 else None

    def _add(self, stop: StopEvent) -> None:
        self.hits += 1
        slot = self._slot(stop)
        kind = self.targets[slot].kind if slot is not None else "access"
        address, text = self._lookup(stop.hit.rip, kind)
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
    is found again each run (it isn't known until then)."""
    for loc in cheat.locations:
        if loc.kind is LocationKind.CODE_PATCH or loc.anchor is not None:
            continue
        if len(loc.on) in WATCH_SIZES:
            return WatchTarget(loc.address(module_base), len(loc.on), "write")
    return None
