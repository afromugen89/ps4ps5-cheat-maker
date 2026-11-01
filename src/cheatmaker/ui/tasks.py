"""Run slow work (network, scans) off the UI thread and come back with the result."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], Any], signals: _Signals) -> None:
        super().__init__()
        self.fn, self.signals = fn, signals

    def run(self) -> None:
        try:
            result = self.fn()
        except BaseException as exc:  # handed to the UI, which logs it
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(result)


class TaskRunner(QObject):
    """``sync=True`` runs inline (tests); otherwise on Qt's thread pool."""

    busy_changed = Signal(bool)

    def __init__(self, sync: bool = False, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.sync = sync
        self._running = 0
        self._keep: set[_Signals] = set()

    @property
    def busy(self) -> bool:
        return self._running > 0

    def run(
        self,
        fn: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._running += 1
        self.busy_changed.emit(True)

        def finish(cb: Callable[[Any], None], value: Any) -> None:
            self._running -= 1
            self.busy_changed.emit(self.busy)
            cb(value)

        if self.sync:
            try:
                result = fn()
            except BaseException as exc:
                finish(on_error, exc)
            else:
                finish(on_done, result)
            return
        sig = _Signals()
        self._keep.add(sig)
        sig.done.connect(lambda r: (self._keep.discard(sig), finish(on_done, r)))
        sig.failed.connect(lambda x: (self._keep.discard(sig), finish(on_error, x)))
        QThreadPool.globalInstance().start(_Job(fn, sig))
