"""Ps5DebugNgClient against a local fake server that speaks the documented wire format.

This proves our framing matches our reading of PROTOCOL.md; console tests S1-S3
prove it matches the real PS5Debug-NG.
"""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from cheatmaker.console.client import ProcInfo
from cheatmaker.console.ps5debug_ng import (
    INTERRUPT_SIZE,
    MAGIC,
    RIP_OFFSET,
    SENTINEL_U64,
    Cmd,
    Ps5DebugNgClient,
    Status,
    build_keystream,
    merge_ranges,
)
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.errors import (
    ConnectionLost,
    ConsoleUnreachable,
    ReadFailed,
    ScanNotSupported,
    WriteFailed,
)
from tests.conftest import BAR_ADDR, GOLD_ADDR, HEALTH_ADDR, HEAP_BASE, PID, build_fake

OK = struct.pack("<I", Status.SUCCESS)
ERR = struct.pack("<I", Status.ERROR)


def _pad(s: str, n: int) -> bytes:
    return s.encode().ljust(n, b"\0")


REG_ORDER = (
    [
        "r15",
        "r14",
        "r13",
        "r12",
        "r11",
        "r10",
        "r9",
        "r8",
        "rdi",
        "rsi",
        "rbp",
        "rbx",
        "rdx",
        "rcx",
        "rax",
    ],
    ["rip", "cs", "rflags", "rsp", "ss"],
)


def interrupt_packet(regs: dict[str, int]) -> bytes:
    """A 0x4A0 packet with FreeBSD struct reg at 0x30, built independently of the client."""
    pkt = bytearray(INTERRUPT_SIZE)
    gp, tail = REG_ORDER
    for i, name in enumerate(gp):
        struct.pack_into("<Q", pkt, 0x30 + 8 * i, regs.get(name, 0))
    struct.pack_into("<IHHIHH", pkt, 0x30 + 120, 12, 0, 0, 0, 0, 0)  # trapno=T_TRCTRAP
    for i, name in enumerate(tail):
        struct.pack_into("<Q", pkt, 0x30 + 136 + 8 * i, regs.get(name, 0))
    return bytes(pkt)


