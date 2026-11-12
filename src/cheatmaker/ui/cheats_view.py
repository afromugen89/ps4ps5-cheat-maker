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
from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import builder as bd
from cheatmaker.core import carry as cr
from cheatmaker.core import cave as cv
from cheatmaker.core import project as pj
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import Cheat, Location, LocationKind, combine, versions_differ
from cheatmaker.core.values import parse_number
from cheatmaker.errors import CheatMakerError, ProjectFileError
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.find_again import FindAgain, LearnDialog
from cheatmaker.ui.find_wizard import fmt_value

log = logging.getLogger(__name__)

COL_ON, COL_NAME, COL_VALUE, COL_KEEPS, COL_DETAILS = range(5)
COMBINE_TIP = "Ctrl-click two or more cheats, then press Combine to make them one cheat"
FOUND_AGAIN = "Found again each run"
CHECK_AFTER_RESTART = "Check after restart"


def keeps_text(c: Cheat) -> str:
    if c.carried == "test":  # brought-over code parts, not ticked yet (spec AC-U8)
        return hints.KEEPS_TEST
    if c.carried == "lost":  # its value came over with no way to be found
        return hints.KEEPS_LOST
    if c.found_again:  # one found-again value, or a combined cheat with nothing moving
        return hints.KEEPS_CHECK if c.carried == "check" else FOUND_AGAIN
    if c.check_after_restart:  # Make it work, Only me by a field (spec-cheat-builder B8)
        return CHECK_AFTER_RESTART
    return "This session only" if c.session_only else "Yes"


def single_value(c: Cheat | None) -> bool:
    """One value at one address (not a code patch, not found again each run): what
    Make it work can start from."""
    return (
        c is not None
        and c.anchor is None
        and len(c.locations) == 1
        and c.locations[0].kind is not LocationKind.CODE_PATCH
    )


def details(c: Cheat) -> str:
    return "; ".join(_line_details(loc) for loc in c.locations)


def _line_details(loc: Location) -> str:
    bytes_ = f"{loc.off.hex(' ').upper()} -> {loc.on.hex(' ').upper()}"
    if loc.anchor is not None:  # a learned part: its offset is unused
        return f"{loc.part}: found again {loc.anchor[0]}/{loc.anchor[1]}: {bytes_}"
    where = f"{loc.kind.value} +{loc.offset:X}"
    return f"{loc.part}: {where}: {bytes_}" if loc.part else f"{where}: {bytes_}"


