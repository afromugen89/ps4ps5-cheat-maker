"""Make the running version's notes look like an older version's (console tests C21b-C21g).

Close Cheat Maker first. Then:
    python tools/make_old_notes.py PPSA01285 03.000.005 03.000.004

copies the notes (and fingerprints) of version 03.000.005 as if they were made on
03.000.004, and sets the real 03.000.005 files aside as ``.keep``. Start Cheat Maker and
Connect: it offers to bring the 03.000.004 notes over to 03.000.005 (a dry run of a
game update). Afterwards, with the app closed again:
    python tools/make_old_notes.py PPSA01285 03.000.005 03.000.004 --undo

puts the real files back byte for byte. ``--undo`` only works with the same three
values as the dry run (a note file next to the notes remembers them), so a mistyped
version never touches real notes. The files the dry run made are kept as ``.carried``
(never deleted). Uses CHEATMAKER_HOME like the app.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cheatmaker.core import project as pj
from cheatmaker.core import updater as up
from cheatmaker.core.cheats import normalize_version
from cheatmaker.errors import ProjectFileError


class Refused(Exception):
    """A plain reason to stop before anything was changed."""


def _keep(path: Path) -> Path:
    return path.with_name(path.name + ".keep")


def _aside(path: Path, suffix: str) -> Path:
    """A free name for ``path`` + ``suffix`` (``.carried``, ``.carried2``, ...)."""
    n = 1
    while True:
        out = path.with_name(path.name + (suffix if n == 1 else f"{suffix}{n}"))
        if not out.exists():
            return out
        n += 1


def _files(title: str, version: str) -> tuple[Path, Path]:
    """(notes, fingerprints) of a version. The notes' name has the version cleaned,
    the fingerprints' name doesn't (as the app names them)."""
    return pj.project_path(title, version), up.signatures_path(title, version)


def _marker(title: str) -> Path:
    """What the dry run did (title, versions, files it wrote), for --undo."""
    return pj.project_path(title, "x").parent / f"{title}_make_old_notes.json"


def _same(a: str, b: str) -> bool:
    return normalize_version(a) == normalize_version(b)


def _read_marker(title: str, cur: str, old: str) -> dict:
    """The dry run's record when it was made with these values. Raises Refused."""
    path = _marker(title)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Refused(f"No dry run was made for {title} ({path.name} is missing).") from None
    except (OSError, ValueError) as exc:
        raise Refused(f"{path.name} can't be read ({exc}).") from exc
    if not (
        isinstance(doc, dict)
        and isinstance(doc.get("made"), list)
        and doc.get("title") == title
        and _same(str(doc.get("current", "")), cur)
        and _same(str(doc.get("old", "")), old)
    ):
        if not isinstance(doc, dict):
            raise Refused(f"{path.name} isn't a dry run record.")
        raise Refused(
            f"The dry run was made for {doc.get('title')} {doc.get('current')} "
            f"{doc.get('old')}: run --undo with those."
        )
    return doc


def _copy_as(src: Path, dst: Path, version: str) -> None:
    doc = json.loads(src.read_text(encoding="utf-8"))
    doc["version"] = version
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")


def forward(title: str, cur: str, old: str, say=print) -> None:
    if normalize_version(cur) == normalize_version(old):
        raise Refused(f"{cur} and {old} are the same version.")
    if _marker(title).exists():  # a dry run is set up already
        raise Refused(f"{_marker(title).name} already exists: run with --undo first.")
    cur_notes, cur_sigs = _files(title, cur)
    old_notes, old_sigs = _files(title, old)
    try:
        notes = pj.load(cur_notes)
    except ProjectFileError as exc:
        raise Refused(f"The notes for {cur} can't be read ({exc}).") from exc
    if notes is None:
        raise Refused(f"There are no notes for {title} {cur} ({cur_notes}).")
    for p in (cur_notes, cur_sigs, old_notes, old_sigs):
        if _keep(p).exists():
            raise Refused(f"{_keep(p).name} already exists: run with --undo first.")
    for p in (old_notes, old_sigs):
        if p.exists():
            p.replace(_keep(p))
            say(f"Set aside {p.name} as {_keep(p).name}")
    made = [old_notes.name]
    _copy_as(cur_notes, old_notes, old)
    say(f"Wrote {old_notes.name} (a copy of the {cur} notes, marked {old})")
    if cur_sigs.exists():
        _copy_as(cur_sigs, old_sigs, old)
        made.append(old_sigs.name)
        say(f"Wrote {old_sigs.name} (a copy of the {cur} fingerprints)")
    record = {"title": title, "current": cur, "old": old, "made": made}
    _marker(title).write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    for p in (cur_notes, cur_sigs):
        if p.exists():
            p.replace(_keep(p))
            say(f"Set aside {p.name} as {_keep(p).name}")
    say(f"Done. Start Cheat Maker and Connect: it offers to bring {old} over to {cur}.")


def undo(title: str, cur: str, old: str, say=print) -> None:
    doc = _read_marker(title, cur, old)
    cur, old = doc["current"], doc["old"]  # as written then (the file names use them)
    cur_notes, cur_sigs = _files(title, cur)
    old_notes, old_sigs = _files(title, old)
    if not _keep(cur_notes).exists():
        raise Refused(f"{_keep(cur_notes).name} is missing, so there is nothing to put back.")
    for p in (old_notes, old_sigs):
        if p.exists() and p.name not in doc["made"] and _keep(p).exists():
            raise Refused(f"{p.name} isn't the dry run's copy and {_keep(p).name} exists too.")
    for p in (cur_notes, cur_sigs):
        if p.exists():  # made or changed by the dry run: kept, never deleted
            dst = _aside(p, ".carried")
            p.replace(dst)
            say(f"Kept the dry run's {p.name} as {dst.name}")
        if _keep(p).exists():
            _keep(p).replace(p)
            say(f"Put back {p.name}")
    for p in (old_notes, old_sigs):
        if p.exists() and p.name in doc["made"]:  # the copies this tool wrote: kept aside
            dst = _aside(p, ".carried")
            p.replace(dst)
            say(f"Kept the copy {p.name} as {dst.name}")
        if _keep(p).exists():
            _keep(p).replace(p)
            say(f"Put back {p.name}")
    _marker(title).unlink()
    say("Done. Your notes are as they were.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("title_id", help="e.g. PPSA01285")
    ap.add_argument("current", help="the version running now, e.g. 03.000.005")
    ap.add_argument("old", help="the version to pretend the notes are from, e.g. 03.000.004")
    ap.add_argument("--undo", action="store_true", help="put the real files back")
    args = ap.parse_args()
    print("Close Cheat Maker before you go on (it saves notes while it runs).")
    try:
        (undo if args.undo else forward)(args.title_id.upper(), args.current, args.old)
    except Refused as exc:
        print(f"Nothing was changed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
