"""Guided scanner (AC-3): first search, narrow down, live list of the last few."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from cheatmaker.console.client import ProgressFn, ScanHit
from cheatmaker.core import percent_filter
from cheatmaker.core.grouping import Group, group_matches
from cheatmaker.core.hit_tester import TEST_LIMIT
from cheatmaker.core.scan_log import ScanLog
from cheatmaker.core.session import Session
from cheatmaker.core.values import (
    SHOWN,
    UNKNOWN_TYPES,
    Compare,
    Scale,
    ValueType,
    guess_types,
    percent_parts,
    window,
)
from cheatmaker.errors import ConnectionLost, ScanCancelled

log = logging.getLogger(__name__)
LOG_MATCHES = 50  # match lists this short are written to the log

FEW_MATCHES = 10  # AC-3.4 default; the player can change it (show_limit)
MAX_SHOW = 500
READ_SPAN = 0x1000  # matches this close together are read in one request
STILL_ROUNDS = 5  # AC-H4 "Stand still"
STILL_INTERVAL_S = 2.0
GROUP_LIMIT = TEST_LIMIT
"""From this many matches down, linked matches are shown as groups."""
RECORD_LIMIT = 20_000
"""From this many matches down, every scan keeps each match's value, for grouping. Well
above GROUP_LIMIT, so the changes that narrow the search down to GROUP_LIMIT already
count: the groups are there as soon as the list is (fetching 20,000 values takes about
0.05 s on the PS5, the scan file fetches up to 100,000 anyway)."""
PERCENT_PC_LIMIT = 20_000
"""A percent scan in a Full search checks at most this many matches on the PC
(setting ``percent_pc_limit``); above it the scan runs as Went up / Went down."""
ABOUT = (75, 50, 25)  # AC-P3 buttons
TOO_MANY = "too many"  # Scanner.note after a percent scan that ran as a plain compare
NOTED = "noted"  # Scanner.note after a percent scan that could only note the percent
"""A range every real value is in: a scan with it drops nothing but makes now the
baseline (x10 of it still fits a 4-byte decimal)."""
GROUP_SCANS = 3  # grouping needs each match's value from this many scans (2 changes)
READ_CHUNK = 512  # spans read between Cancel / progress checks


def default_unit(value: float | int) -> float:
    """Unit for a number given without its typed text: whole numbers 1, else from repr."""
    if isinstance(value, int) or float(value).is_integer():
        return 1.0
    exponent = Decimal(repr(float(value))).as_tuple().exponent
    return 10.0**exponent if isinstance(exponent, int) and exponent < 0 else 1.0


@dataclass(frozen=True)
class Match:
    address: int
    value: float | int
    raw: bytes
    vtype: ValueType | None = None  # set when the search covers several types
    way: Scale | None = None  # how it keeps the number, in a percent search


class Scanner:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.vtype: ValueType | None = None  # None while several types still match
        self.types: dict[ValueType, int] = {}  # matches left per type
        self.count: int | None = None
        self.history: list[str] = []
        self.show_limit = FEW_MATCHES  # list the matches once this few are left
        self.scale: Scale = SHOWN  # AC-H3, kept until Start over
        self.scan_log = ScanLog()  # each scan's matches go to their own text file
        self.pc_limit = PERCENT_PC_LIMIT
        self._clear_search()
        session.on_game_changed.append(lambda _g: self.reset())  # AC-2.3
        session.on_search_taken.append(self._taken)
        self.on_lost: list[Callable[[], None]] = []  # the search was ended by another

    def _taken(self) -> None:
        """Another search replaces ours on the console: forget it (no scan_cancel, the
        other one owns the console's search now) and tell the Find view."""
        if not self.active:
            return
        log.info("action=scan_lost count=%s: the console's search was used for another", self.count)
        self._clear_search()
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
        self._clear_search()

    def _clear_search(self) -> None:
        self.vtype, self.count, self.history = None, None, []
        self.types = {}
        self.parts: dict[tuple[ValueType, Scale | None], int] = {}  # matches per (type, way)
        self.scale = SHOWN
        # Percent search (spec-health-finding AC-P1..P3), kept until Start over.
        self.percent_search = False  # matches are tied to a way; plain numbers count as %
        self.full_mode = False  # "It's full now" was pressed
        self.last_pct: float | int | None = None  # what the game showed at the last scan
        self.last_unit = 0.0
        self.full: dict[tuple[int, ValueType], percent_filter.Full] = {}
        self.note: str | None = None  # TOO_MANY after a percent scan run as a compare
        # Grouping (AC-G1): each match's value at every scan since RECORD_LIMIT or fewer
        # where a value changed (``recorded`` of them).
        self.values: dict[tuple[int, ValueType], list[float | None]] = {}
        self.tags: dict[tuple[int, ValueType], Scale | None] = {}
        self.recorded = 0
        self._groups: list[Group] | None = None

    @property
    def mixed(self) -> bool:
        """The search still has matches of more than one number type."""
        return self.active and self.vtype is None and len(self.types) > 1

    def _sync_types(self, fallback: ValueType | None) -> None:
        """After a scan: which types still match. One left (or none) = a plain search."""
        counts_fn = getattr(self.session.require_client(), "scan_counts", None)
        counts = counts_fn() if counts_fn is not None else {}
        # A search that ends with no matches keeps its type(s), like a one-type search.
        self.types = (
            {t: n for t, n in counts.items() if n}
            or dict(counts)
            or ({fallback: 0} if fallback is not None else {})
        )
        if len(self.types) == 1:
            self.vtype = next(iter(self.types))
        elif len(self.types) > 1:
            self.vtype = None
        ways_fn = getattr(self.session.require_client(), "scan_way_counts", None)
        self.parts = ways_fn() if ways_fn is not None else {}

    @property
    def has_ways(self) -> bool:
        """Some matches are tied to a way (percent, fraction, x10, of full)."""
        return any(w is not None for _t, w in self.parts)

    def first_scan(
        self,
        value: float | int | None,
        vtype: ValueType | None = None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
        value_range: tuple[float, float] | None = None,
        unit: float | None = None,
        scale: Scale = SHOWN,
        label: str | None = None,
    ) -> int:
        """Exact search when ``value`` is given, otherwise "Unknown value" (AC-3.3).

        ``value_range`` (unknown only): keep just values from low to high, e.g. health
        between 1 and 1000, which drops most of the hundreds of millions of candidates.

        ``unit`` is one unit of the last digit typed (AC-H2, default: 1 for whole numbers)
        and ``scale`` how the game stores the number (AC-H3). When either makes the match
        window anything but "less than 1 away", the search runs as an unknown search kept
        to that window. ``label`` names the scan in the history instead.

        With no ``vtype`` the scanner tries the likely types in order and keeps the
        first that finds something (AC-3.6). An unknown search with no ``vtype`` looks
        for every type in UNKNOWN_TYPES at once (decimals and whole numbers).
        """
        given = label
        label = "unknown" if value is None else f"{value}"
        if scale is not SHOWN:
            label += f" ({scale.label})"
        if value is None and value_range is not None:
            label = f"unknown, {value_range}"
        label = given if given is not None else label
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
            candidates = [UNKNOWN_TYPES]  # type: ignore[list-item]  # all side by side
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
                "+".join(t.name for t in vt) if isinstance(vt, tuple) else vt.name,
                cmp.value,
                value,
                count,
            )
            first = vt[0] if isinstance(vt, tuple) else vt
            self.vtype = first
            self.count = count
            self._sync_types(first)
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
        label: str | None = None,
    ) -> int:
        """Narrow down. EXACT and the "by" compares take the number the player typed
        (``unit`` as in ``first_scan``); the search's scale is applied here.

        In a percent search a plain number counts as a percent (AC-P1). Any compare but
        Same forgets what percent the game showed at the last scan (AC-P2)."""
        if not self.active:
            raise ValueError("Start a new search first.")
        if cmp.needs_value and value is None:
            raise ValueError(
                "Type how much it changed by." if cmp.by_amount else "Type the number you see now."
            )
        if cmp is Compare.EXACT and (self.percent_search or self.full_mode):
            # In a percent or Full search a plain number counts as a percent, so every
            # match is checked the same way (AC-P1).
            if not 0 <= value <= 100:  # type: ignore[operator]
                kind = "Full" if self.full_mode else "percent"
                raise ValueError(
                    f"In a {kind} search, type the % the game shows (e.g. 40%). To search "
                    f"for the number {value:g} instead, press Start over."
                )
            return self.percent_scan(value, unit, None, progress, cancel)  # type: ignore[arg-type]
        given = label
        label = cmp.value if value is None else str(value)
        if cmp.by_amount:
            label = f"{cmp.value} {value}"
            if value <= 0:  # type: ignore[operator]
                raise ValueError("Type how much it changed by, as a number more than 0.")
            u = (unit if unit is not None else default_unit(value)) * self.scale.factor
            amount = self.scale.apply(value)  # type: ignore[arg-type]
            if self.vtype is not None and not self.vtype.is_float:  # mixed: per type
                if not float(amount).is_integer():
                    raise ValueError(
                        f"{self.vtype.label} values change by whole numbers; {amount:g} isn't one."
                    )
                amount = int(amount)
            value = (amount, u)
        elif cmp is Compare.EXACT:
            u = (unit if unit is not None else default_unit(value)) * self.scale.factor
            if u != 1 or self.scale.decimal or self.has_ways:  # ways scale the range
                cmp, value = Compare.BETWEEN, window(self.scale.apply(value), u)  # type: ignore[arg-type]
            else:
                value = self.scale.apply(value)  # type: ignore[arg-type]
        elif cmp is Compare.BETWEEN:
            lo, hi = value  # type: ignore[misc]
            if lo > hi:
                raise ValueError("The first number of the range must be the smaller one.")
        count = self._narrow(cmp, value, progress, cancel)
        if cmp is not Compare.UNCHANGED:
            self.last_pct = None
        self.history.append(given if given is not None else label)
        self._record()
        return count

    # -- percent search (spec-health-finding AC-P1..P3) -------------------------------
    def percent_scan(
        self,
        value: float | int,
        unit: float | None = None,
        vtype: ValueType | None = None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """The game shows ``value`` percent. First scan: keep values holding it as the
        percent, a fraction or x10 (``vtype``: that type only, else decimals and whole
        numbers). Later: narrow the same ways; in a Full search also as a percent of each
        match's value when full."""
        if not 0 <= value <= 100:
            raise ValueError("Type a percent from 0 to 100.")
        unit = default_unit(value) if unit is None else unit
        if not self.active:
            return self._percent_first(value, unit, vtype, progress, cancel)
        text = f"{value:g}%"
        if self.percent_search and not self.full_mode and not self._untied():
            count = self._narrow(Compare.BETWEEN, window(value, unit), progress, cancel)
        else:
            count, text = self._percent_pc(percent_filter.Target(value, unit), progress, cancel)
        self.history.append(text)
        self._record()
        return count

    def _untied(self) -> bool:
        """Some matches fit several ways at the last PC check and aren't tied to one yet
        (they are checked on the PC again, every way)."""
        return any(w is None for _t, w in self.parts)

    def about_scan(
        self,
        value: float | int,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """AC-P3: the bar looks about ``value`` percent full (Full search only)."""
        if not (self.active and self.full_mode):
            raise ValueError("Fill it up and press It's full now first.")
        target = percent_filter.Target(value, percent_filter.ABOUT_TOL, about=True)
        count, text = self._percent_pc(target, progress, cancel)
        self.history.append(text)
        self._record()
        return count

    def mark_full(
        self,
        vtype: ValueType | None = None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """AC-P2 "It's full now": a first unknown scan (decimals and whole numbers unless
        ``vtype``) remembered as the Full point. During a search: a Same scan (the player
        keeps it full), and the Full point starts again."""
        if self.active:
            count = self.next_scan(
                Compare.UNCHANGED, None, progress, cancel, label="full again (same)"
            )
        else:
            count = self.first_scan(None, vtype, progress, cancel, label="full")
        self.full_mode = True
        self.last_pct, self.last_unit = 100, 0.0
        self.full = {}
        return count

    def _percent_first(
        self,
        value: float | int,
        unit: float,
        vtype: ValueType | None,
        progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> int:
        parts = percent_parts(value, vtype)
        if not parts:
            raise ValueError(
                f"{vtype.label if vtype else 'That type'} can't hold {value:g}%. Pick a "
                "decimal type, or Pick for me."
            )
        client = self.session.require_client()
        game = self.session.require_game()
        regions = self.session.scannable_regions()
        self.reset()
        self.scan_log.new_search(game)
        types = tuple(vt for vt, _w in parts)
        ways = tuple(w for _vt, w in parts)
        rng = window(value, unit)
        count = client.scan_start(
            game.pid, regions, types, Compare.BETWEEN, rng, progress, cancel, ways=ways
        )
        log.info(
            "action=scan_percent value=%s unit=%s parts=%s count=%d",
            value,
            unit,
            ",".join(f"{t.name}:{w.label}" for t, w in parts),
            count,
        )
        self.vtype, self.count = types[0], count
        self._sync_types(types[0])
        self.percent_search = True
        self.last_pct, self.last_unit = value, unit
        self.history = [f"{value:g}%"]
        if progress:
            progress(1.0)
        self._record()
        return count

    def _percent_pc(
        self,
        target: percent_filter.Target,
        progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> tuple[int, str]:
        """A percent scan checked on the PC. In a Full search with more than
        ``pc_limit`` matches it runs as a plain Went down / Went up instead. When the
        percent can't be used to narrow (the last scan's percent isn't known, or it is too
        close to this one to say up or down), the scan only notes it: nothing is dropped
        and now becomes the baseline for the next one."""
        text = f"about {target.value:g}%" if target.about else f"{target.value:g}%"
        self.note = None
        count = self.count or 0
        last = self.last_pct
        if count > self.pc_limit:
            if not self.full_mode:
                raise ValueError(
                    f"{count:,} matches is too many to check by percent. Make it go up or "
                    "down and press Went up or Went down first, then type the % again."
                )
            if last is None or abs(target.value - last) < self.last_unit + target.unit:
                return self._note_percent(target, text, progress, cancel)
            cmp, word = self._compare_for(target.value)
            count = self._narrow(cmp, None, progress, cancel)
            self.note = TOO_MANY
            self.last_pct, self.last_unit = target.value, target.unit
            return count, f"{text} (as {word})"
        client = self.session.require_client()
        hits = client.scan_results(count) if count else []
        if cancel is not None and cancel.is_set():
            raise ScanCancelled("scan cancelled by the player")
        raw = self._read_values(hits, progress, cancel)
        ests: dict[tuple[int, ValueType], percent_filter.Full | None] = {}
        for h in hits:
            vt = h.vtype or self.vtype
            if vt is None:
                continue
            key = (h.address, vt)
            est = self.full.get(key)
            if est is None and self.full_mode:
                est = percent_filter.full_estimate(vt.decode(h.value), last, self.last_unit)
            ests[key] = est
        if self.full_mode and any(est is None for est in ests.values()):
            # No value when full to check some match against (a manual compare since the
            # last percent, or it was 0%): note it, and now is the baseline.
            now_hits = [
                ScanHit(h.address, raw[h.address][: vt.size], vt, h.way)
                for h in hits
                if (vt := h.vtype or self.vtype) is not None and h.address in raw
            ]
            return self._note_percent(target, text, progress, cancel, now_hits)
        kept: list[ScanHit] = []
        full: dict[tuple[int, ValueType], percent_filter.Full] = {}
        for h in hits:
            vt = h.vtype or self.vtype
            if vt is None:
                continue
            key = (h.address, vt)
            est = ests[key]
            now = raw[h.address][: vt.size]
            ways = percent_filter.fitting(vt, h.way, vt.decode(now), est, target)
            if not ways:
                continue
            way = percent_filter.tie(ways, est)
            kept.append(ScanHit(h.address, now, vt, way))
            if est is not None:
                full[key] = est
        count = client.scan_restrict(kept)
        self.full = full  # each match's value when full, kept once known
        self.count = count
        self._sync_types(self.vtype)
        if not self.full_mode:
            self.percent_search = True  # the matches are tied to a way now
        self.last_pct, self.last_unit = target.value, target.unit
        log.info(
            "action=scan_percent_pc target=%s about=%s checked=%d count=%d",
            target.value,
            target.about,
            len(hits),
            count,
        )
        if progress:
            progress(1.0)
        return count, text

    def _note_percent(
        self,
        target: percent_filter.Target,
        text: str,
        progress: ProgressFn | None,
        cancel: threading.Event | None,
        now: list[ScanHit] | None = None,
    ) -> tuple[int, str]:
        """Drop nothing, but make now the baseline at which the game shows ``target``.
        ``now``: every match with its value now, already read (else read again where the
        matches are)."""
        client = self.session.require_client()
        if now is not None:
            count = client.scan_restrict(now)
        else:
            count = client.scan_rebase(progress, cancel)
        self.count = count
        self._sync_types(self.vtype)
        log.info("action=scan_percent_noted count=%d", count)
        if progress:
            progress(1.0)
        self.note = NOTED
        self.last_pct, self.last_unit = target.value, target.unit
        return count, f"{text} (noted)"

    def _compare_for(self, pct: float | int) -> tuple[Compare, str]:
        """The plain compare from the last percent to ``pct`` (they differ clearly)."""
        assert self.last_pct is not None
        if pct < self.last_pct:
            return Compare.DECREASED, "went down"
        return Compare.INCREASED, "went up"

    def _narrow(
        self,
        cmp: Compare,
        value,
        progress: ProgressFn | None,
        cancel: threading.Event | None,
    ) -> int:
        self.note = None
        count = self.session.require_client().scan_next(cmp, value, progress, cancel)
        self.count = count
        self._sync_types(self.vtype)
        log.info(
            "action=scan_next cmp=%s value=%s count=%d types=%s",
            cmp.value,
            value,
            count,
            ",".join(f"{t.name}:{n}" for t, n in self.types.items()),
        )
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
        """After every scan: write its scan file, keep each match's value for grouping
        (RECORD_LIMIT or fewer left), and with only a few matches left log where they are
        (helps console-test triage). One fetch serves all three."""
        count = self.count or 0
        group = 0 < count <= RECORD_LIMIT
        want = min(count, max(self.scan_log.limit, LOG_MATCHES if count <= LOG_MATCHES else 0))
        if group:
            want = count
        hits = []
        if want:
            started = time.monotonic()
            try:
                hits = self.session.require_client().scan_results(want)
            except ConnectionLost:
                raise  # the link is gone: the UI must hear about it now
            except Exception as exc:  # the scan itself worked; only the file/log suffers
                log.warning("action=scan_file_fetch reason=%s; scan kept", exc)
                self._remember(None)
                return
            log.info("action=scan_results n=%d secs=%.2f", len(hits), time.monotonic() - started)
        self._remember(hits if group else None)
        if 0 < count <= LOG_MATCHES:
            log.info("action=matches addrs=%s", " ".join(f"{h.address:#x}" for h in hits))
        shown = hits[: self.scan_log.limit]
        self.scan_log.write(
            self.vtype, count, shown, self.history, self.scale, types=list(self.types)
        )

    def _remember(self, hits: list[ScanHit] | None) -> None:
        """Add each match's value at this scan to ``values`` (AC-G1). None: too many
        matches (or they couldn't be fetched), so start again from the next scan.

        A scan where no value changed (a "Same" scan) adds nothing a group could use, so
        it isn't kept: ``recorded`` counts the scans that saw a change (plus the first),
        and memory doesn't grow with every "Same"."""
        self._groups = None
        if hits is None:
            self.values, self.tags, self.recorded = {}, {}, 0
            return
        n = self.recorded
        values: dict[tuple[int, ValueType], list[float | None]] = {}
        tags: dict[tuple[int, ValueType], Scale | None] = {}
        for h in hits:
            vt = h.vtype or self.vtype
            if vt is None or len(h.value) < vt.size:
                continue
            key = (h.address, vt)
            v = vt.decode(h.value)
            known = v if not (isinstance(v, float) and not math.isfinite(v)) else None
            history = self.values.get(key) or [None] * n
            history.append(known)
            values[key] = history
            tags[key] = h.way
        if n and values and all(len(v) > 1 and v[-1] == v[-2] for v in values.values()):
            for v in values.values():
                v.pop()  # nothing moved: keep the scans that did
            self.values, self.tags = values, tags
            return
        self.values, self.tags, self.recorded = values, tags, n + 1
        self.groups()  # work it out now, on the scan's thread, not in the live list

    def groups(self) -> list[Group]:
        """Linked matches as groups, best first (AC-G1..G4); [] unless 2 to GROUP_LIMIT
        matches are left and their values are known from at least GROUP_SCANS scans."""
        if not (self.group_size and self.recorded >= GROUP_SCANS):
            return []
        if self._groups is None:
            started = time.monotonic()
            self._groups = group_matches(self.values, self.tags)
            log.info(
                "action=groups matches=%d groups=%d secs=%.2f",
                len(self.values),
                len(self._groups),
                time.monotonic() - started,
            )
        return self._groups

    @property
    def group_size(self) -> bool:
        """Few enough matches left (2 to GROUP_LIMIT) to show them as groups."""
        return self.active and 2 <= (self.count or 0) <= GROUP_LIMIT

    @property
    def changes_to_group(self) -> int:
        """How many more scans where the value changes (Went up / Went down / the new
        number) grouping needs before it can tell linked copies apart; 0 when it can."""
        return max(0, GROUP_SCANS - self.recorded) if self.group_size else 0

    def matches(self, limit: int | None = None) -> list[Match]:
        """Current values of up to ``limit`` matches, read fresh (AC-3.4 live list)."""
        limit = self.show_limit if limit is None else limit
        if not self.active or (self.vtype is None and not self.types):
            return []
        hits = self.session.require_client().scan_results(limit)
        values = self._read_values(hits)
        out = []
        for h in hits:
            vt = h.vtype or self.vtype
            assert vt is not None
            raw = values[h.address][: vt.size]
            out.append(Match(h.address, vt.decode(raw), raw, h.vtype, h.way))
        return out

    def _read_values(
        self,
        hits: list[ScanHit],
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[int, bytes]:
        """The bytes at each hit's address now (as many as its widest type)."""
        if not hits:
            return {}
        client = self.session.require_client()
        game = self.session.require_game()
        size = max(t.size for t in self._types_of(hits))
        # One read per cluster of nearby matches, not one per match (~36 ms each on PS5),
        # and all of them in one go when the client can pipeline reads.
        values: dict[int, bytes] = {}
        addrs = sorted(h.address for h in hits)
        spans: list[tuple[int, int, list[int]]] = []
        i = 0
        while i < len(addrs):
            j = i
            while j + 1 < len(addrs) and addrs[j + 1] + size - addrs[i] <= READ_SPAN:
                j += 1
            spans.append((addrs[i], addrs[j] + size - addrs[i], addrs[i : j + 1]))
            i = j + 1
        read_many = getattr(client, "read_many", None)
        blobs: list[bytes] = []
        for k in range(0, len(spans), READ_CHUNK):
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("scan cancelled by the player")
            part = spans[k : k + READ_CHUNK]
            if read_many is not None and len(part) > 1:
                blobs += read_many(game.pid, [(a, n) for a, n, _ in part])
            else:
                blobs += [client.read(game.pid, a, n) for a, n, _ in part]
            if progress:
                progress(min(1.0, (k + len(part)) / len(spans)))
        for (start, _n, group), data in zip(spans, blobs, strict=True):
            for a in group:
                values[a] = data[a - start : a - start + size]
        return values

    def _types_of(self, hits) -> list[ValueType]:
        found = {h.vtype for h in hits if h.vtype is not None}
        if self.vtype is not None:
            found.add(self.vtype)
        return list(found) or list(self.types)
