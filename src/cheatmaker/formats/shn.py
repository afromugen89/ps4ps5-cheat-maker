"""GoldHEN .shn (XML), as read by CheatRunner.

Real files declare encoding="utf-16" but are stored as plain ASCII/UTF-8 with CRLF.
We write the same shape so CheatRunner sees what it already loads; reading accepts
either real UTF-16 (with BOM) or the ASCII form.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.errors import CheatFileError
from cheatmaker.formats import _value
from cheatmaker.formats._hex import fmt_offset, parse_bytes, parse_offset

TYPE_ATTR = "CheatMakerValueType"  # which bytes are the editable value (ours only)
AT_ATTR = "CheatMakerValueAt"
_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")


def _dash_hex(b: bytes) -> str:
    return "-".join(f"{x:02X}" for x in b)


def dumps(cf: CheatFile) -> bytes:
    cheats = cf.saveable_cheats()
    if not cheats:
        raise ValueError("no cheats that can be saved to a file")
    attrs = (
        f"Game={quoteattr(cf.title or cf.title_id)} Moder={quoteattr(', '.join(cf.credits))} "
        f"Cusa={quoteattr(cf.title_id.upper())} Version={quoteattr(cf.version)} "
        f"Process={quoteattr(cf.process)}"
    )
    lines = [
        '<?xml version="1.0" encoding="utf-16"?>',
        '<Trainer xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" {attrs}>',
        "  <Items />",
    ]
    for c in cheats:
        extra = ""
        vtype = _value.type_text(c)
        if vtype is not None:  # our own attributes; CheatRunner looks others up by name
            extra = f" {TYPE_ATTR}={quoteattr(vtype)}"
            at = _value.at_text(c)
            if at is not None:
                extra += f" {AT_ATTR}={quoteattr(at)}"
        lines.append(f"  <Cheat Text={quoteattr(c.name)}{extra}>")
        for loc in c.locations:
            lines += [
                "    <Cheatline>",
                f"      <Offset>{escape(fmt_offset(loc.offset))}</Offset>",
                f"      <Section>{loc.section}</Section>",
                f"      <ValueOn>{_dash_hex(loc.on)}</ValueOn>",
                f"      <ValueOff>{_dash_hex(loc.off)}</ValueOff>",
                "    </Cheatline>",
            ]
        lines.append("  </Cheat>")
    lines.append("</Trainer>")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def loads(data: bytes | str) -> CheatFile:
    text = _decode(data) if isinstance(data, bytes) else data
    try:
        root = ET.fromstring(_DECL.sub("", text, count=1))
    except ET.ParseError as exc:
        raise CheatFileError(f"not valid XML: {exc}") from exc
    if root.tag != "Trainer":
        raise CheatFileError(f"root element is {root.tag!r}, expected 'Trainer'")
    title_id = root.get("Cusa") or ""
    version = root.get("Version") or ""
    if not title_id or not version:
        raise CheatFileError("missing Cusa or Version attribute")
    cheats = []
    for i, ch in enumerate(root.findall("Cheat")):
        locs = []
        for line in ch.findall("Cheatline"):
            on = parse_bytes(line.findtext("ValueOn", ""))
            off = parse_bytes(line.findtext("ValueOff", ""))
            if len(on) != len(off):
                raise CheatFileError(f"cheat {i}: on/off lengths differ")
            try:
                section = int(line.findtext("Section", "0") or 0)
            except ValueError:
                raise CheatFileError(f"cheat {i}: bad Section") from None
            locs.append(
                Location(
                    LocationKind.CODE_PATCH,
                    parse_offset(line.findtext("Offset", "")),
                    on,
                    off,
                    section=section,
                )
            )
        if not locs:
            raise CheatFileError(f"cheat {i} has no lines")
        cheat = Cheat(ch.get("Text") or f"Cheat {i + 1}", locs)
        _value.apply(cheat, ch.get(TYPE_ATTR), ch.get(AT_ATTR))
        cheats.append(cheat)
    moder = root.get("Moder")
    return CheatFile(
        title=root.get("Game") or "",
        title_id=title_id,
        version=version,
        cheats=cheats,
        process=root.get("Process") or "eboot.bin",
        credits=[moder] if moder else [],
    )
