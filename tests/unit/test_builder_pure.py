"""builder.classify / candidates / judge: the safety rules, the order of fixes and the
pass rules (spec-cheat-builder B3 to B5)."""

from __future__ import annotations

import pytest

from cheatmaker.core import builder as b
from cheatmaker.core.cave import Effect
from cheatmaker.core.locker import FLAGS_UNSAFE, FlagsUse, Writer
from cheatmaker.core.values import ValueType
from tests import fakegame as fg

NONE = FlagsUse()
VM = Writer(fg.WRITER, fg.VMOVSS_W, "vmovss [r12+2ACh],xmm1", 5, flags=NONE)
SHORT = Writer(0x400200, bytes.fromhex("894808"), "mov [rax+8],ecx", 3, flags=NONE)
SUBW = Writer(
    fg.SUB_SITE,
    fg.SUB_W,
    "sub [rax+2B0h],ecx",
    7,
    unsafe=FLAGS_UNSAFE,
    flags=FlagsUse(frozenset({"ZF", "SF", "OF"})),
)
INDEXED = Writer(0x400300, bytes.fromhex("898C88AC020000"), "mov [rax+rcx*4+2ACh],ecx", 2)
INT32 = ValueType.INT32
FLOAT = ValueType.FLOAT


def _w(code: str, **kw) -> Writer:
    return Writer(0x400400, bytes.fromhex(code), code, kw.pop("hits", 1), **kw)


def methods(writer, vtype=INT32, effect=Effect.NEVER_DOWN, only_me=False, **kw):
    eligible, skipped = b.classify([writer], vtype, effect, only_me, **kw)
    if skipped:
        return skipped[0].reason
    return eligible[0].methods


@pytest.mark.parametrize(
    ("writer", "vtype", "reason"),
    [
        (_w("8908", shared=True, flags=NONE), INT32, b.SHARED),  # S1
        (
            _w("f00fc14808", unsafe="it's an atomic update", flags=NONE),
            INT32,
            "it's an atomic update",
        ),
        (_w("48894808", flags=NONE), INT32, b.WRONG_SIZE),  # S4: 8-byte store, 4-byte value
        (_w("8b4008", flags=NONE), INT32, b.MANY_WRITES),  # a reader: writes no memory
        (
            _w("294808", unsafe=FLAGS_UNSAFE, flags=FlagsUse(frozenset({"CF"}))),
            INT32,
            b.FLAGS_CARRY,
        ),
        (_w("294808", unsafe=FLAGS_UNSAFE, flags=FlagsUse(unknown=True)), INT32, b.FLAGS_UNKNOWN),
        (  # inc [m] ; jbe: ZF is the inc's, CF from before it (review 5)
            _w(
                "ff80b0020000",
                unsafe=FLAGS_UNSAFE,
                flags=FlagsUse(frozenset({"ZF"}), before=frozenset({"CF"})),
            ),
            INT32,
            b.FLAGS_BEFORE,
        ),
        (_w("2988B0020000"), INT32, b.FLAGS_UNKNOWN),  # changes flags, never checked
    ],
)
def test_skipped_by_every_method(writer, vtype, reason):
    for effect in Effect:
        assert methods(writer, vtype, effect) == reason


def test_methods_per_effect():
    assert methods(VM, FLOAT) == (b.Method.HOOK,)
    assert methods(SHORT) == (b.Method.NOP,), "too short for a jump: switched off"
    assert methods(INDEXED) == (b.Method.NOP,), "[reg+reg*4]: no object to hook"
    assert methods(SHORT, effect=Effect.HALF) == b.NO_METHOD
    assert methods(VM, FLOAT, Effect.ALWAYS_FULL) == (b.Method.HOOK,)
    assert methods(VM, FLOAT, Effect.ALWAYS_FULL, now_full=True) == (b.Method.HOOK, b.Method.NOP)
    assert methods(SHORT, effect=Effect.ALWAYS_FULL) == b.NO_METHOD
    assert methods(SHORT, effect=Effect.ALWAYS_FULL, now_full=True) == (b.Method.NOP,)
    assert methods(VM, FLOAT, Effect.HALF) == (b.Method.HOOK,)


