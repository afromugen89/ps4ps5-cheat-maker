"""QA: Find the real one behind it (docs/spec-trace.md, AC-T1..T12).

Report: qa/reports/2026-11-11-trace-shown-number.md. Each test names its TC-ID; open bugs
are strict xfails named BUG-xx so the marker has to come off when the fix lands.

Everything runs on tests/tracegame.py (FakeConsole + minicpu) with a fake clock: no
console, no network, no real waiting.
"""

from __future__ import annotations

import itertools
import random
import threading

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import trace_flow as tf
from cheatmaker.core import tracer as tr
from cheatmaker.core.real_check import Verdict
from cheatmaker.core.trace_way import Kind, Step, Way
from cheatmaker.core.tracer import Outcome, Tracer, watch_span
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ConnectionLost, ReadFailed
from tests.asm import asm, ends, ins, mem, rip
from tests.tracegame import PID, TraceGame
from tests.ui.test_trace_ui import Game, Harness, add_row
from tests.unit.test_trace_flow import CODE, K100, Memory
from tests.unit.test_tracer import Clock, Player, setter, two_hops, x100

F, D = ValueType.FLOAT, ValueType.DOUBLE
I8, I16, I32, I64 = ValueType.INT8, ValueType.INT16, ValueType.INT32, ValueType.INT64


# -- helpers ---------------------------------------------------------------------------------
class Recorder:
    """release()/resume() bookkeeping: every release must be resumed exactly once."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.released: list[tuple[int, int]] = []
        self.resumed: list[tuple[int, int]] = []
        self.fail_at = fail_at  # the n-th release (1-based) raises

    def __call__(self, addr: int, size: int):
        if self.fail_at is not None and len(self.released) + 1 == self.fail_at:
            raise ReadFailed(f"can't read the freeze at {addr:#x}")
        self.released.append((addr, size))
        return lambda: self.resumed.append((addr, size))

    def balanced(self) -> bool:
        return sorted(self.released) == sorted(self.resumed)


class StageWatch:
    """Wraps fake.write to note which stage each write happened in."""

    def __init__(self, g: TraceGame, player: Player) -> None:
        self.player = player
        self.stages: list[tuple[tr.Stage, int]] = []  # (stage, hop) per write
        orig = g.fake.write

        def write(pid, addr, data):
            last = player.seen[-1] if player.seen else (0.0, None, 0)
            self.stages.append((last[1], last[2]))
            return orig(pid, addr, data)

        g.fake.write = write


def tracer(g: TraceGame, addr: int, vt: ValueType, **kw):
    clock = Clock()
    kw.setdefault("sleep", clock.sleep)
    return Tracer(g.session, addr, vt, clock=clock, settle_s=1.0, **kw), clock


def flow(*insns, shown=F, lane=0):
    code = asm(CODE, *insns)
    writer_ip = [CODE, *ends(code, CODE)][-2]
    window = tf.decode_window(b"\xcc" * 16 + code, writer_ip, CODE + len(code) - writer_ip)
    m = Memory()
    return tf.follow(window, lane, m.read, m.is_const, shown)


def x100_cut(real=F):
    return Way(
        real, I32, (Step(Kind.MUL, real, const=100.0), Step(Kind.TO_WHOLE, I32, rounding="cut"))
    )


# -- TC-01 no write before the two-value memory check (AC-T5) ----------------------------------
def share_game(g: TraceGame):
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    k100 = g.const(F, 100.0)
    g.set(obj + 0x10, F, 87.0)
    g.set(obj + 0x14, F, 200.0)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("DIVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x14)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(k100)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        regs={"rbx": obj, "rdi": hud},
    )
    return obj, hud


@pytest.mark.parametrize("case", ["x100", "share", "two_hops"])
def test_tc01_every_write_comes_after_the_second_memory_check(case):
    g = TraceGame()
    if case == "x100":
        obj, hud = x100(g)
        player = Player(setter(g, obj + 0x10, F, 0.5))
        start, vt = hud, I32
    elif case == "share":
        obj, hud = share_game(g)
        player = Player(setter(g, obj + 0x10, F, 50.0))
        start, vt = hud, I32
    else:
        obj, _widget, hud = two_hops(g)
        player = Player(setter(g, obj + 0x10, F, 0.5), setter(g, obj + 0x10, F, 0.25))
        start, vt = hud, I32
    sw = StageWatch(g, player)
    t, _clock = tracer(g, start, vt)
    r = t.run(player)
    assert r.outcome is Outcome.FOUND, r.reason
    assert sw.stages, "the write test wrote"
    assert all(stage is tr.Stage.TEST for stage, _hop in sw.stages), sw.stages
    # every write address is a hop's checked source (written, then put back)
    sources = {h.source.address for h in r.hops if h.source}
    assert {a for _p, a, _d in g.fake.writes} <= sources


def test_tc01b_a_decoy_holding_the_same_number_once_is_never_written():
    """The decoy matches at the first steady value, not after the player's change."""
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x80)
    decoy = g.alloc(0x40)
    g.set(decoy + 0x10, F, 87.0)  # same number as the real one, but it doesn't move
    g.set(obj + 0x10, F, 87.0)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm0"),
        regs={"rbx": obj, "rdi": hud},
        trap_regs={"rbx": decoy},
    )
    t, _c = tracer(g, hud + 0x40, F)
    r = t.run(Player(setter(g, obj + 0x10, F, 60.0)))
    assert r.outcome is not Outcome.FOUND
    assert g.fake.writes == [], "nothing written: the decoy failed the second value"


