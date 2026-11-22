"""The Pro Memory view (spec PR-7, read-only): 4 KiB a page, ``??`` for memory the game
doesn't have (AC-7.1), and a value inspector for every PR-2 type at the selected byte."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cheatmaker.core.pro import memory_page as mp
from cheatmaker.core.pro.addresses import parse_place, place_text
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.cave_editor import mono
from cheatmaker.ui.pro.tab import ProMessages, ProTab
from cheatmaker.ui.tasks import TaskRunner

COL_TEXT = mp.ROW  # the last column: the row as text


class MemoryTab(ProTab):
    def __init__(self, ctl: Controller, tasks: TaskRunner, msgs: ProMessages | None = None) -> None:
        super().__init__(ctl, tasks, msgs)
        self.page: mp.MemPage | None = None
        self.target: int | None = None
        self._gen = 0  # page requests: only the last one asked for is shown

        self.hint = QLabel(hints.MEMORY)
        self.hint.setWordWrap(True)
        self.place = QLineEdit()
        self.place.setPlaceholderText("0x20000000 or eboot.bin+0x1234")
        self.go_btn = QPushButton("Go")
        self.prev_btn = QPushButton("Previous page")
        self.next_btn = QPushButton("Next page")
        self.table = QTableWidget(mp.PAGE // mp.ROW, mp.ROW + 1)
        self.table.setHorizontalHeaderLabels([f"{k:X}" for k in range(mp.ROW)] + ["Text"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setFont(mono())
        self.table.horizontalHeader().setDefaultSectionSize(28)
        self.table.setColumnWidth(COL_TEXT, 160)
        self.selected_label = QLabel("")
        self.values: dict[str, QLabel] = {}
        form = QFormLayout()
        for vt in mp.INSPECT_TYPES:
            label = QLabel(mp.UNKNOWN)
            label.setFont(mono())
            self.values[vt.name] = label
            form.addRow(vt.name.lower(), label)
        inspector = QWidget()
        il = QVBoxLayout(inspector)
        il.addWidget(self.selected_label)
        il.addLayout(form)
        il.addStretch(1)

        top = QHBoxLayout()
        top.addWidget(self.prev_btn)
        top.addWidget(self.place, 1)
        top.addWidget(self.go_btn)
        top.addWidget(self.next_btn)
        mid = QHBoxLayout()
        mid.addWidget(self.table, 3)
        mid.addWidget(inspector, 1)
        lay = QVBoxLayout(self.body)
        lay.addWidget(self.hint)
        lay.addLayout(top)
        lay.addLayout(mid, 1)

        self.go_btn.clicked.connect(self.do_go)
        self.place.returnPressed.connect(self.do_go)
        self.prev_btn.clicked.connect(lambda: self._step(-mp.PAGE))
        self.next_btn.clicked.connect(lambda: self._step(mp.PAGE))
        self.table.currentCellChanged.connect(lambda *_a: self._inspect())

    def do_go(self) -> None:
        try:
            addr = parse_place(self.place.text(), self.ctl.session.require_game())
        except Exception as exc:
            self.msgs.fail(exc, "pro_memory")
            return
        self.go(addr)

    def _step(self, delta: int) -> None:
        if self.page is not None and self.page.start + delta >= 0:
            self.go(self.page.start + delta)

    def go(self, addr: int) -> None:
        """Show the page holding ``addr`` with that byte selected (also "Show in Memory")."""
        self._gen += 1
        gen, session = self._gen, self.ctl.session

        def failed(exc: BaseException) -> None:
            if gen == self._gen:
                self.msgs.fail(exc, "pro_memory")

        self.tasks.run(
            lambda: mp.read_page(session, addr), lambda page: self._loaded(gen, page, addr), failed
        )

    def _loaded(self, gen: int, page: mp.MemPage, addr: int) -> None:
        if gen != self._gen:
            return  # a newer page was asked for
        self.page, self.target = page, addr
        self.place.setText(place_text(addr, self.game))
        rows = len(page.data) // mp.ROW
        self.table.setVerticalHeaderLabels([f"{page.start + r * mp.ROW:X}" for r in range(rows)])
        for r in range(rows):
            row = page.data[r * mp.ROW : (r + 1) * mp.ROW]
            for c, b in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem(mp.hex_cell(b)))
            self.table.setItem(r, COL_TEXT, QTableWidgetItem(mp.text_cell(row)))
        at = addr - page.start
        self.table.setCurrentCell(at // mp.ROW, at % mp.ROW)
        self.table.scrollToItem(self.table.currentItem())
        self._inspect()

    def selected_address(self) -> int | None:
        r, c = self.table.currentRow(), self.table.currentColumn()
        if self.page is None or r < 0 or not 0 <= c < mp.ROW:
            return None
        return self.page.start + r * mp.ROW + c

    def _inspect(self) -> None:
        addr = self.selected_address()
        if addr is None or self.page is None:
            return
        self.selected_label.setText(hints.MEMORY_SELECTED.format(place=place_text(addr, self.game)))
        for vt, text in mp.inspect(self.page, addr):
            self.values[vt.name].setText(text)
