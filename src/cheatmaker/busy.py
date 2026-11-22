"""How long the UI thread may wait for something a worker thread holds.

The window must never hang behind the PS5: on the UI (main) thread a lock is waited for
at most MAIN_THREAD_WAIT, then ConsoleBusy says "try again in a moment". Other threads
(freezer, searches) wait as long as it takes.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator

from cheatmaker.errors import ConsoleBusy

MAIN_THREAD_WAIT = 1.5
"""How long the UI thread waits before it gives up with ConsoleBusy."""
BUSY_MESSAGE = "The PS5 is busy right now. Try again in a moment."


_local = threading.local()


@contextlib.contextmanager
def short_wait(seconds: float) -> Iterator[None]:
    """On this (worker) thread, wait at most ``seconds`` for the console, then give up
    with ConsoleBusy (e.g. a liveness poll that must not queue behind a long search)."""
    prev = getattr(_local, "wait", None)
    _local.wait = seconds
    try:
        yield
    finally:
        _local.wait = prev


def thread_wait() -> float | None:
    """The ``short_wait`` limit of this thread, or None (wait as long as it takes)."""
    return getattr(_local, "wait", None)


def on_ui_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


@contextlib.contextmanager
def held(lock: threading.Lock | threading.RLock, wait: float | None = None) -> Iterator[None]:
    """Hold ``lock``; on the UI thread give up with ConsoleBusy after ``wait`` seconds
    (default MAIN_THREAD_WAIT)."""
    if on_ui_thread():
        if not lock.acquire(timeout=MAIN_THREAD_WAIT if wait is None else wait):
            raise ConsoleBusy(BUSY_MESSAGE)
    else:
        lock.acquire()
    try:
        yield
    finally:
        lock.release()
