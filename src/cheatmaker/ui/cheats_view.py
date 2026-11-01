"""My cheats: toggle live, rename, edit value, delete, open file, Lock it, save (AC-5.3, AC-7)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cheatmaker import formats
from cheatmaker.core.cheats import Cheat, versions_differ
from cheatmaker.core.values import parse_number
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.find_wizard import fmt_value

COL_ON, COL_NAME, COL_VALUE, COL_KEEPS, COL_DETAILS = range(5)


def details(c: Cheat) -> str:
    return "; ".join(
        f"{loc.kind.value} +{loc.offset:X}: {loc.off.hex(' ').upper()} -> {loc.on.hex(' ').upper()}"
        for loc in c.locations
    )


class CheatsView(QWidget):
    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.open_lock: Callable[[Cheat], None] = lambda c: None
        self.open_save: Callable[[], None] = lambda: None
        self.pick_file: Callable[[], str] = self._pick_file
        self._filling = False

        self.hint = QLabel(hints.CHEATS_EMPTY)
        self.hint.setWordWrap(True)
        self.warning = QLabel("")
        self.warning.setStyleSheet("color: #b45309;")
        self.warning.setWordWrap(True)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["On", "Name", "Value", "Works after restart?", "Details"]
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.show_details = QCheckBox("Advanced: show details")
        self.delete_btn = QPushButton("Delete")
        self.lock_btn = QPushButton("Lock it")
        self.open_btn = QPushButton("Open cheat file...")
        self.save_btn = QPushButton("Save...")
        buttons = QHBoxLayout()
        for w in (self.delete_btn, self.lock_btn, self.open_btn, self.save_btn):
            buttons.addWidget(w)
        buttons.addStretch(1)
        buttons.addWidget(self.show_details)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addWidget(self.warning)
        lay.addWidget(self.table, 1)
        lay.addLayout(buttons)

        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.show_details.toggled.connect(lambda _on: self.fill())
        self.delete_btn.clicked.connect(self.do_delete)
        self.lock_btn.clicked.connect(lambda: self._with_selected(self.open_lock))
        self.open_btn.clicked.connect(self.do_open)
        self.save_btn.clicked.connect(lambda: self.open_save())
        ctl.cheats_changed.connect(self.fill)
        ctl.game_changed.connect(lambda _g: self._check_version())
        self.fill()

    @property
    def cheats(self) -> list[Cheat]:
        return self.ctl.cheat_file.cheats if self.ctl.cheat_file else []

    def selected(self) -> Cheat | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self.cheats):
            return None
        return self.cheats[rows[0].row()]

    def _with_selected(self, fn: Callable[[Cheat], None]) -> None:
        c = self.selected()
        if c is not None:
            fn(c)

    def fill(self) -> None:
        self._filling = True
        try:
            cheats = self.cheats
            self.table.setColumnHidden(COL_DETAILS, not self.show_details.isChecked())
            self.table.setRowCount(len(cheats))
            for i, c in enumerate(cheats):
                on = QTableWidgetItem()
                on.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                on.setCheckState(Qt.CheckState.Checked if c.enabled else Qt.CheckState.Unchecked)
                self.table.setItem(i, COL_ON, on)
                self.table.setItem(i, COL_NAME, QTableWidgetItem(c.name))
                val = QTableWidgetItem("" if c.value is None else fmt_value(c.value))
                if c.value is None:
                    val.setFlags(val.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i, COL_VALUE, val)
                keeps = QTableWidgetItem("This session only" if c.session_only else "Yes")
                keeps.setFlags(keeps.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i, COL_KEEPS, keeps)
                det = QTableWidgetItem(details(c))
                det.setFlags(det.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i, COL_DETAILS, det)
        finally:
            self._filling = False
        if not cheats:
            self.hint.setText(hints.CHEATS_EMPTY)
        elif any(c.session_only for c in cheats):
            self.hint.setText(hints.CHEATS_SESSION)
        else:
            self.hint.setText(hints.CHEATS)
        self._update_buttons()

    def _update_buttons(self) -> None:
        c = self.selected()
        self.delete_btn.setEnabled(c is not None)
        self.lock_btn.setEnabled(c is not None and c.session_only and self.ctl.session.connected)
        self.save_btn.setEnabled(any(ch.saveable for ch in self.cheats))

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item.row() >= len(self.cheats):
            return
        c = self.cheats[item.row()]
        try:
            if item.column() == COL_ON:
                want = item.checkState() == Qt.CheckState.Checked
                if want and not c.enabled:
                    self.ctl.live.turn_on(c)
                elif not want and c.enabled:
                    self.ctl.live.turn_off(c)
            elif item.column() == COL_NAME:
                name = item.text().strip()
                if not name:
                    raise ValueError("A cheat needs a name.")
                c.name = name
            elif item.column() == COL_VALUE:
                c.set_value(parse_number(item.text()))
                self.ctl.live.refresh(c)
        except Exception as exc:
            self.ctl.fail(exc, "toggle" if item.column() == COL_ON else "edit")
            c.enabled = c.enabled and self.ctl.session.connected
        self.fill()

    def do_delete(self) -> None:
        c = self.selected()
        if c is None:
            return
        if c.enabled:
            try:
                self.ctl.live.turn_off(c)
            except Exception as exc:
                self.ctl.fail(exc, "toggle")
        self.cheats.remove(c)
        self.ctl.cheats_changed.emit()

    def _pick_file(self) -> str:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open cheat file", "", "Cheat files (*.json *.shn)"
        )
        return path

    def do_open(self) -> None:
        path = self.pick_file()
        if not path:
            return
        try:
            cf = formats.load_path(path)
        except Exception as exc:
            self.ctl.fail(exc, "file_load")
            return
        self.ctl.live.mark_all_off()
        self.ctl.cheat_file = cf
        self.ctl.cheats_changed.emit()
        self._check_version()

    def _check_version(self) -> None:
        """AC-7.2: warn when the file was made for another version (or game)."""
        cf, g = self.ctl.cheat_file, self.ctl.session.game
        text = ""
        if cf and g:
            if cf.title_id.upper() != g.title_id:
                text = f"This cheat file is for {cf.title_id}; you're running {g.title_id}."
            elif versions_differ(cf.version, g.version):
                text = (
                    f"This cheat was made for version {cf.version}; you're running "
                    f"{g.version}. It may not work."
                )
        self.warning.setText(text)
        self.warning.setVisible(bool(text))
