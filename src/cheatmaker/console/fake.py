"""FakeConsole: a stand-in PS5 that serves recorded memory (AC-10.1).

Tests build one in code (``add_region``) or load a recorded dump directory
(``from_dump``): ``manifest.json`` + one ``.bin`` file per region. Use
``tools/record_dump.py`` on a real console to record new dumps.
"""

from __future__ import annotations

import json
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cheatmaker.console.client import (
    ForegroundApp,
    MemMap,
    ProcInfo,
    ProgressFn,
    ScanHit,
    WatchHit,
    WatchKind,
)
from cheatmaker.console.local_scan import LocalScanSession, MultiScan
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.errors import ConnectionLost, ReadFailed, ScanNotSupported, WriteFailed

PROT_R, PROT_W, PROT_X = 1, 2, 4


@dataclass
class _Region:
    map: MemMap
    data: bytearray


@dataclass
class FakeProcess:
    info: ProcInfo
    title: str = ""
    version: str = "01.000"
    regions: list[_Region] = field(default_factory=list)


class FakeConsole:
    def __init__(self, server_version: str = "1.3") -> None:
        self.server_version = server_version
        self._procs: dict[int, FakeProcess] = {}
        self.foreground_pid: int | None = None
        self.connected = True
        self._app_closed = False  # the app closed the link (it can open a new one)
        self.fail_writes = False
        self.supports_unknown_scan = True
        # addr -> RIPs that "write" it while watched (set by tests / dumps)
        self.writers: dict[int, list[int]] = {}
        # addr -> hits with registers. write_hits override ``writers`` for that addr;
        # an "access" watch sees read_hits plus the write hits (like DR7 RW=11).
        self.write_hits: dict[int, list[WatchHit]] = {}
        self.read_hits: dict[int, list[WatchHit]] = {}
        self.watched: list[tuple[int, int, str, float]] = []  # (addr, size, kind, timeout)
        # Called before every read: lets a test "run the game" between requests
        # (e.g. tests/fakegame.py runs installed capture routines).
        self.game_ticks: list[Callable[[], None]] = []
        self.pointer_passes: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int, bytes]] = []
        self._lock = threading.RLock()
        self._scan: LocalScanSession | MultiScan | None = None
        self._scan_pid: int | None = None

    # -- building ----------------------------------------------------------
    def add_process(
        self,
        pid: int,
        name: str = "eboot.bin",
        title_id: str = "",
        title: str = "",
        version: str = "01.000",
        foreground: bool = True,
    ) -> FakeProcess:
        p = FakeProcess(ProcInfo(pid, name, title_id, ""), title, version)
        self._procs[pid] = p
        if foreground:
            self.foreground_pid = pid
        return p

    def remove_process(self, pid: int) -> None:
        self._procs.pop(pid, None)
        if self.foreground_pid == pid:
            self.foreground_pid = None

    def add_region(
        self, pid: int, name: str, start: int, data: bytes | int, prot: int = PROT_R | PROT_W
    ) -> MemMap:
        buf = bytearray(data)  # bytes, or an int size for zero-filled memory
        m = MemMap(name, start, start + len(buf), prot)
        self._procs[pid].regions.append(_Region(m, buf))
        return m

    def poke(self, pid: int, addr: int, data: bytes) -> None:
        """Change memory the way the game would (no write log, ignores fail_writes)."""
        r, off = self._locate(pid, addr, len(data))
        r.data[off : off + len(data)] = data

    def peek(self, pid: int, addr: int, size: int) -> bytes:
        r, off = self._locate(pid, addr, size)
        return bytes(r.data[off : off + size])

    def disconnect(self) -> None:
        """The link drops (Wi-Fi, PS5 off): a new connection fails too."""
        self.connected = False
        self._app_closed = False

    @classmethod
    def from_dump(cls, folder: str | Path) -> FakeConsole:
        folder = Path(folder)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        fake = cls(manifest.get("server_version", "1.3"))
        g = manifest["game"]
        pid = int(g["pid"])
        fake.add_process(
            pid,
            g.get("process", "eboot.bin"),
            g["title_id"],
            g.get("title", ""),
            g.get("version", "01.000"),
        )
        for r in manifest["regions"]:
            data = (folder / r["file"]).read_bytes()
            fake.add_region(pid, r["name"], int(r["start"], 16), data, int(r["prot"]))
        for addr, rips in manifest.get("writers", {}).items():
            fake.writers[int(addr, 16)] = [int(x, 16) for x in rips]
        return fake

    # -- helpers -------------------------------------------------------------
    def _alive(self) -> None:
        if not self.connected:
            raise ConnectionLost("fake console disconnected")

    def _proc(self, pid: int) -> FakeProcess:
        try:
            return self._procs[pid]
        except KeyError:
            raise ReadFailed(f"no process {pid}") from None

    def _locate(self, pid: int, addr: int, size: int) -> tuple[_Region, int]:
        for r in self._proc(pid).regions:
            if r.map.start <= addr and addr + size <= r.map.end:
                return r, addr - r.map.start
        raise ReadFailed(f"address {addr:#x}+{size} is not mapped in pid {pid}")

    # -- ConsoleClient -------------------------------------------------------
    def version(self) -> str:
        # version() is the first request on a new connection: after the app closed
        # its link (Connect again, a lost-link reset), a factory that hands back this
        # same console gets a working one, like a new socket would.
        if not self.connected and self._app_closed:
            self.connected, self._app_closed = True, False
        self._alive()
        return self.server_version

    def processes(self) -> list[ProcInfo]:
        self._alive()
        return [p.info for p in self._procs.values()]

    def foreground_app(self) -> ForegroundApp | None:
        self._alive()
        if self.foreground_pid is None or self.foreground_pid not in self._procs:
            return None
        p = self._procs[self.foreground_pid]
        return ForegroundApp(p.info.pid, p.info.title_id, p.title, p.version)

    def maps(self, pid: int) -> list[MemMap]:
        self._alive()
        return [r.map for r in self._proc(pid).regions]

    def read(self, pid: int, addr: int, size: int) -> bytes:
        self._alive()
        with self._lock:
            for tick in list(self.game_ticks):
                tick()
            return self.peek(pid, addr, size)

    def write(self, pid: int, addr: int, data: bytes) -> None:
        self._alive()
        if self.fail_writes:
            raise WriteFailed("the console refused the write (fake)")
        with self._lock:
            try:
                self.poke(pid, addr, data)
            except ReadFailed as exc:
                raise WriteFailed(str(exc)) from None
            self.writes.append((pid, addr, bytes(data)))

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
        self._alive()
        if cmp is Compare.UNKNOWN and not self.supports_unknown_scan:
            raise ScanNotSupported("unknown-value scan not available")
        self._scan_pid = pid
        if isinstance(vtype, tuple):
            if cmp is not Compare.UNKNOWN or value is not None:
                raise ValueError("several number types at once is for unknown searches only")
            sessions = []
            for i, vt in enumerate(vtype):
                s = LocalScanSession(lambda a, n: self.read(pid, a, n))
                s.first(regions, vt, cmp, value, None, cancel)
                sessions.append(s)
                if progress:
                    progress((i + 1) / len(vtype))
            self._scan = MultiScan(sessions)
            return len(self._scan.hits)
        self._scan = LocalScanSession(lambda a, n: self.read(pid, a, n))
        return self._scan.first(regions, vtype, cmp, value, progress, cancel)

    def scan_next(
        self,
        cmp: Compare,
        value: float | int | None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        self._alive()
        if self._scan is None:
            raise ValueError("no scan to narrow down; start a new search")
        return self._scan.next(cmp, value, progress, cancel)

    def scan_results(self, limit: int) -> list[ScanHit]:
        self._alive()
        return [] if self._scan is None else self._scan.results(limit)

    def scan_counts(self) -> dict[ValueType, int]:
        if self._scan is None:
            return {}
        if isinstance(self._scan, MultiScan):
            return self._scan.counts()
        return {} if self._scan.vtype is None else {self._scan.vtype: len(self._scan.hits)}

    def scan_cancel(self) -> None:
        self._scan = None
        self._scan_pid = None

    def find_pointers(
        self,
        pid: int,
        regions: list[MemMap],
        low: int,
        high: int,
        keep,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> list[tuple[int, int]]:
        self._alive()
        self.pointer_passes.append((low, high))
        out = []
        for r in regions:
            data = self.read(pid, r.start, r.size)
            for off in range(0, len(data) - 7, 8):
                (v,) = struct.unpack_from("<Q", data, off)
                if low <= v <= high and keep(v):
                    out.append((r.start + off, v))
        if progress:
            progress(1.0)
        return out

    def watch(
        self, pid: int, addr: int, size: int, kind: WatchKind, timeout_s: float
    ) -> list[WatchHit]:
        self._alive()
        self._proc(pid)
        if kind not in ("write", "access"):
            raise ValueError(f"unknown watch kind {kind!r}")
        self.watched.append((addr, size, kind, timeout_s))
        writes = self.write_hits.get(addr)
        if writes is None:
            writes = [WatchHit(r) for r in self.writers.get(addr, [])]
        hits = list(writes) if kind == "write" else [*self.read_hits.get(addr, []), *writes]
        return hits[:16]

    def watch_writes(self, pid: int, addr: int, size: int, timeout_s: float) -> list[int]:
        return [h.rip for h in self.watch(pid, addr, size, "write", timeout_s)]

    def close(self) -> None:
        if self.connected:
            self._app_closed = True
        self.connected = False
