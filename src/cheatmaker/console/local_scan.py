"""Scan session that keeps candidates on the PC and re-reads them to narrow down.

Used by FakeConsole for everything, and by the real client for "next scan" after a
console-side first scan (candidate lists are small by then).
"""

from __future__ import annotations

import struct
import threading
from collections.abc import Callable, Iterable

from cheatmaker.console.client import MemMap, ProgressFn, ScanHit
from cheatmaker.core.values import Compare, ValueType, matches
from cheatmaker.errors import ScanCancelled

ReadFn = Callable[[int, int], bytes]

READ_GAP = 64
"""Candidates closer than this are fetched with one read."""


def alignment_for(vtype: ValueType) -> int:
    return min(vtype.size, 4)


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled("scan cancelled by the player")


def scan_buffer(
    base: int,
    data: bytes,
    vtype: ValueType,
    cmp: Compare,
    value: float | int | None,
) -> list[ScanHit]:
    """First-scan one region already in memory. Aligned like most game data."""
    step = alignment_for(vtype)
    size = vtype.size
    hits: list[ScanHit] = []
    if cmp is Compare.EXACT and not vtype.is_float:
        # Fast path: find the exact byte pattern, keep aligned hits.
        needle = vtype.encode(value)  # type: ignore[arg-type]
        i = data.find(needle)
        while i != -1:
            if i % step == 0:
                hits.append(ScanHit(base + i, needle))
            i = data.find(needle, i + 1)
        return hits
    for off in range(0, len(data) - size + 1, step):
        chunk = data[off : off + size]
        if matches(vtype, cmp, chunk, None, value):
            hits.append(ScanHit(base + off, bytes(chunk)))
    return hits


def _batched_reads(addrs: list[int], size: int) -> Iterable[tuple[int, int]]:
    """Group sorted addresses into (start, length) reads."""
    if not addrs:
        return
    start = prev = addrs[0]
    for a in addrs[1:]:
        if a - prev > READ_GAP:
            yield start, prev + size - start
            start = a
        prev = a
    yield start, prev + size - start


class LocalScanSession:
    def __init__(self, read: ReadFn) -> None:
        self._read = read
        self.vtype: ValueType | None = None
        self.hits: list[ScanHit] = []

    def reset(self) -> None:
        self.vtype = None
        self.hits = []

    def first(
        self,
        regions: list[MemMap],
        vtype: ValueType,
        cmp: Compare,
        value: float | int | None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        total = sum(r.size for r in regions) or 1
        done = 0
        hits: list[ScanHit] = []
        for r in regions:
            _check(cancel)
            hits.extend(scan_buffer(r.start, self._read(r.start, r.size), vtype, cmp, value))
            done += r.size
            if progress:
                progress(done / total)
        self.vtype = vtype
        self.hits = hits
        return len(hits)

    def set_results(self, vtype: ValueType, hits: list[ScanHit]) -> None:
        self.vtype = vtype
        self.hits = hits

    def current_values(
        self, hits: list[ScanHit], cancel: threading.Event | None = None
    ) -> dict[int, bytes]:
        assert self.vtype is not None
        size = self.vtype.size
        addrs = sorted(h.address for h in hits)
        out: dict[int, bytes] = {}
        for start, length in _batched_reads(addrs, size):
            _check(cancel)
            blob = self._read(start, length)
            for a in addrs:
                if start <= a < start + length:
                    out[a] = blob[a - start : a - start + size]
        return out

    def next(
        self,
        cmp: Compare,
        value: float | int | None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        if self.vtype is None:
            raise ValueError("no scan to narrow down; start a new search")
        if cmp is Compare.UNKNOWN:
            raise ValueError('"Unknown value" is only for the first search')
        current = self.current_values(self.hits, cancel)
        if progress:
            progress(1.0)
        vt = self.vtype
        self.hits = [
            ScanHit(h.address, current[h.address])
            for h in self.hits
            if h.address in current and matches(vt, cmp, current[h.address], h.value, value)
        ]
        return len(self.hits)

    def results(self, limit: int) -> list[ScanHit]:
        return self.hits[:limit]


def unpack_all(vtype: ValueType, data: bytes) -> list[float | int]:
    """Decode a packed run of values (helper for tests and dumps)."""
    return [v[0] for v in struct.iter_unpack(vtype.fmt, data)]