def test_a_writer_the_routine_refuses_falls_back_to_the_switch_off():
    """mov [rax+100h],rsp: effect_code refuses writers that use the stack. classify
    knows it up front, so Never goes down still gets the switch-off (review 14)."""
    rsp = _w("4889a000010000", flags=NONE)
    assert methods(rsp, ValueType.INT64) == (b.Method.NOP,)
    assert methods(rsp, ValueType.INT64, Effect.HALF) == b.NO_METHOD
    assert methods(rsp, ValueType.INT64, only_me=True) == b.NO_METHOD


def test_flags_the_hook_can_fix():
    eligible, _ = b.classify([SUBW], INT32, Effect.NEVER_DOWN, False)
    assert eligible[0].methods == (b.Method.HOOK,) and eligible[0].flags_fix
    eligible, _ = b.classify([SUBW], INT32, Effect.ALWAYS_FULL, False, now_full=True)
    assert eligible[0].methods == (b.Method.HOOK,), "S3: never switched off"


def test_only_me_never_switches_off():
    assert methods(VM, FLOAT, only_me=True) == (b.Method.HOOK,)
    assert methods(SHORT, only_me=True) == b.TOO_SHORT_ONLY_ME
    assert methods(VM, FLOAT, Effect.ALWAYS_FULL, True, now_full=True) == (b.Method.HOOK,)


def test_crashed_methods_are_left_out():  # S7
    hook_crashed = lambda w, m: m is b.Method.HOOK  # noqa: E731
    assert methods(VM, FLOAT, crashed=hook_crashed) == b.CRASHED
    assert methods(VM, FLOAT, Effect.ALWAYS_FULL, crashed=hook_crashed, now_full=True) == (
        b.Method.NOP,
    )
    assert methods(SHORT, crashed=lambda w, m: True) == b.CRASHED


def test_candidates_order_all_and_cap():
    a = b.Eligible(Writer(1, b"", "a", 2), (b.Method.HOOK,))
    c = b.Eligible(Writer(2, b"", "c", 9), (b.Method.HOOK, b.Method.NOP), True)
    d = b.Eligible(Writer(3, b"", "d", 5), (b.Method.NOP,))
    got = b.candidates([a, c, d])
    assert [(p.writer.text, p.method) for x in got[:4] for p in x.parts] == [
        ("c", b.Method.HOOK),
        ("c", b.Method.NOP),
        ("d", b.Method.NOP),
        ("a", b.Method.HOOK),
    ]
    together = got[4]
    assert [(p.writer.text, p.method, p.flags_fix) for p in together.parts] == [
        ("c", b.Method.HOOK, True),
        ("d", b.Method.NOP, False),
        ("a", b.Method.HOOK, False),
    ]
    assert together.label == "patch it: all 3 instructions" and got[0].label == "patch it: c"
    assert len(got) == 5
    assert b.candidates([a]) == [b.Candidate((b.Part(a.writer, b.Method.HOOK),))], "no 'all'"
    many = [
        b.Eligible(Writer(i, b"", str(i), 10 - i), (b.Method.HOOK, b.Method.NOP)) for i in range(4)
    ]
    assert len(b.candidates(many)) == b.MAX_CANDIDATES
    assert b.candidates([]) == []


# -- judge ---------------------------------------------------------------------------
def S(value, count=0, game_new=None):
    return b.Sample(value, count, game_new)


def data(effect, vtype, start, samples, **kw):
    return b.TestData(effect, vtype, start, tuple(samples), **kw)


def outcome(d, answers=None):
    j = b.judge(d, answers)
    return j.outcome, j.question


P, F, NH, ASK = b.Outcome.PASS, b.Outcome.FAIL, b.Outcome.NO_HIT, b.Outcome.NEEDS_ANSWER


@pytest.mark.parametrize(
    ("vtype", "samples", "want"),
    [
        (INT32, [S(100), S(100, 1), S(120, 2), S(120, 3)], P),
        (INT32, [S(100), S(99, 1)], F),  # whole numbers: exact
        (FLOAT, [S(100.0), S(99.995, 1)], P),  # decimals: within 0.01
        (FLOAT, [S(100.0), S(99.9, 1)], F),
        (FLOAT, [S(100.0), S(float("nan"), 1)], P),
        (INT32, [S(100), S(100)], NH),  # no tick: you didn't get hit
        (INT32, [], NH),
    ],
)
def test_judge_never_down(vtype, samples, want):
    assert (
        outcome(data(Effect.NEVER_DOWN, vtype, samples[0].value if samples else 0, samples))[0]
        is want
    )


