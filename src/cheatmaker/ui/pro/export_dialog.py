"""Export for CheatRunner (spec PR-12, AC-12.1): json / shn / mc4, CheatRunner cheats only,
and the PC-only cheats listed with why they are left out."""

from __future__ import annotations

import logging
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
from cheatmaker.core.pro.watch_session import WatchTarget, value_target
from cheatmaker.paths import default_save_dir
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.tab import ProMessages
from cheatmaker.ui.save_dialog import ask_overwrite

log = logging.getLogger(__name__)
EXPORT_FORMATS = (("json", ".json (GoldHEN)"), ("shn", ".shn"), ("mc4", ".mc4"))


class ExportDialog(QDialog):
    def __init__(
        self,
        ctl: Controller,
        msgs: ProMessages,
        parent=None,
        watch: Callable[[list[WatchTarget]], None] | None = None,
    ) -> None:
        """``watch``: "Make a cave at its writer" hands the value to the Watch tab."""
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
        self.make_cave_btn.setToolTip(hints.MAKE_CAVE_TIP)
        self.watch = watch or (lambda targets: None)
        self.left: list = []  # (cheat, badge) of the PC-only cheats, as listed
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
        self.make_cave_btn.clicked.connect(self.make_cave)
        self.left_out.currentRowChanged.connect(lambda _r: self._update_make_cave())
        self._fill()
        self._refresh()

    @property
    def format(self) -> str:
        return self.fmt.currentData()

    def _fill(self) -> None:
        n = len(runner_cheats(self.cf.cheats))
        self.count.setText(hints.EXPORT_COUNT.format(n=n))
        left = pc_only(self.cf.cheats, self.ctl.project)
        self.left = list(left)
        self.left_label.setText(hints.EXPORT_LEFT_OUT if left else hints.EXPORT_NONE_LEFT_OUT)
        self.left_out.setHidden(not left)
        self.make_cave_btn.setHidden(not left)
        for cheat, badge in left:
            item = QListWidgetItem(f"{cheat.name}: {', '.join(badge.reasons)}")
            item.setToolTip(badge.tooltip)
            self.left_out.addItem(item)
        self.save_btn.setEnabled(n > 0)
        self._update_make_cave()

    def _target(self) -> tuple[WatchTarget | None, str]:
        """The watch for the picked PC-only cheat, or why there is none."""
        k, game = self.left_out.currentRow(), self.ctl.session.game
        if game is None or not 0 <= k < len(self.left):
            return None, hints.MAKE_CAVE_TIP
        try:
            target = value_target(self.left[k][0], game.module_base)
        except ValueError as exc:  # an address the PS5 can't watch
            return None, str(exc)
        return target, hints.MAKE_CAVE_TIP if target is not None else hints.MAKE_CAVE_CANT

    def _update_make_cave(self) -> None:
        target, tip = self._target()
        self.make_cave_btn.setEnabled(target is not None)
        self.make_cave_btn.setToolTip(tip)

    def make_cave(self) -> None:
        """AC-12.1: watch what writes the picked cheat's value; the Watch tab then makes
        the cave entry at the writer."""
        target, _tip = self._target()
        if target is None:
            return
        self.accept()
        self.watch([target])

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
        log.info(
            "action=pro_export format=%s cheats=%d left_out=%d path=%s",
            self.format,
            len(runner_cheats(self.cf.cheats)),
            len(self.left),
            path,
        )
        self.result.setText(hints.EXPORT_SAVED.format(path=path))
        return path