class CheatsView(QWidget):
    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.open_lock: Callable[[Cheat], None] = lambda c: None
        self.open_build: Callable[..., None] = lambda **kw: None
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
        # Ctrl/Shift-click picks several cheats, for Combine and Delete.
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.show_details = QCheckBox("Advanced: show details")
        self.delete_btn = QPushButton("Delete")
        self.lock_btn = QPushButton("Lock it")
        self.build_btn = QPushButton(hints.BUILD_BUTTON)
        self.combine_btn = QPushButton("Combine")
        self.combine_btn.setToolTip(COMBINE_TIP)
        self.learn_btn = QPushButton("Find it next time")
        self.learn_btn.setToolTip(
            "Learn how the game keeps this value, so Cheat Maker finds it again by itself "
            "after a restart or a new area"
        )
        self.again_btn = QPushButton("Find it again")
        self.again_btn.setToolTip("Look for your remembered values in the game now")
        self.skip_hook = QCheckBox("Find again without the game's code (slower, for testing)")
        self.skip_hook.setVisible(False)  # Advanced only (console test C10e)
        self.bring_btn = QPushButton(hints.ADVANCED_BRING)
        self.bring_btn.setVisible(False)  # Advanced only (spec-game-update AC-U1)
        self.open_btn = QPushButton("Open cheat file...")
        self.save_btn = QPushButton("Save...")
        self.update_btn = QPushButton("Update for this version")
        self.update_btn.setToolTip(
            "Find these cheats in the game version you're running and move them there"
        )
        self.update_btn.setVisible(False)
        self._learning = False
        self._updating = False  # Update for this version is running
        self._learn_tried: set[tuple[str, str, int]] = set()
        buttons = QHBoxLayout()
        for w in (
            self.delete_btn,
            self.combine_btn,
            self.lock_btn,
            self.build_btn,
            self.learn_btn,
            self.again_btn,
            self.open_btn,
            self.save_btn,
        ):
            buttons.addWidget(w)
        buttons.addStretch(1)
        buttons.addWidget(self.skip_hook)
        buttons.addWidget(self.bring_btn)
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
        self.show_details.toggled.connect(self.skip_hook.setVisible)
        self.show_details.toggled.connect(self.bring_btn.setVisible)
        self.delete_btn.clicked.connect(self.do_delete)
        self.combine_btn.clicked.connect(self.do_combine)
        self.lock_btn.clicked.connect(lambda: self._with_selected(self.open_lock))
        self.build_btn.clicked.connect(lambda: self._with_selected(self.do_build))
        self.learn_btn.clicked.connect(lambda: self._with_selected(self.open_learn))
        self.again_btn.clicked.connect(
            lambda: self.finder.start(use_hook=not self.skip_hook.isChecked())
        )
        self.open_btn.clicked.connect(self.do_open)
        self.save_btn.clicked.connect(lambda: self.open_save())
        self.update_btn.clicked.connect(self.do_update)
        self.finder.carrying_changed.connect(lambda _on: self._update_buttons())
        ctl.cheats_changed.connect(self.fill)
        ctl.cheats_changed.connect(self._maybe_learn)
        ctl.game_changed.connect(lambda _g: self._check_version())
        ctl.connection_changed.connect(lambda _up: self._check_version())
        self.fill()

    @property
    def cheats(self) -> list[Cheat]:
        return self.ctl.cheat_file.cheats if self.ctl.cheat_file else []

    def selected_all(self) -> list[Cheat]:
        """Every selected cheat, in list order."""
        rows = sorted(r.row() for r in self.table.selectionModel().selectedRows())
        return [self.cheats[r] for r in rows if r < len(self.cheats)]

    def selected(self) -> Cheat | None:
        """The selected cheat when exactly one is selected."""
        picked = self.selected_all()
        return picked[0] if len(picked) == 1 else None

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
                places = bd.full_fixed_in_places(c)
                if c.value is None or places:
                    val.setFlags(val.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if places:  # one Full can't be typed for every place (L8)
                    val.setToolTip(hints.BUILD_FULL_PLACES)
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
        picked = self.selected_all()
        self.delete_btn.setEnabled(bool(picked))
        needs_notes = self._needs_notes(picked)
        carrying = self.finder.carrying  # bringing notes over: these wait (spec AC-U3)
        self.combine_btn.setEnabled(len(picked) > 1 and not needs_notes and not carrying)
        self.combine_btn.setToolTip(hints.COMBINE_NEEDS_NOTES if needs_notes else COMBINE_TIP)
        up = self.ctl.session.connected and self.ctl.session.game is not None
        located = c is not None and (c.anchor is None or c.anchor[0] in self.ctl.live.trackers)
        self.lock_btn.setEnabled(
            c is not None
            and c.anchor is None
            and c.session_only
            and len(c.locations) == 1  # Lock it watches one address
            and self.ctl.session.connected
        )
        single = single_value(c)
        self.build_btn.setEnabled(up and single)
        self.build_btn.setToolTip(
            hints.BUILD_PICK_SINGLE if c is not None and not single else hints.BUILD_BUTTON_TIP
        )
        self.learn_btn.setEnabled(up and not carrying and self._can_learn(c, located))
        p = self.ctl.project
        self.again_btn.setEnabled(up and not carrying and p is not None and bool(p.objects))
        self.bring_btn.setEnabled(up and not carrying)
        self.update_btn.setEnabled(not carrying and not self._updating)
        self.save_btn.setEnabled(any(ch.saveable for ch in self.cheats))

    def _can_learn(self, c: Cheat | None, located: bool) -> bool:
        """Find it next time: one value, or a combined cheat whose moving parts all
        have a number type and whose learned parts are all found this run."""
        if c is None:
            return False
        if not c.combined:
            return located and c.session_only and c.value_type is not None
        moving = c.moving_parts
        trackers = [self.ctl.live.trackers.get(o) for o in c.objects]
        return (
            bool(moving)
            and all(loc.vtype is not None for loc in moving)
            # found this run and not waiting on a "moved" question (no console reads)
            and all(t is not None and t.address is not None and t.pending is None for t in trackers)
        )

    def _needs_notes(self, picked: list[Cheat]) -> bool:
        """Found-again cheats are combined in the game's notes too: they must be loaded
        (connected), or the combined cheat would be lost next run."""
        return self.ctl.project is None and any(
            c.anchor is not None or c.learned_parts for c in picked
        )

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item.row() >= len(self.cheats):
            return
        c = self.cheats[item.row()]
        try:
            if item.column() == COL_ON:
                want = item.checkState() == Qt.CheckState.Checked
                if want and not c.enabled:
                    if self.finder.confirm(c):  # AC-12.2: ask once per run
                        self.finder.before_on(c)
                        self.ctl.live.turn_on(c)
                        if c.carried == "test" and c.enabled:
                            self.finder.tested(c)
                elif not want and c.enabled:
                    self.ctl.live.turn_off(c)
                    self.finder.after_off(c)
            elif item.column() == COL_NAME:
                name = item.text().strip()
                if not name:
                    raise ValueError("A cheat needs a name.")
                old, c.name = c.name, name
                if old != name:
                    self.finder.renamed(c, old)
            elif item.column() == COL_VALUE:
                c.set_value(parse_number(item.text()))
                self.ctl.live.refresh(c)
                self.finder.cheat_changed(c)
        except Exception as exc:
            self.ctl.fail(exc, "toggle" if item.column() == COL_ON else "edit")
            c.enabled = c.enabled and self.ctl.session.connected
        self.fill()

    def do_build(self, c: Cheat) -> None:
        """Make it work on this cheat's value; the result replaces it."""
        if not single_value(c):
            self.ctl.message.emit(hints.BUILD_PICK_SINGLE)
            return
        if c.value_type not in cv.EFFECT_TYPES:
            self.ctl.message.emit(cv.UNSUPPORTED_TYPE)
            return
        try:
            base = self.ctl.session.require_game().module_base
        except Exception as exc:
            self.ctl.fail(exc, "build")
            return
        self.open_build(
            address=c.locations[0].address(base),
            vtype=c.value_type,
            name=c.name,
            source=c,
            pool=c.linked,  # only a session cheat made from a grouped Find row has one
        )

    def do_delete(self) -> None:
        picked = self.selected_all()
        if not picked:
            return
        for c in picked:
            if c.enabled:
                try:
                    self.ctl.live.turn_off(c)
                except Exception as exc:
                    self.ctl.fail(exc, "toggle")
            self.cheats.remove(c)
            self.finder.forget(c)
        self.ctl.cheats_changed.emit()

    def do_combine(self) -> None:
        """Replace the selected cheats with one cheat that does all of them."""
        picked = self.selected_all()
        if self._needs_notes(picked):
            self.ctl.message.emit(hints.COMBINE_NEEDS_NOTES)
            return
        game = self.ctl.session.game if self.ctl.session.connected else None
        try:
            combined = combine(
                picked,
                addr_of=self._part_address,
                module_base=game.module_base if game is not None else None,
            )
        except ValueError as exc:
            self.ctl.fail(exc, "combine")
            return
        was_on = any(c.enabled for c in picked)
        for c in picked:
            if c.enabled:  # nothing restored: the combined cheat takes over right away
                self.ctl.live.turn_off(c, restore=False)
        at = self.cheats.index(picked[0])
        for c in picked:
            self.cheats.remove(c)
            self.finder.forget(c, save=False)
        self.cheats.insert(at, combined)
        if combined.found_again:  # nothing moves: it comes back as one row next run
            self.finder.remember_combined(combined, save=False)
        if self.ctl.project is not None:
            self.finder.save()  # once, for every change above
        self.ctl.message.emit(f'Combined into "{combined.name}". One tick turns them all on.')
        if was_on:
            self._turn_on_combined(combined)
        self.ctl.cheats_changed.emit()

    def _turn_on_combined(self, combined: Cheat) -> None:
        """A source was on: the combined cheat takes over, but a found-again part is
        asked about first (one question) and its hook put back, as for a tick."""
        try:
            if combined.learned_parts:
                if not self.finder.confirm(combined):
                    return  # "OK, nothing was changed": it stays off
                self.finder.before_on(combined)
            self.ctl.live.turn_on(combined)
        except Exception as exc:
            self.ctl.fail(exc, "toggle")

    def _part_address(self, loc: Location) -> int | None:
        """Where a learned part is right now (to spot parts that overlap), or None."""
        if loc.anchor is None:
            return None
        tracker = self.ctl.live.trackers.get(loc.anchor[0])
        try:
            return tracker.field_address(loc.anchor[1]) if tracker is not None else None
        except (CheatMakerError, KeyError):
            return None

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
        if self._learning or self.ctl.code_reading or not (cf and g and self.ctl.session.connected):
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
        self.ctl.code_reading = True  # one code read at a time (notes' fingerprints too)
        session, title_id, version = self.ctl.session, g.title_id, g.version

        def work() -> int:
            sigs = up.learn(cf, up.read_code_image(session))
            up.save_signatures(path, title_id, version, sigs)
            return len(sigs)

        def done(n: int) -> None:
            self._learning = False
            self.ctl.code_reading = False
            log.info("action=learn_signatures saved=%d version=%s", n, version)

        def failed(exc: BaseException) -> None:
            self._learning = False
            self.ctl.code_reading = False
            log.warning("action=learn_signatures reason=%s", exc)

        self.ctl.tasks.run(work, done, failed)

    def do_update(self) -> None:
        cf, g = self.ctl.cheat_file, self.ctl.session.game
        if not (cf and g) or self.finder.carrying or self._updating:
            return
        session = self.ctl.session
        old_path = up.signatures_path(cf.title_id, cf.version)
        notes_path = pj.project_path(cf.title_id, cf.version)
        p = self.ctl.project  # the brought-over notes, if this version has them
        new_notes = p if p is not None and not versions_differ(p.version, g.version) else None
        # moved caves never go where the notes' hooks and code parts live (taken here,
        # on the UI thread: the notes can change while the worker runs)
        reserved = list(session.reserved_rooms)
        if new_notes is not None:
            reserved += ah.reserved_rooms(new_notes.objects) + cr.part_rooms(new_notes)
        self._updating = True
        self.update_btn.setEnabled(False)
        self.hint.setText(hints.UPDATE_RUNNING)

        def work():
            image = up.read_code_image(session)
            try:  # the old version's notes: read only, for its hook cheats (AC-U9)
                old_notes = pj.load(notes_path)
            except ProjectFileError as exc:
                log.info("action=update_version old_notes=unreadable reason=%s", exc)
                old_notes = None
            new, report = cr.update_with_hooks(
                session,
                cf,
                up.load_signatures(old_path),
                image,
                g.version,
                old_notes,
                new_notes,
                reserved=reserved,
            )
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
        self._updating = False
        self._update_buttons()
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
        self._updating = False
        self._update_buttons()
        self.fill()
        self.ctl.fail(exc, "update_version")
