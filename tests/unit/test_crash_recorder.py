"""Crash recorder: native crash log, clean-exit marker, Qt messages, app wiring."""

from __future__ import annotations

import faulthandler
import logging
import sys
import types

import pytest
from PySide6.QtCore import QtMsgType

from cheatmaker import logs


def _flush():
    for h in logging.getLogger().handlers:
        h.flush()


@pytest.fixture
def main_log():
    """The real rotating log under the test's CHEATMAKER_HOME; removed afterwards."""
    path = logs.setup_logging()
    try:
        yield path
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_cheatmaker", False):
                root.removeHandler(h)
                h.close()


@pytest.fixture
def native_log():
    """Undo install_native_crash_log and give faulthandler back to pytest's stderr."""
    was_enabled = faulthandler.is_enabled()
    yield
    logs.uninstall_native_crash_log()
    if was_enabled:
        faulthandler.enable(file=sys.__stderr__, all_threads=True)


def _main_log_text(path) -> str:
    _flush()
    return path.read_text(encoding="utf-8")


WINDOWS_CRASH = (
    "Windows fatal exception: access violation\n\n"
    "Thread 0x00001a2c (most recent call first):\n"
    '  File "scanner.py", line 88 in _scan_chunk\n\n'
    "Current thread 0x00002b10 (most recent call first):\n"
    '  File "app.py", line 52 in main\n'
)


# --- native crash log ---------------------------------------------------------


def test_native_crash_log_writes_header_and_enables_faulthandler(main_log, native_log):
    path = logs.install_native_crash_log()
    assert path == logs.logs_dir() / "crash.log"
    assert faulthandler.is_enabled()
    text = path.read_text(encoding="utf-8")
    assert text.startswith(logs.CRASH_HEADER)
    assert f"app={logs.__version__}" in text and "pid=" in text
    assert logs.install_native_crash_log() == path, "second call is a no-op"
    assert path.read_text(encoding="utf-8").count(logs.CRASH_HEADER) == 1
    assert "action=previous_crash" not in _main_log_text(main_log)


def test_native_crash_log_receives_faulthandler_dump(main_log, native_log):
    path = logs.install_native_crash_log()
    faulthandler.dump_traceback(file=logs._crash_file, all_threads=True)
    assert "Current thread" in path.read_text(encoding="utf-8")


def test_previous_native_crash_reported_to_main_log(main_log, native_log):
    path = logs.crash_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{logs.CRASH_HEADER} 2026-11-20 10:00:00 app=0.1.0 pid=1\n"
        "Fatal Python error: old crash, already reported\n"
        f"{logs.CRASH_HEADER} 2026-11-21 10:00:00 app=0.1.0 pid=2\n" + WINDOWS_CRASH,
        encoding="utf-8",
    )
    logs.install_native_crash_log()
    text = _main_log_text(main_log)
    assert "action=previous_crash" in text and "WARNING" in text
    assert "access violation" in text and "_scan_chunk" in text
    assert "old crash" not in text, "only the section after the last header"
    # The next start sees a clean section and stays quiet.
    logs.uninstall_native_crash_log()
    main_log.write_text("", encoding="utf-8")
    logs.install_native_crash_log()
    assert "action=previous_crash" not in _main_log_text(main_log)


def test_previous_native_crash_tail_only():
    path = logs.crash_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = "".join(f'  File "f.py", line {i} in fn{i}\n' for i in range(200))
    path.write_text(f"{logs.CRASH_HEADER} x\n{WINDOWS_CRASH}{frames}", encoding="utf-8")
    tail = logs.previous_native_crash(path)
    assert tail is not None
    lines = tail.splitlines()
    assert len(lines) == logs.CRASH_TAIL_LINES and lines[-1].endswith("fn199")


def test_previous_native_crash_none_cases(tmp_path):
    assert logs.previous_native_crash(tmp_path / "missing.log") is None
    clean = tmp_path / "clean.log"
    clean.write_text(f"{logs.CRASH_HEADER} x\n", encoding="utf-8")
    assert logs.previous_native_crash(clean) is None
    no_header = tmp_path / "no_header.log"
    no_header.write_text(WINDOWS_CRASH, encoding="utf-8")
    assert "access violation" in logs.previous_native_crash(no_header)


def test_crash_log_trimmed_when_too_big(main_log, native_log):
    path = logs.crash_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = "x" * 99 + "\n"
    path.write_text(line * (logs.CRASH_MAX_BYTES // len(line) + 10), encoding="utf-8")
    logs.install_native_crash_log()
    data = path.read_bytes()
    assert len(data) <= logs.CRASH_KEEP_BYTES + 200
    first, header = data.splitlines()[0], data.splitlines()[-1]
    assert first == b"x" * 99, "starts on a whole line"
    assert header.startswith(logs.CRASH_HEADER.encode())


def test_crash_log_small_file_untouched(native_log):
    path = logs.crash_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{logs.CRASH_HEADER} old\n", encoding="utf-8")
    logs._trim_crash_log(path)
    assert path.read_text(encoding="utf-8") == f"{logs.CRASH_HEADER} old\n"


# --- clean exit marker ----------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", False),
        ("x INFO cheatmaker.app: action=start version=0.1.0 log=l\n", True),
        (
            "x INFO cheatmaker.app: action=start version=0.1.0 log=l\n"
            "x INFO cheatmaker.app: action=exit code=0\n",
            False,
        ),
        (
            "x INFO cheatmaker.app: action=start version=0.1.0 log=l\n"
            "x INFO cheatmaker.app: action=exit code=0\n"
            "x INFO cheatmaker.app: action=start version=0.1.0 log=l\n"
            "x INFO scan: action=scan n=3\n",
            True,
        ),
        ("x INFO scan: action=scan n=3\n", False),
    ],
)
def test_previous_run_ended_unexpectedly(tmp_path, content, expected):
    path = tmp_path / "cheatmaker.log"
    path.write_text(content, encoding="utf-8")
    assert logs.previous_run_ended_unexpectedly(path) is expected