# -- TC-02 release / resume on every exit path (AC-T2, AC-T8, AC-T9) ---------------------------
def _cancel_at(stage):
    def arrange(g, obj, player, stop):
        player.on_hint[stage] = stop.set

    return arrange


def _disconnect_at(stage):
    def arrange(g, obj, player, stop):
        player.on_hint[stage] = g.fake.disconnect

    return arrange


def _refuse_writes(g, obj, player, stop):
    g.fake.fail_writes = True


def _maps_lost(g, obj, player, stop):
    def maps(pid):
        raise ConnectionLost("gone")

    g.fake.maps = maps


@pytest.mark.parametrize(
    ("arrange", "outcome"),
    [
        (_cancel_at(tr.Stage.WATCH), Outcome.CANCELLED),
        (_cancel_at(tr.Stage.READ), Outcome.CANCELLED),
        (_cancel_at(tr.Stage.STEADY), Outcome.CANCELLED),
        (_cancel_at(tr.Stage.CHANGE), Outcome.CANCELLED),
        (_cancel_at(tr.Stage.TEST), Outcome.CANCELLED),
        (_disconnect_at(tr.Stage.WATCH), Outcome.LOST),
        (_disconnect_at(tr.Stage.READ), Outcome.LOST),
        (_disconnect_at(tr.Stage.STEADY), Outcome.LOST),
        (_disconnect_at(tr.Stage.TEST), Outcome.LOST),
        (_refuse_writes, Outcome.NOT_FOLLOWED),
        (_maps_lost, Outcome.LOST),
    ],
    ids=[
        "cancel-watch",
        "cancel-read",
        "cancel-steady",
        "cancel-change",
        "cancel-test",
        "lost-watch",
        "lost-read",
        "lost-steady",
        "lost-test",
        "refused-write",
        "maps-lost",
    ],
)
def test_tc02_every_exit_resumes_what_it_paused_and_leaves_no_test_value(arrange, outcome):
    g = TraceGame()
    obj, hud = x100(g)
    player = Player(setter(g, obj + 0x10, F, 0.5))
    stop = threading.Event()
    arrange(g, obj, player, stop)
    rec = Recorder()
    t, _c = tracer(g, hud, I32, release=rec)
    r = t.run(player, stop)
    assert r.outcome is outcome, (r.outcome, r.reason)
    assert rec.released and rec.balanced(), (rec.released, rec.resumed)
    g.fake.connected = True
    real = g.get(obj + 0x10, F)
    assert real in (pytest.approx(0.87), pytest.approx(0.5)), f"test value left: {real}"


def test_tc03_two_hops_release_both_copies_and_resume_both():
    g = TraceGame()
    obj, widget, hud = two_hops(g)
    rec = Recorder()
    t, _c = tracer(g, hud, I32, release=rec)
    r = t.run(Player(setter(g, obj + 0x10, F, 0.5), setter(g, obj + 0x10, F, 0.25)))
    assert r.outcome is Outcome.FOUND
    assert rec.released == [(hud, 4), (widget + 0x20, 4)]
    assert rec.resumed == [(widget + 0x20, 4), (hud, 4)], "last paused, first resumed"


def test_tc04_release_failing_at_hop_2_still_resumes_hop_1():
    g = TraceGame()
    obj, _widget, hud = two_hops(g)
    rec = Recorder(fail_at=2)
    t, _c = tracer(g, hud, I32, release=rec)
    r = t.run(Player(setter(g, obj + 0x10, F, 0.5)))
    assert r.outcome is Outcome.FAILED
    assert rec.released == [(hud, 4)] and rec.resumed == [(hud, 4)]


