"""The smart cheat builder, "Make it work" (docs/spec-cheat-builder.md).

The pure part: which writers may be patched and how (``classify``, rules S1 to S4 and
S7), in what order fixes are tried (``candidates``), and whether a test passed
(``judge``, the B5 rules). No console there, so all of it is tested directly.

The console part, ``Builder``: one method per step the dialog runs as a background
job (``real_check``, ``watch``, ``collect``, ``try_candidate``, ``finalize``). Every
byte a test fix puts in the game is noted in one record under a lock, so ``rollback``
can run from any thread (worker, Cancel, Disconnect) and runs once.

Putting a fix in, keeping it and putting it back are each one run of writes under a
second lock, so they never mix: ``close`` on another thread waits for the writes in
progress (``CLOSE_WAIT_S`` at most) and only then puts back. A put back the PS5 didn't
take is remembered (``not_restored``): the build tries nothing more after it.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum

import iced_x86 as ix

from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import build_notes as notes
from cheatmaker.core import cave as cv
from cheatmaker.core import linked as lk
from cheatmaker.core import real_check as rc
from cheatmaker.core.cave import Effect
from cheatmaker.core.cheats import Cheat, Location, LocationKind, combine, value_positions
from cheatmaker.core.freezer import LiveCheats
from cheatmaker.core.locker import (
    FLAGS_UNSAFE,
    NOP,
    Writer,
    decode_one,
    find_writers,
    writes_memory,
)
from cheatmaker.core.session import Session
from cheatmaker.core.values import ValueType
from cheatmaker.errors import (
    BuildFailed,
    CheatMakerError,
    ConnectionLost,
    LockFailed,
    ProtocolError,
    ReadFailed,
    ScanCancelled,
    WriteFailed,
)

log = logging.getLogger(__name__)

MAX_CANDIDATES = 6
FLOAT_HOLD_TOL = 0.01  # Never goes down: a decimal may read this much lower
FLOAT_FULL_TOL = 0.005  # Always full: decimals within 0.5% of Full
HALF_TOL = 0.02  # Half damage: decimal drops within 2% of half the game's
_CAN_FIX = frozenset({"ZF", "SF", "OF"})  # flags_fix can set these from our value
_info = ix.InstructionInfoFactory()
_R = ix.RflagsBits
_STATUS = _R.CF | _R.PF | _R.AF | _R.ZF | _R.SF | _R.OF

# Reasons shown under "Skipped" (plain words, as the rest of the app).
SHARED = "it writes through a bare pointer, so it's probably shared by many values"
WRONG_SIZE = "it writes more than this value"
MANY_WRITES = "it writes more than one place"
FLAGS_UNKNOWN = "couldn't tell what the game checks right after it"
FLAGS_CARRY = "the game checks a carry or parity right after it"
FLAGS_BEFORE = "the game checks a flag from before it right after it"
CRASHED = "it crashed the game last time"
NO_METHOD = "no safe way to change it with this effect"
TOO_SHORT_ONLY_ME = "it's too short for a jump, and Only me needs one"
OTHER_PLACE_ONLY_ME = "it reaches the value another way, so Only me can't tell you apart there"
SAME_CODE = "it's the same code as an earlier fix"


class Method(Enum):
    HOOK = "hook"  # a jump into our routine
    NOP = "nop"  # the instruction switched off


@dataclass(frozen=True)
class Eligible:
    writer: Writer
    methods: tuple[Method, ...]  # in the order they are tried
    flags_fix: bool = False  # the hook must leave ZF/SF/OF from the stored value


@dataclass(frozen=True)
class Skipped:
    writer: Writer
    reason: str


@dataclass(frozen=True)
class Part:
    """One writer and how a candidate changes it."""

    writer: Writer
    method: Method
    flags_fix: bool = False


@dataclass(frozen=True)
class Candidate:
    parts: tuple[Part, ...]

    @property
    def method(self) -> Method:
        return self.parts[0].method

    @property
    def label(self) -> str:
        what = "switch it off" if self.method is Method.NOP else "patch it"
        if len(self.parts) > 1:
            return f"{what}: all {len(self.parts)} instructions"
        return f"{what}: {self.parts[0].writer.text}"


def _store_size(insn: ix.Instruction) -> int:
    return ix.MemorySizeInfo(insn.memory_size).size


def _memory_writes(insn: ix.Instruction) -> int:
    writes = (ix.OpAccess.WRITE, ix.OpAccess.READ_WRITE, ix.OpAccess.COND_WRITE)
    writes += (ix.OpAccess.READ_COND_WRITE,)
    return sum(1 for m in _info.info(insn).used_memory() if m.access in writes)


def _hookable(writer: Writer, vtype: ValueType) -> bool:
    """A 5-byte jump fits over the writer alone, it writes through ``[reg+disp]``, and
    effect_code takes it (the store is the value, no stack use): so a refusal shows
    here, where the switch-off can still be offered, not when the fix is prepared."""
    if writer.length < cv.JMP_LEN:
        return False
    try:
        target = cv.writer_target(writer)
        cv.hook_span(lambda _a, _n: writer.code, writer.address, single=True)
        cv.check_effect_writer(decode_one(writer.code, writer.address), target, vtype)
    except LockFailed:
        return False
    return True


def classify(
    writers: Sequence[Writer],
    vtype: ValueType,
    effect: Effect,
    only_me: bool,
    crashed: Callable[[Writer, Method], bool] = lambda _w, _m: False,
    *,
    now_full: bool = False,
) -> tuple[list[Eligible], list[Skipped]]:
    """Sort ``writers`` into those a fix may touch (with the methods allowed, in order)
    and those skipped, with the reason (spec B3).

    ``now_full``: the value equals Full right now, which lets Always full fall back to
    switching the writer off."""
    eligible: list[Eligible] = []
    skipped: list[Skipped] = []
    for w in writers:
        reason, ok = _rule_out(w, vtype)
        if reason is not None:
            skipped.append(Skipped(w, reason))
            continue
        flags = w.flags
        read = flags.read if flags is not None else frozenset()
        flags_fix = bool(read)
        methods: list[Method] = []
        if _hookable(w, vtype):
            methods.append(Method.HOOK)
        nop_ok = ok and not read and not only_me
        if nop_ok and (
            (effect is Effect.NEVER_DOWN and not methods)
            or (effect is Effect.ALWAYS_FULL and now_full)
        ):
            methods.append(Method.NOP)
        live = [m for m in methods if not crashed(w, m)]
        if not live:
            if methods:
                why = CRASHED
            elif only_me and w.length < cv.JMP_LEN:
                why = TOO_SHORT_ONLY_ME
            else:
                why = NO_METHOD
            skipped.append(Skipped(w, why))
            continue
        eligible.append(Eligible(w, tuple(live), flags_fix))
    for e in eligible:
        log.info(
            "action=build_writer writer=%#x text=%r hits=%d methods=%s flags_fix=%s",
            e.writer.address,
            e.writer.text,
            e.writer.hits,
            ",".join(m.value for m in e.methods),
            e.flags_fix,
        )
    for s in skipped:
        log.info(
            "action=build_writer writer=%#x text=%r hits=%d skipped=%r",
            s.writer.address,
            s.writer.text,
            s.writer.hits,
            s.reason,
        )
    return eligible, skipped


def _rule_out(w: Writer, vtype: ValueType) -> tuple[str | None, bool]:
    """(why the writer is skipped by every method or None, whether NOPs are safe)."""
    if w.shared:  # S1
        return SHARED, False
    if w.unsafe is not None and w.unsafe != FLAGS_UNSAFE:  # S2
        return w.unsafe, False
    insn = decode_one(w.code, w.address)
    if not writes_memory(insn) or _memory_writes(insn) != 1:  # S4
        return MANY_WRITES, False
    if _store_size(insn) != vtype.size:
        return WRONG_SIZE, False
    flags = w.flags
    if flags is None and insn.rflags_modified & _STATUS:
        return FLAGS_UNKNOWN, False  # not checked (find_writers always checks)
    if flags is not None:  # S3: what a hook can't put right
        if flags.unknown:
            return FLAGS_UNKNOWN, False
        if flags.read - _CAN_FIX:
            return FLAGS_CARRY, False
        if flags.read and flags.before:  # flags_fix's cmp would change those too
            return FLAGS_BEFORE, False
    return None, True


def candidates(eligible: Sequence[Eligible], limit: int = MAX_CANDIDATES) -> list[Candidate]:
    """Most hits first, each writer's methods in order, then the first method on all of
    them together when there are two or more (spec B4). At most ``limit``."""
    ranked = sorted(eligible, key=lambda e: -e.writer.hits)
    out = [Candidate((Part(e.writer, m, e.flags_fix),)) for e in ranked for m in e.methods]
    if len(ranked) >= 2:
        out.append(Candidate(tuple(Part(e.writer, e.methods[0], e.flags_fix) for e in ranked)))
    return out[:limit]


# -- judging a test (spec B5) -------------------------------------------------------------
class Outcome(Enum):
    PASS = "pass"
    FAIL = "fail"
    NO_HIT = "no hit"  # nothing to judge: the same fix is tried once more
    NEEDS_ANSWER = "needs answer"


class Question(Enum):
    GOT_HIT = "Did you take a hit?"
    ENEMIES = "Did enemies still take damage?"


class Answer(Enum):
    YES = "Yes"
    NO = "No"
    NOT_SURE = "Not sure"


@dataclass(frozen=True)
class Sample:
    """One poll: the value, the routine's counter and the game's last new value."""

    value: float | int
    count: int = 0
    game_new: float | int | None = None


@dataclass(frozen=True)
class TestData:
    effect: Effect
    vtype: ValueType
    start: float | int  # the value when the test started
    samples: tuple[Sample, ...]
    full: float | int | None = None
    alive: bool = True
    nop: bool = False  # a plain NOP: no counter, the player says whether it hit
    only_me: bool = False
    others_down: bool | None = None  # an other object's value went down in the test


@dataclass(frozen=True)
class Judgement:
    outcome: Outcome
    reason: str = ""
    question: Question | None = None


def judge(data: TestData, answers: Mapping[Question, Answer] | None = None) -> Judgement:
    """Pass or fail with the reason; or which question the player must answer first."""
    answers = answers or {}
    if not data.alive:
        return Judgement(Outcome.FAIL, "the game stopped responding")
    broken = _effect_broken(data)
    if broken:
        return Judgement(Outcome.FAIL, broken)
    if data.nop:
        got = answers.get(Question.GOT_HIT)
        if got is None:
            return Judgement(Outcome.NEEDS_ANSWER, question=Question.GOT_HIT)
        if got is not Answer.YES:
            return Judgement(Outcome.NO_HIT, "you didn't get hit")
    elif not data.samples or data.samples[-1].count < 1:
        return Judgement(Outcome.NO_HIT, "you didn't get hit")
    if data.effect is Effect.HALF and not data.nop and not _halved_drops(data):
        return Judgement(Outcome.NO_HIT, "no hit that lowered it was seen")
    if data.only_me and not data.others_down:
        enemies = answers.get(Question.ENEMIES)
        if enemies is None:
            return Judgement(Outcome.NEEDS_ANSWER, question=Question.ENEMIES)
        if enemies is not Answer.YES:
            return Judgement(Outcome.FAIL, "enemies may have stopped taking damage")
    return Judgement(Outcome.PASS)


def _effect_broken(d: TestData) -> str | None:
    vt = d.vtype
    if d.effect is Effect.NEVER_DOWN:
        tol = FLOAT_HOLD_TOL if vt.is_float else 0
        if _is_nan(d.start):
            return None  # nothing to compare with
        if any(_is_nan(s.value) for s in d.samples):  # the routine lets a NaN through
            return "the value stopped being a number"
        if any(s.value < d.start - tol for s in d.samples):  # -inf included
            return "the value still went down"
        return None
    if d.effect is Effect.ALWAYS_FULL:
        full = d.full
        if full is None:
            return "Full isn't known"
        ticked = [s for s in d.samples if s.count >= 1] if not d.nop else list(d.samples)
        for s in ticked:
            if vt.is_float:
                if not abs(s.value - full) <= FLOAT_FULL_TOL * abs(full) + 1e-6:
                    return "the value didn't stay full"
            elif s.value != full:
                return "the value didn't stay full"
        return None
    for prev, cur in zip(d.samples, d.samples[1:], strict=False):  # Half damage
        if cur.value < prev.value and cur.count == prev.count:
            return "it went down without the fix running"
        one_tick = cur.count == prev.count + 1 and cur.game_new is not None
        if (
            one_tick
            and cur.value < prev.value
            and not _half_ok(vt, prev.value, cur.value, cur.game_new)
        ):
            return "a hit took more (or less) than half"
    return None


def _halved_drops(d: TestData) -> int:
    return sum(
        1
        for prev, cur in zip(d.samples, d.samples[1:], strict=False)
        if cur.count == prev.count + 1 and cur.value < prev.value
    )


def _half_ok(vt: ValueType, prev, cur, game_new) -> bool:
    game_drop = prev - game_new
    if game_drop <= 0:
        return False
    if vt.is_float:
        want = prev - game_drop / 2
        return abs(cur - want) <= HALF_TOL * game_drop + 1e-6
    return cur == prev - (game_drop + 1) // 2


def _is_nan(v) -> bool:
    return isinstance(v, float) and math.isnan(v)


# -- the console part -------------------------------------------------------------------
WATCH_S = 30.0  # B2: how long the write watch runs at most
EARLY_HITS = 3  # B2: this many writes caught ...
EARLY_MORE_S = 5.0  # ... and this much longer, then the watch ends early
COLLECT_S = 20.0  # B4: Only me's Collect step
TEST_S = 20.0  # B5: one live test
POLL_S = 0.25  # B5: read the value and the counter this often
ALIVE_EVERY_S = 1.0  # B5: check the game is alive this often
ALIVE_ANSWER_S = 2.0  # B5: a read slower than this means the PS5 isn't answering
PROCESSES_EVERY_S = 3.0  # fallback when the PS5 can't say which app is in front
PUT_BACK_WAIT_S = notes.PUT_BACK_WAIT_S  # B6: before routine and counter are zeroed
MAX_OTHERS = 4  # Only me: other objects read during a test
SAMPLE_TRIES = 3  # B5: readings with a hit in the middle are taken again this often
CLOSE_WAIT_S = 10.0  # close(): how long to wait for writes in progress on another thread

NOTHING_CHANGED = "Nothing changed it. Try again and make it change while the bar moves."
NOTHING_WORKED = "None of the safe fixes worked for this value. Your game is unchanged."
NOT_SURE_REAL = "Not sure this is the real one."
ONLY_YOU_SEEN = "Only you were seen. Get hit and hit enemies while the bar moves."
CANT_TELL_APART = "Couldn't tell you apart from enemies."
NOT_TAKEN = "the PS5 didn't take the change"
RESTART = notes.RESTART
SLOT_IN_USE = "the game uses the place kept for the test counter"
CLOSED_WHILE_KEEPING = "Stopped before the fix was kept. Your game is unchanged."
LIMIT = "This value is kept in more places than the app can fix on its own."
SESSION_ONLY = "This session only"
NO_PASSED_FIX = "There's no passed fix to keep."
_STATUS_LOG = {
    lk.Status.KEPT_UP: "kept_up",
    lk.Status.BEHIND: "behind",
    lk.Status.UNKNOWN: "unknown",
}


class PlaceFailed(BuildFailed):
    """A linked place (2 or more) can't be fixed in code: the build goes on to the
    fallback (spec-linked-values L11) with the places already kept."""


class TryResult(Enum):
    PASS = "pass"
    FAIL = "fail"
    NO_HIT = "no hit"
    NEEDS_ANSWER = "needs answer"
    LOST = "lost"  # the game stopped answering
    CANCELLED = "cancelled"


class Rollback(Enum):
    RESTORED = "restored"
    NOT_RESTORED = "not restored"  # the PS5 didn't answer: the bytes may still be there
    NOTHING = "nothing"  # nothing was in the game


@dataclass(frozen=True)
class TestOutcome:
    result: TryResult
    hint: str = ""
    question: Question | None = None
    rollback: Rollback = Rollback.NOTHING


@dataclass(frozen=True)
class BuildResult:
    cheat: Cheat
    after_restart: str  # "Yes", "Check after restart" (B8) or "This session only" (L13)
    skipped: tuple[Skipped, ...] = ()
    places: int = 1
    linked: str = "none"  # none | fixed | freeze | partial (L16)
    reason: str = ""  # the fallback's: why the last place wasn't fixed in code (L11)


@dataclass(frozen=True)
class LinkedPlan:
    """What follows a passed test (L1): done, or the members that fell behind (the
    dialog then says so: hints.BUILD_LINKED_BEHIND)."""

    done: bool
    behind: tuple[lk.PoolMember, ...] = ()


@dataclass(frozen=True)
class NextPlace:
    """What ``next_place`` found: the place to build next, the fallback (L11) with its
    reason, or neither (nothing left that may be fixed: finalize)."""

    place: int | None = None
    total: int = 1
    member: lk.PoolMember | None = None
    fallback_reason: str | None = None

    @property
    def finish(self) -> bool:
        return self.place is None and self.fallback_reason is None


@dataclass
class _Room:
    """One hooked writer's routine rooms (test, saved) and its jump."""

    part: Part
    test_room: int
    saved_room: int
    code: bytes  # the test routine (with counter lines)
    jmp: bytes
    span: list[ix.Instruction]
    target: cv.Target


