"""Scan session that keeps candidates on the PC and re-reads them to narrow down.

Used by FakeConsole for everything, and by the real client for "next scan" after a
console-side first scan (candidate lists are small by then).
"""

from __future__ import annotations

import struct
import threading
from collections.abc import Callable, Iterable

from cheatmaker.console.client import MemMap, ProgressFn, ScanHit
from cheatmaker.core.values import Compare, Scale, ValueType, matches, part_value
from cheatmaker.errors import ScanCancelled

ReadFn = Callable[[int, int], bytes]
ReadManyFn = Callable[[list[tuple[int, int]]], list[bytes]]

READ_GAP = 64
"""Candidates closer than this are fetched with one read."""

READ_BATCH = 4096
"""Spans fetched per round trip when the client supports bulk reads."""


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
    def __init__(self, read: ReadFn, read_many: ReadManyFn | None = None) -> None:
        self._read = read
        self._read_many = read_many
        self.vtype: ValueType | None = None
        self.way: Scale | None = None  # percent search: how these matches keep the number
        self.hits: list[ScanHit] = []

    def reset(self) -> None:
        self.vtype = None
        self.way = None
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

    def set_results(self, vtype: ValueType, hits: list[ScanHit], way: Scale | None = None) -> None:
        self.vtype = vtype
        self.way = way
        self.hits = hits

    def current_values(
        self,
        hits: list[ScanHit],
        cancel: threading.Event | None = None,
        progress: ProgressFn | None = None,
    ) -> dict[int, bytes]:
        assert self.vtype is not None
        size = self.vtype.size
        addrs = sorted(h.address for h in hits)
        spans = list(_batched_reads(addrs, size))
        out: dict[int, bytes] = {}
        idx = 0  # addrs and spans are both sorted, so walk them together
        for i in range(0, len(spans), READ_BATCH):
            _check(cancel)
            batch = spans[i : i + READ_BATCH]
            blobs = (
                self._read_many(batch)
                if self._read_many is not None
                else [self._read(s, n) for s, n in batch]
            )
            for (start, length), blob in zip(batch, blobs, strict=True):
                while idx < len(addrs) and addrs[idx] < start + length:
                    a = addrs[idx]
                    out[a] = blob[a - start : a - start + size]
                    idx += 1
            if progress:
                progress(min(1.0, (i + READ_BATCH) / len(spans)))
        return out

    def next(
        self,
        cmp: Compare,
        value: float | int | None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        self.hits = self.narrowed(cmp, value, progress, cancel)
        return len(self.hits)

    def narrowed(
        self,
        cmp: Compare,
        value: float | int | None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> list[ScanHit]:
        """The matches left after this compare, without keeping them (``next`` does)."""
        if self.vtype is None:
            raise ValueError("no scan to narrow down; start a new search")
        if cmp is Compare.UNKNOWN:
            raise ValueError('"Unknown value" is only for the first search')
        current = self.current_values(self.hits, cancel, progress)
        if progress:
            progress(1.0)
        vt = self.vtype
        return [
            ScanHit(h.address, current[h.address])
            for h in self.hits
            if h.address in current and matches(vt, cmp, current[h.address], h.value, value)
        ]

    def results(self, limit: int) -> list[ScanHit]:
        if self.way is None:
            return self.hits[:limit]
        return [ScanHit(h.address, h.value, h.vtype, self.way) for h in self.hits[:limit]]


NarrowFn = Callable[..., list[ScanHit]]
"""(session, cmp, value, progress, cancel) -> the session's matches left, not kept yet."""
CheckFn = Callable[..., None]
"""(session, cmp, value): raise when this part can't be narrowed this way."""


class MultiScan:
    """Several LocalScanSessions, one per number type (an "I don't know the number"
    search over decimals and whole numbers) or per (type, way) of a percent search,
    narrowed together."""

    def __init__(self, sessions: list[LocalScanSession]) -> None:
        self.sessions = sessions

    @property
    def vtype(self) -> ValueType | None:
        return self.sessions[0].vtype if len(self.sessions) == 1 else None

    @property
    def hits(self) -> list[ScanHit]:
        return [h for s in self.sessions for h in s.hits]

    def counts(self) -> dict[ValueType, int]:
        out: dict[ValueType, int] = {}
        for s in self.sessions:
            if s.vtype is not None:
                out[s.vtype] = out.get(s.vtype, 0) + len(s.hits)
        return out

    def way_counts(self) -> dict[tuple[ValueType, Scale | None], int]:
        return {(s.vtype, s.way): len(s.hits) for s in self.sessions if s.vtype is not None}

    def next(
        self,
        cmp: Compare,
        value,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
        narrow: NarrowFn | None = None,
        check: CheckFn | None = None,
    ) -> int:
        """Narrow every part, all or nothing: ``check(session, cmp, value)`` first runs on
        every part, then ``narrow(session, cmp, value, progress, cancel)`` works out each
        part's new matches (default: read the candidates on the PC), and only when all
        succeeded are they kept. An error or Cancel leaves the search as it was. Each part
        gets the value already scaled for its way."""
        plans = []
        for s in self.sessions:
            assert s.vtype is not None
            v = part_value(s.vtype, s.way, cmp, value)
            plans.append((s, v, value is not None and v is None))  # True: none can match
        if check is not None:
            for s, v, none in plans:
                if not none:
                    check(s, cmp, v)
        new: list[list[ScanHit]] = []
        n = len(plans)
        for i, (s, v, none) in enumerate(plans):

            def mine(p: float, i: int = i) -> None:
                if progress:
                    progress((i + p) / n)

            if none:
                new.append([])
            elif narrow is not None:
                new.append(narrow(s, cmp, v, mine, cancel))
            else:
                new.append(s.narrowed(cmp, v, mine, cancel))
        for (s, _v, _none), hits in zip(plans, new, strict=True):
            s.hits = hits
        if progress:
            progress(1.0)
        drop_empty(self.sessions, lambda s: len(s.hits))
        return sum(len(s.hits) for s in self.sessions)

    def results(self, limit: int) -> list[ScanHit]:
        return merge_results([(s.vtype, s.results(limit)) for s in self.sessions], limit)


def drop_empty(sessions: list, count: Callable) -> list:
    """Remove the sessions with no matches, keeping at least one. Returns the removed."""
    keep = [s for s in sessions if count(s)] or sessions[:1]
    gone = [s for s in sessions if s not in keep]
    sessions[:] = keep
    return gone


def merge_results(per_type: list, limit: int) -> list[ScanHit]:
    """Matches of several types in address order (each tagged with its type, and its
    way when known), at most ``limit``."""
    tagged = [ScanHit(h.address, h.value, vt, h.way) for vt, hits in per_type for h in hits]
    tagged.sort(key=lambda h: h.address)
    return tagged[:limit]


def way_parts(
    vtype: ValueType | tuple[ValueType, ...], ways: tuple[Scale, ...]
) -> list[tuple[ValueType, Scale]]:
    """The (type, way) parts of a percent first scan: ``vtype[i]`` with ``ways[i]``, or
    one type with every way."""
    types = vtype if isinstance(vtype, tuple) else (vtype,) * len(ways)
    if len(types) != len(ways):
        raise ValueError("a percent search needs one number type per way")
    return list(zip(types, ways, strict=True))


def check_ways(cmp: Compare, value) -> None:
    if cmp is not Compare.BETWEEN or value is None:
        raise ValueError("a search with ways is a range search (BETWEEN)")


def restrict(
    hits: list[ScanHit],
    make: Callable[[], LocalScanSession],
    fallback: ValueType | None,
) -> MultiScan:
    """A PC-side search holding just ``hits``, one part per (type, way). A hit's
    value is its baseline for the next compare. ``fallback`` is the type of hits that
    don't name one (and of the empty search when ``hits`` is empty)."""
    parts: dict[tuple[ValueType, Scale | None], list[ScanHit]] = {}
    for h in sorted(hits, key=lambda h: h.address):
        vt = h.vtype or fallback
        if vt is None:
            raise ValueError("no number type for a match; start a new search")
        parts.setdefault((vt, h.way), []).append(ScanHit(h.address, h.value))
    if not parts:
        if fallback is None:
            raise ValueError("no search to narrow down; start a new search")
        parts[(fallback, None)] = []
    sessions = []
    for (vt, way), part in parts.items():
        s = make()
        s.set_results(vt, part, way)
        sessions.append(s)
    return MultiScan(sessions)


def unpack_all(vtype: ValueType, data: bytes) -> list[float | int]:
    """Decode a packed run of values (helper for tests and dumps)."""
    return [v[0] for v in struct.iter_unpack(vtype.fmt, data)]
