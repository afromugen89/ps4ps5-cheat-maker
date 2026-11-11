"""core/tracer: Find the real one behind it, driven end to end on tests/tracegame.py
(AC-T12 driver matrix). The game re-runs its code before every read; the "player"
changes the number when the hint asks (the progress callback)."""

from __future__ import annotations

import itertools
import logging
import struct
import threading

import pytest

from cheatmaker.console.client import WatchHit
from cheatmaker.core import tracer as tr
from cheatmaker.core.real_check import Verdict
from cheatmaker.core.trace_way import Kind
from cheatmaker.core.tracer import Outcome, Tracer, watch_span
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ProtocolError, ReadFailed, WriteFailed
from tests.asm import asm, call, ends, ins, mem, rep_movsb, ret, rip
from tests.tracegame import PID, RSP, TraceGame

F, D = ValueType.FLOAT, ValueType.DOUBLE
I16, I32, I64 = ValueType.INT16, ValueType.INT32, ValueType.INT64


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Player:
    """The progress callback: records steps and changes the number when asked."""

    def __init__(self, *changes) -> None:
        self.changes = list(changes)  # callables, one per "change the number" hint
        self.seen: list[tuple[float, tr.Stage, int]] = []
        self.on_hint: dict[tr.Stage, object] = {}

    def __call__(self, frac: float, stage: tr.Stage, hop: int) -> None:
        self.seen.append((frac, stage, hop))
        if stage is tr.Stage.CHANGE and self.changes:
            self.changes.pop(0)()
        action = self.on_hint.get(stage)
        if action is not None:
            action()


def run(g: TraceGame, addr: int, vt: ValueType, player: Player | None = None, **kw):
    clock = Clock()
    kw.setdefault("sleep", clock.sleep)
    t = Tracer(g.session, addr, vt, clock=clock, settle_s=1.0, **kw)
    result = t.run(player or Player(), kw.pop("cancel", None))
    return result, t, clock


def setter(g: TraceGame, addr: int, vt: ValueType, value):
    return lambda: g.set(addr, vt, value)


def no_test_value_left(g: TraceGame, *expect: tuple[int, ValueType, float | int]) -> None:
    for addr, vt, v in expect:
        assert g.get(addr, vt) == v


# -- watch_span ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("addr", "vt", "want"),
    [
        (0x1000, I64, (0x1000, 8)),
        (0x1004, I64, (0x1004, 4)),
        (0x1002, I64, (0x1002, 2)),
        (0x1001, I64, None),
        (0x1004, I32, (0x1004, 4)),
        (0x1006, F, (0x1006, 2)),
        (0x1003, I32, None),
        (0x1002, I16, (0x1002, 2)),
        (0x1001, I16, None),
        (0x1001, ValueType.INT8, (0x1001, 1)),
    ],
)
def test_watch_span(addr, vt, want):
    assert watch_span(addr, vt) == want


# -- the AC-T12 matrix ---------------------------------------------------------------------
def float_copy(g: TraceGame, value: float = 87.5):
    obj, hud = g.alloc(0x40), g.alloc(0x80)
    g.set(obj + 0x10, F, value)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm0"),
        regs={"rbx": obj, "rdi": hud},
    )
    return obj, hud


def test_float_copy(caplog):
    caplog.set_level(logging.DEBUG, logger="cheatmaker.core.tracer")
    g = TraceGame()
    obj, hud = float_copy(g)
    r, t, _clock = run(g, hud + 0x40, F, Player(setter(g, obj + 0x10, F, 80.25)))
    assert r.outcome is Outcome.FOUND, r.reason
    assert r.address == obj + 0x10 and r.vtype is F and r.verdict is Verdict.REAL
    assert r.way.label() == "as shown" and r.behind == hud + 0x40
    assert r.value == 80.25 and r.way.screen_text() == "as is"
    [hop] = r.hops
    assert hop.source.method == "regs" and hop.source.expr == "rbx+10h" and hop.hits > 0
    assert hop.writers[0].writer.text == "movss [rdi+40h],xmm0"
    no_test_value_left(g, (obj + 0x10, F, 80.25), (hud + 0x40, F, 80.25))
    text = caplog.text
    for action in ("trace_start", "trace_watch", "trace_writer", "trace_path", "trace_source"):
        assert f"action={action}" in text
    for action in ("trace_check", "trace_test", "trace_hop", "trace_result", "trace_restore"):
        assert f"action={action}" in text
    assert t.not_restored == []


