"""v3 screens and glue: Find it next time, find again on connect, confirm, pick one.

Core logic lives in ``core.refind`` / ``core.anchor_hook``; this module only decides
when to run it and what the player sees. No "pointer", "cave" or "vtable" words here
(AC-8.2): the player reads "the game's code", "a memory path", "a memory search".
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.session import GameInfo
from cheatmaker.core.values import ValueType
from cheatmaker.errors import CheatMakerError
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.tasks import _Signals

log = logging.getLogger(__name__)

LIVE_MS = 500


def fmt(v: float | int | None) -> str:
    if v is None:
        return "?"
    return f"{v:,.2f}" if isinstance(v, float) else f"{v:,}"


def read_field(ctl: Controller, obj: pj.ObjectSpec, addr: int, name: str) -> float | int | None:
    """The field's value now, or None if it can't be read."""
    try:
        f = obj.field(name)
        g = ctl.session.require_game()
        raw = ctl.session.require_client().read(g.pid, addr + f.off, f.vtype.size)
    except (CheatMakerError, KeyError):
        return None
    return f.vtype.decode(raw)


class FindAgain(QObject):
    """Loads the game's notes on connect, puts the hooks in, finds every value again,
    and keeps the live cheats tied to where the values are now."""

    def __init__(self, ctl: Controller) -> None:
        super().__init__(ctl)
        self.ctl = ctl
        self.installed: dict[str, pj.ObjectSpec] = {}  # hooks we put in (taken out on close)
        self._ours: dict[str, int] = {}  # object id -> pid we installed its hook in
        self.ask_confirm: Callable[[str], bool] = self._ask_confirm
        self.ask_pick: Callable[[pj.ObjectSpec, list[rf.Found]], int | None] = self._ask_pick
        self.open_search: Callable[[ValueType, float | int | None], None] = lambda vt, v: None
        self.running = False
        self.sleep: Callable[[float], None] = time.sleep  # tests: no waiting
        ctl.game_changed.connect(self.on_game)
        ctl.before_disconnect.append(self.remove_hooks)

    # -- project -----------------------------------------------------------------
    def ensure_project(self) -> pj.Project:
        g = self.ctl.session.require_game()
        if self.ctl.project is None:
            self.ctl.project = pj.Project(g.title_id, g.version, g.process)
        return self.ctl.project

    def save(self) -> None:
        p = self.ctl.project
        if p is None:
            return
        try:
            pj.save(pj.project_path(p.title_id, p.version), p)
        except OSError as exc:
            log.warning("action=project_save reason=%s", exc)

    def on_game(self, game: GameInfo | None) -> None:
        self.installed.clear()
        if game is None:
            self.ctl.project = None
            return
        try:
            self.ctl.project = pj.load(pj.project_path(game.title_id, game.version))
        except CheatMakerError as exc:
            self.ctl.project = None
            self.ctl.fail(exc, "project_load")
            return
        if self.ctl.project is not None and self.ctl.project.objects:
            self.ensure_cheats()
            self.ctl.cheats_changed.emit()
            self.start()

    def ensure_cheats(self) -> None:
        """Live cheats saved in the notes come back (unticked) in My cheats."""
        p = self.ctl.project
        if p is None:
            return
        cf = self.ctl.ensure_file()
        have = {c.anchor for c in cf.cheats if c.anchor}
        for pc in p.cheats:
            if (pc.object, pc.field) in have:
                continue
            try:
                f = p.object(pc.object).field(pc.field)
            except KeyError:
                continue
            raw = f.vtype.encode(pc.value)
            cf.cheats.append(
                Cheat(
                    pc.name,
                    [Location(LocationKind.SESSION_ADDRESS, 0, raw, raw)],
                    freeze=pc.action == "freeze",
                    value_type=f.vtype,
                    hint="Found again each time you connect.",
                    anchor=(pc.object, pc.field),
                )
            )

    # -- find again --------------------------------------------------------------
    def start(self, use_hook: bool = True) -> None:
        p = self.ctl.project
        if p is None or not p.objects or self.running or not self.ctl.session.connected:
            return
        session = self.ctl.session
        objects = list(p.objects)
        ours = dict(self._ours)

        def work():
            pid = session.require_game().pid
            results = []
            for obj in objects:
                state = ""
                if use_hook and obj.hook is not None and not obj.has_proven_chain:
                    try:  # D14: the app puts the hook in on connect
                        state = ah.install(session, obj)
                    except CheatMakerError as exc:
                        log.warning("action=hook_install object=%s reason=%s", obj.id, exc)
                if state == "already" and ours.get(obj.id) == pid:
                    state = "installed"  # ours from earlier in this run
                results.append((obj, state, rf.find_again(session, obj, use_hook=use_hook)))
            return pid, results

        self.running = True
        self.ctl.message.emit(hints.FINDING)
        self.ctl.tasks.run(work, self._done, self._failed)

    def _failed(self, exc: BaseException) -> None:
        self.running = False
        self.ctl.fail(exc, "find_again")

    def _done(self, result) -> None:
        self.running = False
        pid, results = result
        p = self.ctl.project
        lines = []
        for obj, state, out in results:
            if state == "installed":
                self.installed[obj.id] = obj
                self._ours[obj.id] = pid
            if out.status == "disagree":
                picked = self.ask_pick(obj, out.options)
                out = rf.confirm_pick(obj, picked) if picked is not None else out
            if p is not None:
                rf.remember(p, out)
            if out.found and out.address is not None:
                tracker = rf.Tracker(self.ctl.session, obj, out.address)
                tracker.confirmed = out.method == "picked"
                self.ctl.live.trackers[obj.id] = tracker
                f = obj.fields[0]
                f.last = read_field(self.ctl, obj, out.address, f.name)
                how = rf.METHOD_WORDS.get(out.method, out.method)
                lines.append(hints.FOUND_AGAIN.format(name=obj.label, how=how, secs=out.seconds))
            else:
                f = obj.fields[0]
                lines.append(hints.NOT_FOUND.format(name=obj.label))
                self.open_search(f.vtype, f.last)
        self.save()
        self.ctl.cheats_changed.emit()
        if lines:
            self.ctl.message.emit(" ".join(lines))

    def remove_hooks(self) -> None:
        for obj in list(self.installed.values()):
            try:
                ah.remove(self.ctl.session, obj, self.sleep)
            except CheatMakerError as exc:
                log.warning("action=hook_remove object=%s reason=%s", obj.id, exc)
        self.installed.clear()
        self._ours.clear()

    # -- confirm before the first write (AC-12.2) -----------------------------------
    def confirm(self, cheat: Cheat) -> bool:
        """True when the cheat may write. Asks once per run per object."""
        if cheat.anchor is None:
            return True
        obj_id, name = cheat.anchor
        tracker = self.ctl.live.trackers.get(obj_id)
        if tracker is None or tracker.confirmed:
            return True  # not found: turn_on refuses with a plain message
        addr = tracker.current()
        if addr is None:
            return True
        value = read_field(self.ctl, tracker.obj, addr, name)
        if self.ask_confirm(hints.CONFIRM.format(name=tracker.obj.label, value=fmt(value))):
            tracker.confirmed = True
            return True
        self.ctl.message.emit(hints.CONFIRM_NO.format(name=tracker.obj.label))
        return False

    def _ask_confirm(self, text: str) -> bool:
        return QMessageBox.question(None, "Is this it?", text) == QMessageBox.StandardButton.Yes

    def _ask_pick(self, obj: pj.ObjectSpec, options: list[rf.Found]) -> int | None:
        dlg = PickDialog(self.ctl, obj, options)
        dlg.exec()
        return dlg.picked

    # -- after Find it next time ----------------------------------------------------
    def object_id_for(self, cheat: Cheat) -> str:
        p = self.ensure_project()
        if cheat.anchor is not None:
            return cheat.anchor[0]
        for pc in p.cheats:
            if pc.name == cheat.name:
                return pc.object  # a new find updates the anchor (AC-12.4)
        return p.new_object_id()

    def adopt(self, cheat: Cheat, learned: rf.Learned, value_addr: int, state: str) -> None:
        p = self.ensure_project()
        obj = learned.obj
        f = obj.fields[0]
        if cheat.value is not None:
            rf.widen(f, cheat.value)
        p.put_object(obj)
        p.cheats = [c for c in p.cheats if c.name != cheat.name and c.object != obj.id]
        p.cheats.append(
            pj.ProjectCheat(
                cheat.name,
                obj.id,
                f.name,
                "freeze" if cheat.freeze else "set",
                cheat.value if cheat.value is not None else 0,
            )
        )
        cheat.anchor = (obj.id, f.name)
        tracker = rf.Tracker(self.ctl.session, obj, value_addr - f.off)
        tracker.confirmed = True  # the player just found it by hand
        self.ctl.live.trackers[obj.id] = tracker
        if state in ("installed", "already"):
            self.installed[obj.id] = obj
            self._ours[obj.id] = self.ctl.session.require_game().pid
        self.save()
        self.ctl.cheats_changed.emit()

    def forget(self, cheat: Cheat) -> None:
        """A deleted cheat doesn't come back next time (the object's notes stay)."""
        p = self.ctl.project
        if p is None or cheat.anchor is None:
            return
        p.cheats = [c for c in p.cheats if (c.object, c.field) != cheat.anchor]
        self.save()

    def cheat_changed(self, cheat: Cheat) -> None:
        """The player edited an anchored cheat's value: remember it for next time."""
        p = self.ctl.project
        if p is None or cheat.anchor is None or cheat.value is None:
            return
        for pc in p.cheats:
            if (pc.object, pc.field) == cheat.anchor:
                pc.value = cheat.value
                with contextlib.suppress(KeyError):  # object gone: nothing to widen
                    rf.widen(p.object(pc.object).field(pc.field), cheat.value)
        self.save()


