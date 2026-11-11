"""Bring your notes to a new game version (docs/spec-game-update.md): the offer bar, the
run driver and the background fingerprint learner.

Core logic lives in ``core.carry``; this module decides when to run each step and what
the player sees. No "vtable", "cave" or "pointer" words here (AC-8.2).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QProgressBar,
    QPushButton,
    QWidget,
)

from cheatmaker.core import carry as cr
from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import LocationKind, versions_differ
from cheatmaker.core.session import GameInfo
from cheatmaker.errors import CheatMakerError, ConnectionLost, ScanCancelled
from cheatmaker.ui import hints
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.find_again import FindAgain
from cheatmaker.ui.tasks import _Signals

log = logging.getLogger(__name__)


def open_code_lines(ctl: Controller) -> list[tuple[int, bytes]]:
    """(offset, original bytes) of every code line in My cheats: the game's own bytes
    under them, whether they are on or off."""
    cheats = ctl.cheat_file.cheats if ctl.cheat_file else []
    return [
        (loc.offset, loc.off)
        for c in cheats
        for loc in c.locations
        if loc.kind is LocationKind.CODE_PATCH
    ]


def apply_signatures(into: pj.Project, learned: pj.Project) -> int:
    """Copy the hooks' fingerprints learned on a copy of ``into`` back (by object and
    hook place). Returns how many changed."""
    by_place = {(o.id, a.hook): a for o in learned.objects for a in o.anchors_of(pj.HookAnchor)}
    n = 0
    for o in into.objects:
        for a in o.anchors_of(pj.HookAnchor):
            got = by_place.get((o.id, a.hook))
            if got is None or not got.sig_unique:
                continue
            if (a.signature, a.sig_unique) != (got.signature, True):
                a.signature, a.sig_unique = got.signature, True
                n += 1
    return n


class NoteFingerprints(QObject):
    """AC-U2: while the notes' own version runs, every hook and fixed code part gets a
    fingerprint that matches once in the game's code, so the next update can find them.
    Reads only, at most one code read at a time, once per game version per app run."""

    def __init__(self, ctl: Controller, finder: FindAgain) -> None:
        super().__init__(ctl)
        self.ctl, self.finder = ctl, finder
        self._done: set[tuple[str, str]] = set()  # (title, version) learned this run
        self._cancel: threading.Event | None = None
        finder.notes_changed.connect(self.maybe_learn)
        ctl.game_changed.connect(lambda _g: self.cancel())
        ctl.before_disconnect.append(self.cancel)

    @property
    def running(self) -> bool:
        return self._cancel is not None

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _needs_learning(self, p: pj.Project) -> bool:
        """Decided without reading the game: an unchecked hook fingerprint, or a fixed
        code part missing from the signatures file."""
        if cr.hooks_to_learn(p):
            return True
        try:
            have = up.load_signatures(up.signatures_path(p.title_id, p.version))
        except (OSError, ValueError) as exc:
            log.warning("action=learn_signatures source=notes reason=%s", exc)
            have = {}
        return bool(cr.parts_to_learn(p, have))

    def maybe_learn(self) -> None:
        p, ctl, finder = self.ctl.project, self.ctl, self.finder
        g = ctl.session.game if ctl.session.connected else None
        if p is None or g is None or self.running or ctl.code_reading:
            return
        if finder.running or finder.carrying:
            return
        if p.title_id.upper() != g.title_id or versions_differ(p.version, g.version):
            return
        key = (g.title_id, p.version)
        if key in self._done or not self._needs_learning(p):
            return
        copy = pj.Project.from_json(p.to_json())
        extra = open_code_lines(ctl)
        session = ctl.session
        cancel = self._cancel = threading.Event()
        ctl.code_reading = True

        def work():
            started = time.monotonic()
            image = up.read_code_image(session, cancel=cancel)
            have = up.load_signatures(up.signatures_path(copy.title_id, copy.version))
            _n, parts = cr.learn_note_signatures(copy, image, extra, have=have)
            return parts, time.monotonic() - started

        def done(result) -> None:
            self._end()
            parts, seconds = result
            if cancel.is_set() or self.ctl.project is not p:
                log.info("action=learn_signatures source=notes result=dropped")
                return
            self._done.add(key)
            changed = apply_signatures(p, copy)
            if parts:
                up.save_signatures(
                    up.signatures_path(p.title_id, p.version), p.title_id, p.version, parts
                )
            log.info(
                "action=learn_signatures source=notes saved=%d version=%s seconds=%.1f",
                changed + len(parts),
                p.version,
                seconds,
            )
            if changed:
                self.finder.save()

        def failed(exc: BaseException) -> None:
            self._end()
            log.warning("action=learn_signatures source=notes reason=%s", exc)

        self.ctl.tasks.run(work, done, failed)

    def _end(self) -> None:
        self._cancel = None
        self.ctl.code_reading = False

    def mark_learned(self, game: GameInfo | None, version: str) -> None:
        """Fingerprints were learned another way (after a bring-over): no read needed."""
        if game is not None:
            self._done.add((game.title_id, version))


# == the offer and the run ===============================================================
class CarryBar(QWidget):
    """A status line above the main status: the offer (Bring them over / Not now), then
    each step with a progress bar and Cancel. Hidden otherwise."""

    def __init__(self) -> None:
        super().__init__()
        self.label = QLabel("")
        self.label.setWordWrap(True)
        self.bring_btn = QPushButton("Bring them over")
        self.not_now_btn = QPushButton("Not now")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.cancel_btn = QPushButton("Cancel")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.label, 1)
        for w in (self.bring_btn, self.not_now_btn, self.progress, self.cancel_btn):
            lay.addWidget(w)
        self.mode = "hidden"
        self.show_mode("hidden")

    def show_mode(self, mode: str, text: str = "") -> None:
        """``offer``, ``running``, ``learning`` (the run is over, its fingerprints are
        being learned: no Cancel), ``done`` (just the text) or ``hidden``."""
        if mode == "running" and self.mode != "running":
            self.progress.setValue(0)
        self.mode = mode
        self.label.setText(text)
        self.bring_btn.setVisible(mode == "offer")
        self.not_now_btn.setVisible(mode == "offer")
        self.progress.setVisible(mode in ("running", "learning"))
        self.cancel_btn.setVisible(mode == "running")
        self.setVisible(mode != "hidden")


# Progress weights (design): read 20, per hooked value collect 10 + prove 60 + locate 10,
# 1 per other value.
W_READ, W_COLLECT, W_PROVE, W_LOCATE, W_OTHER = 20, 10, 60, 10, 1


@dataclass
class _Run:
    source: pj.Project  # the old notes as read (never written)
    project: pj.Project  # what is brought over (a merge copy for Advanced)
    merge_into: pj.Project | None
    pid: int
    cancel: threading.Event = field(default_factory=threading.Event)
    plan: cr.Plan | None = None
    index: int = 0
    found: dict[str, int | None] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    installed: list[pj.ObjectSpec] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    total: int = 1
    done: int = 0
    reading: bool = False  # holds the one code read (ctl.code_reading) for prepare


def decline_key(title_id: str, old: str, new: str) -> str:
    return f"{title_id.upper()} {old}>{new}"


class CarryOver(QObject):
    """Offers to bring the closest other version's notes over when this version has
    none (AC-U1), and runs the bring-over as a chain of jobs (AC-U3..U8): each job does
    one step in a worker; a pick goes to the player between jobs."""

    def __init__(
        self,
        ctl: Controller,
        finder: FindAgain,
        bar: CarryBar,
        fingerprints: NoteFingerprints | None = None,
    ) -> None:
        super().__init__(ctl)
        self.ctl, self.finder, self.bar, self.fingerprints = ctl, finder, bar, fingerprints
        self.offer: cr.Source | None = None
        self.run: _Run | None = None
        self.sleep: Callable[[float], None] = time.sleep  # tests: drive the game
        self.clock: Callable[[], float] = time.monotonic
        self.pick_version: Callable[[list[str]], str | None] = self._pick_version
        self._progress = _Signals()
        self._progress.done.connect(self._read_progress)
        bar.bring_btn.clicked.connect(self.bring)
        bar.not_now_btn.clicked.connect(self.not_now)
        bar.cancel_btn.clicked.connect(self.cancel)
        ctl.game_changed.connect(self.on_game)
        ctl.connection_changed.connect(self._on_connection)

    @property
    def running(self) -> bool:
        return self.run is not None

    # -- the offer (AC-U1) -----------------------------------------------------------
    def on_game(self, game: GameInfo | None) -> None:
        run = self.run
        if run is not None and (game is None or game.pid != run.pid):
            run.cancel.set()  # another game process: the run can't go on
            self._rollback(hints.CARRY_CANCELLED)
            return
        if run is None:
            self.check_offer(game)

    def _on_connection(self, up_: bool) -> None:
        if up_:
            return
        if self.run is not None:
            self.run.cancel.set()
            self._rollback(None)
        self.offer = None
        self.bar.show_mode("hidden")

    def check_offer(self, game: GameInfo | None) -> None:
        """Show the offer when this version's notes are missing or empty and other
        notes for the same title hold a value. Nothing is written."""
        self.offer = None
        p = self.ctl.project
        if (
            game is None
            or not self.ctl.session.connected
            or (p is not None and (p.objects or p.cheats))
            or self.finder.unreadable is not None
            or not cr.usable_version(game.version)
        ):
            self.bar.show_mode("hidden")
            return
        src = cr.find_source(game.title_id, game.version)
        if src is None:
            self.bar.show_mode("hidden")
            return
        old = src.project
        if (
            decline_key(game.title_id, old.version, game.version)
            in self.ctl.settings.carry_declined
        ):
            self.bar.show_mode("hidden")
            return
        self.offer = src
        text = hints.CARRY_OFFER_NEWER if src.newer else hints.CARRY_OFFER
        self.bar.show_mode(
            "offer", text.format(title=game.title, old=old.version, new=game.version)
        )
        log.info(
            "action=carry_offer title=%s from=%s to=%s objects=%d cheats=%d",
            game.title_id,
            old.version,
            game.version,
            len(old.objects),
            len(old.cheats),
        )

    def not_now(self) -> None:
        g, src = self.ctl.session.game, self.offer
        if g is None or src is None:
            return
        key = decline_key(g.title_id, src.project.version, g.version)
        if key not in self.ctl.settings.carry_declined:
            self.ctl.settings.carry_declined.append(key)
            self.ctl.settings.save()
        log.info("action=carry_declined pair=%s", key)
        self.offer = None
        self.bar.show_mode("hidden")

    def bring(self) -> None:
        if self.offer is not None:
            self.start(self.offer.project)

    # -- Advanced ----------------------------------------------------------------------
    def _pick_version(self, versions: list[str]) -> str | None:
        v, ok = QInputDialog.getItem(
            None, hints.ADVANCED_BRING, hints.CARRY_PICK_VERSION, versions, 0, False
        )
        return v if ok else None

    def open_advanced(self) -> None:
        """Pick any other version's notes; into notes that hold values already, only
        values and cheats whose names are new are added."""
        g = self.ctl.session.game if self.ctl.session.connected else None
        if g is None:
            self.ctl.message.emit(hints.CARRY_NEEDS_GAME)
            return
        sources = cr.list_sources(g.title_id, g.version)
        if not sources:
            self.ctl.message.emit(hints.CARRY_NO_SOURCES.format(title=g.title))
            return
        picked = self.pick_version([p.version for p in sources])
        src = next((p for p in sources if p.version == picked), None)
        if src is not None:
            p = self.ctl.project
            self.start(src, merge=p is not None and bool(p.objects or p.cheats))

    # -- the run (AC-U3..U8) -------------------------------------------------------------
    def _game_in_front(self) -> GameInfo | None:
        s = self.ctl.session
        g = s.game if s.connected else None
        if g is None:
            return None
        try:
            front = s.require_client().foreground_app()
        except CheatMakerError:
            return None
        # The PS5 may not say which app is in front: then the game being listed is enough.
        return g if front is None or front.pid == g.pid else None

    def start(self, source: pj.Project, merge: bool = False) -> None:
        if self.run is not None or self.finder.carrying:
            return
        if self.finder.running or self.ctl.code_reading:  # one job on the game's code at a time
            self.ctl.message.emit(hints.CARRY_WAIT)
            return
        game = self._game_in_front()
        if game is None:
            self.ctl.message.emit(hints.CARRY_NEEDS_GAME)
            return
        p = self.ctl.project
        if p is not None and (p.objects or p.cheats):
            merge = True  # notes that hold values are never overwritten (AC-U7)
        into = p if merge else None
        # object ids the notes or a hook still in the game use stay theirs
        empty = pj.Project(source.title_id, source.version, source.process)
        project = cr.for_merge(source, into or empty, avoid=self.finder.installed_ids())
        run = self.run = _Run(source, project, into, game.pid)
        hooked = sum(1 for o in project.objects if o.hook is not None)
        parts = cr.has_code_parts(project)
        run.total = (
            (W_READ if hooked or parts else 0)
            + hooked * (W_COLLECT + W_PROVE + W_LOCATE)
            + (len(project.objects) - hooked) * W_OTHER
        ) or 1
        self.finder.carrying = True
        self._say("start", old=source.version)
        log.info(
            "action=carry_start from=%s to=%s objects=%d merge=%s",
            source.version,
            game.version,
            len(project.objects),
            merge,
        )
        if hooked or parts:  # the one code read at a time is this run's until prepare is done
            run.reading = self.ctl.code_reading = True
            if self.fingerprints is not None:
                self.fingerprints.cancel()
            self._say("read")
        reserved = list(self.ctl.session.reserved_rooms) + [
            (off, off + len(data)) for off, data in open_code_lines(self.ctl)
        ]
        session, cancel = self.ctl.session, run.cancel
        self._job(
            lambda: cr.prepare(
                session,
                project,
                reserved=reserved,
                progress=self._progress.done.emit,
                cancel=cancel,
            ),
            self._prepared,
        )

    def cancel(self) -> None:
        if self.run is not None:
            self.run.cancel.set()

    # -- job plumbing ----------------------------------------------------------------------
    def _job(self, fn: Callable[[], Any], then: Callable[[Any], None]) -> None:
        run = self.run
        if run is None:
            return
        if run.cancel.is_set():
            self._rollback(hints.CARRY_CANCELLED)
            return

        def done(result: Any) -> None:
            if self.run is not run:
                return  # rolled back meanwhile
            if run.cancel.is_set():
                self._rollback(hints.CARRY_CANCELLED)
                return
            then(result)

        def failed(exc: BaseException) -> None:
            if self.run is run:
                self._failed(exc)

        self.ctl.tasks.run(fn, done, failed)

    def _failed(self, exc: BaseException) -> None:
        if isinstance(exc, ConnectionLost):
            self._rollback(None)
            self.ctl.fail(exc, "carry")
            return
        if isinstance(exc, ScanCancelled) or (self.run is not None and self.run.cancel.is_set()):
            self._rollback(hints.CARRY_CANCELLED)
            return
        self._rollback(None)
        self.ctl.fail(exc, "carry")

    def _release_read(self, run: _Run) -> None:
        if run.reading:
            run.reading = self.ctl.code_reading = False

    def _rollback(self, text: str | None) -> None:
        """Every hook put in during this run comes out; nothing is saved; the offer is
        shown again."""
        run, self.run = self.run, None
        if run is None:
            return
        self._release_read(run)
        if self.ctl.session.connected:
            for obj in run.installed:
                self.finder.take_out(obj)
        self.finder.keep_rooms()
        self.finder.carrying = False
        log.info("action=carry_done from=%s result=cancelled", run.source.version)
        if text:
            self.ctl.message.emit(text)
        self.check_offer(self.ctl.session.game if self.ctl.session.connected else None)

    def _say(self, step: str, **kw: str) -> None:
        text = hints.CARRY_STEPS[step].format(**kw)
        self.bar.show_mode("running", text)
        self.bar.progress.setValue(self._percent())
        self.ctl.message.emit(text)

    def _percent(self, extra: float = 0) -> int:
        run = self.run
        return 0 if run is None else min(100, int(100 * (run.done + extra) / run.total))

    def _advance(self, weight: int) -> None:
        if self.run is not None:
            self.run.done += weight
            self.bar.progress.setValue(self._percent())

    def _read_progress(self, frac: float) -> None:
        self.bar.progress.setValue(self._percent(W_READ * frac))

    def _note_install(self, run: _Run, obj: pj.ObjectSpec, state: str) -> None:
        """From the worker, the moment a hook went in (so Cancel can take it out). Run
        rolled back meanwhile: it is recorded, then taken out at once."""
        self.finder.note_install(obj, state, run.pid)
        if not self.finder.is_ours(obj.id, run.pid):
            return
        if self.run is run:
            run.installed.append(obj)
        else:
            self.finder.take_out(obj)

    # -- steps -------------------------------------------------------------------------------
    def _prepared(self, plan: cr.Plan) -> None:
        run = self.run
        if run is None:
            return
        run.plan = plan
        self._release_read(run)
        if plan.image is not None:
            self._advance(W_READ)
        self._next_item()

    def _item(self) -> cr.Item | None:
        run = self.run
        if run is None or run.plan is None or run.index >= len(run.plan.items):
            return None
        return run.plan.items[run.index]

    def _next_item(self) -> None:
        run = self.run
        if run is None or run.plan is None:
            return
        it = self._item()
        if it is None:
            self._finish()
            return
        if it.site is not None:
            self._collect(it)
        else:
            if it.kind == "hook":  # its hook couldn't come over: no collect, no proof
                self._advance(W_COLLECT + W_PROVE)
            self._locate(it)

    def _collect(self, it: cr.Item) -> None:
        run = self.run
        if run is None or run.plan is None:
            return
        self._say("collect", name=it.obj.label)
        session, plan, cancel, sleep = self.ctl.session, run.plan, run.cancel, self.sleep

        def work():
            try:
                classes = cr.collect_classes(
                    session, it, cancel=cancel, sleep=sleep, reserved=plan.reserved
                )
                return classes, cr.choose(session, classes, it)
            except cr.CarryFailed as exc:
                return None, exc

        self._job(work, lambda r: self._collected(it, *r))

    def _collected(self, it: cr.Item, classes: cr.Classes | None, choice) -> None:
        run = self.run
        if run is None:
            return
        self._advance(W_COLLECT)
        if isinstance(choice, cr.CarryFailed):
            cr.drop_hook(it, choice.reason)
            self._advance(W_PROVE)
            self._locate(it)
            return
        assert classes is not None  # the worker returns both, or nothing and the failure
        picked: int | None = None
        if isinstance(choice, cr.NeedPick):
            picked = self.finder.ask_pick(it.obj, choice.options)
            if self.run is not run:  # rolled back while the player picked
                return
        self._place(it, classes, choice, picked)

    def _place(self, it: cr.Item, classes: cr.Classes, choice, picked: int | None) -> None:
        run = self.run
        if run is None or run.plan is None:
            return
        self._say("prove")
        session, plan, cancel = self.ctl.session, run.plan, run.cancel

        def work():
            try:
                final = (
                    cr.answer(session, classes, choice, picked)
                    if isinstance(choice, cr.NeedPick)
                    else choice
                )
                cr.place_hook(
                    session,
                    it,
                    final,
                    reserved=plan.reserved,
                    cancel=cancel,
                    on_install=lambda o, state: self._note_install(run, o, state),
                )
            except cr.CarryFailed as exc:
                return exc.reason
            return ""

        def placed(reason: str) -> None:
            self._advance(W_PROVE)
            if reason:
                cr.drop_hook(it, reason)
            self._locate(it)

        self._job(work, placed)

    def _locate(self, it: cr.Item) -> None:
        run = self.run
        if run is None:
            return
        self._say("locate", name=it.obj.label)
        session, cancel, sleep, clock = self.ctl.session, run.cancel, self.sleep, self.clock

        def work():
            out = cr.locate(session, it.obj, cancel=cancel, sleep=sleep, clock=clock)
            if out.status == "disagree":
                return out
            addr = out.address if out.found else None
            cr.settle(session, it.obj, addr, placed=it.placed)
            return addr

        self._job(work, lambda r: self._located(it, r))

    def _located(self, it: cr.Item, result: int | rf.Outcome | None) -> None:
        run = self.run
        if run is None:
            return
        if isinstance(result, rf.Outcome):  # two ways found different places: the player picks
            picked = self.finder.ask_pick(it.obj, result.options)
            if self.run is not run:  # rolled back while the player picked
                return
            addr = rf.confirm_pick(it.obj, picked).address if picked is not None else None
            session = self.ctl.session

            def settle() -> int | None:
                cr.settle(session, it.obj, addr, placed=it.placed)
                return addr

            self._job(settle, lambda r: self._located(it, r))
            return
        run.found[it.obj.id] = result
        self._advance(W_LOCATE if it.kind == "hook" else W_OTHER)
        run.index += 1
        self._next_item()

    def _finish(self) -> None:
        run = self.run
        if run is None or run.plan is None:
            return
        plan = run.plan
        have: set[str] = set()  # values the notes hold already (Advanced), kept as they are
        if run.merge_into is not None:
            have = {o.id for o in run.merge_into.objects} - {o.id for o in run.project.objects}
        new, report = cr.assemble(plan, run.found, run.reasons, have)
        learning = plan.image is not None
        if learning:  # before the notes are saved: no second read of the game's code
            self.ctl.code_reading = True
        self.finder.adopt_carried(new, run.found, merge_into=run.merge_into)
        version = new.version
        for name in report.carried:
            log.info("action=carry_cheat name=%s result=carried", name)
        for name, why in report.failed.items():
            log.info("action=carry_cheat name=%s result=failed reason=%s", name, why)
        log.info(
            "action=carry_done from=%s to=%s objects=%d found=%d waiting=%d cheats=%d "
            "carried=%d failed=%d seconds=%.1f",
            run.source.version,
            version,
            report.objects,
            report.found,
            len(report.waiting),
            len(report.carried) + len(report.failed),
            len(report.carried),
            len(report.failed),
            time.monotonic() - run.started,
        )
        # The bring-over is over (its hooks are the notes' now): nothing can roll it
        # back, and the rest of the app works again while the fingerprints are learned.
        self.run = None
        self.offer = None
        self.finder.carrying = False
        summary = report.summary(version)
        self.ctl.message.emit(summary)
        self.ctl.cheats_changed.emit()
        target = self.ctl.project
        if not learning or target is None:
            self.ctl.code_reading = False
            self.bar.show_mode("done", summary)
            return
        # The new version's fingerprints from the code already read (AC-U7, AC-U2).
        self.bar.show_mode("learning", hints.CARRY_STEPS["learn"])
        self.bar.progress.setValue(100)
        copy = pj.Project.from_json(target.to_json())
        image, extra = plan.image, open_code_lines(self.ctl)
        game = self.ctl.session.game

        def work():
            have = up.load_signatures(up.signatures_path(copy.title_id, copy.version))
            return cr.learn_note_signatures(copy, image, extra, have=have)[1]

        def learned(parts) -> None:
            if self.ctl.project is target:
                apply_signatures(target, copy)
                if parts:
                    up.save_signatures(
                        up.signatures_path(target.title_id, target.version),
                        target.title_id,
                        target.version,
                        parts,
                    )
                if self.fingerprints is not None:
                    self.fingerprints.mark_learned(game, target.version)
                self.finder.save()
            self._learned(summary)

        def failed(exc: BaseException) -> None:
            log.warning("action=learn_signatures source=carry reason=%s", exc)
            self._learned(summary)

        self.ctl.tasks.run(work, learned, failed)

    def _learned(self, summary: str) -> None:
        self.ctl.code_reading = False
        if self.run is None and self.bar.mode == "learning":
            self.bar.show_mode("done", summary)