def test_int_copy():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    g.set(obj + 8, I32, 51)
    g.routine(
        ins("MOV_R32_RM32", "eax", mem("rcx", 8)),
        ins("MOV_RM32_R32", mem("rdx"), "eax"),
        regs={"rcx": obj, "rdx": hud},
    )
    r, _t, _c = run(g, hud, I32, Player(setter(g, obj + 8, I32, 40)))
    assert r.outcome is Outcome.FOUND and r.address == obj + 8 and r.vtype is I32
    assert r.value == 40


def test_copy_of_the_low_half_of_an_8_byte_count():
    """``mov rax,[rcx+8] ; mov [rdx],eax``: the real one is the 8-byte number."""
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    g.set(obj + 8, I64, 51)
    g.routine(
        ins("MOV_R64_RM64", "rax", mem("rcx", 8)),
        ins("MOV_RM32_R32", mem("rdx"), "eax"),
        regs={"rcx": obj, "rdx": hud},
    )
    r, _t, _c = run(g, hud, I32, Player(setter(g, obj + 8, I64, 40)))
    assert r.outcome is Outcome.FOUND, r.reason
    assert r.address == obj + 8 and r.vtype is I64 and r.verdict is Verdict.REAL
    assert [s.kind for s in r.way.steps] == [Kind.TRUNCATE] and r.value == 40
    no_test_value_left(g, (obj + 8, I64, 40), (hud, I32, 40))


def test_an_8_byte_copy_of_hp_and_maxhp_finds_hp_alone():
    """``mov rax,[rcx+8] ; mov [rdx],rax``: the watched low 4 bytes are hp; a Set on the
    found row must not touch maxhp next to it."""
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    g.set(obj + 8, I32, 51)
    g.set(obj + 12, I32, 120)  # maxhp
    g.routine(
        ins("MOV_R64_RM64", "rax", mem("rcx", 8)),
        ins("MOV_RM64_R64", mem("rdx"), "rax"),
        regs={"rcx": obj, "rdx": hud},
    )
    r, _t, _c = run(g, hud, I32, Player(setter(g, obj + 8, I32, 40)))
    assert r.outcome is Outcome.FOUND, r.reason
    assert r.address == obj + 8 and r.vtype is I32 and r.way.steps == ()
    assert g.get(obj + 12, I32) == 120, "maxhp untouched by the write test"


def test_cut_conversion():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    g.set(obj + 0x10, F, 87.9)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        regs={"rbx": obj, "rdi": hud},
    )
    assert g.get(hud, I32) == 87
    r, _t, _c = run(g, hud, I32, Player(setter(g, obj + 0x10, F, 50.5)))
    assert r.outcome is Outcome.FOUND and r.vtype is F
    assert r.way.steps[-1].kind is Kind.TO_WHOLE and r.way.rounding == "cut"


def x100(g: TraceGame):
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    k100 = g.const(F, 100.0)
    g.set(obj + 0x10, F, 0.87)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(k100)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        regs={"rbx": obj, "rdi": hud},
    )
    return obj, hud


def test_x100_by_a_rip_relative_constant():
    g = TraceGame()
    obj, hud = x100(g)
    assert g.get(hud, I32) == 87
    r, _t, _c = run(g, hud, I32, Player(setter(g, obj + 0x10, F, 0.5)))
    assert r.outcome is Outcome.FOUND and r.address == obj + 0x10 and r.vtype is F
    assert r.way.factor() == 100 and r.way.label() == "x0.01 of the screen"
    assert r.value == 0.5 and r.way.screen_text() == "x100"
    # Set 87 on the traced row: the float written shows 87 after the game's cut x100
    g.set(obj + 0x10, F, r.way.to_real(87))
    g.fake.read(PID, hud, 4)
    assert g.get(hud, I32) == 87


def test_share_of_max():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    k100 = g.const(F, 100.0)
    g.set(obj + 0x10, F, 43.5)
    g.set(obj + 0x14, F, 50.0)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("DIVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x14)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(k100)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        regs={"rbx": obj, "rdi": hud},
    )
    assert g.get(hud, I32) == 87
    r, _t, _c = run(g, hud, I32, Player(setter(g, obj + 0x10, F, 25.0)))
    assert r.outcome is Outcome.FOUND and r.address == obj + 0x10
    assert [s.kind for s in r.way.steps] == [Kind.SHARE, Kind.MUL, Kind.TO_WHOLE]
    assert r.way.max_addresses == (obj + 0x14,) and r.way.label() == "share of max"


