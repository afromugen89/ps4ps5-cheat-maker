"""The smart cheat builder, "Make it work" (docs/spec-cheat-builder.md).

The pure part: which writers may be patched and how (``classify``, rules S1 to S4 and
S7), in what order fixes are tried (``candidates``), and whether a test passed
(``judge``, the B5 rules). No console here, so all of it is tested directly.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

import iced_x86 as ix

from cheatmaker.core import cave as cv
from cheatmaker.core.cave import Effect
from cheatmaker.core.locker import FLAGS_UNSAFE, Writer, decode_one, writes_memory
from cheatmaker.core.values import ValueType
from cheatmaker.errors import LockFailed

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
CRASHED = "it crashed the game last time"
NO_METHOD = "no safe way to change it with this effect"
TOO_SHORT_ONLY_ME = "it's too short for a jump, and Only me needs one"


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


def _hookable(writer: Writer) -> bool:
    """A 5-byte jump fits over the writer alone, and it writes through ``[reg+disp]``."""
    if writer.length < cv.JMP_LEN:
        return False
    try:
        cv.writer_target(writer)
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
        if _hookable(w):
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


__all__ = [
    "MAX_CANDIDATES",
    "Answer",
    "Candidate",
    "Eligible",
    "Judgement",
    "Method",
    "Outcome",
    "Part",
    "Question",
    "Sample",
    "Skipped",
    "TestData",
    "candidates",
    "classify",
    "judge",
]