@dataclass
class _Installed:
    candidate: Candidate
    n: int
    sites: list[tuple[int, bytes, bytes]]  # (address, original, ours) in the game's code
    rooms: list[tuple[int, int]]  # (address, size) zeroed on put back, slot included
    routines: list[tuple[int, bytes]]  # (address, bytes) written before the jumps
    hooks: list[_Room] = field(default_factory=list)
    slot: int | None = None
    total: int = 0  # how many fixes this build tries, for the hint
    put_back: Rollback | None = None  # how its put back went, for the other thread
    method: str = ""  # the crash note's method when not the candidate's ("collect")
    seen_count: int = 0  # the highest counter read in the test
    pool: lk.PoolData | None = None  # what the pool watch saw in its test (linked values)
    place: int = 1


@dataclass
class _Place:
    """A passed place, swapped to its kept routine, not yet part of a cheat."""

    n: int
    address: int
    vtype: ValueType
    full: float | int | None
    member: lk.PoolMember | None  # None for place 1
    inst: _Installed
    saved: list[tuple[_Room, bytes, bytes]]
    filter: cv.Filter | None
    by_class: bool
    player_obj: int | None  # Only me: the object the collected writer wrote through
    other_objs: list[int]  # Only me: other objects seen by Collect
    skipped: list[Skipped]

    @property
    def by_field(self) -> bool:
        return self.filter is not None and not self.by_class


