"""QA round for the search-freeze fix (PR #36): searches on their own PS5 connection
(the scan link), bounded UI-thread waits (ConsoleBusy), safe turn-off and the crash
recorder. Design: docs/architecture-scan-link.md; console tests C24.
Report: qa/reports/2026-11-21-search-freeze.md.

Bugs are strict xfail tests named after their BUG id; remove the marker once fixed.
The real Ps5DebugNgClient talks to FakeNgServer (gated snapshot / narrow-down), or the
UI runs on FakeConsole whose ``batch()`` is made to answer like a busy PS5 does on the
UI thread (ConsoleBusy before anything is written).
"""

from __future__ import annotations

import contextlib
import logging
import struct
import threading
import time

import pytest
from PySide6.QtCore import Qt

from cheatmaker import logs
from cheatmaker.console import ps5debug_ng
from cheatmaker.console.ps5debug_ng import Cmd, Ps5DebugNgClient
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.scanner import Scanner
from cheatmaker.core.session import Session
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.errors import ConsoleBusy
from cheatmaker.settings import Settings
from cheatmaker.ui import messages
from cheatmaker.ui.cheats_view import COL_ON
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from cheatmaker.ui.watchdog import UiWatchdog
from tests.conftest import HEALTH_ADDR, HEAP_BASE, PID, build_fake
from tests.unit.test_ps5debug_ng import FakeNgServer
from tests.unit.test_scan_link import LINK, MAIN, _Bg, _connect, _regions, _sent, _wait

FAST = 0.5


def _i32_fake(fake, addr):
    return struct.unpack("<i", fake.peek(PID, addr, 4))[0]


def _i32_server(server, addr):
    return struct.unpack("<i", server.fake.read(PID, addr, 4))[0]


@contextlib.contextmanager
def _busy_batch():
    """What the real client's batch() does on the UI thread while the PS5 is busy."""
    raise ConsoleBusy(ps5debug_ng.BUSY_MESSAGE)
    yield  # pragma: no cover


def _fake_win(qtbot, fake):
    ctl = Controller(lambda ip, port: fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()
    assert ctl.session.game is not None
    return w, ctl


def _frozen_health_cheat(ctl, value=999):
    on, off = struct.pack("<i", value), struct.pack("<i", 100)
    cheat = Cheat("Health", [Location(LocationKind.SESSION_ADDRESS, HEALTH_ADDR, on, off)])
    cheat.freeze = True
    ctl.ensure_file().cheats.append(cheat)
    ctl.cheats_changed.emit()
    return cheat


# --- TC-01..TC-04: the window and the freezer during a long search (regression) -----------
def test_tc01_main_connection_free_while_multi_type_snapshot_and_narrow_run():
    """TC-01/TC-02: reads, writes, version on main stay fast during snapshot AND narrow."""
    server = FakeNgServer(snapshot=True)
    cl = _connect(server)
    regions = _regions(cl)
    types = (ValueType.FLOAT, ValueType.INT32)
    server.snapshot_gate.clear()
    bg = _Bg(lambda: cl.scan_start(PID, regions, types, Compare.UNKNOWN, None))
    _wait(lambda: _sent(server, LINK, {Cmd.TURBOSCAN_START}))
    t0 = time.monotonic()
    for _ in range(10):
        cl.read(PID, HEALTH_ADDR, 4)
    cl.write(PID, HEALTH_ADDR, struct.pack("<i", 77))
    assert time.monotonic() - t0 < FAST
    server.snapshot_gate.set()
    bg.join()
    assert bg.error is None
    server.count_gate.clear()
    bg = _Bg(lambda: cl.scan_next(Compare.UNCHANGED, None))
    _wait(lambda: _sent(server, LINK, {Cmd.TURBOSCAN_COUNT}))
    t0 = time.monotonic()
    assert cl.read(PID, HEALTH_ADDR, 4) == struct.pack("<i", 77)
    assert time.monotonic() - t0 < FAST
    server.count_gate.set()
    bg.join()
    assert bg.error is None
    assert not _sent(server, MAIN, {c for c in Cmd if c.name.startswith("TURBOSCAN_")})
    cl.close()


def test_tc05_connection_count_stays_inside_ng_slots(monkeypatch):
    """TC-05: 8 scan connections, an exact search, then a two-type unknown search: the
    PS5 never has more than 11 of our connections open (one slot left for Cancel)."""
    monkeypatch.setattr(ps5debug_ng, "SCAN_SLICE", 0x100)
    monkeypatch.setattr(ps5debug_ng, "BRIDGE_GAP", 0)
    server = FakeNgServer(snapshot=True)
    cl = _connect(server, scan_connections=8)
    regions = _regions(cl)
    assert cl.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100) == 2
    peak = server.connections - len(server.closed)
    server.snapshot_gate.clear()
    types = (ValueType.FLOAT, ValueType.INT32)
    bg = _Bg(lambda: cl.scan_start(PID, regions, types, Compare.UNKNOWN, None))
    _wait(lambda: _sent(server, LINK, {Cmd.TURBOSCAN_START}))
    time.sleep(0.1)
    peak = max(peak, server.connections - len(server.closed))
    server.snapshot_gate.set()
    bg.join()
    assert bg.error is None
    assert peak <= ps5debug_ng.NG_CLIENT_SLOTS - 1, peak
    cl.close()
    _wait(lambda: server.closed == set(range(1, server.connections + 1)), timeout=2.0)


