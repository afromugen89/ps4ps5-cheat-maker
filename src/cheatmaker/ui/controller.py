"""App state shared by the screens: session, scanner, freezer, live cheats, open file."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from cheatmaker.core.cheats import CheatFile
from cheatmaker.core.freezer import Freezer, LiveCheats
from cheatmaker.core.project import Project
from cheatmaker.core.scanner import Scanner
from cheatmaker.core.session import ClientFactory, GameInfo, Session
from cheatmaker.errors import ConnectionLost
from cheatmaker.settings import Settings
from cheatmaker.ui.messages import LOST, report
from cheatmaker.ui.tasks import TaskRunner


class Controller(QObject):
    message = Signal(str)  # one-line plain message for the status area
    connection_changed = Signal(bool)
    game_changed = Signal(object)  # GameInfo | None
    cheats_changed = Signal()
    search_lost = Signal()  # the player's search was ended by another (any thread)
    _lost = Signal()  # freezer thread -> UI thread

    def __init__(
        self,
        factory: ClientFactory,
        settings: Settings,
        tasks: TaskRunner | None = None,
        uploader_factory: Callable | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.tasks = tasks or TaskRunner(parent=self)
        self.session = Session(factory)
        self.scanner = Scanner(self.session)
        self.scanner.on_lost.append(self.search_lost.emit)
        self.scanner.scan_log.enabled = settings.save_scan_files
        self.scanner.scan_log.max_lines = settings.scan_file_max
        self.freezer = Freezer(
            self.session,
            on_lost=self._lost.emit,
            on_error=lambda _k, exc: self.message.emit(report(exc, "freeze")),
        )
        self.live = LiveCheats(self.session, self.freezer)
        self.cheat_file: CheatFile | None = None
        self.uploader_factory = uploader_factory
        self.frozen_addresses: set[int] = set()
        self.hit_test_warned = False  # AC-H6 warning is shown once per run
        self.project: Project | None = None  # v3 "find it again" notes for this game
        self.before_disconnect: list[Callable[[], None]] = []  # e.g. take hooks out
        self._lost.connect(self.handle_lost)
        self.session.on_game_changed.append(self._on_game)

    # -- errors --------------------------------------------------------------
    def fail(self, exc: BaseException, action: str | None = None) -> None:
        """Every UI error goes through here: log, show, and handle a dropped link."""
        if isinstance(exc, ConnectionLost):
            report(exc, action)
            self.handle_lost()
            return
        self.message.emit(report(exc, action))

    def handle_lost(self) -> None:
        """AC-1.4: say so, mark every cheat off, keep running."""
        if not self.session.connected:
            return
        self.live.mark_all_off()
        self.frozen_addresses.clear()
        self.freezer.clear()
        try:
            self.session.disconnect()
        except Exception as exc:  # closing a dead socket can fail; nothing to do
            report(exc, "disconnect")
        self.cheats_changed.emit()
        self.connection_changed.emit(False)
        self.message.emit(LOST)

    # -- game / file ----------------------------------------------------------
    def _on_game(self, game: GameInfo | None) -> None:
        self.live.mark_all_off()
        self.live.trackers.clear()  # objects are somewhere else in a new run
        self.freezer.clear()
        self.frozen_addresses.clear()
        if game is not None and (
            self.cheat_file is None or self.cheat_file.title_id != game.title_id
        ):
            self.cheat_file = CheatFile(game.title, game.title_id, game.version)
        self.game_changed.emit(game)
        self.cheats_changed.emit()

    def ensure_file(self) -> CheatFile:
        if self.cheat_file is None:
            g = self.session.require_game()
            self.cheat_file = CheatFile(g.title, g.title_id, g.version)
        return self.cheat_file

    def run_before_disconnect(self) -> None:
        """Undo what we put in the game (e.g. hooks) while the link still works."""
        if not self.session.connected:
            return
        for fn in list(self.before_disconnect):
            try:
                fn()
            except Exception as exc:  # closing must go on; the reason is logged
                report(exc, "disconnect")

    def shutdown(self) -> None:
        self.freezer.stop()
        self.run_before_disconnect()
        self.session.disconnect()
