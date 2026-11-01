"""Error logging (AC-9): rotating file, context on every line, crash hook."""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from cheatmaker import __version__
from cheatmaker.paths import logs_dir

LOG_FILE_NAME = "cheatmaker.log"
MAX_BYTES = 10 * 1024 * 1024  # AC-9.3: rotate at 10 MB
BACKUP_COUNT = 5  # AC-9.3: keep the last 5


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
