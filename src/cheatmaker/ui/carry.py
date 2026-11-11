"""Bring your notes to a new game version (docs/spec-game-update.md): the offer bar, the
run driver and the background fingerprint learner.

Core logic lives in ``core.carry``; this module decides when to run each step and what
the player sees. No "vtable", "cave" or "pointer" words here (AC-8.2).
"""

from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QObject

from cheatmaker.core import carry as cr
from cheatmaker.core import project as pj
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import LocationKind, versions_differ
from cheatmaker.core.session import GameInfo
from cheatmaker.ui.controller import Controller
from cheatmaker.ui.find_again import FindAgain

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
