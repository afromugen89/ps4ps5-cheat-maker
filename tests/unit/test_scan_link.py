"""Searches run on their own PS5 connection, the scan link (docs/architecture-scan-link.md).

A long snapshot must not hold the socket that reads, writes and the freezer use. These
tests gate the fake server's snapshot / value scan so a search stays "running" while the
main connection is used from the test (main) thread.
"""

from __future__ import annotations

import logging
import struct
import threading
import time

import pytest

from cheatmaker.console import ps5debug_ng
from cheatmaker.console.ps5debug_ng import Cmd, Ps5DebugNgClient
from cheatmaker.core.freezer import Freezer
from cheatmaker.core.scanner import LOST_LINK, Scanner
from cheatmaker.core.session import Session
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.errors import ConsoleBusy, ReadFailed, ScanCancelled, ScanLinkLost
from cheatmaker.ui import messages
from tests.conftest import GOLD_ADDR, HEALTH_ADDR, HEAP_BASE, PID
from tests.unit.test_ps5debug_ng import FakeNgServer

MAIN, LINK = 1, 2  # connection numbers on the fake server: main first, then the link
TURBO = {c for c in Cmd if c.name.startswith("TURBOSCAN_")}
FAST = 0.5  # what "returns at once" means here


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("timed out waiting")
        time.sleep(0.01)


class _Bg:
    """Run ``fn`` on a worker thread; keep its result or error."""

    def __init__(self, fn):
        self.result = self.error = None
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn):
        try:
            self.result = fn()
        except BaseException as exc:
            self.error = exc

    def join(self, timeout=5.0):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "the worker is still running"
        return self


def _connect(server, **kw) -> Ps5DebugNgClient:
    return Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3, **kw)


def _regions(cl):
    return [m for m in cl.maps(PID) if m.writable]


def _sent(server, conn, cmds) -> bool:
    return any(c in cmds for c in server.conn_commands.get(conn, []))


def _i32(server, addr):
    return struct.unpack("<i", server.fake.read(PID, addr, 4))[0]


def _gated_unknown(server, cl, vtype=ValueType.INT32):
    """Start an unknown search on a worker; return once the link's snapshot is held."""
    server.snapshot_gate.clear()
    regions = _regions(cl)
    bg = _Bg(lambda: cl.scan_start(PID, regions, vtype, Compare.UNKNOWN, None))
    _wait(lambda: _sent(server, LINK, {Cmd.TURBOSCAN_START}))
    time.sleep(0.05)  # the request is in; the server now waits on the gate
    return bg


# 1) ------------------------------------------------------------------------------------
def test_reads_and_writes_go_through_during_a_long_snapshot():
    server = FakeNgServer(snapshot=True)
    cl = _connect(server)
    bg = _gated_unknown(server, cl)
    t0 = time.monotonic()
    assert cl.read(PID, HEALTH_ADDR, 4) == server.fake.read(PID, HEALTH_ADDR, 4)
    cl.write(PID, GOLD_ADDR, struct.pack("<i", 4321))
    assert time.monotonic() - t0 < FAST
    assert _i32(server, GOLD_ADDR) == 4321
    assert bg.thread.is_alive(), "the snapshot is still running"
    server.snapshot_gate.set()
    bg.join()
    assert bg.error is None and bg.result == len(server.residents[LINK]["vals"])
    assert not _sent(server, MAIN, TURBO), "no search command on the main connection"
    assert cl.scan_counts() == {ValueType.INT32: bg.result}
    cl.close()


# 2) ------------------------------------------------------------------------------------
def test_unknown_over_several_types_never_uses_the_main_connection():
    server = FakeNgServer(snapshot=True)
    cl = _connect(server)
    server.snapshot_gate.clear()
    regions = _regions(cl)
    types = (ValueType.FLOAT, ValueType.INT32)
    bg = _Bg(lambda: cl.scan_start(PID, regions, types, Compare.UNKNOWN, None))
    _wait(lambda: _sent(server, LINK, {Cmd.TURBOSCAN_START}) and server.connections >= 3)
    t0 = time.monotonic()
    assert cl.version() == "1.3" and cl.read(PID, HEALTH_ADDR, 4)
    assert time.monotonic() - t0 < FAST
    server.snapshot_gate.set()
    bg.join()
    assert bg.error is None and set(cl.scan_counts()) == set(types)
    server.fake.poke(PID, GOLD_ADDR, struct.pack("<i", 2000))
    assert cl.scan_next(Compare.EXACT, 2000) == 1
    assert cl.scan_results(5)[0].address == GOLD_ADDR
    assert not _sent(server, MAIN, TURBO)
    cl.close()