class Builder:
    """Make it work for one value. ``sleep`` and ``clock`` are injected for tests.

    ``release`` stops the app's own freeze on the value (else the watch only sees our
    writes). ``extra_reserved``: module-offset rooms of open cheats' code lines that
    a build must never reuse. ``pool``: the value's Find group (spec-linked-values L2);
    its members are watched during each test. Without one (or when no member is left
    at Start) every request, wait and note is as without linked values."""

    def __init__(
        self,
        session: Session,
        address: int,
        vtype: ValueType,
        effect: Effect,
        *,
        only_me: bool = False,
        name: str = "",
        full: float | int | None = None,
        group: Sequence[rc.Target] = (),
        release: Callable[[], None] | None = None,
        live: LiveCheats | None = None,
        extra_reserved: Iterable[tuple[int, int]] = (),
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        pool: lk.LinkedGroup | None = None,
    ) -> None:
        if vtype not in cv.EFFECT_TYPES:
            raise BuildFailed(cv.UNSUPPORTED_TYPE)
        self.session = session
        self.address, self.vtype, self.effect, self.only_me = address, vtype, effect, only_me
        self.name = name or "Value"
        # What the game will store: a typed 99.9 compares equal to the value read back.
        self.full = None if full is None else vtype.decode(vtype.encode(vtype.clamp(full)))
        self.group = list(group)
        self.release = release
        self.live = live
        self.extra_reserved = list(extra_reserved)
        self.sleep, self.clock = sleep, clock
        self.writers: list[Writer] = []
        self.eligible: list[Eligible] = []
        self.skipped: list[Skipped] = []
        self.queue: list[Candidate] = []
        self.tried: list[tuple[Candidate, TestOutcome]] = []
        self.filter: cv.Filter | None = None
        self.by_class = False
        self.others: list[int] = []  # other objects' value addresses (Only me)
        self.collect_tries = 0
        self.real_results: dict[rc.Key, rc.Result] = {}  # from real_check, for the Find list
        self.closed = False  # close() ran: no step starts or writes any more
        self.not_restored = False  # a put back the PS5 didn't take: stop, say Restart
        self._lock = threading.Lock()  # guards the fields below
        self._op = threading.RLock()  # one run of writes at a time (install, keep, put back)
        self._step_cancel: threading.Event | None = None  # the running step's; close sets it
        self._installed: _Installed | None = None
        self._passed: _Installed | None = None
        self._pending: tuple[_Installed, TestData] | None = None
        self._answers: dict[Question, Answer] = {}
        self._repeat: Candidate | None = None
        self._repeated: set[int] = set()
        # Linked values: all empty without a pool.
        self.linked = pool  # the Find group snapshot
        self.pool: list[lk.PoolMember] = []  # watched members after the Start check
        self.copies: list[lk.PoolMember] = []  # marked copy: watched if room, never fixed
        self.pool_full: dict[int, float | int] = {}  # Always full: each member's Full (L8)
        self.pool_released = 0  # Find freezes the dialog released on the pool
        self.pool_rows: dict[int, lk.PoolRow] = {}  # by address, for Advanced (L15)
        self.place = 1
        self.linked_result = "none"  # none | fixed | freeze | partial
        self.place_member: lk.PoolMember | None = None  # None for place 1
        self.places_total = 1  # for "Place N of M"
        self.tests_total = 0  # every test of the build, repeats included (L10)
        self.limit_hit = False  # next_candidate refused test MAX_TESTS + 1
        self._place1: rc.Target | None = None  # the reference of later places' tests
        self._place_start: float | int | None = None  # its value when its test started
        self._place_tried = 0  # where this place's tries start in ``tried``
        self._player_obj: int | None = None  # Only me: Collect's object
        self._other_objs: list[int] = []
        self._kept: list[_Place] = []  # passed places in the game, not yet a cheat
        self._kept_reserved: list[tuple[int, int]] = []  # their rooms and slots (offsets)
        self._fixed_sites: set[int] = set()  # their writers
        self._last_pool: lk.PoolData | None = None  # the readings after_pass judged
        self._behind: list[lk.PoolMember] = []  # fell behind, may be fixed (or unmarked)
        self._behind_copies: list[lk.PoolMember] = []
        self._hold: lk.PoolMember | None = None  # what L11 holds
        self._hold_ok = True  # False once MAX_PLACES are fixed: nothing more is added
        self._more_behind = False  # other clusters still behind at the fallback

    # -- helpers -------------------------------------------------------------------
    def _game(self):
        return self.session.require_game()

    def _client(self):
        return self.session.require_client()

    def _read_value(self, addr: int | None = None) -> float | int:
        game = self._game()
        raw = self._client().read(game.pid, self.address if addr is None else addr, self.vtype.size)
        return self.vtype.decode(raw)

    def _start_step(self, cancel: threading.Event | None) -> threading.Event:
        """Refuse to start once closed; returns the step's cancel, which close() sets."""
        if self.closed:
            raise ScanCancelled("build closed")
        ev = cancel if cancel is not None else threading.Event()
        with self._lock:
            self._step_cancel = ev
        return ev

    @property
    def has_kept(self) -> bool:
        """Passed places are in the game, waiting for the build to end (linked values)."""
        return bool(self._kept)

    @property
    def place_tried(self) -> list[tuple[Candidate, TestOutcome]]:
        """The tries of the current place (all of them at place 1)."""
        return self.tried[self._place_tried :]

    def _watched_pool(self) -> list[lk.PoolMember]:
        """The pool members judged in this place's test: not a place of this build."""
        places = {p.address for p in self._kept} | {self.address}
        return [m for m in self.pool if m.address not in places]

    @contextmanager
    def _linked_step(self, *, fail_place: bool = True) -> Iterator[None]:
        """A worker step of a linked place (L12): Cancel puts back every place of the
        build here, so the UI thread never waits; a step that can't go on is
        PlaceFailed (the fallback, L11), except a put back the PS5 didn't take."""
        try:
            yield
        except ScanCancelled:
            self.rollback_all("cancelled")
            raise
        except PlaceFailed:
            raise
        except BuildFailed as exc:
            if not fail_place or self.not_restored or str(exc) == RESTART:
                raise
            raise PlaceFailed(str(exc)) from exc
        except (ReadFailed, LockFailed) as exc:  # this place only: the kept ones stay
            if not fail_place:
                raise
            raise PlaceFailed(f"Couldn't watch it: {exc}") from exc

    def _crashed(self, w: Writer, m: Method) -> bool:
        game = self._game()
        return notes.is_crashed(game.title_id, game.version, w.address - game.module_base, m.value)

    # -- B1: which member of the group ----------------------------------------------
    def real_check(self, cancel: threading.Event | None = None) -> str | None:
        """Check which is real on the value's group (one group only) and use the member
        it marks real. Returns a hint when it can't tell."""
        if len(self.group) < 2:
            return None
        game = self._game()
        checker = rc.RealChecker(self._client(), game.pid, [self.group], wait=self.sleep)
        self.real_results = dict(checker.run(cancel=cancel))
        best = checker.best(self.group)
        if best is None or best.vtype is not self.vtype:
            log.info("action=build_real_check address=%#x real=none", self.address)
            return NOT_SURE_REAL
        log.info("action=build_real_check address=%#x real=%#x", self.address, best.address)
        self.address = best.address
        return None

    # -- B2: watching for the code ----------------------------------------------------
    def until(self, cancel: threading.Event | None = None) -> Callable[[list, float], bool]:
        """End the watch on Cancel, or 5 s after the third write."""
        enough_at: list[float] = []

        def stop(hits: list, elapsed: float) -> bool:
            if cancel is not None and cancel.is_set():
                return True
            if len(hits) >= EARLY_HITS and not enough_at:
                enough_at.append(elapsed)
            return bool(enough_at) and elapsed - enough_at[0] >= EARLY_MORE_S

        return stop

    def watch(self, cancel: threading.Event | None = None) -> list[Candidate]:
        """Catch the writers, classify them and line up the fixes to try."""
        cancel = self._start_step(cancel)
        if self.place > 1:
            with self._linked_step():
                return self._watch_place(cancel)
        game = self._game()
        if self.release is not None:
            self.release()
        if self.effect is Effect.ALWAYS_FULL and self.full is None:
            self.full = self._read_value()
        log.info(
            "action=build_start title=%s address=%#x type=%s effect=%s only_me=%s full=%s",
            game.title_id,
            self.address,
            self.vtype.name,
            self.effect.name,
            self.only_me,
            self.full,
        )
        if self.linked is not None and self.place == 1:
            self._prepare_pool()
        try:
            writers = find_writers(
                self.session, self.address, self.vtype.size, WATCH_S, until=self.until(cancel)
            )
        except LockFailed as exc:
            raise BuildFailed(NOTHING_CHANGED) from exc
        if cancel.is_set():
            raise ScanCancelled("build cancelled")
        self.writers = writers
        now_full = self.full is not None and self._read_value() == self.full
        self.eligible, self.skipped = classify(
            writers, self.vtype, self.effect, self.only_me, self._crashed, now_full=now_full
        )
        self.queue = [c for c in candidates(self.eligible) if not self._all_crashed(c)]
        if not self.queue:
            raise BuildFailed(NOTHING_WORKED)
        return list(self.queue)

    def _watch_place(self, cancel: threading.Event) -> list[Candidate]:
        """``watch`` for a linked place (L6): writers that are an earlier place's fix
        are skipped as the same code, and at most MAX_LINKED_FIXES fixes are lined up."""
        game = self._game()
        self._place_start = self._read_value()
        log.info(
            "action=build_start title=%s address=%#x type=%s effect=%s only_me=%s full=%s place=%d",
            game.title_id,
            self.address,
            self.vtype.name,
            self.effect.name,
            self.only_me,
            self.full,
            self.place,
        )
        try:
            writers = find_writers(
                self.session, self.address, self.vtype.size, WATCH_S, until=self.until(cancel)
            )
        except LockFailed as exc:
            raise BuildFailed(NOTHING_CHANGED) from exc
        if cancel.is_set():
            raise ScanCancelled("build cancelled")
        self.writers = writers
        same = [w for w in writers if self._same_code(w)]
        now_full = self.full is not None and self._read_value() == self.full
        self.eligible, skipped = classify(
            [w for w in writers if w not in same],
            self.vtype,
            self.effect,
            self.only_me,
            self._crashed,
            now_full=now_full,
        )
        for w in same:
            log.info("action=build_writer writer=%#x skipped=%r", w.address, SAME_CODE)
        self.skipped = [Skipped(w, SAME_CODE) for w in same] + skipped
        self.queue = self._line_up(self.eligible)
        if not self.queue:
            raise BuildFailed(NOTHING_WORKED)
        return list(self.queue)

    def _same_code(self, w: Writer) -> bool:
        """An earlier place's writer, or code inside its routine rooms (risk 5)."""
        base = self._game().module_base
        return w.address in self._fixed_sites or any(
            base + lo <= w.address < base + hi for lo, hi in self._kept_reserved
        )

    def _line_up(self, eligible: Sequence[Eligible]) -> list[Candidate]:
        limit = MAX_CANDIDATES if self.place == 1 else lk.MAX_LINKED_FIXES
        return [c for c in candidates(eligible, limit) if not self._all_crashed(c)]

    def _prepare_pool(self) -> None:
        """L2 at Start (the dialog released the Find freezes first): place 1 and every
        member read in one batch; members no longer k x place 1 are dropped, the rest
        get the factor read now. Always full: each member's Full (L8)."""
        assert self.linked is not None
        watched, copies = lk.pool_for(self.linked, (self.address, self.vtype), self.real_results)
        members = watched + copies
        place1 = self._place1 = rc.Target(self.address, self.vtype)
        dropped: list[tuple[lk.PoolMember, str]] = []
        kept: list[lk.PoolMember] = []
        if members:
            game, client = self._game(), self._client()
            raw: dict[rc.Key, bytes] = {}
            for ts in rc.spans([place1, *(m.target for m in members)]):
                raw.update(rc.read_span(client, game.pid, ts))
            got = raw.get((self.address, self.vtype))
            v1 = None if got is None else self.vtype.decode(got)
            values = {m.key: m.vtype.decode(raw[m.key]) if m.key in raw else None for m in members}
            kept, dropped = lk.check_start(place1, v1, members, values)
        self.pool = [m for m in kept if m.mark is not rc.Verdict.COPY]
        # Copies are only watched beside a member that may be fixed: with none left,
        # the build is exactly one without a pool.
        self.copies = [m for m in kept if m.mark is rc.Verdict.COPY] if self.pool else []
        if self.effect is Effect.ALWAYS_FULL and self.full is not None:
            self.pool_full = {
                m.address: lk.scaled_full(self.full, m.factor, m.vtype) for m in self.pool
            }
        for m, why in dropped:
            log.info("action=build_pool_drop member=%#x reason=%r", m.address, why)
        log.info(
            "action=build_pool members=%d dropped=%d released=%d copies=%d",
            len(self.pool),
            len(dropped),
            self.pool_released,
            len(self.copies),
        )

    def _all_crashed(self, c: Candidate) -> bool:
        if len(c.parts) < 2:
            return False
        return self._crashed(c.parts[0].writer, c.method) or notes.is_crashed(
            self._game().title_id,
            self._game().version,
            c.parts[0].writer.address - self._game().module_base,
            _method_name(c),
        )

    # -- B4: Only me ----------------------------------------------------------------
    def collect(self, cancel: threading.Event | None = None) -> str | None:
        """Hook the busiest writer for 20 s and work out how to tell the player's object
        from the others. Returns a hint to collect again, or None when done."""
        cancel = self._start_step(cancel)
        if self.place > 1:
            with self._linked_step():
                return self._collect(cancel)
        return self._collect(cancel)

    def _collect(self, cancel: threading.Event) -> str | None:
        if self.not_restored:
            raise BuildFailed(RESTART)
        hookable = [e.writer for e in self.eligible if Method.HOOK in e.methods]
        if not hookable:
            raise BuildFailed(NOTHING_WORKED)
        writer = hookable[0]
        target = cv.writer_target(writer)
        player = self.address - target.disp
        objs = self._collect_objects(writer, target, cancel)
        others = [o for o in objs if o != player]
        if not others:
            self.collect_tries += 1
            if self.collect_tries >= 2:
                raise BuildFailed(CANT_TELL_APART)
            return ONLY_YOU_SEEN
        base = self._game().module_base
        mine = ah.object_vtable(self.session, player)
        theirs = [ah.object_vtable(self.session, o) for o in others]
        if mine is not None and all(t != mine for t in theirs):
            self.filter, self.by_class = cv.Filter("vtable", vtable=base + mine), True
        else:
            try:
                guesses = cv.compare_objects(self.session, player, objs)
            except LockFailed as exc:
                raise BuildFailed(CANT_TELL_APART) from exc
            if not guesses:
                raise BuildFailed(CANT_TELL_APART)
            g = guesses[0]
            self.filter = cv.Filter("field", field=g.offset, field_value=g.player_value)
        self._only_where_learned(target.disp)
        offset = self.address - player
        self._player_obj, self._other_objs = player, others[:MAX_OTHERS]
        self.others = [o + offset for o in others[:MAX_OTHERS]]
        log.info(
            "action=build_collect objects=%d filter=%s others=%d",
            len(objs),
            self.filter.kind,
            len(self.others),
        )
        return None

    def _only_where_learned(self, disp: int) -> None:
        """The filter checks the object the collected writer wrote through: a writer
        with another displacement holds another pointer there, so it is skipped."""
        keep: list[Eligible] = []
        for e in self.eligible:
            if cv.writer_target(e.writer).disp == disp:  # Only me: hooks only
                keep.append(e)
            else:
                self.skipped.append(Skipped(e.writer, OTHER_PLACE_ONLY_ME))
                log.info(
                    "action=build_writer writer=%#x skipped=%r",
                    e.writer.address,
                    OTHER_PLACE_ONLY_ME,
                )
        if len(keep) != len(self.eligible):
            self.eligible = keep
            self.queue = self._line_up(keep)

    def reuse_filter(self) -> bool:
        """L7: a linked place's writer that writes through the same object as a kept
        place's collected writer uses that place's filter as it is (no Collect). True
        when it did; False means Collect runs for this place."""
        for e in self.eligible:
            if Method.HOOK not in e.methods:
                continue
            disp = cv.writer_target(e.writer).disp
            obj = self.address - disp
            src = next((p for p in self._kept if p.filter and p.player_obj == obj), None)
            if src is None:
                continue
            self.filter, self.by_class = src.filter, src.by_class
            self._player_obj, self._other_objs = obj, list(src.other_objs)
            self.others = [o + disp for o in src.other_objs]
            self._only_where_learned(disp)
            log.info(
                "action=build_collect place=%d reused=%d filter=%s others=%d",
                self.place,
                src.n,
                self.filter.kind,
                len(self.others),
            )
            return True
        return False

    def _collect_objects(
        self, writer: Writer, target: cv.Target, cancel: threading.Event
    ) -> list[int]:
        """The sampler hook (cave.sampler_code) for COLLECT_S, put in and taken out as
        a test fix is: noted, under the write lock, and put back by close()."""
        game = self._game()
        client = self._client()
        read = lambda a, k: client.read(game.pid, a, k)  # noqa: E731
        span = cv.hook_span(read, writer.address)
        orig = b"".join(read(i.ip, i.len) for i in span)
        room = cv.find_cave(
            self.session,
            cv.CAVE_ROOM,
            skip=cv.BUILD_CAVE_SKIP,
            extra_reserved=self.extra_reserved + self._kept_reserved,
        )
        table = cv.find_table(self.session)
        code = cv.sampler_code(span, target, table, room)
        inst = _Installed(
            Candidate((Part(writer, Method.HOOK),)),
            0,
            sites=[(writer.address, orig, cv.jmp_patch(writer.address, room, len(orig)))],
            rooms=[(room, cv.CAVE_ROOM), (table, cv.TABLE_SIZE)],
            routines=[(room, code)],
            method="collect",
            place=self.place,
        )
        failed = self._install(inst, cancel)
        if failed is not None:
            if failed.rollback is Rollback.NOT_RESTORED:
                raise BuildFailed(RESTART)
            if failed.result is TryResult.CANCELLED:
                raise ScanCancelled("collecting cancelled")
            raise BuildFailed(f"Couldn't start Collect: {failed.hint}.")
        try:
            waited = 0.0
            while waited < COLLECT_S:
                if cancel.is_set() or self._taken_back(inst) is not None:
                    raise ScanCancelled("collecting cancelled")
                self.sleep(POLL_S)
                waited += POLL_S
            raw = read(table, cv.TABLE_SIZE)  # entries added after this are only lost
        finally:
            # Cancelled with places kept: every place goes back in one run (one wait).
            if cancel.is_set() and self._kept:
                rb = self._end_all(inst, "cancelled")
            else:
                rb = self._end(inst, "collected")
        if rb is Rollback.NOT_RESTORED:
            raise BuildFailed(RESTART)
        objs = sorted({v for (v,) in struct.iter_unpack("<Q", raw) if v})
        log.info("action=player_only_collect objects=%d", len(objs))
        return objs

    # -- B4/B5: one fix at a time -------------------------------------------------------
    def next_candidate(self) -> Candidate | None:
        """The fix to try next: the same one once more after "you didn't get hit".
        None after MAX_TESTS tests in the build (``limit_hit``, L10)."""
        if self.tests_total >= lk.MAX_TESTS:
            self.limit_hit = self._repeat is not None or bool(self.queue)
            return None
        if self._repeat is not None:
            c, self._repeat = self._repeat, None
            return c
        return self.queue.pop(0) if self.queue else None

    def try_candidate(
        self,
        c: Candidate,
        cancel: threading.Event | None = None,
        progress: Callable[[float, str], None] | None = None,
    ) -> TestOutcome:
        """Put the fix in, let the player play for 20 s, judge it. On anything but a pass
        (or a question for the player) the game is put back before this returns."""
        cancel = self._start_step(cancel)
        if self.not_restored:  # an earlier fix may still be in: never add another
            raise BuildFailed(RESTART)
        if self.only_me and self.filter is None:
            raise BuildFailed(CANT_TELL_APART)
        before = {id(t) for t, _o in self.place_tried} - {id(c)}
        n, total = len(before) + 1, len(before) + 1 + len(self.queue)
        self._answers = {}
        self.tests_total += 1
        try:
            inst = self._prepare(c, n)
            inst.total = total
        except (LockFailed, ReadFailed) as exc:  # S6: nothing was written
            return self._done(c, TestOutcome(TryResult.FAIL, str(exc)))
        keep = False
        try:
            outcome = self._install_and_test(inst, cancel, progress)
            keep = outcome.result in (TryResult.PASS, TryResult.NEEDS_ANSWER)
            if outcome.result is TryResult.CANCELLED and self._kept:
                rb = self._end_all(inst, "cancelled")  # kept places too, on this worker
                outcome = replace(outcome, rollback=_worse(outcome.rollback, rb))
            return self._done(c, outcome)
        finally:
            if not keep:
                self.rollback("test ended")

    def _done(self, c: Candidate, outcome: TestOutcome) -> TestOutcome:
        self.tried.append((c, outcome))
        if outcome.result is TryResult.NO_HIT and id(c) not in self._repeated:
            self._repeated.add(id(c))
            self._repeat = c
        log.info(
            "action=build_try_result n=%d method=%s result=%s hint=%r rollback=%s",
            len(self.tried),
            _method_name(c),
            outcome.result.value,
            outcome.hint,
            outcome.rollback.value,
        )
        return outcome

    def _prepare(self, c: Candidate, n: int) -> _Installed:
        """S6 checks and the bytes to write. Writes nothing."""
        game = self._game()
        client = self._client()
        read = lambda a, k: client.read(game.pid, a, k)  # noqa: E731
        sites: list[tuple[int, bytes, bytes]] = []
        hooks = [p for p in c.parts if p.method is Method.HOOK]
        slot = rooms_at = None
        if hooks:
            rooms_at = cv.find_cave(
                self.session,
                2 * cv.CAVE_ROOM * len(hooks),
                skip=cv.BUILD_CAVE_SKIP,
                extra_reserved=self.extra_reserved + self._kept_reserved,
            )
            slot = cv.find_slot(self.session, self._kept_reserved)
        full = self.vtype.encode(self.full) if self.full is not None else None
        inst = _Installed(c, n, sites, [], [], slot=slot, place=self.place)
        k = 0
        for part in c.parts:
            w = part.writer
            if not (game.module_base <= w.address and w.address + w.length <= game.module_end):
                raise LockFailed("the instruction is outside the game's own code")
            if read(w.address, w.length) != w.code:
                raise LockFailed("the game's code isn't what we expected; refusing to patch")
            if part.method is Method.NOP:
                sites.append((w.address, w.code, bytes([NOP]) * w.length))
                continue
            assert rooms_at is not None and slot is not None
            span = cv.hook_span(read, w.address, single=True)
            target = cv.writer_target(w)
            test_room = rooms_at + 2 * cv.CAVE_ROOM * k
            k += 1
            code = cv.effect_code(
                span,
                target,
                self.vtype,
                self.effect,
                test_room,
                full=full,
                counter=slot,
                only_me=self.filter if self.only_me else None,
                flags_fix=part.flags_fix,
            )
            jmp = cv.jmp_patch(w.address, test_room, w.length)
            room = _Room(part, test_room, test_room + cv.CAVE_ROOM, code, jmp, span, target)
            inst.hooks.append(room)
            inst.routines.append((test_room, code))
            inst.rooms.append((test_room, 2 * cv.CAVE_ROOM))
            sites.append((w.address, w.code, jmp))
        if slot is not None:
            inst.rooms.append((slot, cv.SLOT_SIZE))
        for addr, size in inst.rooms:
            if not (game.module_base <= addr and addr + size <= game.module_end):
                raise LockFailed("the patch would land outside the game's own code")
        return inst

    def _note(
        self, inst: _Installed, sites: list[tuple[int, bytes, bytes]] | None = None
    ) -> notes.OpenNote:
        """The crash note for ``inst``; ``sites`` replaces its (address, original,
        ours) list, as when finalize re-aims the jumps."""
        game = self._game()
        base = game.module_base
        return notes.OpenNote(
            title_id=game.title_id,
            version=game.version,
            pid=game.pid,
            site_off=inst.candidate.parts[0].writer.address - base,
            method=inst.method or _method_name(inst.candidate),
            n=inst.n,
            patches=tuple(
                notes.Patch(a - base, o.hex(), new.hex()) for a, o, new in (sites or inst.sites)
            ),
            rooms=tuple((a - base, size) for a, size in inst.rooms),
            place=inst.place,
        )

    def _install_and_test(
        self,
        inst: _Installed,
        cancel: threading.Event,
        progress: Callable[[float, str], None] | None,
    ) -> TestOutcome:
        failed = self._install(inst, cancel)
        if failed is not None:
            return failed
        return self._live_test(inst, cancel, progress)

    def _install(self, inst: _Installed, cancel: threading.Event) -> TestOutcome | None:
        """Note, routines, then jumps, as one run of writes (close waits for it). None
        when the fix is in; else how it ended, with the game put back."""
        game = self._game()
        client = self._client()
        with self._op:
            if self.closed or cancel.is_set():
                return TestOutcome(TryResult.CANCELLED)
            if inst.slot is not None and client.read(game.pid, inst.slot, cv.SLOT_SIZE) != bytes(
                cv.SLOT_SIZE
            ):
                return TestOutcome(TryResult.FAIL, SLOT_IN_USE)  # nothing written
            notes.open_note(self._note(inst))  # before any byte goes in (B6)
            with self._lock:
                self._installed = inst
            log.info(
                "action=build_try n=%d method=%s writers=%s rooms=%s",
                inst.n,
                _method_name(inst.candidate),
                ",".join(f"{p.writer.address:#x}" for p in inst.candidate.parts),
                ",".join(f"{a:#x}" for a, _s in inst.rooms),
            )
            try:
                for addr, code in inst.routines:  # the routine first, then the jump into it
                    client.write(game.pid, addr, code)
                for addr, _orig, new in inst.sites:
                    client.write(game.pid, addr, new)
                landed = all(
                    client.read(game.pid, a, len(code)) == code for a, code in inst.routines
                ) and all(client.read(game.pid, a, len(new)) == new for a, _o, new in inst.sites)
            except WriteFailed:
                landed = False
            if self._installed is not inst:
                # Put back on this thread while we wrote: our later writes went in after
                # it, so put back once more.
                rb = self._put_back(inst, "closed while putting it in")
                return TestOutcome(TryResult.CANCELLED, rollback=rb)
            if not landed:
                return TestOutcome(TryResult.FAIL, NOT_TAKEN, rollback=self._end(inst, NOT_TAKEN))
        return None

    def _live_test(
        self,
        inst: _Installed,
        cancel: threading.Event,
        progress: Callable[[float, str], None] | None,
    ) -> TestOutcome:
        hint = f"Testing fix {inst.n} of {inst.total}: keep playing and take a hit."
        if self.place > 1:
            hint = (
                f"Testing fix {inst.n} of {inst.total} for place {self.place}: keep playing "
                "and take a hit."
            )
        nop = all(p.method is Method.NOP for p in inst.candidate.parts)
        others_start: dict[int, float | int] = {}
        pool = _PoolWatch(self) if self._watched_pool() else None
        try:
            start_value = self._read_value()
            if pool is not None and self.place > 1:
                pool.baseline()  # judge_later's line: the pool as the test starts (L5)
            for o in self.others:
                try:
                    others_start[o] = self._read_value(o)
                except ReadFailed:
                    continue
        except (ConnectionLost, ReadFailed):
            return self._lost(inst, game_gone=False)
        self._place_start = start_value
        if progress is not None:
            progress(0.0, hint)
        samples = [Sample(start_value)]
        others_down = False
        alive = _AliveCheck(self, inst)
        t0 = self.clock()
        while self.clock() - t0 < TEST_S:
            if cancel.is_set():
                return TestOutcome(TryResult.CANCELLED, rollback=self._end_all(inst, "cancelled"))
            # The pool's reads come out of the wait, so the value is read as often.
            self.sleep(POLL_S if pool is None else max(0.0, POLL_S - pool.last_cost))
            taken = self._taken_back(inst)
            if taken is not None:
                return TestOutcome(TryResult.CANCELLED, rollback=taken)
            try:
                sample = self._sample(inst)
                for o, before in others_start.items():
                    try:
                        if self._read_value(o) < before:
                            others_down = True
                    except ReadFailed:
                        continue
                ok, gone = alive.check()
                if ok and pool is not None:
                    # Place 1's value is the reference; later places read it with the pool.
                    ref = None if sample is None or self.place > 1 else sample.value
                    pool.tick(ref)
            except (ConnectionLost, ReadFailed):
                ok, gone = False, self._game_gone()
            if not ok:
                taken = self._taken_back(inst)  # the patch went because it was put back
                if taken is not None:
                    return TestOutcome(TryResult.CANCELLED, rollback=taken)
                return self._lost(inst, game_gone=gone)
            if sample is not None:
                samples.append(sample)
            if progress is not None:
                progress(min(1.0, (self.clock() - t0) / TEST_S), hint)
        if pool is not None:
            inst.pool = pool.finish()
        data = TestData(
            self.effect,
            self.vtype,
            start_value,
            tuple(samples),
            full=self.full,
            nop=nop,
            only_me=self.only_me,
            others_down=others_down if self.only_me else None,
        )
        self._pending = (inst, data)
        return self._verdict(inst, data)

    def _sample(self, inst: _Installed) -> Sample | None:
        """The value with the counter around it: counter, value, counter. A run of the
        routine between two reads changes the counter, so the reading is taken again
        (and dropped after SAMPLE_TRIES); judge sees the value and count of one moment."""
        if inst.slot is None:
            return Sample(self._read_value())
        game = self._game()
        client = self._client()
        raw = client.read(game.pid, inst.slot, cv.SLOT_SIZE)
        for _ in range(SAMPLE_TRIES):
            value = self._read_value()
            after = client.read(game.pid, inst.slot, cv.SLOT_SIZE)
            if after == raw:
                count = struct.unpack_from("<Q", raw)[0]
                inst.seen_count = max(inst.seen_count, count)
                return Sample(value, count, self.vtype.decode(raw[8 : 8 + self.vtype.size]))
            raw = after
        log.info("action=build_sample dropped=True")
        return None

    def _taken_back(self, inst: _Installed) -> Rollback | None:
        """None while the fix is in; else how its put back (by Cancel or Disconnect on
        another thread) went, waiting for that put back to finish."""
        with self._lock:
            if self._installed is inst:
                return None
        with self._op:
            return inst.put_back or Rollback.NOTHING

    def _end(self, inst: _Installed, reason: str) -> Rollback:
        """Put this fix back; if another thread already did, how that went."""
        rb = self.rollback(reason)
        if rb is Rollback.NOTHING and inst.put_back is not None:
            return inst.put_back
        return rb

    def _end_all(self, inst: _Installed, reason: str) -> Rollback:
        """``_end``, and with places kept, those too in the same run (one wait)."""
        if not self._kept:
            return self._end(inst, reason)
        rb = self.rollback_all(reason)
        if rb is Rollback.NOTHING and inst.put_back is not None:
            return inst.put_back
        return rb

    def _verdict(self, inst: _Installed, data: TestData) -> TestOutcome:
        j = judge(data, self._answers)
        log.info(
            "action=build_judge n=%d outcome=%s reason=%r samples=%d last_count=%d",
            inst.n,
            j.outcome.value,
            j.reason,
            len(data.samples),
            data.samples[-1].count,
        )
        if j.outcome is Outcome.PASS:
            self._passed = inst
            return TestOutcome(TryResult.PASS)
        if j.outcome is Outcome.NEEDS_ANSWER:
            return TestOutcome(TryResult.NEEDS_ANSWER, question=j.question)
        rb = self._end(inst, j.reason)
        if j.outcome is Outcome.NO_HIT:
            return TestOutcome(TryResult.NO_HIT, "You didn't get hit; trying again.", rollback=rb)
        return TestOutcome(TryResult.FAIL, j.reason, rollback=rb)

    def answer(self, question: Question, ans: Answer) -> TestOutcome:
        """The player's answer to a NEEDS_ANSWER test; judges it again."""
        if self._pending is None:
            raise BuildFailed("There's no test waiting for an answer.")
        inst, data = self._pending
        self._answers[question] = ans
        try:
            outcome = self._verdict(inst, data)
        except BaseException:
            self.rollback("answer failed")
            raise
        c = inst.candidate
        if self.tried and self.tried[-1][0] is c:
            self.tried[-1] = (c, outcome)
        if outcome.result is TryResult.NO_HIT and id(c) not in self._repeated:
            self._repeated.add(id(c))
            self._repeat = c
        return outcome

    def _game_gone(self) -> bool:
        """The PS5 answers, but the game's process isn't listed any more."""
        try:
            pid = self._game().pid
            return not any(p.pid == pid for p in self._client().processes())
        except CheatMakerError:
            return False

    def _lost(self, inst: _Installed, game_gone: bool) -> TestOutcome:
        """B6: put back at once. A game that is gone crashed with this fix (S7)."""
        rb = self._end(inst, "the game stopped responding")
        if game_gone:
            game = self._game()
            c = inst.candidate
            notes.mark_crashed(
                game.title_id,
                game.version,
                c.parts[0].writer.address - game.module_base,
                _method_name(c),
            )
            notes.clear_note()
            if self._kept:  # the kept places died with the game: nothing to put back
                notes.clear_kept()
                with self._lock:
                    self._kept = []
            return TestOutcome(
                TryResult.LOST,
                f"The game stopped with fix {inst.n}. Restart the game: your game is "
                "unchanged after a restart.",
                rollback=rb,
            )
        if rb is Rollback.NOT_RESTORED:
            return TestOutcome(TryResult.LOST, RESTART, rollback=rb)
        return TestOutcome(
            TryResult.LOST,
            f"The game stopped responding with fix {inst.n}. Put back. Trying the next one.",
            rollback=rb,
        )

    # -- B6: putting things back ----------------------------------------------------------
    def rollback(self, reason: str = "", *, wait: float | None = None) -> Rollback:
        """Original bytes back at the sites, then after 2 s the routine rooms and the
        counter zeroed, then the note cleared. Safe from any thread; the second call
        does nothing. NOT_RESTORED when the PS5 doesn't answer (the note stays open),
        or when writes in progress on another thread don't end within ``wait`` s."""
        if not self._op.acquire(timeout=-1 if wait is None else wait):
            log.warning("action=build_rollback reason=%r restored=False error=busy", reason)
            self.not_restored = True
            return Rollback.NOT_RESTORED
        try:
            with self._lock:
                inst, self._installed = self._installed, None
                if self._passed is inst:
                    self._passed = None
            if inst is None:
                return Rollback.NOTHING
            self._pending = None
            return self._put_back(inst, reason)
        finally:
            self._op.release()

    def _put_back(self, inst: _Installed, reason: str) -> Rollback:
        inst.put_back = self._write_back(inst, reason)
        if inst.put_back is Rollback.NOT_RESTORED:
            self.not_restored = True
        return inst.put_back

    def _slot_ours(self, inst: _Installed) -> bool:
        """The counter holds only what our routine could have left: zero, or a count
        no lower than the test saw. Else the game uses that place too: it is left
        as it is (zeroing it could break the game) and logged."""
        assert inst.slot is not None
        raw = self._client().read(self._game().pid, inst.slot, cv.SLOT_SIZE)
        count = struct.unpack_from("<Q", raw)[0]
        ours = raw == bytes(cv.SLOT_SIZE) or count >= max(1, inst.seen_count)
        if not ours:
            log.warning("action=build_slot_changed slot=%#x raw=%s", inst.slot, raw.hex())
        return ours

    def _write_back(self, inst: _Installed, reason: str) -> Rollback:
        try:
            game = self._game()
            client = self._client()
            for addr, orig, _new in reversed(inst.sites):
                client.write(game.pid, addr, orig)
            self.sleep(PUT_BACK_WAIT_S)  # a thread may still be inside a routine
            for addr, size in inst.rooms:
                if addr != inst.slot or self._slot_ours(inst):
                    client.write(game.pid, addr, bytes(size))
        except CheatMakerError as exc:
            log.warning(
                "action=build_rollback n=%d reason=%r restored=False error=%s",
                inst.n,
                reason,
                exc,
            )
            return Rollback.NOT_RESTORED
        notes.clear_note()
        log.info("action=build_rollback n=%d reason=%r restored=True", inst.n, reason)
        return Rollback.RESTORED

    def rollback_all(self, reason: str = "", *, wait: float | None = None) -> Rollback:
        """``rollback`` for the whole build (L12): the fix under test and every kept
        place, the newest first, in one run of writes with one wait before the rooms
        and counters are zeroed. Without kept places it is ``rollback``. After a put
        back the PS5 didn't take, kept places are left alone (nothing more is tried;
        their notes put them back on the next connect)."""
        if not self._op.acquire(timeout=-1 if wait is None else wait):
            log.warning("action=build_rollback reason=%r restored=False error=busy", reason)
            self.not_restored = True
            return Rollback.NOT_RESTORED
        try:
            if not self._kept or self.not_restored:
                return self.rollback(reason)
            with self._lock:
                inst, self._installed = self._installed, None
                if self._passed is inst:
                    self._passed = None
                kept, self._kept = self._kept, []
            self._pending = None
            items = ([inst] if inst is not None else []) + [p.inst for p in reversed(kept)]
            return self._write_back_many(items, reason)
        finally:
            self._op.release()

    def _write_back_many(self, items: list[_Installed], reason: str) -> Rollback:
        """``_write_back`` for several fixes: every site first, one wait, then rooms."""
        try:
            game = self._game()
            client = self._client()
            for inst in items:
                for addr, orig, _new in reversed(inst.sites):
                    client.write(game.pid, addr, orig)
            self.sleep(PUT_BACK_WAIT_S)  # a thread may still be inside a routine
            for inst in items:
                for addr, size in inst.rooms:
                    if addr != inst.slot or self._slot_ours(inst):
                        client.write(game.pid, addr, bytes(size))
        except CheatMakerError as exc:
            log.warning(
                "action=build_rollback places=%s reason=%r restored=False error=%s",
                ",".join(str(i.place) for i in items),
                reason,
                exc,
            )
            for inst in items:
                inst.put_back = Rollback.NOT_RESTORED
            self.not_restored = True
            return Rollback.NOT_RESTORED
        notes.clear_all()
        for inst in items:
            inst.put_back = Rollback.RESTORED
            log.info(
                "action=build_rollback n=%d place=%d reason=%r restored=True",
                inst.n,
                inst.place,
                reason,
            )
        return Rollback.RESTORED

    def log_end(self, hint: str) -> None:
        """B9: the result line for a build that ends without a cheat."""
        log.info(
            "action=build_result name=%r result=none tried=%d not_restored=%s hint=%r "
            "places=%d linked=%s",
            self.name,
            len(self.tried),
            self.not_restored,
            hint,
            self.place,
            self.linked_result,
        )

    def close(self, wait: float = CLOSE_WAIT_S) -> Rollback:
        """The dialog closes, Disconnect, or the app quits: nothing of ours stays.
        Stops the running step, waits for its writes in progress, then puts back.
        NOT_RESTORED whenever a put back of this build wasn't taken."""
        self.closed = True
        with self._lock:
            ev, inst = self._step_cancel, self._installed
        if ev is not None:
            ev.set()
        rb = self.rollback_all("closed", wait=wait)
        if rb is Rollback.NOTHING and inst is not None and inst.put_back is not None:
            rb = inst.put_back  # the worker saw the cancel and put it back first
        return Rollback.NOT_RESTORED if self.not_restored else rb

    # -- L1/L3: after a passed test ------------------------------------------------------
    def after_pass(self) -> LinkedPlan:
        """Judge every watched pool member over the passed test (no console). Done when
        no member that may be fixed fell behind: copies (L4) and members that couldn't
        be checked are listed, never fixed, and members that couldn't be read are
        unknown. After a linked place, members that now keep up are "not needed"."""
        inst = self._passed
        if inst is None:
            raise BuildFailed(NO_PASSED_FIX)
        if inst.place > 1:
            log.info("action=build_linked_result place=%d result=pass", inst.place)
        data = inst.pool
        self._last_pool = data
        self._behind, self._behind_copies = [], []
        if data is None:
            return LinkedPlan(True)
        judge = lk.judge_member if inst.place == 1 else lk.judge_later
        for m in data.not_watched:
            self.pool_rows[m.address] = lk.PoolRow(m, lk.Status.NOT_WATCHED)
        for m in (*self._watched_pool(), *self.copies):
            rs = data.readings.get(m.key)
            if rs is None:
                continue
            j = judge(m, rs, data.end_t)
            log.info(
                "action=build_linked_check n=%d member=%#x k=%.6g result=%s worst=%s",
                inst.place,
                m.address,
                m.factor,
                _STATUS_LOG[j.status],
                "none" if j.worst is None else f"{j.worst:.6g}",
            )
            self._judged(m, j.status, inst.place)
        if not self._behind:
            return LinkedPlan(True)
        return LinkedPlan(False, tuple(self._behind))

    def _judged(self, m: lk.PoolMember, status: lk.Status, place: int) -> None:
        """Update ``m``'s Advanced row and the fell-behind lists."""
        old = self.pool_rows.get(m.address)
        row = lk.PoolRow(m, status)
        behind = status is lk.Status.BEHIND
        if m.mark is rc.Verdict.COPY:
            row.done = lk.Done.COPY if behind or (old and old.done is lk.Done.COPY) else row.done
            if behind:
                self._behind_copies.append(m)
        elif m.mark is rc.Verdict.FAILED and behind:
            row.done = lk.Done.NOT_CHECKED
        elif behind:
            self._behind.append(m)
        elif old is not None and (old.status is lk.Status.BEHIND or old.after_place):
            # It fell behind before; a later place's fix made it keep up.
            row.done, row.after_place = lk.Done.NOT_NEEDED, old.after_place or place
        self.pool_rows[m.address] = row

    # -- L4 to L10: the next place -------------------------------------------------------
    def next_place(self, cancel: threading.Event | None = None) -> NextPlace:
        """After a pass with members behind: keep the current place in the game (its
        counter out, its note kept), check the unmarked ones with Check which is real
        (L4), sort them into clusters and start the best one's place (L5), within the
        limits (L10). Cancel puts back every place on this worker."""
        cancel = self._start_step(cancel)
        if self.not_restored:
            raise BuildFailed(RESTART)
        with self._linked_step(fail_place=False):
            if cancel.is_set():
                raise ScanCancelled("build cancelled")
            self._keep_place()
            behind = self._check_unmarked(self._behind, cancel)
            if cancel.is_set():
                raise ScanCancelled("build cancelled")
            readings = self._last_pool.readings if self._last_pool is not None else {}
            picks = lk.pick(lk.clusters([*behind, *self._behind_copies], readings))
            for m in [*behind, *self._behind_copies]:  # L4: never fixed, said why
                row = self.pool_rows.get(m.address)
                if row is not None and m.mark not in lk.FIXABLE:
                    row.member = m
                    row.done = lk.Done.COPY if m.mark is rc.Verdict.COPY else lk.Done.NOT_CHECKED
            if not picks:
                return NextPlace()
            rep, cluster = picks[0]
            self._hold, self._more_behind = rep, len(picks) > 1
            n = self.place + 1
            log.info(
                "action=build_linked_pick place=%d member=%#x mark=%s cluster=%d reason=%r",
                n,
                rep.address,
                "real" if rep.mark is rc.Verdict.REAL else "kept",
                len(cluster),
                _pick_reason(rep, cluster),
            )
            if self.place >= lk.MAX_PLACES:
                self._hold_ok = False  # holding it would be a place more than MAX_PLACES
                return NextPlace(fallback_reason=LIMIT)
            if self.tests_total >= lk.MAX_TESTS:
                return NextPlace(fallback_reason=LIMIT)  # held: still within MAX_PLACES
            self.places_total = min(lk.MAX_PLACES, self.place + len(picks))
            try:
                self._begin_place(rep)
            except PlaceFailed as exc:
                return NextPlace(fallback_reason=str(exc))
            return NextPlace(self.place, self.places_total, rep)

    def _check_unmarked(
        self, behind: Sequence[lk.PoolMember], cancel: threading.Event
    ) -> list[lk.PoolMember]:
        """L4: fell-behind members with no mark are checked as one group, with every
        kept fix in; the checker puts back what it wrote. Returns ``behind`` marked."""
        unmarked = [m for m in behind if m.mark is None]
        if not unmarked:
            return list(behind)
        game = self._game()
        checker = rc.RealChecker(
            self._client(), game.pid, [[m.target for m in unmarked]], wait=self.sleep
        )
        results = checker.run(cancel=cancel)
        self.real_results.update(results)
        marked: dict[int, lk.PoolMember] = {}
        for m in unmarked:
            r = results.get(m.key)
            mark = r.verdict if r is not None else rc.Verdict.FAILED
            marked[m.address] = replace(m, mark=mark)
            log.info("action=build_linked_real member=%#x verdict=%s", m.address, mark.name)
        self.pool = [marked.get(m.address, m) for m in self.pool]
        for addr, m in marked.items():
            if addr in self.pool_rows:
                self.pool_rows[addr].member = m
        return [marked.get(m.address, m) for m in behind]

    def _keep_place(self) -> None:
        """The current place passed: its saved routine in (counter out, B8 swap), its
        note moved to the kept list; its rooms, slot and writers are never reused."""
        with self._op:
            inst = self._passed
            if inst is None or self._installed is not inst:
                raise BuildFailed(NO_PASSED_FIX)
            saved = self._keep(inst)
            notes.keep_note()
            with self._lock:
                self._installed = self._passed = None
            self._kept.append(self._place_record(inst, saved))
        base = self._game().module_base
        for room in inst.hooks:
            self._kept_reserved.append(
                (room.test_room - base, room.test_room + 2 * cv.CAVE_ROOM - base)
            )
        if inst.slot is not None:
            self._kept_reserved.append((inst.slot - base, inst.slot + cv.SLOT_SIZE - base))
        self._fixed_sites.update(p.writer.address for p in inst.candidate.parts)
        log.info("action=build_linked_keep place=%d n=%d", self.place, inst.n)

    def _place_record(self, inst: _Installed, saved: list[tuple[_Room, bytes, bytes]]) -> _Place:
        if self.place_member is not None and self.place_member.address in self.pool_rows:
            self.pool_rows[self.place_member.address].done = lk.Done.FIXED
        return _Place(
            n=self.place,
            address=self.address,
            vtype=self.vtype,
            full=self.full,
            member=self.place_member,
            inst=inst,
            saved=saved,
            filter=self.filter if self.only_me else None,
            by_class=self.by_class,
            player_obj=self._player_obj,
            other_objs=list(self._other_objs),
            skipped=list(self.skipped),
        )

    def _begin_place(self, m: lk.PoolMember) -> None:
        """Make ``m`` the current place: every per-place field starts fresh, so watch,
        collect and try_candidate work on it unchanged."""
        self.place += 1
        self.place_member = m
        self.address, self.vtype = m.address, m.vtype
        self.full = self.pool_full.get(m.address) if self.effect is Effect.ALWAYS_FULL else None
        self.writers, self.eligible, self.skipped, self.queue = [], [], [], []
        self._place_tried = len(self.tried)
        self.filter, self.by_class, self.others, self.collect_tries = None, False, [], 0
        self._player_obj, self._other_objs = None, []
        self._repeat, self._repeated, self._answers, self._pending = None, set(), {}, None
        self._place_start = None
        if m.vtype not in cv.EFFECT_TYPES:
            raise PlaceFailed(cv.UNSUPPORTED_TYPE)

    # -- B8: the result -------------------------------------------------------------------
    def finalize(self, cancel: threading.Event | None = None) -> BuildResult:
        """Swap the passed fix to the routine without the counter and make the cheat.
        The new routine goes into the second room first, then the jump is re-aimed
        with one write; after 2 s the test room and the counter are zeroed. All of it
        is one run of writes: close() on another thread waits for it to end."""
        cancel = self._start_step(cancel)
        if self._kept:
            return self._finalize_places(cancel)
        with self._op:
            inst = self._passed
            if inst is None or self._installed is not inst:
                raise BuildFailed(NO_PASSED_FIX)
            if cancel.is_set():
                if self._end(inst, "cancelled") is Rollback.NOT_RESTORED:
                    raise BuildFailed(RESTART)
                raise ScanCancelled("build cancelled")
            saved = self._keep(inst)
            with self._lock:
                self._installed = self._passed = None
        notes.clear_note()
        base = self._game().module_base
        full = self.vtype.encode(self.full) if self.full is not None else None
        cheat = self._cheat(inst, saved, base, full)
        cheat.check_after_restart = self.only_me and not self.by_class
        if self.live is not None:
            self.live.mark_on(cheat)  # its bytes are in already: nothing more written
        else:
            cheat.enabled = True
        after = "Check after restart" if self.only_me and not self.by_class else "Yes"
        log.info(
            "action=build_result name=%r method=%s lines=%d after_restart=%r places=%d linked=%s",
            cheat.name,
            _method_name(inst.candidate),
            len(cheat.locations),
            after,
            self.place,
            self.linked_result,
        )
        # A second place the game keeps (spec-linked-values, replacing B7) is found by
        # after_pass before this runs.
        return BuildResult(cheat, after, tuple(self.skipped))

    def _finalize_places(self, cancel: threading.Event) -> BuildResult:
        """L13: every place fixed in code. The current place (if it passed) is kept as
        place 1 was; the cheat holds every place's lines, places in order. Under the
        write lock, so close() on another thread either puts everything back first or
        finds nothing left (the cheat is on)."""
        with self._op:
            if cancel.is_set() or self.closed:
                self._cancelled_all()
            inst = self._passed
            if inst is not None:
                if self._installed is not inst:
                    raise BuildFailed(NO_PASSED_FIX)
                saved = self._keep(inst)
                with self._lock:
                    self._installed = self._passed = None
                self._kept.append(self._place_record(inst, saved))
            places = list(self._kept)
            cheat = self._places_cheat(places)
            if self.live is not None:
                self.live.mark_on(cheat)  # every byte is in already
            else:
                cheat.enabled = True
            self._done_places()
        self.linked_result = "fixed" if len(places) > 1 else "none"
        after = "Check after restart" if any(p.by_field for p in places) else "Yes"
        return self._result(cheat, after, places)

    def fallback(self, cancel: threading.Event | None = None, reason: str = "") -> BuildResult:
        """L11: the current linked place can't be fixed in code. Never goes down and
        Always full keep the passed places and hold the member by a freeze (one combined
        cheat, this session only). Half damage, a member past MAX_PLACES (L10) or one
        that can't be read keep the passed places only (``linked=partial``)."""
        cancel = self._start_step(cancel)
        if self.not_restored:
            raise BuildFailed(RESTART)
        m = self._hold or self.place_member
        reason = reason or (LIMIT if self.limit_hit else NOTHING_WORKED)
        with self._op:
            if cancel.is_set() or self.closed:
                self._cancelled_all()
            if self._installed is not None:  # never in normal flow: nothing half-tested stays
                self.rollback("fallback")
            places = list(self._kept)
            if not places:
                raise BuildFailed(NO_PASSED_FIX)
            n = len(places) + 1
            code = self._places_cheat(places)
            value = None
            if (
                m is not None
                and self._hold_ok
                and self.effect is not Effect.HALF
                and n <= lk.MAX_PLACES
            ):
                value = self._hold_value(m)
            if m is not None and value is not None:
                cheat = self._hold_cheat(code, m, n, value)
                after = SESSION_ONLY
                if self.live is not None:
                    self.live.turn_on(cheat)  # the code lines as they are, and the freeze
                else:
                    cheat.enabled = True
                self.linked_result = "partial" if self._more_behind else "freeze"
                self._row_done(m, lk.Done.FROZEN, reason)
                result = "fallback_freeze"
            else:
                cheat = code
                after = "Check after restart" if any(p.by_field for p in places) else "Yes"
                if self.live is not None:
                    self.live.mark_on(cheat)
                else:
                    cheat.enabled = True
                self.linked_result = "partial"
                if m is not None:
                    self._row_done(m, lk.Done.NOT_FIXED, reason)
                result = "limit" if reason == LIMIT else "fail"
            log.info("action=build_linked_result place=%d result=%s reason=%r", n, result, reason)
            self._done_places()
        return self._result(cheat, after, places, held=value is not None, reason=reason)

    def _row_done(self, m: lk.PoolMember, done: lk.Done, reason: str) -> None:
        row = self.pool_rows.get(m.address)
        if row is not None:
            row.done, row.reason = done, reason

    def _hold_value(self, m: lk.PoolMember) -> float | int | None:
        """What the freeze holds (L11): Always full its Full; Never goes down its value
        when its last test (or watch) started, else now. None when it can't be read."""
        if self.effect is Effect.ALWAYS_FULL and m.address in self.pool_full:
            return self.pool_full[m.address]
        if m is self.place_member and self._place_start is not None:
            return self._place_start
        game = self._game()
        try:
            return m.vtype.decode(self._client().read(game.pid, m.address, m.vtype.size))
        except ReadFailed as exc:
            log.warning("action=build_linked_hold member=%#x unreadable=%s", m.address, exc)
            return None

    def _hold_cheat(self, code: Cheat, m: lk.PoolMember, n: int, value: float | int) -> Cheat:
        """The kept places' code lines plus a freeze on ``m`` (L13 with a freeze)."""
        data = m.vtype.encode(value)
        for loc in code.locations:
            loc.part = self.name
        hold = Cheat(
            f"{self.name} (place {n})",
            [Location(LocationKind.SESSION_ADDRESS, m.address, data, data, vtype=m.vtype)],
            freeze=True,
            value_type=m.vtype,
        )
        cheat = combine([code, hold], name=code.name)
        cheat.places = n
        return cheat

    def _cancelled_all(self) -> None:
        """Cancel landed on a final step: every place goes back, then ScanCancelled."""
        if self.rollback_all("cancelled") is Rollback.NOT_RESTORED:
            raise BuildFailed(RESTART)
        raise ScanCancelled("build cancelled")

    def _done_places(self) -> None:
        """The kept places are the result now: nothing of them is put back any more."""
        with self._lock:
            self._kept = []
        notes.clear_all()

    def _result(
        self,
        cheat: Cheat,
        after: str,
        places: list[_Place],
        held: bool = False,
        reason: str = "",
    ) -> BuildResult:
        skipped = [s for p in places for s in p.skipped]
        if places[-1].n != self.place:  # the current place failed (L11): its skips too
            skipped += self.skipped
        total = len(places) + (1 if held else 0)
        log.info(
            "action=build_result name=%r method=%s lines=%d after_restart=%r places=%d linked=%s",
            cheat.name,
            ",".join(_method_name(p.inst.candidate) for p in places),
            len(cheat.locations),
            after,
            total,
            self.linked_result,
        )
        return BuildResult(cheat, after, tuple(skipped), total, self.linked_result, reason)

    def _places_cheat(self, places: list[_Place]) -> Cheat:
        """One code cheat for the kept places: exactly ``_cheat`` for one place."""
        base = self._game().module_base
        if len(places) == 1:
            p = places[0]
            full = p.vtype.encode(p.full) if p.full is not None else None
            cheat = self._cheat(p.inst, p.saved, base, full, p)
        else:
            lines = [loc for p in places for loc in self._place_lines(p.inst, p.saved, base)]
            cheat = Cheat(
                name=self._result_name(),
                locations=lines,
                template="lock",
                hint=self._result_hint(),
                places=len(places),
            )
        cheat.check_after_restart = any(p.by_field for p in places)
        return cheat

    def _keep(self, inst: _Installed) -> list[tuple[_Room, bytes, bytes]]:
        """The writes of finalize, under the write lock. On any failure the fix is put
        back and BuildFailed says how that went."""
        game = self._game()
        client = self._client()
        full = self.vtype.encode(self.full) if self.full is not None else None
        try:
            saved: list[tuple[_Room, bytes, bytes]] = []
            for room in inst.hooks:
                code = self._saved_code(room, full)
                jmp = cv.jmp_patch(
                    room.part.writer.address, room.saved_room, room.part.writer.length
                )
                saved.append((room, code, jmp))
            # The note names the re-aimed jumps first: if the app dies in between, the
            # next connect finds a site that is neither and leaves the routines alone.
            kept = {room.part.writer.address: jmp for room, _c, jmp in saved}
            sites = [(a, o, kept.get(a, new)) for a, o, new in inst.sites]
            notes.open_note(self._note(inst, sites))
            for room, code, jmp in saved:
                client.write(game.pid, room.saved_room, code)
                client.write(game.pid, room.part.writer.address, jmp)
            self.sleep(PUT_BACK_WAIT_S)
            if self._installed is not inst:  # put back on this thread meanwhile
                if inst.put_back is Rollback.NOT_RESTORED:
                    raise BuildFailed(RESTART)
                raise BuildFailed(CLOSED_WHILE_KEEPING)
            for room in inst.hooks:
                client.write(game.pid, room.test_room, bytes(len(room.code)))
            if inst.slot is not None and self._slot_ours(inst):
                client.write(game.pid, inst.slot, bytes(cv.SLOT_SIZE))
        except CheatMakerError as exc:
            if isinstance(exc, BuildFailed):
                raise
            if self._end(inst, "finalize failed") is Rollback.NOT_RESTORED:
                raise BuildFailed(RESTART) from exc
            raise BuildFailed(f"Couldn't keep the fix: {exc}. Your game is unchanged.") from exc
        return saved

    def _saved_code(self, room: _Room, full: bytes | None, place: _Place | None = None) -> bytes:
        """The routine kept in the cheat: the test one without the counter lines.
        ``place``: a kept place's (its type and filter), else the current place's."""
        vtype = self.vtype if place is None else place.vtype
        only_me = self.filter if self.only_me else None
        if place is not None:
            only_me = place.filter
        return cv.effect_code(
            room.span,
            room.target,
            vtype,
            self.effect,
            room.saved_room,
            full=full,
            counter=None,
            only_me=only_me,
            flags_fix=room.part.flags_fix,
        )

    def _place_lines(
        self, inst: _Installed, saved: list[tuple[_Room, bytes, bytes]], base: int
    ) -> list[Location]:
        """A place's lines: a routine line and a jump line per hooked writer, then the
        switch-off lines."""
        lines: list[Location] = []
        for room, code, jmp in saved:
            w = room.part.writer
            # The routine line is the same on and off (as anchor_hook.hook_cheat): a game
            # thread may be inside it when the jump goes back.
            lines.append(Location(LocationKind.CODE_PATCH, room.saved_room - base, code, code))
            lines.append(Location(LocationKind.CODE_PATCH, w.address - base, jmp, w.code))
        for part in inst.candidate.parts:
            if part.method is Method.NOP:
                w = part.writer
                lines.append(
                    Location(
                        LocationKind.CODE_PATCH, w.address - base, bytes([NOP]) * w.length, w.code
                    )
                )
        return lines

    def _result_name(self) -> str:
        return f"{self.name}: {self.effect.value}" + (" (only me)" if self.only_me else "")

    def _result_hint(self) -> str:
        return f"{self.effect.value}" + (" for you only." if self.only_me else ".")

    def _cheat(
        self,
        inst: _Installed,
        saved: list[tuple[_Room, bytes, bytes]],
        base: int,
        full: bytes | None,
        place: _Place | None = None,
    ) -> Cheat:
        lines = self._place_lines(inst, saved, base)
        vtype = self.vtype if place is None else place.vtype
        value_at = None
        if self.effect is Effect.ALWAYS_FULL and full is not None and saved:

            def build(v: bytes) -> list[bytes]:
                out = []
                for room, _code, jmp in saved:
                    out += [self._saved_code(room, v, place), jmp]
                return out + [loc.on for loc in lines[2 * len(saved) :]]

            value_at = value_positions(build, vtype.size)
        return Cheat(
            name=self._result_name(),
            locations=lines,
            template="lock",
            hint=self._result_hint(),
            value_type=vtype if value_at is not None else None,
            value_at=value_at,
        )