class LearnDialog(QDialog):
    """Find it next time (AC-11.1): watch, prove, optionally a memory path; then save."""

    def __init__(self, ctl: Controller, finder: FindAgain, cheat: Cheat, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Find it next time: {cheat.name}")
        self.ctl, self.finder, self.cheat = ctl, finder, cheat
        self.address = cheat.locations[0].address(ctl.session.require_game().module_base)
        tracker = ctl.live.trackers.get(cheat.anchor[0]) if cheat.anchor else None
        if tracker is not None and cheat.anchor is not None:
            self.address = tracker.field_address(cheat.anchor[1]) or self.address
        self.learned: rf.Learned | None = None
        self._cancel: threading.Event | None = None
        self._step = _Signals()
        self._step.done.connect(self._on_step)
        self._progress = _Signals()
        self._progress.done.connect(lambda f: self.progress.setValue(int(f * 100)))

        self.hint = QLabel(hints.FIND_NEXT_START.format(name=cheat.name))
        self.hint.setWordWrap(True)
        self.pointers = QCheckBox(hints.FIND_NEXT_POINTERS)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.start_btn = QPushButton("Start")
        self.cancel_btn = QPushButton("Cancel")
        self.close_btn = QPushButton("Close")
        row = QHBoxLayout()
        for w in (self.start_btn, self.cancel_btn, self.close_btn):
            row.addWidget(w)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addWidget(self.pointers)
        lay.addWidget(self.progress)
        lay.addLayout(row)
        self.start_btn.clicked.connect(self.do_start)
        self.cancel_btn.clicked.connect(self.do_cancel)
        self.close_btn.clicked.connect(self.accept)
        self._update()

    def _update(self) -> None:
        busy = self._cancel is not None
        self.start_btn.setEnabled(not busy and self.learned is None)
        self.pointers.setEnabled(not busy and self.learned is None)
        self.cancel_btn.setEnabled(busy)

    def _on_step(self, step: str) -> None:
        text = hints.FIND_NEXT_STEPS.get(step, "")
        if text:
            self.hint.setText(text.format(name=self.cheat.name))

    def do_start(self) -> None:
        cheat, session = self.cheat, self.ctl.session
        if cheat.value_type is None:
            return
        if cheat.enabled:  # our own freeze would hide the game's reads and writes
            self.ctl.live.turn_off(cheat, restore=False)
            self.ctl.cheats_changed.emit()
        obj_id = self.finder.object_id_for(cheat)
        pointer_scan = self.pointers.isChecked()
        self._cancel = threading.Event()
        cancel, addr, vt = self._cancel, self.address, cheat.value_type
        self._update()

        def work():
            learned = rf.learn(
                session,
                addr,
                vt,
                cheat.name,
                obj_id,
                pointer_scan=pointer_scan,
                step=self._step.done.emit,
                progress=self._progress.done.emit,
                cancel=cancel,
            )
            state = ""
            obj = learned.obj
            if obj.hook is not None:
                try:
                    state = ah.install(session, obj)
                except CheatMakerError as exc:
                    learned.notes.append(str(exc))
            return learned, state

        self.ctl.tasks.run(work, self._done, self._failed)

    def _done(self, result) -> None:
        self._cancel = None
        learned, state = result
        self.learned = learned
        self.progress.setValue(100)
        self.finder.adopt(self.cheat, learned, self.address, state)
        first = next(
            (m for m in ("static", "hook_slot", "chain", "vtable_scan") if m in learned.methods), ""
        )
        text = hints.READY[first].format(name=self.cheat.name) if first else ""
        if state:
            text += " " + hints.HOOK_NOTE.format(name=self.cheat.name)
        if learned.notes:
            text += " (" + " ".join(learned.notes) + ")"
        self.hint.setText(text)
        self.ctl.message.emit(text)
        self._update()

    def _failed(self, exc: BaseException) -> None:
        self._cancel = None
        self.hint.setText(hints.FIND_NEXT_START.format(name=self.cheat.name))
        self._update()
        self.ctl.fail(exc, "find_next_time")

    def do_cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()


class PickDialog(QDialog):
    """Two methods found different places (AC-12.3): nothing is written until the
    player picks the one that changes in the game."""

    def __init__(
        self, ctl: Controller, obj: pj.ObjectSpec, options: list[rf.Found], parent=None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Which one is {obj.label}?")
        self.ctl, self.obj, self.options = ctl, obj, options
        self.picked: int | None = None
        self.hint = QLabel(hints.PICK.format(name=obj.label, n=len(options)))
        self.hint.setWordWrap(True)
        self.list = QListWidget()
        self.use_btn = QPushButton("Use this one")
        self.none_btn = QPushButton("None of these")
        row = QHBoxLayout()
        row.addWidget(self.use_btn)
        row.addWidget(self.none_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)
        self.use_btn.clicked.connect(self.do_use)
        self.none_btn.clicked.connect(self.reject)
        self.timer = QTimer(self)
        self.timer.setInterval(LIVE_MS)
        self.timer.timeout.connect(self.refresh)
        self.refresh()
        self.list.setCurrentRow(0)
        self.timer.start()

    def refresh(self) -> None:
        name = self.obj.fields[0].name
        texts = []
        for i, f in enumerate(self.options):
            how = rf.METHOD_WORDS.get(f.method, f.method)
            value = fmt(read_field(self.ctl, self.obj, f.address, name))
            texts.append(f"Place {i + 1}: {value}   (found by {how})")
        if self.list.count() != len(texts):
            self.list.clear()
            self.list.addItems(texts)
        else:
            for i, t in enumerate(texts):
                self.list.item(i).setText(t)

    def do_use(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.options):
            self.picked = self.options[row].address
            log.info("action=refind_pick object=%s address=%#x", self.obj.id, self.picked)
            self.timer.stop()
            self.accept()

    def done(self, r: int) -> None:
        self.timer.stop()
        super().done(r)
