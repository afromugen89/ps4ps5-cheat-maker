"""The window stays usable while a long "I don't know the number" snapshot runs
(docs/architecture-scan-link.md, console test C24).

The real PS5Debug-NG client talks to the fake server; its snapshot is held by a gate, so
the search is "running" while the Find view's live list, find-it-again's check and the
freezer tick on the UI and freezer threads.
"""

from __future__ import annotations

import struct
import threading
import time

from cheatmaker.console.ps5debug_ng import Cmd, Ps5DebugNgClient
from cheatmaker.core.values import ValueType
from cheatmaker.settings import Settings
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests.conftest import HEALTH_ADDR, PID
from tests.unit.test_ps5debug_ng import FakeNgServer

FAST = 0.5


def _i32(server, addr):
    return struct.unpack("<i", server.fake.read(PID, addr, 4))[0]


def test_window_and_freezer_keep_working_during_a_snapshot(qtbot):
    server = FakeNgServer(snapshot=True)
    ctl = Controller(
        lambda ip, port: Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3),
        Settings.load(),
        TaskRunner(),  # searches run on a worker thread, as in the app
    )
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    said: list[str] = []
    ctl.message.connect(said.append)
    try:
        w.connect_view.ip.setText("127.0.0.1")
        w.connect_view.connect_btn.click()
        qtbot.waitUntil(lambda: ctl.session.game is not None and not ctl.tasks.busy, timeout=5000)
        qtbot.wait(50)  # the "new game" signals have reached the views
        w.find.manual[HEALTH_ADDR] = ValueType.INT32  # a live value on the Find view
        ctl.freezer.freeze("health", [(PID, HEALTH_ADDR, struct.pack("<i", 100))])

        server.snapshot_gate.clear()
        w.find.do_unknown()
        qtbot.waitUntil(
            lambda: Cmd.TURBOSCAN_START in server.conn_commands.get(2, []), timeout=5000
        )
        t0 = time.monotonic()
        w.find.refresh_live()
        w.cheats.finder.check_pending()
        assert time.monotonic() - t0 < FAST
        assert [m.address for m in w.find.rows] == [HEALTH_ADDR], said

        server.fake.poke(PID, HEALTH_ADDR, struct.pack("<i", 40))  # took damage
        qtbot.waitUntil(lambda: _i32(server, HEALTH_ADDR) == 100, timeout=1000)
        assert ctl.tasks.busy, "the snapshot is still running"

        server.snapshot_gate.set()
        qtbot.waitUntil(lambda: not ctl.tasks.busy, timeout=5000)
        assert ctl.scanner.active and ctl.session.connected
    finally:
        server.snapshot_gate.set()
        ctl.shutdown()


def test_live_list_keeps_its_match_rows_while_a_search_runs(qtbot, monkeypatch):
    """Review MAJOR 3: no scan_results from the UI thread while a search may be running."""
    from tests.conftest import build_fake

    fake = build_fake()
    ctl = Controller(lambda ip, port: fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    try:
        w.connect_view.ip.setText("192.168.1.50")
        w.connect_view.connect_btn.click()
        w.find.number.setText("100")
        w.find.search_btn.click()
        before = [m.address for m in w.find.rows]
        assert len(before) == 2

        def no_results(_limit):
            raise AssertionError("asked the console for the matches during a task")

        monkeypatch.setattr(fake, "scan_results", no_results)
        monkeypatch.setattr(fake, "search_running", lambda: True, raising=False)
        w.find.refresh_live()
        assert [m.address for m in w.find.rows] == before and w.find.timer.isActive()
        monkeypatch.undo()  # re-review m3: any other task doesn't stop live values
        monkeypatch.setattr(ctl.tasks, "_running", 1)
        w.find.refresh_live()
        assert len(w.find.rows) == 2
    finally:
        ctl.shutdown()


def test_live_list_with_pointer_rows_gives_up_quickly_when_busy(qtbot):
    """Review MAJOR 4: five pointer rows don't each wait for a busy PS5."""
    from cheatmaker.core.pointers import Chain

    server = FakeNgServer()
    ctl = Controller(
        lambda ip, port: Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3),
        Settings.load(),
        TaskRunner(),
    )
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    said: list[str] = []
    ctl.message.connect(said.append)
    try:
        w.connect_view.ip.setText("127.0.0.1")
        w.connect_view.connect_btn.click()
        qtbot.waitUntil(lambda: ctl.session.game is not None and not ctl.tasks.busy, timeout=5000)
        qtbot.wait(50)
        for i in range(5):
            w.find.pointers[Chain(0x100 + 8 * i, (0x10,))] = ValueType.INT32
        held, release = threading.Event(), threading.Event()

        def hold():
            with ctl.session.client._lock:
                held.set()
                release.wait(10)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        held.wait(2)
        try:
            t0 = time.monotonic()
            w.find.refresh_live()
            w.find.refresh_live()  # the next tick too
            assert time.monotonic() - t0 < 2.5
        finally:
            release.set()
            t.join(2)
        assert said == [], "no error message for a busy moment"
        assert w.find.timer.isActive() and ctl.session.connected
    finally:
        ctl.shutdown()


