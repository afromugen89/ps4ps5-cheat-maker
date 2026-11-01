"""Guided scanner (AC-3): first search, narrow down, live list of the last few."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from cheatmaker.console.client import ProgressFn
from cheatmaker.core.session import Session
from cheatmaker.core.values import Compare, ValueType, guess_types

log = logging.getLogger(__name__)

FEW_MATCHES = 10  # AC-3.4


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
        session.on_game_changed.append(lambda _g: self.reset())  # AC-2.3

    @property
    def active(self) -> bool:
        return self.count is not None

    @property
    def few_left(self) -> bool:
        return self.count is not None and 0 < self.count <= FEW_MATCHES

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
    ) -> int:
        """Exact search when ``value`` is given, otherwise "Unknown value" (AC-3.3).

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
        self._log_addresses()
        self.history = [f"{'unknown' if value is None else value}"]
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
        count = self.session.require_client().scan_next(cmp, value, progress, cancel)
        log.info("action=scan_next cmp=%s value=%s count=%d", cmp.value, value, count)
        self.count = count
        self._log_addresses()
        self.history.append(cmp.value if value is None else str(value))
        return count

    def _log_addresses(self) -> None:
        """With only a few matches left, log where they are (helps console-test triage)."""
        if 0 < self.count <= FEW_MATCHES:
            hits = self.session.require_client().scan_results(self.count)
            log.info("action=matches addrs=%s", " ".join(f"{h.address:#x}" for h in hits))

    def matches(self, limit: int = FEW_MATCHES) -> list[Match]:
        """Current values of up to ``limit`` matches, read fresh (AC-3.4 live list)."""
        if not self.active or self.vtype is None:
            return []
        client = self.session.require_client()
        game = self.session.require_game()
        out = []
        for hit in client.scan_results(limit):
            raw = client.read(game.pid, hit.address, self.vtype.size)
            out.append(Match(hit.address, self.vtype.decode(raw), raw))
        return out