# -- TC-05 misaligned watch spans (AC-T2) ----------------------------------------------------------
def test_tc05_an_8_byte_copy_4_aligned_is_watched_by_its_low_half_and_traced():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    g.set(obj + 0x18, I64, 5_000_000_000)
    g.routine(
        ins("MOV_R64_RM64", "rax", mem("rbx", 0x18)),
        ins("MOV_RM64_R64", mem("rdi", 4), "rax"),
        regs={"rbx": obj, "rdi": hud},
    )
    assert watch_span(hud + 4, I64) == (hud + 4, 4)
    t, _c = tracer(g, hud + 4, I64)
    r = t.run(Player(setter(g, obj + 0x18, I64, 4_000_000_001)))
    assert r.outcome is Outcome.FOUND, r.reason
    assert r.address == obj + 0x18 and r.vtype is I64
    assert (hud + 4, 4, "write", tr.WATCH_S) in g.fake.watched


def test_tc05b_a_4_byte_decimal_2_aligned_is_watched_by_2_bytes_and_traced():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    g.set(obj + 0x10, F, 33.5)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 6), "xmm0"),
        regs={"rbx": obj, "rdi": hud},
    )
    assert watch_span(hud + 6, F) == (hud + 6, 2)
    t, _c = tracer(g, hud + 6, F)
    r = t.run(Player(setter(g, obj + 0x10, F, 20.25)))
    assert r.outcome is Outcome.FOUND and r.address == obj + 0x10


# -- TC-06 hops and time limits (AC-T6, AC-T9) --
def chain(g: TraceGame, n: int):
    """real -> c1 -> c2 -> ... -> cn (each a plain float copy)."""
    cells = [g.alloc(0x10) for _ in range(n + 1)]
    g.set(cells[0], F, 40.0)
    for src, dst in itertools.pairwise(cells):
        g.routine(
            ins("MOVSS_XMM_XMMM32", "xmm0", mem("rsi")),
            ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
            regs={"rsi": src, "rdi": dst},
        )
    return cells


def test_tc06_five_copies_deep_stops_at_4_hops_with_the_4th_source_not_sure():
    g = TraceGame()
    cells = chain(g, 5)
    values = iter([30.0, 20.0, 10.0, 5.0, 2.5, 1.25])
    player = Player(*[lambda: g.set(cells[0], F, next(values)) for _ in range(6)])
    t, _c = tracer(g, cells[5], F)
    r = t.run(player)
    assert r.outcome is Outcome.TOO_FAR
    assert len(r.hops) == tr.MAX_HOPS
    assert r.address == cells[1] and not r.sure
    assert g.get(cells[0], F) in (40.0, 30.0, 20.0, 10.0, 5.0, 2.5, 1.25), "no test value"


def test_tc06b_slow_player_whole_trace_stays_within_4_minutes():
    """Every read costs 6 s of the player's time: the trace ends near TOTAL_S."""
    g = TraceGame()
    cells = chain(g, 5)
    clock = Clock()
    orig = g.fake.read

    def slow_read(pid, addr, n):
        clock.t += 6.0
        return orig(pid, addr, n)

    g.fake.read = slow_read
    vals = iter([30.0, 20.0, 10.0, 5.0, 2.5, 1.25])
    player = Player(*[lambda: g.set(cells[0], F, next(vals)) for _ in range(6)])
    t = Tracer(g.session, cells[5], F, clock=clock, sleep=clock.sleep, settle_s=1.0)
    r = t.run(player)
    assert r.outcome is Outcome.TOO_FAR and r.reason == tr.TOO_LONG, (r.outcome, r.reason)
    assert clock.t <= tr.TOTAL_S + 10, clock.t


# -- TC-07 x86 patterns (AC-T3) -----------------------------------------------------------------
def test_tc07a_xchg_store_of_a_copy_is_not_already_real():
    p = flow(
        ins("MOV_R32_RM32", "eax", mem("rbx", 0x10)),
        ins("XCHG_RM32_R32", mem("rdi"), "eax"),
        shown=I32,
    )
    assert p.end is not tf.End.ALREADY_REAL, p.reason


def test_tc07a2_tracer_on_an_xchg_copy_does_not_say_already_real():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    g.set(obj + 0x10, I32, 87)
    start = g.code(
        ins("MOV_R32_RM32", "eax", mem("rbx", 0x10)),
        ins("XCHG_RM32_R32", mem("rdi"), "eax"),
    )
    xchg_end = ends(g.fake.peek(PID, start, 16), start)[1]

    def frame() -> None:  # what the routine does every frame (minicpu has no xchg)
        g.set(hud, I32, g.get(obj + 0x10, I32))
        g.record(hud, 4, WatchHit(xchg_end, {"rbx": obj, "rdi": hud, "rax": 0}))

    g.every_read(frame)
    frame()
    t, _c = tracer(g, hud, I32)
    r = t.run(Player(setter(g, obj + 0x10, I32, 70)))
    assert r.outcome is not Outcome.ALREADY_REAL
    assert r.outcome is Outcome.FOUND and r.address == obj + 0x10


