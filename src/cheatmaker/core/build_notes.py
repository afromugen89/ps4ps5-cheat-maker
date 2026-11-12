"""The builder's crash notes (spec-cheat-builder B6, S7).

While a test fix's bytes are in the game, a note on the PC says where they are. If
the app loses the PS5 with a note open, the fix probably crashed the game (or the
PS5): on the next connect that method is marked "crashed the game" for this title,
version and writer, and the builder never tries it again.

Make it work with linked values (spec-linked-values L12) keeps a passed place in the
game while it builds the next one: its note moves from ``open`` to ``kept``. Kept fixes
are put back on the next connect to the same game process, but never marked as a
crash: only the fix under test (``open``) can have crashed the game.

One JSON file in the app folder, written whole to a temp file and then renamed, so a
crash of the app itself never leaves half a file::

    {"open": {title_id, version, pid, site_off, method, n, patches, rooms, time,
              "place" (only when > 1)} | null,
     "kept": [{...the same fields...}] (only when not empty),
     "crashed": [{title_id, version, writer_off, method, time}]}
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cheatmaker.core.session import Session
from cheatmaker.errors import CheatMakerError
from cheatmaker.paths import app_data_dir

log = logging.getLogger(__name__)

FILE_NAME = "build_notes.json"
PUT_BACK_WAIT_S = 2.0  # as Builder.rollback: let threads leave the routine first
RESTART = "Restart the game: fixes only live in memory until then."
KEPT_PUT_BACK = "Fixes from last time were still in the game. They've been put back."
_lock = threading.Lock()


@dataclass(frozen=True)
class Patch:
    """One code change: ``orig`` goes back at ``site_off`` (module offset)."""

    site_off: int
    orig_hex: str
    new_hex: str


@dataclass(frozen=True)
class OpenNote:
    title_id: str
    version: str
    pid: int
    site_off: int  # the first writer's offset: what a crash mark names
    method: str
    n: int  # which fix of the build ("Fix N crashed the game last time")
    patches: tuple[Patch, ...] = ()
    rooms: tuple[tuple[int, int], ...] = ()  # (module offset, size) zeroed on put back
    time: float = field(default_factory=time.time)
    place: int = 1  # which place of a Make it work build (linked values)

    def to_json(self) -> dict:
        d = asdict(self)
        d["patches"] = [asdict(p) for p in self.patches]
        d["rooms"] = [list(r) for r in self.rooms]
        if self.place == 1:  # a build with one place writes the file as before
            del d["place"]
        return d

    @classmethod
    def from_json(cls, d: dict) -> OpenNote:
        return cls(
            title_id=str(d["title_id"]),
            version=str(d["version"]),
            pid=int(d["pid"]),
            site_off=int(d["site_off"]),
            method=str(d["method"]),
            n=int(d.get("n", 1)),
            patches=tuple(
                Patch(int(p["site_off"]), p["orig_hex"], p["new_hex"]) for p in d.get("patches", [])
            ),
            rooms=tuple((int(a), int(b)) for a, b in d.get("rooms", [])),
            time=float(d.get("time", 0.0)),
            place=int(d.get("place", 1)),
        )


def notes_path() -> Path:
    return app_data_dir() / FILE_NAME


def _load() -> dict:
    p = notes_path()
    if not p.exists():
        return {"open": None, "kept": [], "crashed": []}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        # A damaged file must not stop the builder; it only loses old crash marks.
        log.warning("action=build_notes_unreadable path=%s error=%s", p, exc)
        return {"open": None, "kept": [], "crashed": []}
    if not isinstance(raw.get("open"), dict):
        raw["open"] = None
    kept = raw.get("kept")
    raw["kept"] = [k for k in kept if isinstance(k, dict)] if isinstance(kept, list) else []
    crashed = raw.get("crashed")
    raw["crashed"] = [c for c in crashed if _good_mark(c)] if isinstance(crashed, list) else []
    return raw


def _good_mark(c: object) -> bool:
    """A crash mark with every field of the right type; others are dropped."""
    ok = (
        isinstance(c, dict)
        and all(isinstance(c.get(k), str) for k in ("title_id", "version", "method"))
        and type(c.get("writer_off")) is int
    )
    if not ok:
        log.warning("action=build_notes_bad_mark")
    return ok


def _save(data: dict) -> None:
    if not data.get("kept"):  # no kept places: the file is as an older app writes it
        data = {k: v for k, v in data.items() if k != "kept"}
    p = notes_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def open_note(note: OpenNote) -> None:
    """Before a fix's bytes go into the game."""
    with _lock:
        data = _load()
        data["open"] = note.to_json()
        _save(data)