@pytest.mark.parametrize(
    ("vtype", "full", "samples", "want"),
    [
        (INT32, 250, [S(100), S(250, 1), S(250, 2)], P),  # before the first tick: anything
        (INT32, 250, [S(250, 1), S(249, 2)], F),
        (FLOAT, 250.0, [S(250.0, 1), S(251.0, 2)], P),  # within 0.5%
        (FLOAT, 250.0, [S(250.0, 1), S(252.0, 2)], F),
        (INT32, None, [S(250, 1)], F),
    ],
)
def test_judge_always_full(vtype, full, samples, want):
    d = data(Effect.ALWAYS_FULL, vtype, 100, samples, full=full)
    assert outcome(d)[0] is want


@pytest.mark.parametrize(
    ("vtype", "samples", "want"),
    [
        (INT32, [S(10), S(6, 1, 3)], P),  # the game took 7, we took 4
        (INT32, [S(10), S(9, 1, 9)], P),  # 1 stays 1
        (INT32, [S(10), S(7, 1, 3)], F),
        (INT32, [S(10), S(3, 0)], F),  # down with no tick: not our routine
        (INT32, [S(10), S(12, 1, 12)], NH),  # only raises: nothing to judge
        (FLOAT, [S(100.0), S(70.0, 1, 40.0)], P),
        (FLOAT, [S(100.0), S(70.5, 1, 40.0)], P),  # within 2% of the game's drop
        (FLOAT, [S(100.0), S(72.0, 1, 40.0)], F),
        (FLOAT, [S(100.0), S(50.0, 3, 0.0), S(35.0, 4, 20.0)], P),  # 3 ticks: skipped
    ],
)
def test_judge_half(vtype, samples, want):
    assert outcome(data(Effect.HALF, vtype, samples[0].value, samples))[0] is want


def test_judge_half_case_with_two_ticks_and_exact_halving():
    d = data(Effect.HALF, INT32, 10, [S(10), S(4, 2, 0), S(3, 3, 2)])
    assert outcome(d)[0] is P, "the two-tick interval is skipped; 4 -> 3 is half of 2"


def test_judge_dead_game_fails_first():
    d = data(Effect.NEVER_DOWN, INT32, 100, [S(100, 1)], alive=False)
    assert b.judge(d) == b.Judgement(F, "the game stopped responding")


def test_judge_nop_asks_whether_you_got_hit():
    d = data(Effect.NEVER_DOWN, INT32, 100, [S(100), S(100)], nop=True)
    assert outcome(d) == (ASK, b.Question.GOT_HIT)
    assert outcome(d, {b.Question.GOT_HIT: b.Answer.YES})[0] is P
    assert outcome(d, {b.Question.GOT_HIT: b.Answer.NO})[0] is NH
    assert outcome(d, {b.Question.GOT_HIT: b.Answer.NOT_SURE})[0] is NH
    broken = data(Effect.NEVER_DOWN, INT32, 100, [S(100), S(90)], nop=True)
    assert outcome(broken)[0] is F, "a broken effect fails without asking"


def test_judge_only_me():
    base = [S(100), S(100, 1)]
    seen = data(Effect.NEVER_DOWN, INT32, 100, base, only_me=True, others_down=True)
    assert outcome(seen)[0] is P
    unseen = data(Effect.NEVER_DOWN, INT32, 100, base, only_me=True, others_down=False)
    assert outcome(unseen) == (ASK, b.Question.ENEMIES)
    assert outcome(unseen, {b.Question.ENEMIES: b.Answer.YES})[0] is P
    assert outcome(unseen, {b.Question.ENEMIES: b.Answer.NO})[0] is F
    assert outcome(unseen, {b.Question.ENEMIES: b.Answer.NOT_SURE})[0] is F
    nop = data(Effect.NEVER_DOWN, INT32, 100, base, nop=True, only_me=True, others_down=None)
    assert outcome(nop, {b.Question.GOT_HIT: b.Answer.YES}) == (ASK, b.Question.ENEMIES)
