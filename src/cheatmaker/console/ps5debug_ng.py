"""Real ConsoleClient for PS5Debug-NG (TCP 744).

Wire format from PS5Debug-NG PROTOCOL.md v1.3.0, cross-checked against the
GPL-3.0 ``ps5dbg`` 0.1.1 package (allowed because this app is GPL-3.0, spec D9).
Every command here is UNVERIFIED on fw 13.60 until console tests S1-S3 pass;
see docs/console-tests.md.

Framing: request = u32 magic 0xFFAABBCC | u32 cmd | u32 body length, then body.
Status words come back bit-swapped; we compare the raw wire word.
"""

from __future__ import annotations

import contextlib
import logging
import math
import queue
import socket
import struct
import threading
import time
from collections.abc import Callable
from enum import IntEnum

from cheatmaker.console.client import (
    ForegroundApp,
    MemMap,
    ProcInfo,
    ProgressFn,
    ScanHit,
    WatchHit,
    WatchKind,
    WatchUntil,
)
from cheatmaker.console.local_scan import (
    LocalScanSession,
    MultiScan,
    alignment_for,
    check_ways,
    drop_empty,
    merge_results,
    one_way_each,
    restrict,
    way_parts,
)
from cheatmaker.core.values import Compare, Scale, ValueType, matches, part_value, value_for
from cheatmaker.errors import (
    ConnectionLost,
    ConsoleUnreachable,
    ProtocolError,
    ReadFailed,
    ScanCancelled,
    ScanNotSupported,
    WriteFailed,
)

log = logging.getLogger(__name__)

MAGIC = 0xFFAABBCC
MAX_BODY = 0x100000  # 1 MiB per read/write request
SCAN_SLICE = 64 * 1024 * 1024  # first scan in slices so Cancel and progress respond
BRIDGE_GAP = 1 << 20
"""Writable ranges this close are scanned in one request (one round trip instead of
many); matches that land in the gap are dropped. NG reads unmapped memory as zeros,
so a gap costs at most the time to compare 1 MiB, less than one round trip."""
RESCAN_GAP = 4 << 20
"""Next search with many candidates: candidates this close are re-scanned in one window
(scanning 4 MiB costs about one request's overhead)."""
SCAN_CONNECTIONS = 4
"""Connections that scan slices at the same time. NG runs each connection on its own
thread, so this hides the per-request delay and compares on several cores. NG allows
12 clients; the app also needs one for itself and one for Cancel."""
MAX_SCAN_CONNECTIONS = 8
POOL_RETRY_S = 60.0  # after an extra connection fails, scan without it this long
PIPELINE_DEPTH = 128
"""Plain reads sent before waiting for the answers. NG reads requests in order with
exact lengths, so they queue on the socket; this skips one round trip plus the
server's 10 ms idle poll per read."""
IO_TIMEOUT = 4.0  # short, so a dropped PS5 is noticed within 5 s while freezing (AC-1.4)
SCAN_TIMEOUT = 60.0  # one scan slice runs on the console and may take a while
SENTINEL_U64 = 0xFFFFFFFFFFFFFFFF
TSE_PARALLEL_COMPARE = 0x100  # turbo engine bit: server can compare on several threads
TS_PARALLEL_COMPARE = 0x80  # turbo request flag asking for that (single connection only)
TSE_ALIASING = 0x02  # engine: reads the game's memory in place instead of copying it
TS_USE_ALIASING = 0x01  # request flag for that; NG falls back to copying on any miss
TSE_RESCAN_ALIASING = 0x200  # engine: narrowing on the console reads in place too
TS_RESCAN_ALIASING = 0x100
# Unknown-value search keeps a snapshot ON THE CONSOLE (NG scan_turbo.c), so 12 GiB of
# game memory never crosses the network. Engine bits / request flags from PROTOCOL.md.
TSE_SNAPSHOT = 0x08
TSE_SNAPSHOT_SEGMENTS = 0x10
TSE_FLOAT_POLICY = 0x400
TS_SERVER_RESIDENT = 0x02
TS_SNAPSHOT = 0x04
TS_SNAPSHOT_SEGMENTS = 0x10
TS_FLOAT_SIMPLE = 0x200
"""Floats only: skip values whose exponent is far from 1.0 (garbage bit patterns)."""
SNAP_SEGMENT_MAX = 0x8000_0000  # a segment length is a u32
AUTH_MAGIC = 0xBB40E64D
INTERRUPT_PORT = 755
INTERRUPT_SIZE = 0x4A0
REG64_OFFSET = 0x30
"""PROTOCOL.md §1.9: lwpid u32, status u32, tdname[40], then reg64 (struct reg, 176
bytes) at 0x30, savefpu at 0xE0, dbreg64 at 0x420."""
REG64_SIZE = 176
RIP_OFFSET = REG64_OFFSET + 136
"""FreeBSD amd64 struct reg: r15..rax (15 x 8 = 120 bytes), then trapno/fs/gs/err/es/ds
(16 bytes), then rip. reg64 starts at 0x30 in the interrupt packet."""
_GP_ORDER = (
    "r15", "r14", "r13", "r12", "r11", "r10", "r9", "r8",
    "rdi", "rsi", "rbp", "rbx", "rdx", "rcx", "rax",
)  # fmt: skip
_TAIL_ORDER = ("rip", "cs", "rflags", "rsp", "ss")  # u64 each, after the 16-byte middle

ATTACH_TIMEOUT = 30.0
"""Attaching pauses the game and dials back on port 755; it took over 4 s in C4."""


class Status(IntEnum):  # on-wire (already bit-swapped) values
    SUCCESS = 0x80000000
    ERROR = 0xF0000001
    DATA_NULL = 0xF0000003
    ALREADY_DEBUG = 0xF0000004
    INVALID_INDEX = 0xF0000005


class Cmd(IntEnum):
    VERSION = 0xBD000001
    PROC_LIST = 0xBDAA0001
    PROC_READ = 0xBDAA0002
    PROC_WRITE = 0xBDAA0003
    PROC_MAPS = 0xBDAA0004
    PROC_INFO = 0xBDAA000A
    PROC_AUTH = 0xBDAACCFF
    PROC_SCAN_START = 0xBDAACC01
    PROC_SCAN_GET = 0xBDAACC03
    TURBOSCAN_CAPS = 0xBDAACC10
    TURBOSCAN_START = 0xBDAACC11
    TURBOSCAN_COUNT = 0xBDAACC12
    TURBOSCAN_GET = 0xBDAACC13
    TURBOSCAN_END = 0xBDAACC14
    TURBOSCAN_CANCEL = 0xBDAACC17
    DEBUG_ATTACH = 0xBDBB0001
    DEBUG_DETACH = 0xBDBB0002
    DEBUG_SET_WATCHPOINT = 0xBDBB0004
    DEBUG_CONTINUE = 0xBDBB0010
    CONSOLE_FOREGROUND_APP = 0xBDDD0006


