"""Find-it guide (AC-H5): one suggested next step after every search.

Pure logic, no Qt. The Find view shows ``Suggestion.text`` and highlights the button
for ``Suggestion.action``; the player can ignore it and press anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from cheatmaker.core.hit_tester import TEST_LIMIT
from cheatmaker.core.real_check import Verdict


class Shows(Enum):
    NUMBER = "a number"
    BAR = "a bar or meter"
    NOTHING = "nothing"


class Action(Enum):
    SEARCH = "search"  # type the number, Search
    UNKNOWN = "unknown"  # I don't know the number
    TYPE_NEW = "type new"  # change it in the game, type the new number
    MOVE = "move"  # make it go up or down, Went up / Went down
    STAND_STILL = "stand still"
    TEST_HITS = "test hits"
    MAKE_CHEAT = "make cheat"
    TRY_NEXT = "try next"  # run ``Suggestion.fallback``
    START_OVER = "start over"
    FULL = "full"  # fill it up, It's full now
    PERCENT = "percent"  # type the % the game shows, or About 75 / 50 / 25%
    TRACE = "trace"  # Find the real one behind it (spec-trace)


class Scan(Enum):
    """What the scan that just finished was."""

    FIRST_EXACT = "first exact"
    FIRST_UNKNOWN = "first unknown"
    NEXT_EXACT = "next exact"
    COMPARE = "compare"  # Went up / down / Changed / Same, with or without "by"
    STAND_STILL = "stand still"
    FULL = "full"  # It's full now (first scan, or full again)
    PERCENT = "percent"  # a typed N% or About X%


FALLBACKS: list[tuple[str, str]] = [  # (key, what the hint calls it), in the order tried
    ("shown", "the number shown"),
    ("fraction", "a fraction of 100"),
    ("x10", "the number x10"),
    ("x100", "the number x100"),
    ("unknown", "an unknown value (Went up / Went down)"),
]
_LABEL = dict(FALLBACKS)


FILL_UP = "Fill it up, then press It's full now."
GO_DOWN = (
    "Make it go down, then type the % if the game shows it anywhere (pause or stats "
    "screen), or pick About 75 / 50 / 25%."
)


TRACE_HINT = "These look like copies the screen uses. Press Find the real one behind it."
PUT_BACK_S = 2.0  # a Set/Freeze the game undid within this long counts as put back


@dataclass(frozen=True)
class Suggestion:
    text: str
    action: Action
    fallback: str | None = None  # a FALLBACKS key, for TRY_NEXT and START_OVER


class Guide:
    def __init__(self, show_limit: int = 10) -> None:
        self.shows: Shows | None = None
        self.tried: list[str] = []
        self.compares_in_row = 0
        self.show_limit = show_limit  # the Find view's "Show up to"
        self.last_count: int | None = None
        self.full_mode = False  # the player pressed It's full now in this search

    def start(self, shows: Shows) -> Suggestion:
        self.shows = shows
        self.new_value()
        if shows is Shows.NUMBER:
            return Suggestion("Type the number you see, then press Search.", Action.SEARCH)
        if shows is Shows.BAR:  # AC-P4
            return Suggestion(FILL_UP, Action.FULL)
        return Suggestion(
            "Press I don't know the number. Then make it go up or down in the game.",
            Action.UNKNOWN,
        )

    def new_value(self) -> None:
        """Start over: forget which ways of storing it were tried."""
        self.tried = []
        self.compares_in_row = 0
        self.last_count = None
        self.full_mode = False

    def note_tried(self, key: str) -> None:
        if key in _LABEL and key not in self.tried:
            self.tried.append(key)

    def next_fallback(self) -> str | None:
        return next((k for k, _ in FALLBACKS if k not in self.tried), None)

    def after_scan(self, scan: Scan, count: int) -> Suggestion:
        if scan is Scan.FULL:
            self.full_mode = True
        if scan is Scan.PERCENT:  # a percent search looks at these ways already
            for key in ("shown", "fraction", "x10"):
                self.note_tried(key)
        if scan in (Scan.COMPARE, Scan.STAND_STILL):
            self.compares_in_row += 1
        else:
            self.compares_in_row = 0
        first = scan in (Scan.FIRST_EXACT, Scan.FIRST_UNKNOWN, Scan.FULL)
        stuck = not first and self.last_count == count  # narrowing dropped nothing
        self.last_count = count
        if count == 0:
            return self._nothing(scan)
        if count == 1:
            return Suggestion(
                "Found it! Check the value in the list, then press Make a cheat.",
                Action.MAKE_CHEAT,
            )
        # Writing to memory is the last resort: only when the list is short, or when
        # narrowing down has stopped dropping anything (the rest change together).
        if count <= TEST_LIMIT and (count <= self.show_limit or stuck):
            questions = math.ceil(math.log2(count))
            return Suggestion(
                f"{count:,} left. Press Test the hits to find the real one in about "
                f"{questions} questions.",
                Action.TEST_HITS,
            )
        return self._narrow()

    def after_check(self, verdicts: list[Verdict]) -> Suggestion | None:
        """Check which is real marked every row it could check a copy (rows that
        couldn't be checked, e.g. unloaded, don't count): the real value isn't in the
        list, so trace it from a copy (spec-trace AC-T1)."""
        if Verdict.COPY in verdicts and all(v in (Verdict.COPY, Verdict.FAILED) for v in verdicts):
            return Suggestion(TRACE_HINT, Action.TRACE)
        return None

    def after_put_back(self) -> Suggestion:
        """A Set/Freeze on the selected row was put back by the game (AC-T1)."""
        return Suggestion(TRACE_HINT, Action.TRACE)

    def _nothing(self, scan: Scan) -> Suggestion:
        nxt = self.next_fallback()
        if nxt is None:
            return Suggestion(
                "Nothing found any way we know. The game may hide this value; try Lock it on "
                "something close to it, or a different value.",
                Action.START_OVER,
            )
        if scan is Scan.FIRST_EXACT:
            return Suggestion(
                f"Nothing found. The game may store it another way. Next: try it as {_LABEL[nxt]}.",
                Action.TRY_NEXT,
                nxt,
            )
        return Suggestion(
            f"The value may be stored another way. Start over and try {_LABEL[nxt]}.",
            Action.START_OVER,
            nxt,
        )

    def _narrow(self) -> Suggestion:
        if self.full_mode and self.compares_in_row < 2:
            return Suggestion(GO_DOWN, Action.PERCENT)
        if self.compares_in_row >= 2 and self.shows is not Shows.NUMBER:
            return Suggestion(
                "Still too many. Press Stand still and leave the controller alone to drop "
                "values that move on their own.",
                Action.STAND_STILL,
            )
        if self.shows is Shows.NUMBER:
            return Suggestion(
                "Too many to list. Change it in the game, then type the new number (or use "
                "Went up by / Went down by if you know how much it changed).",
                Action.TYPE_NEW,
            )
        return Suggestion(
            "Too many to list. Make it go up or down in the game, then press Went up or Went down.",
            Action.MOVE,
        )
