"""CheatRunner / GoldHEN cheat file formats (.json default, .shn, .mc4)."""

from __future__ import annotations

import logging
from pathlib import Path

from cheatmaker.core.cheats import CheatFile
from cheatmaker.errors import CheatFileError
from cheatmaker.formats import goldhen_json, mc4, shn

log = logging.getLogger(__name__)

FORMATS = {"json": goldhen_json, "shn": shn, "mc4": mc4}


def dumps(cf: CheatFile, fmt: str) -> bytes:
    try:
        return FORMATS[fmt].dumps(cf)
    except KeyError:
        raise ValueError(f"unknown format {fmt!r}") from None


def sniff(data: bytes) -> str | None:
    """ "shn" for XML, "json" for a JSON object, None when the content doesn't say (an
    .mc4 is base64 text, which looks like neither)."""
    head = data[:4096]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16: look at the text itself
        start = head[: len(head) // 2 * 2].decode("utf-16", "replace").lstrip()
    else:
        start = head.removeprefix(b"\xef\xbb\xbf").lstrip().decode("latin-1")
    if start.startswith("<"):
        return "shn"
    if start.startswith("{"):
        return "json"
    return None


def load_path(path: str | Path) -> CheatFile:
    """Read a .json, .shn or .mc4 file; anything wrong raises CheatFileError (AC-7.3).
    A file whose content is clearly another format than its extension (XML or JSON
    saved under another name) is read as what it is, as ReaperPy does."""
    p = Path(path)
    fmt = p.suffix.lower().lstrip(".")
    if fmt not in FORMATS:
        raise CheatFileError(f"unsupported extension {p.suffix!r}")
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise CheatFileError(f"can't read {p.name}: {exc}") from exc
    found = sniff(data)
    if found is not None and found != fmt:
        log.info("action=load_file name=%s ext=%s content=%s", p.name, fmt, found)
        fmt = found
    return FORMATS[fmt].loads(data)