# 3) ------------------------------------------------------------------------------------
def test_frozen_value_is_written_during_a_pointer_pass():
    server = FakeNgServer()
    server.fake.poke(PID, HEAP_BASE + 0x400, struct.pack("<Q", HEALTH_ADDR - 0x40))
    session = Session(lambda ip, port: _connect(server, scan_connections=1))
    session.connect("127.0.0.1")
    session.refresh_game()
    cl = session.client
    regions = _regions(cl)
    lo, hi = HEALTH_ADDR - 0x1000, HEALTH_ADDR
    server.scan_gate.clear()
    bg = _Bg(lambda: cl.find_pointers(PID, regions, lo, hi, lambda v: True))
    _wait(lambda: _sent(server, LINK, {Cmd.TURBOSCAN_START}))
    freezer = Freezer(session, hz=20)
    freezer.freeze("health", [(PID, HEALTH_ADDR, struct.pack("<i", 100))])
    try:
        server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 40))  # took damage
        _wait(lambda: _i32(server, HEALTH_ADDR) == 100, timeout=1.0)
        assert bg.thread.is_alive(), "the pointer pass is still held"
    finally:
        freezer.clear()
        server.scan_gate.set()
    bg.join()
    assert bg.error is None and (HEAP_BASE + 0x400, HEALTH_ADDR - 0x40) in bg.result
    assert not _sent(server, MAIN, TURBO)
    session.disconnect()


# 4) ------------------------------------------------------------------------------------
def test_scan_cancel_during_a_snapshot_returns_at_once():
    server = FakeNgServer(snapshot=True)
    cl = _connect(server)
    bg = _gated_unknown(server, cl)
    old = cl._link
    t0 = time.monotonic()
    cl.scan_cancel()
    assert time.monotonic() - t0 < FAST
    bg.join(2.0)
    assert isinstance(bg.error, ScanCancelled)
    assert cl._link is None and old._discarded
    assert cl.scan_counts() == {} and cl.scan_results(5) == []
    server.snapshot_gate.set()
    before = server.connections
    n = cl.scan_start(PID, _regions(cl), ValueType.INT32, Compare.UNKNOWN, None)
    assert n > 0 and cl._link is not old and server.connections == before + 1
    assert cl.scan_next(Compare.UNCHANGED, None) == n
    cl.close()


def test_scan_cancel_with_an_idle_link_frees_the_search_and_keeps_the_link():
    server = FakeNgServer(snapshot=True)
    cl = _connect(server)
    cl.scan_start(PID, _regions(cl), ValueType.INT32, Compare.UNKNOWN, None)
    link = cl._link
    assert server.residents
    cl.scan_cancel()
    assert not server.residents and cl._link is link and not link._discarded
    with pytest.raises(ValueError, match="start a new search"):
        cl.scan_next(Compare.UNCHANGED, None)
    cl.close()


# 5) ------------------------------------------------------------------------------------
def test_link_dropped_in_a_first_search_keeps_the_session():
    server = FakeNgServer()
    session = Session(lambda ip, port: _connect(server, scan_connections=1))
    session.connect("127.0.0.1")
    session.refresh_game()
    sc = Scanner(session)
    server.fail_scan_on = {LINK}  # the link hangs up mid-scan
    with pytest.raises(ScanLinkLost):
        sc.first_scan(100, ValueType.INT32)
    assert session.connected and not sc.active and session.client._link is None
    assert _i32(server, HEALTH_ADDR) == 100
    assert session.client.read(PID, HEALTH_ADDR, 4) == struct.pack("<i", 100)
    assert sc.first_scan(100, ValueType.INT32) == 2, "a new link (connection 3)"
    session.disconnect()


def test_link_dropped_in_a_next_search_ends_the_search(monkeypatch):
    monkeypatch.setattr(ps5debug_ng, "MAX_READ_SPANS", 0)  # narrowing re-scans on the PS5
    server = FakeNgServer()
    session = Session(lambda ip, port: _connect(server, scan_connections=1))
    session.connect("127.0.0.1")
    session.refresh_game()
    sc = Scanner(session)
    lost = []
    sc.on_lost.append(lambda: lost.append(sc.lost_why))
    assert sc.first_scan(100, ValueType.INT32) == 2
    server.fail_scan_on = {LINK}
    server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 90))
    with pytest.raises(ScanLinkLost) as exc:
        sc.next_scan(Compare.EXACT, 90)
    assert lost == [LOST_LINK] and not sc.active and session.connected
    assert messages.user_message(exc.value) != messages.LOST
    assert sc.first_scan(90, ValueType.INT32) == 1
    session.disconnect()


# 6) ------------------------------------------------------------------------------------
def test_main_reconnect_during_a_search_leaves_the_search_alone():
    server = FakeNgServer(snapshot=True)
    cl = _connect(server)
    bg = _gated_unknown(server, cl)
    link = cl._link
    with pytest.raises(ReadFailed):  # NG hangs up after a refused read: main reconnects
        cl.read_many(PID, [(HEALTH_ADDR, 4), (0x10, 4)])
    assert cl.version() == "1.3" and cl._link is link
    server.snapshot_gate.set()
    bg.join()
    assert bg.error is None and bg.result > 0
    assert cl.scan_results(5) and cl.scan_next(Compare.UNCHANGED, None) == bg.result
    cl.close()