def test_tc06_disconnect_during_a_snapshot_ends_the_search_and_closes_every_socket(qtbot):
    """TC-06: Connect again (or closing) during a running search: the search ends,
    nothing is left open on the PS5, the window answers at once."""
    server = FakeNgServer(snapshot=True)
    ctl = Controller(
        lambda ip, port: Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3),
        Settings.load(),
        TaskRunner(),
    )
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    try:
        w.connect_view.ip.setText("127.0.0.1")
        w.connect_view.connect_btn.click()
        qtbot.waitUntil(lambda: ctl.session.game is not None and not ctl.tasks.busy, timeout=5000)
        qtbot.wait(50)
        server.snapshot_gate.clear()
        w.find.do_unknown()
        qtbot.waitUntil(lambda: _sent(server, LINK, {Cmd.TURBOSCAN_START}), timeout=5000)
        t0 = time.monotonic()
        ctl.session.disconnect()
        assert time.monotonic() - t0 < FAST
        qtbot.waitUntil(lambda: not ctl.tasks.busy, timeout=5000)
        assert not ctl.scanner.active
        server.snapshot_gate.set()
        _wait(lambda: {MAIN, LINK} <= server.closed, timeout=2.0)
    finally:
        server.snapshot_gate.set()
        ctl.shutdown()


def test_tc07_busy_message_is_the_busy_text_not_lost_or_read_failed():
    """TC-07: ConsoleBusy reads "The PS5 is busy right now" and never as a lost link."""
    exc = ConsoleBusy(ps5debug_ng.BUSY_MESSAGE)
    assert messages.user_message(exc) == ps5debug_ng.BUSY_MESSAGE
    assert messages.user_message(exc) != messages.LOST


def test_tc08_turning_off_from_the_table_while_busy_keeps_it_on_and_ticked(qtbot, monkeypatch):
    """TC-08: untick a frozen cheat while the PS5 is busy: it stays on and ticked; the
    busy message is shown; the next try works."""
    fake = build_fake()
    w, ctl = _fake_win(qtbot, fake)
    said: list[str] = []
    ctl.message.connect(said.append)
    cheat = _frozen_health_cheat(ctl)
    w.cheats.table.item(0, COL_ON).setCheckState(Qt.CheckState.Checked)
    assert cheat.enabled and _i32_fake(fake, HEALTH_ADDR) == 999
    monkeypatch.setattr(fake, "batch", _busy_batch)
    w.cheats.table.item(0, COL_ON).setCheckState(Qt.CheckState.Unchecked)
    assert cheat.enabled and _i32_fake(fake, HEALTH_ADDR) == 999
    assert w.cheats.table.item(0, COL_ON).checkState() == Qt.CheckState.Checked
    assert said and said[-1] == ps5debug_ng.BUSY_MESSAGE
    monkeypatch.undo()
    w.cheats.table.item(0, COL_ON).setCheckState(Qt.CheckState.Unchecked)
    assert not cheat.enabled and _i32_fake(fake, HEALTH_ADDR) == 100
    ctl.shutdown()


