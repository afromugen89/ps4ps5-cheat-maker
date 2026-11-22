"""Import a cheat file into Pro (spec PR-14): one Bytes entry per line, caves shown
disassembled, refused cheats listed with why, and "Convert to assembly"."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from cheatmaker.core.pro import importer
from cheatmaker.core.pro.asm import Assembler
from cheatmaker.core.pro.asm_text import CantConvert
from cheatmaker.core.pro.importer import ImportResult
from cheatmaker.core.pro.model import ProCheat, ProEntry
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.cave_editor import mono


def entry_text(entry: ProEntry) -> str:
    """How an entry reads in the import preview."""
    if entry.kind == "cave":
        return f"Cave hooked at +{entry.offset:#x}, at +{entry.cave:#x}:\n{entry.source}"
    head = f"{entry.kind.capitalize()} at +{entry.offset:#x}"
    if entry.kind == "bytes" and importer.is_cave(entry):
        lines = "\n".join("    " + t for t in importer.disassembly(entry))
        return f"{head} (a cave):\n{lines}"
    return f"{head}: {entry.on.hex(' ').upper()}"


class ImportDialog(QDialog):
    def __init__(
        self,
        ctl: Controller,
        result: ImportResult,
        assembler: Assembler | None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(hints.IMPORT_TITLE)
        self.ctl = ctl
        self.result = result
        self.assembler = assembler
        self.chosen: list[ProCheat] = []

        self.list = QListWidget()
        for pc in result.cheats:
            item = QListWidgetItem(pc.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(item)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(mono())
        refused = [f"{name}: {why}" for name, why in result.refused.items()]
        self.refused = QLabel(
            "\n".join([hints.IMPORT_REFUSED, *refused]) if refused else "",
        )
        self.refused.setWordWrap(True)
        self.message = QLabel("" if result.cheats else hints.IMPORT_NOTHING)
        self.message.setWordWrap(True)
        self.convert_btn = QPushButton(hints.CONVERT)
        if assembler is None:
            self.convert_btn.setToolTip(hints.ASSEMBLER_MISSING)
        self.add_btn = QPushButton(hints.IMPORT_ADD)
        self.cancel_btn = QPushButton("Cancel")

        side = QHBoxLayout()
        side.addWidget(self.list, 1)
        side.addWidget(self.preview, 2)
        brow = QHBoxLayout()
        for w in (self.convert_btn, self.add_btn, self.cancel_btn):
            brow.addWidget(w)
        lay = QVBoxLayout(self)
        lay.addLayout(side, 1)
        lay.addWidget(self.refused)
        lay.addWidget(self.message)
        lay.addLayout(brow)

        self.list.currentRowChanged.connect(lambda _r: self._show())
        self.convert_btn.clicked.connect(self.do_convert)
        self.add_btn.clicked.connect(self.do_add)
        self.cancel_btn.clicked.connect(self.reject)
        if result.cheats:
            self.list.setCurrentRow(0)
        self._update_buttons()

    def selected(self) -> ProCheat | None:
        row = self.list.currentRow()
        return self.result.cheats[row] if 0 <= row < len(self.result.cheats) else None

    def _show(self) -> None:
        pc = self.selected()
        self.preview.setPlainText("" if pc is None else "\n\n".join(map(entry_text, pc.entries)))
        self._update_buttons()

    def _update_buttons(self) -> None:
        pc = self.selected()
        self.convert_btn.setEnabled(
            pc is not None
            and self.assembler is not None
            and not any(e.kind == "cave" for e in pc.entries)
        )
        self.add_btn.setEnabled(bool(self.result.cheats))

    def do_convert(self) -> None:
        pc = self.selected()
        if pc is None or self.assembler is None:
            return
        try:
            n = importer.convert(pc, self.assembler)
        except CantConvert as exc:
            self.message.setText(str(exc))
            return
        self.message.setText(
            hints.CONVERTED.format(name=pc.name, n=n) if n else hints.NO_CAVE.format(name=pc.name)
        )
        self._show()

    def do_add(self) -> None:
        self.chosen = [
            pc
            for k, pc in enumerate(self.result.cheats)
            if self.list.item(k).checkState() == Qt.CheckState.Checked
        ]
        self.accept()
