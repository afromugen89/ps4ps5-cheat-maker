"""Find the real one behind it (docs/spec-trace.md): from a screen copy, follow the
game's code back to the value it copies, check it, and repeat up to 4 hops.

One hop (docs/architecture-trace.md "The driver"):

1. watch the copy for writes (aligned span, ``watch_span``), while any freeze on it
   is paused (``release``);
2. read the code before up to 3 writers and follow the value back
   (``trace_flow``): a source address and the **way** (type, factor);
3. memory check (AC-T5): the source through the way must give the copy's value, at
   two different shown values (the player changes the number once);
4. write test (AC-T6) with Check which is real's ``RealChecker``, one round:
   [source, the copy, every earlier copy];
5. real / kept: found. Copy: the source is the next copy to watch.

Registers in a trap are UNVERIFIED on the console (C10a), so nothing is written
before the memory check passed: wrong registers give "no row", never a wrong row.
Pure logic over a ConsoleClient; the dialog runs ``Tracer.run`` on a worker thread.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum

from cheatmaker.console.client import MemMap, WatchHit
from cheatmaker.core import real_check as rc
from cheatmaker.core import trace_flow as tf
from cheatmaker.core.locker import CONTEXT, Writer
from cheatmaker.core.session import Session
from cheatmaker.core.trace_way import Kind, Way
from cheatmaker.core.values import ValueType, unit_of
from cheatmaker.errors import ConnectionLost, ReadFailed

log = logging.getLogger(__name__)

WATCH_S = 30.0
EARLY_HITS = 3  # the watch ends EARLY_MORE_S after this many hits
EARLY_MORE_S = 2.0
MAX_WRITERS = 3
MAX_HOPS = 4
MAX_CALLERS = 4  # return addresses tried for one writer
EXEC_HITS = 16  # execute breakpoint: at most this many hits...
EXEC_S = 10.0  # ...or this long (AC-T4 rule 4)
STEADY_S = 1.0  # the copy holds one value this long before it is checked
CHANGE_WAIT_S = 30.0
POLL_S = 0.25
TOTAL_S = 240.0  # whole trace, including the player's changes
HOP_WORK_S = 15.0  # one hop without waiting for the player (logged when over)
CODE_READ = tf.WINDOW + tf.MAX_INSN  # bytes read before a writer's RIP


class Stage(Enum):
    """Where a hop is, for the dialog's step label and hint (words in ui/hints.py)."""

    WATCH = "watch"  # watching the copy: the player keeps playing
    READ = "read"  # reading the game's code
    STEADY = "steady"  # checking: waiting for the number to hold still
    CHANGE = "change"  # checking: the player changes the number once
    TEST = "test"  # the write test


_STAGE_AT = {Stage.WATCH: 0, Stage.READ: 1, Stage.STEADY: 2, Stage.CHANGE: 2, Stage.TEST: 3}
_METHOD_ORDER = {"regs": 0, "replay": 1, "block": 2, "caller": 3, "exec": 4}

Progress = Callable[[float, Stage, int], None]
"""``progress(fraction, stage, hop number)``."""
Release = Callable[[int, int], Callable[[], None]]
"""``release(address, size)`` pauses the app's freezes on that range; returns resume."""


def watch_span(addr: int, vtype: ValueType) -> tuple[int, int] | None:
    """(address, size) to watch for a value (AC-T2): DR7 needs an aligned range. An
    8-byte value not 8-aligned is watched as its 4 aligned low bytes; a 4-byte value
    not 4-aligned as its aligned 2 bytes; otherwise it can't be watched (None)."""
    size = vtype.size
    if size == 8:
        if addr % 8 == 0:
            return addr, 8
        size = 4
    if size == 4:
        if addr % 4 == 0:
            return addr, 4
        return (addr, 2) if addr % 2 == 0 else None
    if size == 2:
        return (addr, 2) if addr % 2 == 0 else None
    return addr, 1


@dataclass
class WriterPath:
    writer: Writer
    path: tf.Path | None
    skipped: str | None = None  # "sets it to a fixed number", "scrambled", ...
    window: tf.Window | None = field(default=None, repr=False)


