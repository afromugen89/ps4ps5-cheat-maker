"""The Pro window (spec PR-1): a second window on the main window's connection and game.

- **One connection.** Every tab uses the ``Controller``'s session; nothing here opens a
  console connection (AC-1.1). Pro work runs on its own ``TaskRunner`` so the guided
  screens' busy state never flickers with the Pro window's polls.
- **Liveness.** Every ``POLL_MS`` the window asks ``Session.game_alive`` on the worker;
  when the game has gone, every tab shows "game not running" (AC-1.2, within 5 s).
- **Safety notice** once per game (title ID), kept in the settings.
- Closing the window stops what the Pro tabs run, never what the guided mode started.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QMainWindow, QMessageBox, QTabWidget, QVBoxLayout, QWidget

from cheatmaker.core.pro.asm import Assembler, AssemblerNotFound, default_assembler
from cheatmaker.core.session import GameInfo
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.cheats_tab import CheatsTab
from cheatmaker.ui.pro.tab import ProTab, game_text
from cheatmaker.ui.tasks import TaskRunner

log = logging.getLogger(__name__)

POLL_MS = 2000


class _Placeholder(ProTab):
    def __init__(self, ctl: Controller, tasks: TaskRunner, text: str) -> None:
        super().__init__(ctl, tasks)
        lay = QVBoxLayout(self.body)
        label = QLabel(text)
        label.setWordWrap(True)
        lay.addWidget(label)
        lay.addStretch(1)


class ProWindow(QMainWindow):
    def __init__(
        self,
        ctl: Controller,
        parent: QWidget | None = None,
        *,
        save: Callable[[], None] | None = None,
        assembler_factory: Callable[[], Assembler] | None = None,
    ) -> None:
        """``save`` keeps Pro sources in the game's notes (My cheats' FindAgain.save);
        ``assembler_factory`` defaults to NASM from the settings (``nasm_path``)."""
        super().__init__(parent)
        self.ctl = ctl
        self.tasks = TaskRunner(sync=ctl.tasks.sync, parent=self)
        self.save = save or (lambda: None)
        self.assembler = self._make_assembler(assembler_factory)
        self.setWindowTitle(hints.WINDOW_TITLE)
        self.resize(1100, 720)
        self.header = QLabel()
        self.header.setWordWrap(True)
        self.tabs = QTabWidget()
        self.pro_tabs: list[ProTab] = []
        self._build_tabs()
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.addWidget(self.header)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(root)

        self.alive = True
        self._polling = False
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)
        ctl.game_changed.connect(self._on_game)
        ctl.connection_changed.connect(lambda _up: self._show_state())
        self._show_state()
        self.timer.start()

    def _make_assembler(self, factory: Callable[[], Assembler] | None) -> Assembler | None:
        """The assembler, or None: the Cheats tab then says "Assembler not found"."""
        try:
            return (factory or (lambda: default_assembler(self.ctl.settings.nasm_path)))()
        except AssemblerNotFound as exc:
            log.warning("action=pro_assembler result=missing reason=%s", exc)
            return None

    def _build_tabs(self) -> None:
        for title in (hints.TAB_SCANNER, hints.TAB_ADDRESSES, hints.TAB_CODE, hints.TAB_MEMORY):
            self.add_tab(_Placeholder(self.ctl, self.tasks, ""), title)
        self.add_tab(_Placeholder(self.ctl, self.tasks, hints.WATCH_LATER), hints.TAB_WATCH)
        self.cheats = CheatsTab(self.ctl, self.tasks, self.assembler, self.save)
        self.add_tab(self.cheats, hints.TAB_CHEATS)

    def add_tab(self, tab: ProTab, title: str) -> None:
        self.pro_tabs.append(tab)
        self.tabs.addTab(tab, title)

    # -- game state ---------------------------------------------------------------
    @property
    def game(self) -> GameInfo | None:
        return self.ctl.session.game if self.ctl.session.connected else None

    def _show_state(self) -> None:
        game = self.game
        self.header.setText(game_text(game, self.ctl.session.connected, self.alive))
        for tab in self.pro_tabs:
            tab.set_game_state(game, self.alive)

    def _on_game(self, game: GameInfo | None) -> None:
        self.alive = True  # a newly found game runs
        self._show_state()
        if game is not None and self.isVisible():
            self.show_safety_notice()

    def poll(self) -> None:
        """Ask on the worker whether the game still runs (AC-1.2)."""
        game = self.game
        if self._polling or game is None:
            return
        self._polling = True
        self.tasks.run(
            self.ctl.session.game_alive, lambda ok: self._polled(game, ok), self._poll_failed
        )

    def _polled(self, game: GameInfo, alive: bool) -> None:
        self._polling = False
        if self.game != game or alive == self.alive:
            return  # another game meanwhile, or nothing new
        log.info("action=pro_game_alive pid=%d alive=%s", game.pid, alive)
        self.alive = alive
        self._show_state()

    def _poll_failed(self, exc: BaseException) -> None:
        self._polling = False
        self.ctl.fail(exc, "pro_poll")

    # -- safety notice -----------------------------------------------------------
    def show_safety_notice(self) -> None:
        """Once per game (title ID): single-player, offline, back up the save first."""
        game = self.game
        shown = self.ctl.settings.pro_notice_shown
        if game is None or game.title_id in shown:
            return
        shown.append(game.title_id)
        self.ctl.settings.save()
        QMessageBox.information(self, hints.NOTICE_TITLE, hints.NOTICE)

    def closeEvent(self, event) -> None:
        self.timer.stop()
        for tab in self.pro_tabs:
            tab.closing()
        super().closeEvent(event)
