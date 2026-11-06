"""Scan files: every scan writes its matches (address in hex, value) to its own text file.

One folder per search (a first scan starts a new one), one file per scan in it::

    Documents/CheatMaker/scans/PPSA01285_03.000.005/2026-11-06_10-45-12/
        scan-001.txt   first scan
        scan-002.txt   next scan
        ...

Works the same for every game: nothing here knows about a particular title.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from cheatmaker.console.client import ScanHit
from cheatmaker.core.session import GameInfo
from cheatmaker.core.values import SHOWN, Scale, ValueType
from cheatmaker.paths import scans_dir

log = logging.getLogger(__name__)

SCAN_FILE_MAX = 100_000
"""A first search with an unknown value can match hundreds of millions of addresses;
listing them all would take minutes to fetch and gigabytes of disk. Past this many,
a file lists the first ones and says how many there were in all."""

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(text: str) -> str:
    """A folder name that is valid on Windows (title ids and versions are already)."""
    return _UNSAFE.sub("_", text).strip("._") or "game"


def fmt_address(address: int) -> str:
    return f"0x{address:010X}"


def fmt_scan_value(vtype: ValueType, raw: bytes) -> str:
    """The value as the game stores it. Floats to their own precision, no rounding
    to 2 places like the on-screen list, so a file is good for comparing scans."""
    data = raw[: vtype.size]
    if len(data) < vtype.size:
        return "?"
    v = vtype.decode(data)
    if vtype.is_float:
        # Shortest text that reads back as the same stored number.
        for digits in range(6, 18):
            text = format(v, f".{digits}g")
            if math.isnan(v) or vtype.encode(float(text)) == data:
                return text
        return repr(v)
    return str(v)


class ScanLog:
    """Writes the scan files for one Scanner. Never makes a scan fail: a file that
    can't be written is logged and skipped."""

    def __init__(
        self,
        root: Callable[[], Path] = scans_dir,
        max_lines: int = SCAN_FILE_MAX,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.root = root
        self.max_lines = max_lines
        self.enabled = True
        self.now = now
        self.folder: Path | None = None  # this search's folder, made on its first file
        self.number = 0  # scans written into ``folder``
        self.last_file: Path | None = None
        self._game: GameInfo | None = None

    @property
    def limit(self) -> int:
        """How many matches to fetch for a file (0 when files are off)."""
        if not self.enabled or not isinstance(self.max_lines, int):
            return 0
        return max(0, self.max_lines)

    def new_search(self, game: GameInfo) -> None:
        """A first scan: the next file goes into a new folder, numbered from 1."""
        self._game = game
        self.folder = None
        self.number = 0

    def write(
        self,
        vtype: ValueType | None,
        count: int,
        hits: Sequence[ScanHit],
        history: Sequence[str],
        scale: Scale = SHOWN,
    ) -> Path | None:
        """Write the next scan file. ``hits`` are the matches fetched (at most
        ``limit``), ``count`` how many the scan found in all."""
        if not self.enabled or self._game is None or vtype is None:
            return None
        try:
            path = self._next_path()
            path.write_text(self._text(vtype, count, hits, history, scale), encoding="utf-8")
        except OSError as exc:
            log.warning("action=scan_file reason=%s; scan kept, file skipped", exc)
            return None
        self.last_file = path
        log.info("action=scan_file n=%d count=%d path=%s", self.number, count, path)
        return path

    def _next_path(self) -> Path:
        if self.folder is None:
            g = self._game
            assert g is not None
            game_dir = self.root() / safe_name(f"{g.title_id}_{g.version}")
            stamp = self.now().strftime("%Y-%m-%d_%H-%M-%S")
            folder = game_dir / stamp
            n = 2
            while folder.exists():  # two searches in the same second
                folder = game_dir / f"{stamp}-{n}"
                n += 1
            folder.mkdir(parents=True)
            self.folder = folder
        self.number += 1
        return self.folder / f"scan-{self.number:03d}.txt"

    def _text(
        self,
        vtype: ValueType,
        count: int,
        hits: Sequence[ScanHit],
        history: Sequence[str],
        scale: Scale,
    ) -> str:
        g = self._game
        assert g is not None
        kind = "first scan" if self.number == 1 else "next scan"
        listed = len(hits)
        if listed >= count:
            listed_text = f"{listed:,} (all)"
        else:
            listed_text = f"the first {listed:,} of {count:,}"
        lines = [
            f"# Cheat Maker scan {self.number} ({kind})",
            f"# Game: {g.title} ({g.title_id}, version {g.version})",
            f"# This scan: {history[-1] if history else ''}",
            f"# Search so far: {' > '.join(history)}",
            f"# Number type: {vtype.label}",
        ]
        if scale is not SHOWN:
            lines.append(f"# Game stores it as: {scale.label}")
        lines += [
            f"# Time: {self.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"# Matches: {count:,}",
            f"# Listed: {listed_text}",
            "# Value = what the game had when this scan ran.",
            "",
            "Address\tValue",
        ]
        body = [
            f"{fmt_address(h.address)}\t{fmt_scan_value(vtype, h.value)}"
            for h in sorted(hits, key=lambda h: h.address)
        ]
        return "\n".join(lines + body) + "\n"