def test_replay():
    g = TraceGame()
    obj, holder, inner, hud = g.alloc(0x40), g.alloc(0x40), g.alloc(0x40), g.alloc(0x40)
    g.set_ptr(obj + 0x30, holder)
    g.set_ptr(holder + 8, inner)
    g.set(inner + 0x10, I32, 300)
    g.routine(
        ins("MOV_R64_RM64", "rax", mem("rbx", 0x30)),
        ins("MOV_R64_RM64", "rax", mem("rax", 8)),
        ins("MOV_R32_RM32", "ecx", mem("rax", 0x10)),
        ins("XOR_R32_RM32", "eax", "eax"),
        ins("MOV_RM32_R32", mem("rdx"), "ecx"),
        regs={"rbx": obj, "rdx": hud},
    )
    r, _t, _c = run(g, hud, I32, Player(setter(g, inner + 0x10, I32, 250)))
    assert r.outcome is Outcome.FOUND and r.address == inner + 0x10
    assert r.hops[0].source.method == "replay"
    assert r.hops[0].source.expr == "[[rbx+30h]+8h]+10h"


def test_rip_relative_global():
    g = TraceGame()
    glob, hud = g.global_(4, 4), g.alloc(0x40)
    g.set(glob, I32, 9)
    g.routine(
        ins("MOV_R32_RM32", "eax", rip(glob)),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        regs={"rdi": hud},
    )
    r, _t, _c = run(g, hud, I32, Player(setter(g, glob, I32, 12)))
    assert r.outcome is Outcome.FOUND and r.address == glob
    assert g.session.classify(glob)[1] == glob - g.session.require_game().module_base


def test_block_copy_with_hand_made_registers(caplog):
    caplog.set_level(logging.INFO, logger="cheatmaker.core.tracer")
    g = TraceGame()
    src, dst = g.alloc(0x40), g.alloc(0x40)
    g.set(src + 4, I32, 77)
    start = g.code(ins("MOV_R32_RM32", "ecx", "edx"), rep_movsb())
    rep_ip = ends(g.fake.peek(PID, start, 4), start)[0]

    def copy_frame() -> None:  # what rep movsb does every frame, trapped mid-copy
        g.fake.poke(PID, dst, g.fake.peek(PID, src, 16))
        hit = WatchHit(rep_ip, {"rsi": src + 6, "rdi": dst + 6, "rcx": 10, "rsp": RSP})
        g.record(dst + 4, 4, hit)

    g.every_read(copy_frame)
    copy_frame()
    r, _t, _c = run(g, dst + 4, I32, Player(setter(g, src + 4, I32, 70)))
    assert r.outcome is Outcome.FOUND and r.address == src + 4
    assert r.hops[0].source.method == "block" and r.hops[0].source.expr == "watched+(rsi-rdi)"
    assert "rep_at_rip=True" in caplog.text, "the unverified reading is logged"


def test_vector_lane():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    g.set(obj + 8, F, 33.0)
    g.routine(
        ins("VEX_VMOVUPS_XMM_XMMM128", "xmm0", mem("rsi")),
        ins("VEX_VMOVUPS_XMMM128_XMM", mem("rdi"), "xmm0"),
        regs={"rsi": obj, "rdi": hud},
    )
    r, _t, _c = run(g, hud + 8, F, Player(setter(g, obj + 8, F, 21.0)))
    assert r.outcome is Outcome.FOUND and r.address == obj + 8
    assert r.hops[0].source.expr == "rsi+8h"


def test_caller_found_on_the_stack():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x80)
    g.set(obj + 0x20, F, 12.5)
    store = g.code(ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm1"), ret())
    g.routine(
        ins("LEA_R64_M", "rbx", mem(None, obj)),
        ins("MOVSS_XMM_XMMM32", "xmm1", mem("rbx", 0x20)),
        call(store),
        regs={"rdi": hud},
        writer_ip=store,
    )
    r, _t, _c = run(g, hud + 0x40, F, Player(setter(g, obj + 0x20, F, 6.25)))
    assert r.outcome is Outcome.FOUND and r.address == obj + 0x20
    assert r.hops[0].source.method == "caller"
    assert r.hops[0].writers[0].path.reg == "xmm1"


def test_already_real():
    g = TraceGame()
    obj = g.alloc(0x300)
    g.set(obj + 0x2B0, I32, 100)
    g.routine(ins("SUB_RM32_R32", mem("rax", 0x2B0), "ecx"), regs={"rax": obj, "rcx": 0})
    r, _t, _c = run(g, obj + 0x2B0, I32)
    assert r.outcome is Outcome.ALREADY_REAL and r.address is None
    assert r.reason == "read-modify-write"
    assert g.fake.writes == []


def test_scrambled():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    key = g.const(I32, 0x5A5A5A5A)
    g.set(obj + 0x10, I32, 87)
    g.routine(
        ins("MOV_R32_RM32", "eax", mem("rbx", 0x10)),
        ins("XOR_R32_RM32", "eax", rip(key)),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        regs={"rbx": obj, "rdi": hud},
    )
    r, _t, _c = run(g, hud, I32)
    assert r.outcome is Outcome.SCRAMBLED
    assert r.hops[0].writers[0].skipped == "scrambled"
    assert g.fake.writes == []


def test_wrong_registers_fail_the_memory_check_and_write_nothing():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x80)
    decoy = g.alloc(0x40)
    k100 = g.const(F, 100.0)
    g.set(decoy + 0x10, F, 0.5)
    g.set(obj + 0x10, F, 0.875)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(k100)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm0"),
        regs={"rbx": obj, "rdi": hud},
        trap_regs={"rbx": decoy},  # the console reports rbx wrong
    )
    r, _t, _c = run(g, hud + 0x40, F)
    assert r.outcome is Outcome.SEARCH_THAT_WAY and r.address is None
    assert [why for _c, why in r.hops[0].tried] == ["held 0.5, expected 0.875"]
    assert r.way.real is F and r.way.screen_text() == "x100"
    assert g.fake.writes == []


