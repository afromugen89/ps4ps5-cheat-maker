"""v2: move code-patch cheats to a new game version (update-proof cheats).

A GoldHEN / CheatRunner line is ``offset + on/off bytes`` for one game version. After
an update the game's code shifts, so the offset points at something else. To follow
it, the app learns a **signature** for every patch while the old version runs: the
original bytes at the patch plus the instructions after it, with the parts that change
between builds (jump distances, RIP-relative addresses) replaced by ``??``. Stored in
app data ``signatures/{title}_{version}.json``.

On a new version the app reads the game's code once and:

* **Patches** (e.g. a NOP or ``jmp cave`` over the writer): finds the signature, which
  must match exactly once. With no learned signature it tries the original ("off")
  bytes alone, if they are long enough and unique.
* **Code caves** (patches over zero bytes, like Celogamez's player-only routine): all
  caves move together to a new run of spare zero bytes at the end of the code.
* **Lines inside a cave** (e.g. a ``jne``/``jmp`` toggle at cave+9) move with the cave.

Then every jump and RIP-relative operand in the moved code is re-aimed: targets
inside a moved patch or the cave block follow it; any other target means the cheat
depends on game code the app can't follow, and that cheat is reported, not guessed.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import iced_x86 as ix

from cheatmaker.console.client import ProgressFn
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.core.session import Session
from cheatmaker.errors import ScanCancelled

log = logging.getLogger(__name__)

SIG_MIN = 16
"""Shortest signature the app stores (bytes), even if fewer are already unique."""
SIG_MAX = 96
FALLBACK_MIN = 6
"""Without a learned signature, the original bytes alone must be at least this long."""
READ_CHUNK = 4 << 20

_BRANCHES = (
    ix.FlowControl.UNCONDITIONAL_BRANCH,
    ix.FlowControl.CONDITIONAL_BRANCH,
    ix.FlowControl.CALL,
)


# -- signatures -----------------------------------------------------------------
@dataclass(frozen=True)
class Signature:
    data: bytes
    mask: bytes  # 1 = byte must match, 0 = wildcard

    def __post_init__(self) -> None:
        if len(self.data) != len(self.mask) or not self.data:
            raise ValueError("signature data and mask must be the same, non-zero length")

    def __len__(self) -> int:
        return len(self.data)

    def text(self) -> str:
        return " ".join(
            f"{b:02X}" if m else "??" for b, m in zip(self.data, self.mask, strict=True)
        )

    @classmethod
    def from_text(cls, text: str) -> Signature:
        data, mask = bytearray(), bytearray()
        for tok in text.split():
            if tok == "??":
                data.append(0)
                mask.append(0)
            else:
                data.append(int(tok, 16))
                mask.append(1)
        return cls(bytes(data), bytes(mask))

    def regex(self) -> re.Pattern[bytes]:
        parts = [
            re.escape(bytes([b])) if m else b"." for b, m in zip(self.data, self.mask, strict=True)
        ]
        return re.compile(b"".join(parts), re.DOTALL)

    def matches(self, data: bytes) -> bool:
        return len(data) >= len(self) and all(
            not m or data[i] == b for i, (b, m) in enumerate(zip(self.data, self.mask, strict=True))
        )


def _volatile(decoder: ix.Decoder, ins: ix.Instruction) -> list[tuple[int, int]]:
    """(offset, size) inside the instruction of bytes that change between builds."""
    co = decoder.get_constant_offsets(ins)
    out = []
    if (
        ins.flow_control in _BRANCHES
        and co.has_immediate
        and ins.op0_kind
        in (
            ix.OpKind.NEAR_BRANCH16,
            ix.OpKind.NEAR_BRANCH32,
            ix.OpKind.NEAR_BRANCH64,
        )
    ):
        out.append((co.immediate_offset, co.immediate_size))
    if ins.is_ip_rel_memory_operand and co.has_displacement:
        out.append((co.displacement_offset, co.displacement_size))
    return out


def make_signature(
    code: bytes,
    ip: int,
    unique: Callable[[Signature], bool] | None = None,
    min_len: int = SIG_MIN,
    max_len: int = SIG_MAX,
) -> Signature:
    """Signature for the code starting at ``ip``: whole instructions, jump distances and
    RIP-relative addresses wildcarded. Grows until it is ``min_len`` long and
    ``unique`` accepts it (or it reaches ``max_len``)."""
    data, mask = bytearray(), bytearray()
    decoder = ix.Decoder(64, code, ip=ip)
    sig: Signature | None = None
    for ins in decoder:
        start = ins.ip - ip
        raw = code[start : start + ins.len]
        if len(data) + len(raw) > max_len and data:
            break
        m = bytearray(b"\x01" * len(raw))
        for off, size in _volatile(decoder, ins):
            m[off : off + size] = bytes(size)
        data += raw
        mask += m
        if len(data) >= min_len and any(mask):
            sig = Signature(bytes(data), bytes(mask))
            if unique is None or unique(sig):
                return sig
    if not data:
        raise ValueError("no code to make a signature from")
    return sig or Signature(bytes(data), bytes(mask) if any(mask) else b"\x01" * len(data))


# -- the game's code, read once --------------------------------------------------
@dataclass
class CodeImage:
    """The game module's executable maps, as offsets from the module base."""

    blocks: list[tuple[int, bytes]]  # (offset, data)

    def find(self, sig: Signature, limit: int = 2) -> list[int]:
        rx = sig.regex()
        found: list[int] = []
        for start, data in self.blocks:
            for m in rx.finditer(data):
                found.append(start + m.start())
                if len(found) >= limit:
                    return found
        return found

    def contains(self, offset: int, size: int = 1) -> bool:
        return any(s <= offset and offset + size <= s + len(d) for s, d in self.blocks)

    def read(self, offset: int, size: int) -> bytes:
        for s, d in self.blocks:
            if s <= offset < s + len(d):
                return d[offset - s : offset - s + size]
        return b""

    def zero_run(self, size: int, gap: int = 16) -> int:
        """Offset of ``size`` spare zero bytes near the end of the code (16-aligned),
        leaving ``gap`` zero bytes after the game's last instruction."""
        start, data = max(self.blocks, key=lambda b: b[0] + len(b[1]))
        run = 0
        for i in range(len(data) - 1, -1, -1):
            run = run + 1 if data[i] == 0 else 0
            if run >= size + gap + 16:
                return (start + i + gap + 15) & ~15
        raise UpdateError("no spare space at the end of the game's code for the code caves")


