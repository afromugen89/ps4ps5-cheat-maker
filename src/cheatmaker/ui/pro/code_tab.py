"""The Pro Code view (spec PR-6): 256 instructions a page in NASM syntax, follow and
back/forward, cheat sites marked, "show original", copy AOB, make a NOP/cave entry."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from cheatmaker.core.pro import code_page as cp
from cheatmaker.core.pro.addresses import parse_place, place_text
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.cave_editor import mono
from cheatmaker.ui.pro.tab import ProTab, fail
from cheatmaker.ui.tasks import TaskRunner

COL_ADDR, COL_BYTES, COL_TEXT, COL_CHEAT = range(4)
_MARKED = QColor("#fff2cc")


class CodeTab(ProTab):
    def __init__(self, ctl: Controller, tasks: TaskRunner) -> None:
        super().__init__(ctl, tasks)
        # (kind, module offset) -> the Cheats tab adds the entry (set by the window)
        self.make_entry: Callable[[str, int], None] = lambda kind, offset: None
        self.page: cp.Page | None = None
        self.rows: list[cp.Line] = []
        self.back: list[int] = []
        self.forward: list[int] = []

        self.place = QLineEdit()
        self.place.setPlaceholderText("eboot.bin+0x1234")
        self.go_btn = QPushButton("Go")
        self.back_btn = QPushButton("Back")
        self.forward_btn = QPushButton("Forward")
        self.original = QCheckBox(hints.SHOW_ORIGINAL)
        self.original.setToolTip(hints.SHOW_ORIGINAL_TIP)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Address", "Bytes", "Instruction", "Cheat"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setFont(mono())
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(COL_TEXT, QHeaderView.ResizeMode.Stretch)
        self.aob_btn = QPushButton("Copy AOB")
        self.nop_btn = QPushButton(hints.CODE_MAKE_NOP)
        self.cave_btn = QPushButton(hints.CODE_MAKE_CAVE)
        for b in (self.nop_btn, self.cave_btn):
            b.setToolTip(hints.MAKE_ENTRY_TIP)
        self.hint = QLabel(hints.CODE)
        self.hint.setWordWrap(True)

        top = QHBoxLayout()
        for w in (self.back_btn, self.forward_btn):
            top.addWidget(w)
        top.addWidget(self.place, 1)
        top.addWidget(self.go_btn)
        top.addWidget(self.original)
        actions = QHBoxLayout()
        for w in (self.aob_btn, self.nop_btn, self.cave_btn):
            actions.addWidget(w)
        actions.addStretch(1)
        lay = QVBoxLayout(self.body)
        lay.addWidget(self.hint)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addLayout(actions)

        self.go_btn.clicked.connect(self.do_go)
        self.place.returnPressed.connect(self.do_go)
        self.back_btn.clicked.connect(self.do_back)
        self.forward_btn.clicked.connect(self.do_forward)
        self.original.toggled.connect(lambda _on: self._show())
        self.table.cellDoubleClicked.connect(lambda row, _col: self.follow(row))
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.aob_btn.clicked.connect(self.copy_aob)
        self.nop_btn.clicked.connect(lambda: self._make("nop"))
        self.cave_btn.clicked.connect(lambda: self._make("cave"))
        ctl.cheats_changed.connect(self._show)
        self._update_buttons()

    # -- navigation ------------------------------------------------------------------------
    def do_go(self) -> None:
        try:
            addr = parse_place(self.place.text(), self.ctl.session.require_game())
        except Exception as exc:
            fail(self.ctl, exc, "pro_code")
            return
        self.go(addr)

    def go(self, addr: int, remember: bool = True) -> None:
        """Load the page at ``addr`` and select its instruction (AC-6.1)."""
        if remember and self.page is not None:
            self.back.append(self.page.target)
            self.forward.clear()
        session = self.ctl.session
        self.tasks.run(lambda: cp.read_page(session, addr), self._loaded, self._load_failed)

    def _loaded(self, page: cp.Page) -> None:
        self.page = page
        self.place.setText(place_text(page.target, self.game))
        self._show()

    def _load_failed(self, exc: BaseException) -> None:
        fail(self.ctl, exc, "pro_code")
        self._update_buttons()

    def do_back(self) -> None:
        if self.back and self.page is not None:
            self.forward.append(self.page.target)
            self.go(self.back.pop(), remember=False)

    def do_forward(self) -> None:
        if self.forward and self.page is not None:
            self.back.append(self.page.target)
            self.go(self.forward.pop(), remember=False)

    def follow(self, row: int) -> None:
        if 0 <= row < len(self.rows) and self.rows[row].target is not None:
            self.go(self.rows[row].target)

    # -- the page ------------------------------------------------------------------------------
    def sites(self) -> list[cp.Site]:
        cf, game = self.ctl.cheat_file, self.game
        if cf is None or game is None:
            return []
        return cp.cheat_sites(cf.cheats, game.module_base)

    def _show(self) -> None:
        if self.page is None:
            return
        self.rows = cp.lines(self.page, self.sites(), self.original.isChecked())
        game = self.game
        self.table.setRowCount(len(self.rows))
        selected = 0
        for k, ln in enumerate(self.rows):
            cells = (place_text(ln.ip, game), ln.raw.hex(" ").upper(), ln.text, ln.mark)
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if ln.mark:
                    item.setBackground(_MARKED)
                self.table.setItem(k, col, item)
            if ln.ip == self.page.target:
                selected = k
        if self.rows:
            self.table.selectRow(selected)
            self.table.scrollToItem(
                self.table.item(selected, 0), QAbstractItemView.ScrollHint.PositionAtCenter
            )
        self._update_buttons()

    def selected_line(self) -> cp.Line | None:
        rows = self.table.selectionModel().selectedRows()
        row = rows[0].row() if rows else -1
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def _update_buttons(self) -> None:
        ln, game = self.selected_line(), self.game
        in_module = (
            ln is not None and game is not None and game.module_base <= ln.ip < game.module_end
        )
        self.aob_btn.setEnabled(ln is not None)
        self.nop_btn.setEnabled(in_module)
        self.cave_btn.setEnabled(in_module)
        self.back_btn.setEnabled(bool(self.back))
        self.forward_btn.setEnabled(bool(self.forward))

    # -- actions ---------------------------------------------------------------------------------
    def copy_aob(self) -> str | None:
        ln = self.selected_line()
        if ln is None or self.page is None:
            return None
        try:
            text = cp.aob(self.page, ln.ip, self.original.isChecked(), self.sites())
        except ValueError as exc:
            fail(self.ctl, exc, "pro_aob")
            return None
        QGuiApplication.clipboard().setText(text)
        self.ctl.message.emit(hints.AOB_COPIED.format(aob=text))
        return text

    def _make(self, kind: str) -> None:
        ln, game = self.selected_line(), self.game
        if ln is not None and game is not None:
            self.make_entry(kind, ln.ip - game.module_base)