def clear_note() -> None:
    """After the fix's bytes are out of the game (or saved as the result)."""
    with _lock:
        data = _load()
        if data["open"] is not None:
            data["open"] = None
            _save(data)


def current_note() -> OpenNote | None:
    with _lock:
        raw = _load()["open"]
    try:
        return None if raw is None else OpenNote.from_json(raw)
    except (KeyError, TypeError, ValueError):
        log.warning("action=build_notes_bad_open_note")
        return None


def keep_note() -> None:
    """A passed place stays in the game while the next one is built: its open note
    (already naming the re-aimed jumps) moves to the kept list, in one write."""
    with _lock:
        data = _load()
        if data["open"] is None:
            return
        data["kept"].append(data["open"])
        data["open"] = None
        _save(data)


def kept_notes() -> list[OpenNote]:
    """The kept places, oldest first. Damaged entries are skipped (and logged)."""
    with _lock:
        raw = _load()["kept"]
    out = []
    for k in raw:
        try:
            out.append(OpenNote.from_json(k))
        except (KeyError, TypeError, ValueError):
            log.warning("action=build_notes_bad_kept_note")
    return out


def clear_kept(places: Iterable[int] | None = None) -> None:
    """Forget kept places (all, or those numbered in ``places``)."""
    drop = None if places is None else set(places)
    with _lock:
        data = _load()
        left = [k for k in data["kept"] if drop is not None and k.get("place", 1) not in drop]
        if left != data["kept"]:
            data["kept"] = left
            _save(data)


def clear_all() -> None:
    """The build ended: nothing of it is in the game any more, or it is the result."""
    with _lock:
        data = _load()
        if data["open"] is not None or data["kept"]:
            data["open"], data["kept"] = None, []
            _save(data)


def _key(title_id: str, version: str, writer_off: int, method: str) -> tuple:
    return (title_id.upper(), version, int(writer_off), method)


def is_crashed(title_id: str, version: str, writer_off: int, method: str) -> bool:
    with _lock:
        crashed = _load()["crashed"]
    want = _key(title_id, version, writer_off, method)
    return any(
        _key(c["title_id"], c["version"], c["writer_off"], c["method"]) == want for c in crashed
    )


def mark_crashed(title_id: str, version: str, writer_off: int, method: str) -> None:
    """S7: this method on this writer is never tried again for this game version."""
    if is_crashed(title_id, version, writer_off, method):
        return
    with _lock:
        data = _load()
        data["crashed"].append(
            {
                "title_id": title_id,
                "version": version,
                "writer_off": int(writer_off),
                "method": method,
                "time": time.time(),
            }
        )
        _save(data)
    log.info(
        "action=build_crash_mark title=%s version=%s writer_off=%#x method=%s",
        title_id,
        version,
        writer_off,
        method,
    )


def _put_back_sites(client, pid: int, base: int, patches: tuple[Patch, ...]) -> bool:
    """Our bytes at a site go back to the original. True when every site ends up
    holding the original."""
    clean = True
    for p in reversed(patches):
        addr = base + p.site_off
        orig, new = bytes.fromhex(p.orig_hex), bytes.fromhex(p.new_hex)
        now = client.read(pid, addr, len(new))
        if now == new:
            client.write(pid, addr, orig)
        elif now != orig:
            clean = False
    return clean


