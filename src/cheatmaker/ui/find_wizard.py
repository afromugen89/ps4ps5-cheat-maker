"""Find a value (AC-3), set / freeze it (AC-4), make a cheat from it (AC-5)."""

from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cheatmaker.core import pointers as ptr
from cheatmaker.core.pointers import Chain
from cheatmaker.core.scanner import MAX_SHOW, Match
from cheatmaker.core.templates import TEMPLATES, make_cheat
from cheatmaker.core.values import Compare, ValueType, parse_number
from cheatmaker.errors import ReadFailed
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pointer_dialog import PointerDialog
from cheatmaker.ui.tasks import _Signals

log = logging.getLogger(__name__)

LIVE_MS = 500


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


class FindWizard(QWidget):
    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.unknown_mode = False
        self.rows: list[Match] = []
        self.row_types: list[ValueType] = []
        self.manual: dict[int, ValueType] = {}  # addresses the player typed in
        self.pointers: dict[Chain, ValueType] = {}  # chains followed on every refresh
        self.row_chains: list[Chain | None] = []
        self._cancel: threading.Event | None = None
        self._progress = _Signals()
        self._progress.done.connect(self._on_progress)
        self._started = 0.0

        self.hint = QLabel(hints.FIND_START)
        self.hint.setWordWrap(True)
        self.number = QLineEdit()
        self.number.setPlaceholderText("The number you see, e.g. 2450")
        self.search_btn = QPushButton("Search")
        self.unknown_btn = QPushButton("I don't know the number")
        self.new_btn = QPushButton("Start over")
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
        adv = QHBoxLayout(self.advanced)
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
        self.advanced.toggled.connect(lambda _on: self._fill_table())

        self.add_addr = QLineEdit()
        self.add_addr.setPlaceholderText("Address, e.g. 0x12e0ea60ac")
        self.add_type = QComboBox()
        for vt in ValueType:
            self.add_type.addItem(vt.label, vt)
        self.add_type.setCurrentIndex(list(ValueType).index(ValueType.FLOAT))
        self.add_btn = QPushButton("Add address")
        self.remove_btn = QPushButton("Remove")
        self.pointer_btn = QPushButton("Pointer scan")

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Value now", "Address"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.set_value = QLineEdit()
        self.set_value.setPlaceholderText("New value")
        self.set_btn = QPushButton("Set")
        self.freeze_btn = QPushButton("Freeze")
        self.template = QComboBox()
        for t in TEMPLATES:
            self.template.addItem(t.name, t)
        self.make_btn = QPushButton("Make a cheat")

        top = QHBoxLayout()
        for w in (self.number, self.search_btn, self.unknown_btn, self.new_btn):
            top.addWidget(w)
        cmp_row = QHBoxLayout()
        cmp_row.addWidget(self.count, 1)
        cmp_row.addWidget(QLabel("Show up to"))
        cmp_row.addWidget(self.show_n)
        for w in (self.up_btn, self.down_btn, self.changed_btn, self.same_btn):
            cmp_row.addWidget(w)
        prog = QHBoxLayout()
        prog.addWidget(self.progress, 1)
        prog.addWidget(self.cancel_btn)
        add_row = QHBoxLayout()
        add_row.addWidget(self.add_addr, 1)
        for w in (self.add_type, self.add_btn, self.remove_btn, self.pointer_btn):
            add_row.addWidget(w)
        act = QHBoxLayout()
        for w in (self.set_value, self.set_btn, self.freeze_btn, self.template, self.make_btn):
            act.addWidget(w)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addLayout(top)
        lay.addLayout(cmp_row)
        lay.addLayout(prog)
        lay.addLayout(add_row)
        lay.addWidget(self.table, 1)
        lay.addLayout(act)
        lay.addWidget(self.advanced)

        self.search_btn.clicked.connect(self.do_search)
        self.number.returnPressed.connect(self.do_search)
        self.unknown_btn.clicked.connect(self.do_unknown)
        self.new_btn.clicked.connect(self.do_reset)
        self.up_btn.clicked.connect(lambda: self.do_compare(Compare.INCREASED))
        self.down_btn.clicked.connect(lambda: self.do_compare(Compare.DECREASED))
        self.changed_btn.clicked.connect(lambda: self.do_compare(Compare.CHANGED))
        self.same_btn.clicked.connect(lambda: self.do_compare(Compare.UNCHANGED))
        self.cancel_btn.clicked.connect(self.do_cancel)
        self.set_btn.clicked.connect(self.do_set)
        self.freeze_btn.clicked.connect(self.do_freeze)
        self.make_btn.clicked.connect(self.do_make)
        self.add_btn.clicked.connect(self.do_add_address)
        self.add_addr.returnPressed.connect(self.do_add_address)
        self.remove_btn.clicked.connect(self.do_remove_address)
        self.pointer_btn.clicked.connect(self.do_pointer_scan)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.show_n.valueChanged.connect(self._set_show_limit)

        self.timer = QTimer(self)
        self.timer.setInterval(LIVE_MS)
        self.timer.timeout.connect(self.refresh_live)
        ctl.game_changed.connect(self._new_game)
        self._game_key: tuple[str, str] | None = None
        ctl.connection_changed.connect(lambda _up: self._update_buttons())
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
        self.new_btn.setEnabled(ready and active)
        for b in (self.up_btn, self.down_btn, self.changed_btn, self.same_btn):
            b.setEnabled(ready and active)
        self.cancel_btn.setEnabled(busy)
        sel = ready and self.selected() is not None and self.selected().address != 0
        for w in (self.set_btn, self.freeze_btn, self.make_btn):
            w.setEnabled(sel)
        self.add_btn.setEnabled(ready)
        m = self.selected()
        i = self._selected_row()
        is_ptr = i is not None and i < len(self.row_chains) and self.row_chains[i] is not None
        self.remove_btn.setEnabled(ready and m is not None and (m.address in self.manual or is_ptr))
        self.pointer_btn.setEnabled(ready)
        self.freeze_btn.setText(
            "Unfreeze" if m and m.address in self.ctl.frozen_addresses else "Freeze"
        )

    def _selected_row(self) -> int | None:
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
        self.count.setText(f"{count:,} matches")
        if count == 0:
            self.hint.setText(hints.FIND_NONE)
        elif sc.few_left:
            self.hint.setText(hints.FIND_FEW)
        else:
            self.hint.setText(hints.FIND_UNKNOWN if self.unknown_mode else hints.FIND_NARROW)
        self.refresh_live()
        self._update_buttons()

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
            value = parse_number(self.number.text())
        except ValueError as exc:
            self.ctl.fail(exc, "scan")
            return
        sc = self.ctl.scanner
        if sc.active:
            self._run_scan(lambda p, c: sc.next_scan(Compare.EXACT, value, p, c))
        else:
            self.unknown_mode = False
            vt = self.vtype.currentData() if self.advanced.isChecked() else None
            self._run_scan(lambda p, c: sc.first_scan(value, vt, p, c))

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
        except ValueError as exc:
            self.ctl.fail(exc, "scan")
            return
        self.unknown_mode = True
        vt = self.vtype.currentData() if self.advanced.isChecked() else None
        self._run_scan(lambda p, c: self.ctl.scanner.first_scan(None, vt, p, c, value_range=rng))

    def do_compare(self, cmp: Compare) -> None:
        self._run_scan(lambda p, c: self.ctl.scanner.next_scan(cmp, None, p, c))

    def do_cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def do_reset(self) -> None:
        try:
            self.ctl.scanner.reset()
        except Exception as exc:
            self.ctl.fail(exc, "scan")
        self.unknown_mode = False
        self.rows = []
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
        show_matches = sc.few_left and self._ready()
        if not (show_matches or ((self.manual or self.pointers) and self._ready())):
            self.timer.stop()
            self.rows, self.row_types, self.row_chains = [], [], []
            self._fill_table()
            return
        try:
            rows = sc.matches() if show_matches else []
            types = [self._vtype()] * len(rows) if rows else []
            seen = {m.address for m in rows}
            chains: list[Chain | None] = [None] * len(rows)
            for addr, vt in self.manual.items():
                if addr not in seen:
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
        self._fill_table()
        # Big lists refresh less often (each separate read costs ~36 ms on the PS5).
        self.timer.setInterval(max(LIVE_MS, 40 * len(self.rows)))
        if not self.timer.isActive():
            self.timer.start()

    def _set_show_limit(self, n: int) -> None:
        self.ctl.scanner.show_limit = n
        self.ctl.settings.show_matches = n
        try:
            self.ctl.settings.save()
        except OSError as exc:
            log.warning("action=save_settings reason=%s", exc)
        if self.ctl.scanner.active:
            self._after_scan(self.ctl.scanner.count or 0)

    def _fill_table(self) -> None:
        keep = self.table.selectionModel().selectedRows()
        keep_row = keep[0].row() if keep else None
        self.table.setColumnHidden(1, not self.advanced.isChecked())
        self.table.setRowCount(len(self.rows))
        for i, m in enumerate(self.rows):
            frozen = " (frozen)" if m.address in self.ctl.frozen_addresses else ""
            if m.address in self.manual:
                frozen += f" (added, {self.manual[m.address].label})"
            chain = self.row_chains[i] if i < len(self.row_chains) else None
            if chain is not None:
                frozen += f" (pointer {chain.label()})"
                if m.address == 0:
                    frozen = f" (pointer {chain.label()} leads nowhere right now)"
            self.table.setItem(i, 0, QTableWidgetItem(fmt_value(m.value) + frozen))
            self.table.setItem(i, 1, QTableWidgetItem(f"{m.address:#x}"))
        if keep_row is not None and keep_row < len(self.rows):
            self.table.selectRow(keep_row)
        self._update_buttons()

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

    def _new_game(self, game=None) -> None:
        self.manual.clear()  # addresses belong to the old game's memory
        key = (game.title_id, game.version) if game is not None else None
        if key != self._game_key:
            self.pointers.clear()  # pointers last across restarts of the same version
        self._game_key = key
        self.do_reset()

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
        self.add_addr.clear()
        self.refresh_live()
        self.table.selectRow(next(i for i, m in enumerate(self.rows) if m.address == addr))

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
        self.table.selectRow(len(self.rows) - 1)

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
        self.refresh_live()

    def do_set(self) -> None:
        m = self.selected()
        if m is None:
            return
        try:
            data = self._row_type().encode(parse_number(self.set_value.text()))
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
        self._fill_table()

    def do_make(self) -> None:
        m = self.selected()
        if m is None:
            return
        template = self.template.currentData()
        try:
            kind, offset = self.ctl.session.classify(m.address)
            value = parse_number(self.set_value.text()) if self.set_value.text().strip() else None
            cheat = make_cheat(template, kind, offset, self._row_type(), m.raw, value)
        except Exception as exc:
            self.ctl.fail(exc, "make_cheat")
            return
        self.ctl.ensure_file().cheats.append(cheat)
        self.ctl.cheats_changed.emit()
        self.ctl.message.emit(f'Made "{cheat.name}". Tick it to turn it on.')