def read_code_image(
    session: Session,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> CodeImage:
    game = session.require_game()
    client = session.require_client()
    maps = [
        m
        for m in client.maps(game.pid)
        if m.executable and game.module_base <= m.start < game.module_end
    ]
    total = sum(m.size for m in maps) or 1
    done = 0
    blocks = []
    for m in sorted(maps, key=lambda m: m.start):
        chunks = []
        for a in range(m.start, m.end, READ_CHUNK):
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("reading the game's code was cancelled")
            n = min(READ_CHUNK, m.end - a)
            chunks.append(client.read(game.pid, a, n))
            done += n
            if progress:
                progress(done / total)
        blocks.append((m.start - game.module_base, b"".join(chunks)))
    log.info("action=read_code maps=%d bytes=%d", len(blocks), total)
    return CodeImage(blocks)


class UpdateError(Exception):
    pass


# -- what each line of a cheat file is --------------------------------------------
def _code_lines(cf: CheatFile) -> list[Location]:
    """Every saveable code line once (cheats can share lines)."""
    seen: dict[int, Location] = {}
    for c in cf.cheats:
        for loc in c.locations:
            if loc.kind is LocationKind.CODE_PATCH:
                seen.setdefault(loc.offset, loc)
    return list(seen.values())


def is_cave(loc: Location) -> bool:
    """A code cave: new code written over spare zero bytes."""
    return loc.kind is LocationKind.CODE_PATCH and not any(loc.off) and any(loc.on)


@dataclass(frozen=True)
class _Layout:
    caves: list[Location]
    inside: list[Location]  # lines inside a cave: move with it
    patches: list[Location]  # need a signature

    @property
    def cave_block(self) -> tuple[int, int] | None:
        if not self.caves:
            return None
        return min(c.offset for c in self.caves), max(c.offset + len(c.on) for c in self.caves)


def _layout(cf: CheatFile) -> _Layout:
    lines = _code_lines(cf)
    caves = [loc for loc in lines if is_cave(loc)]

    def in_cave(loc: Location) -> bool:
        return any(c is not loc and c.offset <= loc.offset < c.offset + len(c.on) for c in caves)

    inside = [loc for loc in lines if loc not in caves and in_cave(loc)]
    patches = [loc for loc in lines if loc not in caves and loc not in inside]
    return _Layout(caves, inside, patches)


def patch_lines(cf: CheatFile) -> list[Location]:
    """The lines that need a learned signature to follow an update."""
    return _layout(cf).patches


# -- learn (old version running) ---------------------------------------------------
def learn(cf: CheatFile, image: CodeImage) -> dict[int, Signature]:
    """Signatures for every patch line in ``cf`` (offsets in the running version).
    Live bytes are overlaid with the lines' original bytes, so it works whether the
    cheats are on or off."""
    lay = _layout(cf)
    originals = [(loc.offset, loc.off) for loc in _code_lines(cf)]
    sigs: dict[int, Signature] = {}
    for loc in lay.patches:
        if not image.contains(loc.offset, len(loc.off)):
            log.info("action=learn_signature skip offset=%#x reason=not_code", loc.offset)
            continue
        window = bytearray(image.read(loc.offset, SIG_MAX + 16))
        for off, orig in originals:
            for i, b in enumerate(orig):
                j = off - loc.offset + i
                if 0 <= j < len(window):
                    window[j] = b
        try:
            sigs[loc.offset] = make_signature(
                bytes(window),
                loc.offset,
                unique=lambda s: len(image.find(s, 2)) <= 1,
                min_len=max(SIG_MIN, len(loc.off)),
            )
        except ValueError:
            continue
        log.info("action=learn_signature offset=%#x sig=%s", loc.offset, sigs[loc.offset].text())
    return sigs


# -- move (new version running) -----------------------------------------------------
@dataclass
class UpdateReport:
    moved: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    guessed: list[str] = field(default_factory=list)  # found without a learned signature

    def summary(self, version: str) -> str:
        total = len(self.moved) + len(self.failed)
        text = f"Updated {len(self.moved)} of {total} cheats for version {version}."
        if self.guessed:
            text += (
                " Found from their original bytes only (test them): "
                + ", ".join(self.guessed)
                + "."
            )
        if self.failed:
            text += " Not moved: " + "; ".join(f"{n} ({why})" for n, why in self.failed.items())
        return text


def _find_patch(
    loc: Location, sig: Signature | None, image: CodeImage
) -> tuple[int | None, str, bool]:
    """(new offset, problem, guessed)"""
    guessed = False
    if sig is None:
        if len(loc.off) < FALLBACK_MIN:
            return None, f"no signature learned for +{loc.offset:X}", False
        sig = Signature(loc.off, b"\x01" * len(loc.off))
        guessed = True
    hits = image.find(sig, 2)
    if not hits:
        return None, f"code at +{loc.offset:X} not found in this version", guessed
    if len(hits) > 1:
        return None, f"code at +{loc.offset:X} found in more than one place", guessed
    return hits[0], "", guessed


def _reaim(data: bytes, old_ip: int, new_ip: int, move: Callable[[int], int | None]) -> bytes:
    """Re-aim jumps / RIP-relative operands in ``data`` (code moved from old_ip to
    new_ip). Raises UpdateError when a target can't be followed."""
    out = bytearray(data)
    decoder = ix.Decoder(64, data, ip=old_ip)
    for ins in decoder:
        for off, size in _volatile(decoder, ins):
            if (
                ins.is_ip_rel_memory_operand
                and off == decoder.get_constant_offsets(ins).displacement_offset
            ):
                target = ins.ip_rel_memory_address
            else:
                target = ins.near_branch_target
            new_target = move(target)
            if new_target is None:
                raise UpdateError(f"it uses game code at +{target:X} the app can't follow")
            end_new = new_ip + (ins.ip - old_ip) + ins.len
            rel = new_target - end_new
            lim = 1 << (8 * size - 1)
            if not -lim <= rel < lim:
                raise UpdateError(f"a jump at +{ins.ip:X} can't reach its target any more")
            pos = ins.ip - old_ip + off
            out[pos : pos + size] = rel.to_bytes(size, "little", signed=True)
    return bytes(out)


def update_file(
    cf: CheatFile,
    sigs: dict[int, Signature],
    image: CodeImage,
    new_version: str,
) -> tuple[CheatFile, UpdateReport]:
    """A copy of ``cf`` moved to the running version (whose code is ``image``)."""
    lay = _layout(cf)
    report = UpdateReport()
    problems: dict[int, str] = {}
    new_off: dict[int, int] = {}
    new_orig: dict[int, bytes] = {}
    guessed: set[int] = set()

    patch_moves: list[tuple[int, int, int]] = []  # (old start, old end incl, delta)
    for loc in lay.patches:
        hit, why, was_guess = _find_patch(loc, sigs.get(loc.offset), image)
        if hit is None:
            problems[loc.offset] = why
            continue
        now = image.read(hit, len(loc.off))
        sig = sigs.get(loc.offset)
        if sig is not None and not Signature(
            sig.data[: len(loc.off)], sig.mask[: len(loc.off)]
        ).matches(now):
            problems[loc.offset] = f"code at +{loc.offset:X} changed in this version"
            continue
        new_off[loc.offset], new_orig[loc.offset] = hit, now
        if was_guess:
            guessed.add(loc.offset)
        patch_moves.append((loc.offset, loc.offset + len(loc.off), hit - loc.offset))

    block = lay.cave_block
    cave_delta: int | None = None
    if block is not None:
        try:
            cave_delta = image.zero_run(block[1] - block[0]) - block[0]
        except UpdateError as exc:
            for loc in lay.caves + lay.inside:
                problems[loc.offset] = str(exc)
        else:
            for loc in lay.caves + lay.inside:
                new_off[loc.offset] = loc.offset + cave_delta

    def move(target: int) -> int | None:
        for start, end, delta in patch_moves:
            if start <= target <= end:
                return target + delta
        if block is not None and cave_delta is not None and block[0] <= target <= block[1]:
            return target + cave_delta
        return None

    lines: dict[int, Location] = {}
    for loc in lay.patches + lay.caves + lay.inside:
        if loc.offset in problems or loc.offset not in new_off:
            continue
        dest = new_off[loc.offset]
        try:
            on = _reaim(loc.on, loc.offset, dest, move)
            off = new_orig.get(loc.offset) or _reaim(loc.off, loc.offset, dest, move)
        except UpdateError as exc:
            problems[loc.offset] = str(exc)
            continue
        lines[loc.offset] = replace(loc, offset=dest, on=on, off=off)
    for loc in lay.inside:  # a line inside a cave is useless if its cave couldn't move
        host = next(c for c in lay.caves if c.offset <= loc.offset < c.offset + len(c.on))
        if host.offset in problems:
            problems.setdefault(loc.offset, problems[host.offset])
            lines.pop(loc.offset, None)

    cheats: list[Cheat] = []
    for c in cf.cheats:
        if c.session_only:
            report.failed[c.name] = "this session only"
            continue
        # report the root cause first: a patch not found, before the cave that jumps to it
        first = sorted(c.locations, key=lambda loc: loc not in lay.patches)
        bad = [problems[loc.offset] for loc in first if loc.offset in problems]
        missing = [
            loc
            for loc in c.locations
            if loc.kind is LocationKind.CODE_PATCH and loc.offset not in lines
        ]
        others = [loc for loc in c.locations if loc.kind is not LocationKind.CODE_PATCH]
        if bad or missing or others:
            report.failed[c.name] = bad[0] if bad else "it changes saved values, not code"
            continue
        cheats.append(
            replace(c, locations=[lines[loc.offset] for loc in c.locations], enabled=False)
        )
        report.moved.append(c.name)
        if any(loc.offset in guessed for loc in c.locations):
            report.guessed.append(c.name)
    new = replace(cf, version=new_version, cheats=cheats, credits=list(cf.credits))
    log.info(
        "action=update_version from=%s to=%s moved=%d failed=%d",
        cf.version,
        new_version,
        len(report.moved),
        len(report.failed),
    )
    return new, report


# -- saved signatures: one file per game version -------------------------------------
def signatures_path(title_id: str, version: str) -> Path:
    from cheatmaker.paths import app_data_dir

    return app_data_dir() / "signatures" / f"{title_id.upper()}_{version}.json"


def save_signatures(path: Path, title_id: str, version: str, sigs: dict[int, Signature]) -> None:
    merged = load_signatures(path)
    merged.update(sigs)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "title_id": title_id,
        "version": version,
        "signatures": {f"{off:#x}": s.text() for off, s in sorted(merged.items())},
    }
    path.write_text(json.dumps(doc, indent=1))


def load_signatures(path: Path) -> dict[int, Signature]:
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    return {int(k, 16): Signature.from_text(v) for k, v in doc.get("signatures", {}).items()}


__all__ = [
    "CodeImage",
    "Signature",
    "UpdateError",
    "UpdateReport",
    "is_cave",
    "learn",
    "load_signatures",
    "make_signature",
    "patch_lines",
    "read_code_image",
    "save_signatures",
    "signatures_path",
    "update_file",
]
