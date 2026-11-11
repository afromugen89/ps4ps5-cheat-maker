"""The builder's crash notes (spec-cheat-builder B6, S7).

While a test fix's bytes are in the game, a note on the PC says where they are. If
the app loses the PS5 with a note open, the fix probably crashed the game (or the
PS5): on the next connect that method is marked "crashed the game" for this title,
version and writer, and the builder never tries it again.

One JSON file in the app folder, written whole to a temp file and then renamed, so a
crash of the app itself never leaves half a file::

    {"open": {title_id, version, pid, site_off, method, n, patches, rooms, time} | null,
     "crashed": [{title_id, version, writer_off, method, time}]}
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cheatmaker.core.session import Session
from cheatmaker.errors import CheatMakerError
from cheatmaker.paths import app_data_dir

log = logging.getLogger(__name__)

FILE_NAME = "build_notes.json"
PUT_BACK_WAIT_S = 2.0  # as Builder.rollback: let threads leave the routine first
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

    def to_json(self) -> dict:
        d = asdict(self)
        d["patches"] = [asdict(p) for p in self.patches]
        d["rooms"] = [list(r) for r in self.rooms]
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
        )


def notes_path() -> Path:
    return app_data_dir() / FILE_NAME


def _load() -> dict:
    p = notes_path()
    if not p.exists():
        return {"open": None, "crashed": []}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        # A damaged file must not stop the builder; it only loses old crash marks.
        log.warning("action=build_notes_unreadable path=%s error=%s", p, exc)
        return {"open": None, "crashed": []}
    raw.setdefault("open", None)
    raw.setdefault("crashed", [])
    return raw


def _save(data: dict) -> None:
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


def _key(title_id: str, version: str, writer_off: int, method: str) -> tuple:
    return (title_id.upper(), version, int(writer_off), method)


def is_crashed(title_id: str, version: str, writer_off: int, method: str) -> bool:
    with _lock:
        crashed = _load()["crashed"]
    want = _key(title_id, version, writer_off, method)
    return any(
        _key(
            c.get("title_id", ""),
            c.get("version", ""),
            c.get("writer_off", -1),
            c.get("method", ""),
        )
        == want
        for c in crashed
        if isinstance(c, dict)
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


def check_on_connect(session: Session, sleep: Callable[[float], None] = time.sleep) -> str | None:
    """After connecting and finding the game: deal with a note left open. Returns a hint
    for the player, or None when there was nothing to do.

    The same game process still running means the app (not the game) went away: the
    fix is put back and nothing is marked. Otherwise the game restarted or isn't
    running, so the fix is gone with it and its method is marked as a crash."""
    note = current_note()
    if note is None:
        return None
    game = session.game
    client = session.client
    same = (
        game is not None
        and client is not None
        and game.pid == note.pid
        and game.title_id.upper() == note.title_id.upper()
        and game.version == note.version
    )
    if same:
        assert game is not None and client is not None
        try:
            for p in reversed(note.patches):
                addr = game.module_base + p.site_off
                if client.read(game.pid, addr, len(bytes.fromhex(p.new_hex))) == bytes.fromhex(
                    p.new_hex
                ):
                    client.write(game.pid, addr, bytes.fromhex(p.orig_hex))
            sleep(PUT_BACK_WAIT_S)
            for off, size in note.rooms:
                client.write(game.pid, game.module_base + off, bytes(size))
        except CheatMakerError as exc:
            log.warning("action=build_rollback where=connect restored=False error=%s", exc)
            return "Restart the game: fixes only live in memory until then."
        clear_note()
        log.info("action=build_rollback where=connect restored=True n=%d", note.n)
        return f"Fix {note.n} from last time was still in the game. It's been put back."
    mark_crashed(note.title_id, note.version, note.site_off, note.method)
    clear_note()
    return f"Fix {note.n} crashed the game last time. Your game is unchanged after a restart."


__all__ = [
    "OpenNote",
    "Patch",
    "check_on_connect",
    "clear_note",
    "current_note",
    "is_crashed",
    "mark_crashed",
    "notes_path",
    "open_note",
]