@pytest.mark.parametrize("vex", [False, True], ids=["sse", "vex"])
def test_tc07b_constant_first_multiply_is_followed(vex):
    if vex:
        insns = (
            ins("VEX_VMOVSS_XMM_M32", "xmm1", rip(K100)),
            ins("VEX_VMULSS_XMM_XMM_XMMM32", "xmm0", "xmm1", mem("rbx", 0x10)),
        )
    else:
        insns = (
            ins("MOVSS_XMM_XMMM32", "xmm0", rip(K100)),
            ins("MULSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        )
    p = flow(
        *insns,
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        shown=I32,
    )
    assert p.end is tf.End.SOURCE, p.reason
    assert p.load.mem.text() == "[rbx+10h]" and p.way.factor() == 100


def test_tc07c_store_of_ah_is_byte_1_of_the_register():
    p = flow(
        ins("MOV_R32_RM32", "eax", mem("rbx", 0x10)),
        ins("MOV_RM8_R8", mem("rdi"), "ah"),
        shown=I8,
    )
    # either followed to byte 1 of the load, or not followed: never byte 0
    assert p.end is not tf.End.SOURCE or p.load.lane == 1, (p.load.lane, p.way.text())


@pytest.mark.parametrize(
    ("insns", "shown", "end", "way"),
    [
        (  # 32-bit mov zero-extends into the 8-byte copy
            (ins("MOV_R32_RM32", "eax", mem("rbx", 0x10)), ins("MOV_RM64_R64", mem("rdi"), "rax")),
            I64,
            tf.End.SOURCE,
            "INT32 -> zero-extend to 8 bytes -> INT64",
        ),
        (  # mov al,[m] over a wider value: only a byte of it
            (
                ins("MOV_R32_RM32", "eax", mem("rcx", 0x10)),
                ins("MOV_R8_RM8", "al", mem("rbx", 0x10)),
                ins("MOV_RM32_R32", mem("rdi"), "eax"),
            ),
            I32,
            tf.End.NOT_FOLLOWED,
            None,
        ),
        (  # movd eax,xmm0 between load and store
            (
                ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
                ins("MOVD_RM32_XMM", "eax", "xmm0"),
                ins("MOV_RM32_R32", mem("rdi"), "eax"),
            ),
            F,
            tf.End.SOURCE,
            "FLOAT -> FLOAT",
        ),
        (  # vmovss x0,x1,x2: lane 4 comes from x1
            (
                ins("MOVUPS_XMM_XMMM128", "xmm1", mem("rbx", 0x10)),
                ins("MOVSS_XMM_XMMM32", "xmm2", mem("rcx")),
                ins("VEX_VMOVSS_XMM_XMM_XMM", "xmm0", "xmm1", "xmm2"),
                ins("MOVUPS_XMMM128_XMM", mem("rdi"), "xmm0"),
            ),
            F,
            tf.End.SOURCE,
            "FLOAT -> FLOAT",
        ),
        (  # a cvttsd2si into a 64-bit register, the low 4 bytes stored
            (
                ins("MOVSD_XMM_XMMM64", "xmm0", mem("rbx", 0x10)),
                ins("CVTTSD2SI_R64_XMMM64", "rax", "xmm0"),
                ins("MOV_RM32_R32", mem("rdi"), "eax"),
            ),
            I32,
            tf.End.SOURCE,
            "DOUBLE -> cut to whole -> INT32",
        ),
        (  # x * x: two loaded values
            (
                ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
                ins("MULSS_XMM_XMMM32", "xmm0", "xmm0"),
                ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
            ),
            F,
            tf.End.NOT_FOLLOWED,
            None,
        ),
        (  # the constant register reused for another load after the multiply
            (
                ins("MOVSS_XMM_XMMM32", "xmm1", rip(K100)),
                ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
                ins("MULSS_XMM_XMMM32", "xmm0", "xmm1"),
                ins("MOVSS_XMM_XMMM32", "xmm1", mem("rbx", 0x20)),
                ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
            ),
            F,
            tf.End.SOURCE,
            "FLOAT -> x100 -> FLOAT",
        ),
        (  # a conditional move on the path
            (
                ins("MOV_R32_RM32", "eax", mem("rbx", 0x10)),
                ins("TEST_RM32_R32", "eax", "eax"),
                ins("CMOVS_R32_RM32", "eax", "ecx"),
                ins("MOV_RM32_R32", mem("rdi"), "eax"),
            ),
            I32,
            tf.End.NOT_FOLLOWED,
            None,
        ),
        (  # a copy through a callee-saved register
            (
                ins("MOV_R32_RM32", "ebx", mem("rcx", 0x10)),
                ins("MOV_RM32_R32", mem("rdi"), "ebx"),
            ),
            I32,
            tf.End.SOURCE,
            "INT32 -> INT32",
        ),
    ],
    ids=[
        "mov32-zero-extends",
        "mov-al-partial",
        "movd-gpr",
        "vmovss-3op-lane",
        "cvttsd2si-r64-low",
        "x-times-x",
        "const-reg-reused",
        "cmov-not-followed",
        "rbx-copy",
    ],
)
def test_tc07d_patterns(insns, shown, end, way):
    lane = 4 if any("VMOVSS" in tf.text(i).upper() for i in insns) else 0
    p = flow(*insns, shown=shown, lane=lane)
    assert p.end is end, p.reason
    if way is not None:
        assert p.way.text() == way


def test_tc07e_calls_clobber_caller_saved_registers():
    from tests.asm import call

    p = flow(
        ins("MOVSS_XMM_XMMM32", "xmm6", mem("rbx", 0x10)),
        call(CODE + 0x1000),
        ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm6"),
    )
    assert p.end is tf.End.NOT_FOLLOWED and "a call changes it" in p.reason
    p = flow(
        ins("MOV_R32_RM32", "r12d", mem("rbx", 0x10)),
        call(CODE + 0x1000),
        ins("MOV_RM32_R32", mem("rdi"), "r12d"),
        shown=I32,
    )
    assert p.end is tf.End.SOURCE and p.load.mem.text() == "[rbx+10h]"


def test_tc07f_mis_synced_window_never_gives_a_wrong_source():
    """Random bytes (data, a jump table) before the routine: the window may lose the
    path, but must never report a different source or factor."""
    rnd = random.Random(1)
    for _ in range(200):
        pad = bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 240)))
        code = asm(
            CODE,
            ins("MOV_R64_RM64", "rbx", mem("rsi", 8)),
            ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
            ins("MULSS_XMM_XMMM32", "xmm0", rip(K100)),
            ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
            ins("MOV_RM32_R32", mem("rdi"), "eax"),
        )
        writer_ip = [CODE, *ends(code, CODE)][-2]
        window = tf.decode_window(pad + code, writer_ip, CODE + len(code) - writer_ip)
        m = Memory()
        p = tf.follow(window, 0, m.read, m.is_const, I32)
        if p.end is tf.End.SOURCE:
            assert p.load.mem.text() == "[rbx+10h]" and p.way.factor() == 100