class _AliveCheck:
    """B5 Alive: the PS5 answers a read of our patch within 2 s and it's still there,
    and the game is still the app in front (or, if the PS5 can't say, still listed)."""

    def __init__(self, b: Builder, inst: _Installed) -> None:
        self.b, self.inst = b, inst
        self.next_at = b.clock() + ALIVE_EVERY_S
        self.use_processes = False
        self.next_processes = 0.0

    def check(self) -> tuple[bool, bool]:
        """(alive, the game is gone)."""
        b = self.b
        now = b.clock()
        if now < self.next_at:
            return True, False
        self.next_at = now + ALIVE_EVERY_S
        game = b._game()
        client = b._client()
        addr, _orig, new = self.inst.sites[0]
        before = b.clock()
        held = client.read(game.pid, addr, len(new)) == new
        if b.clock() - before > ALIVE_ANSWER_S:
            log.warning("action=build_alive slow_read=%.1fs", b.clock() - before)
            return False, False
        if not held:
            log.warning("action=build_alive patch_gone site=%#x", addr)
            return False, False
        if not self.use_processes:
            try:
                fg = client.foreground_app()
            except (NotImplementedError, ProtocolError):
                self.use_processes = True
            else:
                if fg is None or fg.pid != game.pid or fg.title_id.upper() != game.title_id.upper():
                    # Put back, but only a game no longer listed counts as a crash: the
                    # player may just have pressed the PS button.
                    gone = b._game_gone()
                    log.warning("action=build_alive game_not_in_front gone=%s", gone)
                    return False, gone
                return True, False
        if now >= self.next_processes:
            self.next_processes = now + PROCESSES_EVERY_S
            if not any(p.pid == game.pid for p in client.processes()):
                log.warning("action=build_alive game_gone")
                return False, True
        return True, False