class FakeNgServer:
    """Minimal PS5Debug-NG over a FakeConsole."""

    def __init__(
        self,
        turbo: bool = True,
        foreground: bool | str = True,
        parallel: bool = False,
        refuse_flags: bool = False,
        snapshot: bool = False,
    ) -> None:
        self.snapshot = snapshot
        self.resident: dict | None = None  # {"pid","vt","fmt","size","vals": {addr: bytes}}
        self.parallel, self.refuse_flags = parallel, refuse_flags
        self.scan_flags: list[int] = []
        self.scan_compares: list[int] = []
        self.fake = build_fake()
        self.turbo, self.foreground = turbo, foreground
        self.authed = False
        self.watch_rips: list[int] = []
        self.watch_regs: list[dict[str, int]] = []  # sent after watch_rips, full reg64
        self.watchpoints: list[tuple[int, ...]] = []
        self.commands: list[int] = []
        self.connections = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _recv(self, c, n):
        buf = b""
        while len(buf) < n:
            chunk = c.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _serve(self):
        while True:  # one client at a time; a dropped client may reconnect
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            with c:
                try:
                    while True:
                        magic, cmd, ln = struct.unpack("<III", self._recv(c, 12))
                        assert magic == MAGIC
                        body = self._recv(c, ln) if ln else b""
                        self.commands.append(cmd)
                        if cmd == 0xDEAD:
                            return
                        if cmd == Cmd.CONSOLE_FOREGROUND_APP and self.foreground == "drop":
                            break  # like a PS5Debug-NG build without this command
                        if cmd == Cmd.PROC_INFO and struct.unpack("<I", body)[0] == 0:
                            c.sendall(ERR)  # real NG 1.3: error, then hang up
                            break
                        self._handle(c, cmd, body)
                except (EOFError, OSError):
                    continue

    def _handle(self, c, cmd, body):
        f = self.fake
        if cmd == Cmd.VERSION:
            c.sendall(struct.pack("<I", 3) + b"1.3")
        elif cmd == Cmd.PROC_LIST:
            procs = [ProcInfo(0, "kernel"), *f.processes()]
            c.sendall(
                OK
                + struct.pack("<I", len(procs))
                + b"".join(_pad(p.name, 32) + struct.pack("<i", p.pid) for p in procs)
            )
        elif cmd == Cmd.PROC_INFO:
            (pid,) = struct.unpack("<I", body)
            p = next(p for p in f.processes() if p.pid == pid)
            c.sendall(
                OK
                + struct.pack("<I", pid)
                + _pad(p.name, 40)
                + _pad("/app0", 64)
                + _pad(p.title_id, 16)
                + _pad("", 64)
            )
        elif cmd == Cmd.CONSOLE_FOREGROUND_APP:
            if not self.foreground:
                c.sendall(ERR)
                return
            fg = f.foreground_app()
            c.sendall(
                OK
                + struct.pack("<I", fg.pid)
                + _pad(fg.title_id, 16)
                + _pad("", 64)
                + _pad(fg.title, 40)
                + _pad(fg.version, 16)
            )
        elif cmd == Cmd.PROC_MAPS:
            (pid,) = struct.unpack("<I", body)
            maps = f.maps(pid)
            c.sendall(
                OK
                + struct.pack("<I", len(maps))
                + b"".join(
                    _pad(m.name, 32) + struct.pack("<QQQH", m.start, m.end, 0, m.prot) for m in maps
                )
            )
        elif cmd == Cmd.PROC_READ:
            pid, addr, n = struct.unpack("<IQI", body)
            try:
                data = f.read(pid, addr, n)
            except Exception:
                c.sendall(ERR)
                return
            c.sendall(OK + data)
        elif cmd == Cmd.PROC_WRITE:
            assert len(body) == 16, "data must follow the ack, not ride in the request"
            pid, addr, n = struct.unpack("<IQI", body)
            c.sendall(OK)
            data = self._recv(c, n)
            try:
                f.write(pid, addr, data)
                c.sendall(OK)
            except Exception:
                c.sendall(ERR)
        elif cmd == Cmd.PROC_AUTH:
            magic, _flags = struct.unpack("<II", body)
            assert magic == 0xBB40E64D
            chal = bytes(range(64))
            c.sendall(OK + struct.pack("<H", 64) + chal)
            ans = self._recv(c, 64)
            ks = build_keystream()
            self.authed = ans == bytes(x ^ ks[i] for i, x in enumerate(chal))
            c.sendall(OK if self.authed else struct.pack("<I", Status.DATA_NULL))
        elif cmd == Cmd.TURBOSCAN_CAPS:
            engines = (0x100 if self.parallel else 0) | (0x418 if self.snapshot else 0)
            caps = struct.pack("<IIII", 1, engines, 8, 0)
            c.sendall(OK + caps if self.turbo else ERR)
        elif cmd == Cmd.TURBOSCAN_START and struct.unpack("<I", body[23:27])[0] & 0x04:
            self._snapshot_start(c, body)
        elif cmd == Cmd.TURBOSCAN_COUNT:
            self._resident_count(c, body)
        elif cmd == Cmd.TURBOSCAN_GET:
            start, count, _flags = struct.unpack("<III", body)
            r = self.resident or {"vals": {}}
            items = sorted(r["vals"].items())[start : start + count]
            recs = b"".join(struct.pack("<Q", a) + v + v for a, v in items)
            c.sendall(OK + struct.pack("<I", len(items)) + recs + OK)
        elif cmd == Cmd.TURBOSCAN_END:
            self.resident = None
            c.sendall(OK)
        elif cmd in (Cmd.TURBOSCAN_START, Cmd.PROC_SCAN_START):
            assert self.authed
            pid, addr, n, vt, ct, align, lv = struct.unpack("<IQIBBBI", body[:23])
            self.scan_compares.append(ct)
            assert (cmd == Cmd.TURBOSCAN_START) == (len(body) == 27)
            if len(body) == 27:
                self.scan_flags.append(struct.unpack("<I", body[23:])[0])
                if self.refuse_flags and self.scan_flags[-1]:
                    c.sendall(ERR)
                    return
            c.sendall(OK)
            needle = self._recv(c, lv)
            c.sendall(OK)
            data = f.read(pid, addr, n)

            size = lv // 2 if ct == 4 else lv  # ValueBetween sends low + high

            def hit(chunk: bytes) -> bool:
                if ct == 4:
                    lo, hi = struct.unpack("<QQ", needle)
                    return lo <= struct.unpack("<Q", chunk)[0] <= hi
                if ct == 1:  # FuzzyValue: |scan - mem| < 1.0, float/double only
                    code = {8: "<f", 9: "<d"}[vt]
                    return abs(struct.unpack(code, needle)[0] - struct.unpack(code, chunk)[0]) < 1
                return chunk == needle

            entries = b"".join(
                struct.pack("<I", o) + data[o : o + size]
                for o in range(0, n - size + 1, align)
                if hit(data[o : o + size])
            )
            if entries:
                c.sendall(struct.pack("<Q", len(entries)) + entries)
            c.sendall(struct.pack("<Q", SENTINEL_U64) + OK)
        elif cmd == Cmd.PROC_SCAN_GET:
            assert self.authed
            pid, count = struct.unpack("<II", body[:8])
            out = OK
            for i in range(count):
                a, n = struct.unpack_from("<QI", body, 8 + 12 * i)
                out += f.read(pid, a, n)
            c.sendall(out + struct.pack("<Q", SENTINEL_U64))
        elif cmd == Cmd.DEBUG_ATTACH:
            c.sendall(OK)
            threading.Thread(target=self._dial_back, daemon=True).start()
        elif cmd == Cmd.DEBUG_SET_WATCHPOINT:
            assert len(body) == 24, "cmd_debug_watchpt_packet is 24 bytes (C4)"
            self.watchpoints.append(struct.unpack("<IIIIQ", body))
            c.sendall(OK)
        elif cmd in (Cmd.DEBUG_CONTINUE, Cmd.DEBUG_DETACH):
            c.sendall(OK)
        else:
            c.sendall(ERR)

    def _snapshot_start(self, c, body):
        assert self.authed and len(body) == 27
        pid, _a, _n, vt, ct, align, lv, flags = struct.unpack("<IQIBBBII", body)
        assert ct == 11 and lv == 0 and flags & 0x10
        self.scan_flags.append(flags)
        c.sendall(OK + OK)  # both acks before the segment list (real NG 1.3)
        (nseg,) = struct.unpack("<I", self._recv(c, 4))
        segs = [struct.unpack("<QI", self._recv(c, 12)) for _ in range(nseg)]
        fmt = {5: "<i", 7: "<q", 8: "<f", 9: "<d"}[vt]
        size = struct.calcsize(fmt)
        vals = {}
        for a, n in segs:
            data = self.fake.read(pid, a, n)
            for o in range(0, n - size + 1, align):
                chunk = data[o : o + size]
                if chunk != bytes(size):  # zero slots are dropped by default
                    vals[a + o] = chunk
        total = sum(n for _, n in segs)
        c.sendall(struct.pack("<QQ", total // align, total))
        c.sendall(struct.pack("<Q", total) + struct.pack("<Q", SENTINEL_U64))
        self.resident = {"pid": pid, "fmt": fmt, "size": size, "vals": vals}
        c.sendall(struct.pack("<IQ", 1, len(vals)) + OK)

    def _resident_count(self, c, body):
        pid, _base, _vt, ct, lv, flags = struct.unpack("<IQBBII", body)
        assert flags & 0x02 and self.resident is not None
        self.scan_compares.append(ct)
        c.sendall(OK)
        needle = self._recv(c, lv) if lv else b""
        r = self.resident
        fmt = r["fmt"]
        keep = {}
        for a, old in r["vals"].items():
            now = self.fake.read(pid, a, r["size"])
            o, v = struct.unpack(fmt, old)[0], struct.unpack(fmt, now)[0]
            if ct == 4:
                lo, hi = (
                    struct.unpack(fmt, needle[: r["size"]])[0],
                    struct.unpack(fmt, needle[r["size"] :])[0],
                )
                ok = lo <= v <= hi
            elif ct == 0:
                ok = now == needle
            elif ct == 1:
                ok = abs(struct.unpack(fmt, needle)[0] - v) < 1
            else:
                ok = {5: v > o, 7: v < o, 9: v != o, 10: v == o}[ct]
            if ok:
                keep[a] = now  # baseline refreshes: next compare is since this scan
        r["vals"] = keep
        c.sendall(struct.pack("<Q", 1) + struct.pack("<Q", SENTINEL_U64))
        c.sendall(struct.pack("<Q", len(keep)) + OK)

    def _dial_back(self):
        import time

        for _ in range(50):
            try:
                s = socket.create_connection(("127.0.0.1", 755), timeout=1)
                break
            except OSError:
                time.sleep(0.05)
        else:
            return
        with s:
            for rip in self.watch_rips:
                pkt = bytearray(INTERRUPT_SIZE)
                struct.pack_into("<Q", pkt, RIP_OFFSET, rip)
                s.sendall(pkt)
            for regs in self.watch_regs:
                s.sendall(interrupt_packet(regs))
            time.sleep(0.5)


@pytest.fixture
def server():
    return FakeNgServer()


@pytest.fixture
def client(server):
    cl = Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3)
    yield cl
    cl.close()


def test_connect_refused_is_unreachable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(ConsoleUnreachable):
        Ps5DebugNgClient.connect("127.0.0.1", port, timeout=1)


def test_info_commands(client):
    assert client.version() == "1.3"
    procs = client.processes()
    assert [p.pid for p in procs] == [0, PID] and procs[1].title_id == "PPSA01234"
    fg = client.foreground_app()
    assert (fg.pid, fg.title_id, fg.title, fg.version) == (PID, "PPSA01234", "Test Game", "01.000")
    assert client.maps(PID)[0].name == "eboot.bin"


def test_foreground_unsupported_falls_back():
    server = FakeNgServer(foreground=False)
    cl = Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3)
    assert cl.foreground_app() is None
    assert cl.foreground_app() is None  # not asked again
    assert server.commands.count(Cmd.CONSOLE_FOREGROUND_APP) == 1
    cl.close()


