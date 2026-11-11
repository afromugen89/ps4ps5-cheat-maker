"""Connect bar and game detection (AC-1, AC-2)."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from cheatmaker.core import build_notes
from cheatmaker.core.session import GameInfo
from cheatmaker.errors import GameNotFound
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.messages import NO_GAME, report


def describe_game(g: GameInfo) -> str:
    return f"{g.title} · {g.title_id} · version {g.version} · {g.kind_label} · {g.process}"


class ConnectView(QWidget):
    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.ip = QLineEdit(ctl.settings.last_ip)
        self.ip.setPlaceholderText("PS5 IP address, e.g. 192.168.1.20")
        self.connect_btn = QPushButton("Connect")
        self.refresh_btn = QPushButton("Refresh")
        self.status = QLabel("Not connected")
        self.game = QLabel(hints.CONNECT)
        self.game.setWordWrap(True)
        row = QHBoxLayout(self)
        row.addWidget(QLabel("PS5 IP:"))
        row.addWidget(self.ip, 1)
        row.addWidget(self.connect_btn)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.status)
        row.addWidget(self.game, 3)
        self.connect_btn.clicked.connect(self.do_connect)
        self.ip.returnPressed.connect(self.do_connect)
        self.refresh_btn.clicked.connect(self.do_refresh)
        ctl.connection_changed.connect(self._on_connection)
        ctl.game_changed.connect(self._on_game)
        self._on_connection(False)

    def do_connect(self) -> None:
        ip, port = self.ip.text().strip(), self.ctl.settings.ng_port
        self.status.setText("Connecting...")
        self.connect_btn.setEnabled(False)
        self.ctl.run_before_disconnect()  # Connect again: take our hooks out first

        def work():
            self.ctl.session.connect(ip, port)
            game = self._detect()
            return game, self._left_over_fix() if game is not None else None

        self.ctl.tasks.run(work, self._connected, self._connect_failed)

    def _left_over_fix(self) -> str | None:
        """A Make it work test fix still open from last time (spec-cheat-builder B6)."""
        try:
            return build_notes.check_on_connect(self.ctl.session)
        except Exception as exc:  # the notes must never stop a connect; logged
            report(exc, "build")
            return None

    def _detect(self) -> GameInfo | None:
        try:
            return self.ctl.session.refresh_game()
        except GameNotFound:
            return None

    def _connected(self, result: tuple[GameInfo | None, str | None]) -> None:
        game, left_over = result
        self.ip.setText(self.ctl.session.ip)
        self.ctl.settings.last_ip = self.ctl.session.ip
        self.ctl.settings.save()
        self.ctl.connection_changed.emit(True)
        if game is None:
            self.ctl.message.emit(NO_GAME)
        elif left_over:
            self.ctl.message.emit(left_over)

    def _connect_failed(self, exc: BaseException) -> None:
        self._on_connection(False)
        self.ctl.fail(exc, "connect")

    def do_refresh(self) -> None:
        self.ctl.tasks.run(self._detect, self._refreshed, self.ctl.fail)

    def _refreshed(self, game: GameInfo | None) -> None:
        if game is None:
            self.ctl.message.emit(NO_GAME)

    def _on_connection(self, up: bool) -> None:
        self.connect_btn.setEnabled(True)
        self.refresh_btn.setEnabled(up)
        self.status.setText(
            f"Connected (PS5Debug-NG {self.ctl.session.server_version})" if up else "Not connected"
        )
        if not up:
            self.game.setText(hints.CONNECT)

    def _on_game(self, game: GameInfo | None) -> None:
        self.game.setText(describe_game(game) if game else hints.NO_GAME)