# Server value-type ids (scan_compare.c) for our signed types.
_NG_VTYPE = {
    ValueType.INT8: 1,
    ValueType.INT16: 3,
    ValueType.INT32: 5,
    ValueType.INT64: 7,
    ValueType.FLOAT: 8,
    ValueType.DOUBLE: 9,
}
_NG_EXACT = 0
_NG_UNKNOWN = 11
_NG_COMPARE = {
    Compare.EXACT: 0,
    Compare.INCREASED: 5,
    Compare.DECREASED: 7,
    Compare.CHANGED: 9,
    Compare.UNCHANGED: 10,
    Compare.BETWEEN: 4,  # server keeps low <= value <= high (both included)
    Compare.INCREASED_BY: 6,  # IncreasedBy / DecreasedBy: UNVERIFIED on console (C11a)
    Compare.DECREASED_BY: 8,
}
_NG_FUZZY = 1
"""FuzzyValue: |typed - memory| < 1.0, float/double only. Legacy ExactValue on floats
is a near-bit-exact match, so typing 80 misses health that is really 79.6 (C3)."""
_DR7_LEN = {1: 0, 2: 1, 4: 3, 8: 2}
_DR7_WRITE = 1
_DR7_RDWR = 3
"""PROTOCOL.md §7.3 breaktype: 0 exec, 1 write only, 3 read-write."""
_DR7_EXEC = 0
_DR7_KIND = {"write": _DR7_WRITE, "access": _DR7_RDWR, "execute": _DR7_EXEC}
MAX_WATCH_HITS = 16


def parse_struct_reg(blob: bytes) -> dict[str, int]:
    """FreeBSD amd64 ``struct reg`` (176 bytes) to {"rax": ..., "rip": ...}.

    Layout from the FreeBSD headers; r12 is at +24 and rip at +136 by it. The rip
    offset is confirmed on the PS5 (C4); the other registers are UNVERIFIED on the
    console until test C10a."""
    if len(blob) < REG64_SIZE:
        raise ProtocolError(f"register block is {len(blob)} bytes, expected {REG64_SIZE}")
    gp = struct.unpack_from("<15Q", blob, 0)
    tail = struct.unpack_from("<5Q", blob, 136)
    return dict(zip(_GP_ORDER, gp, strict=True)) | dict(zip(_TAIL_ORDER, tail, strict=True))


def parse_interrupt(packet: bytes) -> WatchHit:
    regs = parse_struct_reg(packet[REG64_OFFSET : REG64_OFFSET + REG64_SIZE])
    return WatchHit(regs["rip"], regs, raw=bytes(packet))


def merge_ranges(regions: list[MemMap]) -> list[tuple[int, int]]:
    """Join regions that touch, so a game with tens of thousands of small maps
    (Returnal has ~38k) is scanned in a few big requests, not one per map."""
    out: list[tuple[int, int]] = []
    for r in sorted(regions, key=lambda m: m.start):
        if out and r.start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], r.end))
        else:
            out.append((r.start, r.end))
    return out


MAX_READ_SPANS = 4000
"""Above this many reads, narrowing down re-scans on the console instead. Reads are
pipelined (read_many), so a few thousand cost about a second."""


def read_spans(addrs: list[int], size: int) -> list[tuple[int, int]]:
    from cheatmaker.console.local_scan import _batched_reads

    return list(_batched_reads(sorted(addrs), size))


def plan_slices(
    ranges: list[tuple[int, int]],
    size: int,
    align: int,
    slice_len: int | None = None,
    bridge: int | None = None,
) -> list[tuple[int, int]]:
    """(start, length) requests covering ``ranges``: ranges less than ``bridge`` apart
    share a request, and long runs are cut into ``slice_len`` pieces. A piece reaches
    ``size - align`` bytes into the next one, so a value that starts just before a cut
    is still found (in exactly one piece)."""
    slice_len = SCAN_SLICE if slice_len is None else slice_len
    bridge = BRIDGE_GAP if bridge is None else bridge
    runs: list[list[int]] = []
    for s, e in sorted(ranges):
        if runs and s - runs[-1][1] <= bridge:
            runs[-1][1] = max(runs[-1][1], e)
        else:
            runs.append([s, e])
    out = []
    for s, e in runs:
        for a in range(s, e, slice_len):
            out.append((a, min(a + slice_len + size - align, e) - a))
    return out


KEEP_EVERY = (-math.inf, math.inf)


def _every(vtype: ValueType) -> tuple[float, float] | tuple[int, int]:
    """A range compare that keeps every value of ``vtype`` (but a decimal's NaN)."""
    return KEEP_EVERY if vtype.is_float else vtype.int_range()


def inside(hits: list[ScanHit], ranges: list[tuple[int, int]], size: int) -> list[ScanHit]:
    """Only the matches that lie wholly inside one of the (sorted, disjoint) ranges.
    Drops what a bridged request found in the gap between two ranges."""
    import bisect

    if not hits:
        return hits
    starts = [s for s, _ in ranges]
    lo = min(h.address for h in hits)
    hi = max(h.address for h in hits)
    i = bisect.bisect_right(starts, lo) - 1
    if i >= 0 and lo >= ranges[i][0] and hi + size <= ranges[i][1]:
        return hits  # all in one range: the usual case, no per-match work
    out = []
    for h in hits:
        j = bisect.bisect_right(starts, h.address) - 1
        if j >= 0 and h.address + size <= ranges[j][1]:
            out.append(h)
    return out


def candidate_windows(
    ranges: list[tuple[int, int]], sorted_addrs: list[int], size: int, gap: int | None = None
) -> list[tuple[int, int]]:
    """Address windows to re-scan for a next search: candidates less than ``gap``
    apart share a window, cut to the range that holds them. Much less than re-scanning
    every range that holds a candidate (a range can be gigabytes)."""
    import bisect

    gap = RESCAN_GAP if gap is None else gap
    starts = [s for s, _ in ranges]
    out: list[tuple[int, int]] = []
    cur: list[int] | None = None  # [start, end, range index]
    for a in sorted_addrs:
        i = bisect.bisect_right(starts, a) - 1
        if i < 0 or a + size > ranges[i][1]:
            continue
        if cur is not None and cur[2] == i and a - cur[1] <= gap:
            cur[1] = a + size
            continue
        if cur is not None:
            out.append((cur[0], cur[1]))
        cur = [a, a + size, i]
    if cur is not None:
        out.append((cur[0], cur[1]))
    return out


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")


def build_keystream() -> bytes:
    """Keystream for CMD_PROC_AUTH (PS5Debug-NG auth.c 4-register LFSR)."""
    m = 0xFFFFFFFF
    s1, s2, s3, s4 = 200, 300, 400, 500
    out = bytearray()
    for _ in range(256):
        s1 = (((s1 << 18) & 0xFFF80000) ^ (((s1 ^ ((s1 << 6) & m)) & m) >> 13)) & m
        s2 = (((s2 << 2) & 0xFFFFFFE0) ^ (((s2 ^ ((s2 << 2) & m)) & m) >> 27)) & m
        s3 = (((s3 << 7) & 0xFFFFF800) ^ (((s3 ^ ((s3 << 13) & m)) & m) >> 21)) & m
        s4 = (((s4 << 13) & 0xFFF00000) ^ (((s4 ^ ((s4 << 3) & m)) & m) >> 12)) & m
        out.append((s1 ^ s2 ^ s3 ^ s4) & 0xFF)
    return bytes(out)