def test_foreground_drop_reconnects_and_falls_back():
    server = FakeNgServer(foreground="drop")
    cl = Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3)
    assert cl.version() == "1.3"
    assert cl.foreground_app() is None
    assert cl.version() == "1.3", "works again on the new connection"
    assert cl.foreground_app() is None
    assert server.connections == 2 and server.commands.count(Cmd.CONSOLE_FOREGROUND_APP) == 1
    cl.close()


def test_session_detects_game_when_foreground_drops():
    from cheatmaker.core.session import Session

    server = FakeNgServer(foreground="drop")
    s = Session(lambda ip, port: Ps5DebugNgClient.connect(ip, server.port, timeout=3))
    s.connect("127.0.0.1")
    g = s.refresh_game()
    assert g.title_id == "PPSA01234" and g.pid == PID
    s.disconnect()


def test_never_asks_info_for_pid0_and_survives_refusals(client, server):
    """Real PS5Debug-NG 1.3 hangs up after refusing process info for pid 0."""
    client.processes()
    assert client.version() == "1.3" and server.connections == 1
    client._w.request(Cmd.PROC_INFO, struct.pack("<I", 0))
    assert client._w.status() == Status.ERROR


def test_session_with_foreground_skips_process_list(server):
    from cheatmaker.core.session import Session

    s = Session(lambda ip, port: Ps5DebugNgClient.connect(ip, server.port, timeout=3))
    s.connect("127.0.0.1")
    g = s.refresh_game()
    assert (g.title_id, g.version) == ("PPSA01234", "01.000")
    assert Cmd.PROC_LIST not in server.commands and Cmd.PROC_INFO not in server.commands
    s.disconnect()