# --- BUG-01: deleting an ON cheat while busy leaves it frozen with no row -----------------
def test_bug01_delete_while_busy_does_not_leave_an_invisible_frozen_cheat(qtbot, monkeypatch):
    fake = build_fake()
    w, ctl = _fake_win(qtbot, fake)
    cheat = _frozen_health_cheat(ctl)
    w.cheats.table.item(0, COL_ON).setCheckState(Qt.CheckState.Checked)
    assert cheat.enabled and ctl.freezer.keys
    monkeypatch.setattr(fake, "batch", _busy_batch)
    w.cheats.table.selectRow(0)
    w.cheats.delete_btn.click()
    monkeypatch.undo()
    gone = cheat not in ctl.cheat_file.cheats
    still_running = cheat.enabled or id(cheat) in ctl.live.active or ctl.freezer.keys
    # Either the row stays (so it can be turned off later) or the cheat is really off.
    assert not (gone and still_running), (
        f"row gone={gone}, still on={cheat.enabled}, frozen={ctl.freezer.keys}"
    )
    ctl.shutdown()


# --- BUG-02: Lock it's "Test it" forgets a test patch it couldn't take out ---------------
def test_bug02_lock_wizard_untest_while_busy_keeps_track_of_the_test_patch(qtbot, monkeypatch):
    from cheatmaker.ui.lock_wizard import LockWizard

    fake = build_fake()
    _w, ctl = _fake_win(qtbot, fake)
    source = _frozen_health_cheat(ctl)
    wiz = LockWizard(ctl, source)
    qtbot.addWidget(wiz)
    # A tested candidate: a NOP over the writer, on in the game.
    from tests.conftest import WRITER_ADDR

    cand = Cheat(
        "Health lock",
        [Location(LocationKind.SESSION_ADDRESS, WRITER_ADDR, b"\x90\x90", b"\x89\x08")],
    )
    ctl.live.turn_on(cand)
    wiz.candidate = cand
    assert fake.peek(PID, WRITER_ADDR, 2) == b"\x90\x90"
    monkeypatch.setattr(fake, "batch", _busy_batch)
    wiz._untest()  # picking another option / Test again / closing does this
    monkeypatch.undo()
    patched = fake.peek(PID, WRITER_ADDR, 2) == b"\x90\x90"
    # Busy changes nothing: the wizard must still know the patch it put in.
    assert not patched or wiz.candidate is cand
    ctl.shutdown()


# --- TC-10: a game change during a first search -----------------------------------------
def _restart_game(server, new_pid):
    """The player restarted the game: the old process is gone, a new one runs."""
    from tests.conftest import CODE_SIZE, MODULE_BASE

    f = server.fake
    old_heap = f.read(PID, HEAP_BASE, 0x1000)
    f.add_process(new_pid, "eboot.bin", "PPSA01234", "Test Game", "01.000")
    f.add_region(new_pid, "eboot.bin", MODULE_BASE, b"\xcc" * CODE_SIZE, 0x5)
    f.add_region(new_pid, "[anon]", HEAP_BASE, old_heap, 0x3)
    f.remove_process(PID)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_tc10_refresh_during_a_first_search_answers_at_once_and_leaves_no_search():
    """TC-10: Refresh after a game restart no longer waits behind the snapshot; the old
    run's search does not become the new run's (the old process is gone, so the fake's
    snapshot fails like a dropped link). See the report's risk note on a console
    snapshot that still completes."""
    server = FakeNgServer(snapshot=True)
    session = Session(lambda ip, port: _connect(server))
    session.connect("127.0.0.1")
    session.refresh_game()
    sc = Scanner(session)
    server.snapshot_gate.clear()
    bg = _Bg(lambda: sc.first_scan(None, ValueType.INT32))
    _wait(lambda: _sent(server, LINK, {Cmd.TURBOSCAN_START}))
    time.sleep(0.05)
    _restart_game(server, PID + 1)
    t0 = time.monotonic()
    game = session.refresh_game()
    assert time.monotonic() - t0 < FAST and game.pid == PID + 1
    server.snapshot_gate.set()
    bg.join()
    assert bg.error is not None and not sc.active and session.connected
    _wait(lambda: LINK in server.closed, timeout=2.0)  # the fake's thread has ended
    session.disconnect()