# -- TC-08 RIP-relative constants: read-only vs writable (AC-T3) -----------------------------------
def test_tc08_constant_in_the_code_map_counts_as_constant():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    k = g.code(ins("NOPD"))  # a slot in the R|X code map; put 100.0 in it
    g.fake.poke(PID, k, F.encode(100.0))
    g.set(obj + 0x10, F, 0.87)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(k)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        regs={"rbx": obj, "rdi": hud},
    )
    t, _c = tracer(g, hud, I32)
    r = t.run(Player(setter(g, obj + 0x10, F, 0.5)))
    assert r.outcome is Outcome.FOUND and r.way.factor() == 100


def test_tc08b_multiply_by_a_writable_global_is_not_a_constant():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    gk = g.global_(4, 4)
    g.set(gk, F, 100.0)
    g.set(obj + 0x10, F, 0.87)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(gk)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        regs={"rbx": obj, "rdi": hud},
    )
    t, _c = tracer(g, hud, I32)
    r = t.run(Player(setter(g, obj + 0x10, F, 0.5)))
    assert r.outcome is Outcome.NOT_FOLLOWED and g.fake.writes == []


# -- TC-09 Way maths round trips (AC-T5, AC-T7) --
WAYS = {
    "float x100 cut": x100_cut(F),
    "double x100 cut": x100_cut(D),
    "float x100 round": Way(
        F, I32, (Step(Kind.MUL, F, const=100.0), Step(Kind.TO_WHOLE, I32, rounding="round"))
    ),
    "float cut": Way(F, I32, (Step(Kind.TO_WHOLE, I32, rounding="cut"),)),
    "int x0.01 via float": Way(
        I32,
        I32,
        (
            Step(Kind.TO_DECIMAL, F),
            Step(Kind.MUL, F, const=0.01),
            Step(Kind.TO_WHOLE, I32, rounding="cut"),
        ),
    ),
    "float widened x100 cut": Way(
        F,
        I32,
        (
            Step(Kind.WIDEN, D),
            Step(Kind.MUL, D, const=100.0),
            Step(Kind.TO_WHOLE, I32, rounding="cut"),
        ),
    ),
    "int64 low half": Way(I64, I32, (Step(Kind.TRUNCATE, I32),)),
}


