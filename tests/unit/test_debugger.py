"""DebugSession (architecture-pro step 16) against a local server that speaks the
documented debugger commands, plus the FakeDebugger the Pro tests use.

The real PS5 behaviour is checked by console tests C-P3a..e."""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from cheatmaker.console import debugger as dbg
from cheatmaker.console.client import StopEvent, WatchHit, open_debugger
from cheatmaker.console.fake import FakeConsole, FakeStop
from cheatmaker.console.ps5debug_ng import INTERRUPT_SIZE, MAGIC, Cmd, Status
from cheatmaker.errors import (
    DebuggerError,
    DebuggerLost,
    FirewallBlocked,
    PortInUse,
    ProtocolError,
)
from tests.unit.test_ps5debug_ng import interrupt_packet

OK = struct.pack("<I", Status.SUCCESS)
PID = 0x1234
ADDR = 0x20000040


def stop_packet(
    rip: int, *, lwpid: int = 77, name: str = "GameThread", dr6: int = 1, status: int = 0x057F
) -> bytes:
    """A 0x4A0 stop packet built independently of the client."""
    pkt = bytearray(interrupt_packet({"rip": rip, "rax": 7}))
    struct.pack_into("<II", pkt, 0, lwpid, status)
    pkt[8 : 8 + len(name)] = name.encode()
    struct.pack_into("<Q", pkt, 0x420 + 8 * 6, dr6)
    pkt[0xE0:0xE4] = b"FPU!"
    return bytes(pkt)


class DebugServer:
    """PS5Debug-NG's debugger commands on a local port: answers, records, dials back."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.commands: list[tuple[int, bytes]] = []
        self.attach_answers: list[int] = []  # statuses for the next attaches
        self.dial_back = True
        self.answer_version = True
        self.listening_at_attach: list[bool] = []
        self.client_port = 0
        self.back: socket.socket | None = None
        self.dialled = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._conn, args=(c,), daemon=True).start()

    def _recv(self, c: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = c.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def _conn(self, c: socket.socket) -> None:
        with c:
            while True:
                try:
                    magic, cmd, n = struct.unpack("<III", self._recv(c, 12))
                    body = self._recv(c, n) if n else b""
                except (ConnectionError, OSError):
                    return
                assert magic == MAGIC
                self.commands.append((cmd, body))
                if cmd == Cmd.DEBUG_ATTACH:
                    self.listening_at_attach.append(self._port_open())
                    st = self.attach_answers.pop(0) if self.attach_answers else Status.SUCCESS
                    c.sendall(struct.pack("<I", st))
                    if st == Status.SUCCESS and self.dial_back:
                        self._dial()
                elif cmd == Cmd.VERSION:
                    if self.answer_version:
                        c.sendall(struct.pack("<I", 5) + b"1.3.2")
                else:
                    c.sendall(OK)

    def _port_open(self) -> bool:
        """Is the client's back-channel port taken (bound) already?"""
        with socket.socket() as probe:
            try:
                probe.bind(("0.0.0.0", self.client_port))
            except OSError:
                return True
        return False

    def _dial(self) -> None:
        self.back = socket.create_connection(("127.0.0.1", self.client_port), timeout=2)
        self.dialled.set()

    def send_stop(self, packet: bytes) -> None:
        assert self.dialled.wait(2) and self.back is not None
        self.back.sendall(packet)

    def names(self) -> list[str]:
        return [Cmd(c).name for c, _ in self.commands]

    def close(self) -> None:
        self.sock.close()
        if self.back is not None:
            self.back.close()


@pytest.fixture
def server():
    s = DebugServer()
    yield s
    s.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def session(server: DebugServer, **kw) -> dbg.DebugSession:
    port = _free_port()
    server.client_port = port
    kw.setdefault("timeout", 1.0)
    kw.setdefault("back_wait", 1.0)
    kw.setdefault("keepalive_s", 60.0)
    return dbg.DebugSession("127.0.0.1", server.port, PID, listen_port=port, **kw)


