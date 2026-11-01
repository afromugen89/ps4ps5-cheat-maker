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
from tests.conftest import GOLD_ADDR, HEALTH_ADDR, HEAP_BASE, PID, build_fake

OK = struct.pack("<I", Status.SUCCESS)
ERR = struct.pack("<I", Status.ERROR)


def _pad(s: str, n: int) -> bytes:
    return s.encode().ljust(n, b"\0")


class FakeNgServer:
    """Minimal PS5Debug-NG over a FakeConsole."""

    def __init__(
        self,
        turbo: bool = True,
        foreground: bool | str = True,
        parallel: bool = False,
        refuse_flags: bool = False,
    ) -> None:
        self.parallel, self.refuse_flags = parallel, refuse_flags
        self.scan_flags: list[int] = []
        self.fake = build_fake()
        self.turbo, self.foreground = turbo, foreground
        self.authed = False
        self.watch_rips: list[int] = []
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
            pid, addr, n = struct.unpack("<IQI", body[:16])
            try:
                f.write(pid, addr, body[16 : 16 + n])
                c.sendall(OK + OK)
            except Exception:
                c.sendall(OK + ERR)
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
            caps = struct.pack("<IIII", 1, 0x100 if self.parallel else 0, 8, 0)
            c.sendall(OK + caps if self.turbo else ERR)
        elif cmd in (Cmd.TURBOSCAN_START, Cmd.PROC_SCAN_START):
            assert self.authed
            pid, addr, n, _vt, _ct, align, lv = struct.unpack("<IQIBBBI", body[:23])
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
            entries = b"".join(
                struct.pack("<I", o) + needle
                for o in range(0, n - lv + 1, align)
                if data[o : o + lv] == needle
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
        elif cmd in (Cmd.DEBUG_SET_WATCHPOINT, Cmd.DEBUG_CONTINUE, Cmd.DEBUG_DETACH):
            c.sendall(OK)
        else:
            c.sendall(ERR)

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
    assert Cmd.PROC_SCAN_GET in server.commands
    cl.close()


def test_unknown_scan_not_supported_yet(client):
    with pytest.raises(ScanNotSupported):
        client.scan_start(PID, client.maps(PID), ValueType.FLOAT, Compare.UNKNOWN, None)


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
