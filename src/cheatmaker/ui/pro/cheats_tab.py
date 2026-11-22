"""The Pro Cheats tab (spec PR-8, PR-12, PR-14).

Lists every cheat of My cheats. A Pro cheat (``Cheat.pro``) is edited here: entries,
variables and cave sources; Build assembles and places a cave (``compile.build`` with
My cheats' code lines kept free), then the cheat is compiled again. A guided cheat is
shown as it would be adopted; Edit in Pro adopts it (byte for byte), after which the
guided wizards refuse it. On/off goes through ``compile.enable``/``disable`` (the safety
rules, AC-8.4 to 8.8); My cheats' tick follows.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import time
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cheatmaker.core.cheats import Cheat
from cheatmaker.core.pro import compile as pc_compile
from cheatmaker.core.pro import importer, sites
from cheatmaker.core.pro.addresses import parse_place
from cheatmaker.core.pro.asm import Assembler
from cheatmaker.core.pro.asm_text import adopt
from cheatmaker.core.pro.badge import badge
from cheatmaker.core.pro.model import ProCheat, ProEntry, Variable, new_cheat_id
from cheatmaker.core.values import ValueType
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.pro import hints
from cheatmaker.ui.pro.cave_editor import CaveEditor
from cheatmaker.ui.pro.export_dialog import ExportDialog
from cheatmaker.ui.pro.import_dialog import ImportDialog
from cheatmaker.ui.pro.tab import ProMessages, ProTab
from cheatmaker.ui.tasks import TaskRunner

log = logging.getLogger(__name__)

VAR_NAME, VAR_TYPE, VAR_VALUE, VAR_COMMENT, VAR_IN_FILE = range(5)
VAR_TYPES = tuple(ValueType)


def entry_label(entry: ProEntry) -> str:
    if entry.kind == "cave":
        where = "not placed" if entry.cave is None else f"cave at +{entry.cave:#x}"
        stale = " (needs Build)" if entry.stale else ""
        return f"Cave hooked at +{entry.offset:#x}, {where}{stale}"
    return f"{entry.kind.capitalize()} at +{entry.offset:#x} ({len(entry.on)} bytes)"


def _apply(pc: ProCheat, built: ProCheat) -> None:
    """Copy what a worker built on a copy of ``pc`` back into ``pc`` (UI thread). Entry
    objects stay the same (the editor and lists hold them); new entries are added."""
    for k, new in enumerate(built.entries):
        if k < len(pc.entries):
            for f in dataclasses.fields(ProEntry):
                setattr(pc.entries[k], f.name, getattr(new, f.name))
        else:
            pc.entries.append(new)
    pc.variables[:] = built.variables
    pc.editable = built.editable


def _number(text: str, vtype: ValueType) -> float | int:
    text = text.strip()
    if vtype.is_float:
        return float(text)
    return int(text, 0)


class CheatsTab(ProTab):
    def __init__(
        self,
        ctl: Controller,
        tasks: TaskRunner,
        assembler: Assembler | None,
        save: Callable[[], None],
        msgs: ProMessages | None = None,
    ) -> None:
        super().__init__(ctl, tasks, msgs)
        self.assembler = assembler
        self.save = save  # stores Pro sources in the .cmproj (FindAgain.save)
        self.writer = pc_compile.ProWriter(ctl.session, warn=self.msgs.say)
        self.sleep: Callable[[float], None] = time.sleep
        self.ask_text: Callable[[str, str, str], str | None] = self._ask_text
        self.pick_file: Callable[[], str] = self._pick_file
        self.exec_dialog: Callable[[object], object] = lambda d: d.exec()
        self.busy = False
        self.preview: ProCheat | None = None  # a guided cheat as it would be adopted
        self._filling = False

        self.cheat_list = QListWidget()
        self.hint = QLabel(hints.CHEATS)
        self.hint.setWordWrap(True)
        self.asm_missing = QLabel(hints.ASSEMBLER_MISSING)
        self.asm_missing.setWordWrap(True)
        self.asm_missing.setStyleSheet("color: #b00000;")
        self.asm_missing.setHidden(assembler is not None)
        self.new_btn = QPushButton("New cheat...")
        self.adopt_btn = QPushButton("Edit in Pro")
        self.on_box = QCheckBox("On")
        self.import_btn = QPushButton("Import...")
        self.export_btn = QPushButton("Export...")
        self.badge = QLabel("")
        self.entries = QListWidget()
        self.add_cave_btn = QPushButton("Add cave...")
        self.add_nop_btn = QPushButton("Add NOP...")
        self.remove_entry_btn = QPushButton("Remove entry")
        self.vars = QTableWidget(0, 5)
        self.vars.setHorizontalHeaderLabels(["Name", "Type", "Value", "Comment", "In file"])
        self.add_var_btn = QPushButton("Add variable")
        self.remove_var_btn = QPushButton("Remove variable")
        self.editor = CaveEditor(ctl, tasks, self.msgs)
        self.editor.assembler = assembler
        self.editor.on_build = self.build_entry
        self.editor.others = self._others_rooms

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(self.cheat_list, 1)
        row = QHBoxLayout()
        for w in (self.new_btn, self.adopt_btn, self.on_box):
            row.addWidget(w)
        ll.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(self.import_btn)
        row.addWidget(self.export_btn)
        ll.addLayout(row)
        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.addWidget(self.badge)
        ml.addWidget(QLabel("Entries"))
        ml.addWidget(self.entries, 1)
        row = QHBoxLayout()
        for w in (self.add_cave_btn, self.add_nop_btn, self.remove_entry_btn):
            row.addWidget(w)
        ml.addLayout(row)
        ml.addWidget(QLabel("Variables (constants in the cave)"))
        ml.addWidget(self.vars, 1)
        row = QHBoxLayout()
        row.addWidget(self.add_var_btn)
        row.addWidget(self.remove_var_btn)
        ml.addLayout(row)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(mid)
        split.addWidget(self.editor)
        split.setStretchFactor(2, 2)
        lay = QVBoxLayout(self.body)
        lay.addWidget(self.hint)
        lay.addWidget(self.asm_missing)
        lay.addWidget(split, 1)

        self.cheat_list.currentRowChanged.connect(lambda _r: self._select())
        self.entries.currentRowChanged.connect(lambda _r: self._select_entry())
        self.new_btn.clicked.connect(self.do_new)
        self.adopt_btn.clicked.connect(self.do_adopt)
        self.on_box.clicked.connect(self._on_clicked)
        self.import_btn.clicked.connect(self.do_import)
        self.export_btn.clicked.connect(self.do_export)
        self.add_cave_btn.clicked.connect(lambda: self.do_add_entry("cave"))
        self.add_nop_btn.clicked.connect(lambda: self.do_add_entry("nop"))
        self.remove_entry_btn.clicked.connect(self.do_remove_entry)
        self.add_var_btn.clicked.connect(self.do_add_variable)
        self.remove_var_btn.clicked.connect(self.do_remove_variable)
        self.vars.itemChanged.connect(self._var_changed)
        ctl.cheats_changed.connect(self.fill)
        self.fill()

    # -- dialogs (replaced in tests) ----------------------------------------------------------
    def _ask_text(self, title: str, prompt: str, default: str) -> str | None:
        text, ok = QInputDialog.getText(self, title, prompt, text=default)
        return text if ok else None

    def _pick_file(self) -> str:
        path, _ = QFileDialog.getOpenFileName(
            self, hints.IMPORT_TITLE, "", "Cheat files (*.json *.shn *.mc4)"
        )
        return path

    # -- the list -----------------------------------------------------------------------------
    @property
    def cheats(self) -> list[Cheat]:
        return self.ctl.cheat_file.cheats if self.ctl.cheat_file else []

    def selected(self) -> Cheat | None:
        row = self.cheat_list.currentRow()
        return self.cheats[row] if 0 <= row < len(self.cheats) else None

    @property
    def pc(self) -> ProCheat | None:
        """The Pro source on show: the cheat's own, or a guided cheat's preview."""
        c = self.selected()
        return None if c is None else (c.pro or self.preview)

    def select_cheat(self, cheat: Cheat) -> None:
        for k, c in enumerate(self.cheats):
            if c is cheat:
                self.cheat_list.setCurrentRow(k)
                return

    def fill(self) -> None:
        keep = self.selected()
        self._filling = True
        try:
            self.cheat_list.clear()
            for c in self.cheats:
                name = c.name if c.pro is not None else hints.GUIDED.format(name=c.name)
                item = QListWidgetItem(("● " if c.enabled else "") + name)
                item.setToolTip(badge(c, self.ctl.project).tooltip)
                self.cheat_list.addItem(item)
        finally:
            self._filling = False
        self.hint.setText(hints.CHEATS if self.cheats else hints.CHEATS_EMPTY)
        if keep is not None and keep in self.cheats:
            self.select_cheat(keep)
        else:
            self._select()

    def _select(self) -> None:
        if self._filling:
            return
        c = self.selected()
        self.preview = None
        if c is not None and c.pro is None and self.assembler is not None:
            try:
                self.preview = adopt(c, "preview", self.assembler)
            except Exception as exc:  # shown read-only: say why it can't be adopted
                log.info("action=pro_preview cheat=%s reason=%s", c.name, exc)
        self._fill_detail()

    def _fill_detail(self) -> None:
        c, pc = self.selected(), self.pc
        if c is not None:
            b = badge(c, self.ctl.project)
            self.badge.setText(
                f"Badge: {b.target}" + (f" ({', '.join(b.notes)})" if b.notes else "")
            )
            self.badge.setToolTip(b.tooltip)
        else:
            self.badge.setText("")
        self.entries.blockSignals(True)
        self.entries.clear()
        for e in pc.entries if pc is not None else []:
            self.entries.addItem(entry_label(e))
        self.entries.blockSignals(False)
        self._fill_vars()
        if pc is not None and pc.entries:
            cave = next((k for k, e in enumerate(pc.entries) if e.kind == "cave"), 0)
            self.entries.setCurrentRow(cave)
        self._select_entry()
        self._update_buttons()

    def _fill_vars(self) -> None:
        pc = self.pc
        self.vars.blockSignals(True)
        self.vars.setRowCount(0 if pc is None else len(pc.variables))
        for k, v in enumerate(pc.variables if pc is not None else []):
            self.vars.setItem(k, VAR_NAME, QTableWidgetItem(v.name))
            box = QComboBox()
            for t in VAR_TYPES:
                box.addItem(t.name.lower(), t)
            box.setCurrentIndex(VAR_TYPES.index(v.vtype))
            box.currentIndexChanged.connect(lambda _i, row=k: self._type_changed(row))
            self.vars.setCellWidget(k, VAR_TYPE, box)
            self.vars.setItem(k, VAR_VALUE, QTableWidgetItem(v.vtype.text(v.value)))
            self.vars.setItem(k, VAR_COMMENT, QTableWidgetItem(v.comment))
            infile = QTableWidgetItem()
            infile.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            infile.setCheckState(
                Qt.CheckState.Checked if pc.editable == v.name else Qt.CheckState.Unchecked
            )
            self.vars.setItem(k, VAR_IN_FILE, infile)
        self.vars.blockSignals(False)

    def selected_entry(self) -> ProEntry | None:
        pc, row = self.pc, self.entries.currentRow()
        return pc.entries[row] if pc is not None and 0 <= row < len(pc.entries) else None

    def _select_entry(self) -> None:
        c = self.selected()
        read_only = c is None or c.pro is None or c.enabled  # its bytes are in the game
        self.editor.set_entry(self.pc, self.selected_entry(), read_only=read_only)
        if c is not None and c.pro is None and self.preview is not None:
            self.editor.error.setText(hints.GUIDED_PREVIEW)
        self._update_buttons()

    def _update_buttons(self) -> None:
        c = self.selected()
        editable = c is not None and c.pro is not None and not self.busy
        off = editable and not c.enabled
        has_asm = self.assembler is not None
        self.adopt_btn.setEnabled(
            c is not None and c.pro is None and self.preview is not None and not self.busy
        )
        self.on_box.setEnabled(editable and self.running)
        self.on_box.setChecked(c is not None and c.enabled)
        self.new_btn.setEnabled(has_asm and self.running and not self.busy)
        self.add_cave_btn.setEnabled(off and has_asm and self.running)
        self.add_nop_btn.setEnabled(off and self.running)
        self.remove_entry_btn.setEnabled(off and self.selected_entry() is not None)
        self.add_var_btn.setEnabled(editable)
        self.remove_var_btn.setEnabled(editable and self.vars.currentRow() >= 0)
        self.vars.setEnabled(editable)
        self.export_btn.setEnabled(bool(self.cheats))
        self.import_btn.setEnabled(not self.busy)

    def game_state_changed(self, new_game: bool) -> None:
        if new_game:
            self.writer = pc_compile.ProWriter(self.ctl.session, warn=self.msgs.say)
        self._update_buttons()

    # -- running work ---------------------------------------------------------------------------
    def _run(self, work, done, action: str) -> None:
        """``work`` on the worker. It never changes a cheat the UI shows: it works on a
        copy, and ``done`` (UI thread) applies the result. A failure changes nothing and
        saves nothing."""
        self.busy = True
        self._update_buttons()

        def finish(result) -> None:
            self.busy = False
            done(result)
            self._update_buttons()

        def failed(exc: BaseException) -> None:
            self.busy = False
            self.msgs.fail(exc, action)
            self.fill()

        self.tasks.run(work, finish, failed)

    def _changed(self, cheat: Cheat | None = None) -> None:
        """A Pro source changed: compile it again, keep it in the notes, show it."""
        if cheat is not None and cheat.pro is not None:
            pc_compile.refresh(cheat)
        self.save()
        self.ctl.cheats_changed.emit()

    def _others(self, skip: ProCheat | None) -> list[ProCheat]:
        return [c.pro for c in self.cheats if c.pro is not None and c.pro is not skip]

    def _others_rooms(self) -> list[sites.Range]:
        pc = self.pc
        return [
            *sites.cave_rooms(self._others(pc)),
            *sites.open_rooms(self.cheats, skip=pc.id if pc is not None else None),
        ]

    def _taken_ids(self) -> list[ProCheat]:
        p = self.ctl.project
        return [*self._others(None), *(p.pro_cheats if p is not None else [])]

    def _ask_place(self, prompt: str) -> int | None:
        text = self.ask_text(hints.NEW_CHEAT_TITLE, prompt, "eboot.bin+0x")
        if not text:
            return None
        game = self.ctl.session.require_game()
        return parse_place(text, game) - game.module_base

    # -- build --------------------------------------------------------------------------------
    def _builder(self, pc: ProCheat) -> Callable[[ProCheat, ProEntry], None]:
        """Build one cave of a copy of ``pc`` (worker), with the other Pro caves and My
        cheats' code lines kept free as they are now (worked out here, on the UI
        thread)."""
        assert self.assembler is not None
        asm, session = self.assembler, self.ctl.session
        others = [copy.deepcopy(o) for o in self._others(pc)]
        rooms = sites.open_rooms(self.cheats, skip=pc.id)

        def build(trial: ProCheat, entry: ProEntry) -> None:
            result = pc_compile.build(session, trial, entry, asm, others, rooms)
            if not result.ok:
                line, message = result.error  # type: ignore[misc]
                raise ValueError(hints.ASM_ERROR.format(line=line, message=message))

        return build

    def build_entry(self, entry: ProEntry, text: str | None = None) -> None:
        """Build ``entry`` from ``text`` (the editor's draft); the draft becomes its
        source only when the build succeeds."""
        c = self.selected()
        if c is None or c.pro is None or self.assembler is None or entry not in c.pro.entries:
            return
        if c.enabled:
            self.msgs.say(hints.TURN_OFF_FIRST)
            return
        pc = c.pro
        k = next(i for i, e in enumerate(pc.entries) if e is entry)
        trial = copy.deepcopy(pc)
        trial.entries[k].source = entry.source if text is None else text
        build = self._builder(pc)

        def done(built: ProCheat) -> None:
            _apply(pc, built)
            self.editor.built(entry)
            self._changed(c)
            self.msgs.say(
                hints.BUILT.format(
                    name=pc.name, size=len(entry.cave_bytes), place=f"+{entry.cave:#x}"
                )
            )
            self.editor.refresh_budget()

        def work() -> ProCheat:
            build(trial, trial.entries[k])
            return trial

        self._run(work, done, "pro_build")

    def do_new(self) -> None:
        """A new cheat: a name and a hook; its cave is built at once with the default
        source (``original`` + ``jmp return``), so it can be listed and saved."""
        if self.assembler is None:
            return
        name = self.ask_text(
            hints.NEW_CHEAT_TITLE, hints.NEW_CHEAT_NAME, hints.NEW_CHEAT_NAME_DEFAULT
        )
        if not name or not name.strip():
            return
        try:
            hook = self._ask_place(hints.HOOK_PROMPT)
        except Exception as exc:
            self.msgs.fail(exc, "pro_new")
            return
        if hook is None:
            return
        self.new_cheat(name.strip(), "cave", hook)

    def new_cheat(self, name: str, kind: str, offset: int) -> None:
        """A new Pro cheat with one entry at module ``offset``; a cave is built at once
        (with the default source) so the cheat can be listed and saved."""
        pc = ProCheat(new_cheat_id(self._taken_ids()), name)
        session = self.ctl.session
        build = self._builder(pc) if kind == "cave" else None

        def work() -> Cheat:  # ``pc`` is new: nothing shows it until ``done``
            if build is not None:
                entry = pc_compile.new_cave_entry(session, offset)
                pc.entries.append(entry)
                build(pc, entry)
            else:
                pc.entries.append(pc_compile.new_nop_entry(session, offset))
            return pc_compile.compile_cheat(pc)

        def done(cheat: Cheat) -> None:
            self.ctl.ensure_file().cheats.append(cheat)
            log.info("action=pro_new id=%s kind=%s at=%#x", pc.id, kind, offset)
            self._changed()
            self.select_cheat(cheat)

        self._run(work, done, "pro_new")

    def entry_here(self, kind: str, offset: int) -> None:
        """From the Code view: add to the picked Pro cheat when it is off, else make a
        new cheat (its name asked)."""
        c = self.selected()
        if kind == "cave" and self.assembler is None:
            self.msgs.say(hints.ASSEMBLER_MISSING)
            return
        if c is not None and c.pro is not None and not c.enabled:
            self.add_entry_at(c, kind, offset)
            return
        name = self.ask_text(
            hints.NEW_CHEAT_TITLE, hints.NEW_CHEAT_NAME, hints.NEW_CHEAT_NAME_DEFAULT
        )
        if name and name.strip():
            self.new_cheat(name.strip(), kind, offset)

    def do_adopt(self) -> None:
        """Edit in Pro: the guided cheat gets its Pro source (the same bytes)."""
        c = self.selected()
        if c is None or c.pro is not None or self.assembler is None:
            return
        try:
            c.pro = adopt(c, new_cheat_id(self._taken_ids()), self.assembler)
        except Exception as exc:
            self.msgs.fail(exc, "pro_adopt")
            return
        log.info("action=pro_adopt id=%s name=%s", c.pro.id, c.name)
        self._changed()

    # -- entries ------------------------------------------------------------------------------
    def do_add_entry(self, kind: str) -> None:
        c = self.selected()
        if c is None or c.pro is None:
            return
        if c.enabled:
            self.msgs.say(hints.TURN_OFF_FIRST)
            return
        try:
            offset = self._ask_place(hints.HOOK_PROMPT if kind == "cave" else hints.NOP_PROMPT)
        except Exception as exc:
            self.msgs.fail(exc, "pro_entry")
            return
        if offset is not None:
            self.add_entry_at(c, kind, offset)

    def add_entry_at(self, c: Cheat, kind: str, offset: int) -> None:
        """Add a NOP or cave entry at module ``offset`` (also from the Code view)."""
        pc, session = c.pro, self.ctl.session
        if pc is None:
            return
        trial = copy.deepcopy(pc)
        build = self._builder(pc) if kind == "cave" else None

        def work() -> ProCheat:
            if build is not None:
                entry = pc_compile.new_cave_entry(session, offset)
                trial.entries.append(entry)
                build(trial, entry)
            else:
                trial.entries.append(pc_compile.new_nop_entry(session, offset))
            return trial

        def done(built: ProCheat) -> None:
            _apply(pc, built)
            self._changed(c)

        self._run(work, done, "pro_entry")

    def do_remove_entry(self) -> None:
        c, entry = self.selected(), self.selected_entry()
        if c is None or c.pro is None or entry is None:
            return
        if c.enabled:
            self.msgs.say(hints.TURN_OFF_FIRST)
            return
        if len(c.pro.entries) == 1:
            self.msgs.say(hints.LAST_ENTRY)
            return
        c.pro.entries.remove(entry)
        if c.pro.editable is not None and pc_compile.home(c.pro) is None:
            c.pro.editable = None  # the variables had their home in that cave
        self._changed(c)

    # -- on / off (AC-8.4 to 8.8) -------------------------------------------------------------
    def _on_clicked(self, checked: bool) -> None:
        c = self.selected()
        if c is None or c.pro is None:
            return
        self.on_box.setChecked(c.enabled)  # follows the game, not the click
        self.set_on(c, checked)

    def set_on(self, c: Cheat, on: bool) -> None:
        pc, session = c.pro, self.ctl.session
        if pc is None or on == c.enabled:
            return
        if on:
            enabled = [x for x in self.cheats if x.enabled and x is not c]

            def work() -> None:
                pc_compile.enable(session, pc, self.writer, enabled)

            def done(_r) -> None:
                self.ctl.live.mark_on(c)
                self.ctl.cheats_changed.emit()

        else:

            def work() -> None:
                pc_compile.disable(session, pc, self.writer, self.sleep)

            def done(_r) -> None:
                self.ctl.live.turn_off(c, restore=False)  # already written back
                self.ctl.cheats_changed.emit()

        self._run(work, done, "pro_toggle")

    # -- variables (R1: constants in the cave) ------------------------------------------------
    def do_add_variable(self) -> None:
        c = self.selected()
        if c is None or c.pro is None:
            return
        names = {v.name for v in c.pro.variables}
        n = 1
        while f"{hints.VAR_NEW}{n}" in names:
            n += 1
        c.pro.variables.append(Variable(f"{hints.VAR_NEW}{n}", ValueType.INT32, 0))
        self._changed(c)
        self.msgs.say(hints.VAR_NEEDS_BUILD.format(name=f"{hints.VAR_NEW}{n}"))

    def do_remove_variable(self) -> None:
        c, row = self.selected(), self.vars.currentRow()
        if c is None or c.pro is None or not 0 <= row < len(c.pro.variables):
            return
        gone = c.pro.variables.pop(row)
        if c.pro.editable == gone.name:
            c.pro.editable = None
        self._changed(c)

    def _type_changed(self, row: int) -> None:
        c = self.selected()
        if c is None or c.pro is None or row >= len(c.pro.variables):
            return
        box = self.vars.cellWidget(row, VAR_TYPE)
        old = c.pro.variables[row]
        vtype: ValueType = box.currentData()
        try:
            c.pro.variables[row] = Variable(old.name, vtype, vtype.clamp(old.value), old.comment)
        except ValueError as exc:
            self.msgs.fail(exc, "pro_variable")
            self.fill()
            return
        self.msgs.say(hints.VAR_NEEDS_BUILD.format(name=old.name))
        self._changed(c)

    def _var_changed(self, item: QTableWidgetItem) -> None:
        c, row = self.selected(), item.row()
        if c is None or c.pro is None or row >= len(c.pro.variables):
            return
        pc, old = c.pro, c.pro.variables[row]
        col = item.column()
        try:
            if col == VAR_NAME:
                pc.variables[row] = Variable(item.text().strip(), old.vtype, old.value, old.comment)
                if pc.editable == old.name:
                    pc.editable = pc.variables[row].name
                self.msgs.say(hints.VAR_NEEDS_BUILD.format(name=item.text().strip()))
            elif col == VAR_COMMENT:
                pc.variables[row] = Variable(old.name, old.vtype, old.value, item.text())
            elif col == VAR_IN_FILE:
                checked = item.checkState() == Qt.CheckState.Checked
                pc.editable = old.name if checked else None
            elif col == VAR_VALUE:
                self._set_value(c, old, _number(item.text(), old.vtype))
                return
        except (ValueError, KeyError) as exc:
            self.msgs.fail(exc, "pro_variable")
            self.fill()
            return
        self._changed(c)

    def _set_value(self, c: Cheat, var: Variable, value: float | int) -> None:
        """A new value: patched into the built cave (and the game, when on; AC-8.7), or
        kept for the next build when the cave holds another layout."""
        pc = c.pro
        assert pc is not None
        built = pc_compile.home(pc) is not None and pc_compile.layout_current(pc)
        built = built and bool(pc_compile.home(pc).cave_bytes)  # type: ignore[union-attr]
        if not built:
            k = pc.variables.index(var)
            pc.variables[k] = Variable(var.name, var.vtype, value, var.comment)
            self.msgs.say(hints.VAR_NEEDS_BUILD.format(name=var.name))
            self._changed(c)
            return
        on, session, writer = c.enabled, self.ctl.session, self.writer
        trial = copy.deepcopy(pc)

        def work() -> tuple[str, ProCheat]:
            # patches the built bytes in place (never re-assembles; a draft in the
            # editor isn't the source until it is built)
            how = pc_compile.set_variable(
                trial, var.name, value, session=session, writer=writer, on=on
            )
            return how, trial

        def done(result: tuple[str, ProCheat]) -> None:
            how, built = result
            _apply(pc, built)
            text = hints.VAR_WRITTEN if how == pc_compile.WRITTEN else hints.VAR_SAVED
            self.msgs.say(text.format(name=var.name, value=var.vtype.text(value)))
            self._changed(c)

        self._run(work, done, "pro_variable")

    # -- import / export ----------------------------------------------------------------------
    def do_import(self) -> None:
        path = self.pick_file()
        if not path:
            return
        try:
            result = importer.import_path(path, self._taken_ids())
        except Exception as exc:
            self.msgs.fail(exc, "pro_import")
            return
        dlg = ImportDialog(self.ctl, result, self.assembler, self)
        self.exec_dialog(dlg)
        if not dlg.chosen:
            return
        cf = self.ctl.ensure_file()
        added = 0
        for pc in dlg.chosen:
            try:
                cf.cheats.append(pc_compile.compile_cheat(pc))
                added += 1
            except Exception as exc:  # e.g. nothing to write: skip it, say why
                self.msgs.fail(exc, "pro_import")
        self._changed()
        self.msgs.say(hints.IMPORTED.format(n=added))

    def do_export(self) -> None:
        self.exec_dialog(ExportDialog(self.ctl, self.msgs, self))