def test_connection_lost_names_the_command(client):
    client._w.request(0xDEAD)
    with pytest.raises(ConnectionLost, match="after 0x0000dead"):
        client._w.status()


def test_read_write(client, server):
    client.write(PID, GOLD_ADDR, struct.pack("<i", 99999))
    assert client.read(PID, GOLD_ADDR, 4) == struct.pack("<i", 99999)
    with pytest.raises(WriteFailed):
        client.write(PID, 0x10, b"\0")
    with pytest.raises(ReadFailed):
        client.read(PID, 0x10, 4)
    assert client.version() == "1.3", "connection still in sync after errors"


@pytest.mark.parametrize("turbo", [True, False])
def test_scan_exact_and_next(turbo):
    server = FakeNgServer(turbo=turbo)
    cl = Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3)
    regions = [m for m in cl.maps(PID) if m.writable]
    seen = []
    assert cl.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100, seen.append) == 2
    assert seen[-1] == 1.0 and server.authed
    server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 90))
    assert cl.scan_next(Compare.EXACT, 90) == 1
    assert cl.scan_results(10)[0].address == HEALTH_ADDR
    assert Cmd.PROC_SCAN_GET not in server.commands  # timed out on real NG 1.3 (C3)
    cl.close()


def test_next_scan_rescans_on_console_when_many_candidates(monkeypatch):
    from cheatmaker.console import ps5debug_ng

    monkeypatch.setattr(ps5debug_ng, "MAX_READ_SPANS", 0)
    server = FakeNgServer()
    cl = Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3)
    regions = [m for m in cl.maps(PID) if m.writable]
    assert cl.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100) == 2
    server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 90))
    server.fake.poke(PID, HEAP_BASE + 0x300, struct.pack("<i", 90))  # new 90, not a candidate
    reads_before = server.commands.count(Cmd.PROC_READ)
    assert cl.scan_next(Compare.EXACT, 90) == 1
    assert cl.scan_results(5)[0].address == HEALTH_ADDR
    assert server.commands.count(Cmd.PROC_READ) == reads_before, "no per-candidate reads"
    with pytest.raises(ValueError, match="too many to compare"):
        cl.scan_next(Compare.DECREASED, None)
    cl.close()


