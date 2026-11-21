"""Error logging (AC-9): rotating file, context on every line, crash hooks.

Besides Python exceptions, a crash recorder makes a window that just vanishes
diagnosable: native crashes go to ``crash.log`` (faulthandler), a clean exit is
marked in the main log, and Qt's own warnings are forwarded to it.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO

from cheatmaker import __version__
from cheatmaker.paths import logs_dir

LOG_FILE_NAME = "cheatmaker.log"
MAX_BYTES = 10 * 1024 * 1024  # AC-9.3: rotate at 10 MB
BACKUP_COUNT = 5  # AC-9.3: keep the last 5

CRASH_FILE_NAME = "crash.log"
CRASH_MAX_BYTES = 1024 * 1024  # trim crash.log once it passes 1 MB...
CRASH_KEEP_BYTES = 256 * 1024  # ...down to its last 256 KB
CRASH_HEADER = "=== Cheat Maker start"
CRASH_EXIT = "=== Cheat Maker exit"
"""Written on a clean exit. On Windows faulthandler also dumps exceptions the app
handled ("Windows fatal exception: code 0x..."), so a dump before this line isn't a
crash; one after it is (Qt teardown)."""
CRASH_TAIL_LINES = 60
# What faulthandler writes when the process dies natively (or hangs on exit).
CRASH_MARKERS = ("Fatal Python error", "Windows fatal exception", "Current thread", "Thread 0x")

START_MARKER = "action=start "
EXIT_MARKER = "action=exit "
RUN_CHECK_BYTES = 256 * 1024  # only the tail of the main log is read on start


class _GameContext:
    """The game we're attached to, stamped on every log line (AC-9.1)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.title_id = "-"
        self.version = "-"

    def set(self, title_id: str | None, version: str | None) -> None:
        with self._lock:
            self.title_id = title_id or "-"
            self.version = version or "-"


game_context = _GameContext()


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.app_version = __version__
        record.title_id = game_context.title_id
        record.game_version = game_context.version
        return True


FORMAT = (
    "%(asctime)s %(levelname)s app=%(app_version)s game=%(title_id)s "
    "ver=%(game_version)s %(name)s: %(message)s"
)


def log_file_path() -> Path:
    return logs_dir() / LOG_FILE_NAME


def setup_logging(level: int = logging.INFO) -> Path:
    """Attach the rotating file handler to the root logger. Safe to call twice."""
    path = log_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for h in root.handlers:
        if getattr(h, "_cheatmaker", False):
            return path
    handler = RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler._cheatmaker = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter(FORMAT))
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)
    return path


