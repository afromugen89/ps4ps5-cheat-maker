"""The ConsoleClient interface. Core services talk only to this (architecture §4).

Two implementations: ``ps5debug_ng.Ps5DebugNgClient`` (real PS5) and
``fake.FakeConsole`` (recorded memory, for tests, AC-10.1).

Scan state lives inside the client so the real client can keep candidates on the
console later without the core changing.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from cheatmaker.core.values import Compare, Scale, ValueType
from cheatmaker.errors import DebuggerError

ProgressFn = Callable[[float], None]
"""Called with 0.0..1.0 while a long operation runs."""


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    name: str
    title_id: str = ""
    content_id: str = ""


@dataclass(frozen=True)
class MemMap:
    name: str
    start: int
    end: int
    prot: int  # bit 1 = read, bit 2 = write, bit 4 = exec (FreeBSD VM_PROT_*)

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def readable(self) -> bool:
        return bool(self.prot & 1)

    @property
    def writable(self) -> bool:
        return bool(self.prot & 2)

    @property
    def executable(self) -> bool:
        return bool(self.prot & 4)

    def contains(self, addr: int) -> bool:
        return self.start <= addr < self.end


@dataclass(frozen=True)
class ForegroundApp:
    pid: int
    title_id: str
    title: str
    version: str


@dataclass(frozen=True)
class ScanHit:
    address: int
    value: bytes
    vtype: ValueType | None = field(default=None, compare=False)
    """Set when a search covers several number types at once."""
    way: Scale | None = field(default=None, compare=False)
    """How the match keeps the number (percent, fraction, x10, of full), when known."""
    previous: bytes | None = field(default=None, compare=False)
    """The value at the scan before the last one (None after a first scan, or when the
    client can't tell)."""
    first: bytes | None = field(default=None, compare=False)
    """The value at the first scan, when the client kept it (Pro scanner column)."""


WatchKind = Literal["write", "access", "execute"]
"""``write``: trap on writes only. ``access``: trap on reads and writes (x86 DR7 has no
read-only data watchpoint, so a reader is found with ``access``). ``execute``: ``addr``
is an instruction and ``size`` must be 1; the trap comes *before* the instruction runs,
so ``rip`` is the instruction itself and ``regs`` are the values it is about to use."""


@dataclass(frozen=True)
class WatchHit:
    """One watchpoint trap: where the game was and its general registers.

    Data watchpoints trap *after* the instruction, so ``rip`` points just past it and
    ``regs`` hold the values after it ran. ``regs`` uses lower-case names
    (``rax`` .. ``r15``, ``rip``, ``rsp``, ``rflags``); empty when the console didn't
    send them."""

    rip: int
    regs: dict[str, int] = field(default_factory=dict, hash=False)
    raw: bytes = field(default=b"", compare=False, repr=False)
    """The console's whole trap packet (0x4A0 bytes on NG), kept for the debug log;
    empty when the client has none (FakeConsole)."""

    def reg(self, name: str) -> int | None:
        return self.regs.get(name.lower())


DebugState = Literal["detached", "running", "stopped", "lost"]
DEBUG_SLOTS = 4
"""x86 has four hardware watch slots (DR0-DR3)."""


@dataclass(frozen=True)
class StopEvent:
    """One stop the Pro debugger reported: which thread, its registers (``hit``), the
    raw FPU state (XMM, R3) and DR6, whose bits 0-3 say which watch slot fired."""

    lwpid: int
    thread: str
    hit: WatchHit
    fpu: bytes = field(default=b"", repr=False)
    dr6: int = 0
    signal: int = 0

    @property
    def slot(self) -> int | None:
        """The lowest watch slot DR6 names, or None when it names none."""
        for k in range(DEBUG_SLOTS):
            if self.dr6 >> k & 1:
                return k
        return None


class Debugger(Protocol):
    """A debugger session on one game (architecture-pro ``DebugSession``): attach,
    up to ``DEBUG_SLOTS`` watches, stops, resume, detach. ``detach`` clears every slot
    first and is safe to call more than once."""

    state: DebugState

    def attach(self) -> None: ...

    def set_watch(self, slot: int, addr: int, size: int, kind: WatchKind) -> None: ...

    def clear_watch(self, slot: int) -> None: ...

    def next_stop(self, timeout: float) -> StopEvent | None:
        """The next stop, or None when none came within ``timeout`` seconds. Raises
        DebuggerLost when the session dropped or the game closed."""
        ...

    def resume(self) -> None: ...

    def detach(self) -> None: ...


WatchUntil = Callable[[list[WatchHit], float], bool]
"""``until(hits so far, seconds since the watch started)``: return True to end a watch
early (enough hits, or the player pressed Cancel)."""


@runtime_checkable
class ConsoleClient(Protocol):
    def version(self) -> str: ...

    def processes(self) -> list[ProcInfo]: ...

    def foreground_app(self) -> ForegroundApp | None:
        """The running game, or None when nothing is in the foreground."""
        ...

    def maps(self, pid: int) -> list[MemMap]: ...

    def read(self, pid: int, addr: int, size: int) -> bytes: ...

    def write(self, pid: int, addr: int, data: bytes) -> None:
        """Raises WriteFailed when the console refuses or the read-back differs."""
        ...

    def batch(self) -> AbstractContextManager[None]:
        """Hold the connection for several calls (e.g. a cheat's whole set of writes)
        so they go through together. On the UI thread a busy connection raises
        ConsoleBusy here, before any of them."""
        ...

    def scan_start(
        self,
        pid: int,
        regions: list[MemMap],
        vtype: ValueType | tuple[ValueType, ...],
        cmp: Compare,
        value: float | int | None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
        ways: tuple[Scale, ...] = (),
    ) -> int:
        """First scan over ``regions``; returns the match count.

        A tuple of types (unknown value only) runs one search per type side by side;
        next scans narrow them all, results carry their ``vtype``, and a type left
        with no matches is dropped.

        ``ways`` (percent search): ``cmp`` is BETWEEN and ``value`` the range as the game
        shows it. Part i searches type ``vtype[i]`` (or ``vtype`` for all) for
        ``value`` x ``ways[i].factor``. Next scans scale their value per part the same
        way, results carry ``way``, and parts left empty are dropped."""
        ...

    def scan_next(
        self,
        cmp: Compare,
        value: float | int | None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """Narrow the previous results; returns the match count."""
        ...

    def scan_results(self, limit: int, offset: int = 0) -> list[ScanHit]:
        """At most ``limit`` matches in address order, skipping the first ``offset``
        (the Pro scanner pages through them)."""
        ...

    def scan_counts(self) -> dict[ValueType, int]:
        """Matches left per number type in the current search."""
        ...

    def scan_way_counts(self) -> dict[tuple[ValueType, Scale | None], int]:
        """Matches left per (number type, way) in the current search."""
        ...

    def scan_restrict(self, hits: list[ScanHit]) -> int:
        """Replace the search with just ``hits`` (checked on the PC), kept on the PC.
        Each hit's ``value`` is its baseline for the next compare; its ``vtype`` and
        ``way`` say how it is narrowed. Returns the match count."""
        ...

    def scan_rebase(
        self,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """Keep every match, but make its value now the baseline for the next compare
        (a percent that can only be noted). Returns the match count."""
        ...

    def scan_cancel(self) -> None:
        """Drop the current scan session."""
        ...

    def find_pointers(
        self,
        pid: int,
        regions: list[MemMap],
        low: int,
        high: int,
        keep: Callable[[int], bool],
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> list[tuple[int, int]]:
        """Every 8-byte aligned value in ``regions`` between ``low`` and ``high`` that
        ``keep`` accepts, as (address, value). Used by the pointer scan."""
        ...

    def watch(
        self,
        pid: int,
        addr: int,
        size: int,
        kind: WatchKind,
        timeout_s: float,
        *,
        until: WatchUntil | None = None,
    ) -> list[WatchHit]:
        """Hardware watchpoint on ``addr`` for ``timeout_s`` seconds (at most 16 hits).
        Each hit carries the RIP just past the instruction and the registers.

        ``until`` is asked after each hit and while no hit comes (about twice a
        second); when it returns True the watch ends with the hits so far."""
        ...

    def watch_writes(
        self,
        pid: int,
        addr: int,
        size: int,
        timeout_s: float,
        *,
        until: WatchUntil | None = None,
    ) -> list[int]:
        """Watch ``addr`` for writes. Returns the RIP reported after each write.

        x86 data watchpoints trap *after* the writing instruction, so each RIP
        points just past the writer; core.locker finds the instruction itself.
        Same as ``[h.rip for h in watch(pid, addr, size, "write", timeout_s, until=until)]``.
        """
        ...

    def close(self) -> None: ...


def open_debugger(client: object, pid: int) -> Debugger:
    """``client.open_debugger(pid)``: a new, detached debugger session on game ``pid``
    with its own connection (nothing is opened until ``attach``)."""
    opener = getattr(client, "open_debugger", None)
    if opener is None:
        raise DebuggerError("This connection has no debugger.")
    return opener(pid)


def batch_of(client: object) -> AbstractContextManager[None]:
    """``client.batch()``, or no hold for a client without one (simple test doubles)."""
    batch = getattr(client, "batch", None)
    return batch() if batch is not None else contextlib.nullcontext()
