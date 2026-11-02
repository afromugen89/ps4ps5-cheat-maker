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
from pathlib import Path

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
        # Hooks this app put in and has not taken out yet, kept across reconnects and
        # game refreshes (object id -> pid): when install says "already" for one of
        # these, the bytes are ours, not CheatRunner's, and still come out on close.
        self._mine: dict[str, int] = {}
        self.ask_confirm: Callable[[str], bool] = self._ask_confirm
        self.ask_pick: Callable[[pj.ObjectSpec, list[rf.Found]], int | None] = self._ask_pick
        self.open_search: Callable[[ValueType, float | int | None], None] = lambda vt, v: None
        self.running = False
        self.sleep: Callable[[float], None] = time.sleep  # tests: no waiting
        self._lock = threading.Lock()  # installed/_ours are written by worker threads too
        self._asking = False
        self._ask_still: Callable[[], bool] = lambda: True  # the open question still holds
        self._unreadable: Path | None = None  # a .cmproj that failed to load: kept on save
        self.by_chain: set[str] = set()  # found by a memory path: no hook needed
        self._pid: int | None = None  # the game process the bookkeeping is for
        ctl.game_changed.connect(self.on_game)
        ctl.before_disconnect.append(self.remove_hooks)
        self.pending_timer = QTimer(self)
        self.pending_timer.setInterval(1000)
        self.pending_timer.timeout.connect(self.check_pending)
        self.pending_timer.start()

    # -- hooks we put in ----------------------------------------------------------
    def record(self, obj: pj.ObjectSpec, pid: int) -> None:
        """Called the moment our install returns, from any thread, so a disconnect or
        an error later in the same job still takes the hook out."""
        with self._lock:
            self.installed[obj.id] = obj
            self._ours[obj.id] = pid
            self._mine[obj.id] = pid

    def note_install(self, obj: pj.ObjectSpec, state: str, pid: int) -> None:
        """Record the result of ``ah.install``: "installed" is ours; "already" is ours
        too when this app put it in earlier (a Connect again, lost link or refresh)."""
        with self._lock:
            mine = self._mine.get(obj.id) == pid
        if state == "installed" or (state == "already" and mine):
            self.record(obj, pid)

    def _unrecord(self, obj_id: str) -> None:
        with self._lock:
            self.installed.pop(obj_id, None)
            self._ours.pop(obj_id, None)

    def is_ours(self, obj_id: str, pid: int) -> bool:
        with self._lock:
            return self._ours.get(obj_id) == pid

    # -- project -----------------------------------------------------------------
    def ensure_project(self) -> pj.Project:
        g = self.ctl.session.require_game()
        if self.ctl.project is None:
            self.ctl.project = pj.Project(g.title_id, g.version, g.process)
        return self.ctl.project

    def keep_rooms(self) -> None:
        """New caves and slots never go where the notes' hooks live (even unused)."""
        p = self.ctl.project
        self.ctl.session.reserved_rooms = ah.reserved_rooms(p.objects) if p else []

    def save(self) -> None:
        self.keep_rooms()
        p = self.ctl.project
        if p is None:
            return
        path = pj.project_path(p.title_id, p.version)
        try:
            if path == self._unreadable:  # never overwrite it: a newer app may need it
                bak = pj.set_aside(path)
                self._unreadable = None
                if bak is not None:
                    log.info("action=project_save kept unreadable file as %s", bak.name)
            pj.save(path, p)
        except OSError as exc:
            log.warning("action=project_save reason=%s", exc)

    def on_game(self, game: GameInfo | None) -> None:
        # A Refresh that sees the same game process (e.g. from the PS home screen,
        # where the version can't be read) keeps our hooks: they are still in it and
        # must still come out on close. Only another process, or no game, forgets them.
        same = game is not None and game.pid == self._pid
        self._pid = game.pid if game is not None else None
        if not same:
            with self._lock:
                self.installed.clear()
                self._ours.clear()
            self.by_chain.clear()
        if game is None:
            self.ctl.project = None
            self.keep_rooms()
            return
        path = pj.project_path(game.title_id, game.version)
        self._unreadable = None
        try:
            self.ctl.project = pj.load(path)
        except CheatMakerError as exc:
            self.ctl.project = None
            self._unreadable = path
            self.ctl.fail(exc, "project_load")
            return
        finally:
            self.keep_rooms()
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

        def work():
            pid = session.require_game().pid
            results = []
            for obj in objects:
                state = ""
                if obj.anchors_of(pj.ChainAnchor):  # memory paths first: no code change
                    out = rf.find_again(session, obj, use_hook=False, use_scan=False)
                    if out.found:
                        self.by_chain.add(obj.id)
                        results.append((obj, state, out))
                        continue
                self.by_chain.discard(obj.id)
                if use_hook and obj.hook is not None and not obj.has_proven_chain:
                    try:  # D14: the app puts the hook in on connect
                        state = ah.install(session, obj)
                    except CheatMakerError as exc:
                        log.warning("action=hook_install object=%s reason=%s", obj.id, exc)
                    self.note_install(obj, state, pid)
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
        _pid, results = result
        p = self.ctl.project
        lines = []
        for obj, _state, out in results:
            if out.status == "disagree":
                picked = self.ask_pick(obj, out.options)
                out = rf.confirm_pick(obj, picked) if picked is not None else out
            if p is not None:
                rf.remember(p, out)
            if out.found and out.address is not None:
                tracker = rf.Tracker(self.ctl.session, obj, out.address)
                tracker.confirmed = out.method == "picked"
                tracker.on_lost = lambda o=obj: self.path_lost(o)
                self.ctl.live.trackers[obj.id] = tracker
                f = obj.fields[0]
                f.last = read_field(self.ctl, obj, out.address, f.name)
                how = rf.METHOD_WORDS.get(out.method, out.method)
                lines.append(hints.FOUND_AGAIN.format(name=obj.label, how=how, secs=out.seconds))
            else:
                f = obj.fields[0]
                lines.append(hints.NOT_FOUND.format(name=obj.label))
                self.open_search(f.vtype, f.last)
        if p is not None and p.dropped_hooks:
            lines.append(hints.OLD_NOTES)
            p.dropped_hooks = []
        self.save()
        self.ctl.cheats_changed.emit()
        if lines:
            self.ctl.message.emit(" ".join(lines))

    def remove_hooks(self) -> None:
        with self._lock:
            objs = list(self.installed.values())
        for obj in objs:
            self._remove(obj)
        with self._lock:
            self.installed.clear()
            self._ours.clear()

    def _remove(self, obj: pj.ObjectSpec) -> None:
        try:
            ah.remove(self.ctl.session, obj, self.sleep)
        except CheatMakerError as exc:
            log.warning("action=hook_remove object=%s reason=%s", obj.id, exc)
        else:
            with self._lock:
                self._mine.pop(obj.id, None)
        self._unrecord(obj.id)

    def path_lost(self, obj: pj.ObjectSpec) -> None:
        """A value found by a memory path stopped checking out (it moved). Simple games
        stay simple only while the path works: now put the hook in (it finds the new
        place on the next freeze tick), or tell the player to press Find it again."""
        with self._lock:  # runs on the freezer thread: decide once
            if obj.id not in self.by_chain:
                return
            self.by_chain.discard(obj.id)
        session = self.ctl.session
        if obj.hook is not None and session.connected:
            try:
                state = ah.install(session, obj)
                self.note_install(obj, state, session.require_game().pid)
            except CheatMakerError as exc:
                log.warning("action=hook_install object=%s reason=%s", obj.id, exc)
            else:
                self.ctl.message.emit(hints.PATH_LOST_HOOK.format(name=obj.label))
                return
        self.ctl.message.emit(hints.PATH_LOST.format(name=obj.label))

    def before_on(self, cheat: Cheat) -> None:
        """Turning on a cheat that is found again: put its hook back if it was taken
        out when the last one went off, so the value keeps being followed."""
        if cheat.anchor is None or not self.ctl.session.connected:
            return
        tracker = self.ctl.live.trackers.get(cheat.anchor[0])
        obj = tracker.obj if tracker is not None else None
        if obj is None or obj.hook is None or obj.has_proven_chain or obj.id in self.installed:
            return
        if obj.id in self.by_chain:
            return
        try:
            state = ah.install(self.ctl.session, obj)
            self.note_install(obj, state, self.ctl.session.require_game().pid)
        except CheatMakerError as exc:
            log.warning("action=hook_install object=%s reason=%s", obj.id, exc)

    def after_off(self, cheat: Cheat) -> None:
        """AC-11.2: the hook comes out when the last cheat on its value is turned off.
        A code cheat on the same place (PC-off) takes our hook out with it when it goes
        off, so hooks we still need are put back."""
        if not self.ctl.session.connected:
            return
        cheats = self.ctl.cheat_file.cheats if self.ctl.cheat_file else []
        if cheat.anchor is not None:
            obj_id = cheat.anchor[0]
            still_on = any(c.enabled and c.anchor and c.anchor[0] == obj_id for c in cheats)
            obj = self.installed.get(obj_id)
            if obj is not None and not still_on:
                self._remove(obj)
            return
        for obj in list(self.installed.values()):
            try:
                if not ah.is_installed(self.ctl.session, obj):
                    ah.install(self.ctl.session, obj)
            except CheatMakerError as exc:
                log.warning("action=hook_reinstall object=%s reason=%s", obj.id, exc)

    # -- a weak object moved: ask before following it ---------------------------------
    def check_pending(self) -> None:
        if self._asking or not self.ctl.session.connected:
            return
        cheats = self.ctl.cheat_file.cheats if self.ctl.cheat_file else []
        for obj_id, tracker in list(self.ctl.live.trackers.items()):
            if tracker.pending is None:
                continue
            mine = [c for c in cheats if c.enabled and c.anchor and c.anchor[0] == obj_id]
            if not mine:
                continue
            pend = tracker.pending
            value = read_field(self.ctl, tracker.obj, pend, mine[0].anchor[1])
            self._asking = True
            # The freezer keeps checking while the question is open: if only one
            # object still verifies the question answers itself and the box closes.
            self._ask_still = lambda t=tracker, p=pend: t.pending == p
            try:
                ok = self.ask_confirm(
                    hints.CONFIRM_MOVED.format(name=tracker.obj.label, value=fmt(value))
                )
            finally:
                self._asking = False
                self._ask_still = lambda: True
            if tracker.pending != pend:  # answered itself meanwhile: the reply is moot
                continue
            if ok:
                tracker.accept_pending(pend)
                continue
            kept = tracker.pending_both
            tracker.reject_pending(pend)
            if kept:  # the first one still checks out: keep changing it
                self.ctl.message.emit(hints.KEPT_FIRST.format(name=tracker.obj.label))
                continue
            for c in mine:
                self.ctl.live.turn_off(c, restore=False)
            self.ctl.message.emit(hints.CONFIRM_NO.format(name=tracker.obj.label))
            self.ctl.cheats_changed.emit()

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
        box = QMessageBox(QMessageBox.Icon.Question, "Is this it?", text)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        watch = QTimer(box)
        watch.setInterval(250)
        watch.timeout.connect(lambda: None if self._ask_still() else box.done(0))
        watch.start()
        return box.exec() == QMessageBox.StandardButton.Yes

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
        self.pc_off_btn = QPushButton("Also make a PC-off cheat")
        self.pc_off_btn.setToolTip(hints.PC_OFF_TIP)
        row = QHBoxLayout()
        for w in (self.start_btn, self.cancel_btn, self.pc_off_btn, self.close_btn):
            row.addWidget(w)
        lay = QVBoxLayout(self)
        lay.addWidget(self.hint)
        lay.addWidget(self.pointers)
        lay.addWidget(self.progress)
        lay.addLayout(row)
        self.start_btn.clicked.connect(self.do_start)
        self.cancel_btn.clicked.connect(self.do_cancel)
        self.close_btn.clicked.connect(self.accept)
        self.pc_off_btn.clicked.connect(self.do_pc_off)
        self._update()

    def _update(self) -> None:
        busy = self._cancel is not None
        self.start_btn.setEnabled(not busy and self.learned is None)
        self.pointers.setEnabled(not busy and self.learned is None)
        self.cancel_btn.setEnabled(busy)
        hook = self.learned.obj.hook if self.learned is not None else None
        self.pc_off_btn.setVisible(hook is not None and hook.filter.kind != "none")

    def do_pc_off(self) -> None:
        """AC-13.1: one routine that finds the value and keeps it at the cheat's value
        for you only. Plain code lines, so it saves to GoldHEN / CheatRunner."""
        if self.learned is None or self.cheat.value is None or self.cheat.value_type is None:
            return
        try:
            pc_off = ah.hook_cheat(
                self.ctl.session,
                self.learned.obj,
                f"{self.cheat.name} (works with the PC off)",
                self.cheat.value_type.encode(self.cheat.value),
            )
        except CheatMakerError as exc:
            self.ctl.fail(exc, "pc_off_cheat")
            return
        self.ctl.ensure_file().cheats.append(pc_off)
        self.ctl.cheats_changed.emit()
        self.pc_off_btn.setEnabled(False)
        self.ctl.message.emit(hints.PC_OFF_MADE.format(name=pc_off.name))

    def _on_step(self, step: str) -> None:
        text = hints.FIND_NEXT_STEPS.get(step, "")
        if text:
            self.hint.setText(text.format(name=self.cheat.name))

    def do_start(self) -> None:
        cheat, session, finder = self.cheat, self.ctl.session, self.finder
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
            if obj.anchors_of(pj.ChainAnchor):  # a memory path: simple games stay simple
                finder.by_chain.add(obj.id)
            elif obj.hook is not None:
                try:
                    state = ah.install(session, obj)
                except CheatMakerError as exc:
                    learned.notes.append(str(exc))
                # "already" = loaded by CheatRunner: not ours, unless we put it in
                finder.note_install(obj, state, session.require_game().pid)
            return learned, state

        self.ctl.tasks.run(work, self._done, self._failed)

    def _done(self, result) -> None:
        self._cancel = None
        learned, state = result
        self.learned = learned
        self.progress.setValue(100)
        self.finder.adopt(self.cheat, learned, self.address, state)
        first = next(
            (m for m in ("static", "chain", "hook_slot", "vtable_scan") if m in learned.methods), ""
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
