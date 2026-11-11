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
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
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
from cheatmaker.core import carry as cr
from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.session import GameInfo
from cheatmaker.core.values import ValueType
from cheatmaker.errors import CheatMakerError, ConnectionLost, FindFailed, ScanCancelled
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


@dataclass
class PartLearned:
    """One moving part of a combined cheat after Find it next time: its line, the
    object it was found in (shared by parts in the same object), its field and where
    the object is now."""

    loc: Location
    obj: pj.ObjectSpec
    field: str
    obj_addr: int


def part_field(cheat: Cheat, obj_id: str) -> tuple[str, str]:
    """(field, label) of the first part of ``cheat`` found again by ``obj_id``: the
    label is "fraction (part of Health)" for a combined cheat."""
    if cheat.anchor is not None:
        return cheat.anchor[1], cheat.name
    for loc in cheat.learned_parts:
        if loc.anchor is not None and loc.anchor[0] == obj_id:
            return loc.anchor[1], hints.PART_OF.format(part=loc.part, name=cheat.name)
    raise KeyError(obj_id)


class PartFailed(FindFailed):
    """A part of a combined cheat couldn't be learned (or Cancel was pressed): every
    hook put in during the dialog is out again and nothing was changed. ``stuck``: a
    hook couldn't be taken out (it still comes out on close)."""

    def __init__(self, part: str, k: int, n: int, reason: str, stuck: bool = False) -> None:
        text = hints.LEARN_PART_STUCK if stuck else hints.LEARN_PART_FAILED
        super().__init__(text.format(part=part, k=k, n=n, reason=reason))
        self.part, self.k, self.n, self.stuck = part, k, n, stuck


def _reason(exc: BaseException) -> str:
    """Why a part couldn't be learned, to follow "Couldn't learn x (part k of n): "."""
    if isinstance(exc, ScanCancelled):
        return hints.LEARN_PART_CANCELLED
    notes = getattr(exc, "notes", None)
    text = " ".join(notes) if notes else str(exc)
    text = text.strip() or "it couldn't be done."
    if not text.startswith("Cheat Maker") and text[1:2].islower():
        text = text[0].lower() + text[1:]
    return text if text.endswith(".") else text + "."


def _join(obj: pj.ObjectSpec, other: pj.ObjectSpec, fld: pj.Field) -> None:
    """Add ``fld`` (and the memory paths to it) from ``other`` to ``obj``, the same
    object. A name ``obj`` already has gets " (2)", ..."""
    old, n = fld.name, 2
    names = {f.name for f in obj.fields}
    while fld.name in names:
        fld.name = f"{old} ({n})"
        n += 1
    obj.fields.append(fld)
    for a in other.anchors_of(pj.ChainAnchor):
        if a.field == old:
            a.field = fld.name
        obj.anchors.append(a)


