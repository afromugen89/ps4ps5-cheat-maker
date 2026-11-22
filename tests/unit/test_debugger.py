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
        self.dial_first = False  # dial back before answering DEBUG_ATTACH
        self.reply_attach = True  # False: DEBUG_ATTACH is never answered
        self.fresh_detaches = 0  # DEBUG_DETACH on a connection that never attached
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
        attached = False
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
                    attached = True
                    self.listening_at_attach.append(self._port_open())
                    st = self.attach_answers.pop(0) if self.attach_answers else Status.SUCCESS
                    dial = st == Status.SUCCESS and self.dial_back
                    if dial and self.dial_first:
                        self._dial()
                    if self.reply_attach:
                        c.sendall(struct.pack("<I", st))
                    if dial and not self.dial_first:
                        self._dial()
                elif cmd == Cmd.DEBUG_DETACH and not attached:
                    self.fresh_detaches += 1
                    c.sendall(OK)
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
    kw.setdefault("deadline", 1.0)
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
    s = session(server, deadline=0.3)
    started = time.monotonic()
    with pytest.raises(FirewallBlocked, match=f"TCP port {s.listen_port}"):
        s.attach()
    assert time.monotonic() - started < 3
    assert _wait(lambda: server.names() == ["DEBUG_ATTACH", "DEBUG_DETACH"])
    assert s.state == "detached"


def test_dial_back_before_the_attach_reply_is_accepted(server):
    """The accept runs before DEBUG_ATTACH is sent: NG may dial back first."""
    server.dial_first = True
    s = session(server)
    with s:
        assert s.state == "running" and server.dialled.is_set()


def test_no_attach_reply_is_the_firewall_and_detaches_on_a_fresh_connection(server):
    """M1: the reply never comes (NG waits for a dial back the firewall blocks)."""
    server.reply_attach = False
    server.dial_back = False
    s = session(server, deadline=0.4)
    started = time.monotonic()
    with pytest.raises(FirewallBlocked, match=f"TCP port {s.listen_port}"):
        s.attach()
    assert time.monotonic() - started < 3
    assert _wait(lambda: server.fresh_detaches == 1) and s.state == "detached"


def test_dial_back_without_an_attach_reply_is_not_the_firewall(server):  # m-A
    """It connected back, so the port is open: a stuck attach, said as such."""
    server.dial_first = True
    server.reply_attach = False
    s = session(server, deadline=0.4)
    with pytest.raises(DebuggerError, match="connected back") as caught:
        s.attach()
    assert not isinstance(caught.value, FirewallBlocked)
    assert _wait(lambda: server.fresh_detaches == 1) and s.state == "detached"


def test_a_failed_attach_doesnt_wait_for_the_accept_deadline(server):  # m-D
    server.attach_answers = [Status.ERROR]
    server.dial_back = False
    s = session(server, deadline=5.0)
    started = time.monotonic()
    with pytest.raises(DebuggerError, match="wouldn't attach"):
        s.attach()
    assert time.monotonic() - started < 1.0  # the accept thread woke at once
    assert s.state == "detached" and s._threads == []


def test_a_dial_back_after_the_session_closed_is_closed(server):  # m-A
    s = session(server)
    a, b = socket.socketpair()
    with b:
        s._end.set()
        s._take_back(a, ("127.0.0.1", 0))
        assert s._back is None and a.fileno() == -1  # closed, not kept
        s._end.clear()
        c, d = socket.socketpair()
        with c, d:
            s._take_back(c, ("127.0.0.1", 0))
            assert s._back is c


def test_only_the_console_may_dial_back():  # QA risk: another machine on port 755
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        stranger = socket.socket()
        stranger.bind(("127.0.0.2", 0))
        stranger.connect(("127.0.0.1", port))
        console = socket.create_connection(("127.0.0.1", port))
        try:
            got = dbg._accept_until(listener, time.monotonic() + 2, threading.Event(), "127.0.0.1")
            assert got is not None and got[1][0] == "127.0.0.1"
            got[0].close()
            stranger.settimeout(1)
            assert stranger.recv(1) == b""  # the stranger was closed
        finally:
            stranger.close()
            console.close()


def test_console_ip_resolves_or_allows_any():
    assert dbg._console_ip("127.0.0.1") == "127.0.0.1"
    assert dbg._console_ip("no-such-host.invalid") is None


def test_accept_until_stops_when_the_session_ends():  # m-D
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        end = threading.Event()
        threading.Timer(0.1, end.set).start()
        started = time.monotonic()
        assert dbg._accept_until(listener, time.monotonic() + 5, end) is None
        assert time.monotonic() - started < 1.0