# --- BUG-03: editing a frozen cheat's value while busy -----------------------------------
def test_bug03_value_edit_while_busy_does_not_show_a_value_that_is_not_frozen(qtbot, monkeypatch):
    from cheatmaker.ui.cheats_view import COL_VALUE

    fake = build_fake()
    w, ctl = _fake_win(qtbot, fake)
    on, off = struct.pack("<i", 999), struct.pack("<i", 100)
    cheat = Cheat(
        "Health",
        [Location(LocationKind.SESSION_ADDRESS, HEALTH_ADDR, on, off)],
        freeze=True,
        value_type=ValueType.INT32,
    )
    ctl.ensure_file().cheats.append(cheat)
    ctl.cheats_changed.emit()
    w.cheats.table.item(0, COL_ON).setCheckState(Qt.CheckState.Checked)
    assert cheat.enabled and _i32_fake(fake, HEALTH_ADDR) == 999
    monkeypatch.setattr(fake, "batch", _busy_batch)
    w.cheats.table.item(0, COL_VALUE).setText("500")  # the player types a new value
    monkeypatch.undo()
    fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 40))  # took a hit
    ctl.freezer.tick()  # the freezer's next round, the PS5 free again
    shown = w.cheats.table.item(0, COL_VALUE).text()
    in_game = _i32_fake(fake, HEALTH_ADDR)
    assert shown == str(in_game), f"table shows {shown}, the game is held at {in_game}"
    ctl.shutdown()


# --- TC-15: Start over during a running Went down search (C24b) --------------------------
def test_bug04_start_over_can_be_pressed_during_a_running_search(qtbot):
    server = FakeNgServer(snapshot=True)
    ctl = Controller(
        lambda ip, port: Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3),
        Settings.load(),
        TaskRunner(),
    )
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    try:
        w.connect_view.ip.setText("127.0.0.1")
        w.connect_view.connect_btn.click()
        qtbot.waitUntil(lambda: ctl.session.game is not None and not ctl.tasks.busy, timeout=5000)
        qtbot.wait(50)
        w.find.do_unknown()
        qtbot.waitUntil(lambda: not ctl.tasks.busy and ctl.scanner.active, timeout=5000)
        server.count_gate.clear()
        w.find.down_btn.click()  # Went down: runs on the link
        qtbot.waitUntil(lambda: _sent(server, LINK, {Cmd.TURBOSCAN_COUNT}), timeout=5000)
        assert w.find.new_btn.isEnabled(), "Start over can't be pressed during the search"
    finally:
        server.count_gate.set()
        server.snapshot_gate.set()
        ctl.shutdown()


# --- BUG-05 / crash recorder ------------------------------------------------------------
def _log_line(msg: str) -> str:
    return f"2026-11-21 10:00:00,000 INFO cheatmaker.app [PPSA01234 01.000] {msg}\n"


def test_tc20_clean_previous_run_is_not_reported(tmp_path):
    p = tmp_path / "cheatmaker.log"
    p.write_text(
        _log_line("action=start version=1 log=x")
        + _log_line("x" * 100) * 50
        + _log_line("action=exit code=0")
    )
    assert logs.previous_run_ended_unexpectedly(p) is False


def test_tc21_short_crashed_run_is_reported(tmp_path):
    p = tmp_path / "cheatmaker.log"
    p.write_text(_log_line("action=start version=1 log=x") + _log_line("y" * 100) * 50)
    assert logs.previous_run_ended_unexpectedly(p) is True


