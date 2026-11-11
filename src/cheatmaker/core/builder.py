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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

import iced_x86 as ix

from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import build_notes as notes
from cheatmaker.core import cave as cv
from cheatmaker.core import real_check as rc
from cheatmaker.core.cave import Effect
from cheatmaker.core.cheats import Cheat, Location, LocationKind, value_positions
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
        if any(s.value < d.start - tol for s in d.samples if _finite(s.value)):
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


def _finite(v) -> bool:
    return not isinstance(v, float) or math.isfinite(v)


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
    after_restart: str  # "Yes" or "Check after restart" (B8)
    skipped: tuple[Skipped, ...] = ()


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


class Builder:
    """Make it work for one value. ``sleep`` and ``clock`` are injected for tests.

    ``release`` stops the app's own freeze on the value (else the watch only sees our
    writes). ``extra_reserved``: module-offset rooms of open cheats' code lines that
    a build must never reuse."""

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
            self.queue = [c for c in candidates(keep) if not self._all_crashed(c)]

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
            self.session, cv.CAVE_ROOM, skip=cv.BUILD_CAVE_SKIP, extra_reserved=self.extra_reserved
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
        )
        failed = self._install(inst, cancel)
        if failed is not None:
            if failed.rollback is Rollback.NOT_RESTORED:
                raise BuildFailed(RESTART)
            if failed.result is TryResult.CANCELLED:
                raise ScanCancelled("collecting cancelled")
            raise BuildFailed(CANT_TELL_APART)
        try:
            waited = 0.0
            while waited < COLLECT_S:
                if cancel.is_set() or self._taken_back(inst) is not None:
                    raise ScanCancelled("collecting cancelled")
                self.sleep(POLL_S)
                waited += POLL_S
            raw = read(table, cv.TABLE_SIZE)  # entries added after this are only lost
        finally:
            rb = self._end(inst, "collected")
        if rb is Rollback.NOT_RESTORED:
            raise BuildFailed(RESTART)
        objs = sorted({v for (v,) in struct.iter_unpack("<Q", raw) if v})
        log.info("action=player_only_collect objects=%d", len(objs))
        return objs

    # -- B4/B5: one fix at a time -------------------------------------------------------
    def next_candidate(self) -> Candidate | None:
        """The fix to try next: the same one once more after "you didn't get hit"."""
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
        before = {id(t) for t, _o in self.tried} - {id(c)}
        n, total = len(before) + 1, len(before) + 1 + len(self.queue)
        self._answers = {}
        try:
            inst = self._prepare(c, n)
            inst.total = total
        except (LockFailed, ReadFailed) as exc:  # S6: nothing was written
            return self._done(c, TestOutcome(TryResult.FAIL, str(exc)))
        keep = False
        try:
            outcome = self._install_and_test(inst, cancel, progress)
            keep = outcome.result in (TryResult.PASS, TryResult.NEEDS_ANSWER)
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
                extra_reserved=self.extra_reserved,
            )
            slot = cv.find_slot(self.session)
        full = self.vtype.encode(self.full) if self.full is not None else None
        inst = _Installed(c, n, sites, [], [], slot=slot)
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
        nop = all(p.method is Method.NOP for p in inst.candidate.parts)
        others_start: dict[int, float | int] = {}
        try:
            start_value = self._read_value()
            for o in self.others:
                try:
                    others_start[o] = self._read_value(o)
                except ReadFailed:
                    continue
        except (ConnectionLost, ReadFailed):
            return self._lost(inst, game_gone=False)
        if progress is not None:
            progress(0.0, hint)
        samples = [Sample(start_value)]
        others_down = False
        alive = _AliveCheck(self, inst)
        t0 = self.clock()
        while self.clock() - t0 < TEST_S:
            if cancel.is_set():
                return TestOutcome(TryResult.CANCELLED, rollback=self._end(inst, "cancelled"))
            self.sleep(POLL_S)
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

    def log_end(self, hint: str) -> None:
        """B9: the result line for a build that ends without a cheat."""
        log.info(
            "action=build_result name=%r result=none tried=%d not_restored=%s hint=%r",
            self.name,
            len(self.tried),
            self.not_restored,
            hint,
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
        rb = self.rollback("closed", wait=wait)
        if rb is Rollback.NOTHING and inst is not None and inst.put_back is not None:
            rb = inst.put_back  # the worker saw the cancel and put it back first
        return Rollback.NOT_RESTORED if self.not_restored else rb

    # -- B8: the result -------------------------------------------------------------------
    def finalize(self, cancel: threading.Event | None = None) -> BuildResult:
        """Swap the passed fix to the routine without the counter and make the cheat.
        The new routine goes into the second room first, then the jump is re-aimed
        with one write; after 2 s the test room and the counter are zeroed. All of it
        is one run of writes: close() on another thread waits for it to end."""
        cancel = self._start_step(cancel)
        with self._op:
            inst = self._passed
            if inst is None or self._installed is not inst:
                raise BuildFailed("There's no passed fix to keep.")
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
            "action=build_result name=%r method=%s lines=%d after_restart=%r",
            cheat.name,
            _method_name(inst.candidate),
            len(cheat.locations),
            after,
        )
        # B7 (a second address that went down while this one held) isn't built in this
        # version; see "Not in this version" in docs/spec-cheat-builder.md.
        return BuildResult(cheat, after, tuple(self.skipped))

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

    def _saved_code(self, room: _Room, full: bytes | None) -> bytes:
        """The routine kept in the cheat: the test one without the counter lines."""
        return cv.effect_code(
            room.span,
            room.target,
            self.vtype,
            self.effect,
            room.saved_room,
            full=full,
            counter=None,
            only_me=self.filter if self.only_me else None,
            flags_fix=room.part.flags_fix,
        )

    def _cheat(
        self,
        inst: _Installed,
        saved: list[tuple[_Room, bytes, bytes]],
        base: int,
        full: bytes | None,
    ) -> Cheat:
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
        value_at = None
        if self.effect is Effect.ALWAYS_FULL and full is not None and saved:

            def build(v: bytes) -> list[bytes]:
                out = []
                for room, _code, jmp in saved:
                    out += [self._saved_code(room, v), jmp]
                return out + [loc.on for loc in lines[2 * len(saved) :]]

            value_at = value_positions(build, self.vtype.size)
        name = f"{self.name}: {self.effect.value}" + (" (only me)" if self.only_me else "")
        return Cheat(
            name=name,
            locations=lines,
            template="lock",
            hint=f"{self.effect.value}" + (" for you only." if self.only_me else "."),
            value_type=self.vtype if value_at is not None else None,
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
    "Method",
    "Outcome",
    "Part",
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