class _PoolWatch:
    """L3: the pool members read during one test, about once a second each, in 4 KiB
    spans. A span is due POOL_EVERY_S after its last read (the first reads staggered
    over the first second); a tick reads the spans due by then (or within
    POOL_EARLY_S), oldest first, at most MAX_SPAN_READS_PER_TICK. Ticks don't fall on
    due times, so a span that would be read more than ONCE_A_SECOND_S after its last
    read if left to the next tick is read now instead, past the limit (a large pool
    at real latency). A member that can't
    be read gets no value (it may be unknown at the end); after 2 such reads it isn't
    read again in this test. Only a lost link leaves the tick (the test's Lost path).

    In a later place's test the sample is that place's value, so place 1 (the
    reference) is read with the pool: in a member's span when it is within 4 KiB, else
    in a read of its own just before, which doesn't count against the tick's limit.
    There every span is also read once as the test starts (``baseline``)."""

    def __init__(self, b: Builder) -> None:
        self.b = b
        watched = b._watched_pool()
        members = [*watched, *b.copies]
        self.ref = b._place1 if b.place > 1 else None
        self.ref_key = None if self.ref is None else (self.ref.address, self.ref.vtype)
        self.ref_value: float | int | None = None
        self.spans = lk.plan_spans(watched, b.copies, self.ref)
        self.home = (  # the reference's span
            None
            if self.ref is None
            else next(i for i, ts in enumerate(self.spans) if self.ref in ts)
        )
        self.ref_only = self.home is not None and self.spans[self.home] == [self.ref]
        read = {(t.address, t.vtype) for s in self.spans for t in s} - {self.ref_key}
        self.data = lk.PoolData(
            readings={k: [] for k in read},
            not_watched=[m for m in members if m.key not in read],
            spans=len(self.spans),
        )
        now, n = b.clock(), max(1, len(self.spans))
        self.due = [now + i * lk.POOL_EVERY_S / n for i in range(len(self.spans))]
        self.last_read: list[float | None] = [None] * len(self.spans)
        self.fails: dict[rc.Key, int] = {}
        self.last_cost = 0.0  # how long the last tick's reads took
        self.t0 = now
        self.last_tick: float | None = None
        self.sample_s = 0.0  # what the tick's own reads (sample, alive) took, as last seen

    def tick(self, ref: float | int | None) -> None:
        """After the tick's sample: read the spans that are due. ``ref`` is place 1's
        value in that sample (None when it was dropped)."""
        b = self.b
        start = b.clock()
        if self.last_tick is not None:  # a tick is: reads, sleep(POLL_S - reads), sample
            self.sample_s = max(0.0, start - self.last_tick - max(POLL_S, self.last_cost))
        self.last_tick = start
        members = [
            i
            for i in range(len(self.spans))
            if self.spans[i] and not (self.ref_only and i == self.home)
        ]
        # A span that would pass its once-a-second mark before the next tick must be read
        # now (ticks don't fall on due times), even past the limit; the limit is filled
        # up with the spans that are due, oldest first.
        per_read = self.data.read_s / self.data.reads if self.data.reads else 0.0
        reads = max(POLL_S, lk.MAX_SPAN_READS_PER_TICK * per_read)
        nxt = start + reads + self.sample_s + lk.NEXT_TICK_SLACK_S  # the next tick's reads
        must = [
            i
            for _l, i in sorted((self._last(i), i) for i in members)
            if _l + lk.ONCE_A_SECOND_S <= nxt
        ]
        due = sorted(
            (self.due[i], i)
            for i in members
            if i not in must and self.due[i] <= start + lk.POOL_EARLY_S
        )
        picked = must or [i for _d, i in due[: lk.MAX_SPAN_READS_PER_TICK]]
        if self.home is not None and picked:  # the reference first, then the members
            picked = [self.home, *(i for i in picked if i != self.home)]
        for i in picked:
            t = b.clock()
            last = self.last_read[i]
            if last is not None and t - last > lk.ONCE_A_SECOND_S:
                self.data.late += 1
            self._read(i, ref)
            self.last_read[i] = t
            self.due[i] = t + lk.POOL_EVERY_S
        self.last_cost = b.clock() - start
        self.data.read_s += self.last_cost

    def _last(self, i: int) -> float:
        last = self.last_read[i]
        return self.t0 if last is None else last

    def baseline(self) -> None:
        """Every span once, the reference first, right after the test's start value:
        what each member was against place 1 as this place's test began."""
        order = list(range(len(self.spans)))
        if self.home is not None:
            order = [self.home, *(i for i in order if i != self.home)]
        for i in order:
            if self.spans[i]:
                self._read(i, None)

    def _read(self, i: int, ref: float | int | None) -> None:
        b = self.b
        ts = self.spans[i]
        t = b.clock()
        got = rc.read_span(b._client(), b._game().pid, ts)
        self.data.reads += 1
        if self.ref_key is not None and self.ref in ts:
            raw = got.get(self.ref_key)
            ref = self.ref_value = None if raw is None else self.ref.vtype.decode(raw)
        elif self.ref_key is not None:
            ref = self.ref_value
        for target in ts:
            key = (target.address, target.vtype)
            if key == self.ref_key:
                continue
            raw = got.get(key)
            if raw is None:
                self.fails[key] = self.fails.get(key, 0) + 1
            value = None if raw is None else target.vtype.decode(raw)
            self.data.readings[key].append(lk.Reading(t, ref, value))
        self.spans[i] = [
            x
            for x in ts
            if x == self.ref or self.fails.get((x.address, x.vtype), 0) < 2  # keep the reference
        ]

    def finish(self) -> lk.PoolData:
        d = self.data
        d.end_t = self.b.clock()
        log.info(
            "action=build_linked_reads place=%d spans=%d reads=%d avg_ms=%.0f late=%d",
            self.b.place,
            d.spans,
            d.reads,
            1000 * d.read_s / d.reads if d.reads else 0.0,
            d.late,
        )
        return d


def _pick_reason(rep: lk.PoolMember, cluster: Sequence[lk.PoolMember]) -> str:
    why = "marked real" if rep.mark is rc.Verdict.REAL else "kept the test value"
    if rep.likely:
        why += ", likely real"
    return f"{why}, {len(cluster)} in its cluster"


def _worse(a: Rollback, b: Rollback) -> Rollback:
    """Of two put backs, the one to tell the player about."""
    for rb in (Rollback.NOT_RESTORED, Rollback.RESTORED):
        if rb in (a, b):
            return rb
    return Rollback.NOTHING


def _method_name(c: Candidate) -> str:
    return c.method.value + (":all" if len(c.parts) > 1 else "")


__all__ = [
    "MAX_CANDIDATES",
    "Answer",
    "BuildResult",
    "Builder",
    "Candidate",
    "Eligible",
    "Judgement",
    "LinkedPlan",
    "Method",
    "NextPlace",
    "Outcome",
    "Part",
    "PlaceFailed",
    "Question",
    "Rollback",
    "Sample",
    "Skipped",
    "TestData",
    "TestOutcome",
    "TryResult",
    "candidates",
    "classify",
    "judge",
]
