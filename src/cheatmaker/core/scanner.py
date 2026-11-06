"""Guided scanner (AC-3): first search, narrow down, live list of the last few."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from cheatmaker.console.client import ProgressFn
from cheatmaker.core.scan_log import ScanLog
from cheatmaker.core.session import Session
from cheatmaker.core.values import SHOWN, Compare, Scale, ValueType, guess_types

log = logging.getLogger(__name__)
LOG_MATCHES = 50  # match lists this short are written to the log

FEW_MATCHES = 10  # AC-3.4 default; the player can change it (show_limit)
MAX_SHOW = 500
READ_SPAN = 0x1000  # matches this close together are read in one request
STILL_ROUNDS = 5  # AC-H4 "Stand still"
STILL_INTERVAL_S = 2.0


def default_unit(value: float | int) -> float:
    """Unit for a number given without its typed text: whole numbers 1, else from repr."""
    if isinstance(value, int) or float(value).is_integer():
        return 1.0
    exponent = Decimal(repr(float(value))).as_tuple().exponent
    return 10.0**exponent if isinstance(exponent, int) and exponent < 0 else 1.0


def window(value: float | int, unit: float) -> tuple[float, float]:
    """Values less than ``unit`` away from ``value`` (open ends), as an inclusive range."""
    return (math.nextafter(value - unit, math.inf), math.nextafter(value + unit, -math.inf))


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
        self.scale: Scale = SHOWN  # AC-H3, kept until Start over
        self.scan_log = ScanLog()  # each scan's matches go to their own text file
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
        self.scale = SHOWN
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
        self.scale = SHOWN

    def first_scan(
        self,
        value: float | int | None,
        vtype: ValueType | None = None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
        value_range: tuple[float, float] | None = None,
        unit: float | None = None,
        scale: Scale = SHOWN,
    ) -> int:
        """Exact search when ``value`` is given, otherwise "Unknown value" (AC-3.3).

        ``value_range`` (unknown only): keep just values from low to high, e.g. health
        between 1 and 1000, which drops most of the hundreds of millions of candidates.

        ``unit`` is one unit of the last digit typed (AC-H2, default: 1 for whole numbers)
        and ``scale`` how the game stores the number (AC-H3). When either makes the match
        window anything but "less than 1 away", the search runs as an unknown search kept
        to that window.

        With no ``vtype`` the scanner tries the likely types in order and keeps the
        first that finds something (AC-3.6).
        """
        label = "unknown" if value is None else f"{value}"
        if scale is not SHOWN:
            label += f" ({scale.label})"
        if value is None and value_range is not None:
            label = f"unknown, {value_range}"
        if value is not None:
            u = (unit if unit is not None else default_unit(value)) * scale.factor
            if u != 1 or scale.decimal:
                # A window other than "less than 1 away": one ValueBetween search per type.
                stored = scale.apply(value)
                types = [vtype] if vtype is not None else guess_types(stored)
                if scale.decimal and vtype is None:
                    types = [t for t in types if t.is_float]
                rng = window(stored, u)
                count = self._first(rng, None, progress, cancel, Compare.BETWEEN, types)
                self.scale = scale
                self.history = [label]
                self._record()
                return count
        count = self._first(None if value is None else scale.apply(value), vtype, progress, cancel)
        self.scale = scale
        self.history = [label]
        if value is None and value_range is not None and count:
            count = self._narrow(Compare.BETWEEN, value_range, None, cancel)
        self._record()
        return count

    def _first(
        self,
        value,
        vtype: ValueType | None,
        progress: ProgressFn | None,
        cancel: threading.Event | None,
        cmp: Compare | None = None,
        types: list[ValueType] | None = None,
    ) -> int:
        client = self.session.require_client()
        game = self.session.require_game()
        regions = self.session.scannable_regions()
        self.reset()
        self.scan_log.new_search(game)
        if cmp is None:
            cmp = Compare.UNKNOWN if value is None else Compare.EXACT
        if types:
            candidates = types
        elif vtype is not None:
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
        return count

    def next_scan(
        self,
        cmp: Compare,
        value: float | int | tuple[float, float] | None = None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
        unit: float | None = None,
    ) -> int:
        """Narrow down. EXACT and the "by" compares take the number the player typed
        (``unit`` as in ``first_scan``); the search's scale is applied here."""
        if not self.active:
            raise ValueError("Start a new search first.")
        if cmp.needs_value and value is None:
            raise ValueError(
                "Type how much it changed by." if cmp.by_amount else "Type the number you see now."
            )
        label = cmp.value if value is None else str(value)
        if cmp.by_amount:
            label = f"{cmp.value} {value}"
            if value <= 0:  # type: ignore[operator]
                raise ValueError("Type how much it changed by, as a number more than 0.")
            u = (unit if unit is not None else default_unit(value)) * self.scale.factor
            amount = self.scale.apply(value)  # type: ignore[arg-type]
            if self.vtype is not None and not self.vtype.is_float:
                if not float(amount).is_integer():
                    raise ValueError(
                        f"{self.vtype.label} values change by whole numbers; {amount:g} isn't one."
                    )
                amount = int(amount)
            value = (amount, u)
        elif cmp is Compare.EXACT:
            u = (unit if unit is not None else default_unit(value)) * self.scale.factor
            if u != 1 or self.scale.decimal:
                cmp, value = Compare.BETWEEN, window(self.scale.apply(value), u)  # type: ignore[arg-type]
            else:
                value = self.scale.apply(value)  # type: ignore[arg-type]
        elif cmp is Compare.BETWEEN:
            lo, hi = value  # type: ignore[misc]
            if lo > hi:
                raise ValueError("The first number of the range must be the smaller one.")
        count = self._narrow(cmp, value, progress, cancel)
        self.history.append(label)
        self._record()
        return count

    def _narrow(
        self,
        cmp: Compare,
        value,
        progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> int:
        count = self.session.require_client().scan_next(cmp, value, progress, cancel)
        log.info("action=scan_next cmp=%s value=%s count=%d", cmp.value, value, count)
        self.count = count
        return count

    def stand_still(
        self,
        rounds: int | None = None,
        interval_s: float | None = None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> int:
        """AC-H4: "Same" scans every ``interval_s`` while the player doesn't touch the
        controller. Stops early once two scans in a row drop nothing, or 0-1 are left."""
        if not self.active:
            raise ValueError("Start a new search first.")
        rounds = STILL_ROUNDS if rounds is None else rounds
        interval_s = STILL_INTERVAL_S if interval_s is None else interval_s
        count = self.count or 0
        quiet = 0
        for i in range(rounds):
            if cancel is not None and cancel.is_set():
                break
            if i:
                if cancel is not None:
                    if cancel.wait(interval_s):
                        break
                else:
                    sleep(interval_s)
            before = count
            count = self.next_scan(Compare.UNCHANGED, None, None, cancel)
            if progress:
                progress((i + 1) / rounds)
            quiet = quiet + 1 if count == before else 0
            if quiet >= 2 or count <= 1:
                break
        if progress:
            progress(1.0)
        return count

    def _record(self) -> None:
        """After every scan: write its scan file, and with only a few matches left
        log where they are (helps console-test triage). One fetch serves both."""
        count = self.count or 0
        want = min(count, max(self.scan_log.limit, LOG_MATCHES if count <= LOG_MATCHES else 0))
        hits = self.session.require_client().scan_results(want) if want else []
        if 0 < count <= LOG_MATCHES:
            log.info("action=matches addrs=%s", " ".join(f"{h.address:#x}" for h in hits))
        shown = hits[: self.scan_log.limit]
        self.scan_log.write(self.vtype, count, shown, self.history, self.scale)

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
