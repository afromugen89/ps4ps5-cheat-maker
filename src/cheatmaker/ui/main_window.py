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
from cheatmaker.ui.cheats_view import CheatsView
from cheatmaker.ui.connect_view import ConnectView
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.find_wizard import FindWizard
from cheatmaker.ui.lock_wizard import LockWizard
from cheatmaker.ui.save_dialog import SaveDialog

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
        lay.addLayout(bottom)
        self.setCentralWidget(root)

        ctl.message.connect(self.status.setText)
        self.copy_log_btn.clicked.connect(self.copy_log)
        self.cheats.open_lock = self.open_lock
        self.cheats.open_save = self.open_save
        ctl.cheats_changed.connect(self._cheat_added)
        self._last_count = 0

    def _cheat_added(self) -> None:
        n = len(self.ctl.cheat_file.cheats) if self.ctl.cheat_file else 0
        if n > self._last_count and self.tabs.currentWidget() is self.find:
            self.tabs.setCurrentWidget(self.cheats)
        self._last_count = n

    def copy_log(self) -> None:
        """AC-9.4."""
        QGuiApplication.clipboard().setText(read_latest_log())
        self.status.setText("Log copied. Paste it into your bug report.")

    def open_lock(self, cheat: Cheat) -> None:
        LockWizard(self.ctl, cheat, self).exec()

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
