""" "Lock it" wizard (spec D8): watch, pick the instruction, test live, keep."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from cheatmaker.core.cheats import Cheat
from cheatmaker.core.locker import (
    Writer,
    find_writers,
    make_lock_cheat,
    patch_landed,
    verify_original,
)
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.player_only_dialog import PlayerOnlyDialog

WATCH_SECONDS = 30.0


class LockWizard(QDialog):
    def __init__(self, ctl: Controller, source: Cheat, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Lock it: {source.name}")
        self.ctl, self.source = ctl, source
        self.writers: list[Writer] = []
        self.candidate: Cheat | None = None
        self.result_cheat: Cheat | None = None

        self.hint = QLabel(hints.LOCK_READY)
        self.hint.setWordWrap(True)
        self.start_btn = QPushButton("Start")
        self.options = QListWidget()
        self.show_code = QCheckBox("Advanced: show instructions")
        self.test_btn = QPushButton("Test it")
        self.test_all_btn = QPushButton("Test all at once")
        self.player_btn = QPushButton("Player only...")
        self.keep_btn = QPushButton("Keep it")
        self.cancel_btn = QPushButton("Cancel")
        row = QHBoxLayout()
        for w in (
            self.test_btn,
            self.test_all_btn,
            self.player_btn,
            self.keep_btn,
            self.cancel_btn,
        ):
            row.addWidget(w)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addWidget(self.start_btn)
        lay.addWidget(self.options, 1)
        lay.addWidget(self.show_code)
        lay.addLayout(row)

        self.start_btn.clicked.connect(self.do_start)
        self.test_btn.clicked.connect(self.do_test)
        self.test_all_btn.clicked.connect(self.do_test_all)
        self.player_btn.clicked.connect(self.do_player_only)
        self.keep_btn.clicked.connect(self.do_keep)
        self.cancel_btn.clicked.connect(self.reject)
        self.options.currentRowChanged.connect(lambda _r: self._pick())
        self.show_code.toggled.connect(lambda _on: self._fill())
        self._update()

    def _update(self) -> None:
        has = self.options.currentRow() >= 0
        self.test_btn.setEnabled(has)
        self.test_all_btn.setEnabled(len(self.writers) > 1)
        self.player_btn.setEnabled(has)
        self.keep_btn.setEnabled(has and self.candidate is not None and self.candidate.enabled)

    def do_start(self) -> None:
        loc = self.source.locations[0]
        size = len(loc.on)
        self.hint.setText(hints.LOCK_WATCHING)
        self.start_btn.setEnabled(False)
        # Stop our own freeze while watching, or we'd only see our own writes.
        was_on = self.source.enabled
        if was_on:
            self.ctl.live.turn_off(self.source, restore=False)
        self.ctl.cheats_changed.emit()
        self.ctl.tasks.run(
            lambda: find_writers(self.ctl.session, loc.offset, size, WATCH_SECONDS),
            self._found,
            self._failed,
        )

    def _found(self, writers: list[Writer]) -> None:
        self.writers = writers
        self.hint.setText(hints.LOCK_PICK)
        self._fill()
        self.options.setCurrentRow(0)

    def _failed(self, exc: BaseException) -> None:
        self.start_btn.setEnabled(True)
        self.hint.setText(hints.LOCK_READY)
        self.ctl.fail(exc, "lock")

    def _fill(self) -> None:
        row = self.options.currentRow()
        self.options.clear()
        for i, w in enumerate(self.writers, 1):
            label = f"Option {i} (changed it {w.hits} time{'s' if w.hits != 1 else ''})"
            if self.show_code.isChecked():
                label += f"   {w.address:#x}: {w.text}"
            self.options.addItem(label)
        if 0 <= row < len(self.writers):
            self.options.setCurrentRow(row)

    def _pick(self) -> None:
        self._untest()
        self._update()

    def _untest(self) -> None:
        if self.candidate is not None and self.candidate.enabled:
            try:
                self.ctl.live.turn_off(self.candidate)
            except Exception as exc:
                self.ctl.fail(exc, "lock")
        self.candidate = None

    def do_test(self) -> None:
        row = self.options.currentRow()
        if row < 0:
            return
        self._untest()
        try:
            cheat = make_lock_cheat(self.ctl.session, self.writers[row], self.source.name)
            verify_original(self.ctl.session, cheat)
            self.ctl.live.turn_on(cheat)
            landed = patch_landed(self.ctl.session, cheat)
        except Exception as exc:
            self.ctl.fail(exc, "lock")
            return
        self.candidate = cheat
        n = row + 1
        if landed:
            self.hint.setText(hints.LOCK_TESTING.format(n=n))
        else:
            self.hint.setText(hints.LOCK_NOT_LANDED.format(n=n))
        self._update()

    def do_test_all(self) -> None:
        """Switch off every instruction that changed the value, as one cheat. Quicker
        than trying options one by one; if the game misbehaves, test them singly."""
        if len(self.writers) < 2:
            return
        self._untest()
        try:
            parts = [make_lock_cheat(self.ctl.session, w, self.source.name) for w in self.writers]
            cheat = parts[0]
            cheat.locations = [loc for c in parts for loc in c.locations]
            verify_original(self.ctl.session, cheat)
            self.ctl.live.turn_on(cheat)
            landed = patch_landed(self.ctl.session, cheat)
        except Exception as exc:
            self.ctl.fail(exc, "lock")
            return
        self.candidate = cheat
        self.hint.setText(hints.LOCK_ALL_ON if landed else hints.LOCK_NOT_LANDED.format(n="all"))
        self._update()

    def do_player_only(self) -> None:
        row = self.options.currentRow()
        if row < 0:
            return
        self._untest()
        try:
            dlg = PlayerOnlyDialog(self.ctl, self.source, self.writers[row], self)
        except Exception as exc:
            self.ctl.fail(exc, "player_only")
            return
        if dlg.exec() and dlg.result_cheat is not None:
            self.result_cheat = dlg.result_cheat
            self.accept()

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
        self.ctl.message.emit(f'"{self.result_cheat.name}" now works after a restart.')
        self.accept()

    def reject(self) -> None:
        self._untest()
        super().reject()
