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
import queue
import socket
import struct
import threading
import time
from enum import IntEnum

from cheatmaker.console.client import ForegroundApp, MemMap, ProcInfo, ProgressFn, ScanHit
from cheatmaker.console.local_scan import LocalScanSession, alignment_for
from cheatmaker.core.values import Compare, ValueType, matches
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
IO_TIMEOUT = 4.0  # short, so a dropped PS5 is noticed within 5 s while freezing (AC-1.4)
SCAN_TIMEOUT = 60.0  # one scan slice runs on the console and may take a while
SENTINEL_U64 = 0xFFFFFFFFFFFFFFFF
TSE_PARALLEL_COMPARE = 0x100  # turbo engine bit: server can compare on several threads
TS_PARALLEL_COMPARE = 0x80  # turbo request flag asking for that
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
RIP_OFFSET = 0x30 + 136
"""FreeBSD amd64 struct reg: r15..rax (15 x 8 = 120 bytes), then trapno/fs/gs/err/es/ds
(16 bytes), then rip. reg64 starts at 0x30 in the interrupt packet."""
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
}
_NG_FUZZY = 1
"""FuzzyValue: |typed - memory| < 1.0, float/double only. Legacy ExactValue on floats
is a near-bit-exact match, so typing 80 misses health that is really 79.6 (C3)."""
_DR7_LEN = {1: 0, 2: 1, 4: 3, 8: 2}
_DR7_WRITE = 1


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


MAX_READ_SPANS = 400
"""Above this many reads, narrowing down re-scans on the console instead."""


def read_spans(addrs: list[int], size: int) -> list[tuple[int, int]]:
    from cheatmaker.console.local_scan import _batched_reads

    return list(_batched_reads(sorted(addrs), size))


