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
from cheatmaker.core.values import Compare, ValueType
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
AUTH_MAGIC = 0xBB40E64D
INTERRUPT_PORT = 755
INTERRUPT_SIZE = 0x4A0
RIP_OFFSET = 0x30 + 15 * 8  # struct reg: r15..rax (15 regs) then rip


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
                self._w.request(
                    Cmd.PROC_WRITE, struct.pack("<IQI", pid, addr + i, len(part)) + part
                )
                self._w.ok(f"write {addr + i:#x}", WriteFailed)
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
        hdr = struct.pack(
            "<IQIBBBI",
            pid,
            start,
            length,
            _NG_VTYPE[vtype],
            _NG_EXACT,
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
        if cmp is not Compare.EXACT or value is None:
            # Unknown-value first scans must stay on the console (architecture Q3);
            # waiting on console test S2 and an architect decision.
            raise ScanNotSupported("unknown-value search isn't available on the PS5 yet")
        ranges = merge_ranges(regions)
        hits = self._scan_ranges(pid, ranges, vtype, value, progress, cancel)
        # PROC_SCAN_GET (bulk read) timed out on PS5Debug-NG 1.3 (console test C3),
        # so only plain reads are used for re-checking candidates.
        self._scan = LocalScanSession(lambda a, n: self.read(pid, a, n))
        self._scan.set_results(vtype, hits)
        self._scan_pid = pid
        self._scan_ranges_list = ranges
        return len(hits)

    def scan_next(
        self,
        cmp: Compare,
        value: float | int | None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """Narrow down. Few candidates: read them. Many: for an exact number, scan
        again on the console over the ranges that still hold candidates and keep the
        overlap (one read per candidate is ~36 ms on the PS5, far too slow)."""
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
        return [] if self._scan is None else self._scan.results(limit)

    def scan_cancel(self) -> None:
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
                self._w.request(Cmd.DEBUG_ATTACH, struct.pack("<I", pid))
                self._w.ok("debug attach")
                self._w.request(
                    Cmd.DEBUG_SET_WATCHPOINT,
                    struct.pack("<IIIHQ", 0, 1, _DR7_LEN[size], _DR7_WRITE, addr),
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
                        struct.pack("<IIIHQ", 0, 0, 0, 0, 0),
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