@pytest.mark.parametrize("name", list(WAYS))
def test_tc09_typed_screen_numbers_round_trip(name):
    way = WAYS[name]
    for screen in [*range(-1000, 1001), 99_999, 123_456, -77_777]:
        real, exact = way.real_for(screen)
        assert exact and way.to_screen(real) == screen, (screen, real)
        assert way.matches(real, screen)


def test_tc09b_double_narrowed_to_float_round_trips():
    way = Way(
        D,
        I32,
        (
            Step(Kind.NARROW, F),
            Step(Kind.MUL, F, const=100.0),
            Step(Kind.TO_WHOLE, I32, rounding="cut"),
        ),
    )
    bad = [s for s in range(-1000, 1001) if not way.real_for(s)[1]]
    assert bad == [], f"{len(bad)} screens not settable, e.g. {bad[:5]}"


def test_tc09c_share_of_max_with_max_0_refuses_cleanly():
    way = Way(
        F,
        I32,
        (
            Step(Kind.SHARE, F, max_at=0x10),
            Step(Kind.MUL, F, const=100.0),
            Step(Kind.TO_WHOLE, I32, rounding="cut"),
        ),
    )
    for mx in (0.0, -0.0, float("nan")):
        with pytest.raises(ValueError):
            way.real_for(50, {0x10: mx})
        with pytest.raises(ValueError):
            way.as_scale({0x10: mx})
        assert way.label({0x10: mx}) == "share of max"
        assert not way.matches(0.5, 50, {0x10: mx})
    real, exact = way.real_for(50, {0x10: 250.0})
    assert exact and way.to_screen(real, {0x10: 250.0}) == 50


def test_tc09d_negative_values_cut_toward_zero():
    way = x100_cut(F)
    assert way.to_screen(F.decode(F.encode(-0.879))) == -87
    assert way.matches(F.decode(F.encode(-0.879)), -87)
    assert not way.matches(F.decode(F.encode(-0.879)), -88)


def test_tc09e_decimal_screen_value_is_settable_without_a_nearest_notice():
    way = Way(F, F, (Step(Kind.MUL, F, const=1000.0),))
    _real, exact = way.real_for(127.0)
    assert exact, "x1000 decimal copy: typed 127 -> 127.0000076 on screen counts as 127"


# -- TC-10 log lines and hop messages (AC-T8, AC-T11) --
def test_tc10_too_far_text_after_one_hop_is_grammatical():
    from cheatmaker.ui.trace_dialog import result_text

    r = tr.TraceResult(Outcome.TOO_FAR, 0x1000, hops=(tr.Hop(1, None),), reason=tr.TOO_LONG)
    assert "1 steps" not in result_text(r)


def test_tc10b_raw_packet_is_logged_at_debug(caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="cheatmaker.core.tracer")
    g = TraceGame()
    obj, hud = x100(g)
    orig = g.fake.watch

    def watch(*a, **kw):
        hits = orig(*a, **kw)
        return [WatchHit(h.rip, h.regs, raw=b"\x01\x02" * 8) for h in hits]

    g.fake.watch = watch
    t, _c = tracer(g, hud, I32)
    t.run(Player(setter(g, obj + 0x10, F, 0.5)))
    assert "raw=0102010201020102" in caplog.text


# -- TC-12 Find list: traced rows next to search rows (AC-T1, AC-T7) -----------------------------


@pytest.fixture
def tgame():
    return Game()


@pytest.fixture
def twin(qtbot, tgame):
    """The main window on the trace tgame, Advanced on (as tests/ui/test_trace_ui.py)."""
    from cheatmaker.settings import Settings
    from cheatmaker.ui.controller import Controller
    from cheatmaker.ui.main_window import MainWindow
    from cheatmaker.ui.tasks import TaskRunner

    ctl = Controller(lambda ip, port: tgame.g.fake, Settings.load(), TaskRunner(sync=True))
    w = MainWindow(ctl)
    qtbot.addWidget(w)
    w.connect_view.ip.setText("1.2.3.4")
    w.connect_view.connect_btn.click()
    assert ctl.session.game is not None
    w.find.advanced.setChecked(True)
    yield w
    ctl.shutdown()


R = tr.TraceResult


def _match_and_traced(twin, tgame):
    """A search match (INT32) and a traced row (FLOAT) at one address."""
    f = twin.find
    f.vtype.setCurrentIndex(f.vtype.findData(I32))
    f.number.setText("87")
    f.search_btn.click()
    f.use_traced(R(Outcome.FOUND, 1, tgame.hud, F, x100_cut(F), Verdict.REAL, value=0.87))
    rows = [(i, f.row_types[i]) for i, m in enumerate(f.rows) if m.address == tgame.hud]
    assert [vt for _i, vt in rows] == [I32, F]
    return rows[0][0], rows[1][0]