@dataclass
class Hop:
    n: int  # 1..MAX_HOPS
    watched: rc.Target
    span: tuple[int, int] = (0, 0)
    hits: int = 0
    writers: list[WriterPath] = field(default_factory=list)
    tried: list[tuple[tf.Candidate, str]] = field(default_factory=list)  # why dropped
    source: tf.Candidate | None = None
    way: Way | None = None  # this hop: source -> watched
    verdict: rc.Verdict | None = None
    stop: str = ""


class Outcome(Enum):
    FOUND = "found"
    ALREADY_REAL = "already real"
    NO_WRITER = "no writer"
    CANT_WATCH = "can't watch"
    SEARCH_THAT_WAY = "search that way"
    NOT_FOLLOWED = "not followed"
    SCRAMBLED = "scrambled"
    TOO_FAR = "too far"
    UNCONFIRMED = "number didn't change"
    CANCELLED = "cancelled"
    LOST = "lost"


@dataclass(frozen=True)
class TraceResult:
    outcome: Outcome
    behind: int  # the start row's address
    address: int | None = None  # found (or "not sure") source
    vtype: ValueType | None = None
    way: Way | None = None  # total: real -> the start row's screen value
    verdict: rc.Verdict | None = None  # REAL / KEPT; None = not sure
    hops: tuple[Hop, ...] = ()
    not_restored: tuple[int, ...] = ()
    value: float | int | None = None  # FOUND: the found value now
    max_now: dict[int, float | int] | None = None  # a share-of-max way's max, read now
    reason: str = ""  # why it ended, for Advanced ("goes round in a circle")

    @property
    def sure(self) -> bool:
        return self.verdict in (rc.Verdict.REAL, rc.Verdict.KEPT)


@dataclass(frozen=True)
class _Option:
    """A candidate source with the way that goes with it."""

    cand: tf.Candidate
    way: Way


class _Stop(Exception):
    """Ends the trace with a result (raised from inside a hop)."""

    def __init__(self, result: TraceResult) -> None:
        super().__init__(result.outcome.value)
        self.result = result


class _Cancelled(Exception):
    pass


