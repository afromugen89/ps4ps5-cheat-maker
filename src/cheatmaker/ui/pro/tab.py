"""What every Pro tab shares: the game it works on, and "game not running" (AC-1.2)."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from cheatmaker.core.pro.asm_text import CantConvert
from cheatmaker.core.pro.compile import SiteChanged
from cheatmaker.core.pro.sites import Overlap
from cheatmaker.core.session import GameInfo
from cheatmaker.errors import LockFailed
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.messages import report
from cheatmaker.ui.pro import hints
from cheatmaker.ui.tasks import TaskRunner

PRO_ERRORS = (SiteChanged, Overlap, LockFailed, CantConvert)
"""Errors whose own text is the message in Pro (they name the site, the bytes, the other
cheat or the room); the guided wording ("Couldn't lock this value") would mislead."""


def fail(ctl: Controller, exc: BaseException, action: str) -> None:
    """``Controller.fail`` with Pro's own errors shown as they read."""
    if isinstance(exc, PRO_ERRORS):
        ctl.message.emit(str(exc))
        report(exc, action)
        return
    ctl.fail(exc, action)


def game_text(game: GameInfo | None, connected: bool, alive: bool) -> str:
    """The shared game header's text (AC-1.1): the same game as the main window."""
    if not connected:
        return hints.NOT_CONNECTED
    if game is None:
        return hints.NO_GAME
    if not alive:
        return hints.GAME_NOT_RUNNING
    return hints.GAME.format(
        title=game.title, title_id=game.title_id, version=game.version, pid=game.pid
    )


class ProTab(QWidget):
    """A Pro tab: ``body`` holds its controls and is disabled while there is no running
    game; ``banner`` then says why. Subclasses put their widgets in ``body`` and react
    in ``game_state_changed``."""

    def __init__(self, ctl: Controller, tasks: TaskRunner) -> None:
        super().__init__()
        self.ctl = ctl
        self.tasks = tasks
        self.game: GameInfo | None = None
        self.alive = False
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("font-weight: bold;")
        self.body = QWidget()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.banner)
        lay.addWidget(self.body, 1)

    @property
    def running(self) -> bool:
        return self.alive and self.game is not None

    def set_game_state(self, game: GameInfo | None, alive: bool) -> None:
        changed = game != self.game
        self.game, self.alive = game, alive and game is not None
        self.banner.setText(game_text(game, self.ctl.session.connected, self.alive))
        self.banner.setHidden(self.running)
        self.body.setEnabled(self.running)
        self.game_state_changed(changed)

    def game_state_changed(self, new_game: bool) -> None:
        """Called after every state change; ``new_game`` when the game itself changed."""

    def closing(self) -> None:
        """The Pro window is closing: stop what this tab runs (never guided work)."""