# 7) ------------------------------------------------------------------------------------
def test_close_closes_every_socket(monkeypatch):
    monkeypatch.setattr(ps5debug_ng, "SCAN_SLICE", 0x100)
    monkeypatch.setattr(ps5debug_ng, "BRIDGE_GAP", 0)
    server = FakeNgServer(snapshot=True)
    cl = _connect(server, scan_connections=3)
    regions = _regions(cl)
    cl.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100)  # link + 2 extras
    types = (ValueType.FLOAT, ValueType.INT32)
    cl.scan_start(PID, regions, types, Compare.UNKNOWN, None)  # + 1 for the second type
    assert server.connections == 5
    cl.close()
    _wait(lambda: server.closed == set(range(1, server.connections + 1)), timeout=2.0)


def test_close_during_a_snapshot_returns_at_once():
    server = FakeNgServer(snapshot=True)
    cl = _connect(server)
    bg = _gated_unknown(server, cl)
    t0 = time.monotonic()
    cl.close()
    assert time.monotonic() - t0 < FAST
    bg.join(2.0)
    assert isinstance(bg.error, ScanCancelled)
    server.snapshot_gate.set()
    _wait(lambda: server.closed == {MAIN, LINK}, timeout=2.0)


# 8) ------------------------------------------------------------------------------------
def test_ui_thread_gets_console_busy_instead_of_hanging():
    assert threading.current_thread() is threading.main_thread()
    server = FakeNgServer()
    session = Session(lambda ip, port: _connect(server))
    session.connect("127.0.0.1")
    cl = session.client
    held, release = threading.Event(), threading.Event()

    def hold():
        with cl._lock:
            held.set()
            release.wait(5)

    holder = _Bg(hold)
    held.wait(2)
    waiting = _Bg(lambda: cl.read(PID, HEALTH_ADDR, 4))  # not the UI thread: it waits
    t0 = time.monotonic()
    with pytest.raises(ConsoleBusy) as exc:
        cl.read(PID, HEALTH_ADDR, 4)
    assert 1.0 <= time.monotonic() - t0 < 2.0
    assert isinstance(exc.value, ReadFailed)
    assert messages.user_message(exc.value) == ps5debug_ng.BUSY_MESSAGE
    assert waiting.thread.is_alive(), "other threads wait as before"
    release.set()
    holder.join()
    waiting.join()
    assert waiting.error is None and waiting.result == struct.pack("<i", 100)
    assert session.connected and cl.read(PID, HEALTH_ADDR, 4) == struct.pack("<i", 100)
    session.disconnect()


def test_freezer_skips_a_round_when_the_console_is_busy():
    class Busy:
        def write(self, pid, addr, data):
            raise ConsoleBusy(ps5debug_ng.BUSY_MESSAGE)

    class S:
        client = Busy()

    fz = Freezer(S())  # type: ignore[arg-type]
    with fz._lock:  # no worker thread: tick() is driven here
        fz._entries["health"] = [(PID, HEALTH_ADDR, b"\x64\0\0\0")]
    fz.tick()
    assert fz.keys == {"health"}, "still frozen"


# connection limits and fallbacks --------------------------------------------------------
def test_link_that_cannot_open_falls_back_to_the_main_connection(caplog):
    server = FakeNgServer()
    cl = _connect(server, scan_connections=1)
    regions = _regions(cl)
    cl.port = 1  # no second connection
    with caplog.at_level(logging.WARNING):
        assert cl.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100) == 2
    assert "searching on the main connection" in caplog.text
    assert cl._link is None and server.scan_conns == {MAIN}
    assert cl.scan_counts() == {ValueType.INT32: 2}
    cl.port = server.port
    assert cl.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100) == 2
    assert server.scan_conns == {MAIN, LINK} and cl._scan is None, "old search freed"
    cl.close()


def test_scan_counts_never_opens_a_link():
    server = FakeNgServer()
    cl = _connect(server)
    assert cl.version() == "1.3"  # the server has counted the main connection
    assert cl.scan_counts() == {} and cl.scan_way_counts() == {}
    assert cl.scan_results(5) == []
    assert server.connections == 1 and cl._link is None
    cl.close()


def test_scan_pool_is_capped_to_the_consoles_client_slots(monkeypatch, caplog):
    monkeypatch.setattr(ps5debug_ng, "SCAN_SLICE", 0x100)
    monkeypatch.setattr(ps5debug_ng, "BRIDGE_GAP", 0)
    monkeypatch.setattr(ps5debug_ng, "NG_CLIENT_SLOTS", ps5debug_ng.RESERVED_SLOTS + 2)
    server = FakeNgServer()
    cl = _connect(server, scan_connections=8)
    with caplog.at_level(logging.INFO):
        cl.scan_start(PID, _regions(cl), ValueType.INT32, Compare.EXACT, 100)
    assert len(cl._link._pool) == 2 and "action=scan_pool capped=2" in caplog.text
    cl.close()


def test_default_connections_fit_in_the_consoles_slots():
    # main + link + pool + one extra per unknown type + one Cancel
    most = 2 + (ps5debug_ng.MAX_SCAN_CONNECTIONS - 1) + ps5debug_ng.RESERVED_SLOTS - 3 + 1
    assert most <= ps5debug_ng.NG_CLIENT_SLOTS
