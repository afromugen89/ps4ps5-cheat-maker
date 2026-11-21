"""UI freeze watchdog: log every thread's stack when the window stops responding.

A QTimer on the UI thread bumps a heartbeat; a daemon thread checks it. If the
heartbeat is older than ``freeze_s`` the UI thread is stuck, so the stacks show
where. Logged once per freeze, then once more when the UI moves again.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer

from cheatmaker.logs import flush_handlers

FREEZE_S = 15.0
BEAT_MS = 1000
CHECK_S = 2.0
SLEEP_GAP_S = 5 * CHECK_S
"""Checks this far apart mean the PC was asleep (or the process paused), not the UI."""

log = logging.getLogger("cheatmaker.watchdog")


def all_thread_stacks() -> str:
    """Every thread's current stack, named, as one block of text."""
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        stack = "".join(traceback.format_stack(frame))
        parts.append(f"--- thread {names.get(ident, '?')} (id {ident}) ---\n{stack}")
    return "\n".join(parts)


class UiWatchdog(QObject):
    """``clock`` and ``freeze_s`` are injectable so tests can drive ``check()``."""

    def __init__(
        self,
        freeze_s: float = FREEZE_S,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.freeze_s = freeze_s
        self._clock = clock
        self._lock = threading.Lock()
        self._heartbeat = clock()
        self._frozen_since: float | None = None  # heartbeat value when the freeze began
        self._last_check: float | None = None
        self._woke_at = float("-inf")  # a new freeze is counted from here after a sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(BEAT_MS)
        self._timer.timeout.connect(self.beat)

    def beat(self) -> None:
        """Called on the UI thread by the timer: the UI is alive."""
        with self._lock:
            self._heartbeat = self._clock()

    def check(self) -> None:
        """One watchdog look (normally on the background thread)."""
        now = self._clock()
        last, self._last_check = self._last_check, now
        if last is not None and now - last > SLEEP_GAP_S:
            # The PC slept: nothing beat meanwhile. Count a new freeze from the wake-up
            # only; the heartbeat stays as it is (only a real beat means "unfrozen").
            log.info("action=watchdog_skip gap=%d: the PC was probably asleep", round(now - last))
            self._woke_at = now
            return
        with self._lock:
            heartbeat = self._heartbeat
            frozen_since = self._frozen_since
            if frozen_since is not None and heartbeat > frozen_since:
                self._frozen_since = None
            elif frozen_since is None and now - max(heartbeat, self._woke_at) > self.freeze_s:
                self._frozen_since = heartbeat
            else:
                return
        if frozen_since is not None:
            log.info("action=ui_unfrozen seconds=%d", round(heartbeat - frozen_since))
            return
        log.warning(
            "action=ui_frozen seconds=%d the window stopped responding; all threads:\n%s",
            round(now - max(heartbeat, self._woke_at)),  # since the wake-up after a sleep
            all_thread_stacks(),
        )
        flush_handlers()

    def start(self) -> None:
        if self._thread is not None:
            return
        self.beat()
        self._stop.clear()
        self._timer.start()
        self._thread = threading.Thread(target=self._run, name="ui-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._timer.stop()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=CHECK_S + 1)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(CHECK_S):
            try:
                self.check()
            except Exception:  # the watchdog must never take the app down
                log.exception("action=watchdog_failed")
