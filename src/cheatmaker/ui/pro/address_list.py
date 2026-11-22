"""The Pro address list (spec PR-2): description, type, value, set, freeze, group,
comment and badge, plus Show in Memory and Pointer scan. Freezes use the shared
Freezer under ``pro:`` keys, so they never touch a guided freeze."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from cheatmaker.core.pro import addresses as ad
from cheatmaker.core.pro import compile as pc_compile
from cheatmaker.core.values import ValueType
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pointer_dialog import PointerDialog
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.tab import ProTab, fail
from cheatmaker.ui.tasks import TaskRunner

COL_DESC, COL_ADDR, COL_TYPE, COL_VALUE, COL_FREEZE, COL_GROUP, COL_COMMENT, COL_BADGE = range(8)
HEADERS = ["Description", "Address", "Type", "Value", "Freeze", "Group", "Comment", "Badge"]
TYPES = tuple(ValueType)


def parse_value(text: str, vtype: ValueType) -> float | int:
    text = text.strip()
    try:
        value = float(text) if vtype.is_float else int(text, 0)
    except ValueError:
        raise ValueError(f"“{text}” isn't a {vtype.label.lower()}.") from None
    vtype.encode(value)  # ValueError when it doesn't fit
    return value


class AddressListTab(ProTab):
    def __init__(self, ctl: Controller, tasks: TaskRunner) -> None:
        super().__init__(ctl, tasks)
        self.rows: list[ad.AddressRow] = []
        self.writer = pc_compile.ProWriter(ctl.session, warn=ctl.message.emit)
        self.show_memory: Callable[[int], None] = lambda addr: None  # set by the window
        self.ask_text: Callable[[str, str, str], str | None] = self._ask_text
        self.exec_dialog: Callable[[object], object] = lambda d: d.exec()
        self._filling = False

        self.hint = QLabel(hints.ADDRESSES)
        self.hint.setWordWrap(True)
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.add_btn = QPushButton("Add address...")
        self.remove_btn = QPushButton("Remove")
        self.refresh_btn = QPushButton("Read values")
        self.memory_btn = QPushButton("Show in Memory")
        self.pointer_btn = QPushButton("Pointer scan...")
        self.writes_btn = QPushButton(hints.FIND_WRITES)
        self.accesses_btn = QPushButton(hints.FIND_ACCESSES)
        for b in (self.writes_btn, self.accesses_btn):
            b.setEnabled(False)  # the Watch tab (architecture step 17)
            b.setToolTip(hints.WATCH_LATER)

        row = QHBoxLayout()
        for w in (
            self.add_btn,
            self.remove_btn,
            self.refresh_btn,
            self.memory_btn,
            self.pointer_btn,
            self.writes_btn,
            self.accesses_btn,
        ):
            row.addWidget(w)
        row.addStretch(1)
        lay = QVBoxLayout(self.body)
        lay.addWidget(self.hint)
        lay.addWidget(self.table, 1)
        lay.addLayout(row)

        self.add_btn.clicked.connect(self.do_add)
        self.remove_btn.clicked.connect(self.do_remove)
        self.refresh_btn.clicked.connect(self.refresh_values)
        self.memory_btn.clicked.connect(self._show_memory)
        self.pointer_btn.clicked.connect(self.do_pointer)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self._update_buttons()

    def _ask_text(self, title: str, prompt: str, default: str) -> str | None:
        text, ok = QInputDialog.getText(self, title, prompt, text=default)
        return text if ok else None

    # -- rows ------------------------------------------------------------------------------
    def add(self, address: int, vtype: ValueType, description: str = "") -> ad.AddressRow:
        """Add an address (from the scanner, or typed); one row per address."""
        for row in self.rows:
            if row.address == address:
                return row
        row = ad.AddressRow(address, vtype, description)
        self.rows.append(row)
        self.refresh_values()
        return row

    def selected_rows(self) -> list[ad.AddressRow]:
        picked = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self.rows[k] for k in picked if k < len(self.rows)]

    def fill(self) -> None:
        self._filling = True
        try:
            self.table.setRowCount(len(self.rows))
            game = self.game
            for k, row in enumerate(self.rows):
                self._set(k, COL_DESC, row.description)
                self._set(k, COL_ADDR, ad.place_text(row.address, game), editable=False)
                box = QComboBox()
                for t in TYPES:
                    box.addItem(t.name.lower(), t)
                box.setCurrentIndex(TYPES.index(row.vtype))
                box.currentIndexChanged.connect(lambda _i, r=row, b=box: self._retype(r, b))
                self.table.setCellWidget(k, COL_TYPE, box)
                value = "??" if row.value is None else row.vtype.text(row.value)
                self._set(k, COL_VALUE, value)
                freeze = QTableWidgetItem()
                freeze.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                on = row.frozen is not None
                freeze.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                self.table.setItem(k, COL_FREEZE, freeze)
                self._set(k, COL_GROUP, row.group)
                self._set(k, COL_COMMENT, row.comment)
                self._set_badge(k, row)
        finally:
            self._filling = False
        self._update_buttons()

    def _set(self, k: int, col: int, text: str, editable: bool = True) -> None:
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(k, col, item)

    def _set_badge(self, k: int, row: ad.AddressRow) -> None:
        if self.game is None:
            self._set(k, COL_BADGE, "", editable=False)
            return
        b = ad.row_badge(self.ctl.session, row)
        self._set(k, COL_BADGE, b.target, editable=False)
        self.table.item(k, COL_BADGE).setToolTip(b.tooltip)

    def _update_buttons(self) -> None:
        one = len(self.selected_rows()) == 1
        self.remove_btn.setEnabled(bool(self.selected_rows()))
        self.memory_btn.setEnabled(one)
        self.pointer_btn.setEnabled(one)
        self.refresh_btn.setEnabled(bool(self.rows))

    def game_state_changed(self, new_game: bool) -> None:
        if new_game:
            # the Controller already cleared every freeze for the new game
            for row in self.rows:
                row.frozen = None
            self.writer = pc_compile.ProWriter(self.ctl.session, warn=self.ctl.message.emit)
        self.fill()

    # -- reading and editing -------------------------------------------------------------
    def refresh_values(self) -> None:
        if not self.running or not self.rows:
            self.fill()
            return
        rows, session = list(self.rows), self.ctl.session

        def done(values) -> None:
            for row, value in zip(rows, values, strict=True):
                row.value = value
            self.fill()

        self.tasks.run(
            lambda: ad.read_values(session, rows), done, lambda e: fail(self.ctl, e, "pro_read")
        )

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item.row() >= len(self.rows):
            return
        row, col = self.rows[item.row()], item.column()
        try:
            if col == COL_DESC:
                row.description = item.text()
            elif col == COL_GROUP:
                row.group = item.text()
            elif col == COL_COMMENT:
                row.comment = item.text()
            elif col == COL_VALUE:
                self.set_value(row, parse_value(item.text(), row.vtype))
                return
            elif col == COL_FREEZE:
                self.set_freeze(row, item.checkState() == Qt.CheckState.Checked)
                return
        except Exception as exc:
            fail(self.ctl, exc, "pro_address")
        self.fill()

    def set_value(self, row: ad.AddressRow, value: float | int) -> None:
        """Set once; a frozen row keeps the new value (AC-2.3)."""
        try:
            ad.set_value(row, value, self.writer.write_checked)
            if row.frozen is not None:
                ad.freeze(self.ctl.session, self.ctl.freezer, row, value)
        except Exception as exc:
            fail(self.ctl, exc, "pro_set")
        self.fill()

    def set_freeze(self, row: ad.AddressRow, on: bool) -> None:
        try:
            if on:
                value = row.value
                if value is None:
                    raise ValueError("This address can't be read, so there is nothing to keep.")
                ad.freeze(self.ctl.session, self.ctl.freezer, row, value)
            else:
                ad.unfreeze(self.ctl.freezer, row)
        except Exception as exc:
            fail(self.ctl, exc, "pro_freeze")
        self.fill()

    def _retype(self, row: ad.AddressRow, box: QComboBox) -> None:
        if row.frozen is not None:
            ad.unfreeze(self.ctl.freezer, row)
        row.vtype = box.currentData()
        self.refresh_values()

    # -- actions --------------------------------------------------------------------------
    def do_add(self) -> None:
        text = self.ask_text(hints.TAB_ADDRESSES, hints.ADD_ADDRESS, "0x")
        if not text:
            return
        try:
            addr = ad.parse_place(text, self.ctl.session.require_game())
        except Exception as exc:
            fail(self.ctl, exc, "pro_address")
            return
        self.add(addr, ValueType.INT32)

    def do_remove(self) -> None:
        for row in self.selected_rows():
            if row.frozen is not None:
                ad.unfreeze(self.ctl.freezer, row)
            self.rows.remove(row)
        self.fill()

    def _show_memory(self) -> None:
        picked = self.selected_rows()
        if len(picked) == 1:
            self.show_memory(picked[0].address)

    def do_pointer(self) -> None:
        picked = self.selected_rows()
        if len(picked) != 1:
            return
        row = picked[0]

        def use(chain, _vtype) -> None:
            row.comment = f"pointer: {chain.label()}"
            self.ctl.message.emit(
                hints.POINTER_FOUND.format(
                    place=ad.place_text(row.address, self.game), chain=chain.label()
                )
            )
            self.fill()

        try:
            dlg = PointerDialog(self.ctl, row.address, row.vtype, use, self)
        except Exception as exc:
            fail(self.ctl, exc, "pro_pointer")
            return
        self.exec_dialog(dlg)

    def closing(self) -> None:
        for row in self.rows:
            if row.frozen is not None:
                ad.unfreeze(self.ctl.freezer, row)