class Tracer:
    def __init__(
        self,
        session: Session,
        address: int,
        vtype: ValueType,
        *,
        release: Release | None = None,
        exec_breakpoint: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        settle_s: float | None = None,
    ) -> None:
        if vtype.size not in (1, 2, 4, 8) or not address:
            raise ValueError("Pick one row in the list.")
        self.session = session
        self.client = session.require_client()
        self.game = session.require_game()
        self.pid = self.game.pid
        self.address, self.vtype = address, vtype
        self.release = release
        self.exec_breakpoint = exec_breakpoint  # setting trace_exec_breakpoint (C20d)
        self.sleep, self.clock = sleep, clock
        self.settle_s = settle_s
        self.hops: list[Hop] = []
        self.not_restored: list[int] = []
        self._resumes: list[Callable[[], None]] = []
        self._const_maps: list[MemMap] | None = None
        self._progress: Progress | None = None
        self._cancel: threading.Event | None = None
        self._start = 0.0
        self._waited = 0.0  # this hop's time spent waiting for the player

    # -- the whole trace -------------------------------------------------------------
    def run(
        self, progress: Progress | None = None, cancel: threading.Event | None = None
    ) -> TraceResult:
        self._progress, self._cancel = progress, cancel
        self._start = self.clock()
        log.info(
            "action=trace_start addr=%#x type=%s hops_max=%d exec_bp=%s",
            self.address,
            self.vtype.name,
            MAX_HOPS,
            self.exec_breakpoint,
        )
        try:
            result = self._hops()
        except _Stop as stop:
            result = stop.result
        except _Cancelled:
            result = self._end(Outcome.CANCELLED)
        except ConnectionLost:
            log.warning("action=trace_lost hops=%d", len(self.hops))
            result = self._end(Outcome.LOST, "lost connection")
        finally:
            self._resume_all()
        if self.not_restored:
            result = replace(result, not_restored=tuple(self.not_restored))
        log.info(
            "action=trace_result outcome=%s addr=%s way=%r hops=%d secs=%.1f",
            result.outcome.value,
            f"{result.address:#x}" if result.address is not None else "-",
            result.way.text() if result.way else "-",
            len(result.hops),
            self.clock() - self._start,
        )
        log.info(
            "action=trace_restore not_restored=%s",
            ",".join(f"{a:#x}" for a in result.not_restored) or "-",
        )
        return result

    def _end(self, outcome: Outcome, reason: str = "", **kw) -> TraceResult:
        return TraceResult(outcome, self.address, hops=tuple(self.hops), reason=reason, **kw)

    def _hops(self) -> TraceResult:
        copy = rc.Target(self.address, self.vtype)
        total: Way | None = None  # the current copy -> the start row's screen
        earlier: list[rc.Target] = []
        seen = {self.address}
        last: tuple[int, Way] | None = None  # the last source checked (address, total way)
        for n in range(1, MAX_HOPS + 1):
            if self.clock() - self._start > TOTAL_S:
                return self._too_far(last, "the trace took too long")
            hop = Hop(n, copy)
            self.hops.append(hop)
            work_start, self._waited = self.clock(), 0.0
            option = self._hop(hop, total)
            if option is None:  # the watched copy is the real one (read-modify-write)
                if n == 1:
                    return self._end(Outcome.ALREADY_REAL, "read-modify-write")
                return self._found(copy.address, total or Way(copy.vtype, copy.vtype), None)
            way = option.way if total is None else option.way.then(total)
            src = option.cand.address
            if src in seen:
                hop.stop = "goes round in a circle"
                return self._too_far(last, hop.stop)
            verdict = self._test(hop, option, copy, earlier)
            last = (src, way)
            log.info("action=trace_hop n=%d stop=%s", n, hop.stop or "-")
            work = self.clock() - work_start - self._waited
            if work > HOP_WORK_S:
                log.info("action=trace_hop n=%d slow work_secs=%.1f", n, work)
            if verdict in (rc.Verdict.REAL, rc.Verdict.KEPT):
                return self._found(src, way, verdict)
            if verdict is not rc.Verdict.COPY:
                hop.stop = "couldn't be checked"
                return self._end(Outcome.NOT_FOLLOWED, "couldn't be checked")
            hop.stop = "a copy: following it"
            seen.add(src)
            earlier.insert(0, copy)
            copy = rc.Target(src, option.way.real)
            total = way
        return self._too_far(last, f"{MAX_HOPS} hops")

    def _too_far(self, last: tuple[int, Way] | None, why: str) -> TraceResult:
        log.info("action=trace_stop reason=%r", why)
        if last is None:
            return self._end(Outcome.TOO_FAR, why)
        addr, way = last
        return self._end(
            Outcome.TOO_FAR,
            why,
            address=addr,
            vtype=way.real,
            way=way,
        )

    def _found(self, addr: int, way: Way, verdict: rc.Verdict | None) -> TraceResult:
        now, max_now = None, None
        try:
            now = way.real.decode(self.client.read(self.pid, addr, way.real.size))
            max_now = self._max_now(way)
        except ReadFailed as exc:
            log.info("action=trace_found_read addr=%#x reason=%s", addr, exc)
        return self._end(
            Outcome.FOUND,
            address=addr,
            vtype=way.real,
            way=way,
            verdict=verdict,
            value=now,
            max_now=max_now,
        )

    # -- one hop -------------------------------------------------------------------------
    def _say(self, hop: Hop, stage: Stage) -> None:
        if self._progress is not None:
            at = _STAGE_AT[stage]
            self._progress(min(1.0, ((hop.n - 1) * 4 + at) / (MAX_HOPS * 4)), stage, hop.n)

    def _check_cancel(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise _Cancelled

    def _hop(self, hop: Hop, total: Way | None) -> _Option | None:
        span = watch_span(hop.watched.address, hop.watched.vtype)
        if span is None:
            hop.stop = "can't be watched"
            raise _Stop(self._end(Outcome.CANT_WATCH, hop.stop))
        hop.span = span
        self._say(hop, Stage.WATCH)
        if self.release is not None:
            self._resumes.append(self.release(*span))
        hits = self._watch(hop)
        self._check_cancel()
        if not hits:
            hop.stop = "nothing wrote to it"
            raise _Stop(self._end(Outcome.NO_WRITER, hop.stop))
        self._say(hop, Stage.READ)
        by_writer = self._writers(hop, hits)
        if any(wp.path and wp.path.end is tf.End.ALREADY_REAL for wp, _h in by_writer):
            hop.stop = "read-modify-write: it's the real one"
            return None
        options = self._options(hop, by_writer)
        if any(o.cand.address == hop.watched.address for o in options):
            hop.stop = "read back from the same place: it's the real one"
            return None
        self._check_cancel()
        self._say(hop, Stage.STEADY)
        winner = self._check(hop, options) if options else None
        if winner is None and self.exec_breakpoint:
            winner = self.exec_fallback(hop)
        if winner is None:
            raise _Stop(self._no_address(hop, options, total))
        hop.source, hop.way = winner.cand, winner.way
        return winner

    def _watch(self, hop: Hop) -> list[WatchHit]:
        enough_at: list[float] = []

        def until(hits: list[WatchHit], elapsed: float) -> bool:
            if self._cancel is not None and self._cancel.is_set():
                return True
            if len(hits) >= EARLY_HITS and not enough_at:
                enough_at.append(elapsed)
            return bool(enough_at) and elapsed >= enough_at[0] + EARLY_MORE_S

        addr, size = hop.span
        started = self.clock()
        hits = self.client.watch(self.pid, addr, size, "write", WATCH_S, until=until)
        hop.hits = len(hits)
        self._waited += self.clock() - started
        log.info("action=trace_watch hop=%d addr=%#x size=%d hits=%d", hop.n, addr, size, len(hits))
        if hits and hits[0].raw:
            log.debug("action=trace_watch hop=%d raw=%s", hop.n, hits[0].raw.hex())
        return hits

    # -- writers and their paths -----------------------------------------------------------
    def _read(self, addr: int, n: int) -> bytes:
        return self.client.read(self.pid, addr, n)

    def _read_back(self, rip: int) -> bytes:
        """Up to CODE_READ bytes ending at ``rip`` (fewer near a map's start)."""
        for n in (CODE_READ, CONTEXT, tf.MAX_INSN):
            try:
                return self._read(rip - n, n)
            except ReadFailed:
                continue
        return b""

    def is_const(self, addr: int) -> bool:
        """Inside a mapped region that isn't writable (read-only data, code)."""
        if self._const_maps is None:
            self._const_maps = [m for m in self.client.maps(self.pid) if not m.writable]
        return any(m.contains(addr) for m in self._const_maps)

    def _writers(self, hop: Hop, hits: list[WatchHit]) -> list[tuple[WriterPath, list[WatchHit]]]:
        """Up to MAX_WRITERS distinct writers, most hits first, each with its path."""
        groups: dict[int, list[WatchHit]] = {}
        for h in hits:
            groups.setdefault(h.rip, []).append(h)
        out = []
        for rip, group in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:MAX_WRITERS]:
            wp = self._writer_path(hop, rip, group)
            if wp is not None:
                hop.writers.append(wp)
                out.append((wp, group))
        return out

    def _writer_path(self, hop: Hop, rip: int, hits: list[WatchHit]) -> WriterPath | None:
        before = self._read_back(rip)
        try:
            at = self._read(rip, tf.MAX_INSN)
        except ReadFailed:
            at = b""
        found = tf.writer_at(before[-CONTEXT:], at, rip) if before or at else None
        if found is None:
            log.warning("action=trace_writer rip=%#x no writing instruction found", rip)
            return None
        start, code, text = found
        if start == rip:
            log.info(
                "action=trace_writer rip=%#x rep_at_rip=True (UNVERIFIED: a mid-copy trap "
                "leaves RIP at the rep instruction)",
                rip,
            )
            window = before + code
        else:
            window = before
        win = tf.decode_window(window[-(tf.WINDOW + len(code)) :], start, len(code))
        writer = Writer(start, code, text, len(hits))
        path = self._follow(hop, win, hits[0])
        skipped = None
        if path.end is tf.End.CONSTANT:
            skipped = "sets it to a fixed number"
        elif path.end is tf.End.SCRAMBLED:
            skipped = "scrambled"
        log.info(
            "action=trace_writer writer=%#x text=%r hits=%d skipped=%s",
            start,
            text,
            len(hits),
            skipped or "-",
        )
        log.info(
            "action=trace_path end=%s followed=%r way=%r reason=%r",
            path.end.value,
            "|".join(path.followed),
            path.way.text() if path.way else "-",
            path.reason,
        )
        return WriterPath(writer, path, skipped, win)

    def _follow(self, hop: Hop, win: tf.Window, hit: WatchHit) -> tf.Path:
        if win.writer.has_rep_prefix or win.writer.has_repe_prefix:
            return tf.follow(win, 0, self._read, self.is_const, hop.watched.vtype)
        store = tf.store_address(win.writer, hit)
        if store is None:
            return tf.Path(tf.End.NOT_FOLLOWED, None, reason="can't tell where it stored")
        lane = hop.watched.address - store
        if lane < 0 or lane >= 64:
            return tf.Path(tf.End.NOT_FOLLOWED, None, reason=f"it stored at {store:#x}")
        return tf.follow(win, lane, self._read, self.is_const, hop.watched.vtype)

    # -- candidates ------------------------------------------------------------------------------
    def _options(
        self, hop: Hop, by_writer: list[tuple[WriterPath, list[WatchHit]]]
    ) -> list[_Option]:
        """Every candidate source of every writer, one per address, in method order."""
        found: dict[int, _Option] = {}
        for wp, hits in by_writer:
            path, win = wp.path, wp.window
            if path is None or path.way is None or win is None:
                continue
            if path.end in (tf.End.SOURCE, tf.End.BLOCK_COPY):
                for h in hits:
                    for c in tf.candidates(win, path, h, hop.watched.address, self._read):
                        found.setdefault(c.address, _Option(c, _with_max(path.way, c)))
            elif path.end is tf.End.BLOCK_START and path.reg in tf.ARGUMENT_REGS:
                for opt in self._from_callers(hop, win, path, hits[0]):
                    found.setdefault(opt.cand.address, opt)
        options = sorted(found.values(), key=lambda o: _METHOD_ORDER[o.cand.method])
        for o in options:
            log.info(
                "action=trace_source method=%s addr=%#x expr=%r max=%s",
                o.cand.method,
                o.cand.address,
                o.cand.expr,
                f"{o.cand.max_address:#x}" if o.cand.max_address is not None else "-",
            )
        return options

    def _from_callers(
        self, hop: Hop, win: tf.Window, path: tf.Path, hit: WatchHit
    ) -> list[_Option]:
        rsp = hit.regs.get("rsp")
        if rsp is None or path.way is None or path.reg is None:
            return []
        stack = b""
        for n in (tf.STACK_SEARCH, 256, 64):
            with contextlib.suppress(ReadFailed):
                stack = self._read(rsp, n)
                break
        code_range = (self.game.module_base, self.game.module_end)
        out = []
        for ret in tf.call_sites(stack, rsp, code_range, self._read)[:MAX_CALLERS]:
            cpath, cands = tf.follow_into_caller(
                self._read,
                ret,
                path.reg,
                path.lane,
                hit,
                self.is_const,
                path.way.real,
                win,
                hop.watched.address,
            )
            log.info(
                "action=trace_path caller=%#x end=%s followed=%r reason=%r",
                ret,
                cpath.end.value,
                "|".join(cpath.followed),
                cpath.reason,
            )
            if cpath.way is None:
                continue
            way = cpath.way.then(path.way)
            out += [_Option(c, _with_max(way, c)) for c in cands]
        return out

    # -- execute breakpoint fallback (AC-T4 rule 4, setting trace_exec_breakpoint) ----------
    def exec_fallback(self, hop: Hop) -> _Option | None:
        """A 1-byte execute breakpoint on each path's load: the registers come from
        before the load, so its address is exact. Only addresses that pass the memory
        check count. Whether NG sets RF on continue is UNVERIFIED (C20d): the same
        breakpoint firing EXEC_HITS times with the very same registers means it
        repeats instead of the game running on, which is logged and ends the fallback."""
        options: dict[int, _Option] = {}
        for wp in hop.writers:
            path = wp.path
            if path is None or path.end is not tf.End.SOURCE or path.load is None:
                continue
            if path.way is None:
                continue
            hits = self._exec_watch(path.load.ip)
            if _repeats(hits):
                log.warning(
                    "action=trace_source method=exec ip=%#x the breakpoint repeats "
                    "(%d hits, same registers): stopped",
                    path.load.ip,
                    len(hits),
                )
                hop.stop = "the breakpoint repeats"
                continue
            for h in hits:
                addr = tf.exec_address(path.load, h)
                if addr is None or addr in options:
                    continue
                max_address = None
                if path.max_load is not None:
                    max_address = tf.exec_address(path.max_load, h)
                    if max_address is None:
                        continue
                expr = path.load.mem.text() + (f"+{path.load.lane:X}h" if path.load.lane else "")
                cand = tf.Candidate(addr, "exec", expr, max_address, wp.writer.address)
                options[addr] = _Option(cand, _with_max(path.way, cand))
                log.info(
                    "action=trace_source method=exec addr=%#x expr=%r load=%#x",
                    addr,
                    expr,
                    path.load.ip,
                )
        if not options:
            return None
        self._check_cancel()
        return self._check(hop, list(options.values()))

    def _exec_watch(self, ip: int) -> list[WatchHit]:
        def until(hits: list[WatchHit], _elapsed: float) -> bool:
            stop = self._cancel is not None and self._cancel.is_set()
            return stop or len(hits) >= EXEC_HITS

        started = self.clock()
        hits = self.client.watch(self.pid, ip, 1, "execute", EXEC_S, until=until)
        self._waited += self.clock() - started
        log.info("action=trace_watch kind=execute ip=%#x hits=%d", ip, len(hits))
        self._check_cancel()
        return hits

    # -- memory check (AC-T5) --------------------------------------------------------------
    def _copy_value(self, hop: Hop) -> float | int:
        t = hop.watched
        return t.vtype.decode(self._read(t.address, t.vtype.size))

    def _wait_steady(self, hop: Hop, until: float, differ_from=None) -> float | int | None:
        """Poll the copy until it held one value for STEADY_S (and, with
        ``differ_from``, one that isn't that). None when ``until`` passes."""
        held, since = None, self.clock()
        began = since
        try:
            return self._poll(hop, until, differ_from, held, since)
        finally:
            self._waited += self.clock() - began

    def _poll(self, hop: Hop, until: float, differ_from, held, since) -> float | int | None:
        while True:
            self._check_cancel()
            v = self._copy_value(hop)
            now = self.clock()
            if v != held and not (_nan(v) and _nan(held)):
                held, since = v, now
            elif now - since >= STEADY_S and (differ_from is None or v != differ_from):
                return v
            if now >= until:
                return None
            self.sleep(POLL_S)

    def _deadline(self, wait: float) -> float:
        return min(self.clock() + wait, self._start + TOTAL_S)

    def _check(self, hop: Hop, options: list[_Option]) -> _Option | None:
        first = self._wait_steady(hop, self._deadline(CHANGE_WAIT_S))
        if first is None:
            hop.stop = "the number never held still"
            raise _Stop(self._end(Outcome.UNCONFIRMED, hop.stop))
        alive = [o for o in options if self._matches(hop, o, first)]
        if not alive:
            return None
        self._say(hop, Stage.CHANGE)
        second = self._wait_steady(hop, self._deadline(CHANGE_WAIT_S), differ_from=first)
        if second is None:
            hop.stop = "the number didn't change"
            raise _Stop(self._end(Outcome.UNCONFIRMED, hop.stop))
        alive = [o for o in alive if self._matches(hop, o, second)]
        return alive[0] if alive else None

    def _max_now(self, way: Way) -> dict[int, float | int] | None:
        maxes = {}
        for s in way.steps:
            if s.kind is Kind.SHARE and s.max_at is not None:
                vt = ValueType.DOUBLE if s.to is ValueType.DOUBLE else ValueType.FLOAT
                maxes[s.max_at] = vt.decode(self._read(s.max_at, vt.size))
        return maxes or None

    def _matches(self, hop: Hop, o: _Option, shown: float | int) -> bool:
        way = o.way
        try:
            real = way.real.decode(self._read(o.cand.address, way.real.size))
            max_now = self._max_now(way)
        except ReadFailed:
            hop.tried.append((o.cand, "can't be read"))
            log.info(
                "action=trace_check addr=%#x held=- expected=%s ok=False", o.cand.address, shown
            )
            return False
        unit = unit_of(hop.watched.vtype.text(shown)) if hop.watched.vtype.is_float else 1.0
        ok = way.matches(real, shown, max_now, unit)
        log.info(
            "action=trace_check addr=%#x held=%s expected=%s ok=%s",
            o.cand.address,
            real,
            shown,
            ok,
        )
        if not ok:
            hop.tried.append(
                (o.cand, f"held {_fmt(real)}, expected {_fmt(_expected(way, shown, max_now))}")
            )
        return ok

    def _no_address(self, hop: Hop, options: list[_Option], total: Way | None) -> TraceResult:
        """No source passed: say why, the best we can (AC-T8)."""
        paths = [wp.path for wp in hop.writers if wp.path is not None]
        ways = [o.way for o in options] + [
            p.way
            for p in paths
            if p.way is not None and p.end in (tf.End.SOURCE, tf.End.BLOCK_START, tf.End.BLOCK_COPY)
        ]
        if ways:
            way = ways[0] if total is None else ways[0].then(total)
            hop.stop = "no address passed the memory check"
            try:
                max_now = self._max_now(way)
            except ReadFailed:
                max_now = None
            return self._end(
                Outcome.SEARCH_THAT_WAY, hop.stop, vtype=way.real, way=way, max_now=max_now
            )
        if any(p.end is tf.End.SCRAMBLED for p in paths):
            hop.stop = "scrambled"
            return self._end(Outcome.SCRAMBLED, hop.stop)
        if paths and all(p.end is tf.End.CONSTANT for p in paths):
            hop.stop = "sets it to a fixed number"
            return self._end(Outcome.NO_WRITER, hop.stop)
        hop.stop = "; ".join(p.reason or p.end.value for p in paths) or "no writer could be read"
        return self._end(Outcome.NOT_FOLLOWED, hop.stop)

    # -- write test (AC-T6) ----------------------------------------------------------------
    def _test(
        self, hop: Hop, option: _Option, copy: rc.Target, earlier: list[rc.Target]
    ) -> rc.Verdict:
        self._check_cancel()
        self._say(hop, Stage.TEST)
        src = rc.Target(option.cand.address, option.way.real)
        group = [src, copy, *earlier]
        checker = rc.RealChecker(
            self.client, self.pid, [group], settle_s=self.settle_s, wait=self.sleep
        )
        try:
            results = checker.run(cancel=self._cancel, rounds=1)
        finally:
            self.not_restored += checker.not_restored
        result = results.get((src.address, src.vtype))
        verdict = result.verdict if result is not None else rc.Verdict.FAILED
        hop.verdict = verdict
        log.info(
            "action=trace_test addr=%#x verdict=%s followers=%d",
            src.address,
            verdict.name,
            result.followers if result is not None else 0,
        )
        self._check_cancel()
        return verdict

    def _resume_all(self) -> None:
        while self._resumes:
            resume = self._resumes.pop()
            try:
                resume()
            except Exception:  # a lost link: the rest are still resumed
                log.exception("action=trace_resume_failed")


def _with_max(way: Way, cand: tf.Candidate) -> Way:
    """The way with its share-of-max step pointing at this candidate's max."""
    if cand.max_address is None or not way.has_share:
        return way
    steps = tuple(
        replace(s, max_at=cand.max_address) if s.kind is Kind.SHARE and s.max_at is None else s
        for s in way.steps
    )
    return replace(way, steps=steps)


def _repeats(hits: list[WatchHit]) -> bool:
    """EXEC_HITS hits at one place with identical registers: the breakpoint fires again
    and again without the game moving on."""
    if len(hits) < EXEC_HITS:
        return False
    first = hits[0]
    return all(h.rip == first.rip and h.regs == first.regs for h in hits)


def _nan(v) -> bool:
    return isinstance(v, float) and math.isnan(v)


def _fmt(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def _expected(way: Way, shown: float | int, max_now) -> float | int:
    try:
        return way.to_real(shown, max_now)
    except (ValueError, OverflowError, ZeroDivisionError):
        return shown
