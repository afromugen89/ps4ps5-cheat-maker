"""The window stays usable while a long "I don't know the number" snapshot runs
(docs/architecture-scan-link.md, console test C24).

The real PS5Debug-NG client talks to the fake server; its snapshot is held by a gate, so
the search is "running" while the Find view's live list, find-it-again's check and the
freezer tick on the UI and freezer threads.
"""

from __future__ import annotations

import struct
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
