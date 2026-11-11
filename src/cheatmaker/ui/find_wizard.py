"""Find a value (AC-3), set / freeze it (AC-4), make a cheat from it (AC-5)."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cheatmaker.core import cave as cv
from cheatmaker.core import pointers as ptr
from cheatmaker.core.grouping import Group, promote
from cheatmaker.core.guide import PUT_BACK_S, Action, Guide, Scan, Shows, Suggestion
from cheatmaker.core.hit_tester import TEST_LIMIT
from cheatmaker.core.pointers import Chain
from cheatmaker.core.real_check import (
    RealChecker,
    Result,
    Verdict,
    targets_from_groups,
    targets_from_list,
)
from cheatmaker.core.scan_log import way_label
from cheatmaker.core.scanner import ABOUT, FEW_MATCHES, MAX_SHOW, NOTED, TOO_MANY, Match
from cheatmaker.core.templates import TEMPLATES, make_cheat
from cheatmaker.core.trace_way import Way
from cheatmaker.core.tracer import TraceResult
from cheatmaker.core.values import (
    OF_FULL,
    SHOWN,
    Compare,
    Scale,
    ValueType,
    in_filter,
    make_scale,
    parse_amount,
    parse_filter,
    parse_number,
    parse_percent,
)
from cheatmaker.errors import ReadFailed
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.hit_test_dialog import HitTestDialog
from cheatmaker.ui.pointer_dialog import PointerDialog
from cheatmaker.ui.tasks import _Signals
from cheatmaker.ui.trace_dialog import result_text as trace_result_text

log = logging.getLogger(__name__)

LIVE_MS = 500
SCALES = [  # (label, make_scale kind) for "Game stores it as" (AC-H3)
    ("the number shown", "shown"),
    ("a fraction (0 to 1) of max", "fraction"),
    ("x10", "x10"),
    ("x100", "x100"),
    ("x1000", "x1000"),
]


def eta_text(fraction: float, elapsed: float) -> str:
    """Progress bar text with time left (big PS5 games take ~100 s, C3)."""
    if fraction <= 0.02 or fraction >= 1 or elapsed < 2:
        return "%p%"
    left = elapsed * (1 - fraction) / fraction
    if left < 60:
        return f"%p% · about {max(5, round(left / 5) * 5)} s left"
    return f"%p% · about {round(left / 60, 1):g} min left"


def fmt_value(v: float | int) -> str:
    return f"{v:,.2f}" if isinstance(v, float) else f"{v:,}"


ROW = Qt.ItemDataRole.UserRole  # tree items keep their index into FindWizard.rows
TRACED_SCALE = "traced"  # "Game stores it as" item that Search for it that way adds
PUT_BACK_MS = int(PUT_BACK_S * 1000)  # a Set the game undid this soon: it's a copy


@dataclass(frozen=True)
class TracedRow:
    """A row Find the real one behind it added (spec-trace AC-T7): typed numbers are
    in screen units and go through ``way`` before they are written."""

    way: Way
    behind: int  # the copy it was found behind
    check: str  # "checked: real", "checked: kept the test value", "not sure"
    sure: bool

    def notes(self) -> str:
        return f"found behind {self.behind:#x}, {self.check}"


class FindWizard(QWidget):
    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.unknown_mode = False
        self.rows: list[Match] = []
        self.row_types: list[ValueType] = []
        self.groups: list[Group] = []  # shown as the tree when they fold copies together
        self._n_matches = 0  # rows[:_n_matches] are matches, the rest typed in / pointers
        # (address, is an expanded member) selected before the tree was built again
        self._tree_pick: tuple[int, ValueType, bool] | None = None
        self.manual: dict[int, ValueType] = {}  # addresses the player typed in
        self.traced: dict[int, TracedRow] = {}  # manual rows found by a trace
        self._trace_max: dict[int, dict[int, float | int] | None] = {}  # per refresh
        self._traced_scale: Scale | None = None  # Search for it that way's factor
        # Check which is real: what each checked match did with its test value
        self.verdicts: dict[tuple[int, ValueType], Result] = {}
        self.open_build: Callable[..., None] = lambda **kw: None  # MainWindow.open_build
        self.open_trace: Callable[..., None] = lambda **kw: None  # MainWindow.open_trace
        self._search_gen = 0  # bumped when the search ends: a running check's results drop
        self._checking = False
        self.pointers: dict[Chain, ValueType] = {}  # chains followed on every refresh
        self.row_chains: list[Chain | None] = []
        self._cancel: threading.Event | None = None
        self._progress = _Signals()
        self._progress.done.connect(self._on_progress)
        self._started = 0.0

        self.hint = QLabel(hints.FIND_START)
        self.hint.setWordWrap(True)
        self.guide = Guide(ctl.scanner.show_limit)  # AC-H5
        self._guide_set_advanced = False  # "Try it" changed Advanced; Start over undoes it
        self._scan_kind: Scan | None = None
        self.suggestion: Suggestion | None = None
        self.shows_btns = {
            Shows.NUMBER: QPushButton("A number"),
            Shows.BAR: QPushButton("A bar or meter"),
            Shows.NOTHING: QPushButton("Nothing"),
        }
        self.try_btn = QPushButton("Try it")
        self.try_btn.setVisible(False)
        self.test_btn = QPushButton("Test the hits")
        self.test_btn.setToolTip(
            f"With 2 to {TEST_LIMIT} matches: write a test value to half of them and say "
            "whether the game showed it, until one is left"
        )
        self.check_btn = QPushButton("Check which is real")
        self.check_btn.setToolTip(hints.CHECK_REAL_TIP)
        # spec-trace: under Advanced, "(being tested)", until console test C20a passes
        self.trace_btn = QPushButton(
            hints.TRACE_BUTTON if hints.TRACE_TESTED else hints.TRACE_BUTTON_TESTING
        )
        self.number = QLineEdit()
        self.number.setPlaceholderText("The number you see, e.g. 2450 or 40%")
        self.number.setToolTip(
            "Type a number, or a percent like 40% when the game shows one (health 40%)"
        )
        self.search_btn = QPushButton("Search")
        self.unknown_btn = QPushButton("I don't know the number")
        self.full_btn = QPushButton("It's full now")
        self.full_btn.setToolTip(
            "For a bar or a value that isn't out of 100: fill it up, then press this. "
            "Later, type the % it shows or pick About 75 / 50 / 25%."
        )
        self.about_btns = {pct: QPushButton(f"About {pct}%") for pct in ABOUT}
        for pct, b in self.about_btns.items():
            b.setToolTip(f"The bar looks about {pct}% full now")
            b.clicked.connect(lambda _c=False, p=pct: self.do_about(p))
        self.new_btn = QPushButton("Start over")
        self.files_btn = QPushButton("Scan files")
        self.files_btn.setToolTip(
            "Open the folder where every scan saves its matches (address and value)"
        )
        self.count = QLabel("")
        self.show_n = QSpinBox()
        self.show_n.setRange(1, MAX_SHOW)
        self.show_n.setValue(max(1, min(MAX_SHOW, ctl.settings.show_matches)))
        self.show_n.setToolTip("List the matches with their live values once this few are left")
        ctl.scanner.show_limit = self.show_n.value()
        self.up_btn = QPushButton("Went up")
        self.down_btn = QPushButton("Went down")
        self.changed_btn = QPushButton("Changed")
        self.same_btn = QPushButton("Same")
        self.by_amount = QLineEdit()
        self.by_amount.setPlaceholderText("How much, e.g. 15")
        self.by_amount.setToolTip("If you know how much the value changed (took 15 damage)")
        self.up_by_btn = QPushButton("Went up by")
        self.down_by_btn = QPushButton("Went down by")
        self.still_btn = QPushButton("Stand still")
        self.still_btn.setToolTip(
            "Runs Same 5 times, 2 seconds apart, to drop values that change on their own"
        )
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.cancel_btn = QPushButton("Cancel")

        self.advanced = QGroupBox("Advanced")
        self.advanced.setCheckable(True)
        self.advanced.setChecked(False)
        self.vtype = QComboBox()
        self.vtype.addItem("Pick for me", None)
        for vt in ValueType:
            self.vtype.addItem(vt.label, vt)
        adv_box = QVBoxLayout(self.advanced)
        adv = QHBoxLayout()
        adv_box.addLayout(adv)
        adv.addWidget(QLabel("Number type:"))
        adv.addWidget(self.vtype)
        self.range_lo = QLineEdit()
        self.range_lo.setPlaceholderText("e.g. 1")
        self.range_hi = QLineEdit()
        self.range_hi.setPlaceholderText("e.g. 1000")
        adv.addWidget(QLabel("Don't know the number, but it's between"))
        adv.addWidget(self.range_lo)
        adv.addWidget(QLabel("and"))
        adv.addWidget(self.range_hi)
        self.scale = QComboBox()
        for label, kind in SCALES:
            self.scale.addItem(label, kind)
        self.scale_max = QLineEdit("100")
        self.scale_max.setToolTip("The most the value can be, e.g. 100 for a health bar")
        self.scale_max.setEnabled(False)
        self.scale.currentIndexChanged.connect(
            lambda _i: self.scale_max.setEnabled(self.scale.currentData() == "fraction")
        )
        scale_row = QHBoxLayout()
        adv_box.addLayout(scale_row)
        scale_row.addWidget(QLabel("Game stores it as:"))
        scale_row.addWidget(self.scale)
        scale_row.addWidget(QLabel("max"))
        scale_row.addWidget(self.scale_max)
        scale_row.addStretch(1)
        if not hints.TRACE_TESTED:
            trace_row = QHBoxLayout()
            adv_box.addLayout(trace_row)
            trace_row.addWidget(self.trace_btn)
            trace_row.addStretch(1)
        self.advanced.toggled.connect(lambda _on: self._fill_table())
        self.advanced.toggled.connect(lambda _on: self._update_buttons())

        self.add_addr = QLineEdit()
        self.add_addr.setPlaceholderText("Address, e.g. 0x12e0ea60ac")
        self.add_type = QComboBox()
        for vt in ValueType:
            self.add_type.addItem(vt.label, vt)
        self.add_type.setCurrentIndex(list(ValueType).index(ValueType.FLOAT))
        self.add_btn = QPushButton("Add address")
        self.remove_btn = QPushButton("Remove")
        self.pointer_btn = QPushButton("Pointer scan")
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Only list values, e.g. 100 or 90-110")
        self.filter.setToolTip("Hides listed rows whose value doesn't match. No new search.")
        self.filter.setClearButtonEnabled(True)
        self.filter_count = QLabel("")

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Value now", "Address"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Groups of linked copies (AC-G4): one row per group, expand to see every address.
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Value now", "Stored as", "Notes", "Address"])
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.setVisible(False)
        self.tree.setToolTip(hints.FIND_GROUPS)

        self.set_value = QLineEdit()
        self.set_value.setPlaceholderText("New value")
        self.set_btn = QPushButton("Set")
        self.freeze_btn = QPushButton("Freeze")
        self.template = QComboBox()
        for t in TEMPLATES:
            self.template.addItem(t.name, t)
        self.make_btn = QPushButton("Make a cheat")
        self.build_btn = QPushButton(hints.BUILD_BUTTON)
        self.build_btn.setToolTip(hints.BUILD_BUTTON_TIP)

        top = QHBoxLayout()
        for w in (
            self.number,
            self.search_btn,
            self.unknown_btn,
            self.full_btn,
            self.new_btn,
            self.files_btn,
        ):
            top.addWidget(w)
        cmp_row = QHBoxLayout()
        cmp_row.addWidget(self.count, 1)
        cmp_row.addWidget(QLabel("Show up to"))
        cmp_row.addWidget(self.show_n)
        for w in (self.up_btn, self.down_btn, self.changed_btn, self.same_btn):
            cmp_row.addWidget(w)
        by_row = QHBoxLayout()
        by_row.addWidget(QLabel("Know how much it changed?"))
        for w in (self.by_amount, self.up_by_btn, self.down_by_btn):
            by_row.addWidget(w)
        by_row.addStretch(1)
        for b in self.about_btns.values():
            by_row.addWidget(b)
        by_row.addWidget(self.still_btn)
        prog = QHBoxLayout()
        prog.addWidget(self.progress, 1)
        prog.addWidget(self.cancel_btn)
        add_row = QHBoxLayout()
        add_row.addWidget(self.add_addr, 1)
        for w in (self.add_type, self.add_btn, self.remove_btn, self.pointer_btn):
            add_row.addWidget(w)
        act = QHBoxLayout()
        act.addWidget(self.check_btn)
        act.addWidget(self.test_btn)
        for w in (
            self.set_value,
            self.set_btn,
            self.freeze_btn,
            self.template,
            self.make_btn,
            self.build_btn,
        ):
            act.addWidget(w)
        if hints.TRACE_TESTED:
            act.addWidget(self.trace_btn)
        shows_row = QHBoxLayout()
        shows_row.addWidget(QLabel("What does the game show?"))
        for shows, b in self.shows_btns.items():
            shows_row.addWidget(b)
            b.clicked.connect(lambda _c=False, s=shows: self.do_shows(s))
        shows_row.addStretch(1)
        shows_row.addWidget(self.try_btn)
        lay = QVBoxLayout(self)
        lay.addLayout(shows_row)
        lay.addWidget(self.hint)
        lay.addLayout(top)
        lay.addLayout(cmp_row)
        lay.addLayout(by_row)
        lay.addLayout(prog)
        lay.addLayout(add_row)
        filt = QHBoxLayout()
        filt.addWidget(QLabel("Filter list by value:"))
        filt.addWidget(self.filter, 1)
        filt.addWidget(self.filter_count)
        lay.addLayout(filt)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.tree, 1)
        lay.addLayout(act)
        lay.addWidget(self.advanced)

        self.search_btn.clicked.connect(self.do_search)
        self.number.returnPressed.connect(self.do_search)
        self.unknown_btn.clicked.connect(self.do_unknown)
        self.full_btn.clicked.connect(self.do_full)
        self.new_btn.clicked.connect(self.do_reset)
        self.files_btn.clicked.connect(self.open_scan_files)
        self.up_btn.clicked.connect(lambda: self.do_compare(Compare.INCREASED))
        self.down_btn.clicked.connect(lambda: self.do_compare(Compare.DECREASED))
        self.changed_btn.clicked.connect(lambda: self.do_compare(Compare.CHANGED))
        self.same_btn.clicked.connect(lambda: self.do_compare(Compare.UNCHANGED))
        self.up_by_btn.clicked.connect(lambda: self.do_compare_by(Compare.INCREASED_BY))
        self.down_by_btn.clicked.connect(lambda: self.do_compare_by(Compare.DECREASED_BY))
        self.still_btn.clicked.connect(self.do_stand_still)
        self.try_btn.clicked.connect(self.do_try_next)
        self.test_btn.clicked.connect(self.do_test_hits)
        self.check_btn.clicked.connect(self.do_check_real)
        self.cancel_btn.clicked.connect(self.do_cancel)
        self.set_btn.clicked.connect(self.do_set)
        self.freeze_btn.clicked.connect(self.do_freeze)
        self.make_btn.clicked.connect(self.do_make)
        self.build_btn.clicked.connect(self.do_build)
        self.trace_btn.clicked.connect(self.do_trace)
        self.add_btn.clicked.connect(self.do_add_address)
        self.add_addr.returnPressed.connect(self.do_add_address)
        self.remove_btn.clicked.connect(self.do_remove_address)
        self.pointer_btn.clicked.connect(self.do_pointer_scan)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.show_n.valueChanged.connect(self._set_show_limit)
        self.filter.textChanged.connect(lambda _t: self._fill_table())

        self.timer = QTimer(self)
        self.timer.setInterval(LIVE_MS)
        self.timer.timeout.connect(self.refresh_live)
        ctl.game_changed.connect(self._new_game)
        ctl.search_lost.connect(self._search_lost)
        self._game_key: tuple[str, str] | None = None
        ctl.connection_changed.connect(lambda _up: self._update_buttons())
        ctl.tasks.busy_changed.connect(lambda _busy: self._update_buttons())
        self._update_buttons()

    # -- state ----------------------------------------------------------------
    def _ready(self) -> bool:
        return self.ctl.session.connected and self.ctl.session.game is not None

    def _update_buttons(self) -> None:
        busy = self._cancel is not None
        ready = self._ready() and not busy
        active = self.ctl.scanner.active
        self.search_btn.setEnabled(ready)
        self.search_btn.setText("Search again" if active else "Search")
        self.unknown_btn.setEnabled(ready and not active)
        self.full_btn.setEnabled(ready)
        full = active and self.ctl.scanner.full_mode
        for b in self.about_btns.values():
            b.setVisible(full)
            b.setEnabled(ready and full)
        self.new_btn.setEnabled(ready and active)
        self.files_btn.setVisible(self.ctl.scanner.scan_log.enabled)
        for b in (
            self.up_btn,
            self.down_btn,
            self.changed_btn,
            self.same_btn,
            self.up_by_btn,
            self.down_by_btn,
            self.still_btn,
        ):
            b.setEnabled(ready and active)
        self.cancel_btn.setEnabled(busy)
        count = self.ctl.scanner.count or 0
        self.test_btn.setEnabled(ready and active and 2 <= count <= TEST_LIMIT)
        self.check_btn.setEnabled(ready and active and self._n_matches > 0)
        self.try_btn.setEnabled(ready)
        for b in self.shows_btns.values():
            b.setEnabled(ready)
        sel = ready and self.selected() is not None and self.selected().address != 0
        for w in (self.set_btn, self.freeze_btn, self.make_btn, self.build_btn):
            w.setEnabled(sel)
        # one debugger job at a time: not while Lock it / Make it work / a check runs
        can_trace = sel and not self.ctl.tasks.busy
        if not hints.TRACE_TESTED:  # in the Advanced box: only with Advanced on
            can_trace = can_trace and self.advanced.isChecked()
        self.trace_btn.setEnabled(can_trace)
        self.trace_btn.setToolTip(hints.TRACE_BUTTON_TIP if can_trace else hints.TRACE_PICK_ONE)
        self.add_btn.setEnabled(ready)
        m = self.selected()
        i = self._selected_row()
        is_ptr = i is not None and i < len(self.row_chains) and self.row_chains[i] is not None
        self.remove_btn.setEnabled(ready and m is not None and (m.address in self.manual or is_ptr))
        self.pointer_btn.setEnabled(ready)
        self.freeze_btn.setText(
            "Unfreeze" if m and m.address in self.ctl.frozen_addresses else "Freeze"
        )

    @property
    def grouped(self) -> bool:
        """The list shows groups (the tree) instead of one row per match."""
        return bool(self.groups)

    def _selected_row(self) -> int | None:
        if self.grouped:
            items = self.tree.selectedItems()
            i = items[0].data(0, ROW) if items else None
            return i if isinstance(i, int) and 0 <= i < len(self.rows) else None
        rows = self.table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self.rows):
            return None
        return rows[0].row()

    def selected(self) -> Match | None:
        i = self._selected_row()
        return None if i is None else self.rows[i]

    def _on_progress(self, fraction: float) -> None:
        self.progress.setValue(int(fraction * 100))
        self.progress.setFormat(eta_text(fraction, time.monotonic() - self._started))

    def _after_scan(self, count: int) -> None:
        self._cancel = None
        self.progress.setValue(100)
        self.progress.setFormat("%p%")
        sc = self.ctl.scanner
        text = f"{count:,} matches"
        if sc.mixed:  # e.g. "1,200 matches (900 decimal, 300 whole number)"
            parts = ", ".join(
                f"{n:,} {'decimal' if t.is_float else 'whole number'} ({t.size} bytes)"
                for t, n in sc.types.items()
            )
            text += f" ({parts})"
        self.count.setText(text)
        last = sc.scan_log.last_file
        self.count.setToolTip(f"Saved to {last}" if last is not None else "")
        kind, self._scan_kind = self._scan_kind, None
        if self.guide.shows is None:  # the player skipped the question: guess it
            self.guide.shows = Shows.BAR if self.unknown_mode else Shows.NUMBER
        tip = self.guide.after_scan(kind, count) if kind is not None else None
        if sc.note in (TOO_MANY, NOTED):  # a percent scan ran as Went up / down, or noted
            self._suggest(tip, show_text=False)
            self.hint.setText(
                hints.FIND_PERCENT_TOO_MANY if sc.note == TOO_MANY else hints.FIND_PERCENT_NOTED
            )
        elif sc.few_left and count > FEW_MATCHES and sc.changes_to_group:
            # Listed, but the groups can't be told apart yet: say what brings them,
            # rather than "Found it" over a long flat list of copies. A handful of
            # matches is "Found it" as before: Test the hits sorts those out quickly.
            n = sc.changes_to_group
            self.hint.setText(hints.FIND_FEW_GROUPING.format(n=n, s="" if n == 1 else "s"))
            self._suggest(tip, show_text=False)
        elif sc.few_left and count > 1:
            self.hint.setText(f"{hints.FIND_FEW} {hints.FIND_TEST_HITS}")
            self._suggest(tip, show_text=False)
        elif tip is not None:
            self._suggest(tip)
        elif count == 0:
            self.hint.setText(hints.FIND_NONE)
        elif sc.few_left:
            self.hint.setText(hints.FIND_FEW)
        else:
            self.hint.setText(hints.FIND_UNKNOWN if self.unknown_mode else hints.FIND_NARROW)
        self.refresh_live()
        self._update_buttons()

    # -- guide (AC-H5) ----------------------------------------------------------
    def _action_widget(self, action: Action):
        return {
            Action.SEARCH: self.search_btn,
            Action.UNKNOWN: self.unknown_btn,
            Action.TYPE_NEW: self.search_btn,
            Action.MOVE: self.down_btn,
            Action.STAND_STILL: self.still_btn,
            Action.TEST_HITS: self.test_btn,
            Action.MAKE_CHEAT: self.make_btn,
            Action.TRY_NEXT: self.try_btn,
            Action.START_OVER: self.try_btn,
            Action.FULL: self.full_btn,
            Action.PERCENT: self.search_btn,
            Action.TRACE: self.trace_btn,
        }[action]

    def _suggest(self, tip: Suggestion | None, show_text: bool = True) -> None:
        """Show the guide's next step and make its button stand out."""
        self.suggestion = tip
        for w in {self._action_widget(a) for a in Action}:
            w.setStyleSheet("")
        has_try = tip is not None and tip.fallback is not None
        self.try_btn.setVisible(has_try)
        if has_try:
            self.try_btn.setText(
                "Start over and try it" if tip.action is Action.START_OVER else "Try it"
            )
        if tip is None:
            return
        if show_text:
            self.hint.setText(tip.text)
        if tip.action is Action.TRACE and not hints.TRACE_TESTED and not self.advanced.isChecked():
            self.advanced.setChecked(True)  # the button is in there
            self._guide_set_advanced = True  # Start over takes Advanced back off
        if tip.action is not Action.START_OVER or has_try:
            self._action_widget(tip.action).setStyleSheet("font-weight: bold")

    def do_shows(self, shows: Shows) -> None:
        tip = self.guide.start(shows)
        self._suggest(tip)
        self._action_widget(tip.action).setFocus()
        if shows is Shows.NUMBER:
            self.number.setFocus()

    def do_try_next(self) -> None:
        """Run the guide's next way of storing the value (fraction, x10, ..., unknown)."""
        tip = self.suggestion
        if tip is None or tip.fallback is None:
            return
        key = tip.fallback
        if self.ctl.scanner.active:
            self.ctl.scanner.reset()
        self.advanced.setChecked(key != "shown")
        self._guide_set_advanced = key != "shown"
        self.vtype.setCurrentIndex(0)  # Pick for me
        self.range_lo.clear()
        self.range_hi.clear()
        if key == "unknown":
            self.guide.note_tried(key)
            self.do_unknown()
            return
        self.scale.setCurrentIndex(next(i for i, (_l, k) in enumerate(SCALES) if k == key))
        self.number.setText(self.number.text().strip().rstrip("%").strip())  # not a % search
        if not self.number.text().strip():
            self.hint.setText("Type the number you see, then press Try it.")
            self._update_buttons()
            return
        self.do_search()

    def _scan_failed(self, exc: BaseException) -> None:
        self._cancel = None
        self.progress.setValue(0)
        self._update_buttons()
        self.ctl.fail(exc, "scan")

    def _run_scan(self, fn) -> None:
        self._cancel = threading.Event()
        cancel = self._cancel
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self._started = time.monotonic()
        self._update_buttons()
        self.ctl.tasks.run(
            lambda: fn(self._progress.done.emit, cancel), self._after_scan, self._scan_failed
        )

    # -- actions --------------------------------------------------------------
    def do_search(self) -> None:
        try:
            pct = parse_percent(self.number.text())
            if pct is None:
                value, unit = parse_amount(self.number.text())
                scale = self._scale()
        except ValueError as exc:
            self.ctl.fail(exc, "scan")
            return
        sc = self.ctl.scanner
        if pct is not None:  # AC-P1 / AC-P2: "40%"
            if not sc.active:
                self.unknown_mode = False
            vt = self.vtype.currentData() if self.advanced.isChecked() else None
            self._scan_kind = Scan.PERCENT
            self._run_scan(lambda p, c: sc.percent_scan(pct[0], pct[1], vt, p, c))
            return
        if sc.active:
            self._scan_kind = Scan.NEXT_EXACT
            self._run_scan(lambda p, c: sc.next_scan(Compare.EXACT, value, p, c, unit=unit))
        else:
            self.unknown_mode = False
            self._scan_kind = Scan.FIRST_EXACT
            self.guide.note_tried(
                self.scale.currentData() if self.advanced.isChecked() else "shown"
            )
            vt = self.vtype.currentData() if self.advanced.isChecked() else None
            self._run_scan(lambda p, c: sc.first_scan(value, vt, p, c, unit=unit, scale=scale))

    def scan_files_folder(self) -> Path:
        """This search's folder once it has a file, else the folder of all scan files."""
        log_ = self.ctl.scanner.scan_log
        return log_.folder if log_.folder is not None else log_.root()

    def open_scan_files(self) -> None:
        folder = self.scan_files_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.ctl.fail(exc, "scan files")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _scale(self) -> Scale:
        """Advanced > "Game stores it as" (AC-H3). Raises ValueError."""
        if not self.advanced.isChecked():
            return SHOWN
        kind = self.scale.currentData()
        if kind == TRACED_SCALE and self._traced_scale is not None:
            return self._traced_scale
        if kind != "fraction":
            return make_scale(kind)
        return make_scale(kind, parse_number(self.scale_max.text() or "100"))

    def _range(self) -> tuple[float, float] | None:
        """The optional "between low and high" from Advanced. Raises ValueError."""
        if not self.advanced.isChecked():
            return None
        lo, hi = self.range_lo.text().strip(), self.range_hi.text().strip()
        if not lo and not hi:
            return None
        if not (lo and hi):
            raise ValueError("Fill in both numbers of the range, or leave both empty.")
        low, high = parse_number(lo), parse_number(hi)
        if low > high:
            raise ValueError("The first number of the range must be the smaller one.")
        return (low, high)

    def do_unknown(self) -> None:
        try:
            rng = self._range()
            scale = self._scale()
        except ValueError as exc:
            self.ctl.fail(exc, "scan")
            return
        self.unknown_mode = True
        self._scan_kind = Scan.FIRST_UNKNOWN
        self.guide.note_tried("unknown")
        vt = self.vtype.currentData() if self.advanced.isChecked() else None
        if vt is None and scale.decimal:
            vt = ValueType.FLOAT
        self._run_scan(
            lambda p, c: self.ctl.scanner.first_scan(None, vt, p, c, value_range=rng, scale=scale)
        )

    def do_full(self) -> None:
        """AC-P2 "It's full now": remember this moment as the Full point."""
        sc = self.ctl.scanner
        if sc.active and not self._confirm("Full again?", hints.FIND_FULL_CONFIRM):
            return
        if not sc.active:
            self.unknown_mode = True
            self.guide.note_tried("unknown")
        vt = self.vtype.currentData() if self.advanced.isChecked() else None
        self._scan_kind = Scan.FULL
        self._run_scan(lambda p, c: sc.mark_full(vt, p, c))

    def do_about(self, pct: int) -> None:
        """AC-P3: only a bar, and it looks about ``pct`` percent full."""
        self._scan_kind = Scan.PERCENT
        self._run_scan(lambda p, c: self.ctl.scanner.about_scan(pct, p, c))

    def _confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def do_compare(self, cmp: Compare) -> None:
        self._scan_kind = Scan.COMPARE
        self._run_scan(lambda p, c: self.ctl.scanner.next_scan(cmp, None, p, c))

    def do_compare_by(self, cmp: Compare) -> None:
        """Went up by / Went down by the amount typed (AC-H1)."""
        try:
            amount, unit = parse_amount(self.by_amount.text())
            if amount <= 0:
                raise ValueError("Type how much it changed by, as a number more than 0.")
        except ValueError as exc:
            self.ctl.fail(exc, "scan")
            return
        sc = self.ctl.scanner
        self._scan_kind = Scan.COMPARE
        self._run_scan(lambda p, c: sc.next_scan(cmp, amount, p, c, unit=unit))

    def do_stand_still(self) -> None:
        """AC-H4: Same, a few times, while the player leaves the controller alone."""
        self.hint.setText(hints.FIND_STILL)
        self._scan_kind = Scan.STAND_STILL
        self._run_scan(lambda p, c: self.ctl.scanner.stand_still(progress=p, cancel=c))

    def do_cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            if self._checking:
                self.hint.setText(hints.CHECK_REAL_STOPPING)

    def _end_search(self) -> None:
        """The matches are gone: a running check stops after its round (with the values
        put back) and its results are thrown away."""
        self._search_gen += 1
        if self._checking and self._cancel is not None:
            self._cancel.set()
        self.verdicts.clear()

    def do_reset(self) -> None:
        try:
            self.ctl.scanner.reset()
        except Exception as exc:
            self.ctl.fail(exc, "scan")
        self.unknown_mode = False
        self.guide.new_value()
        self._suggest(None)
        if self._guide_set_advanced:  # back to the plain search the player expects
            self.scale.setCurrentIndex(0)
            self.advanced.setChecked(False)
            self._guide_set_advanced = False
        self.rows = []
        self._n_matches = 0
        self._end_search()
        self._show_groups([])
        self.count.setText("")
        self.number.clear()
        self.table.setRowCount(0)
        self.timer.stop()
        self.hint.setText(hints.FIND_START if self._ready() else hints.NO_GAME)
        self._update_buttons()

    def refresh_live(self) -> None:
        """AC-3.4: with 10 or fewer matches, show them with live values. Typed-in
        addresses are listed under the matches."""
        sc = self.ctl.scanner
        groups = self._groups_to_show() if self._ready() else []
        show_matches = (sc.few_left or bool(groups)) and self._ready()
        if not (show_matches or ((self.manual or self.pointers) and self._ready())):
            self.timer.stop()
            self.rows, self.row_types, self.row_chains = [], [], []
            self._n_matches = 0
            self._show_groups([])
            self._fill_table()
            return
        try:
            rows = (sc.matches(sc.count) if groups else sc.matches()) if show_matches else []
            self._n_matches = len(rows)
            types = [m.vtype or self._vtype() for m in rows]
            # by address and type: a match read as another type doesn't hide a typed-in
            # (or traced) row, which converts screen units with its own type
            seen = {(m.address, t) for m, t in zip(rows, types, strict=True)}
            chains: list[Chain | None] = [None] * len(rows)
            for addr, vt in self.manual.items():
                if (addr, vt) not in seen:
                    rows.append(self._read_match(addr, vt))
                    types.append(vt)
                    chains.append(None)
            for chain, vt in self.pointers.items():
                addr = ptr.resolve_live(self.ctl.session, chain)
                try:
                    m = self._read_match(addr, vt) if addr else Match(0, 0, b"")
                except ReadFailed:
                    m = Match(0, 0, b"")
                rows.append(m)
                types.append(vt)
                chains.append(chain)
        except Exception as exc:
            self.timer.stop()
            self.ctl.fail(exc, "read")
            return
        self.rows, self.row_types, self.row_chains = rows, types, chains
        self._read_trace_maxes()
        self._show_groups(groups)
        self._fill_table()
        # Big lists refresh less often (each separate read costs ~36 ms on the PS5).
        self.timer.setInterval(max(LIVE_MS, 40 * len(self.rows)))
        if not self.timer.isActive():
            self.timer.start()

    def _set_show_limit(self, n: int) -> None:
        self.ctl.scanner.show_limit = n
        self.guide.show_limit = n
        self.ctl.settings.show_matches = n
        try:
            self.ctl.settings.save()
        except OSError as exc:
            log.warning("action=save_settings reason=%s", exc)
        if self.ctl.scanner.active:
            self._after_scan(self.ctl.scanner.count or 0)

    def _groups_to_show(self) -> list[Group]:
        """Groups, when they fold copies together and fit the "Show up to" (AC-G1)."""
        sc = self.ctl.scanner
        groups = sc.groups()
        if len(groups) > sc.show_limit or all(len(g.members) == 1 for g in groups):
            return []
        return self._checked_first(groups) if self.verdicts else groups

    def _checked_first(self, groups: list[Group]) -> list[Group]:
        """A member Check which is real found becomes its group's head, so the group
        row, Make a cheat and Freeze act on it; those groups go first."""
        out = []
        for g in groups:
            real = [
                (r.followers, -k)  # most followers; the first on a tie
                for k, m in enumerate(g.members)
                if (r := self.verdicts.get((m.address, m.vtype))) is not None
                and r.verdict is Verdict.REAL
            ]
            out.append(promote(g, -max(real)[1]) if real else g)
        return sorted(out, key=lambda g: not self._found_real(g.head.address, g.head.vtype))

    def _found_real(self, addr: int, vt: ValueType) -> bool:
        r = self.verdicts.get((addr, vt))
        return r is not None and r.verdict is Verdict.REAL

    def _show_groups(self, groups: list[Group]) -> None:
        if [g.members for g in groups] != [g.members for g in self.groups]:
            self._tree_pick = self._tree_selection()
            self.tree.clear()  # a new grouping: build the tree again in _fill_table
        self.groups = groups
        self.table.setVisible(not groups)
        self.tree.setVisible(bool(groups))

    def _row_text(self, i: int) -> str:
        m = self.rows[i]
        frozen = " (frozen)" if m.address in self.ctl.frozen_addresses else ""
        t = self._traced_for(m.address, self.row_types[i])
        if m.address in self.manual:
            frozen += f" (added, {self.manual[m.address].label})"
        if t is not None and not self.grouped:  # the tree says these in its columns
            frozen += f" ({t.way.label()}, {t.notes()})"
        chain = self.row_chains[i] if i < len(self.row_chains) else None
        if chain is not None:
            frozen += f" (pointer {chain.label()})"
            if m.address == 0:
                frozen = f" (pointer {chain.label()} leads nowhere right now)"
        r = self.verdicts.get((m.address, self.row_types[i])) if i < self._n_matches else None
        if r is not None and not self.grouped:  # the tree says it in Notes
            frozen += f" ({r.verdict.short})"
        return self._value_text(m.address, self.row_types[i], m.value) + frozen

    def _traced_for(self, addr: int, vt: ValueType) -> TracedRow | None:
        """The trace behind a row, only for a row of the traced type: a search match
        at the same address read as another type is not converted."""
        t = self.traced.get(addr)
        if t is None or addr not in self.manual or t.way.real is not vt:
            return None
        return t

    def _value_text(self, addr: int, vt: ValueType, value: float | int) -> str:
        """Value now; a traced row also shows the screen value: ``0.87 (screen: 87)``."""
        t = self._traced_for(addr, vt)
        if t is None or (isinstance(value, float) and not math.isfinite(value)):
            return fmt_value(value)
        max_now = self._trace_max.get(addr)
        try:
            factor = t.way.factor(max_now)
            screen = t.way.to_screen(value, max_now)
        except (ValueError, OverflowError):
            return fmt_value(value)
        if factor == 0 or not math.isfinite(factor):  # a max of 0: nothing sensible shows
            return f"{fmt_value(value)} (screen: ?)"
        return f"{fmt_value(value)} (screen: {fmt_value(screen)})"

    def _read_trace_maxes(self) -> None:
        """A share-of-max way's max, read once per refresh (one read per traced row)."""
        self._trace_max = {}
        for addr, t in self.traced.items():
            if not t.way.max_addresses:
                continue
            try:
                self._trace_max[addr] = self._read_maxes(t.way)
            except Exception as exc:  # shown without the screen value this time
                log.info("action=trace_max_read addr=%#x reason=%s", addr, exc)

    def _read_maxes(self, way: Way) -> dict[int, float | int] | None:
        if not way.max_addresses:
            return None
        g = self.ctl.session.require_game()
        client = self.ctl.session.require_client()
        out: dict[int, float | int] = {}
        for s in way.steps:
            if s.max_at is not None:
                vt = ValueType.DOUBLE if s.to is ValueType.DOUBLE else ValueType.FLOAT
                out[s.max_at] = vt.decode(client.read(g.pid, s.max_at, vt.size))
        return out

    def _fill_table(self) -> None:
        if self.grouped:
            self._fill_tree()
            self._update_buttons()
            return
        keep = self.table.selectionModel().selectedRows()
        keep_row = keep[0].row() if keep else None
        self.table.setRowCount(len(self.rows))
        for i, m in enumerate(self.rows):
            self.table.setItem(i, 0, QTableWidgetItem(self._row_text(i)))
            self.table.setItem(i, 1, QTableWidgetItem(f"{m.address:#x}"))
        self._apply_filter()
        if keep_row is not None and keep_row < len(self.rows):
            if self.table.isRowHidden(keep_row):
                self.table.clearSelection()
            else:
                self.table.selectRow(keep_row)
        self._update_buttons()

    def _fill_tree(self) -> None:
        """One top row per group (its likely real match first), its members under it;
        typed-in addresses and pointers after them (AC-G4)."""
        index: dict[tuple[int, ValueType], int] = {}
        for i, m in enumerate(self.rows):
            index.setdefault((m.address, self.row_types[i]), i)
        if self.tree.topLevelItemCount() != len(self.groups) + len(self.rows) - self._n_matches:
            self._tree_pick = self._tree_pick or self._tree_selection()
            self.tree.clear()  # rows were added or removed: build it again
        if self.tree.topLevelItemCount() == 0:
            for g in self.groups:
                head = index.get((g.head.address, g.head.vtype), -1)
                top = QTreeWidgetItem()
                top.setData(0, ROW, head)
                if len(g.members) > 1:
                    for k, m in enumerate(g.members):
                        child = QTreeWidgetItem()
                        child.setData(0, ROW, index.get((m.address, m.vtype), head))
                        child.setData(1, ROW, k)
                        top.addChild(child)
                self.tree.addTopLevelItem(top)
            for i in range(self._n_matches, len(self.rows)):
                top = QTreeWidgetItem()
                top.setData(0, ROW, i)
                self.tree.addTopLevelItem(top)
            if self._tree_pick is not None:
                addr, vt, member = self._tree_pick
                self._tree_pick = None
                row = index.get((addr, vt))
                if row is not None:
                    self._select_row(row, member)
        for t in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(t)
            g = self.groups[t] if t < len(self.groups) else None
            self._item_text(top, g, None)
            for c in range(top.childCount()):
                self._item_text(top.child(c), g, top.child(c).data(1, ROW))
        self._apply_filter()

    def _item_text(self, item: QTreeWidgetItem, g: Group | None, member: int | None) -> None:
        i = item.data(0, ROW)
        if not isinstance(i, int) or not 0 <= i < len(self.rows):
            return
        m = self.rows[i]
        item.setText(0, self._row_text(i))
        item.setText(1, way_label(m.way) if self.ctl.scanner.has_ways else "")
        item.setText(3, f"{m.address:#x}")
        checked = self.verdicts.get((m.address, self.row_types[i]))
        traced = self._traced_for(m.address, self.row_types[i]) if g is None else None
        if traced is not None:
            item.setText(1, traced.way.label())
            item.setText(2, traced.notes())
        elif g is None:
            item.setText(2, "")
        elif member is None:  # the group's row
            notes = [g.label] if g.label else []
            if checked is not None and (checked.verdict is Verdict.REAL or not g.copies):
                notes = [checked.verdict.short]
            if g.copies:
                notes.append(f"+{g.copies} {'copy' if g.copies == 1 else 'copies'}")
            item.setText(2, ", ".join(notes))
        elif checked is not None:
            item.setText(2, checked.verdict.short)
        else:
            item.setText(2, "likely real" if member in g.likely else "")

    def _apply_filter(self) -> None:
        """Hide rows whose live value doesn't match the filter box. Rows are hidden,
        not removed, so table row i is still self.rows[i]."""
        try:
            keep = parse_filter(self.filter.text())
            self.filter.setStyleSheet("")
        except ValueError:
            keep = None  # half-typed, e.g. "90-": show everything until it parses
            self.filter.setStyleSheet("color: #c0392b;")
        if self.grouped:
            self._filter_tree(keep)
            return
        shown = 0
        for i, m in enumerate(self.rows):
            hide = keep is not None and (m.raw == b"" or not in_filter(m.value, keep))
            self.table.setRowHidden(i, hide)
            shown += not hide
        if keep is None or not self.rows:
            self.filter_count.setText("")
        else:
            self.filter_count.setText(f"{shown} of {len(self.rows)} shown")

    def _filter_tree(self, keep: tuple[float, float] | None) -> None:
        """Hide a group when none of its matches fits the filter (AC-G4)."""

        def fits(item: QTreeWidgetItem) -> bool:
            i = item.data(0, ROW)
            if keep is None:
                return True
            if not isinstance(i, int) or not 0 <= i < len(self.rows):
                return False
            m = self.rows[i]
            return m.raw != b"" and in_filter(m.value, keep)

        shown = total = 0
        for t in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(t)
            kids = [top.child(c) for c in range(top.childCount())]
            hide = not any(fits(it) for it in [top, *kids])
            top.setHidden(hide)
            for kid in kids:
                kid.setHidden(not fits(kid))
            total += 1
            shown += not hide
        if keep is None or not total:
            self.filter_count.setText("")
        else:
            self.filter_count.setText(f"{shown} of {total} shown")
        if self.tree.selectedItems() and self.tree.selectedItems()[0].isHidden():
            self.tree.clearSelection()

    def _tree_selection(self) -> tuple[int, ValueType, bool] | None:
        """The selected (address, number type, is an expanded member) in the tree."""
        items = self.tree.selectedItems()
        i = items[0].data(0, ROW) if items else None
        if not isinstance(i, int) or not 0 <= i < len(self.rows):
            return None
        return self.rows[i].address, self.row_types[i], items[0].parent() is not None

    def _select_row(self, i: int, member: bool = False) -> None:
        """Select row ``i`` in whichever list is showing. In the tree a group's row is
        preferred, or an expanded member when ``member``; either is found."""
        if not self.grouped:
            self.table.selectRow(i)
            return
        tops = [self.tree.topLevelItem(t) for t in range(self.tree.topLevelItemCount())]
        kids = [top.child(c) for top in tops for c in range(top.childCount())]
        for it in (kids + tops) if member else (tops + kids):
            if it.data(0, ROW) == i:
                if it.parent() is not None:
                    it.parent().setExpanded(True)
                self.tree.setCurrentItem(it)
                return

    def _vtype(self) -> ValueType:
        assert self.ctl.scanner.vtype is not None
        return self.ctl.scanner.vtype

    def _row_type(self) -> ValueType:
        i = self._selected_row()
        assert i is not None
        return self.row_types[i]

    def _read_match(self, addr: int, vt: ValueType) -> Match:
        g = self.ctl.session.require_game()
        raw = self.ctl.session.require_client().read(g.pid, addr, vt.size)
        return Match(addr, vt.decode(raw), raw)

    def _search_lost(self) -> None:
        """Find it again searched memory: the matches here are gone (the number the
        player typed stays, so Search starts again in one click)."""
        self.unknown_mode = False
        self.rows = []
        self._n_matches = 0
        self._end_search()
        self._show_groups([])
        self.tree.clear()
        self.count.setText("")
        self.table.setRowCount(0)
        self.timer.stop()
        self.hint.setText(hints.SEARCH_LOST)
        self._update_buttons()

    def _new_game(self, game=None) -> None:
        self.manual.clear()  # addresses belong to the old game's memory
        self.traced.clear()
        key = (game.title_id, game.version) if game is not None else None
        if key != self._game_key:
            self.pointers.clear()  # pointers last across restarts of the same version
        self._game_key = key
        self.do_reset()

    def prefill(self, vtype: ValueType, value: float | int | None) -> None:
        """Find it again failed (AC-12.4): start a new search with the type and the last
        value already filled in."""
        self.do_reset()
        self.advanced.setChecked(True)
        self.vtype.setCurrentIndex(self.vtype.findData(vtype))
        if value is not None:
            self.number.setText(f"{value:g}" if vtype.is_float else str(value))
        self.hint.setText(hints.FIND_START)

    def do_add_address(self) -> None:
        text = self.add_addr.text().strip().replace(" ", "")
        try:
            addr = int(text, 16)
            if addr <= 0:
                raise ValueError
        except ValueError:
            self.ctl.fail(
                ValueError(f'"{text}" isn\'t an address. Type it like 0x12e0ea60ac.'), "add"
            )
            return
        vt = self.add_type.currentData()
        try:
            self._read_match(addr, vt)
        except Exception:
            self.ctl.fail(
                ValueError(
                    f"Nothing is at {addr:#x} in the game right now. Addresses change when "
                    "the game restarts or an area unloads."
                ),
                "add",
            )
            return
        log.info("action=add_address addr=%#x type=%s", addr, vt.name)
        self.manual[addr] = vt
        self.traced.pop(addr, None)
        self.add_addr.clear()
        self.refresh_live()
        self._select_row(next(i for i, m in enumerate(self.rows) if m.address == addr))

    def do_test_hits(self) -> None:
        """AC-H6: narrow the current matches to the real one by asking the player."""
        sc = self.ctl.scanner
        if not sc.active:
            return
        if sc.vtype is None:
            self.ctl.message.emit(
                "Test the hits works on one number type. Search again until only "
                "decimals or only whole numbers are left, or pick the type in Advanced."
            )
            return
        try:
            addrs, scale = self._test_candidates()
            note = hints.TEST_HITS_RAW if scale == OF_FULL else ""
            dlg = HitTestDialog(self.ctl, addrs, sc.vtype, self.use_found, self, scale, note)
        except Exception as exc:
            self.ctl.fail(exc, "test hits")
            return
        self.hit_dialog = dlg
        dlg.exec()

    def do_check_real(self) -> None:
        """spec-real-check: write a test value to each listed match, see which ones the
        game puts back (copies) and which one the others follow (real)."""
        if not self.ctl.scanner.active or self._n_matches == 0:
            return
        frozen = set(self.ctl.frozen_addresses)
        if self.grouped:
            targets = targets_from_groups(self.groups, frozen)
        else:
            rows = [(self.rows[i].address, self.row_types[i]) for i in range(self._n_matches)]
            targets = targets_from_list(rows, frozen)
        try:
            g = self.ctl.session.require_game()
            checker = RealChecker(self.ctl.session.require_client(), g.pid, targets)
        except Exception as exc:
            self.ctl.fail(exc, "check")
            return
        log.info(
            "action=check_real_start groups=%d addresses=%d",
            len(checker.groups),
            sum(len(t) for t in checker.groups),
        )
        self.timer.stop()  # the list would show the test values for a moment
        self.hint.setText(hints.CHECK_REAL_RUNNING)
        self._cancel = threading.Event()
        cancel = self._cancel
        gen = self._search_gen
        self._checking = True
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self._started = time.monotonic()
        self._update_buttons()
        self.ctl.tasks.run(
            lambda: checker.run(self._progress.done.emit, cancel),
            lambda _r: self._after_check(checker, cancel.is_set(), gen),
            lambda exc: self._check_failed(exc, checker, gen),
        )

    def _after_check(self, checker: RealChecker, stopped: bool, gen: int) -> None:
        self._cancel = None
        self._checking = False
        self.progress.setValue(100)
        if gen != self._search_gen:  # the search ended while it ran: these are stale
            self._update_buttons()
            return
        self.verdicts.update(checker.results)
        found = [b for grp in checker.groups if (b := checker.best(grp)) is not None]
        verdicts = [r.verdict for r in checker.results.values()]
        if found:
            best = max(found, key=lambda t: checker.results[(t.address, t.vtype)].followers)
            n = checker.results[(best.address, best.vtype)].followers
            text = hints.CHECK_REAL_FOUND.format(
                addr=f"{best.address:#x}", n=n, copies="copy" if n == 1 else "copies"
            )
        elif Verdict.KEPT in verdicts:
            text = hints.CHECK_REAL_KEPT
        elif Verdict.COPY in verdicts and all(
            v in (Verdict.COPY, Verdict.FAILED) for v in verdicts
        ):
            text = hints.CHECK_REAL_NONE
        else:
            text = hints.CHECK_REAL_STOPPED if stopped else hints.CHECK_REAL_FAILED
        if stopped:
            text = f"{hints.CHECK_REAL_STOPPED} {text}" if found else hints.CHECK_REAL_STOPPED
        self.hint.setText(text)
        self.refresh_live()
        tip = None if stopped else self.guide.after_check(verdicts)
        if tip is not None:
            self._suggest(tip)
        if found:
            rows = [
                i
                for i in range(self._n_matches)
                if (self.rows[i].address, self.row_types[i]) == (best.address, best.vtype)
            ]
            if rows:
                self._select_row(rows[0])
        self._update_buttons()

    def _check_failed(self, exc: BaseException, checker: RealChecker, gen: int) -> None:
        self._cancel = None
        self._checking = False
        self.progress.setValue(0)
        if checker.not_restored:
            addrs = ", ".join(f"{a:#x}" for a in checker.not_restored)
            self.hint.setText(hints.CHECK_REAL_NOT_RESTORED.format(addrs=addrs))
        elif gen == self._search_gen:
            self.hint.setText(hints.FIND_FEW)
        self._update_buttons()
        self.ctl.fail(exc, "check")
        if self.ctl.session.connected and gen == self._search_gen:
            self.refresh_live()

    def _test_candidates(self) -> tuple[list[int], Scale]:
        """Test the hits candidates and the units to ask in. Grouped list: one per
        group, its likely real match (or the expanded row picked in it); a single group
        tests its members (AC-G4).

        One test value has to mean the same to every candidate, so when they keep the
        number different ways (percent, fraction, x10, ...) only the ones stored the way
        the first candidate is are tested: the first group's head (or the row picked in
        it). Without groups there is no head, and when the head's way would leave just
        the head there is nothing to test it against: then the most common way is used
        (the first candidate's on a tie). Raises ValueError when fewer than 2 are left."""
        sc = self.ctl.scanner
        hits = self.ctl.session.require_client().scan_results(TEST_LIMIT)
        # (address, way), in the order they are offered: first group first
        if not self.grouped:
            cands = [(h.address, h.way) for h in hits]
        elif len(self.groups) == 1:
            cands = [(m.address, m.way) for m in self.groups[0].members]
        else:
            pick = self._tree_selection()
            cands = []
            for g in self.groups:
                use = g.head
                if pick is not None and pick[2]:  # an expanded row was picked
                    use = next((m for m in g.members if (m.address, m.vtype) == pick[:2]), g.head)
                cands.append((use.address, use.way))
        counts: dict[Scale | None, int] = {}
        for _a, w in cands:
            counts[w] = counts.get(w, 0) + 1
        way = cands[0][1] if cands else None  # the head's way
        if not self.grouped or counts.get(way, 0) < 2:
            way = max(counts, key=lambda w: (counts[w], w == cands[0][1])) if cands else None
        addrs = [a for a, w in cands if w == way]
        if len(counts) > 1 and len(addrs) < 2:
            raise ValueError(
                "These matches keep the number different ways, so one test value can't "
                "check them all. Expand a group and pick a row, or search again first."
            )
        if way is None or sc.scale is not SHOWN:
            return addrs, sc.scale
        return addrs, way

    def use_found(self, addr: int, vt: ValueType) -> None:
        """The winner of Test the hits goes in the list like a typed-in address."""
        log.info("action=test_hits_use addr=%#x type=%s", addr, vt.name)
        self.manual[addr] = vt
        self.refresh_live()
        rows = [i for i, m in enumerate(self.rows) if m.address == addr]
        if rows:
            self._select_row(rows[-1])

    def do_pointer_scan(self) -> None:
        i = self._selected_row()
        m = self.selected()
        target = m.address if m is not None and m.address else None
        vt = self.row_types[i] if i is not None else self.add_type.currentData()
        dlg = PointerDialog(self.ctl, target, vt, self.use_pointer, self)
        dlg.exec()

    def use_pointer(self, chain: Chain, vt: ValueType) -> None:
        log.info("action=use_pointer chain=%s type=%s", chain.label(), vt.name)
        self.pointers[chain] = vt
        self.refresh_live()
        self._select_row(len(self.rows) - 1)

    def do_remove_address(self) -> None:
        i = self._selected_row()
        if i is not None and i < len(self.row_chains) and self.row_chains[i] is not None:
            m = self.rows[i]
            if m.address in self.ctl.frozen_addresses:
                self.ctl.freezer.unfreeze(f"find:{m.address}")
                self.ctl.frozen_addresses.discard(m.address)
            del self.pointers[self.row_chains[i]]
            self.refresh_live()
            return
        m = self.selected()
        if m is None or m.address not in self.manual:
            return
        if m.address in self.ctl.frozen_addresses:
            self.ctl.freezer.unfreeze(f"find:{m.address}")
            self.ctl.frozen_addresses.discard(m.address)
        del self.manual[m.address]
        self.traced.pop(m.address, None)
        self.refresh_live()

    def do_set(self) -> None:
        m = self.selected()
        if m is None:
            return
        try:
            value = self._real_value(
                m.address, self._row_type(), parse_number(self.set_value.text())
            )
            data = self._row_type().encode(value)
            g = self.ctl.session.require_game()
            log.info(
                "action=write type=%s addr=%#x bytes=%s",
                self._row_type().name,
                m.address,
                data.hex(),
            )
            self.ctl.session.require_client().write(g.pid, m.address, data)
            if m.address in self.ctl.frozen_addresses:
                self.ctl.freezer.freeze(f"find:{m.address}", [(g.pid, m.address, data)])
        except Exception as exc:
            self.ctl.fail(exc, "write")
            return
        self.refresh_live()
        if m.address not in self.ctl.frozen_addresses:
            self._watch_put_back(m.address, data)

    def _real_value(self, addr: int, vt: ValueType, typed: float | int) -> float | int:
        """A traced row's typed number is what the screen shows: turn it into the real
        value with the row's way (87 -> 0.87). Other rows: as typed. When the game
        can't show exactly that number, the nearest is used and the player told."""
        t = self._traced_for(addr, vt)
        if t is None:
            return typed
        max_now = self._read_maxes(t.way)
        real, exact = t.way.real_for(typed, max_now)
        log.info("action=trace_set addr=%#x screen=%s real=%s exact=%s", addr, typed, real, exact)
        if not exact:
            try:
                shows = fmt_value(t.way.to_screen(real, max_now))
            except (ValueError, OverflowError):
                shows = "?"
            self.ctl.message.emit(hints.TRACE_NEAREST.format(typed=fmt_value(typed), shows=shows))
        return real

    def _watch_put_back(self, addr: int, data: bytes) -> None:
        """Set/Freeze put back by the game (spec-trace AC-T1): look again in 2 s."""
        QTimer.singleShot(PUT_BACK_MS, self, lambda: self.check_put_back(addr, data))

    def check_put_back(self, addr: int, data: bytes) -> bool:
        """The game wrote its own value over ours: suggest tracing the real one."""
        if not self._ready():
            return False
        try:
            g = self.ctl.session.require_game()
            now = self.ctl.session.require_client().read(g.pid, addr, len(data))
        except Exception as exc:  # gone or lost: no suggestion
            log.info("action=put_back_check addr=%#x reason=%s", addr, exc)
            return False
        if now == data:
            return False
        log.info("action=put_back addr=%#x wrote=%s now=%s", addr, data.hex(), now.hex())
        self._suggest(self.guide.after_put_back())
        return True

    def do_freeze(self) -> None:
        m = self.selected()
        if m is None:
            return
        key = f"find:{m.address}"
        if m.address in self.ctl.frozen_addresses:
            self.ctl.freezer.unfreeze(key)
            self.ctl.frozen_addresses.discard(m.address)
        else:
            try:
                g = self.ctl.session.require_game()
                raw = self.ctl.session.require_client().read(
                    g.pid, m.address, self._row_type().size
                )
            except Exception as exc:
                self.ctl.fail(exc, "read")
                return
            self.ctl.freezer.freeze(key, [(g.pid, m.address, raw)])
            self.ctl.frozen_addresses.add(m.address)
            # best effort: the freezer may win the race with the game's own write
            self._watch_put_back(m.address, raw)
        self._fill_table()

    def do_make(self) -> None:
        m = self.selected()
        if m is None:
            return
        template = self.template.currentData()
        try:
            kind, offset = self.ctl.session.classify(m.address)
            value = parse_number(self.set_value.text()) if self.set_value.text().strip() else None
            if value is not None:
                value = self._real_value(m.address, self._row_type(), value)
            cheat = make_cheat(template, kind, offset, self._row_type(), m.raw, value)
        except Exception as exc:
            self.ctl.fail(exc, "make_cheat")
            return
        self.ctl.ensure_file().cheats.append(cheat)
        self.ctl.cheats_changed.emit()
        self.ctl.message.emit(f'Made "{cheat.name}". Tick it to turn it on.')

    # -- Find the real one behind it (spec-trace) ------------------------------------------
    def do_trace(self) -> None:
        """Trace the picked row (a group row traces its head, an expanded member that
        member: the selection already says which)."""
        m = self.selected()
        if m is None or not m.address:
            self.ctl.message.emit(hints.TRACE_PICK_ONE)
            return
        vt = self._row_type()
        frozen = {
            a: self.row_types[i].size
            for i, row in enumerate(self.rows)
            if (a := row.address) in self.ctl.frozen_addresses
        }
        self.open_trace(
            address=m.address,
            vtype=vt,
            on_found=self.use_traced,
            on_search_way=self.search_traced_way,
            frozen=frozen,
        )

    def use_traced(self, result: TraceResult) -> None:
        """The value behind the copy goes in the list like Test the hits' winner."""
        if result.address is None or result.vtype is None:
            return
        log.info(
            "action=trace_use addr=%#x type=%s sure=%s",
            result.address,
            result.vtype.name,
            result.sure,
        )
        self.manual[result.address] = result.vtype
        if result.way is not None:
            check = result.verdict.short if result.verdict is not None else "not sure"
            self.traced[result.address] = TracedRow(result.way, result.behind, check, result.sure)
        self.refresh_live()
        rows = [
            i
            for i, m in enumerate(self.rows)
            if (m.address, self.row_types[i]) == (result.address, result.vtype)
        ]
        if rows:
            self._select_row(rows[-1])
        self.hint.setText(trace_result_text(result))

    def search_traced_way(self, result: TraceResult, screen: float | int | None) -> None:
        """Search for it that way (AC-T8): Start over with Advanced filled in (the
        traced number type, "Game stores it as" the traced factor) and the number the
        screen shows now typed in; the player presses Search."""
        way = result.way
        if way is None:
            return
        try:
            scale = way.as_scale(result.max_now)
        except ValueError as exc:
            self.ctl.fail(exc, "trace")
            return
        self.do_reset()
        self._traced_scale = scale
        self.advanced.setChecked(True)
        self._guide_set_advanced = True  # Start over takes Advanced back off
        self.vtype.setCurrentIndex(self.vtype.findData(way.real))
        at = self.scale.findData(TRACED_SCALE)
        if at < 0:
            self.scale.addItem(scale.label, TRACED_SCALE)
            at = self.scale.count() - 1
        self.scale.setItemText(at, scale.label)
        self.scale.setCurrentIndex(at)
        if screen is not None:
            self.number.setText(way.shown.text(screen))
        self._suggest(Suggestion(hints.FIND_START, Action.SEARCH))

    # -- Make it work (spec-cheat-builder B1) ------------------------------------------
    def _group_of(self, addr: int, vt: ValueType) -> Group | None:
        return next(
            (g for g in self.groups if any((m.address, m.vtype) == (addr, vt) for m in g.members)),
            None,
        )

    def do_build(self) -> None:
        """Open Make it work on the picked row. A group that hasn't been checked yet is
        checked first (one group only); its verdicts come back to this list."""
        m = self.selected()
        if m is None:
            return
        vt = self._row_type()
        if vt not in cv.EFFECT_TYPES:
            self.ctl.message.emit(cv.UNSUPPORTED_TYPE)
            return
        group = self._group_of(m.address, vt)
        targets: list = []
        if group is not None and not any(
            (gm.address, gm.vtype) in self.verdicts for gm in group.members
        ):
            found = targets_from_groups([group], set(self.ctl.frozen_addresses))
            targets = found[0] if found else []
        self.open_build(
            address=m.address,
            vtype=vt,
            name=self.template.currentText() or "Value",
            group=targets,
            on_real_check=self.merge_verdicts,
        )

    def merge_verdicts(self, results: dict[tuple[int, ValueType], Result]) -> None:
        """Verdicts from Make it work's Check which is real, shown like our own."""
        self.verdicts.update(results)
        if self.ctl.session.connected:
            self.refresh_live()
