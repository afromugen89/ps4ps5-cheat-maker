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

from cheatmaker.core.pro.addresses import place_text
from cheatmaker.core.pro.asm import Assembler, AssemblerNotFound, default_assembler
from cheatmaker.core.pro.compile import SiteChanged
from cheatmaker.core.pro.watch_session import WatchTarget
from cheatmaker.core.session import GameInfo
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.address_list import AddressListTab
from cheatmaker.ui.pro.cheats_tab import CheatsTab
from cheatmaker.ui.pro.code_tab import CodeTab
from cheatmaker.ui.pro.memory_tab import MemoryTab
from cheatmaker.ui.pro.scanner_tab import ScannerTab
from cheatmaker.ui.pro.tab import ProMessages, ProTab, game_text
from cheatmaker.ui.pro.watch_tab import WatchTab
from cheatmaker.ui.tasks import TaskRunner

log = logging.getLogger(__name__)

POLL_MS = 2000


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
        self.msgs = ProMessages(ctl)
        self.assembler = self._make_assembler(assembler_factory)
        self.force_close = False  # the app is closing: no questions
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
        self.status = QLabel("")  # Pro's own status line (never the main window's)
        self.status.setWordWrap(True)
        self.statusBar().addWidget(self.status, 1)
        self.msgs.message.connect(self.status.setText)
        self.msgs.site_changed.connect(self.show_site_changed)

        self.alive = True
        self._polling = False
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)
        ctl.game_changed.connect(self._on_game)
        ctl.connection_changed.connect(lambda _up: self._show_state())
        self._show_state()

    def _make_assembler(self, factory: Callable[[], Assembler] | None) -> Assembler | None:
        """The assembler, or None: the Cheats tab then says "Assembler not found"."""
        try:
            return (factory or (lambda: default_assembler(self.ctl.settings.nasm_path)))()
        except AssemblerNotFound as exc:
            log.warning("action=pro_assembler result=missing reason=%s", exc)
            return None

    def _build_tabs(self) -> None:
        self.scanner = ScannerTab(self.ctl, self.tasks, self.msgs)
        self.add_tab(self.scanner, hints.TAB_SCANNER)
        self.addresses = AddressListTab(self.ctl, self.tasks, self.msgs)
        self.add_tab(self.addresses, hints.TAB_ADDRESSES)
        self.code = CodeTab(self.ctl, self.tasks, self.msgs)
        self.add_tab(self.code, hints.TAB_CODE)
        self.memory = MemoryTab(self.ctl, self.tasks, self.msgs)
        self.add_tab(self.memory, hints.TAB_MEMORY)
        self.watch = WatchTab(self.ctl, self.tasks, self.msgs)
        self.add_tab(self.watch, hints.TAB_WATCH)
        self.cheats = CheatsTab(self.ctl, self.tasks, self.assembler, self.save, self.msgs)
        self.add_tab(self.cheats, hints.TAB_CHEATS)
        self.code.make_entry = self._entry_from_code
        self.scanner.add_address = lambda addr, vtype: self.addresses.add(addr, vtype)
        self.scanner.show_code = lambda addr: self._show(self.code, addr)
        self.addresses.show_memory = lambda addr: self._show(self.memory, addr)
        self.addresses.watch = self.watch_targets
        self.watch.make_entry = self._entry_from_code
        self.watch.show_code = lambda addr: self._show(self.code, addr)
        self.watch.access.add_address = lambda addr, vtype: self.addresses.add(addr, vtype)
        self.code.sample = self.sample_access
        self.cheats.watch = self.watch_targets

    def _show(self, tab: CodeTab | MemoryTab, addr: int) -> None:
        self.tabs.setCurrentWidget(tab)
        tab.go(addr)

    def watch_targets(self, targets: list[WatchTarget]) -> None:
        """Find what writes / accesses (address list, export dialog): the Watch tab."""
        self.tabs.setCurrentWidget(self.watch)
        self.watch.watch(targets)

    def sample_access(self, addr: int) -> None:
        """What does the instruction at ``addr`` access (Code view): the Watch tab."""
        self.tabs.setCurrentWidget(self.watch)
        self.watch.access.sample_at(addr)

    def _entry_from_code(self, kind: str, offset: int) -> None:
        self.tabs.setCurrentWidget(self.cheats)
        self.cheats.entry_here(kind, offset)

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
        self.msgs.fail(exc, "pro_poll")

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

    # -- details --------------------------------------------------------------------
    def show_site_changed(self, exc: SiteChanged) -> None:
        """Every site that holds other bytes than recorded: place, expected, found."""
        game = self.game
        rows = [
            hints.SITE_ROW.format(
                place=place_text(addr, game),
                expected=want.hex(" ").upper(),
                found=got.hex(" ").upper(),
            )
            for addr, want, got in exc.found
        ]
        QMessageBox.warning(
            self, hints.SITE_CHANGED_TITLE, "\n\n".join([hints.SITE_CHANGED, *rows])
        )

    # -- showing and closing ------------------------------------------------------------
    def showEvent(self, event) -> None:
        """(Re)opened: the liveness poll runs again, starting now."""
        super().showEvent(event)
        if not self.timer.isActive():
            self.timer.start()
            self.poll()

    def confirm_close(self, frozen: int) -> bool:
        answer = QMessageBox.question(
            self, hints.CLOSE_FROZEN_TITLE, hints.CLOSE_FROZEN.format(n=frozen)
        )
        return answer == QMessageBox.StandardButton.Yes

    def closeEvent(self, event) -> None:
        frozen = self.addresses.frozen_count()
        if frozen and not self.force_close and not self.confirm_close(frozen):
            event.ignore()
            return
        self.timer.stop()
        for tab in self.pro_tabs:
            tab.closing()
        super().closeEvent(event)
