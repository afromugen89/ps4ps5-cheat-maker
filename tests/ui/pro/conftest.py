"""Shared fixtures for the Pro window tests: the main window on the fake game, and Pro."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from cheatmaker.core.pro.asm import FakeAssembler
from cheatmaker.settings import Settings
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.main_window import MainWindow
from cheatmaker.ui.tasks import TaskRunner
from tests import fakegame as fg


@pytest.fixture
def notices(monkeypatch) -> list[str]:
    """Message boxes shown (title, text) instead of blocking the test."""
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: shown.append(a[1]))
    return shown


@pytest.fixture
def warnings(monkeypatch) -> list[tuple[str, str]]:
    """Warning boxes shown (title, text)."""
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: shown.append((a[1], a[2])))
    return shown


@pytest.fixture
def questions(monkeypatch) -> list[str]:
    """Questions asked (title); each answered Yes."""
    asked: list[str] = []

    def ask(*a):
        asked.append(a[1])
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", ask)
    return asked


@pytest.fixture
def game():
    return fg.build(1, connect=False)


@pytest.fixture
def win(qtbot, game, notices, warnings, questions):
    ctl = Controller(lambda ip, port: game.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    w.pro_assembler_factory = FakeAssembler
    qtbot.addWidget(w)
    w.connect_view.ip.setText("192.168.1.50")
    w.connect_view.connect_btn.click()
    yield w
    if w.pro is not None:
        w.pro.force_close = True
        w.pro.close()
    ctl.shutdown()


@pytest.fixture
def pro(win, qtbot):
    win.open_pro()
    qtbot.addWidget(win.pro)
    return win.pro


@pytest.fixture
def tab(pro):
    """The Cheats tab, with no waiting between a jump and its cave going (DISABLE_WAIT)."""
    t = pro.cheats
    t.sleep = lambda _s: None
    return t
