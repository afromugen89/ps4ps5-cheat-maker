"""The ConsoleClient interface. Core services talk only to this (architecture §4).

Two implementations: ``ps5debug_ng.Ps5DebugNgClient`` (real PS5) and
``fake.FakeConsole`` (recorded memory, for tests, AC-10.1).

Scan state lives inside the client so the real client can keep candidates on the
console later without the core changing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from cheatmaker.core.values import Compare, ValueType

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

    def scan_start(
        self,
        pid: int,
        regions: list[MemMap],
        vtype: ValueType,
        cmp: Compare,
        value: float | int | None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """First scan over ``regions``; returns the match count."""
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

    def scan_results(self, limit: int) -> list[ScanHit]: ...

    def scan_cancel(self) -> None:
        """Drop the current scan session."""
        ...

    def watch_writes(self, pid: int, addr: int, size: int, timeout_s: float) -> list[int]:
        """Watch ``addr`` for writes. Returns the RIP reported after each write.

        x86 data watchpoints trap *after* the writing instruction, so each RIP
        points just past the writer; core.locker finds the instruction itself.
        """
        ...

    def close(self) -> None: ...