def _wait(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# -- attach --------------------------------------------------------------------------------
def test_listener_is_bound_before_attach_and_back_channel_taken(server):
    s = session(server)
    s.attach()
    try:
        assert server.listening_at_attach == [True]
        assert s.state == "running" and server.dialled.is_set()
        assert server.commands[0] == (Cmd.DEBUG_ATTACH, struct.pack("<I", PID))
    finally:
        s.detach()


def test_port_in_use_is_named(server):
    s = session(server)
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", s.listen_port))
    blocker.listen(1)
    try:
        with pytest.raises(PortInUse, match=f"Port {s.listen_port} is in use"):
            s.attach()
    finally:
        blocker.close()
    assert server.commands == [] and s.state == "detached"


def test_no_dial_back_is_the_firewall_and_detaches(server):
    """AC-1.3: says so within the wait, names the port, and leaves nothing attached."""
    server.dial_back = False
    s = session(server, back_wait=0.3)
    started = time.monotonic()
    with pytest.raises(FirewallBlocked, match=f"TCP port {s.listen_port}"):
        s.attach()
    assert time.monotonic() - started < 3
    assert _wait(lambda: server.names() == ["DEBUG_ATTACH", "DEBUG_DETACH"])
    assert s.state == "detached"


def test_already_debugged_detaches_waits_and_retries_once(server):
    server.attach_answers = [Status.ALREADY_DEBUG]
    slept: list[float] = []
    s = session(server, sleep=slept.append)
    s.attach()
    try:
        assert server.names()[:3] == ["DEBUG_ATTACH", "DEBUG_DETACH", "DEBUG_ATTACH"]
        assert slept == [dbg.ALREADY_DEBUG_WAIT] and s.state == "running"
    finally:
        s.detach()


def test_already_debugged_twice_is_refused(server):
    server.attach_answers = [Status.ALREADY_DEBUG, Status.ALREADY_DEBUG]
    s = session(server, sleep=lambda _t: None)
    with pytest.raises(DebuggerError, match="ALREADY_DEBUG"):
        s.attach()
    assert s.state == "detached"


# -- watches, stops, DR6 ------------------------------------------------------------------------
def test_set_watch_packet_and_validation(server):
    s = session(server)
    with s:
        s.set_watch(2, ADDR, 4, "access")
        assert server.commands[-1] == (
            Cmd.DEBUG_SET_WATCHPOINT,
            struct.pack("<IIIIQ", 2, 1, 3, 3, ADDR),
        )
        for bad in ((4, ADDR, 4, "write"), (0, ADDR, 3, "write"), (0, ADDR, 4, "exec")):
            with pytest.raises(ValueError):
                s.set_watch(*bad)
        s.clear_watch(2)
        assert server.commands[-1][1] == struct.pack("<IIIIQ", 2, 0, 0, 0, 0)


def test_a_stop_is_decoded_and_resume_continues(server):
    s = session(server)
    with s:
        s.set_watch(1, ADDR, 4, "write")
        server.send_stop(stop_packet(0x400105, dr6=0b0010))
        stop = s.next_stop(2.0)
        assert stop is not None and s.state == "stopped"
        assert (stop.lwpid, stop.thread, stop.slot, stop.signal) == (77, "GameThread", 1, 5)
        assert stop.hit.rip == 0x400105 and stop.hit.reg("rax") == 7
        assert stop.fpu[:4] == b"FPU!" and len(stop.fpu) == dbg.FPU_SIZE
        s.resume()
        assert s.state == "running"
        assert server.commands[-1] == (Cmd.DEBUG_CONTINUE, struct.pack("<I", 0))
        assert s.next_stop(0.05) is None


def test_dr6_names_the_lowest_slot_or_none():
    hit = WatchHit(1)
    assert StopEvent(1, "", hit, dr6=0b1100).slot == 2
    assert StopEvent(1, "", hit, dr6=0x4000).slot is None  # BS only: no watch slot


def test_parse_stop_rejects_a_short_packet():
    with pytest.raises(ProtocolError):
        dbg.parse_stop(b"\0" * 10)
    assert dbg.stop_signal(0x097F) == 9 and dbg.stop_signal(0x0900) == 0


def test_game_killed_ends_the_session(server):
    s = session(server)
    with s:
        server.send_stop(stop_packet(0x400105, status=0x097F))
        with pytest.raises(DebuggerLost, match="game closed"):
            s.next_stop(2.0)
        assert s.state == "lost"
    assert s.state == "detached"


def test_back_channel_closing_ends_the_session(server):
    s = session(server)
    with s:
        assert server.dialled.wait(2)
        server.back.close()
        with pytest.raises(DebuggerLost, match="back channel"):
            s.next_stop(2.0)
        with pytest.raises(DebuggerLost):  # every later call says the same
            s.next_stop(0.01)


# -- keep-alive --------------------------------------------------------------------------------
def test_keep_alive_pings_only_while_running(server):
    s = session(server, keepalive_s=0.05)
    with s:
        assert _wait(lambda: server.names().count("VERSION") >= 2)
        server.send_stop(stop_packet(0x400105))
        assert s.next_stop(2.0) is not None
        time.sleep(0.1)  # a ping on its way when the stop came may still finish
        pings = server.names().count("VERSION")
        time.sleep(0.3)
        assert server.names().count("VERSION") == pings, "never while a thread is stopped"
        s.resume()
        assert _wait(lambda: server.names().count("VERSION") > pings)


def test_two_missed_keep_alives_lose_the_session(server):
    server.answer_version = False
    s = session(server, keepalive_s=0.05, timeout=0.2)
    with s:
        with pytest.raises(DebuggerLost, match="stopped answering"):
            s.next_stop(5.0)
        assert s.state == "lost"
        with pytest.raises(DebuggerLost):
            s.resume()


# -- detach ------------------------------------------------------------------------------------
def test_detach_clears_all_four_slots_then_detaches_and_frees_the_port(server):
    s = session(server)
    s.attach()
    s.set_watch(0, ADDR, 4, "write")
    s.detach()
    tail = server.commands[-5:]
    assert [b for c, b in tail[:4]] == [struct.pack("<IIIIQ", k, 0, 0, 0, 0) for k in range(4)]
    assert tail[4][0] == Cmd.DEBUG_DETACH
    n = len(server.commands)
    s.detach()  # safe to repeat
    assert len(server.commands) == n and s.state == "detached"
    with socket.socket() as again:  # the listener is closed
        again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        again.bind(("0.0.0.0", s.listen_port))


def test_with_block_detaches_on_an_error(server):
    s = session(server)
    with pytest.raises(RuntimeError), s:
        raise RuntimeError("boom")
    assert Cmd.DEBUG_DETACH in [c for c, _ in server.commands] and s.state == "detached"


def test_commands_need_an_attached_session(server):
    s = session(server)
    with pytest.raises(DebuggerError, match="isn't attached"):
        s.set_watch(0, ADDR, 4, "write")
    with pytest.raises(DebuggerError):
        s.next_stop(0.01)


def test_ng_client_opens_a_detached_session_on_its_back_channel_port():
    from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient

    cl = Ps5DebugNgClient.__new__(Ps5DebugNgClient)
    cl.host, cl.port, cl.interrupt_port = "10.0.0.2", 744, 7555
    s = cl.open_debugger(PID)
    assert (s.host, s.port, s.pid, s.listen_port) == ("10.0.0.2", 744, PID, 7555)
    assert s.state == "detached"


# -- FakeDebugger ----------------------------------------------------------------------------------
@pytest.fixture
def fake() -> FakeConsole:
    f = FakeConsole()
    f.add_process(PID, title_id="PPSA01234")
    return f


def test_fake_debugger_delivers_stops_for_set_slots_and_records(fake):
    d = open_debugger(fake, PID)
    d.stops = [FakeStop(1, WatchHit(0x10)), FakeStop(0, WatchHit(0x20))]
    d.attach()
    d.set_watch(0, ADDR, 4, "write")
    stop = d.next_stop(0.5)
    assert stop.hit.rip == 0x20 and stop.slot == 0 and d.state == "stopped"
    d.resume()
    assert d.next_stop(0.5) is None and d.clock() == pytest.approx(0.55)
    d.detach()
    assert d.calls[-5:] == [*[("clear", k) for k in range(4)], ("detach",)]


def test_fake_debugger_failure_modes(fake):
    d = fake.debugger
    d.back_channel = False
    with pytest.raises(FirewallBlocked):
        open_debugger(fake, PID).attach()
    d.back_channel, d.already_debugged_once = True, True
    d.attach()
    assert ("already_debugged",) in d.calls and d.state == "running"
    d.detach()
    d.game_exits_after = 0
    d.attach()
    with pytest.raises(DebuggerLost, match="game closed"):
        d.next_stop(1)
    d.detach()
    assert d.calls[-1] == ("detach",)


def test_open_debugger_needs_a_client_that_has_one():
    with pytest.raises(DebuggerError):
        open_debugger(object(), PID)


def test_stop_packet_size_matches_the_wire():
    assert len(stop_packet(1)) == INTERRUPT_SIZE