def test_tc12a_remove_is_not_offered_on_the_search_match_row(twin, tgame):
    f = twin.find
    match_row, _traced_row = _match_and_traced(twin, tgame)
    f._select_row(match_row)
    f._update_buttons()
    if f.remove_btn.isEnabled():
        f.do_remove_address()
    assert tgame.hud in f.traced and f.manual.get(tgame.hud) is F, "the traced row stays"


def test_tc12b_search_match_row_doesnt_claim_to_be_the_added_row(twin, tgame):
    f = twin.find
    match_row, _traced_row = _match_and_traced(twin, tgame)
    assert "added" not in f._row_text(match_row), f._row_text(match_row)


def test_tc12c_set_on_a_share_of_max_row_with_max_0_says_why_and_writes_nothing(twin, tgame):
    f = twin.find
    real, mx = tgame.obj + 0x10, tgame.obj + 0x14
    tgame.g.set(mx, F, 0.0)
    way = Way(
        F,
        I32,
        (
            Step(Kind.SHARE, F, max_at=mx),
            Step(Kind.MUL, F, const=100.0),
            Step(Kind.TO_WHOLE, I32, rounding="cut"),
        ),
    )
    f.use_traced(R(Outcome.FOUND, 1, real, F, way, Verdict.REAL, value=0.5))
    failed = []
    twin.ctl.fail = lambda exc, where: failed.append(str(exc))
    before = len(tgame.g.fake.writes)
    f.set_value.setText("50")
    f.set_btn.click()
    assert failed and "can't be turned back" in failed[0]
    assert len(tgame.g.fake.writes) == before


def test_tc12d_too_far_row_is_added_marked_not_sure(twin, tgame):
    f = twin.find
    real = tgame.obj + 0x10
    f.use_traced(R(Outcome.TOO_FAR, 1, real, F, x100_cut(F), None, reason="4 hops"))
    t = f.traced[real]
    assert not t.sure and t.notes().endswith("not sure")


def test_tc12e_whole_flow_through_the_dialog_leaves_no_test_value(twin, tgame):
    h = Harness(twin, tgame)
    add_row(twin, tgame.hud, I32)
    twin.find.trace_btn.click()
    [d] = h.dialogs
    assert d.result.outcome is Outcome.FOUND
    real = tgame.g.get(tgame.obj + 0x10, F)
    assert real == pytest.approx(0.5)  # the player's last change, not a test value
    assert d.result.not_restored == ()


def test_tc12f_cancel_closes_without_adding_a_row(twin, tgame):
    h = Harness(twin, tgame)
    h.after_start = lambda d: None
    add_row(twin, tgame.hud, I32)

    def cancel_on_watch(_f, stage, _n):
        if stage is tr.Stage.WATCH:
            h.dialogs[-1].reject()

    orig = h.make

    def make(**kw):
        d = orig(**kw)
        d._signals.progress.connect(cancel_on_watch)
        return d

    twin.make_trace_dialog = make
    twin.find.trace_btn.click()
    [d] = h.dialogs
    assert d.result.outcome is Outcome.CANCELLED
    assert tgame.obj + 0x10 not in twin.find.manual and tgame.g.fake.writes == []


# -- TC-13 read back from the watched place ignores who writes most (AC-T3) -----------------
def test_tc13_a_rare_self_copy_doesnt_outvote_the_copier():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    g.set(obj + 8, F, 51.0)
    once = g.routine(  # a one-off self copy (a widget re-laid out): one hit
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rsi")),
        ins("MOVSS_XMMM32_XMM", mem("rsi"), "xmm0"),
        regs={"rsi": hud},
    )
    g.routines.remove(once)
    g.routine(  # the HUD copy, every frame
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rcx", 8)),
        ins("MOVSS_XMMM32_XMM", mem("rdx"), "xmm0"),
        regs={"rcx": obj, "rdx": hud},
    )
    for _ in range(5):
        g.fake.read(PID, hud, 4)  # some frames: the copier has the most hits
    t, _c = tracer(g, hud, F)
    r = t.run(Player(setter(g, obj + 8, F, 40.0)))
    assert r.outcome is Outcome.FOUND and r.address == obj + 8, (r.outcome, r.hops[0].stop)


# -- TC-14 a freeze on an 8-byte value that isn't listed any more (AC-T2) -----------------------
def _unlisted_8_byte_freeze(twin, tgame):
    from cheatmaker.ui.trace_dialog import TraceDialog

    ctl = twin.ctl
    x = tgame.obj + 0x20
    tgame.g.set(x, D, 1234.5)
    ctl.freezer.freeze(f"find:{x}", [(PID, x, D.encode(1234.5))])
    ctl.frozen_addresses.add(x)  # e.g. a search row frozen, then Start over
    d = TraceDialog(
        ctl,
        address=x + 4,
        vtype=I32,
        on_found=lambda r: None,
        on_search_way=lambda r, s: None,
    )
    return ctl, x, d


