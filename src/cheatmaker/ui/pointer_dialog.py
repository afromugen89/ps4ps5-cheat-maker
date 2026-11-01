"""Pointer scan window: find chains to a value, re-check them after a restart, use one."""

from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from cheatmaker.core import pointers as ptr
from cheatmaker.core.values import ValueType
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.tasks import _Signals

SHOW_CHAINS = 200


class PointerDialog(QDialog):
    def __init__(
        self,
        ctl: Controller,
        target: int | None,
        vtype: ValueType,
        on_use: Callable[[ptr.Chain, ValueType], None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pointer scan")
        self.ctl, self.target, self.vtype, self.on_use = ctl, target, vtype, on_use
        game = ctl.session.require_game()
        self.path = ptr.chains_path(game.title_id, game.version)
        self.chains: list[ptr.Chain] = ptr.load_chains(self.path)
        self._cancel: threading.Event | None = None
        self._progress = _Signals()
        self._progress.done.connect(lambda f: self.progress.setValue(int(f * 100)))

        where = f"{target:#x}" if target is not None else "no address picked"
        self.hint = QLabel(hints.POINTER_START if target is not None else hints.POINTER_NO_TARGET)
        self.hint.setWordWrap(True)
        self.depth = QSpinBox()
        self.depth.setRange(1, 5)
        self.depth.setValue(ptr.DEFAULT_DEPTH)
        self.offset = QComboBox()
        for off in (0x400, 0x1000, 0x2000, 0x4000):
            self.offset.addItem(f"{off:#x}", off)
        self.offset.setCurrentIndex(1)
        self.scan_btn = QPushButton("Find pointers")
        self.recheck_btn = QPushButton("Re-check with this address")
        self.cancel_btn = QPushButton("Cancel")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.list = QListWidget()
        self.count = QLabel("")
        self.use_btn = QPushButton("Use this pointer")
        self.close_btn = QPushButton("Close")

        opts = QHBoxLayout()
        opts.addWidget(QLabel(f"Value at {where}"), 1)
        opts.addWidget(QLabel("Levels:"))
        opts.addWidget(self.depth)
        opts.addWidget(QLabel("Max offset:"))
        opts.addWidget(self.offset)
        run = QHBoxLayout()
        for w in (self.scan_btn, self.recheck_btn):
            run.addWidget(w)
        run.addWidget(self.progress, 1)
        run.addWidget(self.cancel_btn)
        bottom = QHBoxLayout()
        bottom.addWidget(self.count, 1)
        bottom.addWidget(self.use_btn)
        bottom.addWidget(self.close_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addLayout(opts)
        lay.addLayout(run)
        lay.addWidget(self.list, 1)
        lay.addLayout(bottom)

        self.scan_btn.clicked.connect(self.do_scan)
        self.recheck_btn.clicked.connect(self.do_recheck)
        self.cancel_btn.clicked.connect(self.do_cancel)
        self.use_btn.clicked.connect(self.do_use)
        self.close_btn.clicked.connect(self.accept)
        self.list.currentRowChanged.connect(lambda _r: self._update())
        self._fill()

    # -- state ----------------------------------------------------------------
    def _update(self) -> None:
        busy = self._cancel is not None
        has_target = self.target is not None
        self.scan_btn.setEnabled(has_target and not busy)
        self.recheck_btn.setEnabled(has_target and bool(self.chains) and not busy)
        self.cancel_btn.setEnabled(busy)
        self.use_btn.setEnabled(not busy and 0 <= self.list.currentRow() < len(self.chains))

    def _fill(self) -> None:
        self.list.clear()
        for c in self.chains[:SHOW_CHAINS]:
            self.list.addItem(c.label())
        more = f" (showing {SHOW_CHAINS})" if len(self.chains) > SHOW_CHAINS else ""
        self.count.setText(f"{len(self.chains):,} pointers{more}" if self.chains else "")
        if self.chains:
            self.list.setCurrentRow(0)
        self._update()

    def _run(self, fn, done) -> None:
        self._cancel = threading.Event()
        cancel = self._cancel
        self.progress.setValue(0)
        self._update()
        self.ctl.tasks.run(lambda: fn(self._progress.done.emit, cancel), done, self._failed)

    def _failed(self, exc: BaseException) -> None:
        self._cancel = None
        self._update()
        self.ctl.fail(exc, "pointer_scan")

    def _save(self) -> None:
        game = self.ctl.session.require_game()
        ptr.save_chains(self.path, game.title_id, game.version, self.chains)

    # -- actions --------------------------------------------------------------
    def do_scan(self) -> None:
        if self.target is None:
            return
        target, depth, off = self.target, self.depth.value(), self.offset.currentData()
        self.hint.setText(hints.POINTER_RUNNING)
        self._run(
            lambda p, c: ptr.scan_session(self.ctl.session, target, depth, off, p, c),
            self._scanned,
        )

    def _scanned(self, chains: list[ptr.Chain]) -> None:
        self._cancel = None
        self.progress.setValue(100)
        self.chains = chains
        self._save()
        self.hint.setText(hints.POINTER_FOUND if chains else hints.POINTER_NONE)
        self._fill()

    def do_recheck(self) -> None:
        if self.target is None or not self.chains:
            return
        target, chains = self.target, list(self.chains)
        self._run(
            lambda _p, _c: ptr.recheck_session(self.ctl.session, chains, target),
            self._rechecked,
        )

    def _rechecked(self, kept: list[ptr.Chain]) -> None:
        self._cancel = None
        self.progress.setValue(100)
        before = len(self.chains)
        self.chains = kept
        self._save()
        self.hint.setText(hints.POINTER_RECHECKED.format(kept=len(kept), before=before))
        self._fill()

    def do_cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def do_use(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.chains):
            self.on_use(self.chains[row], self.vtype)
            self.accept()
