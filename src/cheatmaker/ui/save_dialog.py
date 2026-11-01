"""Save and send (AC-6.1-6.3, AC-6.5)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from cheatmaker import formats
from cheatmaker.console.uploader import Uploader, remote_path, save_to_pc
from cheatmaker.paths import default_save_dir
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller


def ask_overwrite(parent, name: str) -> bool:
    return (
        QMessageBox.question(parent, "Replace file?", f"{name} already exists. Replace it?")
        == QMessageBox.StandardButton.Yes
    )


class SaveDialog(QDialog):
    def __init__(self, ctl: Controller, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save cheats")
        self.ctl = ctl
        self.confirm: Callable[[str], bool] = lambda name: ask_overwrite(self, name)
        self.last_saved: Path | None = None

        self.hint = QLabel(hints.SAVE)
        self.hint.setWordWrap(True)
        self.fmt = QComboBox()
        self.fmt.addItem(".json (recommended)", "json")
        self.fmt.addItem(".shn", "shn")
        self.fmt.setCurrentIndex(0 if ctl.settings.output_format == "json" else 1)
        self.folder = QLineEdit(str(default_save_dir()))
        self.browse_btn = QPushButton("Choose folder...")
        self.file_label = QLabel("")
        self.skipped = QLabel("")
        self.skipped.setWordWrap(True)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(self.result.textInteractionFlags())
        self.save_btn = QPushButton("Save to PC")
        self.send_btn = QPushButton("Send to PS5")
        self.close_btn = QPushButton("Close")

        frow = QHBoxLayout()
        frow.addWidget(QLabel("Format:"))
        frow.addWidget(self.fmt, 1)
        drow = QHBoxLayout()
        drow.addWidget(QLabel("Folder:"))
        drow.addWidget(self.folder, 1)
        drow.addWidget(self.browse_btn)
        brow = QHBoxLayout()
        for w in (self.save_btn, self.send_btn, self.close_btn):
            brow.addWidget(w)
        lay = QVBoxLayout(self)
        for w in (self.hint, self.file_label, self.skipped):
            lay.addWidget(w)
        lay.addLayout(frow)
        lay.addLayout(drow)
        lay.addWidget(self.result)
        lay.addLayout(brow)

        self.fmt.currentIndexChanged.connect(lambda _i: self._refresh())
        self.browse_btn.clicked.connect(self._browse)
        self.save_btn.clicked.connect(self.do_save)
        self.send_btn.clicked.connect(self.do_send)
        self.close_btn.clicked.connect(self.accept)
        self._refresh()

    @property
    def format(self) -> str:
        return self.fmt.currentData()

    def _refresh(self) -> None:
        cf = self.ctl.ensure_file()
        self.file_label.setText(f"File: {cf.file_name(self.format)}")
        n = sum(1 for c in cf.cheats if not c.saveable)
        self.skipped.setText(
            f'{n} cheat(s) marked "This session only" won\'t be saved. Use Lock it first.'
            if n
            else ""
        )
        self.send_btn.setEnabled(self.ctl.session.connected)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Save cheats to", self.folder.text())
        if d:
            self.folder.setText(d)

    def _payload(self) -> tuple[str, bytes]:
        cf = self.ctl.ensure_file()
        self.ctl.settings.output_format = self.format
        self.ctl.settings.save()
        return cf.file_name(self.format), formats.dumps(cf, self.format)

    def do_save(self) -> Path | None:
        try:
            name, data = self._payload()
            folder = Path(self.folder.text().strip() or default_save_dir())
            try:
                path = save_to_pc(folder, name, data)
            except FileExistsError:
                if not self.confirm(name):
                    return None
                path = save_to_pc(folder, name, data, overwrite=True)
        except Exception as exc:
            self.ctl.fail(exc, "file_save")
            return None
        self.last_saved = path
        self.result.setText(
            f"Saved to {path}.\nTo use it, copy it to {remote_path(self.format, name)} on your "
            "PS5, or press Send to PS5."
        )
        return path

    def do_send(self) -> None:
        if self.do_save() is None:
            return
        name, data = self._payload()
        fmt = self.format
        factory = self.ctl.uploader_factory or (
            lambda: Uploader(self.ctl.session.ip, self.ctl.settings.ftp_port)
        )
        up = factory()

        def check():
            return up.exists(fmt, name)

        def after_check(exists: bool) -> None:
            if exists and not self.confirm(f"{name} on the PS5"):
                return
            self.ctl.tasks.run(lambda: up.upload(fmt, name, data), self._sent, self._send_failed)

        self.ctl.tasks.run(check, after_check, self._send_failed)

    def _sent(self, path: str) -> None:
        self.result.setText(f"Sent to the PS5: {path}. Turn it on in CheatRunner.")
        self.ctl.message.emit("Sent to the PS5.")

    def _send_failed(self, exc: BaseException) -> None:
        self.ctl.fail(exc, "upload")
        if self.last_saved:
            self.result.setText(
                f"Couldn't send it, but it's saved at {self.last_saved}.\nCopy it to "
                f"{remote_path(self.format, self.last_saved.name)} on your PS5."
            )
