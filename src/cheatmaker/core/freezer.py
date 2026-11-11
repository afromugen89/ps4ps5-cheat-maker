"""Set and freeze (AC-4), live cheat toggling (AC-5.3), disconnect handling (AC-1.4)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from cheatmaker.core.cheats import Cheat
from cheatmaker.core.session import Session
from cheatmaker.errors import CheatMakerError, ConnectionLost, FindFailed

if TYPE_CHECKING:
    from cheatmaker.core.refind import Tracker

log = logging.getLogger(__name__)

FREEZE_HZ = 10  # spec §6: at least 10 writes per second

Writes = list[tuple[int, int, bytes]]
Resolver = Callable[[], Writes | None]
"""Gives this tick's writes, or None while the target can't be verified (then nothing
is written: v3 "never write blind")."""


class Freezer:
    """Writes frozen values back FREEZE_HZ times a second on a worker thread."""

    def __init__(
        self,
        session: Session,
        on_lost: Callable[[], None] | None = None,
        on_error: Callable[[str, Exception], None] | None = None,
        hz: float = FREEZE_HZ,
    ) -> None:
        self.session = session
        self.on_lost = on_lost
        self.on_error = on_error
        self.interval = 1.0 / hz
        self._entries: dict[str, Writes | Resolver] = {}
        self.paused: set[str] = set()  # tracked entries waiting for their target
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def keys(self) -> set[str]:
        with self._lock:
            return set(self._entries)

    def freeze(self, key: str, writes: list[tuple[int, int, bytes]]) -> None:
        """``writes`` = [(pid, address, bytes)], all written each tick."""
        for _pid, addr, data in writes:
            log.info("action=freeze key=%s addr=%#x bytes=%s", key, addr, data.hex())
        with self._lock:
            self._entries[key] = list(writes)
        self._ensure_thread()

    def freeze_tracked(self, key: str, resolve: Resolver) -> None:
        """Like freeze, but the address is worked out again every tick (v3, AC-12.5)."""
        log.info("action=freeze key=%s tracked=yes", key)
        with self._lock:
            self._entries[key] = resolve
            self.paused.discard(key)
        self._ensure_thread()

    def unfreeze(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)
            self.paused.discard(key)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.paused.clear()

    def stop(self) -> None:
        self.clear()
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="freezer", daemon=True)
        self._thread.start()

    def tick(self) -> None:
        """One round of writes. Public so tests can drive it without timing."""
        with self._lock:
            entries = list(self._entries.items())
        client = self.session.client
        if client is None or not entries:
            return
        for key, entry in entries:
            try:
                writes = entry() if callable(entry) else entry
                if writes is None:
                    if key not in self.paused:
                        log.info("action=freeze key=%s paused: target not verified", key)
                        self.paused.add(key)
                    continue
                if key in self.paused:
                    log.info("action=freeze key=%s resumed", key)
                    self.paused.discard(key)
                for pid, addr, data in writes:
                    client.write(pid, addr, data)
            except ConnectionLost:
                raise
            except CheatMakerError as exc:
                log.error("action=freeze key=%s reason=%s", key, exc)
                self.unfreeze(key)
                if self.on_error:
                    self.on_error(key, exc)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except ConnectionLost as exc:
                log.error("action=freeze reason=connection lost: %s", exc)
                self.clear()
                if self.on_lost:
                    self.on_lost()
                return
            with self._lock:
                if not self._entries:
                    return


class LiveCheats:
    """Turns cheats on and off on the running game."""

    def __init__(self, session: Session, freezer: Freezer) -> None:
        self.session = session
        self.freezer = freezer
        self.active: dict[int, Cheat] = {}
        self.trackers: dict[str, Tracker] = {}  # object id -> where it is now (v3)
        # Learned parts of combined cheats not written right now (id(cheat) -> part
        # names). Touched by the freezer thread and by turn_off: guarded by the lock.
        self._part_paused: dict[int, set[str]] = {}
        self._parts_lock = threading.Lock()

    def _tracked(self, cheat: Cheat, on: bool) -> Writes | None:
        """Writes for an anchored cheat at the object's verified address, or None."""
        assert cheat.anchor is not None
        obj_id, field_name = cheat.anchor
        tracker = self.trackers.get(obj_id)
        addr = tracker.field_address(field_name) if tracker is not None else None
        if addr is None:
            return None
        game = self.session.require_game()
        loc = cheat.locations[0]  # its offset is unused: the Tracker holds the address
        return [(game.pid, addr, loc.on if on else loc.off)]

    def _part_writes(self, cheat: Cheat, on: bool, strict: bool) -> Writes | None:
        """Writes for a combined cheat with learned parts. A learned part goes only to
        its tracker's verified address (never to its unused ``offset``); fixed and
        moving parts go where they are. ``strict``: a learned part that isn't found
        raises FindFailed and nothing is written. Otherwise that part is skipped
        (logged once as paused) and the rest is written; None when nothing is left."""
        game = self.session.require_game()
        bases: dict[str, int | None] = {}  # each object is checked once per call
        writes: Writes = []
        lost: list[str] = []
        found: list[str] = []
        for loc in cheat.locations:
            data = loc.on if on else loc.off
            if loc.anchor is None:
                writes.append((game.pid, loc.address(game.module_base), data))
                continue
            addr = self._part_address(loc.anchor, bases)
            name = loc.part or loc.anchor[1]
            if addr is None:
                if strict:
                    raise FindFailed(
                        f"{cheat.name}: {name} isn't found right now, so nothing was "
                        "changed. Press Find it again."
                    )
                lost.append(name)
                continue
            found.append(name)
            writes.append((game.pid, addr, data))
        if not strict:
            self._note_paused(cheat, lost, found)
        return writes or None

    def _part_address(self, anchor: tuple[str, str], bases: dict[str, int | None]) -> int | None:
        """A learned part's verified address now, or None."""
        obj_id, field_name = anchor
        tracker = self.trackers.get(obj_id)
        if tracker is None:
            return None
        if obj_id not in bases:
            bases[obj_id] = tracker.current()
        base = bases[obj_id]
        if not base:
            return None
        try:
            return base + tracker.obj.field(field_name).off
        except KeyError:
            return None

    def _note_paused(self, cheat: Cheat, lost: list[str], found: list[str]) -> None:
        key = str(id(cheat))
        with self._parts_lock:
            if id(cheat) not in self.active:  # turned off meanwhile: leave nothing behind
                return
            paused = self._part_paused.setdefault(id(cheat), set())
            for name in lost:
                if name not in paused:
                    paused.add(name)
                    log.info("action=freeze key=%s part=%s paused", key, name)
            for name in found:
                if name in paused:
                    paused.discard(name)
                    log.info("action=freeze key=%s part=%s resumed", key, name)

    def _writes(self, cheat: Cheat, on: bool) -> Writes:
        if cheat.learned_parts:
            writes = self._part_writes(cheat, on, strict=True)
            return writes or []
        if cheat.anchor is not None:
            writes = self._tracked(cheat, on)
            if writes is None:
                raise FindFailed(
                    f"{cheat.name} isn't found in the game right now, so nothing was "
                    "changed. Press Find it again."
                )
            return writes
        game = self.session.require_game()
        return [
            (game.pid, loc.address(game.module_base), loc.on if on else loc.off)
            for loc in cheat.locations
        ]

    def set_once(self, cheat: Cheat) -> None:
        """Write the cheat's value once (AC-4.1). Raises WriteFailed (AC-4.3)."""
        client = self.session.require_client()
        for pid, addr, data in self._writes(cheat, True):
            log.info("action=write cheat=%r addr=%#x bytes=%s", cheat.name, addr, data.hex())
            client.write(pid, addr, data)

    def turn_on(self, cheat: Cheat) -> None:
        self.set_once(cheat)
        if cheat.freeze and cheat.learned_parts:
            self.freezer.freeze_tracked(
                str(id(cheat)), lambda: self._part_writes(cheat, True, strict=False)
            )
        elif cheat.freeze and cheat.anchor is not None:
            self.freezer.freeze_tracked(str(id(cheat)), lambda: self._tracked(cheat, True))
        elif cheat.freeze:
            self.freezer.freeze(str(id(cheat)), self._writes(cheat, True))
        cheat.enabled = True
        self.active[id(cheat)] = cheat

    def mark_on(self, cheat: Cheat) -> None:
        """A cheat whose bytes are already in the game (Make it work just kept its
        fix): on and listed as active, with nothing written."""
        if cheat.freeze:
            raise ValueError("a kept fix never freezes")
        cheat.enabled = True
        self.active[id(cheat)] = cheat

    def turn_off(self, cheat: Cheat, restore: bool = True) -> None:
        self.freezer.unfreeze(str(id(cheat)))
        cheat.enabled = False
        self.active.pop(id(cheat), None)
        with self._parts_lock:
            self._part_paused.pop(id(cheat), None)
        if restore and self.session.client is not None:
            client = self.session.client
            if cheat.learned_parts:  # learned parts are left as they are
                game = self.session.require_game()
                writes = [
                    (game.pid, loc.address(game.module_base), loc.off)
                    for loc in cheat.locations
                    if loc.anchor is None
                ]
            elif cheat.anchor is not None:  # only where it's verified to be right now
                writes = self._tracked(cheat, False) or []
            else:
                writes = self._writes(cheat, False)
            # Undo in reverse: a jump into a code cave must go before the cave does.
            for pid, addr, data in reversed(writes):
                client.write(pid, addr, data)

    def refresh(self, cheat: Cheat) -> None:
        """Re-apply after the player edits the value of an active cheat."""
        if cheat.enabled:
            self.turn_on(cheat)

    def mark_all_off(self) -> list[Cheat]:
        """After a lost connection: nothing is running any more (AC-1.4)."""
        self.freezer.clear()
        with self._parts_lock:
            self._part_paused.clear()
        cheats = list(self.active.values())
        for c in cheats:
            c.enabled = False
        self.active.clear()
        return cheats