def test_tc14_an_unlisted_8_byte_freeze_over_the_copy_is_paused_and_put_back_whole(twin, tgame):
    ctl, x, d = _unlisted_8_byte_freeze(twin, tgame)
    resume = d.release(x + 4, 4)  # the high half: the freeze writes it too
    paused = f"find:{x}" not in ctl.freezer.keys
    resume()
    resume = d.release(x, 4)
    resume()
    entry = ctl.freezer._entries[f"find:{x}"]
    assert paused, "the freeze writing the watched bytes was left running"
    assert [len(data) for _p, _a, data in entry] == [8], "re-frozen with fewer bytes"


# -- TC-15 guide after Check which is real (AC-T1) ------------------------------------------------
def test_tc15_all_checked_rows_copies_with_one_unchecked_still_suggests_the_trace():
    from cheatmaker.core.guide import Action, Guide

    tip = Guide().after_check([Verdict.COPY, Verdict.COPY, Verdict.FAILED])
    assert tip is not None and tip.action is Action.TRACE


def test_tc15b_guide_suggests_after_all_copies_and_not_after_a_real_one():
    from cheatmaker.core.guide import Action, Guide

    assert Guide().after_check([Verdict.COPY, Verdict.COPY]).action is Action.TRACE
    assert Guide().after_check([Verdict.COPY, Verdict.REAL]) is None
    assert Guide().after_check([]) is None


# -- TC-16 execute-breakpoint fallback with a share of a max (AC-T4 rule 4) -----------------------
def test_tc16_exec_fallback_max_address_uses_the_max_loads_registers():
    g = TraceGame()
    obj, maxobj, hud = g.alloc(0x40), g.alloc(0x40), g.alloc(0x40)
    k100 = g.const(F, 100.0)
    g.set(obj + 0x10, F, 87.0)
    g.set(obj + 0x14, F, 50.0)  # not the max: what [rbx+14h] is before rbx moves on
    g.set_ptr(obj + 0x30, maxobj)
    g.set(maxobj + 0x14, F, 200.0)
    r0 = g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOV_R64_RM64", "rbx", mem("rbx", 0x30)),
        ins("DIVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x14)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(k100)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        regs={"rbx": obj, "rdi": hud},
    )
    assert g.get(hud, I32) == 43
    load_ip = r0.start
    g.fake.exec_hits[load_ip] = [WatchHit(load_ip, {"rbx": obj, "rdi": hud, "rip": load_ip})]
    player = Player(setter(g, obj + 0x10, F, 50.0), setter(g, obj + 0x10, F, 40.0))
    t, _c = tracer(g, hud, I32, exec_breakpoint=True)
    r = t.run(player)
    assert r.outcome is Outcome.FOUND, (r.outcome, r.hops[0].tried, r.hops[0].stop)
    assert r.way.max_addresses == (maxobj + 0x14,)


# -- TC-09f the narrow way's forward maths matches a real CPU (unicorn) ---------------------------
def test_tc09f_narrow_x100_cut_forward_matches_unicorn():
    """BUG-04 is in the inverse only: the forward model agrees with the CPU."""
    unicorn = pytest.importorskip("unicorn")
    from unicorn import x86_const as X

    code_at, data = 0x400000, 0x600000
    k = data + 0x100
    code = asm(
        code_at,
        ins("MOVSD_XMM_XMMM64", "xmm0", mem("rbx")),
        ins("CVTSD2SS_XMM_XMMM64", "xmm0", "xmm0"),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(k)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
    )
    way = Way(
        D,
        I32,
        (
            Step(Kind.NARROW, F),
            Step(Kind.MUL, F, const=100.0),
            Step(Kind.TO_WHOLE, I32, rounding="cut"),
        ),
    )
    for x in (2.37, -2.37, 0.53, 0.59, 1.05, 2.3700001239776611, 87.0, -0.01):
        mu = unicorn.Uc(unicorn.UC_ARCH_X86, unicorn.UC_MODE_64)
        mu.mem_map(code_at, 0x1000)
        mu.mem_map(data, 0x1000)
        mu.mem_write(code_at, code)
        mu.mem_write(k, F.encode(100.0))
        mu.mem_write(data, D.encode(x))
        mu.reg_write(X.UC_X86_REG_RBX, data)
        mu.reg_write(X.UC_X86_REG_RDI, data + 0x40)
        mu.emu_start(code_at, code_at + len(code))
        assert I32.decode(bytes(mu.mem_read(data + 0x40, 4))) == way.to_screen(x), x