def test_unknown_scan_needs_snapshot_engine(client):
    with pytest.raises(ScanNotSupported, match="Update it"):
        client.scan_start(PID, client.maps(PID), ValueType.FLOAT, Compare.UNKNOWN, None)


def test_unknown_value_then_went_down_on_console():
    """Health hunt: unknown first search, then Went down / Same / Went up (C4 follow-up)."""
    server = FakeNgServer(snapshot=True)
    cl = Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3)
    regions = [m for m in cl.maps(PID) if m.writable]
    seen = []
    first = cl.scan_start(PID, regions, ValueType.FLOAT, Compare.UNKNOWN, None, seen.append)
    assert first > 1 and seen[-1] == 1.0
    assert server.scan_flags[-1] & 0x200, "float searches skip garbage floats"
    server.fake.poke(PID, BAR_ADDR, struct.pack("<f", 50.0))
    assert cl.scan_next(Compare.DECREASED, None) == 1
    assert cl.scan_next(Compare.UNCHANGED, None) == 1
    server.fake.poke(PID, BAR_ADDR, struct.pack("<f", 60.0))
    assert cl.scan_next(Compare.INCREASED, None) == 1
    [hit] = cl.scan_results(10)
    assert hit.address == BAR_ADDR and struct.unpack("<f", hit.value)[0] == 60.0
    assert cl.scan_next(Compare.CHANGED, None) == 0
    assert server.scan_compares[-4:] == [7, 10, 5, 9]
    cl.scan_cancel()
    assert server.resident is None, "new search frees the console's snapshot"
    cl.close()


