"""Guided scanner (AC-3): first search, narrow down, live list of the last few."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from cheatmaker.console.client import ProgressFn
from cheatmaker.core.session import Session
from cheatmaker.core.values import Compare, ValueType, guess_types

log = logging.getLogger(__name__)
LOG_MATCHES = 50  # match lists this short are written to the log

FEW_MATCHES = 10  # AC-3.4 default; the player can change it (show_limit)
MAX_SHOW = 500
READ_SPAN = 0x1000  # matches this close together are read in one request


@dataclass(frozen=True)
class Match:
    address: int
    value: float | int
    raw: bytes


class Scanner:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.vtype: ValueType | None = None
        self.count: int | None = None
        self.history: list[str] = []
        self.show_limit = FEW_MATCHES  # list the matches once this few are left
        session.on_game_changed.append(lambda _g: self.reset())  # AC-2.3
        session.on_search_taken.append(self._taken)
        self.on_lost: list[Callable[[], None]] = []  # the search was ended by another

    def _taken(self) -> None:
        """Another search replaces ours on the console: forget it (no scan_cancel, the
        other one owns the console's search now) and tell the Find view."""
        if not self.active:
            return
        log.info("action=scan_lost count=%s: the console's search was used for another", self.count)
        self.vtype, self.count, self.history = None, None, []
        for cb in list(self.on_lost):
            cb()

    @property
    def active(self) -> bool:
        return self.count is not None

    @property
    def few_left(self) -> bool:
        return self.count is not None and 0 < self.count <= self.show_limit

    def reset(self) -> None:
        if self.session.client is not None and self.active:
            try:
                self.session.client.scan_cancel()
            except Exception:  # dropping a dead session must not raise
                log.debug("scan_cancel failed during reset", exc_info=True)
        self.vtype, self.count, self.history = None, None, []

    def first_scan(
        self,
        value: float | int | None,
        vtype: ValueType | None = None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
        value_range: tuple[float, float] | None = None,
    ) -> int:
        """Exact search when ``value`` is given, otherwise "Unknown value" (AC-3.3).

        ``value_range`` (unknown only): keep just values from low to high, e.g. health
        between 1 and 1000, which drops most of the hundreds of millions of candidates.

        With no ``vtype`` the scanner tries the likely types in order and keeps the
        first that finds something (AC-3.6).
        """
        client = self.session.require_client()
        game = self.session.require_game()
        regions = self.session.scannable_regions()
        self.reset()
        cmp = Compare.UNKNOWN if value is None else Compare.EXACT
        if vtype is not None:
            candidates = [vtype]
        elif value is None:
            candidates = [ValueType.FLOAT]  # health bars are almost always floats
        else:
            candidates = guess_types(value)
        count = 0
        for i, vt in enumerate(candidates):

            def scaled(p: float, i: int = i) -> None:
                if progress:
                    progress((i + p) / len(candidates))

            count = client.scan_start(game.pid, regions, vt, cmp, value, scaled, cancel)
            log.info(
                "action=scan_first type=%s cmp=%s value=%s count=%d",
                vt.name,
                cmp.value,
                value,
                count,
            )
            self.vtype = vt
            if count:
                break
        if progress:
            progress(1.0)
        self.count = count
        self.history = [f"{'unknown' if value is None else value}"]
        if value is None and value_range is not None and count:
            return self.next_scan(Compare.BETWEEN, value_range, None, cancel)  # type: ignore[arg-type]
        self._log_addresses()
        return count

    def next_scan(
        self,
        cmp: Compare,
        value: float | int | None = None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        if not self.active:
            raise ValueError("Start a new search first.")
        if cmp.needs_value and value is None:
            raise ValueError("Type the number you see now.")
        if cmp is Compare.BETWEEN:
            lo, hi = value  # type: ignore[misc]
            if lo > hi:
                raise ValueError("The first number of the range must be the smaller one.")
        count = self.session.require_client().scan_next(cmp, value, progress, cancel)
        log.info("action=scan_next cmp=%s value=%s count=%d", cmp.value, value, count)
        self.count = count
        self._log_addresses()
        self.history.append(cmp.value if value is None else str(value))
        return count

    def _log_addresses(self) -> None:
        """With only a few matches left, log where they are (helps console-test triage)."""
        if 0 < self.count <= LOG_MATCHES:
            hits = self.session.require_client().scan_results(self.count)
            log.info("action=matches addrs=%s", " ".join(f"{h.address:#x}" for h in hits))

    def matches(self, limit: int | None = None) -> list[Match]:
        """Current values of up to ``limit`` matches, read fresh (AC-3.4 live list)."""
        limit = self.show_limit if limit is None else limit
        if not self.active or self.vtype is None:
            return []
        client = self.session.require_client()
        game = self.session.require_game()
        size = self.vtype.size
        hits = client.scan_results(limit)
        # One read per cluster of nearby matches, not one per match (~36 ms each on PS5).
        values: dict[int, bytes] = {}
        addrs = sorted(h.address for h in hits)
        i = 0
        while i < len(addrs):
            j = i
            while j + 1 < len(addrs) and addrs[j + 1] + size - addrs[i] <= READ_SPAN:
                j += 1
            start = addrs[i]
            data = client.read(game.pid, start, addrs[j] + size - start)
            for a in addrs[i : j + 1]:
                values[a] = data[a - start : a - start + size]
            i = j + 1
        return [
            Match(h.address, self.vtype.decode(values[h.address]), values[h.address]) for h in hits
        ]
