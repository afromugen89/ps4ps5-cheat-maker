"""UI freeze watchdog: driven by a fake clock and direct check() calls, no sleeping."""

from __future__ import annotations

import logging
import threading

import pytest

from cheatmaker.ui import watchdog as wd


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def dog(qapp, clock):
    d = wd.UiWatchdog(freeze_s=15, clock=clock)
    yield d
    d.stop()


def advance(dog, clock, seconds):
    """Let ``seconds`` pass the way the watchdog thread sees it: a check every CHECK_S
    (much bigger steps look like the PC sleeping, which is skipped)."""
    end = clock.t + seconds
    while clock.t < end:
        clock.t = min(end, clock.t + wd.CHECK_S)
        dog.check()


def _messages(caplog):
    return [r for r in caplog.records if r.name == "cheatmaker.watchdog"]


def test_no_warning_while_heartbeat_is_fresh(dog, clock, caplog):
    with caplog.at_level(logging.INFO, logger="cheatmaker.watchdog"):
        for _ in range(30):
            clock.t += 1
            dog.beat()
            dog.check()
        advance(dog, clock, 15)  # exactly at the threshold is not a freeze yet
    assert _messages(caplog) == []


def test_freeze_logged_once_with_all_thread_stacks(dog, clock, caplog, monkeypatch):
    flushed = []
    monkeypatch.setattr(wd, "flush_handlers", lambda: flushed.append(True))
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, name="scan-worker", daemon=True)
    worker.start()
    try:
        with caplog.at_level(logging.INFO, logger="cheatmaker.watchdog"):
            advance(dog, clock, 16)
            advance(dog, clock, 10)  # still frozen: no second warning
    finally:
        stop.set()
        worker.join()
    [rec] = _messages(caplog)
    msg = rec.getMessage()
    assert rec.levelno == logging.WARNING and "action=ui_frozen seconds=16" in msg
    assert "scan-worker" in msg and "MainThread" in msg
    assert "test_freeze_logged_once_with_all_thread_stacks" in msg, "main thread's stack"
    assert flushed == [True]


def test_unfrozen_logged_when_heartbeat_moves(dog, clock, caplog, monkeypatch):
    monkeypatch.setattr(wd, "flush_handlers", lambda: None)
    with caplog.at_level(logging.INFO, logger="cheatmaker.watchdog"):
        advance(dog, clock, 20)
        advance(dog, clock, 22)
        dog.beat()  # UI thread is back
        dog.check()
        dog.check()  # nothing more
        advance(dog, clock, 16)  # a new freeze is reported again
    recs = _messages(caplog)
    assert [r.levelno for r in recs] == [logging.WARNING, logging.INFO, logging.WARNING]
    assert "action=ui_unfrozen seconds=42" in recs[1].getMessage()


def test_check_errors_do_not_kill_the_thread(dog, caplog, monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bad")
        dog._stop.set()

    monkeypatch.setattr(dog, "check", boom)
    monkeypatch.setattr(wd, "CHECK_S", 0)
    with caplog.at_level(logging.ERROR, logger="cheatmaker.watchdog"):
        dog._run()
    assert len(calls) == 2
    assert any("action=watchdog_failed" in r.getMessage() for r in caplog.records)


def test_start_and_stop(dog, clock, qtbot):
    dog.start()
    thread = dog._thread
    assert thread is not None and thread.daemon and thread.is_alive()
    assert dog._timer.isActive()
    dog.start()  # second start is a no-op
    assert dog._thread is thread
    clock.t += 5
    qtbot.waitUntil(lambda: dog._heartbeat == clock.t, timeout=3000)  # timer beats
    dog.stop()
    assert not thread.is_alive() and not dog._timer.isActive() and dog._thread is None


def test_all_thread_stacks_names_threads():
    text = wd.all_thread_stacks()
    assert "--- thread MainThread" in text and "test_all_thread_stacks_names_threads" in text


def test_a_long_gap_between_checks_is_the_pc_sleeping(dog, clock, caplog, monkeypatch):
    monkeypatch.setattr(wd, "flush_handlers", lambda: None)
    with caplog.at_level(logging.INFO, logger="cheatmaker.watchdog"):
        dog.check()
        clock.t += 3600  # the PC slept for an hour
        dog.check()
        advance(dog, clock, 10)  # the UI beats again soon after waking...
        dog.beat()
        advance(dog, clock, 10)
    recs = _messages(caplog)
    assert [r.levelno for r in recs] == [logging.INFO]
    assert "action=watchdog_skip gap=3600" in recs[0].getMessage()
    advance(dog, clock, 16)  # ...and a real freeze after waking is still seen
    assert "action=ui_frozen" in _messages(caplog)[-1].getMessage()


def test_freeze_right_after_a_sleep_counts_its_seconds_from_the_wake_up(
    dog, clock, caplog, monkeypatch
):
    """Review round 4: the logged seconds don't include the hour the PC slept."""
    monkeypatch.setattr(wd, "flush_handlers", lambda: None)
    with caplog.at_level(logging.INFO, logger="cheatmaker.watchdog"):
        dog.check()
        clock.t += 3600  # the PC slept; the UI never beats after waking
        dog.check()
        advance(dog, clock, 16)
    frozen = [r.getMessage() for r in _messages(caplog) if "ui_frozen" in r.getMessage()]
    assert len(frozen) == 1 and "seconds=16 " in frozen[0], frozen