def test_unknown_then_between_on_console():
    server = FakeNgServer(snapshot=True)
    cl = Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3)
    regions = [m for m in cl.maps(PID) if m.writable]
    cl.scan_start(PID, regions, ValueType.FLOAT, Compare.UNKNOWN, None)
    n = cl.scan_next(Compare.BETWEEN, (80, 90))
    assert n >= 1 and server.scan_compares[-1] == 4
    assert BAR_ADDR in {h.address for h in cl.scan_results(50)}
    cl.close()


def test_unknown_then_exact_number_on_console():
    server = FakeNgServer(snapshot=True)
    cl = Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3)
    regions = [m for m in cl.maps(PID) if m.writable]
    cl.scan_start(PID, regions, ValueType.INT32, Compare.UNKNOWN, None)
    server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 77))
    assert cl.scan_next(Compare.EXACT, 77) == 1
    assert cl.scan_results(5)[0].address == HEALTH_ADDR
    assert server.scan_compares[-1] == 0
    cl.close()


def test_unknown_scan_segments_split_at_u32():
    from cheatmaker.console import ps5debug_ng

    assert ps5debug_ng.SNAP_SEGMENT_MAX <= 0xFFFFFFFF


def test_watch_writes(client, server):
    server.watch_rips = [0x400102, 0x400102]
    rips = client.watch_writes(PID, HEALTH_ADDR, 4, timeout_s=1.5)
    assert rips == [0x400102, 0x400102]
    assert server.commands.count(Cmd.DEBUG_CONTINUE) == 2
    assert Cmd.DEBUG_DETACH in server.commands
    assert client.version() == "1.3"


def test_watch_rejects_odd_size(client):
    with pytest.raises(ValueError):
        client.watch_writes(PID, HEAP_BASE, 3, 1)


def test_server_gone_raises_connection_lost(client, server):
    client._w.request(0xDEAD)  # tells the fake server to hang up
    with pytest.raises(ConnectionLost):
        client.version()


def test_keystream_is_stable():
    ks = build_keystream()
    assert len(ks) == 256 and ks != bytes(256)


def test_merge_ranges_joins_touching_maps():
    from cheatmaker.console.client import MemMap

    maps = [
        MemMap("c", 0x3000, 0x4000, 3),
        MemMap("a", 0x1000, 0x2000, 3),
        MemMap("b", 0x2000, 0x3000, 3),
        MemMap("d", 0x8000, 0x9000, 3),
    ]
    assert merge_ranges(maps) == [(0x1000, 0x4000), (0x8000, 0x9000)]
    assert merge_ranges([]) == []


@pytest.mark.parametrize("refuse", [False, True])
def test_parallel_compare_flag_used_and_dropped_if_refused(refuse):
    server = FakeNgServer(parallel=True, refuse_flags=refuse)
    cl = Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3)
    regions = [m for m in cl.maps(PID) if m.writable]
    assert cl.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100) == 2
    assert server.scan_flags[0] == 0x80
    if refuse:
        assert server.scan_flags[1:] and all(f == 0 for f in server.scan_flags[1:])
    cl.close()


def test_ranges_with():
    from cheatmaker.console.ps5debug_ng import ranges_with

    r = [(0x1000, 0x2000), (0x3000, 0x4000), (0x5000, 0x6000)]
    assert ranges_with(r, [0x1500, 0x5000, 0x7000, 0x10]) == [(0x1000, 0x2000), (0x5000, 0x6000)]


def test_float_search_matches_rounded_number_on_console(monkeypatch):
    """Health shown as 80 may be 79.6: float searches use NG FuzzyValue (C3)."""
    from cheatmaker.console import ps5debug_ng

    monkeypatch.setattr(ps5debug_ng, "MAX_READ_SPANS", 0)  # force console re-scan
    server = FakeNgServer()
    cl = Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3)
    regions = [m for m in cl.maps(PID) if m.writable]
    assert BAR_ADDR in {h.address for h in _first(cl, regions, 87)}
    server.fake.poke(PID, BAR_ADDR, struct.pack("<f", 79.6))
    assert cl.scan_next(Compare.EXACT, 80) == 1
    assert cl.scan_results(5)[0].address == BAR_ADDR
    server.fake.poke(PID, BAR_ADDR, struct.pack("<f", 80.7))
    assert cl.scan_next(Compare.EXACT, 80) == 0, "keeps the app's +-0.5 rule"
    assert set(server.scan_compares) == {1}
    cl.close()