def test_a_slow_fresh_detach_doesnt_hold_the_firewall_message(server, monkeypatch):  # BUG-02
    server.reply_attach = False
    server.dial_back = False
    s = session(server, deadline=0.4)
    real = s._detach_fresh
    done = threading.Event()

    def slow() -> None:
        time.sleep(1.5)  # a PS5 slow to answer the fresh connection
        real()
        done.set()

    monkeypatch.setattr(s, "_detach_fresh", slow)
    started = time.monotonic()
    with pytest.raises(FirewallBlocked):
        s.attach()
    assert time.monotonic() - started < s.deadline + 0.75
    assert done.wait(5) and server.fresh_detaches == 1  # still always tried


def test_attach_deadline_covers_reply_and_dial_back(server):
    """The whole attach stays within the deadline (AC-1.3: 10 s by default)."""
    assert dbg.ATTACH_DEADLINE <= 10
    server.dial_back = False
    s = session(server, deadline=0.5)
    started = time.monotonic()
    with pytest.raises(FirewallBlocked):
        s.attach()
    assert time.monotonic() - started < 1.5


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


def test_a_stop_is_decoded_and_resume_continues(server, caplog):
    caplog.set_level("INFO", logger="cheatmaker.console.debugger")
    s = session(server)
    with s:
        s.set_watch(1, ADDR, 4, "write")
        server.send_stop(stop_packet(0x400105, dr6=0b0010))
        stop = s.next_stop(2.0)
        assert stop is not None and s.state == "stopped"
        assert (stop.lwpid, stop.thread, stop.slot, stop.signal) == (77, "GameThread", 1, 5)
        assert stop.hit.rip == 0x400105 and stop.hit.reg("rax") == 7
        assert stop.fpu[:4] == b"FPU!" and len(stop.fpu) == dbg.FPU_SIZE
        assert "action=pro_debug_stop lwpid=77 status=0x57f signal=5" in caplog.text  # C-P3d
        s.resume()
        assert s.state == "running"
        assert server.commands[-1] == (Cmd.DEBUG_CONTINUE, struct.pack("<I", 0))
        assert s.next_stop(0.05) is None


def test_only_the_first_stops_are_logged_at_info(server, caplog, monkeypatch):  # nit
    import logging

    monkeypatch.setattr(dbg, "STOP_LOG_INFO", 2)
    caplog.set_level(logging.DEBUG, logger="cheatmaker.console.debugger")
    with session(server) as s:
        for k in range(3):
            server.send_stop(stop_packet(0x400100 + k))
            assert s.next_stop(2.0) is not None
            s.resume()
    levels = [r.levelno for r in caplog.records if "action=pro_debug_stop" in r.getMessage()]
    assert levels == [logging.INFO, logging.INFO, logging.DEBUG]


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


def test_a_failed_keep_alive_loses_the_session(server):
    """m1: a keep-alive that fails leaves the socket out of step: the wire is dead and
    nothing more is sent on it (detach goes over a fresh connection)."""
    server.answer_version = False
    s = session(server, keepalive_s=0.05, timeout=0.2)
    with s:
        with pytest.raises(DebuggerLost, match="stopped answering"):
            s.next_stop(5.0)
        assert s.state == "lost"
        with pytest.raises(DebuggerLost):
            s.resume()
    assert _wait(lambda: server.fresh_detaches == 1)


def test_other_signals_end_the_session(server):
    """M3: only SIGTRAP is a hit; SIGSEGV (11) ends the watch with the signal named."""
    s = session(server)
    with s:
        server.send_stop(stop_packet(0x400105, status=0x0B7F))
        with pytest.raises(DebuggerLost, match="signal 11"):
            s.next_stop(2.0)
        assert s.state == "lost"


def test_listener_options_per_platform():
    class Sock:
        def __init__(self):
            self.opts = []

        def setsockopt(self, level, opt, value):
            self.opts.append((level, opt, value))

    from cheatmaker.console.ps5debug_ng import listen_options

    win, other = Sock(), Sock()
    listen_options(win, nt=True)
    listen_options(other, nt=False)
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)
    assert win.opts == [(socket.SOL_SOCKET, exclusive, 1)]
    assert other.opts == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]


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


def test_fake_debugger_mirrors_the_session(fake):
    """m8 and M1/M3 modes: a dead wire sends no clears; other signals end it."""
    d = fake.debugger
    d.never_reply = True
    with pytest.raises(FirewallBlocked):
        d.attach()
    assert d.calls[-1] == ("fresh_detach",) and d.state == "detached"
    d.never_reply, d.dial_before_reply = False, True
    d.attach()
    assert ("dialled_first",) in d.calls and d.state == "running"
    d.lost_after = 0
    with pytest.raises(DebuggerLost):
        d.next_stop(1)
    d.detach()
    assert d.calls[-1] == ("fresh_detach",) and ("clear", 0) not in d.calls[-2:]
    d.lost_after = None
    d.attach()
    d.set_watch(0, ADDR, 4, "write")
    d.stops = [FakeStop(0, WatchHit(1), signal=11)]
    with pytest.raises(DebuggerLost, match="signal 11"):
        d.next_stop(1)
    d.detach()
    assert d.calls[-5:] == [*[("clear", k) for k in range(4)], ("detach",)]