def test_previous_run_missing_log(tmp_path):
    assert logs.previous_run_ended_unexpectedly(tmp_path / "nope.log") is False


def test_previous_run_reads_only_the_tail(tmp_path):
    path = tmp_path / "cheatmaker.log"
    filler = "x INFO scan: action=scan n=3\n" * (logs.RUN_CHECK_BYTES // 20)
    path.write_text("x action=start version=0\n" + filler, encoding="utf-8")
    assert logs.previous_run_ended_unexpectedly(path) is False, "start rolled out of the tail"


def test_check_previous_run_logs_warning(main_log):
    main_log.write_text("x INFO cheatmaker.app: action=start version=0.1.0 log=l\n")
    logs.check_previous_run()
    assert "WARNING" in _main_log_text(main_log).splitlines()[-1]
    assert "action=previous_run_ended_unexpectedly" in _main_log_text(main_log)


# --- Qt messages ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "level"),
    [
        (QtMsgType.QtWarningMsg, logging.WARNING),
        (QtMsgType.QtCriticalMsg, logging.ERROR),
        (QtMsgType.QtFatalMsg, logging.CRITICAL),
    ],
)
def test_qt_messages_forwarded(caplog, mode, level):
    ctx = types.SimpleNamespace(file="qwidget.cpp", line=12)
    with caplog.at_level(logging.DEBUG, logger="cheatmaker.qt"):
        logs.qt_message_handler(mode, ctx, "QWidget: something odd")
    [rec] = caplog.records
    assert rec.name == "cheatmaker.qt" and rec.levelno == level
    assert "QWidget: something odd" in rec.getMessage() and "qwidget.cpp:12" in rec.getMessage()


@pytest.mark.parametrize("mode", [QtMsgType.QtDebugMsg, QtMsgType.QtInfoMsg])
def test_qt_debug_and_info_skipped(caplog, mode):
    with caplog.at_level(logging.DEBUG, logger="cheatmaker.qt"):
        logs.qt_message_handler(mode, None, "noise")
    assert caplog.records == []


def test_qt_fatal_flushes_handlers(monkeypatch):
    flushed = []
    monkeypatch.setattr(logs, "flush_handlers", lambda: flushed.append(True))
    logs.qt_message_handler(QtMsgType.QtWarningMsg, None, "w")
    assert flushed == []
    logs.qt_message_handler(QtMsgType.QtFatalMsg, None, "f")
    assert flushed == [True]


def test_install_qt_message_handler_routes_qwarning(caplog):
    from PySide6.QtCore import qInstallMessageHandler, qWarning

    logs.install_qt_message_handler()
    try:
        with caplog.at_level(logging.WARNING, logger="cheatmaker.qt"):
            qWarning("from Qt")
    finally:
        qInstallMessageHandler(None)
    assert any("from Qt" in r.getMessage() for r in caplog.records)


# --- app wiring -----------------------------------------------------------------


def test_main_logs_start_and_exit(main_log, native_log, monkeypatch):
    """app.main checks the last run, arms crash.log, starts the watchdog, marks exit."""
    import threading

    import PySide6.QtWidgets as qtw

    from cheatmaker import app as app_mod
    from cheatmaker.ui import controller, main_window, watchdog

    events = []

    class FakeSignal:
        def connect(self, fn):
            events.append(("quit_hook", fn))

    class FakeApp:
        aboutToQuit = FakeSignal()

        def __init__(self, argv):
            pass

        def setApplicationName(self, name):
            pass

        def exec(self):
            events.append("exec")
            return 3

    class FakeWindow:
        def __init__(self, ctl):
            pass

        def show(self):
            events.append("show")

        def show_first_run_warning(self):
            pass

    class FakeWatchdog:
        def __init__(self, parent=None):
            pass

        def start(self):
            events.append("watchdog_start")

        def stop(self):
            pass

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # main installs crash hooks
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(controller, "Controller", lambda *a: object())
    monkeypatch.setattr(main_window, "MainWindow", FakeWindow)
    monkeypatch.setattr(watchdog, "UiWatchdog", FakeWatchdog)
    monkeypatch.setattr(logs, "install_qt_message_handler", lambda: events.append("qt"))
    main_log.write_text("x INFO cheatmaker.app: action=start version=0 log=l\n")

    with monkeypatch.context() as m:  # pytest-qt needs the real QApplication after the call
        m.setattr(qtw, "QApplication", FakeApp)
        assert app_mod.main() == 3

    text = _main_log_text(main_log)
    order = [
        text.index("action=previous_run_ended_unexpectedly"),
        text.index("action=start version=" + logs.__version__),
        text.index("action=exit code=3"),
    ]
    assert order == sorted(order)
    assert logs.crash_log_path().exists() and faulthandler.is_enabled()
    assert events[:4] == ["qt", "show", "watchdog_start", events[3]]
    assert events[3][0] == "quit_hook" and events[4] == "exec"
    assert logs.previous_run_ended_unexpectedly() is False
