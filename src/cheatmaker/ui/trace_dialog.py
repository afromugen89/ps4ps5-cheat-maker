""" "Find the real one behind it" (docs/spec-trace.md): the dialog around core.tracer.

The whole trace is one background job (``Tracer.run``); the player only plays, so no
question is asked between steps. Cancel sets the job's Event: the watch ends within
about half a second and a write test within its settle, with everything put back.
While it runs, freezes and cheats on the watched copy are paused (``release``) and
switched back on after.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from cheatmaker.core import tracer as tr
from cheatmaker.core.cheats import Cheat
from cheatmaker.core.freezer import Freezer
from cheatmaker.core.values import ValueType
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.messages import report

log = logging.getLogger(__name__)

STOP_WAIT_S = 3.0
"""Disconnect: how long to wait for a cancelled trace to put its test values back."""

Found = Callable[[tr.TraceResult], None]
SearchWay = Callable[[tr.TraceResult, float | int | None], None]
"""``on_search_way(result, the start row's value now)``."""


def result_text(r: tr.TraceResult) -> str:
    """The plain hint for a finished trace (AC-T7, AC-T8, AC-T9), plus the addresses
    that may still hold a test value, whatever the outcome."""
    text = _outcome_text(r)
    if r.not_restored:
        addrs = ", ".join(f"{a:#x}" for a in r.not_restored)
        text += " " + hints.TRACE_NOT_RESTORED.format(addrs=addrs)
    return text


def _outcome_text(r: tr.TraceResult) -> str:
    Out = tr.Outcome
    if r.outcome is Out.FOUND:
        if r.value is None or r.way is None or r.vtype is None:
            return hints.TRACE_FOUND_PLAIN
        try:
            shown = r.way.screen_text(r.max_now)
        except ValueError:
            return hints.TRACE_FOUND_PLAIN
        return hints.TRACE_FOUND.format(
            value=r.vtype.text(r.value), kind=hints.TRACE_KINDS[r.vtype.name], shown=shown
        )
    if r.outcome is Out.SEARCH_THAT_WAY and r.way is not None:
        try:
            shown = r.way.screen_text(r.max_now)
        except ValueError:
            shown = "another way"
        return hints.TRACE_SEARCH_WAY.format(type=r.way.real.label.lower(), shown=shown)
    if r.outcome is Out.TOO_FAR:
        if r.reason == tr.TOO_LONG:
            text = hints.TRACE_TOO_LONG
        else:
            n = len(r.hops)
            text = hints.TRACE_TOO_FAR.format(n=n, steps="step" if n == 1 else "steps")
        return f"{text} {hints.TRACE_NOT_SURE}" if r.address is not None else text
    return {
        Out.ALREADY_REAL: hints.TRACE_ALREADY_REAL,
        Out.NO_WRITER: hints.TRACE_NO_WRITER,
        Out.CANT_WATCH: hints.TRACE_CANT_WATCH,
        Out.SEARCH_THAT_WAY: hints.TRACE_NOT_FOLLOWED,
        Out.NOT_FOLLOWED: hints.TRACE_NOT_FOLLOWED,
        Out.SCRAMBLED: hints.TRACE_SCRAMBLED,
        Out.UNCONFIRMED: hints.TRACE_UNCONFIRMED,
        Out.CANCELLED: hints.TRACE_CANCELLED,
        Out.LOST: hints.TRACE_LOST,
        Out.FAILED: hints.TRACE_FAILED,
    }[r.outcome]


def details(r: tr.TraceResult) -> list[str]:
    """Advanced: show details, per hop (AC-T10)."""
    out: list[str] = []
    for hop in r.hops:
        addr, size = hop.span
        out.append(hints.TRACE_ADV_HOP.format(n=hop.n, addr=f"{addr:#x}", size=size, hits=hop.hits))
        for wp in hop.writers:
            skipped = f", skipped: {wp.skipped}" if wp.skipped else ""
            out.append(
                hints.TRACE_ADV_WRITER.format(
                    addr=f"{wp.writer.address:#x}",
                    text=wp.writer.text,
                    hits=wp.writer.hits,
                    skipped=skipped,
                )
            )
            if wp.path is not None:
                out.append(
                    hints.TRACE_ADV_PATH.format(
                        path=" <- ".join(wp.path.followed),
                        end=wp.path.end.value,
                        reason=f" ({wp.path.reason})" if wp.path.reason else "",
                    )
                )
        for cand, why in hop.tried:
            out.append(
                hints.TRACE_ADV_TRIED.format(
                    addr=f"{cand.address:#x}",
                    how=hints.TRACE_HOW.get(cand.method, cand.method),
                    expr=cand.expr,
                    why=why,
                )
            )
        if hop.source is not None and hop.way is not None:
            out.append(
                hints.TRACE_ADV_SOURCE.format(
                    addr=f"{hop.source.address:#x}",
                    how=hints.TRACE_HOW.get(hop.source.method, hop.source.method),
                    expr=hop.source.expr,
                    way=hop.way.text(),
                )
            )
        if hop.verdict is not None:
            out.append(hints.TRACE_ADV_VERDICT.format(verdict=hop.verdict.value))
        if hop.stop:
            out.append(hints.TRACE_ADV_STOP.format(why=hop.stop))
    if r.not_restored:
        out.append(
            hints.TRACE_NOT_RESTORED.format(addrs=", ".join(f"{a:#x}" for a in r.not_restored))
        )
    return out


class _Signals(QObject):
    progress = Signal(float, object, int)  # worker thread -> dialog
    cheats = Signal()


class TraceDialog(QDialog):
    """``frozen``: {address: size} of the Find list's frozen rows, so a paused freeze
    is put back with the same bytes. ``sleep``/``clock`` are for tests."""

    def __init__(
        self,
        ctl: Controller,
        *,
        address: int,
        vtype: ValueType,
        on_found: Found,
        on_search_way: SearchWay,
        parent=None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(hints.TRACE_TITLE)
        self.ctl, self.address, self.vtype = ctl, address, vtype
        self.on_found, self.on_search_way = on_found, on_search_way
        self.sleep, self.clock = sleep, clock
        self.result: tr.TraceResult | None = None
        self._cancel: threading.Event | None = None
        self._job_done = threading.Event()  # set on the worker thread when run() returns
        self._job_done.set()
        self._rejected = False
        self.notice: Callable[[], bool] = self._notice  # tests replace this
        self._signals = _Signals()
        self._signals.progress.connect(self._on_progress)
        self._signals.cheats.connect(ctl.cheats_changed.emit)

        self.hint = QLabel(hints.TRACE_READY)
        self.hint.setWordWrap(True)
        self.step = QLabel("")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.start_btn = QPushButton("Start")
        self.cancel_btn = QPushButton("Cancel")
        self.search_btn = QPushButton(hints.TRACE_SEARCH_WAY_BUTTON)
        self.search_btn.setVisible(False)
        self.show_advanced = QCheckBox(hints.BUILD_ADVANCED)
        self.advanced = QListWidget()
        self.advanced.setVisible(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.show_advanced)
        buttons.addStretch(1)
        buttons.addWidget(self.search_btn)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.cancel_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addWidget(self.step)
        lay.addWidget(self.progress)
        lay.addWidget(self.advanced, 1)
        lay.addLayout(buttons)

        self.show_advanced.toggled.connect(self.advanced.setVisible)
        self.start_btn.clicked.connect(self.do_start)
        self.cancel_btn.clicked.connect(self.reject)
        self.search_btn.clicked.connect(self.do_search_way)

    @property
    def busy(self) -> bool:
        return self._cancel is not None

    # -- the one-time notice ---------------------------------------------------------
    def _notice(self) -> bool:
        box = QMessageBox(
            QMessageBox.Icon.Information, hints.TRACE_NOTICE_TITLE, hints.TRACE_NOTICE
        )
        hide = QCheckBox(hints.TRACE_NOTICE_HIDE)
        box.setCheckBox(hide)
        box.exec()
        return hide.isChecked()

    def _maybe_notice(self) -> None:
        s = self.ctl.settings
        if s.trace_notice_hidden or self.ctl.trace_notice_shown:
            return
        self.ctl.trace_notice_shown = True
        if self.notice():
            s.trace_notice_hidden = True
            s.save()

    # -- running -----------------------------------------------------------------------
    def do_start(self) -> None:
        self._maybe_notice()
        try:
            tracer = tr.Tracer(
                self.ctl.session,
                self.address,
                self.vtype,
                release=self.release,
                exec_breakpoint=self.ctl.settings.trace_exec_breakpoint,
                sleep=self.sleep,
                clock=self.clock,
            )
        except Exception as exc:
            self.ctl.fail(exc, "trace")
            return
        self._cancel = cancel = threading.Event()
        self.ctl.before_disconnect.append(self._stop_for_disconnect)
        self.start_btn.setEnabled(False)
        self.search_btn.setVisible(False)
        self.advanced.clear()
        self.result = None
        self._job_done.clear()
        self.ctl.tasks.run(lambda: self._run(tracer, cancel), self._done, self._failed)

    def _run(self, tracer: tr.Tracer, cancel: threading.Event) -> tr.TraceResult:
        """The job (worker thread). ``_job_done`` lets a disconnect wait for it."""
        try:
            return tracer.run(self._signals.progress.emit, cancel)
        finally:
            self._job_done.set()

    def _on_progress(self, fraction: float, stage: tr.Stage, n: int) -> None:
        self.progress.setValue(int(fraction * 100))
        if stage is tr.Stage.WATCH:
            self.step.setText(hints.TRACE_STEP_WATCH.format(n=n, most=tr.MAX_HOPS))
        elif stage is tr.Stage.READ:
            self.step.setText(hints.TRACE_STEP_READ)
        else:
            self.step.setText(hints.TRACE_STEP_CHECK)
        self.hint.setText(
            {
                tr.Stage.WATCH: hints.TRACE_WATCHING,
                tr.Stage.READ: hints.TRACE_READING,
                tr.Stage.STEADY: hints.TRACE_STEADY,
                tr.Stage.CHANGE: hints.TRACE_CHANGE,
                tr.Stage.TEST: hints.TRACE_TESTING,
            }[stage]
        )

    def _finish(self) -> None:
        self._cancel = None
        if self._stop_for_disconnect in self.ctl.before_disconnect:
            self.ctl.before_disconnect.remove(self._stop_for_disconnect)
        self.start_btn.setEnabled(self.result is None or not self._usable(self.result))
        self.cancel_btn.setText("Close")

    @staticmethod
    def _usable(r: tr.TraceResult) -> bool:
        return r.outcome in (tr.Outcome.FOUND, tr.Outcome.TOO_FAR) and r.address is not None

    def _done(self, r: tr.TraceResult) -> None:
        self.result = r
        self._finish()
        self.progress.setValue(100)
        self.hint.setText(result_text(r))
        for line in details(r):
            self.advanced.addItem(line)
        if r.outcome is tr.Outcome.LOST:
            self.ctl.handle_lost()
        if self._rejected:
            # the dialog may be closed: the player still has to hear what stayed changed
            if r.not_restored:
                addrs = ", ".join(f"{a:#x}" for a in r.not_restored)
                self.ctl.message.emit(hints.TRACE_NOT_RESTORED.format(addrs=addrs))
            return
        if self._usable(r):
            self.on_found(r)
        elif r.outcome is tr.Outcome.SEARCH_THAT_WAY and r.way is not None:
            self.search_btn.setVisible(True)

    def _failed(self, exc: BaseException) -> None:
        self._finish()
        self.hint.setText(hints.TRACE_CANCELLED if self._rejected else "")
        self.ctl.fail(exc, "trace")

    def do_search_way(self) -> None:
        r = self.result
        if r is None or r.way is None:
            return
        try:
            g = self.ctl.session.require_game()
            raw = self.ctl.session.require_client().read(g.pid, self.address, self.vtype.size)
            screen = self.vtype.decode(raw)
        except Exception as exc:  # the number is left empty; the player types it
            report(exc, "trace")
            screen = None
        self.on_search_way(r, screen)
        self.accept()

    def _stop_for_disconnect(self) -> None:
        """Disconnect: stop the trace and give it up to STOP_WAIT_S to put its test
        values back while the link still works."""
        if self._cancel is not None:
            self._rejected = True
            self._cancel.set()
            if not self._job_done.wait(STOP_WAIT_S):
                log.warning("action=trace_stop_wait timed_out=True")

    def reject(self) -> None:
        """Cancel or close: the running trace stops and puts everything back."""
        if self._cancel is not None:
            self._cancel.set()
            self._rejected = True
            self.hint.setText(hints.TRACE_CANCELLED)
        super().reject()

    # -- pausing the app's own writes on the watched copy (worker thread) ----------------
    def release(self, addr: int, size: int) -> Callable[[], None]:
        """Pause Find's freezes and the freezing cheats that write into
        [addr, addr+size); the returned resume puts them back as they were. A cheat
        written once isn't touched (resuming would write it again). If pausing fails
        half way, what was already paused is resumed before the error goes on."""
        end = addr + size
        ctl = self.ctl
        refreeze: list[tuple[str, list[tuple[int, int, bytes]]]] = []
        cheats: list[Cheat] = []
        try:
            g = ctl.session.require_game()
        except Exception as exc:  # nothing to pause without a game
            report(exc, "trace")
            return lambda: None

        def resume() -> None:
            for key, writes in refreeze:
                try:
                    ctl.freezer.freeze(key, writes)  # exactly as it was, every byte
                except Exception as exc:  # the rest are still put back
                    report(exc, "trace")
            for c in cheats:
                try:
                    ctl.live.turn_on(c)
                except Exception as exc:
                    report(exc, "trace")
            if cheats:
                self._signals.cheats.emit()

        try:
            for key, writes in _find_freezes(ctl.freezer).items():
                if any(a < end and addr < a + len(data) for _pid, a, data in writes):
                    ctl.freezer.unfreeze(key)
                    refreeze.append((key, writes))
            cf = ctl.cheat_file
            for c in list(cf.cheats) if cf is not None else []:
                if not (c.enabled and c.freeze):
                    continue
                if any(a < end and addr < a + n for a, n in self._spans(c, g.module_base)):
                    ctl.live.turn_off(c, restore=False)
                    cheats.append(c)
        except BaseException:
            resume()
            raise
        if cheats:
            self._signals.cheats.emit()
        log.info(
            "action=trace_release addr=%#x size=%d freezes=%d cheats=%d",
            addr,
            size,
            len(refreeze),
            len(cheats),
        )
        return resume

    def _spans(self, c: Cheat, module_base: int) -> list[tuple[int, int]]:
        """(address, size) a cheat writes now. An anchored cheat writes where its
        object's tracker says the field is (its location's offset is unused)."""
        if c.anchor is None:
            return [(loc.address(module_base), len(loc.on)) for loc in c.locations]
        obj_id, field_name = c.anchor
        tracker = self.ctl.live.trackers.get(obj_id)
        at = tracker.field_address(field_name) if tracker is not None else None
        if at is None or not c.locations:
            return []
        return [(at, len(c.locations[0].on))]


def _find_freezes(freezer: Freezer) -> dict[str, list[tuple[int, int, bytes]]]:
    """Find's freezes (``find:<address>``) with the bytes each one holds, listed or
    not. The Freezer has no public view of its entries and core/freezer.py is owned
    by another branch for now, so they are read under its lock here."""
    with freezer._lock:
        entries = dict(freezer._entries)
    return {
        key: list(writes)
        for key, writes in entries.items()
        if key.startswith("find:") and not callable(writes)
    }


__all__ = ["TraceDialog", "details", "result_text"]