def test_bug05_long_crashed_run_is_still_reported(tmp_path):
    p = tmp_path / "cheatmaker.log"
    lines = _log_line("action=scan_stats requests=1 ranges=1 mib=1 " + "z" * 150)
    body = lines * (logs.RUN_CHECK_BYTES // len(lines) + 50)  # a long session's lines
    p.write_text(_log_line("action=start version=1 log=x") + body)  # no exit: it crashed
    assert logs.previous_run_ended_unexpectedly(p) is True


def test_tc22_crash_log_handled_dumps_before_exit_are_ignored(tmp_path):
    p = tmp_path / "crash.log"
    p.write_text(
        f"{logs.CRASH_HEADER} 2026-11-21 app=1 pid=1\n"
        "Windows fatal exception: code 0xc0000005\n\nThread 0x01 (most recent call first):\n"
        f"{logs.CRASH_EXIT} 2026-11-21\n"
    )
    assert logs.previous_native_crash(p, ended_unexpectedly=False) is None
    assert logs.previous_native_crash(p, ended_unexpectedly=True) is None


def test_tc23_crash_log_crash_without_exit_is_reported_only_if_run_ended_badly(tmp_path):
    p = tmp_path / "crash.log"
    p.write_text(
        f"{logs.CRASH_HEADER} 2026-11-21 app=1 pid=1\n"
        "Fatal Python error: Segmentation fault\n\nCurrent thread 0x01 (most recent call first):\n"
    )
    assert logs.previous_native_crash(p, ended_unexpectedly=False) is None
    assert "Segmentation fault" in (logs.previous_native_crash(p, ended_unexpectedly=True) or "")


def test_tc24_teardown_crash_after_exit_marker_is_reported(tmp_path):
    p = tmp_path / "crash.log"
    p.write_text(
        f"{logs.CRASH_HEADER} 2026-11-21 app=1 pid=1\n{logs.CRASH_EXIT} 2026-11-21\n"
        "Windows fatal exception: access violation\n\nCurrent thread 0x01:\n"
    )
    assert "access violation" in (logs.previous_native_crash(p, ended_unexpectedly=False) or "")


# --- BUG-06: watchdog says the UI unfroze after a sleep gap while it is still frozen ------
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_bug06_watchdog_no_false_unfrozen_after_sleep_gap(qtbot, caplog):
    clock = _Clock()
    wd = UiWatchdog(freeze_s=15.0, clock=clock)
    wd.beat()
    for _ in range(9):  # UI stuck: no beats; checks every 2 s
        clock.t += 2.0
        wd.check()
    with caplog.at_level(logging.INFO, logger="cheatmaker.watchdog"):
        assert "ui_frozen" in caplog.text or wd._frozen_since is not None
        caplog.clear()
        clock.t += 600.0  # the PC slept; the UI is still stuck afterwards
        wd.check()
        clock.t += 2.0
        wd.check()
    assert "ui_unfrozen" not in caplog.text, caplog.text


def test_tc25_watchdog_reports_a_freeze_once_and_the_recovery(qtbot, caplog):
    clock = _Clock()
    wd = UiWatchdog(freeze_s=15.0, clock=clock)
    wd.beat()
    with caplog.at_level(logging.INFO, logger="cheatmaker.watchdog"):
        for _ in range(12):
            clock.t += 2.0
            wd.check()
        assert caplog.text.count("action=ui_frozen") == 1
        wd.beat()
        clock.t += 2.0
        wd.check()
    assert caplog.text.count("action=ui_unfrozen") == 1


# --- TC-09: the freezer keeps writing while the UI thread gets ConsoleBusy ----------------
def test_tc09_freezer_thread_never_gets_console_busy():
    """The freezer runs on its own thread: it waits (as before) instead of skipping."""
    from cheatmaker.core.freezer import Freezer

    server = FakeNgServer()
    session = Session(lambda ip, port: _connect(server))
    session.connect("127.0.0.1")
    session.refresh_game()
    cl = session.client
    held, release = threading.Event(), threading.Event()

    def hold():
        with cl._lock:
            held.set()
            release.wait(5)

    holder = _Bg(hold)
    held.wait(2)
    fz = Freezer(session, hz=20)
    fz.freeze("health", [(PID, HEALTH_ADDR, struct.pack("<i", 321))])
    time.sleep(2.0)  # longer than MAIN_THREAD_WAIT
    release.set()
    holder.join()
    _wait(lambda: _i32_server(server, HEALTH_ADDR) == 321, timeout=2.0)
    assert fz.keys == {"health"}, "not turned off by a busy moment"
    fz.stop()
    session.disconnect()


# --- BUG-07: a new search on a link that died while idle fails once ----------------------
def test_bug07_new_search_after_the_idle_link_dropped_opens_a_new_link():
    server = FakeNgServer()
    cl = _connect(server, scan_connections=1)
    regions = _regions(cl)
    assert cl.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100) == 2
    cl._link._w.request(0xDEAD)  # the fake hangs up this connection: the link is gone
    _wait(lambda: LINK in server.closed)
    try:
        assert cl.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100) == 2
    finally:
        cl.close()


# --- BUG-08: a deferred Start over wipes a search started while it is applied ------------
def test_bug08_deferred_cancel_after_a_pointer_pass_does_not_wipe_a_new_search():
    server = FakeNgServer(snapshot=True)
    cl = _connect(server, scan_connections=1)
    regions = _regions(cl)
    cl.scan_start(PID, regions, ValueType.INT32, Compare.UNKNOWN, None)
    link = cl._link
    server.scan_gate.clear()
    lo, hi = HEALTH_ADDR - 0x1000, HEALTH_ADDR
    bg = _Bg(lambda: cl.find_pointers(PID, regions, lo, hi, lambda v: True))
    _wait(lambda: server.conn_commands[LINK].count(Cmd.TURBOSCAN_START) >= 2)
    cl.scan_cancel()  # deferred: the pointer pass is running
    in_catch_up, go = threading.Event(), threading.Event()
    real = link._cancel_here

    def slow_catch_up():  # widen the window: the pass thread applies the Start over
        if threading.current_thread() is bg.thread:
            in_catch_up.set()
            go.wait(5)
        real()

    link._cancel_here = slow_catch_up
    server.scan_gate.set()
    assert in_catch_up.wait(5)
    new = _Bg(lambda: cl.scan_start(PID, regions, ValueType.INT32, Compare.EXACT, 100))
    time.sleep(0.3)
    go.set()
    new.join()
    bg.join()
    try:
        assert new.error is None and new.result == 2
        assert cl.scan_counts() == {ValueType.INT32: 2}, "the new search was wiped"
    finally:
        cl.close()