def install_crash_hook() -> None:
    """AC-9.5: write unexpected crashes (main and worker threads) to the log."""
    crash_log = logging.getLogger("cheatmaker.crash")
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        crash_log.critical("action=crash unexpected error", exc_info=(exc_type, exc, tb))
        for h in logging.getLogger().handlers:
            h.flush()
        previous(exc_type, exc, tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        crash_log.critical(
            "action=crash thread=%s unexpected error",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = hook
    threading.excepthook = thread_hook


def read_latest_log(max_chars: int = 200_000) -> str:
    """Text for the "Copy log" button (AC-9.4): the tail of the current log file."""
    path = log_file_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def flush_handlers() -> None:
    """Push buffered log lines to disk, e.g. just before a likely crash."""
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # a broken handler must not hide the reason we flushed
            logging.getLogger(__name__).debug("action=flush_failed", exc_info=True)


# --- native crashes (faulthandler) -------------------------------------------

_crash_file: IO[str] | None = None  # kept open for faulthandler for the whole run


def crash_log_path() -> Path:
    return logs_dir() / CRASH_FILE_NAME


def _trim_crash_log(path: Path) -> None:
    """Keep crash.log from growing forever: past 1 MB keep only the last 256 KB."""
    try:
        if path.stat().st_size <= CRASH_MAX_BYTES:
            return
        with path.open("rb") as f:
            f.seek(-CRASH_KEEP_BYTES, os.SEEK_END)
            tail = f.read()
    except OSError:
        return
    newline = tail.find(b"\n")
    tail = tail[newline + 1 :] if newline >= 0 else tail  # start on a whole line
    try:
        path.write_bytes(tail)
    except OSError:
        logging.getLogger("cheatmaker.crash").warning(
            "action=crash_log_trim_failed file=%s", path, exc_info=True
        )


def _after_line(text: str, marker: str) -> str | None:
    """The text after the line holding the last ``marker``; None if there is none."""
    at = text.rfind(marker)
    if at < 0:
        return None
    line_end = text.find("\n", at)
    return "" if line_end < 0 else text[line_end + 1 :]


def previous_native_crash(path: Path, ended_unexpectedly: bool = True) -> str | None:
    """The tail of what faulthandler wrote in the last run, if that run crashed.

    A run that wrote the exit marker only counts what came after it (a crash while
    closing). A run without one counts only when ``ended_unexpectedly`` (the main log
    has its start but no exit): otherwise the dumps are exceptions Windows reported
    but the app handled."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section = _after_line(text, CRASH_HEADER)
    if section is None:
        section = text  # header trimmed away or never written: look at it all
    after_exit = _after_line(section, CRASH_EXIT)
    if after_exit is not None:
        section = after_exit
    elif not ended_unexpectedly:
        return None
    if not any(marker in section for marker in CRASH_MARKERS):
        return None
    lines = section.strip().splitlines()
    return "\n".join(lines[-CRASH_TAIL_LINES:])


def install_native_crash_log(ended_unexpectedly: bool = True) -> Path:
    """Send native crashes (access violations, aborts) of every thread to crash.log.

    Reports a crash left by the previous run to the main log first, so it shows
    up in "Copy log" (see previous_native_crash for ``ended_unexpectedly``). Safe to
    call twice.
    """
    global _crash_file
    path = crash_log_path()
    if _crash_file is not None:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    _trim_crash_log(path)
    previous = previous_native_crash(path, ended_unexpectedly)
    if previous:
        logging.getLogger("cheatmaker.crash").warning(
            "action=previous_crash file=%s last run stopped with a native error:\n%s",
            path,
            previous,
        )
    f = path.open("a", encoding="utf-8")
    stamp = datetime.now().isoformat(sep=" ", timespec="seconds")
    f.write(f"{CRASH_HEADER} {stamp} app={__version__} pid={os.getpid()}\n")
    f.flush()  # faulthandler writes to the raw fd, so nothing may sit in the buffer
    faulthandler.enable(file=f, all_threads=True)
    _crash_file = f
    return path


def mark_clean_exit() -> None:
    """Write the exit marker to crash.log; it stays armed for Qt's teardown."""
    if _crash_file is None:
        return
    stamp = datetime.now().isoformat(sep=" ", timespec="seconds")
    try:
        _crash_file.write(f"{CRASH_EXIT} {stamp}\n")
        _crash_file.flush()
    except OSError:
        logging.getLogger("cheatmaker.crash").warning("action=crash_log_exit_failed")


def uninstall_native_crash_log() -> None:
    """Stop writing to crash.log and close it (tests, and a clean shutdown)."""
    global _crash_file
    if _crash_file is None:
        return
    faulthandler.disable()
    _crash_file.close()
    _crash_file = None


# --- did the previous run exit cleanly? --------------------------------------


def previous_run_ended_unexpectedly(path: Path | None = None) -> bool:
    """True when the last ``action=start`` in the log has no ``action=exit`` after it.

    Call before logging this run's start. Reads only the tail of the file.
    """
    path = path or log_file_path()
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - RUN_CHECK_BYTES))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    start = text.rfind(START_MARKER)
    if start < 0:
        return False
    return EXIT_MARKER not in text[start:]


def check_previous_run() -> bool:
    """Log and return whether the previous run ended without its exit line."""
    if previous_run_ended_unexpectedly():
        logging.getLogger("cheatmaker.crash").warning(
            "action=previous_run_ended_unexpectedly the last run has a start but no exit "
            "(closed by a crash, a freeze or Task Manager)"
        )
        return True
    return False


# --- Qt's own messages --------------------------------------------------------


def install_qt_message_handler() -> None:
    """Forward Qt warnings/criticals/fatals to the ``cheatmaker.qt`` logger."""
    from PySide6.QtCore import qInstallMessageHandler

    qInstallMessageHandler(qt_message_handler)


QT_REPEAT_S = 10.0  # the same Qt message is logged at most once in this many seconds


class _Repeats:
    """Rate limit for identical messages (Qt can warn hundreds of times a second)."""

    MAX_KEYS = 500

    def __init__(self, every_s: float, clock=time.monotonic) -> None:
        self.every_s = every_s
        self.clock = clock
        self._lock = threading.Lock()
        self._last: dict[object, tuple[float, int]] = {}  # key -> (logged at, skipped)

    def seen(self, key: object) -> int | None:
        """None: skip it. Else log it, saying how many were skipped since."""
        now = self.clock()
        with self._lock:
            logged_at, skipped = self._last.get(key, (None, 0))
            if logged_at is not None and now - logged_at < self.every_s:
                self._last[key] = (logged_at, skipped + 1)
                return None
            if len(self._last) >= self.MAX_KEYS:
                self._last.clear()
            self._last[key] = (now, 0)
            return skipped


_qt_repeats = _Repeats(QT_REPEAT_S)


def qt_message_handler(mode, context, message: str) -> None:
    from PySide6.QtCore import QtMsgType

    level = {
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }.get(mode)
    if level is None:  # debug/info: too chatty for the log
        return
    where = ""
    if context is not None and getattr(context, "file", None):
        where = f" at={context.file}:{context.line}"
    repeats = 0 if level == logging.CRITICAL else _qt_repeats.seen((level, where, message))
    if repeats is None:  # the same message logged moments ago
        return
    more = f" repeats={repeats}" if repeats else ""
    logging.getLogger("cheatmaker.qt").log(level, "action=qt_message%s%s %s", where, more, message)
    if level == logging.CRITICAL:
        flush_handlers()  # Qt aborts the process right after a fatal message