def test_whole_number_search_stays_exact(client, server):
    regions = [m for m in client.maps(PID) if m.writable]
    client.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100)
    assert set(server.scan_compares) == {0}


def _first(cl, regions, value):
    cl.scan_start(PID, regions, ValueType.FLOAT, Compare.EXACT, value)
    return cl.scan_results(100)


def test_watchpoint_packet_and_rip_offset(client, server):
    server.watch_rips = [0x400102]
    client.watch_writes(PID, HEALTH_ADDR, 4, timeout_s=1.0)
    # index 0, enabled, DR7 len 4 bytes = 3, write-only = 1, address; then cleared.
    assert server.watchpoints[0] == (0, 1, 3, 1, HEALTH_ADDR)
    assert server.watchpoints[-1] == (0, 0, 0, 0, 0)
    from cheatmaker.console.ps5debug_ng import RIP_OFFSET

    assert RIP_OFFSET == 0xB8  # 0x30 + struct reg offset of r_rip (136)


def test_find_pointers_uses_value_between_and_filters(client, server):
    server.fake.poke(PID, HEAP_BASE + 0x400, struct.pack("<Q", HEALTH_ADDR - 0x40))
    server.fake.poke(PID, HEAP_BASE + 0x408, struct.pack("<Q", HEALTH_ADDR - 0x20))
    regions = [m for m in client.maps(PID) if m.writable]
    lo, hi = HEALTH_ADDR - 0x1000, HEALTH_ADDR
    got = client.find_pointers(PID, regions, lo, hi, lambda v: v != HEALTH_ADDR - 0x20)
    assert got == [(HEAP_BASE + 0x400, HEALTH_ADDR - 0x40)]
    assert server.scan_compares[-1] == 4


def test_watch_access_uses_read_write_breaktype_and_returns_registers(client, server):
    server.watch_regs = [{"rip": 0x40010A, "r12": HEAP_BASE, "rax": 7, "rsp": 0x7FFF0000}]
    hits = client.watch(PID, HEALTH_ADDR, 4, "access", timeout_s=1.0)
    assert server.watchpoints[0] == (0, 1, 3, 3, HEALTH_ADDR)  # breaktype 3 = read-write
    assert [h.rip for h in hits] == [0x40010A]
    assert hits[0].reg("r12") == HEAP_BASE and hits[0].reg("RAX") == 7
    assert hits[0].reg("rsp") == 0x7FFF0000 and hits[0].reg("r13") == 0
    assert server.watchpoints[-1] == (0, 0, 0, 0, 0)


def test_watch_write_kind_and_bad_kind(client, server):
    server.watch_rips = [0x400102]
    assert [h.rip for h in client.watch(PID, HEALTH_ADDR, 8, "write", 1.0)] == [0x400102]
    assert server.watchpoints[0] == (0, 1, 2, 1, HEALTH_ADDR)  # DR7 length 8 bytes = 2
    with pytest.raises(ValueError):
        client.watch(PID, HEALTH_ADDR, 4, "exec", 1.0)


def test_parse_struct_reg_layout():
    from cheatmaker.console.ps5debug_ng import parse_interrupt, parse_struct_reg
    from cheatmaker.errors import ProtocolError

    pkt = interrupt_packet({"r15": 1, "r12": 0x1234, "rax": 2, "rip": 3, "rflags": 4, "ss": 5})
    regs = parse_struct_reg(pkt[0x30 : 0x30 + 176])
    assert regs["r15"] == 1 and regs["r12"] == 0x1234 and regs["rax"] == 2
    assert regs["rip"] == 3 and regs["rflags"] == 4 and regs["ss"] == 5
    assert struct.unpack_from("<Q", pkt, 0x30 + 24)[0] == 0x1234, "r12 at +24"
    assert parse_interrupt(pkt).rip == 3
    with pytest.raises(ProtocolError):
        parse_struct_reg(b"\0" * 100)