class FindAgain(QObject):
    """Loads the game's notes on connect, puts the hooks in, finds every value again,
    and keeps the live cheats tied to where the values are now."""

    notes_changed = Signal()  # the notes were saved (fingerprints may need learning)
    carrying_changed = Signal(bool)  # a bring-over started or ended

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
        self._carrying = False
        ctl.game_changed.connect(self.on_game)
        ctl.before_disconnect.append(self.remove_hooks)
        self.pending_timer = QTimer(self)
        self.pending_timer.setInterval(1000)
        self.pending_timer.timeout.connect(self.check_pending)
        self.pending_timer.start()

    @property
    def carrying(self) -> bool:
        """Notes are being brought over from another version: Find it next time,
        Combine, Find it again, Lock it and Make it work wait (spec AC-U3)."""
        return self._carrying

    @carrying.setter
    def carrying(self, on: bool) -> None:
        if on != self._carrying:
            self._carrying = on
            self.carrying_changed.emit(on)

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
        """New caves and slots never go where the notes' hooks live (even unused), nor
        where a hook of ours still is that the notes don't hold (one that couldn't be
        taken out after a failed Find it next time), nor where the notes' combined
        cheats keep new code over spare zeros."""
        p = self.ctl.project
        objects = list(p.objects) if p else []
        in_notes = {o.id for o in objects}
        with self._lock:
            objects += [o for i, o in self.installed.items() if i not in in_notes]
        parts = cr.part_rooms(p) if p else []
        self.ctl.session.reserved_rooms = ah.reserved_rooms(objects) + parts

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
            return
        self.notes_changed.emit()

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
        """Live cheats saved in the notes come back (unticked) in My cheats; a combined
        cheat comes back as one row, never as rows of its own parts."""
        p = self.ctl.project
        if p is None:
            return
        cf = self.ctl.ensure_file()
        have = {c.anchor for c in cf.cheats if c.anchor}
        rows = [c for c in cf.cheats if c.learned_parts]
        have_parts = {self._key(c.name, self._anchors(c)) for c in rows}
        noted = {self._key(pc.name, pc.anchors) for pc in p.cheats if pc.parts}
        # Rows no notes entry names: renamed while the notes weren't loaded.
        unnamed = [
            c for c in rows if c.found_again and self._key(c.name, self._anchors(c)) not in noted
        ]
        renamed = False
        for pc in p.cheats:
            if pc.parts:
                if self._key(pc.name, pc.anchors) in have_parts:
                    continue
                row = next((c for c in unnamed if self._anchors(c) == pc.anchors), None)
                if row is not None:  # that row is this entry: keep the row's new name
                    unnamed.remove(row)
                    pc.name = row.name
                    have_parts.add(self._key(pc.name, pc.anchors))
                    renamed = True
                    continue
                cheat = self._combined_from(p, pc)
                if cheat is not None:
                    cf.cheats.append(cheat)
                    have_parts.add(self._key(pc.name, pc.anchors))
                continue
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
        self.mark_carried()
        if renamed:
            self.save()

    def _entry(self, cheat: Cheat) -> pj.ProjectCheat | None:
        """The notes entry ``cheat`` comes from, if any."""
        p = self.ctl.project
        if p is None:
            return None
        if cheat.learned_parts:
            key = self._key(cheat.name, self._anchors(cheat))
            return next(
                (pc for pc in p.cheats if pc.parts and self._key(pc.name, pc.anchors) == key), None
            )
        if cheat.anchor is not None:
            return next(
                (pc for pc in p.cheats if not pc.parts and (pc.object, pc.field) == cheat.anchor),
                None,
            )
        return None

    def mark_carried(self) -> None:
        """Each cheat's "brought over" state from the notes (spec AC-U8): "test" until
        its brought-over code parts were ticked once, "check" until every object it
        uses was confirmed."""
        p = self.ctl.project
        cheats = self.ctl.cheat_file.cheats if self.ctl.cheat_file else []
        checking = {o.id for o in p.objects if o.check} if p is not None else set()
        for c in cheats:
            pc = self._entry(c)
            if pc is not None and pc.test:
                c.carried = "test"
            elif pc is not None and any(o in checking for o in c.objects):
                c.carried = "check"
            else:
                c.carried = ""

    @staticmethod
    def _combined_from(p: pj.Project, pc: pj.ProjectCheat) -> Cheat | None:
        """A combined cheat from its notes entry, or None if a part's object or field
        is gone (then the whole cheat is left out)."""
        locs = []
        try:
            for part in pc.parts:
                if part.object is not None and part.field is not None:
                    f = p.object(part.object).field(part.field)
                    raw = f.vtype.encode(part.value)
                    locs.append(
                        Location(
                            LocationKind.SESSION_ADDRESS,
                            0,
                            raw,
                            raw,
                            anchor=(part.object, part.field),
                            vtype=f.vtype,
                            part=part.name,
                        )
                    )
                else:
                    locs.append(
                        Location(
                            LocationKind(part.kind),
                            part.offset,
                            part.on,
                            part.off,
                            part.module,
                            part.section,
                            vtype=part.vtype,
                            part=part.name,
                        )
                    )
        except (KeyError, ValueError, OverflowError) as exc:
            log.warning("action=project_cheat name=%r skipped: %s", pc.name, exc)
            return None
        return Cheat(
            pc.name,
            locs,
            freeze=pc.action == "freeze",
            hint="Found again each time you connect.",
        )

    @staticmethod
    def _anchors(cheat: Cheat) -> list[tuple[str, str]]:
        return [loc.anchor for loc in cheat.learned_parts if loc.anchor is not None]

    @staticmethod
    def _key(name: str, anchors: list[tuple[str, str]]) -> tuple:
        """A combined cheat's notes entry: its name and learned parts (two combined
        cheats may share the same parts)."""
        return (name, tuple(anchors))

    def _drop_entry(self, p: pj.Project, name: str, anchors: list[tuple[str, str]]) -> None:
        key = self._key(name, anchors)
        p.cheats = [c for c in p.cheats if not c.parts or self._key(c.name, c.anchors) != key]

    @staticmethod
    def uses(cheat: Cheat, obj_id: str) -> bool:
        """``cheat`` is found again (wholly or in part) by the object ``obj_id``."""
        return obj_id in cheat.objects

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
        cheats = self.ctl.cheat_file.cheats if self.ctl.cheat_file else []
        combined = [c for c in cheats if c.learned_parts]
        # Objects only combined cheats use get one line per combined cheat instead.
        single = {c.anchor[0] for c in cheats if c.anchor is not None}
        in_combined = {o for c in combined for o in c.objects} - single
        lines = []
        seconds: dict[str, float] = {}
        for obj, _state, out in results:
            if out.status == "disagree":
                picked = self.ask_pick(obj, out.options)
                out = rf.confirm_pick(obj, picked) if picked is not None else out
            if p is not None:
                rf.remember(p, out)
            if out.found and out.address is not None:
                self._track(obj, out.address, confirmed=out.method == "picked")
                seconds[obj.id] = out.seconds
                if obj.id in in_combined:
                    continue
                how = rf.METHOD_WORDS.get(out.method, out.method)
                lines.append(hints.FOUND_AGAIN.format(name=obj.label, how=how, secs=out.seconds))
            elif obj.id not in in_combined:
                f = obj.fields[0]
                lines.append(hints.NOT_FOUND.format(name=obj.label))
                self.open_search(f.vtype, f.last)
        lines += [self._combined_line(c, seconds) for c in combined]
        if p is not None and p.dropped_hooks:
            lines.append(hints.OLD_NOTES)
            p.dropped_hooks = []
        self.save()
        self.ctl.cheats_changed.emit()
        if lines:
            self.ctl.message.emit(" ".join(lines))

    def _track(self, obj: pj.ObjectSpec, addr: int, confirmed: bool) -> rf.Tracker:
        """Follow ``obj`` from ``addr`` for the live cheats. A brought-over object
        (``check``) is never confirmed here, even after a pick: its question asks
        about the version it came from (spec AC-U8)."""
        tracker = rf.Tracker(self.ctl.session, obj, addr)
        tracker.confirmed = confirmed and not obj.check
        tracker.on_lost = lambda o=obj: self.path_lost(o)
        self.ctl.live.trackers[obj.id] = tracker
        for f in obj.fields:
            f.last = read_field(self.ctl, obj, addr, f.name)
        return tracker

    def adopt_carried(
        self,
        project: pj.Project,
        found: dict[str, int | None],
        merge_into: pj.Project | None = None,
    ) -> None:
        """Notes brought over from another version become this version's notes (or,
        with ``merge_into``, are added to them): their cheats come back in My cheats
        (unticked, "check it"), found objects are followed unconfirmed, and the notes
        are saved. Hooks put in during the bring-over were recorded with note_install."""
        if merge_into is not None:
            merge_into.objects += project.objects
            merge_into.cheats += project.cheats
            if merge_into.carried_from is None:
                merge_into.carried_from = project.carried_from
            self.ctl.project = merge_into
        else:
            self.ctl.project = project
        self.ensure_cheats()
        for obj in project.objects:
            addr = found.get(obj.id)
            if not addr:
                continue
            self._track(obj, addr, confirmed=False)
            if obj.hook is None and obj.anchors_of(pj.ChainAnchor):
                with self._lock:
                    self.by_chain.add(obj.id)
        self.save()
        self.ctl.cheats_changed.emit()

    def carried_yes(self, obj: pj.ObjectSpec, addr: int) -> None:
        """The player said Yes to a brought-over object: it is confirmed, and its class
        is learned again from its first 8 bytes when they point into the game's module
        (memory search and the wide ranges come back, spec AC-U5). Saved."""
        obj.check = False
        if obj.relearn_class:
            obj.relearn_class = False
            try:
                vt = ah.object_vtable(self.ctl.session, addr)
            except CheatMakerError as exc:
                log.warning("action=carry_confirm object=%s reason=%s", obj.id, exc)
                vt = None
            if vt is not None:
                obj.vtable = vt
                if not obj.anchors_of(pj.VtableAnchor):
                    obj.anchors.append(pj.VtableAnchor())
                for f in obj.fields:
                    if f.range is not None:
                        f.range = rf.class_range(f.vtype, f.last if f.last is not None else 0)
        log.info(
            "action=carry_confirm object=%s class=%s",
            obj.id,
            "-" if obj.vtable is None else f"{obj.vtable:#x}",
        )
        self.mark_carried()
        self.save()

    def tested(self, cheat: Cheat) -> None:
        """A cheat with brought-over code parts was ticked and turned on: it no longer
        says "test it"; the player is told to check it in the game."""
        pc = self._entry(cheat)
        if pc is None or not pc.test:
            return
        pc.test = False
        self.mark_carried()
        self.save()
        self.ctl.message.emit(hints.CARRY_TEST_HINT)

    def _carried_version(self) -> str:
        p = self.ctl.project
        return p.carried_from if p is not None and p.carried_from else "?"

    def _combined_line(self, cheat: Cheat, seconds: dict[str, float]) -> str:
        """One status line for a combined cheat: all its parts found, or the first part
        that isn't (its search opens with that part's type and last value)."""
        p = self.ctl.project
        for loc in cheat.learned_parts:
            if loc.anchor is None or loc.anchor[0] in seconds:
                continue
            last = None
            vtype = loc.vtype
            if p is not None:
                with contextlib.suppress(KeyError):
                    f = p.object(loc.anchor[0]).field(loc.anchor[1])
                    vtype, last = f.vtype, f.last
            if vtype is not None:
                self.open_search(vtype, last)
            return hints.COMBINED_NOT_FOUND.format(part=loc.part, name=cheat.name)
        secs = sum(seconds[o] for o in cheat.objects)
        n = len(cheat.learned_parts)
        parts = hints.PART_COUNT[n == 1].format(n=n)
        return hints.COMBINED_FOUND.format(name=cheat.name, parts=parts, secs=secs)

    def remove_hooks(self) -> None:
        with self._lock:
            objs = list(self.installed.values())
        for obj in objs:
            self._remove(obj)
        with self._lock:
            self.installed.clear()
            self._ours.clear()

    def _remove(self, obj: pj.ObjectSpec) -> None:
        if not self.take_out(obj):
            self._unrecord(obj.id)

    def take_out(self, obj: pj.ObjectSpec) -> bool:
        """Take ``obj``'s hook out. False if it couldn't be: then it stays recorded as
        ours (another try on close)."""
        try:
            ah.remove(self.ctl.session, obj, self.sleep)
        except CheatMakerError as exc:
            log.warning("action=hook_remove object=%s reason=%s", obj.id, exc)
            return False
        with self._lock:
            self._mine.pop(obj.id, None)
        self._unrecord(obj.id)
        return True

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
        """Turning on a cheat that is found again: put its hooks back if they were
        taken out when the last one went off, so the values keep being followed."""
        if not self.ctl.session.connected:
            return
        for obj_id in cheat.objects:
            tracker = self.ctl.live.trackers.get(obj_id)
            obj = tracker.obj if tracker is not None else None
            if obj is None or obj.hook is None or obj.has_proven_chain:
                continue
            if obj.id in self.installed or obj.id in self.by_chain:
                continue
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
        if cheat.objects:
            for obj_id in cheat.objects:
                still_on = any(c.enabled and self.uses(c, obj_id) for c in cheats)
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
            mine = [c for c in cheats if c.enabled and self.uses(c, obj_id)]
            if not mine:
                continue
            pend = tracker.pending
            field_name, label = part_field(mine[0], obj_id)
            if not mine[0].combined:
                label = tracker.obj.label
            value = read_field(self.ctl, tracker.obj, pend, field_name)
            self._asking = True
            # The freezer keeps checking while the question is open: if only one
            # object still verifies the question answers itself and the box closes.
            self._ask_still = lambda t=tracker, p=pend: t.pending == p
            try:
                ok = self.ask_confirm(hints.CONFIRM_MOVED.format(name=label, value=fmt(value)))
            finally:
                self._asking = False
                self._ask_still = lambda: True
            if tracker.pending != pend:  # answered itself meanwhile: the reply is moot
                continue
            if ok:
                tracker.accept_pending(pend)
                if tracker.obj.check and tracker.address == pend:
                    self.carried_yes(tracker.obj, pend)
                    self.ctl.cheats_changed.emit()
                continue
            kept = tracker.pending_both
            tracker.reject_pending(pend)
            if kept:  # the first one still checks out: keep changing it
                self.ctl.message.emit(hints.KEPT_FIRST.format(name=label))
                continue
            for c in mine:  # the whole cheat, every part
                self.ctl.live.turn_off(c, restore=False)
            self.ctl.message.emit(hints.CONFIRM_NO.format(name=label))
            self.ctl.cheats_changed.emit()

    # -- confirm before the first write (AC-12.2) -----------------------------------
    def confirm(self, cheat: Cheat) -> bool:
        """True when the cheat may write. Asks once per run per object."""
        if cheat.learned_parts:
            return self._confirm_parts(cheat)
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
        obj = tracker.obj
        if obj.check:  # brought over from another version (spec AC-U8)
            text = hints.CONFIRM_CARRIED.format(
                name=obj.label, version=self._carried_version(), value=fmt(value)
            )
        else:
            text = hints.CONFIRM.format(name=obj.label, value=fmt(value))
        if self.ask_confirm(text):
            tracker.confirmed = True
            if obj.check:
                self.carried_yes(obj, addr)
            return True
        no = hints.CONFIRM_NO_CARRIED if obj.check else hints.CONFIRM_NO
        self.ctl.message.emit(no.format(name=obj.label))
        return False

    def _confirm_parts(self, cheat: Cheat) -> bool:
        """One question for the whole combined cheat, naming each learned part's value
        now. Skipped when every part's object was already confirmed this run. A part
        not found right now: True, and turn_on refuses with a plain message."""
        trackers = [self.ctl.live.trackers.get(o) for o in cheat.objects]
        if any(t is None for t in trackers) or all(t.confirmed for t in trackers if t):
            return True
        values = []
        where: dict[str, int] = {}
        for loc in cheat.learned_parts:
            assert loc.anchor is not None
            tracker = self.ctl.live.trackers[loc.anchor[0]]
            addr = tracker.current()
            if addr is None:
                return True
            where[tracker.obj.id] = addr
            value = read_field(self.ctl, tracker.obj, addr, loc.anchor[1])
            values.append(hints.CONFIRM_PART_VALUE.format(part=loc.part, value=fmt(value)))
        text = hints.CONFIRM_PARTS.format(name=cheat.name, values=", ".join(values))
        carried = [t.obj for t in trackers if t is not None and t.obj.check]
        if carried:  # one question for the whole cheat, naming the old version
            text = hints.CARRIED_FROM.format(version=self._carried_version()) + " " + text
        if self.ask_confirm(text):
            for t in trackers:
                if t is not None:
                    t.confirmed = True
            for obj in carried:
                self.carried_yes(obj, where[obj.id])
            return True
        no = hints.CONFIRM_NO_CARRIED if carried else hints.CONFIRM_NO
        self.ctl.message.emit(no.format(name=cheat.name))
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
            if pc.name == cheat.name and not pc.parts:
                return pc.object  # a new find updates the anchor (AC-12.4)
        return self.new_object_ids(1)[0]

    def new_object_ids(self, n: int) -> list[str]:
        """``n`` object ids not used in the notes yet, nor by a hook of ours still in
        the game (one per part to learn)."""
        p = self.ensure_project()
        used = {o.id for o in p.objects}
        with self._lock:
            used |= set(self.installed) | set(self._mine)
        ids: list[str] = []
        k = 1
        while len(ids) < n:
            if f"obj{k}" not in used:
                ids.append(f"obj{k}")
            k += 1
        return ids

    def adopt(self, cheat: Cheat, learned: rf.Learned, value_addr: int, state: str) -> None:
        p = self.ensure_project()
        obj = learned.obj
        f = obj.fields[0]
        if cheat.value is not None:
            rf.widen(f, cheat.value)
        p.put_object(obj)
        p.cheats = [c for c in p.cheats if c.parts or (c.name != cheat.name and c.object != obj.id)]
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

    def forget(self, cheat: Cheat, save: bool = True) -> None:
        """A deleted cheat doesn't come back next time (the object's notes stay).
        ``save=False``: the caller saves once after several changes."""
        p = self.ctl.project
        if p is None:
            return
        if cheat.learned_parts:
            self._drop_entry(p, cheat.name, self._anchors(cheat))
        elif cheat.anchor is not None:
            p.cheats = [c for c in p.cheats if c.parts or (c.object, c.field) != cheat.anchor]
        else:
            return
        if save:
            self.save()

    def renamed(self, cheat: Cheat, old: str) -> None:
        """A combined cheat's new name is kept in its notes entry (its key)."""
        p = self.ctl.project
        if p is None or not cheat.learned_parts:
            return
        key = self._key(old, self._anchors(cheat))
        for pc in p.cheats:
            if pc.parts and self._key(pc.name, pc.anchors) == key:
                pc.name = cheat.name
                self.save()
                return

    def remember_combined(self, cheat: Cheat, save: bool = True) -> None:
        """Save a combined cheat with nothing moving as one notes entry (its parts in
        order), replacing an entry with the same name and learned parts."""
        p = self.ctl.project
        if p is None or not cheat.found_again or cheat.anchor is not None:
            return
        parts = []
        for loc in cheat.locations:
            if loc.anchor is None:
                parts.append(
                    pj.ProjectPart(
                        loc.part or "",
                        kind=loc.kind.value,
                        module=loc.module,
                        section=loc.section,
                        offset=loc.offset,
                        on=loc.on,
                        off=loc.off,
                        vtype=loc.vtype,
                    )
                )
                continue
            obj_id, name = loc.anchor
            try:
                f = p.object(obj_id).field(name)
            except KeyError:
                log.warning("action=project_cheat name=%r part=%s gone", cheat.name, loc.part)
                return
            value = f.vtype.decode(loc.on)
            rf.widen(f, value)
            parts.append(pj.ProjectPart(loc.part or name, obj_id, name, value))
        self._drop_entry(p, cheat.name, self._anchors(cheat))
        p.cheats.append(
            pj.ProjectCheat(cheat.name, action="freeze" if cheat.freeze else "set", parts=parts)
        )
        if save:
            self.save()

    def adopt_parts(self, cheat: Cheat, results: list[PartLearned]) -> None:
        """Every moving part was learned (Find it next time on a combined cheat): its
        objects go in the notes, each part becomes learned, and the cheat is saved
        once as one entry. The player just found them by hand: confirmed."""
        p = self.ensure_project()
        placed: set[str] = set()
        for r in results:
            r.loc.anchor = (r.obj.id, r.field)
            r.loc.offset = 0  # unused from now on: the tracker holds the address
            if r.obj.id in placed:  # a second part in the same object
                continue
            placed.add(r.obj.id)
            p.put_object(r.obj)
            tracker = self.ctl.live.trackers.get(r.obj.id)
            if tracker is None:
                tracker = rf.Tracker(self.ctl.session, r.obj, r.obj_addr)
                self.ctl.live.trackers[r.obj.id] = tracker
            else:  # an already-learned object got a field: keep following it as it is
                with tracker._lock:
                    tracker.obj = r.obj
            tracker.confirmed = True
            tracker.on_lost = lambda o=r.obj: self.path_lost(o)
        self.remember_combined(cheat)
        self.ctl.cheats_changed.emit()

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
        # A combined cheat learns each of its moving parts in turn (parts mode).
        self.parts = cheat.moving_parts if cheat.combined else []
        self.address = cheat.locations[0].address(ctl.session.require_game().module_base)
        tracker = ctl.live.trackers.get(cheat.anchor[0]) if cheat.anchor else None
        if tracker is not None and cheat.anchor is not None:
            self.address = tracker.field_address(cheat.anchor[1]) or self.address
        self.learned: rf.Learned | None = None
        self.parts_done = False
        self._cancel: threading.Event | None = None
        self._step = _Signals()
        self._step.done.connect(self._on_step)
        self._progress = _Signals()
        self._progress.done.connect(lambda f: self.progress.setValue(int(f * 100)))

        self.hint = QLabel(self._start_text())
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

    def _start_text(self) -> str:
        if not self.parts:
            return hints.FIND_NEXT_START.format(name=self.cheat.name)
        names = [loc.part or "?" for loc in self.parts]
        parts = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        return hints.FIND_NEXT_START_PARTS.format(
            parts=parts,
            secs=hints.PART_SECONDS,
            total=hints.PART_SECONDS * len(names),
            name=self.cheat.name,
        )

    def _update(self) -> None:
        busy = self._cancel is not None
        done = self.learned is not None or self.parts_done
        self.start_btn.setEnabled(not busy and not done)
        self.pointers.setEnabled(not busy and not done)
        self.cancel_btn.setEnabled(busy)
        hook = self.learned.obj.hook if self.learned is not None else None
        # No PC-off cheat for a combined cheat (spec-restart-combined AC-2).
        self.pc_off_btn.setVisible(
            not self.parts and hook is not None and hook.filter.kind != "none"
        )

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
                self.cheat.value_type,
            )
        except CheatMakerError as exc:
            self.ctl.fail(exc, "pc_off_cheat")
            return
        self.ctl.ensure_file().cheats.append(pc_off)
        self.ctl.cheats_changed.emit()
        self.pc_off_btn.setEnabled(False)
        self.ctl.message.emit(hints.PC_OFF_MADE.format(name=pc_off.name))

    def _on_step(self, step: str | tuple[int, str]) -> None:
        """A step of the single learn ("watch"), or (part number, step) in parts mode."""
        if isinstance(step, tuple):
            k, step = step
            part = self.parts[k - 1].part or ""
            text = hints.FIND_NEXT_STEPS.get(step, "")
            head = hints.FIND_NEXT_PART.format(k=k, n=len(self.parts), part=part)
            self.hint.setText(f"{head} {text.format(name=part)}".strip())
            return
        text = hints.FIND_NEXT_STEPS.get(step, "")
        if text:
            self.hint.setText(text.format(name=self.cheat.name))

    def do_start(self) -> None:
        if self.parts:
            self._start_parts()
            return
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

    # -- a combined cheat: learn every moving part in one Start ---------------------
    def _start_parts(self) -> None:
        cheat, session, finder = self.cheat, self.ctl.session, self.finder
        parts = list(self.parts)
        if any(loc.vtype is None for loc in parts):
            return
        if cheat.enabled:  # our own freeze would hide the game's reads and writes
            # Fixed parts get their bytes back; learned parts are left as they are.
            fixed = any(loc.kind is not LocationKind.SESSION_ADDRESS for loc in cheat.locations)
            self.ctl.live.turn_off(cheat, restore=fixed)
            self.ctl.cheats_changed.emit()
        ids = finder.new_object_ids(len(parts))
        seeds = self._seed_objects()
        rooms0 = list(session.reserved_rooms)
        by_chain0 = set(finder.by_chain)
        pointer_scan = self.pointers.isChecked()
        self._cancel = threading.Event()
        cancel = self._cancel
        self._update()
        n = len(parts)
        installed_here: list[pj.ObjectSpec] = []

        def rollback() -> list[pj.ObjectSpec]:
            """Every hook put in during this dialog comes out; the notes' rooms and the
            memory-path list are as before. Returns the hooks that couldn't come out:
            they stay recorded and their rooms stay kept."""
            stuck = [obj for obj in installed_here if not finder.take_out(obj)]
            with finder._lock:
                finder.by_chain.clear()
                finder.by_chain.update(by_chain0)
            session.reserved_rooms = rooms0 + ah.reserved_rooms(stuck)
            return stuck

        def work():
            try:
                results, learned, notes = self._learn_parts(parts, ids, pointer_scan, cancel, seeds)
                states = self._install_parts(
                    results, cancel, installed_here, {o.id for o, _ in seeds}, notes
                )
            except BaseException as exc:  # whatever went wrong, nothing stays changed
                stuck = rollback()
                if isinstance(exc, CheatMakerError) and not isinstance(exc, ConnectionLost):
                    at = min(max(self._part_k, 1), n)
                    part = parts[at - 1].part or "?"
                    raise PartFailed(part, at, n, _reason(exc), bool(stuck)) from exc
                raise
            return results, learned, notes, states

        self._part_k = 0
        self.ctl.tasks.run(work, self._done_parts, self._failed_parts)

    def _seed_objects(self) -> list[tuple[pj.ObjectSpec, int]]:
        """The cheat's learned parts' objects (copies) and where they start now, so a
        part found in one of them joins it. Copies: the notes change only on success."""
        seeds = []
        for obj_id in self.cheat.objects:
            tracker = self.ctl.live.trackers.get(obj_id)
            if tracker is None or tracker.address is None or tracker.obj.vtable is None:
                continue
            seeds.append((pj.ObjectSpec.from_json(tracker.obj.to_json()), tracker.address))
        return seeds

    def _learn_parts(
        self,
        parts: list[Location],
        ids: list[str],
        pointer_scan: bool,
        cancel: threading.Event,
        seeds: list[tuple[pj.ObjectSpec, int]],
    ) -> tuple[list[PartLearned], list[rf.Learned], list[str]]:
        """Learn each part in turn (one watchpoint at a time). A part in the same object
        (same start, same class) as an earlier part or as one of ``seeds`` joins that
        object: two fields, one hook."""
        session, n = self.ctl.session, len(parts)
        rooms0 = list(session.reserved_rooms)
        results: list[PartLearned] = []
        learned_all: list[rf.Learned] = []
        notes: list[str] = []
        objs: list[tuple[pj.ObjectSpec, int]] = list(seeds)  # distinct objects, start
        for k, loc in enumerate(parts, 1):
            self._part_k = k
            if cancel.is_set():
                raise ScanCancelled("Find it next time was cancelled")
            self._progress.done.emit((k - 1) / n)
            assert loc.vtype is not None and loc.part is not None
            learned = rf.learn(
                session,
                loc.offset,
                loc.vtype,
                loc.part,
                ids[k - 1],
                pointer_scan=pointer_scan,
                step=lambda st, k=k: self._step.done.emit((k, st)),
                progress=lambda f, k=k: self._progress.done.emit((k - 1 + f) / n),
                cancel=cancel,
            )
            if cancel.is_set():
                raise ScanCancelled("Find it next time was cancelled")
            fld = learned.obj.fields[0]
            start = loc.offset - fld.off
            same = next(
                (
                    o
                    for o, at in objs
                    if at == start and o.vtable is not None and o.vtable == learned.obj.vtable
                ),
                None,
            )
            if same is not None:  # two fields, one hook
                _join(same, learned.obj, fld)
                obj = same
                log.info("action=learn object=%s part=%s same object", obj.id, loc.part)
            else:
                obj = learned.obj
                objs.append((obj, start))
            results.append(PartLearned(loc, obj, fld.name, start))
            learned_all.append(learned)
            notes += learned.notes
            # The next part's hook never takes this one's spare room.
            session.reserved_rooms = rooms0 + ah.reserved_rooms([o for o, _ in objs])
            self._progress.done.emit(k / n)
        return results, learned_all, notes

    def _install_parts(
        self,
        results: list[PartLearned],
        cancel: threading.Event,
        installed_here: list[pj.ObjectSpec],
        existing: set[str],
        notes: list[str],
    ) -> list[str]:
        """Put in each new object's hook (or note its memory path), like a single
        cheat. Objects already in the notes (``existing``) are left as they are. A hook
        that can't go in is added to ``notes``. Cancel is checked before each hook goes
        in: pressed during the last one, the work is done and it ends as a success.
        Returns the install states."""
        session, finder = self.ctl.session, self.finder
        pid = session.require_game().pid
        states: list[str] = []
        seen: set[str] = set(existing)
        for k, r in enumerate(results, 1):
            obj = r.obj
            if obj.id in seen:
                continue
            seen.add(obj.id)
            self._part_k = k  # a failure now names the part whose hook goes in
            if cancel.is_set():
                raise ScanCancelled("Find it next time was cancelled")
            if obj.anchors_of(pj.ChainAnchor):
                with finder._lock:
                    finder.by_chain.add(obj.id)
                continue
            if obj.hook is None:
                continue
            state = ""
            try:
                state = ah.install(session, obj)
            except CheatMakerError as exc:
                log.warning("action=hook_install object=%s reason=%s", obj.id, exc)
                notes.append(str(exc))
            finder.note_install(obj, state, pid)
            if finder.is_ours(obj.id, pid):
                installed_here.append(obj)
            states.append(state)
        return states

    def _done_parts(self, result) -> None:
        self._cancel = None
        results, learned, notes, states = result
        self.parts_done = True
        self.progress.setValue(100)
        names = [r.loc.part for r in results]
        self.finder.adopt_parts(self.cheat, results)
        said = []
        for name, lr in zip(names, learned, strict=True):
            first = next(
                (m for m in ("static", "chain", "hook_slot", "vtable_scan") if m in lr.methods),
                "",
            )
            if first:
                said.append(hints.READY_PART[first].format(name=name))
        text = hints.READY_PARTS.format(parts="; ".join(said))
        if any(states):
            text += " " + hints.HOOK_NOTE.format(name=self.cheat.name)
        if notes:
            text += " (" + " ".join(notes) + ")"
        self.hint.setText(text)
        self.ctl.message.emit(text)
        self._update()

    def _failed_parts(self, exc: BaseException) -> None:
        self._cancel = None
        self.hint.setText(str(exc) if isinstance(exc, PartFailed) else self._start_text())
        self.progress.setValue(0)
        self._update()
        self.ctl.fail(exc, "find_next_time")


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
