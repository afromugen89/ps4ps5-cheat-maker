"""Main window: connect bar on top, Find and My cheats tabs, status line, Copy log."""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cheatmaker.core.cheats import Cheat
from cheatmaker.logs import read_latest_log
from cheatmaker.ui import hints
from cheatmaker.ui.build_dialog import MakeItWorkDialog
from cheatmaker.ui.carry import CarryBar, CarryOver, NoteFingerprints
from cheatmaker.ui.cheats_view import CheatsView
from cheatmaker.ui.connect_view import ConnectView
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.find_wizard import FindWizard
from cheatmaker.ui.lock_wizard import LockWizard
from cheatmaker.ui.save_dialog import SaveDialog
from cheatmaker.ui.trace_dialog import TraceDialog

FIRST_RUN_WARNING = (
    "Before you start:\n\n"
    "Cheats can crash games or damage save files. Back up your saves first.\n\n"
    "Use cheats only in offline, single-player games."
)


class MainWindow(QMainWindow):
    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.setWindowTitle("Cheat Maker")
        self.resize(1000, 640)
        self.connect_view = ConnectView(ctl)
        self.find = FindWizard(ctl)
        self.cheats = CheatsView(ctl)
        self.fingerprints = NoteFingerprints(ctl, self.cheats.finder)
        self.carry_bar = CarryBar()
        self.carry = CarryOver(ctl, self.cheats.finder, self.carry_bar, self.fingerprints)
        self.cheats.bring_btn.clicked.connect(self.carry.open_advanced)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.find, "1. Find a value")
        self.tabs.addTab(self.cheats, "2. My cheats")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.copy_log_btn = QPushButton("Copy log")
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.copy_log_btn)
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.addWidget(self.connect_view)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self.carry_bar)
        lay.addLayout(bottom)
        self.setCentralWidget(root)

        ctl.message.connect(self.status.setText)
        self.copy_log_btn.clicked.connect(self.copy_log)
        self.cheats.open_lock = self.open_lock
        self.cheats.open_build = self.open_build
        self.find.open_build = self.open_build
        self.find.open_trace = self.open_trace
        self.cheats.open_save = self.open_save
        self.cheats.finder.open_search = self.open_search
        ctl.cheats_changed.connect(self._cheat_added)
        self._last_count = 0

    def _cheat_added(self) -> None:
        n = len(self.ctl.cheat_file.cheats) if self.ctl.cheat_file else 0
        if n > self._last_count and self.tabs.currentWidget() is self.find:
            self.tabs.setCurrentWidget(self.cheats)
        self._last_count = n

    def open_search(self, vtype, value) -> None:
        self.find.prefill(vtype, value)
        self.tabs.setCurrentWidget(self.find)

    def copy_log(self) -> None:
        """AC-9.4."""
        QGuiApplication.clipboard().setText(read_latest_log())
        self.status.setText("Log copied. Paste it into your bug report.")

    def _carry_busy(self) -> bool:
        """Notes are being brought over: Lock it and Make it work wait (spec AC-U3)."""
        if self.cheats.finder.carrying:
            self.ctl.message.emit(hints.CARRY_BUSY)
            return True
        return False

    def open_lock(self, cheat: Cheat) -> None:
        if self._carry_busy():
            return
        LockWizard(self.ctl, cheat, self).exec()

    def open_build(self, **kw) -> None:
        """Make it work (spec-cheat-builder) from Find or My cheats."""
        if self._carry_busy():
            return
        self.make_build_dialog(**kw).exec()

    def make_build_dialog(self, **kw) -> MakeItWorkDialog:
        return MakeItWorkDialog(self.ctl, parent=self, **kw)

    def open_trace(self, **kw) -> None:
        """Find the real one behind it (spec-trace) from Find."""
        self.make_trace_dialog(**kw).exec()

    def make_trace_dialog(self, **kw) -> TraceDialog:
        return TraceDialog(self.ctl, parent=self, **kw)

    def open_save(self) -> None:
        SaveDialog(self.ctl, self).exec()

    def show_first_run_warning(self) -> None:
        """AC-8.3: once, on first run."""
        if self.ctl.settings.first_run_done:
            return
        QMessageBox.warning(self, "Back up your saves", FIRST_RUN_WARNING)
        self.ctl.settings.first_run_done = True
        self.ctl.settings.save()

    def closeEvent(self, event) -> None:
        self.ctl.shutdown()
        super().closeEvent(event)
