"""GoldHEN-style .json, as read by CheatRunner (verified against real repo files, AC-6.6).

{"name","id","version","process":"eboot.bin",
 "mods":[{"name","type":"checkbox","memory":[{"offset","on","off"}]}],"credits":[...]}
"""

from __future__ import annotations

import json

from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.errors import CheatFileError
from cheatmaker.formats import _value
from cheatmaker.formats._hex import fmt_offset, parse_bytes, parse_offset

VALUE_KEY = "cheatmaker_value"  # our own extra field: which bytes are the editable value


def _mod(c: Cheat) -> dict:
    mod: dict = {
        "name": c.name,
        "type": "checkbox",
        "memory": [
            {
                "offset": fmt_offset(loc.offset),
                "on": loc.on.hex().upper(),
                "off": loc.off.hex().upper(),
            }
            for loc in c.locations
        ],
    }
    vtype = _value.type_text(c)
    if vtype is not None:
        mod[VALUE_KEY] = {"type": vtype}
        at = _value.at_pairs(c)
        if at is not None:
            mod[VALUE_KEY]["at"] = at
    return mod


def dumps(cf: CheatFile) -> bytes:
    cheats = cf.saveable_cheats()
    if not cheats:
        raise ValueError("no cheats that can be saved to a file")
    doc = {
        "name": cf.title or cf.title_id,
        "id": cf.title_id.upper(),
        "version": cf.version,
        "process": cf.process,
        "mods": [_mod(c) for c in cheats],
        "credits": list(cf.credits),
    }
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _req(obj: dict, key: str, kind: type) -> object:
    if key not in obj:
        raise CheatFileError(f"missing field {key!r}")
    v = obj[key]
    if not isinstance(v, kind):
        raise CheatFileError(f"field {key!r} has the wrong type")
    return v


def loads(data: bytes | str) -> CheatFile:
    try:
        text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
        doc = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheatFileError(f"not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise CheatFileError("top level is not an object")
    cheats: list[Cheat] = []
    for i, mod in enumerate(_req(doc, "mods", list)):  # type: ignore[arg-type]
        if not isinstance(mod, dict):
            raise CheatFileError(f"mod {i} is not an object")
        locs = []
        for mem in _req(mod, "memory", list):  # type: ignore[arg-type]
            if not isinstance(mem, dict):
                raise CheatFileError(f"mod {i} has a bad memory entry")
            on, off = parse_bytes(mem.get("on", "")), parse_bytes(mem.get("off", ""))
            if len(on) != len(off):
                raise CheatFileError(f"mod {i}: on/off lengths differ")
            locs.append(
                Location(LocationKind.CODE_PATCH, parse_offset(mem.get("offset", "")), on, off)
            )
        if not locs:
            raise CheatFileError(f"mod {i} has no memory entries")
        hint = mod.get("hint")
        cheat = Cheat(
            str(mod.get("name") or f"Cheat {i + 1}"),
            locs,
            hint=hint if isinstance(hint, str) else None,
        )
        extra = mod.get(VALUE_KEY)
        if isinstance(extra, dict):
            _value.apply(cheat, extra.get("type"), extra.get("at"))
        cheats.append(cheat)
    credits = doc.get("credits")
    return CheatFile(
        title=str(doc.get("name", "")),
        title_id=str(_req(doc, "id", str)),
        version=str(_req(doc, "version", str)),
        cheats=cheats,
        process=str(doc.get("process") or "eboot.bin"),
        credits=[str(c) for c in credits] if isinstance(credits, list) else [],
    )
