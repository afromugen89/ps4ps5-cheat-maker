"""Export for CheatRunner (spec PR-12, AC-12.1): json / shn / mc4, CheatRunner cheats only,
and the PC-only cheats listed with why they are left out."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from cheatmaker import formats
from cheatmaker.console.uploader import save_to_pc
from cheatmaker.core.pro.badge import pc_only, runner_cheats
from cheatmaker.paths import default_save_dir
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.tab import ProMessages
from cheatmaker.ui.save_dialog import ask_overwrite

EXPORT_FORMATS = (("json", ".json (GoldHEN)"), ("shn", ".shn"), ("mc4", ".mc4"))


class ExportDialog(QDialog):
    def __init__(self, ctl: Controller, msgs: ProMessages, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(hints.EXPORT_TITLE)
        self.ctl = ctl
        self.msgs = msgs
        self.cf = ctl.ensure_file()
        self.confirm: Callable[[str], bool] = lambda name: ask_overwrite(self, name)
        self.saved: Path | None = None

        self.fmt = QComboBox()
        for key, label in EXPORT_FORMATS:
            self.fmt.addItem(label, key)
        self.folder = QLineEdit(str(default_save_dir()))
        self.count = QLabel("")
        self.left_label = QLabel("")
        self.left_out = QListWidget()
        self.make_cave_btn = QPushButton(hints.MAKE_CAVE)
        self.make_cave_btn.setEnabled(False)  # needs Watch (architecture step 17)
        self.make_cave_btn.setToolTip(hints.MAKE_CAVE_LATER)
        self.file_label = QLabel("")
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.save_btn = QPushButton("Export")
        self.close_btn = QPushButton("Close")

        frow = QHBoxLayout()
        frow.addWidget(QLabel("Format:"))
        frow.addWidget(self.fmt, 1)
        drow = QHBoxLayout()
        drow.addWidget(QLabel("Folder:"))
        drow.addWidget(self.folder, 1)
        brow = QHBoxLayout()
        brow.addWidget(self.save_btn)
        brow.addWidget(self.close_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.count)
        lay.addWidget(self.left_label)
        lay.addWidget(self.left_out)
        lay.addWidget(self.make_cave_btn)
        lay.addLayout(frow)
        lay.addLayout(drow)
        lay.addWidget(self.file_label)
        lay.addWidget(self.result)
        lay.addLayout(brow)

        self.fmt.currentIndexChanged.connect(lambda _i: self._refresh())
        self.save_btn.clicked.connect(self.do_export)
        self.close_btn.clicked.connect(self.accept)
        self._fill()
        self._refresh()

    @property
    def format(self) -> str:
        return self.fmt.currentData()

    def _fill(self) -> None:
        n = len(runner_cheats(self.cf.cheats))
        self.count.setText(hints.EXPORT_COUNT.format(n=n))
        left = pc_only(self.cf.cheats, self.ctl.project)
        self.left_label.setText(hints.EXPORT_LEFT_OUT if left else hints.EXPORT_NONE_LEFT_OUT)
        self.left_out.setHidden(not left)
        self.make_cave_btn.setHidden(not left)
        for cheat, badge in left:
            item = QListWidgetItem(f"{cheat.name}: {', '.join(badge.reasons)}")
            item.setToolTip(badge.tooltip)
            self.left_out.addItem(item)
        self.save_btn.setEnabled(n > 0)

    def _refresh(self) -> None:
        self.file_label.setText(f"File: {self.cf.file_name(self.format)}")

    def do_export(self) -> Path | None:
        try:
            name = self.cf.file_name(self.format)
            data = formats.dumps(self.cf, self.format)
            folder = Path(self.folder.text().strip() or default_save_dir())
            try:
                path = save_to_pc(folder, name, data)
            except FileExistsError:
                if not self.confirm(name):
                    return None
                path = save_to_pc(folder, name, data, overwrite=True)
        except Exception as exc:
            self.msgs.fail(exc, "pro_export")
            return None
        self.saved = path
        self.result.setText(hints.EXPORT_SAVED.format(path=path))
        return path