# --- Re-test of 16b8499 -----------------------------------------------------------------
def _ng_win(qtbot, server, **kw):
    ctl = Controller(
        lambda ip, port: Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3, **kw),
        Settings.load(),
        TaskRunner(),
    )
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("127.0.0.1")
    w.connect_view.connect_btn.click()
    qtbot.waitUntil(lambda: ctl.session.game is not None and not ctl.tasks.busy, timeout=5000)
    qtbot.wait(50)
    return w, ctl


def test_rt01_start_over_stops_a_running_first_search_at_once_and_a_new_one_works(qtbot):
    """RT-01 (BUG-04 decision): Start over during a running first unknown search answers
    at once, leaves no search and no error, and the next search keeps its result."""
    server = FakeNgServer(snapshot=True)
    w, ctl = _ng_win(qtbot, server)
    said: list[str] = []
    ctl.message.connect(said.append)
    try:
        server.snapshot_gate.clear()
        w.find.do_unknown()
        qtbot.waitUntil(lambda: _sent(server, LINK, {Cmd.TURBOSCAN_START}), timeout=5000)
        assert w.find.new_btn.isEnabled()
        t0 = time.monotonic()
        w.find.new_btn.click()
        assert time.monotonic() - t0 < FAST
        # Review round 4: Search waits until the stopped search's worker has ended.
        assert not ctl.scanner.active and not w.find.search_btn.isEnabled()
        qtbot.waitUntil(lambda: not ctl.tasks.busy, timeout=5000)  # ScanCancelled, dropped
        server.snapshot_gate.set()
        assert not ctl.scanner.active and said == [], said
        assert w.find.search_btn.isEnabled()
        w.find.number.setText("100")
        w.find.search_btn.click()
        qtbot.waitUntil(lambda: not ctl.tasks.busy and ctl.scanner.active, timeout=5000)
        assert ctl.scanner.count == 2
    finally:
        server.snapshot_gate.set()
        ctl.shutdown()


def test_rt02_start_over_is_not_offered_during_check_which_is_real(qtbot):
    """RT-02 (BUG-04 decision): a running check must put its test values back first."""
    fake = build_fake()
    w, ctl = _fake_win(qtbot, fake)
    w.find.number.setText("100")
    w.find.search_btn.click()
    assert ctl.scanner.active
    w.find._cancel, w.find._checking = threading.Event(), True  # a check is running
    w.find._update_buttons()
    assert not w.find.new_btn.isEnabled()
    w.find._cancel, w.find._checking = None, False
    w.find._update_buttons()
    assert w.find.new_btn.isEnabled()
    ctl.shutdown()


def test_bug09_live_list_does_not_wait_on_the_link_during_a_pointer_pass(qtbot):
    server = FakeNgServer(snapshot=True)
    w, ctl = _ng_win(qtbot, server, scan_connections=1)
    try:
        w.find.do_unknown()  # few matches on the fake game: the live list shows them
        qtbot.waitUntil(lambda: not ctl.tasks.busy and ctl.scanner.active, timeout=5000)
        assert ctl.scanner.few_left
        cl = ctl.session.client
        regions = _regions(cl)
        server.scan_gate.clear()
        lo, hi = HEALTH_ADDR - 0x1000, HEALTH_ADDR
        bg = _Bg(lambda: cl.find_pointers(PID, regions, lo, hi, lambda v: True))
        _wait(lambda: server.conn_commands[LINK].count(Cmd.TURBOSCAN_START) >= 2)
        t0 = time.monotonic()
        w.find.refresh_live()  # one live-list tick while the pointer pass runs
        took = time.monotonic() - t0
        server.scan_gate.set()
        bg.join()
        assert took < FAST, f"the window waited {took:.1f} s"
    finally:
        server.scan_gate.set()
        ctl.shutdown()
