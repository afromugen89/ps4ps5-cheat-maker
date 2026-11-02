"""My cheats: toggle live, rename, edit value, delete, open file, Lock it, save (AC-5.3, AC-7)."""

from __future__ import annotations

import logging
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
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import Cheat, versions_differ
from cheatmaker.core.values import parse_number
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.find_again import FindAgain, LearnDialog
from cheatmaker.ui.find_wizard import fmt_value

log = logging.getLogger(__name__)

COL_ON, COL_NAME, COL_VALUE, COL_KEEPS, COL_DETAILS = range(5)
FOUND_AGAIN = "Found again each run"


def keeps_text(c: Cheat) -> str:
    if c.anchor is not None:
        return FOUND_AGAIN
    return "This session only" if c.session_only else "Yes"


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
        self.finder = FindAgain(ctl)
        self.open_learn: Callable[[Cheat], None] = lambda c: LearnDialog(
            self.ctl, self.finder, c, self
        ).exec()
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
        self.learn_btn = QPushButton("Find it next time")
        self.learn_btn.setToolTip(
            "Learn how the game keeps this value, so Cheat Maker finds it again by itself "
            "after a restart or a new area"
        )
        self.again_btn = QPushButton("Find it again")
        self.again_btn.setToolTip("Look for your remembered values in the game now")
        self.open_btn = QPushButton("Open cheat file...")
        self.save_btn = QPushButton("Save...")
        self.update_btn = QPushButton("Update for this version")
        self.update_btn.setToolTip(
            "Find these cheats in the game version you're running and move them there"
        )
        self.update_btn.setVisible(False)
        self._learning = False
        self._learn_tried: set[tuple[str, str, int]] = set()
        buttons = QHBoxLayout()
        for w in (
            self.delete_btn,
            self.lock_btn,
            self.learn_btn,
            self.again_btn,
            self.open_btn,
            self.save_btn,
        ):
            buttons.addWidget(w)
        buttons.addStretch(1)
        buttons.addWidget(self.show_details)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        warn = QHBoxLayout()
        warn.addWidget(self.warning, 1)
        warn.addWidget(self.update_btn)
        lay.addLayout(warn)
        lay.addWidget(self.table, 1)
        lay.addLayout(buttons)

        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.show_details.toggled.connect(lambda _on: self.fill())
        self.delete_btn.clicked.connect(self.do_delete)
        self.lock_btn.clicked.connect(lambda: self._with_selected(self.open_lock))
        self.learn_btn.clicked.connect(lambda: self._with_selected(self.open_learn))
        self.again_btn.clicked.connect(lambda: self.finder.start())
        self.open_btn.clicked.connect(self.do_open)
        self.save_btn.clicked.connect(lambda: self.open_save())
        self.update_btn.clicked.connect(self.do_update)
        ctl.cheats_changed.connect(self.fill)
        ctl.cheats_changed.connect(self._maybe_learn)
        ctl.game_changed.connect(lambda _g: self._check_version())
        ctl.connection_changed.connect(lambda _up: self._check_version())
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
                keeps = QTableWidgetItem(keeps_text(c))
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
        up = self.ctl.session.connected and self.ctl.session.game is not None
        located = c is not None and (c.anchor is None or c.anchor[0] in self.ctl.live.trackers)
        self.lock_btn.setEnabled(located and c.session_only and self.ctl.session.connected)
        self.learn_btn.setEnabled(up and located and c.session_only and c.value_type is not None)
        p = self.ctl.project
        self.again_btn.setEnabled(up and p is not None and bool(p.objects))
        self.save_btn.setEnabled(any(ch.saveable for ch in self.cheats))

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item.row() >= len(self.cheats):
            return
        c = self.cheats[item.row()]
        try:
            if item.column() == COL_ON:
                want = item.checkState() == Qt.CheckState.Checked
                if want and not c.enabled:
                    if self.finder.confirm(c):  # AC-12.2: ask once per run
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
                self.finder.cheat_changed(c)
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
        self.finder.forget(c)
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
        can_update = False
        if cf and g:
            if cf.title_id.upper() != g.title_id:
                text = f"This cheat file is for {cf.title_id}; you're running {g.title_id}."
            elif versions_differ(cf.version, g.version):
                text = (
                    f"This cheat was made for version {cf.version}; you're running "
                    f"{g.version}. It may not work."
                )
                can_update = self.ctl.session.connected and any(c.saveable for c in cf.cheats)
                if can_update:
                    text += " Press Update for this version to move the cheats."
        self.warning.setText(text)
        self.warning.setVisible(bool(text))
        self.update_btn.setVisible(can_update)
        self._maybe_learn()

    # -- v2: follow game updates ------------------------------------------------
    def _maybe_learn(self) -> None:
        """While the file's own version runs, learn a signature for each code line so
        the cheats can be moved after the next game update. Reads only."""
        cf, g = self.ctl.cheat_file, self.ctl.session.game
        if self._learning or not (cf and g and self.ctl.session.connected):
            return
        if cf.title_id.upper() != g.title_id or versions_differ(cf.version, g.version):
            return
        path = up.signatures_path(g.title_id, g.version)
        try:
            known = up.load_signatures(path)
        except (OSError, ValueError) as exc:
            log.warning("action=learn_signature reason=%s", exc)
            known = {}
        todo = [
            loc.offset
            for loc in up.patch_lines(cf)
            if loc.offset not in known
            and (g.title_id, g.version, loc.offset) not in self._learn_tried
        ]
        if not todo:
            return
        for off in todo:
            self._learn_tried.add((g.title_id, g.version, off))
        self._learning = True
        session, title_id, version = self.ctl.session, g.title_id, g.version

        def work() -> int:
            sigs = up.learn(cf, up.read_code_image(session))
            up.save_signatures(path, title_id, version, sigs)
            return len(sigs)

        def done(n: int) -> None:
            self._learning = False
            log.info("action=learn_signatures saved=%d version=%s", n, version)

        def failed(exc: BaseException) -> None:
            self._learning = False
            log.warning("action=learn_signatures reason=%s", exc)

        self.ctl.tasks.run(work, done, failed)

    def do_update(self) -> None:
        cf, g = self.ctl.cheat_file, self.ctl.session.game
        if not (cf and g):
            return
        session = self.ctl.session
        old_path = up.signatures_path(cf.title_id, cf.version)
        self.update_btn.setEnabled(False)
        self.hint.setText(hints.UPDATE_RUNNING)

        def work():
            image = up.read_code_image(session)
            new, report = up.update_file(cf, up.load_signatures(old_path), image, g.version)
            up.save_signatures(
                up.signatures_path(g.title_id, g.version),
                g.title_id,
                g.version,
                up.learn(new, image),
            )
            return new, report

        self.ctl.tasks.run(work, self._updated, self._update_failed)

    def _updated(self, result) -> None:
        new, report = result
        self.update_btn.setEnabled(True)
        self.ctl.live.mark_all_off()
        self.ctl.cheat_file = new
        self.ctl.cheats_changed.emit()
        self._check_version()
        summary = report.summary(new.version)
        self.warning.setText(summary)
        self.warning.setVisible(True)
        self.ctl.message.emit(summary)
        if report.moved:
            self.hint.setText(hints.UPDATE_DONE)

    def _update_failed(self, exc: BaseException) -> None:
        self.update_btn.setEnabled(True)
        self.fill()
        self.ctl.fail(exc, "update_version")
