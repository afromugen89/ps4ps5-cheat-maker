"""core/trace_flow one caller up (AC-T4 rule 3): a setter gets the value in an argument
register; the caller is found from the return address on the stack. Run on
tests/tracegame.py so the trap registers and the stack are what the code leaves."""

from __future__ import annotations

import iced_x86 as ix

from cheatmaker.core.locker import CONTEXT
from cheatmaker.core.trace_flow import (
    STACK_SEARCH,
    WINDOW,
    End,
    call_sites,
    decode_window,
    follow,
    follow_into_caller,
    store_address,
    writer_at,
)
from cheatmaker.core.values import ValueType
from tests.asm import call, ins, mem, ret
from tests.tracegame import BASE, CODE_SIZE, PID, TraceGame

F = ValueType.FLOAT


def setup(*setter_body):
    g = TraceGame()
    obj, hud = g.alloc(0x40), g.alloc(0x80)
    g.set(obj + 0x20, F, 12.5)
    setter = g.code(*setter_body, ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm1"), ret())
    r = g.routine(
        ins("LEA_R64_M", "rbx", mem(None, obj)),
        ins("MOVSS_XMM_XMMM32", "xmm1", mem("rbx", 0x20)),
        call(setter),
        regs={"rdi": hud},
        writer_ip=_store_in(g, setter),
    )
    return g, obj, hud, setter, r


def _store_in(g: TraceGame, start: int) -> int:
    """The setter's movss store."""
    for insn in ix.Decoder(64, g.fake.peek(PID, start, 32), ip=start):
        if insn.mnemonic == ix.Mnemonic.MOVSS:
            return insn.ip
    raise AssertionError("no store")


def analyse(g: TraceGame, watched: int):
    read = lambda a, n: g.fake.peek(PID, a, n)  # noqa: E731
    hit = g.fake.write_hits[watched][-1]
    found = writer_at(read(hit.rip - CONTEXT, CONTEXT), read(hit.rip, 16), hit.rip)
    assert found is not None
    start, code, _text = found
    win = decode_window(read(start - WINDOW, WINDOW) + code, start, len(code))
    lane = watched - store_address(win.writer, hit)
    path = follow(win, lane, read, lambda a: False, F)
    return read, hit, win, path


def test_value_in_xmm1_is_found_in_the_caller():
    g, obj, hud, _setter, r = setup()
    assert g.get(hud + 0x40, F) == 12.5
    read, hit, win, path = analyse(g, hud + 0x40)
    assert path.end is End.BLOCK_START and path.reg == "xmm1" and path.lane == 0
    stack = read(hit.regs["rsp"], STACK_SEARCH)
    rets = call_sites(stack, hit.regs["rsp"], (BASE, BASE + CODE_SIZE), read)
    assert rets[0] == r.stop, "the return address into the caller"
    cpath, cands = follow_into_caller(
        read, rets[0], path.reg, path.lane, hit, lambda a: False, path.way.real, win, hud + 0x40
    )
    assert cpath.end is End.SOURCE and cpath.load.mem.text() == "[rbx+20h]"
    assert [(c.address, c.method, c.expr) for c in cands] == [(obj + 0x20, "caller", "rbx+20h")]
    assert cpath.way.then(path.way).real is F


def test_callee_that_writes_rbx_drops_the_candidate():
    g, _obj, hud, _setter, _r = setup(ins("XOR_R32_RM32", "ebx", "ebx"))
    read, hit, win, path = analyse(g, hud + 0x40)
    assert path.end is End.BLOCK_START
    rets = call_sites(
        read(hit.regs["rsp"], STACK_SEARCH), hit.regs["rsp"], (BASE, BASE + CODE_SIZE), read
    )
    cpath, cands = follow_into_caller(
        read, rets[0], path.reg, 0, hit, lambda a: False, F, win, hud + 0x40
    )
    assert cpath.end is End.SOURCE and cands == [], "rbx at the trap isn't the caller's"


def test_stack_values_that_are_not_return_addresses_are_ignored():
    g = TraceGame()
    read = lambda a, n: g.fake.peek(PID, a, n)  # noqa: E731
    junk = (BASE + 0x10).to_bytes(8, "little") + (0x1234).to_bytes(8, "little")
    assert call_sites(junk, 0, (BASE, BASE + CODE_SIZE), read) == []