def ranges_with(ranges: list[tuple[int, int]], sorted_addrs: list[int]) -> list[tuple[int, int]]:
    """The ranges that contain at least one of the (sorted) addresses."""
    import bisect

    starts = [s for s, _ in ranges]
    keep: dict[int, tuple[int, int]] = {}
    for a in sorted_addrs:
        i = bisect.bisect_right(starts, a) - 1
        if i >= 0 and ranges[i][0] <= a < ranges[i][1]:
            keep[i] = ranges[i]
    return [keep[i] for i in sorted(keep)]


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
    def __init__(self, sock: socket.socket, host: str, port: int = 744) -> None:
        self._w = _Wire(sock)
        self.host, self.port = host, port
        self._lock = threading.RLock()  # freezer thread and UI share the socket
        self._authed = False
        self._turbo: bool | None = None
        self._turbo_flags = 0
        self._engines = 0
        self._resident: tuple[int, ValueType, int] | None = None  # pid, type, float flags
        self._resident_count = 0
        self._scan: LocalScanSession | None = None
        self._scan_pid: int | None = None
        self._scan_ranges_list: list[tuple[int, int]] = []
        self._fg_supported = True

    @classmethod
    def connect(cls, host: str, port: int = 744, timeout: float = 8.0) -> Ps5DebugNgClient:
        """Open the connection. Fails within ``timeout`` seconds (AC-1.2: under 10 s)."""
        return cls(cls._open(host, port, timeout), host, port)

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
        self._w = _Wire(self._open(self.host, self.port))
        self._authed = False
        self._scan = None
        self._resident = None  # the console frees it when the socket closes

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._w.sock.close()

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
        """Bulk fetch with CMD_PROC_SCAN_GET (one round trip for many spans)."""
        self._auth()
        with self._lock, self._timeout(SCAN_TIMEOUT):
            body = struct.pack("<II", pid, len(spans)) + b"".join(
                struct.pack("<QI", a, n) for a, n in spans
            )
            self._w.request(Cmd.PROC_SCAN_GET, body)
            self._w.ok("bulk read", ReadFailed)
            out = [self._w.recv(n) for _, n in spans]
            if self._w.u64() != SENTINEL_U64:
                raise ProtocolError("bulk read: missing end marker")
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
                    if engines & TSE_PARALLEL_COMPARE:
                        self._turbo_flags = TS_PARALLEL_COMPARE
                    log.info(
                        "turbo scan caps: version=%d engines=%#x max_threads=%d",
                        ver,
                        engines,
                        threads,
                    )
            log.info("turbo scan available: %s flags=%#x", self._turbo, self._turbo_flags)
        return self._turbo

    def _scan_slice(
        self, pid: int, start: int, length: int, vtype: ValueType, value: float | int
    ) -> list[ScanHit]:
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
        flags = self._turbo_flags
        body = hdr + (struct.pack("<I", flags) if turbo else b"")
        hits = []
        with self._lock, self._timeout(SCAN_TIMEOUT):
            self._w.request(cmd, body)
            if self._w.status() != Status.SUCCESS:
                if flags:
                    # Server refused the parallel flag: stop asking and retry plainly.
                    log.warning("turbo scan refused flags=%#x; retrying without", flags)
                    self._turbo_flags = 0
                    return self._scan_slice(pid, start, length, vtype, value)
                raise ReadFailed(f"scan start at {start:#x}: console refused")
            self._w.send(vbytes)
            self._w.ok("scan", ReadFailed)
            stride = 4 + vtype.size
            while (block_len := self._w.u64()) != SENTINEL_U64:
                block = self._w.recv(block_len)
                for off in range(0, len(block) - stride + 1, stride):
                    rel = struct.unpack_from("<I", block, off)[0]
                    hits.append(ScanHit(start + rel, block[off + 4 : off + stride]))
            self._w.ok("scan end", ReadFailed)
        if vtype.is_float:
            # Fuzzy is +-1.0; keep the app's own +-0.5 rounding rule so PC and PS5 agree.
            hits = [h for h in hits if matches(vtype, Compare.EXACT, h.value, target=value)]
        return hits

    def _scan_ranges(
        self,
        pid: int,
        ranges: list[tuple[int, int]],
        vtype: ValueType,
        value: float | int,
        progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> list[ScanHit]:
        """Exact-value turbo scan over address ranges, in slices for progress/Cancel."""
        self._auth()
        slices = [
            (s, min(SCAN_SLICE, end - s))
            for start, end in ranges
            for s in range(start, end, SCAN_SLICE)
        ]
        total = sum(n for _, n in slices) or 1
        done, hits = 0, []
        t0 = time.monotonic()
        for s, n in slices:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("scan cancelled by the player")
            hits.extend(self._scan_slice(pid, s, n, vtype, value))
            done += n
            if progress:
                progress(done / total)
        log.info(
            "action=scan_stats requests=%d ranges=%d mib=%.0f seconds=%.1f turbo=%s flags=%#x",
            len(slices),
            len(ranges),
            total / 2**20,
            time.monotonic() - t0,
            self._turbo,
            self._turbo_flags,
        )
        return hits

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
        self.scan_cancel()
        ranges = merge_ranges(regions)
        if cmp is not Compare.EXACT or value is None:
            return self._snapshot_start(pid, ranges, vtype, progress, cancel)
        hits = self._scan_ranges(pid, ranges, vtype, value, progress, cancel)
        # PROC_SCAN_GET (bulk read) timed out on PS5Debug-NG 1.3 (console test C3),
        # so only plain reads are used for re-checking candidates.
        self._scan = LocalScanSession(lambda a, n: self.read(pid, a, n))
        self._scan.set_results(vtype, hits)
        self._scan_pid = pid
        self._scan_ranges_list = ranges
        return len(hits)

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
        val = vtype.encode(value) if cmp.needs_value and value is not None else b""
        total = max(self._resident_count, 1)
        cancelled = False
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
                    TS_SERVER_RESIDENT | float_flags,
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
        log.info("action=resident_next cmp=%s count=%d", cmp.value, count)
        if cancelled:
            self._end_resident()
            raise ScanCancelled("scan cancelled; start a new search")
        self._resident_count = count
        return count

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
        them. Many: for an exact number, scan again on the console over the ranges
        that still hold candidates and keep the overlap (one read per candidate is
        ~36 ms on the PS5, far too slow)."""
        if self._resident is not None:
            return self._resident_next(cmp, value, progress, cancel)
        scan = self._scan
        if scan is None or scan.vtype is None or self._scan_pid is None:
            raise ValueError("no scan to narrow down; start a new search")
        spans = read_spans([h.address for h in scan.hits], scan.vtype.size)
        if len(spans) <= MAX_READ_SPANS:
            return scan.next(cmp, value, progress, cancel)
        if cmp is not Compare.EXACT or value is None:
            raise ValueError(
                f"{len(scan.hits):,} matches is too many to compare. Type the number you "
                "see now to narrow it down first."
            )
        addrs = {h.address for h in scan.hits}
        ranges = ranges_with(self._scan_ranges_list, sorted(addrs))
        new = self._scan_ranges(self._scan_pid, ranges, scan.vtype, value, progress, cancel)
        scan.set_results(scan.vtype, [h for h in new if h.address in addrs])
        self._scan_ranges_list = ranges
        return len(scan.hits)

    def scan_results(self, limit: int) -> list[ScanHit]:
        if self._resident is not None:
            return self._resident_results(limit)
        return [] if self._scan is None else self._scan.results(limit)

    def scan_cancel(self) -> None:
        self._end_resident()
        self._scan = None
        self._scan_pid = None

    # -- watchpoint ("Lock it") ---------------------------------------------
    def watch_writes(self, pid: int, addr: int, size: int, timeout_s: float) -> list[int]:
        """Hardware write watchpoint (DR0). The PS5 dials back to us on port 755."""
        if size not in _DR7_LEN:
            raise ValueError(f"can't watch {size} bytes")
        rips: queue.Queue[int] = queue.Queue()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", INTERRUPT_PORT))
        listener.listen(1)
        listener.settimeout(timeout_s)
        stop = threading.Event()
        t = threading.Thread(target=self._interrupt_loop, args=(listener, rips, stop), daemon=True)
        t.start()
        found: list[int] = []
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
                    struct.pack("<IIIIQ", 0, 1, _DR7_LEN[size], _DR7_WRITE, addr),
                )
                self._w.ok("set watchpoint")
            deadline = time.monotonic() + timeout_s
            while len(found) < 16 and (left := deadline - time.monotonic()) > 0:
                try:
                    found.append(rips.get(timeout=min(left, 0.5)))
                except queue.Empty:
                    continue
                with self._lock:  # the game is stopped at the trap; let it run on
                    self._w.request(Cmd.DEBUG_CONTINUE, struct.pack("<I", 0))
                    self._w.ok("continue")
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
        listener: socket.socket, rips: queue.Queue[int], stop: threading.Event
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
                    rips.put(struct.unpack_from("<Q", buf, RIP_OFFSET)[0])
                    buf = b""
