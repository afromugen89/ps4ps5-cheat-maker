"""Test the hits (AC-H6): find which of a few matches really is the game's value.

Each round writes a test value once to half of the candidates and asks the player
whether the game showed it. Copies (UI, save, network) don't change what the game
shows; the real value does. The original bytes are put back after every answer.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field

from cheatmaker.console.client import ConsoleClient
from cheatmaker.core.values import ValueType

log = logging.getLogger(__name__)

TEST_LIMIT = 500
"""More candidates than this and one round's writes get slow (~36 ms each on PS5)."""
READ_SPAN = 0x1000  # candidates this close together are read in one request
TEST_STEP = 11


def suggest_value(vtype: ValueType, current: float | int) -> float | int:
    """A test value the player will notice: whole numbers +11, decimals halved."""
    if vtype.is_float:
        if not math.isfinite(current):
            return 50.0
        return float(current) * 0.5 if current else float(TEST_STEP)
    lo, hi = vtype.int_range()
    v = int(current) + TEST_STEP
    return v if v <= hi else max(lo, int(current) - TEST_STEP)


@dataclass
class HitTester:
    client: ConsoleClient
    pid: int
    vtype: ValueType
    candidates: list[int]
    rounds: int = 0
    stopped: bool = False
    _written: dict[int, bytes] = field(default_factory=dict)
    _cancel: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.candidates = sorted(set(self.candidates))
        if not 2 <= len(self.candidates) <= TEST_LIMIT:
            raise ValueError(f"Test the hits works with 2 to {TEST_LIMIT} matches.")

    @property
    def done(self) -> bool:
        return self.stopped or len(self.candidates) == 1

    @property
    def result(self) -> int | None:
        return self.candidates[0] if len(self.candidates) == 1 and not self.stopped else None

    @property
    def rounds_left(self) -> int:
        return 0 if self.done else math.ceil(math.log2(len(self.candidates)))

    @property
    def testing(self) -> list[int]:
        """Half A: the candidates the next (or current) round writes to."""
        return self.candidates[: (len(self.candidates) + 1) // 2]

    @property
    def waiting(self) -> bool:
        """A round's test value is in the game and the player hasn't answered yet."""
        return bool(self._written)

    def current_value(self) -> float | int:
        addr = self.candidates[0]
        return self.vtype.decode(self.client.read(self.pid, addr, self.vtype.size))

    def write_test(self, value: float | int) -> list[int]:
        """Write ``value`` once to half A, remembering the original bytes."""
        if self.done:
            raise ValueError("The test is over.")
        if self.waiting:
            raise ValueError("Answer the last question first.")
        data = self.vtype.encode(value)  # raises ValueError if it doesn't fit
        half = self.testing
        originals = self._read_all(half)
        try:
            for addr in half:
                if self._cancel.is_set():
                    break
                # Noted before the write: a write that lands but times out is put back too.
                self._written[addr] = originals[addr]
                self.client.write(self.pid, addr, data)
        except Exception:
            self.restore()
            raise
        if self._cancel.is_set():  # Stop pressed mid-round
            self.stop()
            return []
        self.rounds += 1
        log.info(
            "action=test_hits round=%d wrote=%d of=%d value=%s",
            self.rounds,
            len(half),
            len(self.candidates),
            value,
        )
        return half

    def answer(self, changed: bool | None) -> None:
        """True: the game showed the test value (keep half A). False: it didn't (keep B).
        None: not sure (stop, nothing found). The original values go back first."""
        if not self.waiting:
            raise ValueError("Write a test value first.")
        half = self.testing
        self.restore()
        if changed is None:
            self.stopped = True
        elif changed:
            self.candidates = half
        else:
            keep = set(half)
            self.candidates = [a for a in self.candidates if a not in keep]
        log.info("action=test_hits_answer changed=%s left=%d", changed, len(self.candidates))

    def request_stop(self) -> None:
        """From another thread: make a running ``write_test`` stop and put values back."""
        self._cancel.set()

    def stop(self) -> None:
        """Put back what was written and end with no result."""
        self.stopped = True
        self.restore()

    def restore(self) -> None:
        """Write the original bytes back. Keeps trying the rest if one write fails, then
        raises the first error; the ones that failed stay noted, so it can be retried."""
        first: Exception | None = None
        for addr, data in list(self._written.items()):
            try:
                self.client.write(self.pid, addr, data)
                del self._written[addr]
            except Exception as exc:  # a lost link: try the rest, report the first
                log.warning("action=test_hits_restore_failed addr=%#x", addr)
                first = first or exc
        if first is not None:
            raise first

    def _read_all(self, addrs: list[int]) -> dict[int, bytes]:
        """One read per cluster of nearby addresses (``addrs`` is sorted)."""
        size = self.vtype.size
        out: dict[int, bytes] = {}
        i = 0
        while i < len(addrs):
            j = i
            while j + 1 < len(addrs) and addrs[j + 1] + size - addrs[i] <= READ_SPAN:
                j += 1
            start = addrs[i]
            data = self.client.read(self.pid, start, addrs[j] + size - start)
            for a in addrs[i : j + 1]:
                out[a] = data[a - start : a - start + size]
            i = j + 1
        return out