def _same_game(session: Session, note: OpenNote) -> bool:
    game = session.game
    return (
        game is not None
        and session.client is not None
        and game.pid == note.pid
        and game.title_id.upper() == note.title_id.upper()
        and game.version == note.version
    )


def check_on_connect(session: Session, sleep: Callable[[float], None] = time.sleep) -> str | None:
    """After connecting and finding the game: deal with a note left open, then with
    kept places. Returns a hint for the player (the open note's first), or None when
    there was nothing to do."""
    hint = _open_on_connect(session, sleep)
    kept = _kept_on_connect(session, sleep)
    return hint or kept


def _open_on_connect(session: Session, sleep: Callable[[float], None]) -> str | None:
    """A note left open. Returns a hint for the player, or None when there was nothing
    to do.

    The same game process still running means the app (not the game) went away: the
    fix is put back and nothing is marked. The routines are only zeroed when every
    site holds the game's own bytes again; a site holding something else (a jump we
    had re-aimed, another tool's patch) may still lead into them, so then they stay
    and the player is asked to restart. Otherwise the game restarted or isn't
    running, so the fix is gone with it and its method is marked as a crash."""
    note = current_note()
    if note is None:
        return None
    game = session.game
    client = session.client
    if _same_game(session, note):
        assert game is not None and client is not None
        try:
            clean = _put_back_sites(client, game.pid, game.module_base, note.patches)
            if clean:
                sleep(PUT_BACK_WAIT_S)
                for off, size in note.rooms:
                    client.write(game.pid, game.module_base + off, bytes(size))
        except CheatMakerError as exc:
            log.warning("action=build_rollback where=connect restored=False error=%s", exc)
            return RESTART
        clear_note()  # either way: this process isn't a crash of the fix
        if not clean:
            log.warning("action=build_rollback where=connect restored=False error=site_changed")
            return RESTART
        log.info("action=build_rollback where=connect restored=True n=%d", note.n)
        return f"Fix {note.n} from last time was still in the game. It's been put back."
    mark_crashed(note.title_id, note.version, note.site_off, note.method)
    clear_note()
    return f"Fix {note.n} crashed the game last time. Your game is unchanged after a restart."


def _kept_on_connect(session: Session, sleep: Callable[[float], None]) -> str | None:
    """Kept places of a build the app didn't finish. In the same game process they are
    put back, the newest place first, with one wait before their routines are zeroed
    (only where every site is clean). In another process or with no game they died
    with the game: dropped, never marked as a crash (only the fix under test can be)."""
    kept = kept_notes()
    if not kept:
        return None
    same = [n for n in kept if _same_game(session, n)]
    if len(same) != len(kept):
        log.info("action=build_rollback where=connect kept_dropped=%d", len(kept) - len(same))
    if not same:
        clear_kept()
        return None
    game, client = session.game, session.client
    assert game is not None and client is not None
    order = sorted(reversed(same), key=lambda n: -n.place)  # newest place first
    try:
        clean = [_put_back_sites(client, game.pid, game.module_base, n.patches) for n in order]
        if any(clean):
            sleep(PUT_BACK_WAIT_S)
        for n, ok in zip(order, clean, strict=True):
            if ok:
                for off, size in n.rooms:
                    client.write(game.pid, game.module_base + off, bytes(size))
    except CheatMakerError as exc:
        log.warning("action=build_rollback where=connect kept restored=False error=%s", exc)
        return RESTART
    clear_kept()
    if not all(clean):
        log.warning("action=build_rollback where=connect kept restored=False error=site_changed")
        return RESTART
    log.info(
        "action=build_rollback where=connect kept restored=True places=%s",
        ",".join(str(n.place) for n in order),
    )
    return KEPT_PUT_BACK


__all__ = [
    "OpenNote",
    "Patch",
    "check_on_connect",
    "clear_all",
    "clear_kept",
    "clear_note",
    "current_note",
    "is_crashed",
    "keep_note",
    "kept_notes",
    "mark_crashed",
    "notes_path",
    "open_note",
]
