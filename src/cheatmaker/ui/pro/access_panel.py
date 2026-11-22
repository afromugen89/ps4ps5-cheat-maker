""" "What does this instruction access" (spec PR-3, AC-3.4/3.5), below the Watch list.

The sample runs on the Pro worker (``core/pro/access_sampler.py``): the game never
stops, and the hook, the table and the sampler cave are cleared at the end. With no
room for the sampler the panel offers an execute watch on the instruction instead
(the debugger pauses the game at each pass; addresses come from the registers).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cheatmaker.console.client import StopEvent, open_debugger
from cheatmaker.core.pro import access_sampler as sm
from cheatmaker.core.pro import sites
from cheatmaker.core.pro import watch_session as ws
from cheatmaker.core.pro.addresses import parse_place, place_text
from cheatmaker.core.pro.compile import ProWriter
from cheatmaker.core.values import ValueType
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.cave_editor import mono
from cheatmaker.ui.pro.tab import ProMessages
from cheatmaker.ui.tasks import TaskRunner

COL_ADDR, COL_OBJECT, COL_COUNT = range(3)


class AccessPanel(QWidget):
    _update = Signal(object)  # (rows, polls) from the worker

    def __init__(self, ctl: Controller, tasks: TaskRunner, msgs: ProMessages) -> None:
        super().__init__()
        self.ctl, self.tasks, self.msgs = ctl, tasks, msgs
        self.seconds = float(sm.SAMPLE_SECONDS)
        self.sleep: Callable[[float], None] = time.sleep  # tests: no waiting
        self.clock: Callable[[], float] = time.monotonic  # the fallback watch's clock
        self.add_address: Callable[[int, ValueType], None] = lambda a, t: None  # window
        self.ask_watch: Callable[[str], bool] = self._ask_watch
        self.rows: list[sm.AccessRow] = []
        self.running = False  # the game runs (set by the Watch tab)
        self._cancel: threading.Event | None = None

        self.hint = QLabel(hints.ACCESS.format(seconds=int(self.seconds)))
        self.hint.setWordWrap(True)
        self.place = QLineEdit()
        self.place.setPlaceholderText("eboot.bin+0x1234")
        self.run_btn = QPushButton("Run")
        self.stop_btn = QPushButton("Stop")
        self.state = QLabel("")
        self.state.setWordWrap(True)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Address", "Object (base register)", "Seen"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setFont(mono())
        self.add_btn = QPushButton(hints.ACCESS_ADD)

        row = QHBoxLayout()
        row.addWidget(self.place, 1)
        row.addWidget(self.run_btn)
        row.addWidget(self.stop_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.hint)
        lay.addLayout(row)
        lay.addWidget(self.state)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.add_btn)

        self._update.connect(self._show_update)
        self.run_btn.clicked.connect(self.do_run)
        self.place.returnPressed.connect(self.do_run)
        self.stop_btn.clicked.connect(self.stop)
        self.add_btn.clicked.connect(self._add_picked)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.update_buttons()

    @property
    def sampling(self) -> bool:
        return self._cancel is not None

    def _ask_watch(self, reason: str) -> bool:
        answer = QMessageBox.question(
            self, hints.ACCESS_WATCH_TITLE, hints.ACCESS_WATCH_ASK.format(reason=reason)
        )
        return answer == QMessageBox.StandardButton.Yes

    # -- running ------------------------------------------------------------------------------
    def do_run(self) -> None:
        try:
            addr = parse_place(self.place.text(), self.ctl.session.require_game())
        except Exception as exc:
            self.msgs.fail(exc, "pro_sample")
            return
        self.sample_at(addr)

    def sample_at(self, addr: int) -> None:
        """Sample the instruction at ``addr`` (from the Code view, a Watch row or typed)."""
        if self.sampling:
            self.msgs.say(hints.ACCESS_BUSY)
            return
        session = self.ctl.session
        game = session.game
        self.place.setText(place_text(addr, game))
        cheats = self.ctl.cheat_file.cheats if self.ctl.cheat_file else []
        reserved = [
            *sites.cave_rooms(c.pro for c in cheats if c.pro is not None),
            *sites.open_rooms(cheats),
        ]
        enabled = [c for c in cheats if c.enabled]
        writer = ProWriter(session, warn=self.msgs.say)
        cancel = self._start()

        def work() -> sm.SampleResult:
            return sm.sample(
                session,
                addr,
                writer,
                seconds=self.seconds,
                cancel=cancel,
                sleep=self.sleep,
                reserved=reserved,
                enabled=enabled,
                on_update=lambda rows, polls: self._update.emit((rows, polls)),
            )

        self.tasks.run(work, self._sampled, lambda exc: self._failed(exc, addr))

    def _start(self) -> threading.Event:
        self._cancel = threading.Event()
        self.rows = []
        self._fill()
        self.state.setText(hints.ACCESS_RUNNING.format(n=0))
        self.update_buttons()
        return self._cancel

    def stop(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _show_update(self, update: tuple) -> None:
        rows, _polls = update
        if not self.sampling:
            return
        self.rows = rows
        self._fill()
        self.state.setText(hints.ACCESS_RUNNING.format(n=len(rows)))

    def _sampled(self, result: sm.SampleResult) -> None:
        self._cancel = None
        self.rows = result.rows
        self._fill()
        n = len(result.rows)
        text = (
            hints.ACCESS_STOPPED.format(n=n)
            if result.cancelled
            else hints.ACCESS_DONE.format(n=n, polls=result.polls)
        )
        self.state.setText(text)
        self.msgs.say(text)
        self.update_buttons()

    def _failed(self, exc: BaseException, addr: int) -> None:
        self._cancel = None
        self.state.setText("")
        self.update_buttons()
        if isinstance(exc, sm.NoRoomForSampler):
            self.msgs.say(str(exc))
            if self.ask_watch(str(exc)):
                self.watch_instead(addr)
            return
        self.msgs.fail(exc, "pro_sample")

    def watch_instead(self, addr: int) -> None:
        """AC-3.5: an execute watch on the instruction; addresses from the registers."""
        session = self.ctl.session
        settings = self.ctl.settings
        try:
            game = session.require_game()
            client = session.require_client()
            operand = sm.operand_at(client.read(game.pid, addr, 15), addr)
            dbg = open_debugger(client, game.pid)
        except Exception as exc:
            self.msgs.fail(exc, "pro_sample")
            return
        hits: list = []
        cancel = self._start()
        watch = ws.WatchSession(
            dbg,
            lambda a, n: client.read(game.pid, a, n),
            [ws.WatchTarget(addr, 1, "execute")],
            seconds=self.seconds,
            max_hits=settings.pro_watch_hits,
            clock=self.clock,
            on_hit=lambda stop: self._watched_hit(stop, hits, operand),
        )

        def work() -> ws.WatchResult:
            with session.claim_debugger(hints.WATCH_OWNER):
                return watch.run(cancel)

        def done(result: ws.WatchResult) -> None:
            self._cancel = None
            self.rows = sm.accesses_from_hits([s.hit for s in hits], operand)
            self._fill()
            text = hints.ACCESS_WATCHED.format(n=len(self.rows), hits=result.hits)
            if result.ended == ws.LOST:
                text = hints.WATCH_ENDED[ws.LOST].format(hits=result.hits, reason=result.message)
            self.state.setText(text)
            self.msgs.say(text)
            self.update_buttons()

        self.tasks.run(work, done, lambda exc: self._failed(exc, addr))

    def _watched_hit(self, stop: StopEvent, hits: list, operand: sm.Operand) -> None:
        hits.append(stop)
        rows = sm.accesses_from_hits([s.hit for s in hits], operand)
        self._update.emit((rows, len(hits)))

    # -- results -------------------------------------------------------------------------------
    def _fill(self) -> None:
        game = self.ctl.session.game
        self.table.setRowCount(len(self.rows))
        for k, r in enumerate(self.rows):
            for col, text in (
                (COL_ADDR, place_text(r.address, game)),
                (COL_OBJECT, f"{r.object:#x}"),
                (COL_COUNT, str(r.count)),
            ):
                self.table.setItem(k, col, QTableWidgetItem(text))
        self.update_buttons()

    def _add_picked(self) -> None:
        picked = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        for k in picked:
            if k < len(self.rows):
                self.add_address(self.rows[k].address, ValueType.INT32)
        if picked:
            self.msgs.say(hints.ADDED.format(n=len(picked)))

    def update_buttons(self) -> None:
        idle = not self.sampling
        self.run_btn.setEnabled(idle and self.running)
        self.place.setEnabled(idle)
        self.stop_btn.setEnabled(not idle)
        self.add_btn.setEnabled(bool(self.table.selectionModel().selectedRows()))
