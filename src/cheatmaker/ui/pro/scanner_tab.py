"""The Pro Scanner tab (spec PR-2): regions, every number type and compare, progress with
time left (AC-2.1), results paged 1,000 at a time with current, previous and first
values, AOB search over the game's code, and adding results to the address list."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from cheatmaker.console.client import ScanHit
from cheatmaker.core.pro import scan as sc
from cheatmaker.core.pro.addresses import place_text
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.errors import ScanCancelled
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.address_list import parse_value
from cheatmaker.ui.pro.tab import ProMessages, ProTab
from cheatmaker.ui.tasks import TaskRunner

FIRST_COMPARES = (Compare.EXACT, Compare.BETWEEN, Compare.GREATER, Compare.LESS, Compare.UNKNOWN)
NEXT_COMPARES = (
    Compare.EXACT,
    Compare.BETWEEN,
    Compare.GREATER,
    Compare.LESS,
    Compare.INCREASED,
    Compare.DECREASED,
    Compare.INCREASED_BY,
    Compare.DECREASED_BY,
    Compare.CHANGED,
    Compare.UNCHANGED,
)
COL_ADDR, COL_VALUE, COL_PREVIOUS, COL_FIRST = range(4)


class ScannerTab(ProTab):
    _progress = Signal(float)  # worker thread -> UI thread

    def __init__(self, ctl: Controller, tasks: TaskRunner, msgs: ProMessages | None = None) -> None:
        super().__init__(ctl, tasks, msgs)
        self.scan = sc.ProScan(ctl.session)
        self.add_address: Callable[[int, ValueType], None] = lambda a, t: None  # the window
        self.show_code: Callable[[int], None] = lambda addr: None
        self.clock: Callable[[], float] = time.monotonic
        self.hits: list[ScanHit] = []
        self.aob_hits: list[int] = []
        self.page = 0
        self.running_scan = False
        self._cancel: threading.Event | None = None
        self._started = 0.0

        self.hint = QLabel(hints.SCANNER)
        self.hint.setWordWrap(True)
        self.region = QComboBox()
        self.region.addItems(list(sc.REGIONS))
        self.vtype = QComboBox()
        for t in ValueType:
            self.vtype.addItem(t.label, t)
        self.vtype.setCurrentIndex(list(ValueType).index(ValueType.INT32))
        self.compare = QComboBox()
        self.value = QLineEdit()
        self.value.setPlaceholderText("Value")
        self.value2 = QLineEdit()
        self.value2.setPlaceholderText("to")
        self.first_btn = QPushButton("First scan")
        self.next_btn = QPushButton("Next scan")
        self.new_btn = QPushButton("New scan")
        self.cancel_btn = QPushButton("Cancel")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.left = QLabel("")
        self.count = QLabel("")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Address", "Value", "Previous", "First"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.prev_page_btn = QPushButton("Previous 1,000")
        self.next_page_btn = QPushButton("Next 1,000")
        self.page_label = QLabel("")
        self.add_btn = QPushButton("Add to address list")
        self.aob = QLineEdit()
        self.aob.setPlaceholderText(hints.AOB_TIP)
        self.aob_btn = QPushButton("Find AOB")

        r1 = QHBoxLayout()
        for w in (QLabel("Where:"), self.region, QLabel("Type:"), self.vtype):
            r1.addWidget(w)
        r1.addStretch(1)
        r2 = QHBoxLayout()
        for w in (self.compare, self.value, self.value2, self.first_btn, self.next_btn):
            r2.addWidget(w)
        r2.addWidget(self.new_btn)
        r3 = QHBoxLayout()
        r3.addWidget(self.progress, 1)
        r3.addWidget(self.left)
        r3.addWidget(self.cancel_btn)
        r4 = QHBoxLayout()
        for w in (self.count, self.prev_page_btn, self.page_label, self.next_page_btn):
            r4.addWidget(w)
        r4.addStretch(1)
        r4.addWidget(self.add_btn)
        r5 = QHBoxLayout()
        r5.addWidget(QLabel("AOB:"))
        r5.addWidget(self.aob, 1)
        r5.addWidget(self.aob_btn)
        lay = QVBoxLayout(self.body)
        lay.addWidget(self.hint)
        for r in (r1, r2, r3):
            lay.addLayout(r)
        lay.addWidget(self.table, 1)
        lay.addLayout(r4)
        lay.addLayout(r5)

        self._progress.connect(self._show_progress)
        self.compare.currentIndexChanged.connect(lambda _i: self._update_buttons())
        self.first_btn.clicked.connect(self.do_first)
        self.next_btn.clicked.connect(self.do_next)
        self.new_btn.clicked.connect(self.do_new)
        self.cancel_btn.clicked.connect(self.do_cancel)
        self.prev_page_btn.clicked.connect(lambda: self.load_page(self.page - 1))
        self.next_page_btn.clicked.connect(lambda: self.load_page(self.page + 1))
        self.add_btn.clicked.connect(self.do_add)
        self.table.cellDoubleClicked.connect(lambda row, _c: self._add_rows([row]))
        self.aob_btn.clicked.connect(self.do_aob)
        self.aob.returnPressed.connect(self.do_aob)
        self._set_compares()

    # -- state ----------------------------------------------------------------------------
    def _set_compares(self) -> None:
        keep = self.compare.currentData()
        options = NEXT_COMPARES if self.scan.active else FIRST_COMPARES
        self.compare.blockSignals(True)
        self.compare.clear()
        for c in options:
            self.compare.addItem(c.value, c)
        k = self.compare.findData(keep)
        self.compare.setCurrentIndex(max(k, 0))
        self.compare.blockSignals(False)
        self._update_buttons()

    def _update_buttons(self) -> None:
        cmp: Compare | None = self.compare.currentData()
        idle = not self.running_scan
        active = self.scan.active
        self.first_btn.setEnabled(idle and not active)
        self.next_btn.setEnabled(idle and active)
        self.new_btn.setEnabled(idle and (active or bool(self.aob_hits)))
        self.region.setEnabled(idle and not active)
        self.vtype.setEnabled(idle and not active)
        self.cancel_btn.setEnabled(not idle)
        self.value.setEnabled(cmp is not None and cmp.needs_value)
        self.value2.setVisible(cmp is Compare.BETWEEN)
        pages = self.scan.pages() if active else 0
        self.prev_page_btn.setEnabled(idle and self.page > 0)
        self.next_page_btn.setEnabled(idle and self.page + 1 < pages)
        self.page_label.setText(
            hints.SCAN_PAGE.format(page=self.page + 1, pages=pages) if pages > 1 else ""
        )
        self.add_btn.setEnabled(idle and bool(self.hits) and not self.aob_hits)
        self.aob_btn.setEnabled(idle)

    def game_state_changed(self, new_game: bool) -> None:
        if new_game:
            self.scan.reset()
            self._clear()
        self._set_compares()

    def _clear(self) -> None:
        self.hits, self.aob_hits, self.page = [], [], 0
        self.table.setRowCount(0)
        self.count.setText("")

    # -- scans on the worker --------------------------------------------------------------
    def _value(self, cmp: Compare, vtype: ValueType):
        if not cmp.needs_value:
            return None
        first = parse_value(self.value.text(), vtype)
        second = parse_value(self.value2.text(), vtype) if cmp is Compare.BETWEEN else None
        return sc.scan_value(cmp, vtype, first, second)

    def _start(self, work, action: str) -> None:
        self.running_scan = True
        self._cancel = threading.Event()
        self._started = self.clock()
        self.progress.setValue(0)
        self.left.setText("")
        self._update_buttons()

        def done(count: int) -> None:
            self.running_scan = False
            self.progress.setValue(100)
            self.left.setText("")
            self._set_compares()
            self.count.setText(hints.SCAN_COUNT.format(n=count))
            self.load_page(0)

        def failed(exc: BaseException) -> None:
            self.running_scan = False
            self.left.setText("")
            if isinstance(exc, ScanCancelled):
                self.msgs.say(hints.SCAN_CANCELLED)
            else:
                self.msgs.fail(exc, action)
            self._set_compares()

        self.tasks.run(work, done, failed)

    def _show_progress(self, fraction: float) -> None:
        pct = max(0, min(100, int(fraction * 100)))
        self.progress.setValue(pct)
        left = sc.time_left(self.clock() - self._started, fraction)
        self.left.setText(hints.SCAN_PROGRESS.format(pct=pct, left=sc.time_left_text(left)))

    def do_first(self) -> None:
        vtype: ValueType = self.vtype.currentData()
        cmp: Compare = self.compare.currentData()
        try:
            value = self._value(cmp, vtype)
        except ValueError as exc:
            self.msgs.fail(exc, "pro_scan")
            return
        if self.ctl.tasks.busy:  # a guided search or read may be using the console's search
            self.msgs.say(hints.GUIDED_BUSY)
            return
        region = self.region.currentText()
        self._clear()
        self.scan.claim()  # here, on the UI thread, before the worker starts

        def work() -> int:
            return self.scan.first(region, vtype, cmp, value, self._progress.emit, self._cancel)

        self._start(work, "pro_scan")

    def do_next(self) -> None:
        cmp: Compare = self.compare.currentData()
        vtype = self.scan.vtype or self.vtype.currentData()
        try:
            value = self._value(cmp, vtype)
        except ValueError as exc:
            self.msgs.fail(exc, "pro_scan")
            return

        def work() -> int:
            return self.scan.next(cmp, value, self._progress.emit, self._cancel)

        self._start(work, "pro_scan")

    def do_new(self) -> None:
        self.scan.reset()
        self._clear()
        self._set_compares()

    def do_cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    # -- results ----------------------------------------------------------------------------
    def load_page(self, n: int) -> None:
        if not self.scan.active or n < 0:
            self._update_buttons()
            return

        def done(hits: list[ScanHit]) -> None:
            self.page, self.hits, self.aob_hits = n, hits, []
            self._show_hits()

        self.tasks.run(lambda: self.scan.page(n), done, self._page_failed)

    def _page_failed(self, exc: BaseException) -> None:
        self.msgs.fail(exc, "pro_scan")
        self._set_compares()

    def _decode(self, hit: ScanHit, raw: bytes | None) -> str:
        vt = hit.vtype or self.scan.vtype
        if raw is None or vt is None:
            return ""
        return vt.text(vt.decode(raw))

    def _show_hits(self) -> None:
        game = self.game
        self.table.setRowCount(len(self.hits))
        for k, h in enumerate(self.hits):
            cells = (
                place_text(h.address, game),
                self._decode(h, h.value),
                self._decode(h, h.previous),
                self._decode(h, h.first),
            )
            for col, text in enumerate(cells):
                self.table.setItem(k, col, QTableWidgetItem(text))
        self._update_buttons()

    def _add_rows(self, rows: list[int]) -> None:
        if self.aob_hits:
            if rows and rows[0] < len(self.aob_hits):
                self.show_code(self.aob_hits[rows[0]])
            return
        picked = [self.hits[r] for r in rows if r < len(self.hits)]
        for h in picked:
            self.add_address(h.address, h.vtype or self.scan.vtype or ValueType.INT32)
        if picked:
            self.msgs.say(hints.ADDED.format(n=len(picked)))

    def do_add(self) -> None:
        self._add_rows(sorted({i.row() for i in self.table.selectionModel().selectedRows()}))

    # -- AOB over the game's code ---------------------------------------------------------------
    def do_aob(self) -> None:
        text, session = self.aob.text(), self.ctl.session
        self.running_scan = True
        self._cancel = threading.Event()
        self._started = self.clock()
        self._update_buttons()

        def done(found: list[int]) -> None:
            self.running_scan = False
            self.hits, self.aob_hits = [], found
            game = self.game
            self.table.setRowCount(len(found))
            for k, addr in enumerate(found):
                self.table.setItem(k, COL_ADDR, QTableWidgetItem(place_text(addr, game)))
                for col in (COL_VALUE, COL_PREVIOUS, COL_FIRST):
                    self.table.setItem(k, col, QTableWidgetItem(""))
            self.count.setText(hints.AOB_FOUND.format(n=len(found)))
            self._update_buttons()

        def failed(exc: BaseException) -> None:
            self.running_scan = False
            self.msgs.fail(exc, "pro_aob")
            self._update_buttons()

        self.tasks.run(
            lambda: sc.find_aob(session, text, self._progress.emit, self._cancel), done, failed
        )

    def closing(self) -> None:
        self.do_cancel()