def test_a_way_that_is_the_copys_own_search_is_not_offered():
    """As is, same type: searching that way finds the same rows (review finding 11)."""
    g, _obj, hud, _decoy, _ip = wrong_regs_game()
    r, _t, _c = run(g, hud + 0x40, F)
    assert r.outcome is Outcome.NOT_FOLLOWED and r.way is None
    assert r.reason == "no address passed the memory check"


def two_hops(g: TraceGame):
    obj, widget, hud = g.alloc(0x40), g.alloc(0x40), g.alloc(0x40)
    k100 = g.const(F, 100.0)
    g.set(obj + 0x10, F, 0.87)
    g.routine(  # widget = real x 100
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(k100)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 0x20), "xmm0"),
        regs={"rbx": obj, "rdi": widget},
    )
    g.routine(  # HUD = whole part of the widget
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rsi", 0x20)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdx"), "eax"),
        regs={"rsi": widget, "rdx": hud},
    )
    return obj, widget, hud


def test_two_hops_hud_widget_real():
    g = TraceGame()
    obj, widget, hud = two_hops(g)
    player = Player(setter(g, obj + 0x10, F, 0.5), setter(g, obj + 0x10, F, 0.25))
    r, _t, _c = run(g, hud, I32, player)
    assert r.outcome is Outcome.FOUND, (r.reason, [h.stop for h in r.hops])
    assert r.address == obj + 0x10 and r.vtype is F and r.verdict is Verdict.REAL
    assert [h.source.address for h in r.hops] == [widget + 0x20, obj + 0x10]
    assert r.hops[0].verdict is Verdict.COPY
    assert r.way.factor() == 100 and r.way.real is F and r.way.shown is I32
    assert [s.kind for s in r.way.steps] == [Kind.MUL, Kind.TO_WHOLE]
    # hop 2's write test wrote only the real one (its group: real, widget, HUD)
    hop2 = [a for _p, a, _d in g.fake.writes][-2:]
    assert hop2 == [obj + 0x10, obj + 0x10]
    assert (tr.Stage.WATCH, 2) in [(st, n) for _f, st, n in player.seen]
    no_test_value_left(g, (obj + 0x10, F, 0.25))


def test_circle():
    g = TraceGame()
    a, b = g.alloc(0x40), g.alloc(0x40)
    g.set(a, F, 5.0)
    g.set(b, F, 5.0)
    g.routine(  # B = A
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rsi")),
        ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
        regs={"rsi": a, "rdi": b},
    )
    g.routine(  # A = B
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rdx")),
        ins("MOVSS_XMMM32_XMM", mem("rcx"), "xmm0"),
        regs={"rdx": b, "rcx": a},
    )

    def both(v: float):
        return lambda: (g.set(a, F, v), g.set(b, F, v))

    r, _t, _c = run(g, a, F, Player(both(4.0), both(3.0)))
    assert r.outcome is Outcome.TOO_FAR, r.reason
    assert r.address == b and r.verdict is None and not r.sure
    assert r.hops[-1].stop == "goes round in a circle"
    assert len(r.hops) == 2 and r.reason == "goes round in a circle"


def test_four_hops_is_the_limit():
    g = TraceGame()
    cells = [g.alloc(0x10) for _ in range(6)]
    g.set(cells[0], F, 9.0)
    for src, dst in itertools.pairwise(cells):
        g.routine(
            ins("MOVSS_XMM_XMMM32", "xmm0", mem("rsi")),
            ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
            regs={"rsi": src, "rdi": dst},
        )
    changes = [setter(g, cells[0], F, v) for v in (8.0, 7.0, 6.0, 5.0)]
    r, _t, _c = run(g, cells[5], F, Player(*changes))
    assert r.outcome is Outcome.TOO_FAR and len(r.hops) == tr.MAX_HOPS
    assert r.address == cells[1] and r.verdict is None
    assert r.reason == "4 hops"


