"""Disconnect, Connect again or closing the app while a Pro job is in the game (B1):
the job is stopped and cleans up (the sampler's hook out, the debugger detached)
before the link closes, as Find the real one does. The job runs on a real worker
thread here, so the disconnect really has to wait for it."""

from __future__ import annotations

import threading
import time

import pytest

from cheatmaker.ui.pro import tab as pro_tab
from cheatmaker.ui.pro.tab import JobGuard
from tests import fakegame as fg
from tests.ui.pro.test_watch_tab import HP_ADDR, _add

WAIT_MS = 5000


@pytest.fixture
def threaded(pro):
    """The shared TaskRunner on Qt's thread pool for this test."""
    tasks = pro.watch.tasks
    tasks.sync = False
    yield tasks
    tasks.sync = True


def _hooked(game) -> bool:
    return game.fake.peek(fg.PID, fg.WRITER, len(fg.VMOVSS_W)) != fg.VMOVSS_W


def test_disconnect_during_a_sample_takes_the_hook_out_first(pro, game, qtbot, threaded):
    panel = pro.watch.access
    panel.seconds = 60.0  # only the disconnect ends it
    panel.sleep = lambda _s: time.sleep(0.01)
    panel.sample_at(fg.WRITER)
    qtbot.waitUntil(lambda: _hooked(game), timeout=WAIT_MS)
    assert len(panel.ctl.before_disconnect) >= 1

    panel.ctl.run_before_disconnect()
    # back from the callback: the site holds the game's bytes again, link still open
    assert not _hooked(game) and panel.ctl.session.connected
    qtbot.waitUntil(lambda: not panel.sampling, timeout=WAIT_MS)
    assert panel._guard._stop not in panel.ctl.before_disconnect


def test_disconnect_during_a_watch_detaches_first(pro, game, qtbot, threaded):
    watch = pro.watch
    watch.clock = time.monotonic
    watch.ctl.settings.pro_watch_seconds = 60
    d = game.fake.debugger
    _add(watch, hex(HP_ADDR))
    watch.start_btn.click()
    qtbot.waitUntil(lambda: ("set", 0, HP_ADDR, 4, "write") in d.calls, timeout=WAIT_MS)

    watch.ctl.run_before_disconnect()
    assert d.state == "detached" and d.calls[-1] == ("detach",)
    qtbot.waitUntil(lambda: not watch.watching, timeout=WAIT_MS)
    assert watch._guard._stop not in watch.ctl.before_disconnect
    assert watch.ctl.session.debug_owner is None


class _Ctl:
    def __init__(self) -> None:
        self.before_disconnect: list = []


def test_guard_registers_once_and_removes_itself():
    ctl = _Ctl()
    guard = JobGuard(ctl, "test")
    job = guard.start(threading.Event(), lambda: 7)
    guard.start(threading.Event(), lambda: 8)
    assert ctl.before_disconnect == [guard._stop]
    assert job() == 7 and guard.done.is_set()
    guard.finish()
    assert ctl.before_disconnect == []


def test_guard_stop_without_a_job_does_nothing():
    guard = JobGuard(_Ctl(), "test")
    guard._stop()  # no job: returns at once


def test_guard_wait_is_bounded(monkeypatch):
    monkeypatch.setattr(pro_tab, "STOP_WAIT_S", 0.01)
    guard = JobGuard(_Ctl(), "test")
    cancel = threading.Event()
    guard.start(cancel, lambda: None)  # never run: the job never says it's done
    guard._stop()
    assert cancel.is_set() and not guard.done.is_set()
