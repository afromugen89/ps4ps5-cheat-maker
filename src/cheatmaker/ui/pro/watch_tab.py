"""The Pro Watch tab (spec PR-3): find what writes / accesses up to four addresses.

The watch runs on the Pro worker (``core/pro/watch_session.py``) on the debugger's own
connection, holding ``Session.claim_debugger`` so a guided watch can't start meanwhile
(and the other way round). Live counts come back through a signal. Stop, the time
and hit limits (settings ``pro_watch_seconds`` / ``pro_watch_hits``), the game going
away and closing the window all end it; the session always detaches, and the list
stays (AC-3.2). Registers are marked "unverified" until S-P1 passes (AC-3.3).
Below the list, ``AccessPanel`` answers "what does this instruction access".
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from cheatmaker.console.client import open_debugger
from cheatmaker.core.pro import watch_session as ws
from cheatmaker.core.pro.addresses import parse_place, place_text
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.access_panel import AccessPanel
from cheatmaker.ui.pro.cave_editor import mono
from cheatmaker.ui.pro.tab import JobGuard, ProMessages, ProTab
from cheatmaker.ui.tasks import TaskRunner

T_SLOT, T_ADDR, T_SIZE, T_KIND = range(4)
COL_WATCH, COL_ADDR, COL_TEXT, COL_COUNT, COL_FIRST, COL_LAST = range(6)


def result_headers() -> list[str]:
    tag = "" if ws.REGISTERS_VERIFIED else hints.REGS_UNVERIFIED
    return ["Watch", "Address", "Instruction", "Count", f"First hit{tag}", f"Last hit{tag}"]


class WatchTab(ProTab):
    _update = Signal(object)  # (rows, hits, elapsed) from the worker

    def __init__(self, ctl: Controller, tasks: TaskRunner, msgs: ProMessages | None = None) -> None:
        super().__init__(ctl, tasks, msgs)
        self.targets: list[ws.WatchTarget] = []
        self.rows: list[ws.InstructionRow] = []
        self.running_targets: list[ws.WatchTarget] = []
        self.clock: Callable[[], float] = time.monotonic  # tests use the debugger's
        self.make_entry: Callable[[str, int], None] = lambda kind, offset: None  # window
        self.show_code: Callable[[int], None] = lambda addr: None  # set by the window
        self._cancel: threading.Event | None = None
        self._guard = JobGuard(ctl, "watch")  # a disconnect detaches first (B1)
        self._seconds = 0.0

        self.hint = QLabel(hints.WATCH)
        self.hint.setWordWrap(True)
        self.place = QLineEdit()
        self.place.setPlaceholderText("0x20000040 or eboot.bin+0x4010")
        self.size = QComboBox()
        for n in ws.WATCH_SIZES:
            self.size.addItem(f"{n} byte(s)", n)
        self.size.setCurrentIndex(ws.WATCH_SIZES.index(4))
        self.size.setToolTip(hints.WATCH_SIZE_TIP)
        self.kind = QComboBox()
        for k in ws.WATCH_KINDS:
            self.kind.addItem(hints.WATCH_KINDS[k], k)
        self.add_btn = QPushButton("Add")
        self.remove_btn = QPushButton("Remove")
        self.target_table = QTableWidget(0, 4)
        self.target_table.setHorizontalHeaderLabels(["Slot", "Address", "Size", "Kind"])
        self.target_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.target_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.target_table.setMaximumHeight(140)
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.state = QLabel("")
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(result_headers())
        for col in (COL_FIRST, COL_LAST):
            self.table.horizontalHeaderItem(col).setToolTip(
                "" if ws.REGISTERS_VERIFIED else hints.REGS_UNVERIFIED_TIP
            )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setFont(mono())
        self.table.horizontalHeader().setSectionResizeMode(COL_TEXT, QHeaderView.ResizeMode.Stretch)
        self.code_btn = QPushButton(hints.SHOW_CODE)
        self.cave_btn = QPushButton("Make cave entry here")
        self.nop_btn = QPushButton("Make NOP entry here")
        self.access_btn = QPushButton(hints.ACCESS_BUTTON)
        self.access = AccessPanel(ctl, tasks, self.msgs)
        for b in (self.cave_btn, self.nop_btn):
            b.setToolTip(hints.MAKE_ENTRY_TIP)

        add_row = QHBoxLayout()
        add_row.addWidget(self.place, 1)
        for w in (self.size, self.kind, self.add_btn, self.remove_btn):
            add_row.addWidget(w)
        run_row = QHBoxLayout()
        for w in (self.start_btn, self.stop_btn):
            run_row.addWidget(w)
        run_row.addWidget(self.state, 1)
        act_row = QHBoxLayout()
        for w in (self.code_btn, self.cave_btn, self.nop_btn, self.access_btn):
            act_row.addWidget(w)
        act_row.addStretch(1)
        self.lay = QVBoxLayout(self.body)
        self.lay.addWidget(self.hint)
        self.lay.addLayout(add_row)
        self.lay.addWidget(self.target_table)
        self.lay.addLayout(run_row)
        self.lay.addWidget(self.table, 1)
        self.lay.addLayout(act_row)
        self.lay.addWidget(self.access, 1)

        self._update.connect(self._show_update)
        self.add_btn.clicked.connect(self.do_add)
        self.place.returnPressed.connect(self.do_add)
        self.remove_btn.clicked.connect(self.do_remove)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.code_btn.clicked.connect(self._show_code)
        self.cave_btn.clicked.connect(lambda: self._make("cave"))
        self.nop_btn.clicked.connect(lambda: self._make("nop"))
        self.access_btn.clicked.connect(self._sample_row)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.target_table.itemSelectionChanged.connect(self._update_buttons)
        self._update_buttons()

    # -- the watch list ---------------------------------------------------------------------
    @property
    def watching(self) -> bool:
        return self._cancel is not None

    def add_target(self, target: ws.WatchTarget) -> bool:
        """Add one watch (one per address and kind). False when all slots are taken."""
        if target in self.targets:
            return True
        if len(self.targets) >= ws.MAX_WATCH_SLOTS:
            self.msgs.say(hints.WATCH_FULL)
            return False
        self.targets.append(target)
        self._fill_targets()
        return True

    def do_add(self) -> None:
        try:
            addr = parse_place(self.place.text(), self.ctl.session.require_game())
            target = ws.WatchTarget(addr, self.size.currentData(), self.kind.currentData())
        except Exception as exc:
            self.msgs.fail(exc, "pro_watch")
            return
        if self.add_target(target):
            self.place.clear()

    def do_remove(self) -> None:
        picked = sorted({i.row() for i in self.target_table.selectionModel().selectedRows()})
        for k in reversed(picked):
            if k < len(self.targets):
                del self.targets[k]
        self._fill_targets()

    def watch(self, targets: list[ws.WatchTarget]) -> None:
        """From other tabs: watch these (replacing the list) and start at once."""
        if self.watching:
            self.msgs.say(hints.WATCH_BUSY)
            return
        self.targets = list(targets[: ws.MAX_WATCH_SLOTS])
        self._fill_targets()
        self.start()

    def _fill_targets(self) -> None:
        game = self.game
        self.target_table.setRowCount(len(self.targets))
        for k, t in enumerate(self.targets):
            for col, text in (
                (T_SLOT, str(k)),
                (T_ADDR, place_text(t.addr, game)),
                (T_SIZE, str(t.size)),
                (T_KIND, hints.WATCH_KINDS[t.kind]),
            ):
                self.target_table.setItem(k, col, QTableWidgetItem(text))
        self._update_buttons()

    # -- running ------------------------------------------------------------------------------
    def start(self) -> None:
        if self.watching or not self.running:
            return
        if not self.targets:
            self.msgs.say(hints.WATCH_NONE)
            return
        session = self.ctl.session
        settings = self.ctl.settings
        try:
            game = session.require_game()
            dbg = open_debugger(session.require_client(), game.pid)
        except Exception as exc:
            self.msgs.fail(exc, "pro_watch")
            return
        client = session.require_client()
        cancel = threading.Event()
        self._cancel = cancel
        self.running_targets = list(self.targets)
        self._seconds = float(settings.pro_watch_seconds)
        self.rows = []
        self._fill_rows()
        self.state.setText(hints.WATCH_STARTING)
        watch = ws.WatchSession(
            dbg,
            lambda a, n: client.read(game.pid, a, n),
            self.running_targets,
            seconds=self._seconds,
            max_hits=settings.pro_watch_hits,
            clock=self.clock,
            on_update=lambda rows, hits, t: self._update.emit((rows, hits, t)),
        )

        def work() -> ws.WatchResult:
            with session.claim_debugger(hints.WATCH_OWNER):
                return watch.run(cancel)

        self._update_buttons()
        self.tasks.run(self._guard.start(cancel, work), self._ended, self._failed)

    def stop(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _show_update(self, update: tuple) -> None:
        rows, hits, elapsed = update
        if not self.watching:
            return  # a late update after the end
        self.rows = rows
        self._fill_rows()
        left = max(0, round(self._seconds - elapsed))
        self.state.setText(hints.WATCH_RUNNING.format(hits=hits, left=left))

    def _ended(self, result: ws.WatchResult) -> None:
        self._cancel = None
        self._guard.finish()
        self.rows = result.rows
        self._fill_rows()
        text = hints.WATCH_ENDED[result.ended].format(hits=result.hits, reason=result.message)
        self.state.setText(text)
        self.msgs.say(text)
        self._update_buttons()

    def _failed(self, exc: BaseException) -> None:
        self._cancel = None
        self._guard.finish()
        self.state.setText("")
        self.msgs.fail(exc, "pro_watch")
        self._update_buttons()

    # -- results --------------------------------------------------------------------------------
    def _fill_rows(self) -> None:
        game = self.game
        self.table.setRowCount(len(self.rows))
        for k, r in enumerate(self.rows):
            watch = "?"
            if r.slot is not None and r.slot < len(self.running_targets):
                t = self.running_targets[r.slot]
                watch = f"{hints.WATCH_KINDS[t.kind]} {place_text(t.addr, game)}"
            for col, text in (
                (COL_WATCH, watch),
                (COL_ADDR, place_text(r.address, game)),
                (COL_TEXT, r.text),
                (COL_COUNT, str(r.count)),
                (COL_FIRST, ws.registers_text(r.first)),
                (COL_LAST, ws.registers_text(r.last)),
            ):
                self.table.setItem(k, col, QTableWidgetItem(text))
        self._update_buttons()

    def selected_row(self) -> ws.InstructionRow | None:
        k = self.table.currentRow()
        picked = self.table.selectionModel().selectedRows()
        return self.rows[k] if picked and 0 <= k < len(self.rows) else None

    def _show_code(self) -> None:
        row = self.selected_row()
        if row is None:
            self.msgs.say(hints.WATCH_PICK_ROW)
            return
        self.show_code(row.address)

    def _sample_row(self) -> None:
        row = self.selected_row()
        if row is None or row.text == ws.UNKNOWN_TEXT:
            self.msgs.say(hints.WATCH_PICK_ROW)
            return
        self.access.sample_at(row.address)

    def _make(self, kind: str) -> None:
        row, game = self.selected_row(), self.game
        if row is None or row.text == ws.UNKNOWN_TEXT:
            self.msgs.say(hints.WATCH_PICK_ROW)
            return
        if game is None or not game.module_base <= row.address < game.module_end:
            self.msgs.say(hints.WATCH_NOT_MODULE)
            return
        self.make_entry(kind, row.address - game.module_base)

    def _update_buttons(self) -> None:
        idle = not self.watching
        self.add_btn.setEnabled(idle and len(self.targets) < ws.MAX_WATCH_SLOTS)
        self.remove_btn.setEnabled(idle and bool(self.target_table.selectionModel().selectedRows()))
        self.start_btn.setEnabled(idle and self.running and bool(self.targets))
        self.stop_btn.setEnabled(not idle)
        picked = self.selected_row() is not None
        for b in (self.code_btn, self.cave_btn, self.nop_btn, self.access_btn):
            b.setEnabled(picked)

    # -- game state -------------------------------------------------------------------------------
    def game_state_changed(self, new_game: bool) -> None:
        self.access.running = self.running
        if self.access.sampling and (new_game or not self.running):
            self.access.stop()
        self.access.update_buttons()
        if self.watching and (new_game or not self.running):  # AC-1.2: the game went away
            self.stop()
            self.msgs.say(hints.WATCH_GAME_GONE)
        if new_game and not self.watching:
            self.targets = []
            self.rows = []
            self._fill_targets()
            self._fill_rows()
        self._update_buttons()

    def closing(self) -> None:
        self.stop()
        self.access.stop()