def test_no_writer():
    g = TraceGame()
    quiet = g.alloc(0x10)
    r, _t, _c = run(g, quiet, I32)
    assert r.outcome is Outcome.NO_WRITER and r.reason == "nothing wrote to it"


def test_cant_watch():
    g = TraceGame()
    r, _t, _c = run(g, g.alloc(0x10) + 1, I32)
    assert r.outcome is Outcome.CANT_WATCH
    assert g.fake.watched == []


def test_constant_writer_is_skipped():
    g = TraceGame()
    hud = g.alloc(0x10)
    g.routine(ins("MOV_RM32_IMM32", mem("rdi"), 5), regs={"rdi": hud})
    r, _t, _c = run(g, hud, I32)
    assert r.outcome is Outcome.NO_WRITER
    assert r.hops[0].writers[0].skipped == "sets it to a fixed number"


def test_number_that_never_changes_is_unconfirmed():
    g = TraceGame()
    _obj, hud = float_copy(g)
    r, _t, clock = run(g, hud + 0x40, F)
    assert r.outcome is Outcome.UNCONFIRMED and r.reason == "the number didn't change"
    assert clock.t <= tr.CHANGE_WAIT_S + 5
    assert g.fake.writes == []


def test_release_and_resume():
    g = TraceGame()
    obj, hud = float_copy(g)
    calls = []

    def release(addr, size):
        calls.append(("release", addr, size))
        return lambda: calls.append(("resume", addr, size))

    r, _t, _c = run(g, hud + 0x40, F, Player(setter(g, obj + 0x10, F, 1.0)), release=release)
    assert r.outcome is Outcome.FOUND
    assert calls == [("release", hud + 0x40, 4), ("resume", hud + 0x40, 4)]


def test_cancel_during_the_watch():
    g = TraceGame()
    _obj, hud = float_copy(g)
    stop = threading.Event()
    player = Player()
    player.on_hint[tr.Stage.WATCH] = stop.set
    resumed = []
    clock = Clock()
    t = Tracer(
        g.session,
        hud + 0x40,
        F,
        clock=clock,
        sleep=clock.sleep,
        release=lambda a, n: lambda: resumed.append(a),
    )
    r = t.run(player, stop)
    assert r.outcome is Outcome.CANCELLED and resumed == [hud + 0x40]
    assert g.fake.until_answers == [True], "the watch's until ends it"
    assert g.fake.writes == []


def test_lost_connection_while_checking():
    g = TraceGame()
    _obj, hud = float_copy(g)
    player = Player()
    player.on_hint[tr.Stage.CHANGE] = g.fake.disconnect
    resumed = []
    r, _t, _c = run(g, hud + 0x40, F, player, release=lambda a, n: lambda: resumed.append(a))
    assert r.outcome is Outcome.LOST
    assert resumed == [hud + 0x40]


def test_lost_connection_during_the_write_test_names_what_stayed():
    g = TraceGame()
    obj, hud = float_copy(g)
    clock = Clock()
    testing = []
    player = Player(setter(g, obj + 0x10, F, 50.0))
    player.on_hint[tr.Stage.TEST] = lambda: testing.append(True)

    def sleep(s: float) -> None:
        clock.sleep(s)
        if testing:
            g.fake.disconnect()  # during the settle after the test write

    t = Tracer(g.session, hud + 0x40, F, clock=clock, sleep=sleep, settle_s=1.0)
    r = t.run(player)
    assert r.outcome is Outcome.LOST
    assert r.not_restored == (obj + 0x10,)
    assert struct.unpack("<f", g.fake.peek(PID, obj + 0x10, 4))[0] != 50.0, "test value stayed"


def test_progress_steps_and_bad_rows():
    g = TraceGame()
    obj, hud = float_copy(g)
    player = Player(setter(g, obj + 0x10, F, 2.0))
    run(g, hud + 0x40, F, player)
    steps = [st for _f, st, _n in player.seen]
    assert steps[0] is tr.Stage.WATCH
    assert {tr.Stage.READ, tr.Stage.STEADY, tr.Stage.CHANGE, tr.Stage.TEST} <= set(steps)
    fracs = [f for f, _st, _n in player.seen]
    assert fracs == sorted(fracs) and all(0 <= f <= 1 for f in fracs)
    with pytest.raises(ValueError, match="Pick one row"):
        Tracer(g.session, 0, F)