def test_start_over_stops_a_running_search_and_drops_its_late_answer(qtbot):
    """QA BUG-04 / C24b: Start over during a Went down scan answers at once; the scan
    ends (ScanCancelled) and nothing it leaves shows up."""
    server = FakeNgServer(snapshot=True)
    ctl = Controller(
        lambda ip, port: Ps5DebugNgClient.connect("127.0.0.1", server.port, timeout=3),
        Settings.load(),
        TaskRunner(),
    )
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    said: list[str] = []
    ctl.message.connect(said.append)
    try:
        w.connect_view.ip.setText("127.0.0.1")
        w.connect_view.connect_btn.click()
        qtbot.waitUntil(lambda: ctl.session.game is not None and not ctl.tasks.busy, timeout=5000)
        qtbot.wait(50)
        w.find.do_unknown()
        qtbot.waitUntil(lambda: not ctl.tasks.busy and ctl.scanner.active, timeout=5000)
        server.count_gate.clear()
        w.find.down_btn.click()
        qtbot.waitUntil(
            lambda: Cmd.TURBOSCAN_COUNT in server.conn_commands.get(2, []), timeout=5000
        )
        t0 = time.monotonic()
        w.find.new_btn.click()
        assert time.monotonic() - t0 < FAST
        assert not ctl.scanner.active and w.find.count.text() == ""
        # Review round 4: nothing new starts while the stopped scan's worker still runs,
        # and its progress can't move the bar.
        f = w.find
        assert not f.search_btn.isEnabled() and not f.unknown_btn.isEnabled()
        assert f.new_btn.isEnabled()
        f._scan_progress.done.emit((f._scan_gen - 1, 0.5))
        qtbot.wait(20)
        assert f.progress.value() == 0
        server.count_gate.set()
        qtbot.waitUntil(lambda: not ctl.tasks.busy, timeout=5000)  # the scan ended
        qtbot.wait(50)
        assert not ctl.scanner.active and w.find.count.text() == "" and said == []
        assert ctl.session.connected and w.find.unknown_btn.isEnabled()
        assert f._stopping == 0 and f.search_btn.isEnabled()
        f.number.setText("100")
        f.search_btn.click()
        qtbot.waitUntil(lambda: not ctl.tasks.busy and ctl.scanner.active, timeout=5000)
        qtbot.wait(50)
        assert ctl.scanner.active and ctl.scanner.count == 2, said
    finally:
        server.count_gate.set()
        ctl.shutdown()


def test_lock_wizard_close_while_busy_keeps_the_test_patch_and_retries(qtbot, monkeypatch):
    """QA BUG-02: closing while the PS5 is busy doesn't leave a test patch nobody
    tracks; the window stays and the next close takes it out."""
    import contextlib

    from cheatmaker.core.cheats import Cheat, Location, LocationKind
    from cheatmaker.errors import ConsoleBusy
    from cheatmaker.ui.lock_wizard import LockWizard
    from tests.conftest import WRITER_ADDR, build_fake

    fake = build_fake()
    ctl = Controller(lambda ip, port: fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()
    source = Cheat(
        "Health",
        [Location(LocationKind.SESSION_ADDRESS, HEALTH_ADDR, b"\x01\0\0\0", b"\x64\0\0\0")],
    )
    ctl.ensure_file().cheats.append(source)
    wiz = LockWizard(ctl, source)
    qtbot.addWidget(wiz)
    wiz.show()
    cand = Cheat(
        "Health lock",
        [Location(LocationKind.SESSION_ADDRESS, WRITER_ADDR, b"\x90\x90", b"\x89\x08")],
    )
    ctl.live.turn_on(cand)
    wiz.candidate = cand

    @contextlib.contextmanager
    def busy():
        raise ConsoleBusy("busy")
        yield

    with monkeypatch.context() as m:
        m.setattr(fake, "batch", busy)
        wiz.reject()
    assert wiz.isVisible() and wiz.candidate is cand
    assert fake.peek(PID, WRITER_ADDR, 2) == b"\x90\x90"
    wiz.reject()
    assert not wiz.isVisible() and wiz.candidate is None
    assert fake.peek(PID, WRITER_ADDR, 2) == b"\x89\x08"
    ctl.shutdown()