class _Wire:
    """One TCP socket; every send/receive error becomes ConnectionLost."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.last_cmd = "-"  # named in errors so a log shows which command failed

    def send(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError as exc:
            raise ConnectionLost(f"send failed after {self.last_cmd}: {exc}") from exc

    def request(self, cmd: int, body: bytes = b"") -> None:
        self.last_cmd = Cmd(cmd).name if cmd in Cmd._value2member_map_ else f"{cmd:#010x}"
        self.send(struct.pack("<III", MAGIC, int(cmd), len(body)) + body)

    def recv(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(min(n - len(buf), 1 << 20))
            except OSError as exc:  # includes timeout
                raise ConnectionLost(f"receive failed after {self.last_cmd}: {exc}") from exc
            if not chunk:
                raise ConnectionLost(f"the PS5 closed the connection after {self.last_cmd}")
            buf += chunk
        return bytes(buf)

    def u16(self) -> int:
        return struct.unpack("<H", self.recv(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.recv(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.recv(8))[0]

    def status(self) -> int:
        return self.u32()

    def ok(self, what: str, error: type[Exception] = ProtocolError) -> None:
        st = self.status()
        if st != Status.SUCCESS:
            name = Status(st).name if st in Status._value2member_map_ else f"{st:#010x}"
            raise error(f"{what}: console answered {name}")


class Ps5DebugNgClient:
    def __init__(
        self,
        sock: socket.socket,
        host: str,
        port: int = 744,
        scan_connections: int = SCAN_CONNECTIONS,
        fast_scan: bool = True,
    ) -> None:
        self._w = _Wire(sock)
        self.host, self.port = host, port
        # Speed settings (settings.json): how many connections scan at once, and
        # whether to ask NG to read memory in place (TS_USE_ALIASING).
        self.scan_connections = max(1, min(MAX_SCAN_CONNECTIONS, int(scan_connections)))
        self.fast_scan = bool(fast_scan)
        self._pool: list[Ps5DebugNgClient] = []  # extra scan connections, opened on demand
        self._pool_lock = threading.Lock()
        self._pool_failed_at: float | None = None  # last time an extra connection failed
        self._flags_refused = False  # the PS5 refused turbo request flags: send none
        self._lock = threading.RLock()  # freezer thread and UI share the socket
        # The PS5 dials back here for watchpoint hits. Tests set 0 (any free port) and
        # read the one bound from interrupt_bound.
        self.interrupt_port = INTERRUPT_PORT
        self.interrupt_bound = 0
        self._authed = False
        self._turbo: bool | None = None
        self._engines = 0
        self._resident: tuple[int, ValueType, int] | None = None  # pid, type, float flags
        self._resident_count = 0
        # Unknown search over several types: one console snapshot per type, each on its
        # own connection (NG keeps one per connection). This one first, then extras.
        self._multi: list[Ps5DebugNgClient] | None = None
        self._multi_vtype: ValueType | None = None  # this connection's type in _multi
        # PC-side candidates: one session, or one per (type, way) of a percent search
        self._scan: LocalScanSession | MultiScan | None = None
        self._scan_pid: int | None = None
        self._scan_ranges_list: list[tuple[int, int]] = []
        self._fg_supported = True

    @classmethod
    def connect(
        cls,
        host: str,
        port: int = 744,
        timeout: float = 8.0,
        scan_connections: int = SCAN_CONNECTIONS,
        fast_scan: bool = True,
    ) -> Ps5DebugNgClient:
        """Open the connection. Fails within ``timeout`` seconds (AC-1.2: under 10 s)."""
        return cls(cls._open(host, port, timeout), host, port, scan_connections, fast_scan)

    @staticmethod
    def _open(host: str, port: int, timeout: float = 8.0) -> socket.socket:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise ConsoleUnreachable(f"can't connect to {host}:{port}: {exc}") from exc
        sock.settimeout(min(timeout, IO_TIMEOUT))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock

    def _reconnect(self) -> None:
        """Open a fresh socket after the server dropped us (scan auth is per socket)."""
        self.close()
        self._pool_failed_at = None
        self._flags_refused = False  # a refusal was probably not about the flags
        self._w = _Wire(self._open(self.host, self.port))
        self._authed = False
        self._scan = None
        self._resident = None  # the console frees it when the socket closes
        self._close_multi()

    def close(self) -> None:
        self._close_pool()
        self._close_multi()
        with contextlib.suppress(OSError):
            self._w.sock.close()

    def _close_pool(self) -> None:
        with self._pool_lock:
            pool, self._pool = self._pool, []
        for w in pool:
            w.close()

    @contextlib.contextmanager
    def _timeout(self, seconds: float):
        old = self._w.sock.gettimeout()
        self._w.sock.settimeout(seconds)
        try:
            yield
        finally:
            self._w.sock.settimeout(old)

    # -- info ----------------------------------------------------------------
    def version(self) -> str:
        with self._lock:
            self._w.request(Cmd.VERSION)
            return self._w.recv(self._w.u32()).decode("ascii", "replace")

    def processes(self) -> list[ProcInfo]:
        with self._lock:
            self._w.request(Cmd.PROC_LIST)
            self._w.ok("process list")
            entries = []
            for _ in range(self._w.u32()):
                rec = self._w.recv(36)
                entries.append((_cstr(rec[:32]), struct.unpack("<i", rec[32:36])[0]))
            out = []
            for name, pid in entries:
                title_id = content_id = ""
                # PS5Debug-NG 1.3 answers process info for pid 0 (kernel) with an
                # error and then closes the connection, so never ask for it.
                if pid > 0:
                    self._w.request(Cmd.PROC_INFO, struct.pack("<I", pid))
                    if self._w.status() == Status.SUCCESS:
                        rec = self._w.recv(188)
                        title_id, content_id = _cstr(rec[108:124]), _cstr(rec[124:188])
                    else:
                        # Same server may drop us after any refused info call.
                        log.info("process info refused for pid %d; reconnecting", pid)
                        self._reconnect()
                out.append(ProcInfo(pid, name, title_id, content_id))
            return out

    def foreground_app(self) -> ForegroundApp | None:
        """Title + version of the running game. Not every PS5Debug-NG build has this
        command; some answer with an error, some drop the connection. Either way we
        stop asking and the session falls back to the process list."""
        if not self._fg_supported:
            return None
        with self._lock:
            try:
                self._w.request(Cmd.CONSOLE_FOREGROUND_APP)
                status = self._w.status()
            except ConnectionLost as exc:
                log.warning("foreground-app command dropped the connection (%s); reconnecting", exc)
                self._fg_supported = False
                self._reconnect()
                return None
            if status != Status.SUCCESS:
                log.info("foreground-app command not supported; using the process list")
                self._fg_supported = False
                return None
            rec = self._w.recv(140)
        pid = struct.unpack("<I", rec[:4])[0]
        title_id = _cstr(rec[4:20])
        if not title_id:
            return None
        return ForegroundApp(pid, title_id, _cstr(rec[84:124]), _cstr(rec[124:140]))

    def maps(self, pid: int) -> list[MemMap]:
        with self._lock:
            self._w.request(Cmd.PROC_MAPS, struct.pack("<I", pid))
            self._w.ok("memory map", ReadFailed)
            out = []
            for _ in range(self._w.u32()):
                rec = self._w.recv(58)
                start, end, _off, prot = struct.unpack("<QQQH", rec[32:58])
                out.append(MemMap(_cstr(rec[:32]), start, end, prot))
            return out

    # -- memory --------------------------------------------------------------
    def read(self, pid: int, addr: int, size: int) -> bytes:
        out = bytearray()
        with self._lock:
            while len(out) < size:
                n = min(MAX_BODY, size - len(out))
                self._w.request(Cmd.PROC_READ, struct.pack("<IQI", pid, addr + len(out), n))
                self._w.ok(f"read {addr + len(out):#x}", ReadFailed)
                out += self._w.recv(n)
        return bytes(out)

    def write(self, pid: int, addr: int, data: bytes) -> None:
        with self._lock:
            for i in range(0, len(data), MAX_BODY):
                part = data[i : i + MAX_BODY]
                # The server acks the 16-byte header, THEN reads the bytes to write
                # (sending them inside the request hung NG 1.3 in console test C3).
                self._w.request(Cmd.PROC_WRITE, struct.pack("<IQI", pid, addr + i, len(part)))
                self._w.ok(f"write {addr + i:#x}", WriteFailed)
                self._w.send(part)
                self._w.ok(f"write {addr + i:#x}", WriteFailed)

    def read_many(self, pid: int, spans: list[tuple[int, int]]) -> list[bytes]:
        """Many plain reads, pipelined: up to PIPELINE_DEPTH requests go out before the
        answers are read, so a batch costs about one round trip, not one per read.

        (PROC_SCAN_GET, NG's own bulk read, timed out on NG 1.3 in console test C3.)
        NG only refuses a read when it is out of memory, and then hangs up (proc.c
        returns 1), so the answers still queued can't be trusted: a refusal opens a
        fresh connection and raises ReadFailed."""
        out: list[bytes] = []
        bad: int | None = None
        with self._lock:
            for i in range(0, len(spans), PIPELINE_DEPTH):
                batch = spans[i : i + PIPELINE_DEPTH]
                self._w.last_cmd = Cmd.PROC_READ.name
                self._w.send(
                    b"".join(
                        struct.pack("<III", MAGIC, Cmd.PROC_READ, 16)
                        + struct.pack("<IQI", pid, a, n)
                        for a, n in batch
                    )
                )
                for a, n in batch:
                    if self._w.status() != Status.SUCCESS:
                        bad = a
                        break
                    out.append(self._w.recv(n))
                if bad is not None:
                    log.warning("action=read_many refused=%#x; reconnecting", bad)
                    self._reconnect()
                    raise ReadFailed(f"read {bad:#x}: console refused")
        return out

    # -- scanning ------------------------------------------------------------
    def _auth(self) -> None:
        if self._authed:
            return
        with self._lock:
            self._w.request(Cmd.PROC_AUTH, struct.pack("<II", AUTH_MAGIC, 2))
            self._w.ok("scan auth")
            n = self._w.u16()
            challenge = self._w.recv(n)
            ks = build_keystream()
            self._w.send(bytes(c ^ ks[i & 0xFF] for i, c in enumerate(challenge)))
            self._w.ok("scan auth answer")
        self._authed = True

    def _has_turbo(self) -> bool:
        if self._turbo is None:
            with self._lock:
                self._w.request(Cmd.TURBOSCAN_CAPS)
                self._turbo = self._w.status() == Status.SUCCESS
                if self._turbo:
                    ver, engines, threads, _r = struct.unpack("<IIII", self._w.recv(16))
                    self._engines = engines
                    log.info(
                        "turbo scan caps: version=%d engines=%#x max_threads=%d",
                        ver,
                        engines,
                        threads,
                    )
            log.info("turbo scan available: %s", self._turbo)
        return self._turbo

    def _start_flags(self, connections: int) -> int:
        """Turbo START flags for an exact/between scan run on ``connections`` sockets."""
        if self._flags_refused:
            return 0
        flags = 0
        if self.fast_scan and self._engines & TSE_ALIASING:
            flags |= TS_USE_ALIASING
        # NG: parallel compare is for single-connection clients only; with several
        # connections the server already uses one thread each (over-subscribed otherwise).
        if connections == 1 and self._engines & TSE_PARALLEL_COMPARE:
            flags |= TS_PARALLEL_COMPARE
        return flags

    def _scan_pool(self) -> list[Ps5DebugNgClient]:
        """This connection plus the extra scan connections (opened and authed once)."""
        want = self.scan_connections - 1
        with self._pool_lock:
            failed = self._pool_failed_at
            if failed is not None and time.monotonic() - failed < POOL_RETRY_S:
                want = len(self._pool)  # an extra connection failed lately: don't ask yet
            while len(self._pool) < want:
                try:
                    w = Ps5DebugNgClient(
                        self._open(self.host, self.port), self.host, self.port, 1, self.fast_scan
                    )
                    try:
                        w._auth()
                    except Exception:
                        w.close()
                        raise
                except Exception as exc:  # e.g. the PS5's 12 client slots are full
                    log.warning("action=scan_pool reason=%s; scanning on fewer connections", exc)
                    self._pool_failed_at = time.monotonic()
                    break
                w._turbo, w._engines = self._turbo, self._engines
                self._pool.append(w)
            return [self, *self._pool]

    def _drop_from_pool(self, w: Ps5DebugNgClient) -> None:
        with self._pool_lock:
            if w in self._pool:
                self._pool.remove(w)
        w.close()

    def _scan_slice(
        self,
        pid: int,
        start: int,
        length: int,
        vtype: ValueType,
        value: float | int | tuple[float, float],
        flags: int = 0,
    ) -> list[ScanHit]:
        """One console pass: exact (Fuzzy for decimals), or ValueBetween for a
        ``(low, high)`` tuple (AC-H2: typed decimals and scaled values)."""
        if isinstance(value, tuple):
            vbytes = vtype.encode(value[0]) + vtype.encode(value[1])
            compare = _NG_COMPARE[Compare.BETWEEN]
        else:
            vbytes = vtype.encode(value)
            compare = _NG_FUZZY if vtype.is_float else _NG_EXACT
        hdr = struct.pack(
            "<IQIBBBI",
            pid,
            start,
            length,
            _NG_VTYPE[vtype],
            compare,
            alignment_for(vtype),
            len(vbytes),
        )
        turbo = self._has_turbo()
        cmd = Cmd.TURBOSCAN_START if turbo else Cmd.PROC_SCAN_START
        if self._flags_refused:
            flags = 0
        body = hdr + (struct.pack("<I", flags) if turbo else b"")
        hits = []
        with self._lock, self._timeout(SCAN_TIMEOUT):
            self._w.request(cmd, body)
            if self._w.status() != Status.SUCCESS:
                if turbo and flags:
                    # Server refused the flags: stop asking and retry plainly.
                    log.warning("turbo scan refused flags=%#x; retrying without", flags)
                    self._flags_refused = True
                    return self._scan_slice(pid, start, length, vtype, value)
                raise ReadFailed(f"scan start at {start:#x}: console refused")
            self._w.send(vbytes)
            self._w.ok("scan", ReadFailed)
            # ValueBetween hits carry one value, not two
            rec = struct.Struct(f"<I{vtype.size}s")
            while (block_len := self._w.u64()) != SENTINEL_U64:
                block = self._w.recv(block_len)
                usable = len(block) - len(block) % rec.size
                hits.extend(
                    ScanHit(start + rel, raw)
                    for rel, raw in rec.iter_unpack(memoryview(block)[:usable])
                )
            self._w.ok("scan end", ReadFailed)
        if isinstance(value, tuple):
            hits = [h for h in hits if matches(vtype, Compare.BETWEEN, h.value, target=value)]
        elif vtype.is_float:
            # Same rule as Fuzzy (< 1.0); re-checked on the PC so both always agree.
            hits = [h for h in hits if matches(vtype, Compare.EXACT, h.value, target=value)]
        return hits

    def _pointer_slice(
        self, pid: int, start: int, length: int, low: int, high: int, keep
    ) -> list[tuple[int, int]]:
        """One console pass: 8-byte aligned values in [low, high] (NG ValueBetween)."""
        vbytes = struct.pack("<QQ", low, high)
        turbo = self._has_turbo()
        hdr = struct.pack(
            "<IQIBBBI", pid, start, length, _NG_VTYPE[ValueType.INT64], 4, 8, len(vbytes)
        )
        body = hdr + (struct.pack("<I", 0) if turbo else b"")
        out = []
        unpack = struct.Struct("<IQ").unpack_from
        with self._lock, self._timeout(SCAN_TIMEOUT):
            self._w.request(Cmd.TURBOSCAN_START if turbo else Cmd.PROC_SCAN_START, body)
            self._w.ok("pointer scan", ReadFailed)
            self._w.send(vbytes)
            self._w.ok("pointer scan", ReadFailed)
            while (block_len := self._w.u64()) != SENTINEL_U64:
                block = self._w.recv(block_len)
                for off in range(0, len(block) - 11, 12):
                    rel, value = unpack(block, off)
                    if keep(value):
                        out.append((start + rel, value))
            self._w.ok("pointer scan end", ReadFailed)
        return out

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
        self._auth()
        ranges = merge_ranges(regions)
        slices = [
            (s, min(SCAN_SLICE, end - s))
            for start, end in ranges
            for s in range(start, end, SCAN_SLICE)
        ]
        total = sum(n for _, n in slices) or 1
        done, out = 0, []
        t0 = time.monotonic()
        for s, n in slices:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("pointer scan cancelled")
            out.extend(self._pointer_slice(pid, s, n, low, high, keep))
            done += n
            if progress:
                progress(done / total)
        log.info(
            "action=pointer_pass low=%#x high=%#x hits=%d mib=%.0f seconds=%.1f",
            low,
            high,
            len(out),
            total / 2**20,
            time.monotonic() - t0,
        )
        return out

    def _scan_ranges(
        self,
        pid: int,
        ranges: list[tuple[int, int]],
        vtype: ValueType,
        value: float | int | tuple[float, float],
        progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> list[ScanHit]:
        """Exact (or ValueBetween) turbo scan over address ranges.

        Nearby ranges share a request (``plan_slices``), and the slices are shared out
        over several connections that scan at the same time (``scan_connections``).
        Matches come back in address order, only from inside ``ranges``."""
        self._auth()
        self._has_turbo()
        size, align = vtype.size, alignment_for(vtype)
        # NG reads unmapped gaps as zeros: a search that zeros match must not bridge
        # them, or every gap streams back a flood of matches that are thrown away.
        zero = bytes(size)
        if isinstance(value, tuple):
            zero_matches = matches(vtype, Compare.BETWEEN, zero, target=value)
        else:
            zero_matches = (
                abs(value) < 1
                if vtype.is_float
                else matches(vtype, Compare.EXACT, zero, target=value)
            )
        slices = plan_slices(ranges, size, align, bridge=0 if zero_matches else None)
        total = sum(n for _, n in slices) or 1
        conns = self._scan_pool() if len(slices) > 1 else [self]
        flags = self._start_flags(len(conns))
        results: dict[int, list[ScanHit]] = {}
        todo = list(range(len(slices)))[::-1]  # pop() takes the lowest address first
        lock = threading.Lock()
        done = [0]
        failure: list[BaseException] = []
        t0 = time.monotonic()

        def stop() -> bool:
            return bool(failure) or (cancel is not None and cancel.is_set())

        def work(cl: Ps5DebugNgClient) -> None:
            while True:
                with lock:
                    if stop() or not todo:
                        return
                    i = todo.pop()
                s, n = slices[i]
                try:
                    hits = cl._scan_slice(pid, s, n, vtype, value, flags)
                except BaseException as exc:
                    with lock:
                        if cl is self or isinstance(exc, ScanCancelled):
                            failure.append(exc)
                        else:  # an extra connection broke: this one takes its slice
                            log.warning("action=scan_conn_lost reason=%s; slice retried", exc)
                            todo.append(i)
                    if cl is not self:
                        self._drop_from_pool(cl)
                    return
                hits = inside(hits, ranges, size)
                with lock:
                    results[i] = hits
                    done[0] += n

        threads = [
            threading.Thread(target=work, args=(cl,), daemon=True, name="scan-conn") for cl in conns
        ]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            for t in threads:
                if t.is_alive():
                    t.join(0.1)  # waits even when the first worker has already finished
                    break
            if progress:
                progress(min(done[0] / total, 1.0))
        if failure:
            raise failure[0]
        if cancel is not None and cancel.is_set():
            raise ScanCancelled("scan cancelled by the player")
        if todo:  # every extra connection broke; finish here
            for i in sorted(todo):
                if cancel is not None and cancel.is_set():
                    raise ScanCancelled("scan cancelled by the player")
                s, n = slices[i]
                results[i] = inside(self._scan_slice(pid, s, n, vtype, value, flags), ranges, size)
                done[0] += n
                if progress:
                    progress(min(done[0] / total, 1.0))
        if progress:
            progress(1.0)
        log.info(
            "action=scan_stats requests=%d ranges=%d mib=%.0f seconds=%.1f turbo=%s "
            "flags=%#x connections=%d",
            len(slices),
            len(ranges),
            total / 2**20,
            time.monotonic() - t0,
            self._turbo,
            flags,
            len(conns),
        )
        return [h for i in range(len(slices)) for h in results[i]]

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
        self.scan_cancel()
        ranges = merge_ranges(regions)
        self._scan_pid = pid
        self._scan_ranges_list = ranges
        if ways:
            return self._parts_start(pid, ranges, vtype, cmp, value, ways, progress, cancel)
        if isinstance(vtype, tuple):
            if cmp is not Compare.UNKNOWN or value is not None:
                raise ValueError("several number types at once is for unknown searches only")
            if len(vtype) > 1:
                return self._multi_start(pid, ranges, vtype, progress, cancel)
            vtype = vtype[0]
        if cmp not in (Compare.EXACT, Compare.BETWEEN) or value is None:
            return self._snapshot_start(pid, ranges, vtype, progress, cancel)
        hits = self._scan_ranges(pid, ranges, vtype, value, progress, cancel)
        self._scan = self._session(pid)
        self._scan.set_results(vtype, hits)
        return len(hits)

    def _session(self, pid: int) -> LocalScanSession:
        # PROC_SCAN_GET (bulk read) timed out on PS5Debug-NG 1.3 (console test C3), so
        # candidates are re-checked with plain reads, pipelined (read_many).
        return LocalScanSession(
            lambda a, n: self.read(pid, a, n), lambda spans: self.read_many(pid, spans)
        )

    def _parts_start(
        self,
        pid: int,
        ranges: list[tuple[int, int]],
        vtype: ValueType | tuple[ValueType, ...],
        cmp: Compare,
        value,
        ways: tuple[Scale, ...],
        progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> int:
        """Percent search: one console ValueBetween pass per (type, way) part (NG has
        one range per compare); the candidates then live on the PC."""
        check_ways(cmp, value)
        parts = way_parts(vtype, ways)
        sessions: list[LocalScanSession] = []
        for i, (vt, way) in enumerate(parts):

            def mine(p: float, i: int = i) -> None:
                if progress:
                    progress((i + p) / len(parts))

            v = part_value(vt, way, cmp, value)
            if v is None:
                continue
            hits = self._scan_ranges(pid, ranges, vt, v, mine, cancel)
            s = self._session(pid)
            s.set_results(vt, hits, way)
            sessions.append(s)
            log.info("action=scan_part type=%s way=%s count=%d", vt.name, way.label, len(hits))
        if not sessions:
            raise ValueError("no number type can hold that percent")
        one_way_each(sessions)
        drop_empty(sessions, lambda s: len(s.hits))
        self._scan = MultiScan(sessions)
        if progress:
            progress(1.0)
        return len(self._scan.hits)

    # -- unknown value: snapshot kept on the console ---------------------------
    def _snapshot_start(
        self,
        pid: int,
        ranges: list[tuple[int, int]],
        vtype: ValueType,
        progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> int:
        need = TSE_SNAPSHOT | TSE_SNAPSHOT_SEGMENTS
        if not self._has_turbo() or self._engines & need != need:
            raise ScanNotSupported(
                "This PS5Debug-NG can't search without a number. Update it to 1.3.2 or later."
            )
        self._auth()
        segs = [
            (s, min(SNAP_SEGMENT_MAX, end - s))
            for start, end in ranges
            for s in range(start, end, SNAP_SEGMENT_MAX)
        ]
        float_flags = TS_FLOAT_SIMPLE if vtype.is_float and self._engines & TSE_FLOAT_POLICY else 0
        flags = TS_SNAPSHOT | TS_SNAPSHOT_SEGMENTS | float_flags
        hdr = struct.pack(
            "<IQIBBBII", pid, 0, 0, _NG_VTYPE[vtype], _NG_UNKNOWN, alignment_for(vtype), 0, flags
        )
        t0 = time.monotonic()
        cancelled = False
        with self._lock, self._timeout(SCAN_TIMEOUT):
            self._w.request(Cmd.TURBOSCAN_START, hdr)
            self._w.ok("snapshot start", ReadFailed)
            self._w.ok("snapshot start", ReadFailed)  # no seed value for unknown
            self._w.send(
                struct.pack("<I", len(segs)) + b"".join(struct.pack("<QI", a, n) for a, n in segs)
            )
            _slots, total = struct.unpack("<QQ", self._w.recv(16))
            while (done := self._w.u64()) != SENTINEL_U64:
                if progress and total:
                    progress(min(done / total, 1.0))
                if cancel is not None and cancel.is_set() and not cancelled:
                    cancelled = self._send_cancel(pid)
            ok, count = struct.unpack("<IQ", self._w.recv(12))
            self._w.ok("snapshot end", ReadFailed)
        log.info(
            "action=snapshot type=%s ok=%d count=%d segments=%d mib=%.0f seconds=%.1f flags=%#x",
            vtype.name,
            ok,
            count,
            len(segs),
            sum(n for _, n in segs) / 2**20,
            time.monotonic() - t0,
            flags,
        )
        if cancelled or (cancel is not None and cancel.is_set()):
            self._end_resident()
            raise ScanCancelled("scan cancelled by the player")
        if not ok:
            raise ScanNotSupported(
                "The PS5 didn't have room to remember the game's memory. Try a number search, "
                "or free up space on the PS5."
            )
        if progress:
            progress(1.0)
        self._resident = (pid, vtype, float_flags)
        self._resident_count = count
        return count

    def _resident_next(
        self,
        cmp: Compare,
        value: float | int | None,
        progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> int:
        assert self._resident is not None
        pid, vtype, float_flags = self._resident
        code = _NG_COMPARE[cmp]
        if cmp is Compare.EXACT and vtype.is_float:
            code = _NG_FUZZY
        if cmp is Compare.BETWEEN:
            lo, hi = value_for(vtype, cmp, value) or value  # whole numbers: clamp to the type
            val = vtype.encode(lo) + vtype.encode(hi)
        elif cmp.by_amount:
            amount, _tol = value  # type: ignore[misc]  # the server applies its own rule
            val = vtype.encode(amount)
        else:
            val = vtype.encode(value) if cmp.needs_value and value is not None else b""
        total = max(self._resident_count, 1)
        cancelled = False
        rescan = TS_RESCAN_ALIASING if self._rescan_aliasing() else 0
        t0 = time.monotonic()
        with self._lock, self._timeout(SCAN_TIMEOUT):
            self._w.request(
                Cmd.TURBOSCAN_COUNT,
                struct.pack(
                    "<IQBBII",
                    pid,
                    0,
                    _NG_VTYPE[vtype],
                    code,
                    len(val),
                    TS_SERVER_RESIDENT | float_flags | rescan,
                ),
            )
            self._w.ok("narrow down", ReadFailed)
            if val:
                self._w.send(val)
            while (done := self._w.u64()) != SENTINEL_U64:
                if progress:
                    progress(min(done / total, 1.0))
                if cancel is not None and cancel.is_set() and not cancelled:
                    cancelled = self._send_cancel(pid)
            count = self._w.u64()
            self._w.ok("narrow down end", ReadFailed)
        log.info(
            "action=resident_next cmp=%s count=%d seconds=%.1f flags=%#x",
            cmp.value,
            count,
            time.monotonic() - t0,
            rescan,
        )
        if cancelled:
            self._end_resident()
            raise ScanCancelled("scan cancelled; start a new search")
        self._resident_count = count
        return count

    def _rescan_aliasing(self) -> bool:
        return (
            self.fast_scan and not self._flags_refused and bool(self._engines & TSE_RESCAN_ALIASING)
        )

    def _resident_results(self, limit: int) -> list[ScanHit]:
        assert self._resident is not None
        _pid, vtype, _ = self._resident
        n = vtype.size
        with self._lock:
            self._w.request(Cmd.TURBOSCAN_GET, struct.pack("<III", 0, limit, 0))
            self._w.ok("get matches", ReadFailed)
            hdr = self._w.u32()
            count, stride = hdr & 0x7FFFFFFF, 8 + n * (3 if hdr & 0x80000000 else 2)
            data = self._w.recv(count * stride) if count else b""
            self._w.ok("get matches end", ReadFailed)
        return [
            ScanHit(struct.unpack_from("<Q", data, i)[0], data[i + 8 : i + 8 + n])
            for i in range(0, len(data), stride)
        ]

    def _send_cancel(self, pid: int) -> bool:
        """Cancel has to arrive on a second connection: this one is busy streaming."""
        try:
            other = Ps5DebugNgClient.connect(self.host, self.port)
            try:
                other._auth()
                other._w.request(Cmd.TURBOSCAN_CANCEL, struct.pack("<I", pid))
                other._w.status()
            finally:
                other.close()
        except Exception as exc:  # best effort: the scan then just runs to the end
            log.warning("couldn't cancel the scan on the PS5: %s", exc)
        return True

    # -- unknown value over several number types --------------------------------
    def _multi_start(
        self,
        pid: int,
        ranges: list[tuple[int, int]],
        vtypes: tuple[ValueType, ...],
        progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> int:
        """One snapshot per type, created at the same time on separate connections."""
        self._has_turbo()
        conns: list[Ps5DebugNgClient] = [self]
        for _ in vtypes[1:]:
            try:
                # In-place rescans stay on the main connection only (NG: not on many
                # connections at once).
                c = Ps5DebugNgClient(
                    self._open(self.host, self.port), self.host, self.port, 1, False
                )
                c._turbo, c._engines = self._turbo, self._engines
                try:
                    c._auth()
                except Exception:
                    c.close()
                    raise
            except Exception as exc:  # no free connection: search the first types only
                log.warning("action=multi_type_conn reason=%s; fewer types searched", exc)
                break
            conns.append(c)
        types = vtypes[: len(conns)]
        for c, vt in zip(conns, types, strict=True):
            c._multi_vtype = vt
        done = [0.0] * len(conns)
        failure: list[BaseException | None] = [None] * len(conns)

        def run(i: int) -> None:
            def mine(p: float) -> None:
                done[i] = p

            try:
                conns[i]._snapshot_start(pid, ranges, types[i], mine, cancel)
            except BaseException as exc:
                failure[i] = exc

        self._multi = conns
        self._wait_all([lambda i=i: run(i) for i in range(len(conns))], done, progress)
        ok = [c for c, f in zip(conns, failure, strict=True) if f is None]
        hard = [f for f in failure if f is not None and not isinstance(f, ScanNotSupported)]
        if hard or not ok:
            # A cancel or lost link ends the search; so does every type failing.
            self._close_multi()
            raise next(f for f in [*hard, *failure] if f is not None)
        for c, f in zip(conns, failure, strict=True):
            if f is not None:  # e.g. no room on the PS5 for this type's snapshot
                log.warning("action=multi_type_skip reason=%s; searching the others", f)
                c._end_resident()
                if c is not self:
                    c.close()
        conns[:] = ok
        if progress:
            progress(1.0)
        counts = {c._resident[1].name: c._resident_count for c in conns if c._resident}
        log.info("action=multi_type_snapshot counts=%s", counts)
        return sum(c._resident_count for c in conns)

    @staticmethod
    def _wait_all(jobs: list, done: list[float], progress: ProgressFn | None) -> None:
        """Run ``jobs`` on their own threads; report the mean of ``done`` meanwhile."""
        threads = [threading.Thread(target=j, daemon=True, name="scan-type") for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(0.1)
                if progress:
                    progress(min(sum(done) / len(done), 1.0))

    def _multi_next(
        self,
        cmp: Compare,
        value,
        progress: ProgressFn | None,
        cancel: threading.Event | None,
        per_type: Callable[[ValueType], object] | None = None,
    ) -> int:
        """Narrow every type's snapshot at once. ``per_type(vtype)``: the value for that
        type, when it isn't ``value`` made to fit it."""
        conns = self._multi
        assert conns is not None
        done = [0.0] * len(conns)
        failure: list[BaseException] = []

        def run(i: int) -> None:
            c = conns[i]
            try:
                if c._resident is None:
                    done[i] = 1.0
                    return  # this type was emptied earlier
                vt = c._resident[1]
                if per_type is not None:
                    v = per_type(vt)
                else:
                    v = value_for(vt, cmp, value) if value is not None else None
                if value is not None and v is None:
                    c._resident_count = 0  # no number of this type can match:
                    c._end_resident()  # free it so nothing stale is listed later
                else:
                    c._resident_next(cmp, v, lambda p: done.__setitem__(i, p), cancel)
            except BaseException as exc:
                failure.append(exc)
            done[i] = 1.0

        self._wait_all([lambda i=i: run(i) for i in range(len(conns))], done, progress)
        if failure:
            self._close_multi()
            raise failure[0]
        for c in drop_empty(conns, lambda c: c._resident_count if c._resident else 0):
            c._end_resident()
            if c is not self:
                c.close()
        return sum(c._resident_count for c in conns if c._resident)

    def _close_multi(self) -> None:
        conns, self._multi = self._multi, None
        for c in conns or []:
            if c is not self:
                c._end_resident()
                c.close()
        if conns:
            self._end_resident()

    def scan_counts(self) -> dict[ValueType, int]:
        if self._multi is not None:
            return {
                c._multi_vtype: c._resident_count if c._resident else 0
                for c in self._multi
                if c._multi_vtype is not None
            }
        if self._resident is not None:
            return {self._resident[1]: self._resident_count}
        if isinstance(self._scan, MultiScan):
            return self._scan.counts()
        if self._scan is not None and self._scan.vtype is not None:
            return {self._scan.vtype: len(self._scan.hits)}
        return {}

    def scan_way_counts(self) -> dict[tuple[ValueType, Scale | None], int]:
        if isinstance(self._scan, MultiScan):
            return self._scan.way_counts()
        if isinstance(self._scan, LocalScanSession) and self._scan.vtype is not None:
            return {(self._scan.vtype, self._scan.way): len(self._scan.hits)}
        return {(t, None): n for t, n in self.scan_counts().items()}

    def scan_restrict(self, hits: list[ScanHit]) -> int:
        """Keep just ``hits`` on the PC: frees the console's snapshots; next scans read
        the candidates (pipelined)."""
        if self._scan_pid is None:
            raise ValueError("no search to narrow down; start a new search")
        pid = self._scan_pid
        fallback = next(iter(self.scan_counts()), None)
        scan = restrict(hits, lambda: self._session(pid), fallback)  # may raise: nothing freed
        self._close_multi()
        self._end_resident()
        self._scan = scan
        return len(scan.hits)

    def _end_resident(self) -> None:
        if self._resident is None:
            return
        self._resident = None
        with contextlib.suppress(Exception), self._lock:
            self._w.request(Cmd.TURBOSCAN_END)
            self._w.status()

    def scan_next(
        self,
        cmp: Compare,
        value: float | int | None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """Narrow down. Snapshot search: compare on the console. Few candidates: read
        them (pipelined). Many: for an exact number, scan again on the console over
        windows around the candidates and keep the overlap."""
        if self._multi is not None:
            return self._multi_next(cmp, value, progress, cancel)
        if self._resident is not None:
            return self._resident_next(cmp, value, progress, cancel)
        scan = self._scan
        if isinstance(scan, MultiScan) and self._scan_pid is not None:
            return scan.next(
                cmp, value, progress, cancel, narrow=self._narrowed, check=self._check_narrow
            )
        if scan is None or scan.vtype is None or self._scan_pid is None:
            raise ValueError("no scan to narrow down; start a new search")
        self._check_narrow(scan, cmp, value)
        hits = self._narrowed(scan, cmp, value, progress, cancel)
        if self._rescans(scan):  # the next window search only needs these windows
            self._scan_ranges_list = candidate_windows(
                self._scan_ranges_list, sorted(h.address for h in scan.hits), scan.vtype.size
            )
        scan.hits = hits
        return len(hits)

    def scan_rebase(
        self,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """Keep every match with its value now as the baseline. Snapshots: a range
        compare over every number the type holds (decimals from -inf to inf; NG has no
        "keep all" compare, so a decimal that is NaN now is dropped). Candidates on the
        PC: read them. Too many to read: refused, before anything changes (re-scanning
        windows for every value would send back every address in them)."""
        if self._multi is not None:
            return self._multi_next(Compare.BETWEEN, KEEP_EVERY, progress, cancel, _every)
        if self._resident is not None:
            return self._resident_next(Compare.BETWEEN, _every(self._resident[1]), progress, cancel)
        scan = self._scan
        if scan is None or self._scan_pid is None:
            raise ValueError("no scan to narrow down; start a new search")
        parts = scan.sessions if isinstance(scan, MultiScan) else [scan]
        n = sum(len(s.hits) for s in parts)
        if any(self._rescans(s) for s in parts):
            raise ValueError(
                f"{n:,} matches is too many to note a percent. Make it go up or down, "
                "then type the % again."
            )
        if isinstance(scan, MultiScan):
            return scan.rebase(progress, cancel)
        scan.hits = scan.rebased(progress, cancel)
        return len(scan.hits)

    @staticmethod
    def _rescans(scan: LocalScanSession) -> bool:
        """Too many candidates to read: narrowing re-scans windows on the console."""
        assert scan.vtype is not None
        return len(read_spans([h.address for h in scan.hits], scan.vtype.size)) > MAX_READ_SPANS

    def _check_narrow(self, scan: LocalScanSession, cmp: Compare, value) -> None:
        """Raise before anything changes when ``scan`` can't be narrowed this way."""
        if self._rescans(scan) and (cmp not in (Compare.EXACT, Compare.BETWEEN) or value is None):
            raise ValueError(
                f"{len(scan.hits):,} matches is too many to compare. Type the number you "
                "see now to narrow it down first."
            )

    def _narrowed(
        self,
        scan: LocalScanSession,
        cmp: Compare,
        value,
        progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> list[ScanHit]:
        """One PC-side session's matches after this compare, not kept yet: read few
        candidates, or re-scan windows around many on the console (number or range)."""
        assert scan.vtype is not None and self._scan_pid is not None
        if not self._rescans(scan):
            return scan.narrowed(cmp, value, progress, cancel)
        self._check_narrow(scan, cmp, value)
        addrs = {h.address for h in scan.hits}
        windows = candidate_windows(self._scan_ranges_list, sorted(addrs), scan.vtype.size)
        new = self._scan_ranges(self._scan_pid, windows, scan.vtype, value, progress, cancel)
        return [ScanHit(h.address, h.value) for h in new if h.address in addrs]

    def scan_results(self, limit: int) -> list[ScanHit]:
        if self._multi is not None:
            return merge_results(
                [(c._resident[1], c._resident_results(limit)) for c in self._multi if c._resident],
                limit,
            )
        if self._resident is not None:
            return self._resident_results(limit)
        return [] if self._scan is None else self._scan.results(limit)

    def scan_cancel(self) -> None:
        self._close_multi()
        self._end_resident()
        self._scan = None
        self._scan_pid = None
        self._scan_ranges_list = []

    # -- watchpoints ("Lock it", "Find it next time") -------------------------
    def watch_writes(
        self,
        pid: int,
        addr: int,
        size: int,
        timeout_s: float,
        *,
        until: WatchUntil | None = None,
    ) -> list[int]:
        return [h.rip for h in self.watch(pid, addr, size, "write", timeout_s, until=until)]

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
        """Hardware watchpoint (DR0). The PS5 dials back to us on port 755 with one
        0x4A0-byte packet per trap. ``access`` (breaktype 3, read-write) is from
        PROTOCOL.md §7.3 and is UNVERIFIED on the console until test C10a.
        ``execute`` (breaktype 0, length 1 byte) traps before the instruction; whether
        NG sets RF on continue (or the same breakpoint fires again at once) is
        UNVERIFIED until test C20d.
        ``until`` is asked after each hit and each idle half second."""
        if size not in _DR7_LEN:
            raise ValueError(f"can't watch {size} bytes")
        if kind not in _DR7_KIND:
            raise ValueError(f"unknown watch kind {kind!r}")
        if kind == "execute" and size != 1:
            raise ValueError("an execute breakpoint covers 1 byte")
        hits: queue.Queue[WatchHit] = queue.Queue()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", self.interrupt_port))
        self.interrupt_bound = listener.getsockname()[1]
        listener.listen(1)
        listener.settimeout(timeout_s)
        stop = threading.Event()
        t = threading.Thread(target=self._interrupt_loop, args=(listener, hits, stop), daemon=True)
        t.start()
        found: list[WatchHit] = []
        try:
            with self._lock:
                with self._timeout(ATTACH_TIMEOUT):
                    self._w.request(Cmd.DEBUG_ATTACH, struct.pack("<I", pid))
                    status = self._w.status()
                if status != Status.SUCCESS:
                    raise ReadFailed(
                        "The PS5 wouldn't attach the debugger to the game. If a previous "
                        "Lock it was cut off, restart the game and try again."
                    )
                # 24 bytes: index, enabled, length, breaktype (u32 each), address (u64).
                # A 22-byte packet made NG 1.3 close the connection (C4).
                self._w.request(
                    Cmd.DEBUG_SET_WATCHPOINT,
                    struct.pack("<IIIIQ", 0, 1, _DR7_LEN[size], _DR7_KIND[kind], addr),
                )
                self._w.ok("set watchpoint")
            started = time.monotonic()
            deadline = started + timeout_s
            while len(found) < MAX_WATCH_HITS and (left := deadline - time.monotonic()) > 0:
                try:
                    found.append(hits.get(timeout=min(left, 0.5)))
                except queue.Empty:
                    if until is not None and until(list(found), time.monotonic() - started):
                        break
                    continue
                with self._lock:  # the game is stopped at the trap; let it run on
                    self._w.request(Cmd.DEBUG_CONTINUE, struct.pack("<I", 0))
                    self._w.ok("continue")
                if until is not None and until(list(found), time.monotonic() - started):
                    break
        finally:
            stop.set()
            try:
                with self._lock:
                    self._w.request(
                        Cmd.DEBUG_SET_WATCHPOINT,
                        struct.pack("<IIIIQ", 0, 0, 0, 0, 0),
                    )
                    self._w.status()
                    self._w.request(Cmd.DEBUG_DETACH)
                    self._w.status()
            finally:
                listener.close()
                t.join(timeout=2)
        return found

    @staticmethod
    def _interrupt_loop(
        listener: socket.socket, hits: queue.Queue[WatchHit], stop: threading.Event
    ) -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        conn.settimeout(0.5)
        buf = b""
        with conn:
            while not stop.is_set():
                try:
                    chunk = conn.recv(INTERRUPT_SIZE - len(buf))
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                if len(buf) == INTERRUPT_SIZE:
                    hits.put(parse_interrupt(buf))
                    buf = b""
