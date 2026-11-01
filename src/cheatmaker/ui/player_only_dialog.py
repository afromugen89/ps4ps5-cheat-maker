""" "Player only" for Lock it: collect characters, pick the field that marks you, test."""

from __future__ import annotations

import threading

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from cheatmaker.core import cave as cv
from cheatmaker.core.cheats import Cheat
from cheatmaker.core.locker import Writer, patch_landed, verify_original
from cheatmaker.core.values import ValueType, parse_number
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller

COLLECT_SECONDS = 20.0


class PlayerOnlyDialog(QDialog):
    def __init__(self, ctl: Controller, source: Cheat, writer: Writer, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Player only: {source.name}")
        self.ctl, self.source, self.writer = ctl, source, writer
        self.target = cv.writer_target(writer)
        game = ctl.session.require_game()
        loc = source.locations[0]
        self.player_obj = loc.address(game.module_base) - self.target.disp
        self.vtype: ValueType | None = source.value_type
        self.guesses: list[cv.FieldGuess] = []
        self.candidate: Cheat | None = None
        self.result_cheat: Cheat | None = None
        self._cancel: threading.Event | None = None

        self.hint = QLabel(hints.PLAYER_ONLY_START)
        self.hint.setWordWrap(True)
        where = f"[{self.target.base_name}+{self.target.disp:#x}]"
        self.info = QLabel(f"{writer.text}   (writes {where}; your object {self.player_obj:#x})")
        self.collect_btn = QPushButton(f"Collect ({COLLECT_SECONDS:.0f} s)")
        self.fields = QListWidget()
        self.value = QLineEdit()
        if self.vtype is not None and source.value is not None:
            self.value.setText(f"{source.value:g}" if self.vtype.is_float else str(source.value))
        self.value.setPlaceholderText("Value to keep, e.g. 999")
        self.test_btn = QPushButton("Test it")
        self.keep_btn = QPushButton("Keep it")
        self.cancel_btn = QPushButton("Cancel")

        val = QHBoxLayout()
        val.addWidget(QLabel("Keep your value at:"))
        val.addWidget(self.value, 1)
        row = QHBoxLayout()
        for w in (self.test_btn, self.keep_btn, self.cancel_btn):
            row.addWidget(w)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addWidget(self.info)
        lay.addWidget(self.collect_btn)
        lay.addWidget(QLabel("Field that marks you (best guess first):"))
        lay.addWidget(self.fields, 1)
        lay.addLayout(val)
        lay.addLayout(row)

        self.collect_btn.clicked.connect(self.do_collect)
        self.test_btn.clicked.connect(self.do_test)
        self.keep_btn.clicked.connect(self.do_keep)
        self.cancel_btn.clicked.connect(self.reject)
        self.fields.currentRowChanged.connect(lambda _r: self._untest_and_update())
        self._update()

    def _update(self) -> None:
        busy = self._cancel is not None
        self.collect_btn.setEnabled(not busy)
        self.test_btn.setEnabled(not busy and self.fields.currentRow() >= 0)
        self.keep_btn.setEnabled(self.candidate is not None and self.candidate.enabled)

    def _untest_and_update(self) -> None:
        self._untest()
        self._update()

    def _untest(self) -> None:
        if self.candidate is not None and self.candidate.enabled:
            try:
                self.ctl.live.turn_off(self.candidate)
            except Exception as exc:
                self.ctl.fail(exc, "lock")
        self.candidate = None

    def do_collect(self) -> None:
        self._untest()
        if self.source.enabled:  # our own freeze would only add our writes
            self.ctl.live.turn_off(self.source, restore=False)
            self.ctl.cheats_changed.emit()
        self._cancel = threading.Event()
        cancel = self._cancel
        self.hint.setText(hints.PLAYER_ONLY_FIGHT)
        self._update()

        def work() -> list[cv.FieldGuess]:
            objs = cv.collect_objects(self.ctl.session, self.writer, COLLECT_SECONDS, cancel)
            return cv.compare_objects(self.ctl.session, self.player_obj, objs)

        self.ctl.tasks.run(work, self._collected, self._failed)

    def _collected(self, guesses: list[cv.FieldGuess]) -> None:
        self._cancel = None
        self.guesses = guesses
        self.fields.clear()
        for g in guesses:
            self.fields.addItem(g.label())
        self.hint.setText(hints.PLAYER_ONLY_PICK if guesses else hints.PLAYER_ONLY_NONE)
        if guesses:
            self.fields.setCurrentRow(0)
        self._update()

    def _failed(self, exc: BaseException) -> None:
        self._cancel = None
        self.hint.setText(hints.PLAYER_ONLY_START)
        self._update()
        self.ctl.fail(exc, "player_only")

    def _value_bytes(self) -> bytes:
        number = parse_number(self.value.text())
        vt = self.vtype or (ValueType.FLOAT if isinstance(number, float) else ValueType.INT32)
        return vt.encode(number)

    def do_test(self) -> None:
        row = self.fields.currentRow()
        if row < 0:
            return
        self._untest()
        try:
            cheat = cv.make_player_only_cheat(
                self.ctl.session,
                self.writer,
                self.guesses[row],
                self._value_bytes(),
                self.source.name,
            )
            verify_original(self.ctl.session, cheat)
            self.ctl.live.turn_on(cheat)
            landed = patch_landed(self.ctl.session, cheat)
        except Exception as exc:
            self.ctl.fail(exc, "player_only")
            return
        self.candidate = cheat
        self.hint.setText(hints.PLAYER_ONLY_TESTING if landed else hints.PLAYER_ONLY_NOT_LANDED)
        self._update()

    def do_keep(self) -> None:
        if self.candidate is None:
            return
        cheats = self.ctl.ensure_file().cheats
        idx = cheats.index(self.source) if self.source in cheats else len(cheats)
        if self.source in cheats:
            cheats.remove(self.source)
        cheats.insert(idx, self.candidate)
        self.result_cheat, self.candidate = self.candidate, None
        self.ctl.cheats_changed.emit()
        self.ctl.message.emit(f'"{self.result_cheat.name}" (player only) works after a restart.')
        self.accept()

    def reject(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
        self._untest()
        super().reject()
