"""Make the running version's notes look like an older version's (console tests C21b-C21g).

Close Cheat Maker first. Then:
    python tools/make_old_notes.py PPSA01285 03.000.005 03.000.004

copies the notes (and fingerprints) of version 03.000.005 as if they were made on
03.000.004, and sets the real 03.000.005 files aside as ``.keep``. Start Cheat Maker and
Connect: it offers to bring the 03.000.004 notes over to 03.000.005 (a dry run of a
game update). Afterwards, with the app closed again:
    python tools/make_old_notes.py PPSA01285 03.000.005 03.000.004 --undo

puts the real files back byte for byte. The notes the dry run made are kept as
``.carried`` (never deleted). Uses CHEATMAKER_HOME like the app.
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


def _copy_as(src: Path, dst: Path, version: str) -> None:
    doc = json.loads(src.read_text(encoding="utf-8"))
    doc["version"] = version
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")


def forward(title: str, cur: str, old: str, say=print) -> None:
    if normalize_version(cur) == normalize_version(old):
        raise Refused(f"{cur} and {old} are the same version.")
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
    _copy_as(cur_notes, old_notes, old)
    say(f"Wrote {old_notes.name} (a copy of the {cur} notes, marked {old})")
    if cur_sigs.exists():
        _copy_as(cur_sigs, old_sigs, old)
        say(f"Wrote {old_sigs.name} (a copy of the {cur} fingerprints)")
    for p in (cur_notes, cur_sigs):
        if p.exists():
            p.replace(_keep(p))
            say(f"Set aside {p.name} as {_keep(p).name}")
    say(f"Done. Start Cheat Maker and Connect: it offers to bring {old} over to {cur}.")


def undo(title: str, cur: str, old: str, say=print) -> None:
    cur_notes, cur_sigs = _files(title, cur)
    old_notes, old_sigs = _files(title, old)
    if not _keep(cur_notes).exists():
        raise Refused(f"{_keep(cur_notes).name} is missing, so there is nothing to put back.")
    for p in (cur_notes, cur_sigs):
        if p.exists():  # made or changed by the dry run: kept, never deleted
            dst = _aside(p, ".carried")
            p.replace(dst)
            say(f"Kept the dry run's {p.name} as {dst.name}")
        if _keep(p).exists():
            _keep(p).replace(p)
            say(f"Put back {p.name}")
    for p in (old_notes, old_sigs):
        if p.exists():
            p.unlink()
            say(f"Removed the copy {p.name}")
        if _keep(p).exists():
            _keep(p).replace(p)
            say(f"Put back {p.name}")
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