# -- execute breakpoint fallback (setting trace_exec_breakpoint) --------------------------
def wrong_regs_game():
    """A float copy whose trap registers are wrong (rbx points at a decoy)."""
    g = TraceGame()
    obj, hud, decoy = g.alloc(0x40), g.alloc(0x80), g.alloc(0x40)
    g.set(decoy + 0x10, F, 12.0)
    g.set(obj + 0x10, F, 87.0)
    r = g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm0"),
        regs={"rbx": obj, "rdi": hud},
        trap_regs={"rbx": decoy},
    )
    return g, obj, hud, decoy, r.start


def test_exec_fallback_finds_the_load_address(caplog):
    caplog.set_level(logging.INFO, logger="cheatmaker.core.tracer")
    g, obj, hud, decoy, load_ip = wrong_regs_game()
    g.fake.exec_hits[load_ip] = [
        WatchHit(load_ip, {"rbx": decoy, "rip": load_ip}),  # fails the memory check
        WatchHit(load_ip, {"rbx": obj, "rip": load_ip}),
    ]
    player = Player(setter(g, obj + 0x10, F, 50.0), setter(g, obj + 0x10, F, 40.0))
    r, _t, _c = run(g, hud + 0x40, F, player, exec_breakpoint=True)
    assert r.outcome is Outcome.FOUND and r.address == obj + 0x10
    assert r.hops[0].source.method == "exec" and r.hops[0].source.expr == "[rbx+10h]"
    assert (load_ip, 1, "execute", tr.EXEC_S) in g.fake.watched
    assert "method=exec" in caplog.text


def test_exec_fallback_stops_when_the_breakpoint_repeats(caplog):
    caplog.set_level(logging.INFO, logger="cheatmaker.core.tracer")
    g, obj, hud, _decoy, load_ip = wrong_regs_game()
    same = WatchHit(load_ip, {"rbx": obj, "rip": load_ip})
    g.fake.exec_hits[load_ip] = [same] * tr.EXEC_HITS
    r, _t, _c = run(g, hud + 0x40, F, Player(), exec_breakpoint=True)
    assert r.outcome is Outcome.NOT_FOLLOWED
    assert "the breakpoint repeats" in caplog.text
    assert r.hops[0].stop.startswith("the breakpoint repeats; "), "kept, not overwritten"
    assert g.fake.writes == []


def test_exec_fallback_is_off_by_default():
    g, _obj, hud, _decoy, load_ip = wrong_regs_game()
    g.fake.exec_hits[load_ip] = [WatchHit(load_ip, {"rbx": 0, "rip": load_ip})]
    r, _t, _c = run(g, hud + 0x40, F)
    assert r.outcome is Outcome.NOT_FOLLOWED
    assert all(kind != "execute" for _a, _s, kind, _t in g.fake.watched)


# -- review follow-ups: errors, time, read-modify-write, verdicts -------------------------
def test_kept_test_value_ends_found_kept():
    """Nothing copies the source any more by the write test: it keeps the test value."""
    g = TraceGame()
    obj, hud = float_copy(g)
    player = Player(setter(g, obj + 0x10, F, 50.0))
    player.on_hint[tr.Stage.TEST] = g.routines.clear
    r, _t, _c = run(g, hud + 0x40, F, player)
    assert r.outcome is Outcome.FOUND and r.verdict is Verdict.KEPT and r.sure
    assert r.address == obj + 0x10
    assert g.get(obj + 0x10, F) == 50.0, "put back"


def test_a_write_test_that_fails_is_not_followed():
    g = TraceGame()
    obj, hud = float_copy(g)
    player = Player(setter(g, obj + 0x10, F, 50.0))
    player.on_hint[tr.Stage.TEST] = lambda: setattr(g.fake, "fail_writes", True)
    r, _t, _c = run(g, hud + 0x40, F, player)
    assert r.outcome is Outcome.NOT_FOLLOWED and r.reason == "couldn't be checked"
    assert r.hops[0].verdict is Verdict.FAILED


def test_a_source_that_passes_the_first_value_but_not_the_second():
    """The trap's rbx points at a decoy that holds the same number at first."""
    g = TraceGame()
    obj, hud, decoy = g.alloc(0x40), g.alloc(0x80), g.alloc(0x40)
    k100 = g.const(F, 100.0)
    g.set(decoy + 0x10, F, 0.875)
    g.set(obj + 0x10, F, 0.875)
    g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(k100)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm0"),
        regs={"rbx": obj, "rdi": hud},
        trap_regs={"rbx": decoy},
    )
    r, _t, _c = run(g, hud + 0x40, F, Player(setter(g, obj + 0x10, F, 0.5)))
    assert r.outcome is Outcome.SEARCH_THAT_WAY
    assert [why for _c, why in r.hops[0].tried] == ["held 0.875, expected 0.5"]
    assert g.fake.writes == []


