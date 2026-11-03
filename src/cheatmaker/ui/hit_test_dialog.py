"""Test the hits window (AC-H6): narrow a few matches to the real one by asking."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from cheatmaker.core.hit_tester import HitTester, suggest_value
from cheatmaker.core.values import SHOWN, Scale, ValueType, parse_number
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller

log = logging.getLogger(__name__)


def _fmt(v: float | int) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


class HitTestDialog(QDialog):
    def __init__(
        self,
        ctl: Controller,
        addresses: list[int],
        vtype: ValueType,
        on_found: Callable[[int, ValueType], None],
        parent=None,
        scale: Scale = SHOWN,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Test the hits")
        self.ctl, self.vtype, self.on_found = ctl, vtype, on_found
        self.scale = scale  # test values are typed and asked about as the game shows them
        self._busy = False  # a write or put-back is running on the console
        self._close_pending = False
        game = ctl.session.require_game()
        self.tester = HitTester(ctl.session.require_client(), game.pid, vtype, addresses)
        self._value: float | int | None = None

        self.warning = QLabel(hints.TEST_HITS_WARNING)
        self.warning.setWordWrap(True)
        self.warning.setVisible(not ctl.hit_test_warned)
        ctl.hit_test_warned = True
        self.status = QLabel("")
        self.hint = QLabel(hints.TEST_HITS_START)
        self.hint.setWordWrap(True)
        self.value = QLineEdit()
        self.value.setToolTip("The number to write. Pick one you will notice in the game.")
        self.write_btn = QPushButton("Write it")
        self.yes_btn = QPushButton("Yes, it changed")
        self.no_btn = QPushButton("No")
        self.unsure_btn = QPushButton("Not sure")
        self.close_btn = QPushButton("Stop")

        row = QHBoxLayout()
        row.addWidget(QLabel("Test value:"))
        row.addWidget(self.value, 1)
        row.addWidget(self.write_btn)
        ask = QHBoxLayout()
        for b in (self.yes_btn, self.no_btn, self.unsure_btn):
            ask.addWidget(b)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.close_btn)
        lay = QVBoxLayout(self)
        for w in (self.warning, self.status, self.hint):
            lay.addWidget(w)
        lay.addLayout(row)
        lay.addLayout(ask)
        lay.addLayout(bottom)

        self.write_btn.clicked.connect(self.do_write)
        self.value.returnPressed.connect(self.do_write)
        self.yes_btn.clicked.connect(lambda: self.do_answer(True))
        self.no_btn.clicked.connect(lambda: self.do_answer(False))
        self.unsure_btn.clicked.connect(lambda: self.do_answer(None))
        self.close_btn.clicked.connect(self.reject)
        self._prefill()
        self._update()

    def _prefill(self) -> None:
        try:
            shown = self.tester.current_value() / self.scale.factor
            if not self.vtype.is_float and float(shown).is_integer():
                shown = int(shown)
            self.value.setText(_fmt(suggest_value(self._shown_type(), shown)))
        except Exception:  # a read that fails here fails again on Write it, with a message
            log.debug("couldn't read a value to suggest", exc_info=True)

    def _shown_type(self) -> ValueType:
        """Scaled whole numbers (x10) are still whole on screen only when the factor is."""
        if self.vtype.is_float or float(self.scale.factor).is_integer():
            return self.vtype
        return ValueType.FLOAT

    def _stored(self, shown: float | int) -> float | int:
        stored = self.scale.apply(shown)
        if not self.vtype.is_float:
            stored = round(stored)
        self.vtype.encode(stored)  # raises ValueError if it doesn't fit
        return stored

    def _update(self) -> None:
        t = self.tester
        waiting = t.waiting and not self._busy
        n = len(t.candidates)
        self.status.setText(
            "" if t.done else f"{n:,} candidates left, about {t.rounds_left} questions to go."
        )
        idle = not self._busy and not t.waiting and not t.done
        self.write_btn.setEnabled(idle)
        self.value.setEnabled(idle)
        for b in (self.yes_btn, self.no_btn, self.unsure_btn):
            b.setEnabled(waiting)
        self.close_btn.setText("Close" if t.done else "Stop")

    def do_write(self) -> None:
        try:
            value = parse_number(self.value.text())
            stored = self._stored(value)
        except ValueError as exc:
            self.ctl.fail(exc, "test hits")
            return
        self._value = value
        self._run(lambda: self.tester.write_test(stored), self._written)

    def _run(self, fn, done) -> None:
        """Console work off the UI thread; Stop/close waits for it (see reject)."""
        self._busy = True
        self._update()
        self.ctl.tasks.run(fn, lambda r: self._finish(done, r), self._failed)

    def _finish(self, done, result) -> None:
        self._busy = False
        if self._close_pending:
            self._close_now()
            return
        done(result)

    def _written(self, _half) -> None:
        if self.tester.done:  # stopped mid-round: values are back
            self.hint.setText(hints.TEST_HITS_STOPPED)
        else:
            self.hint.setText(hints.TEST_HITS_ASK.format(value=_fmt(self._value)))
        self._update()

    def _failed(self, exc: BaseException) -> None:
        self._busy = False
        self._update()
        self.ctl.fail(exc, "test hits")
        if self._close_pending:
            self._close_now()

    def do_answer(self, changed: bool | None) -> None:
        self._run(lambda: self.tester.answer(changed), self._answered)

    def _answered(self, _r) -> None:
        t = self.tester
        if t.result is not None:
            self.hint.setText(hints.TEST_HITS_FOUND.format(addr=t.result))
            log.info("action=test_hits_found addr=%#x rounds=%d", t.result, t.rounds)
            self.on_found(t.result, self.vtype)
        elif t.done:
            self.hint.setText(hints.TEST_HITS_STOPPED)
        else:
            self.hint.setText(hints.TEST_HITS_NEXT)
            self._prefill()
        self._update()

    def reject(self) -> None:
        """Stop or close: put back anything still written. While a write runs, ask it
        to stop and close once it has put the values back."""
        if self._busy:
            self._close_pending = True
            self.tester.request_stop()
            self.hint.setText(hints.TEST_HITS_STOPPING)
            return
        self._close_now()

    def _close_now(self) -> None:
        self._close_pending = False
        if not self.tester.done or self.tester.waiting:
            try:
                self.tester.stop()
            except Exception as exc:
                self.ctl.fail(exc, "test hits")
        super().reject()
