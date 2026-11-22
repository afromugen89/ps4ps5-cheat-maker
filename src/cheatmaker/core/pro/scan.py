"""The Pro manual scanner (spec PR-2): every number type and compare, a choice of
memory regions, results paged ``PAGE_SIZE`` at a time, and AOB search over the game's
code. It drives the console's search directly, with no guided guessing: the value is
searched as typed.

The console has one search at a time. A Pro first scan takes it from the guided
Scanner (``Session.take_search``); when anything else starts a search afterwards
(``Session.search_owner`` changes) the Pro search is over and says so.
"""

from __future__ import annotations

import logging
import threading

from cheatmaker.console.client import MemMap, ProgressFn, ScanHit
from cheatmaker.core import updater as up
from cheatmaker.core.scanner import default_unit
from cheatmaker.core.session import Session
from cheatmaker.core.values import Compare, ValueType
from cheatmaker.errors import CheatMakerError

log = logging.getLogger(__name__)

PAGE_SIZE = 1000
GAME_DATA, ALL_WRITABLE, ALL_READABLE, GAME_MODULE = (
    "Game data",
    "All writable memory",
    "All readable memory",
    "Game module",
)
REGIONS = (GAME_DATA, ALL_WRITABLE, ALL_READABLE, GAME_MODULE)
AOB_LIMIT = 1000
REPLACED = "Another search replaced this one on the PS5. Start a new search."


class SearchReplaced(CheatMakerError):
    """The console's search now belongs to someone else."""


def regions(session: Session, which: str) -> list[MemMap]:
    """The game's memory maps for a region choice. Game data: readable, writable and
    not code (where values live, as the guided search); all writable; all readable;
    the game module: every readable map inside the game's own module."""
    game = session.require_game()
    maps = [m for m in session.require_client().maps(game.pid) if m.size > 0 and m.readable]
    if which == GAME_DATA:
        return [m for m in maps if m.writable and not m.executable]
    if which == ALL_WRITABLE:
        return [m for m in maps if m.writable]
    if which == ALL_READABLE:
        return maps
    if which == GAME_MODULE:
        return [m for m in maps if game.module_base <= m.start < game.module_end]
    raise ValueError(f"unknown region {which!r}")


def scan_value(cmp: Compare, vtype: ValueType, value, second=None):
    """The value the console compares with: a pair for "between" (low, high) and for
    "increased/decreased by" (amount, tolerance: whole numbers exact, decimals within
    one unit of the last digit typed)."""
    if cmp is Compare.BETWEEN:
        lo, hi = value, second
        if lo is None or hi is None:
            raise ValueError("Type both ends of the range.")
        if lo > hi:
            raise ValueError("The first number of the range must be the smaller one.")
        return (lo, hi)
    if cmp.by_amount:
        if value is None or value <= 0:
            raise ValueError("Type how much it changed by, as a number more than 0.")
        return (value, default_unit(value) if vtype.is_float else 1)
    if cmp.needs_value and value is None:
        raise ValueError("Type a number.")
    return value if cmp.needs_value else None


def time_left(elapsed: float, fraction: float) -> float | None:
    """Seconds left, from the time so far and the part done (None until it can tell)."""
    if fraction <= 0.01 or elapsed <= 0:
        return None
    return max(0.0, elapsed * (1 - fraction) / fraction)


def time_left_text(seconds: float | None) -> str:
    if seconds is None:
        return "working out the time left..."
    s = round(seconds)
    return f"about {s // 60} min {s % 60:02d} s left" if s >= 60 else f"about {s} s left"


class ProScan:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.vtype: ValueType | None = None
        self.count: int | None = None
        self.history: list[str] = []

    @property
    def active(self) -> bool:
        return self.count is not None and self.session.search_owner is self

    def _check(self) -> None:
        if self.count is None:
            raise ValueError("Start a new search first.")
        if self.session.search_owner is not self:
            self.count = None
            raise SearchReplaced(REPLACED)

    def claim(self) -> None:
        """Take the console's search (UI thread, before the first scan's worker starts):
        the guided Scanner hears it at once and the owner is set before any console
        call, so a guided scan can't slip in between."""
        self.count = None
        self.session.take_search(owner=self, by="pro")

    def first(
        self,
        region: str,
        vtype: ValueType,
        cmp: Compare,
        value=None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """The first scan (worker thread), after ``claim``."""
        if cmp.needs_previous:
            raise ValueError("A first search compares with a number, or starts unknown.")
        game = self.session.require_game()
        maps = regions(self.session, region)
        with self.session.search_lock:
            if self.session.search_owner is not self:
                raise SearchReplaced(REPLACED)
            count = self.session.require_client().scan_start(
                game.pid, maps, vtype, cmp, value, progress, cancel
            )
        self.vtype, self.count = vtype, count
        self.history = [f"{cmp.value} {value}" if value is not None else cmp.value]
        log.info(
            "action=pro_scan_first region=%s type=%s cmp=%s count=%d",
            region,
            vtype.name,
            cmp.value,
            count,
        )
        return count

    def next(
        self,
        cmp: Compare,
        value=None,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        if cmp is Compare.UNKNOWN:
            raise ValueError("Unknown is for a first search only.")
        with self.session.search_lock:
            self._check()
            count = self.session.require_client().scan_next(cmp, value, progress, cancel)
        self.count = count
        self.history.append(f"{cmp.value} {value}" if value is not None else cmp.value)
        log.info("action=pro_scan_next cmp=%s count=%d", cmp.value, count)
        return count

    def page(self, n: int) -> list[ScanHit]:
        """Results ``n * PAGE_SIZE`` onwards, at most ``PAGE_SIZE`` of them."""
        with self.session.search_lock:
            self._check()
            return self.session.require_client().scan_results(PAGE_SIZE, n * PAGE_SIZE)

    def pages(self) -> int:
        return 0 if not self.count else (self.count + PAGE_SIZE - 1) // PAGE_SIZE

    def reset(self) -> None:
        self.count, self.vtype, self.history = None, None, []


def find_aob(
    session: Session,
    text: str,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    limit: int = AOB_LIMIT,
) -> list[int]:
    """Addresses in the game's code where the pattern (hex with ``??``) matches."""
    try:
        sig = up.Signature.from_text(text)
    except ValueError:
        raise ValueError("Type the bytes as hex pairs, with ?? for any byte.") from None
    image = up.read_code_image(session, progress, cancel)
    base = session.require_game().module_base
    found = [base + off for off in image.find(sig, limit)]
    log.info("action=pro_aob len=%d found=%d", len(sig), len(found))
    return found


__all__ = [
    "ALL_READABLE",
    "ALL_WRITABLE",
    "AOB_LIMIT",
    "GAME_DATA",
    "GAME_MODULE",
    "PAGE_SIZE",
    "REGIONS",
    "REPLACED",
    "ProScan",
    "SearchReplaced",
    "find_aob",
    "regions",
    "scan_value",
    "time_left",
    "time_left_text",
]
