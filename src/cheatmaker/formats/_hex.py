from __future__ import annotations

from cheatmaker.errors import CheatFileError


def parse_offset(text: str) -> int:
    t = str(text).strip()
    if t.lower().startswith("0x"):
        t = t[2:]
    try:
        v = int(t, 16)
    except ValueError:
        raise CheatFileError(f"bad offset {text!r}") from None
    if v < 0:
        raise CheatFileError(f"bad offset {text!r}")
    return v


def parse_bytes(text: str) -> bytes:
    t = str(text).replace("-", "").replace(" ", "").strip()
    try:
        b = bytes.fromhex(t)
    except ValueError:
        raise CheatFileError(f"bad hex bytes {text!r}") from None
    if not b:
        raise CheatFileError("empty hex bytes")
    return b


def fmt_offset(v: int) -> str:
    return f"{v:X}"