def cancel_run(g: TraceGame, addr: int, player: Player, stop: threading.Event, **kw):
    clock = Clock()
    kw.setdefault("sleep", clock.sleep)
    t = Tracer(g.session, addr, F, clock=clock, settle_s=1.0, **kw)
    return t.run(player, stop)


def test_cancel_during_the_change_wait():
    g = TraceGame()
    _obj, hud = float_copy(g)
    stop = threading.Event()
    player = Player()
    player.on_hint[tr.Stage.CHANGE] = stop.set
    r = cancel_run(g, hud + 0x40, player, stop)
    assert r.outcome is Outcome.CANCELLED and r.hops[0].verdict is None
    assert g.fake.writes == []


def test_cancel_during_the_write_test_puts_the_value_back():
    g = TraceGame()
    obj, hud = float_copy(g)
    stop = threading.Event()
    clock = Clock()
    testing: list[bool] = []
    player = Player(setter(g, obj + 0x10, F, 50.0))
    player.on_hint[tr.Stage.TEST] = lambda: testing.append(True)

    def sleep(s: float) -> None:
        clock.sleep(s)
        if testing:
            stop.set()  # Cancel pressed while the test value is in

    t = Tracer(g.session, hud + 0x40, F, clock=clock, sleep=sleep, settle_s=1.0)
    r = t.run(player, stop)
    assert r.outcome is Outcome.CANCELLED
    assert any(a == obj + 0x10 for _p, a, _d in g.fake.writes), "the test value went in"
    assert g.get(obj + 0x10, F) == 50.0, "its own value is back"
    assert r.not_restored == ()


def test_the_copy_freed_while_checking_is_not_followed(caplog):
    caplog.set_level(logging.INFO, logger="cheatmaker.core.tracer")
    g = TraceGame()
    _obj, hud = float_copy(g)
    real_read = g.fake.read

    def gone(pid, addr, size):
        if addr == hud + 0x40:
            raise ReadFailed("not mapped")
        return real_read(pid, addr, size)

    player = Player()
    player.on_hint[tr.Stage.STEADY] = lambda: setattr(g.fake, "read", gone)
    r, _t, _c = run(g, hud + 0x40, F, player)
    assert r.outcome is Outcome.NOT_FOLLOWED
    assert r.reason == "the copy can't be read any more"
    assert "action=trace_result outcome=not followed" in caplog.text
    assert "action=trace_restore" in caplog.text


def test_a_console_error_ends_failed_with_both_log_lines(caplog):
    caplog.set_level(logging.INFO, logger="cheatmaker.core.tracer")
    g = TraceGame()
    _obj, hud = float_copy(g)

    def bad_watch(*_a, **_kw):
        raise ProtocolError("unexpected answer")

    g.fake.watch = bad_watch
    resumed = []
    r, _t, _c = run(g, hud + 0x40, F, release=lambda a, n: lambda: resumed.append(a))
    assert r.outcome is Outcome.FAILED and r.reason == "unexpected answer"
    assert resumed == [hud + 0x40]
    assert "action=trace_result outcome=failed" in caplog.text
    assert "action=trace_restore not_restored=-" in caplog.text


def test_a_failed_put_back_names_what_stayed():
    g = TraceGame()
    obj, hud = float_copy(g)
    player = Player(setter(g, obj + 0x10, F, 50.0))
    real_write = g.fake.write
    count = []

    def write(pid, addr, data):
        count.append(addr)
        if len(count) > 1:  # the test value goes in, the put-back is refused
            raise WriteFailed("refused")
        real_write(pid, addr, data)

    g.fake.write = write
    g.routines.clear()  # nothing rewrites it: the test value is still there to put back
    g.every_read(lambda: g.set(hud + 0x40, F, g.get(obj + 0x10, F)))
    r, _t, _c = run(g, hud + 0x40, F, player)
    assert r.outcome is Outcome.FAILED and r.not_restored == (obj + 0x10,)


def test_time_running_out_in_the_check_offers_the_last_source():
    """Hop 2's check runs past TOTAL_S: TOO_FAR with hop 1's source, not sure."""
    g = TraceGame()
    obj, widget, hud = two_hops(g)
    clock = Clock()
    player = Player(setter(g, obj + 0x10, F, 0.5))  # only hop 1's change comes

    def late() -> None:
        if player.seen[-1][2] == 2:
            clock.t = tr.TOTAL_S - 5  # hop 2's watch starts with 5 s left

    player.on_hint[tr.Stage.WATCH] = late
    t = Tracer(g.session, hud, I32, clock=clock, sleep=clock.sleep, settle_s=1.0)
    r = t.run(player)
    assert r.outcome is Outcome.TOO_FAR and r.reason == "the trace took too long"
    assert r.address == widget + 0x20 and r.verdict is None and not r.sure
    watches = [timeout for _a, _s, kind, timeout in g.fake.watched if kind == "write"]
    assert watches[0] == tr.WATCH_S and watches[1] <= 5, "the watch is capped"
    assert clock.t <= tr.TOTAL_S + tr.STEADY_S + tr.POLL_S


def rmw_once(g: TraceGame, addr: int) -> None:
    """``add [rsi],ecx`` (ecx 0) on ``addr``, run once: one read-modify-write hit."""
    r = g.routine(ins("ADD_RM32_R32", mem("rsi"), "ecx"), regs={"rsi": addr, "rcx": 0})
    g.routines.remove(r)


def test_one_read_modify_write_among_more_frequent_writers_doesnt_decide():
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x40)
    g.set(obj + 8, I32, 51)
    rmw_once(g, hud)
    g.routine(
        ins("MOV_R32_RM32", "eax", mem("rcx", 8)),
        ins("MOV_RM32_R32", mem("rdx"), "eax"),
        regs={"rcx": obj, "rdx": hud},
    )
    for _ in range(3):
        g.tick()  # the copy is written every frame, the change in place only once
    r, _t, _c = run(g, hud, I32, Player(setter(g, obj + 8, I32, 40)))
    assert r.outcome is Outcome.FOUND and r.address == obj + 8, r.reason
    assert [w.writer.hits for w in r.hops[0].writers] == [4, 1]


def test_a_read_modify_write_decides_when_no_other_source_passes():
    g = TraceGame()
    obj, hud, decoy = g.alloc(0x40), g.alloc(0x40), g.alloc(0x40)
    g.set(obj + 8, I32, 51)
    g.set(decoy + 8, I32, 7)
    rmw_once(g, hud)
    g.routine(
        ins("MOV_R32_RM32", "eax", mem("rcx", 8)),
        ins("MOV_RM32_R32", mem("rdx"), "eax"),
        regs={"rcx": obj, "rdx": hud},
        trap_regs={"rcx": decoy},
    )
    r, _t, _c = run(g, hud, I32, Player(setter(g, obj + 8, I32, 40)))
    assert r.outcome is Outcome.ALREADY_REAL and r.address is None
    assert "no other source passed" in r.hops[0].stop


def test_read_modify_write_found_at_hop_2_is_found_not_sure():
    """The widget is changed in place (add [rsi],ecx) and the HUD copies it; something
    the watch doesn't see puts the widget back after a test write, so hop 1 says copy."""
    g = TraceGame()
    widget, hud = g.alloc(0x40), g.alloc(0x40)
    held = {"v": 60}
    g.set(widget, I32, 60)
    g.routine(ins("ADD_RM32_R32", mem("rsi"), "ecx"), regs={"rsi": widget, "rcx": 0})
    g.routine(
        ins("MOV_R32_RM32", "eax", mem("rsi")),
        ins("MOV_RM32_R32", mem("rdx"), "eax"),
        regs={"rsi": widget, "rdx": hud},
    )
    g.every_read(lambda: g.set(widget, I32, held["v"]))
    r, _t, _c = run(g, hud, I32, Player(lambda: held.update(v=45)))
    assert r.outcome is Outcome.FOUND, (r.reason, [h.stop for h in r.hops])
    assert r.address == widget and r.verdict is None and not r.sure
    assert r.hops[0].verdict is Verdict.COPY
    assert r.hops[1].stop == "read-modify-write: it's the real one"


def test_exec_fallback_max_after_a_pointer_reload_uses_the_moved_register():
    """The source load's registers are carried forward to the max load
    (``mov rbx,[rbx+30h]`` in between), as QA BUG-12."""
    from cheatmaker.core import trace_flow as tf

    g = TraceGame()
    obj, maxobj = g.alloc(0x40), g.alloc(0x40)
    g.set_ptr(obj + 0x30, maxobj)
    k100 = g.const(F, 100.0)
    body = (
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOV_R64_RM64", "rbx", mem("rbx", 0x30)),
        ins("DIVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x14)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(k100)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
    )
    start = g.code(*body)
    code = asm(start, *body)
    writer = [start, *ends(code, start)][-2]
    win = tf.decode_window(b"\xcc" * 8 + code, writer, start + len(code) - writer)

    def read(a: int, n: int) -> bytes:
        return g.fake.peek(PID, a, n)

    path = tf.follow(win, 0, read, lambda a: a == k100, I32)
    assert path.end is tf.End.SOURCE and path.max_load is not None
    hit = WatchHit(start, {"rbx": obj, "rip": start})
    assert tf.exec_max_address(win, path, hit, read) == maxobj + 0x14
    assert tf.exec_max_address(win, path, WatchHit(start, {"rip": start}), read) is None
