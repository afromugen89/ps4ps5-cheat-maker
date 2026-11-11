"""core/trace_flow.candidates: working out the source address (AC-T4 rules 1-3)."""

from __future__ import annotations

import itertools
import struct

from cheatmaker.console.client import WatchHit
from cheatmaker.core.trace_flow import End, candidates, follow, store_address
from cheatmaker.core.values import ValueType
from tests.asm import asm, call, ends, ins, mem, rep_movsb, rip
from tests.unit.test_trace_flow import CODE, DATA, Memory, window

F, I32 = ValueType.FLOAT, ValueType.INT32
HEAP = 0x2000_0000
OBJ = HEAP + 0x100
HUD = HEAP + 0x800


class Heap(Memory):
    def __init__(self) -> None:
        super().__init__()
        self.regions[HEAP] = bytearray(0x1000)


def run(*insns, regs, shown=F, lane=0, watched=None, mem_=None):
    m = mem_ or Heap()
    win, code = window(*insns)
    path = follow(win, lane, m.read, m.is_const, shown)
    hit = WatchHit(CODE + len(code), regs)
    return path, candidates(win, path, hit, HUD if watched is None else watched, m.read), win


def test_regs_rule_with_index_and_scale():
    path, [c], win = run(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10, "rcx", 4)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm0"),
        regs={"rbx": OBJ, "rcx": 3, "rdi": HUD - 0x40},
    )
    assert path.end is End.SOURCE
    assert c.address == OBJ + 12 + 0x10 and c.method == "regs" and c.expr == "rbx+rcx*4+10h"
    assert c.writer == win.writer.ip and c.max_address is None


def test_missing_register_gives_no_candidate():
    _p, cands, _w = run(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
        regs={"rdi": HUD},
    )
    assert cands == []


def test_rip_relative_global_is_fixed():
    g = DATA + 0x40
    _p, [c], _w = run(
        ins("MOV_R32_RM32", "eax", rip(g)),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        regs={"rdi": HUD},
        shown=I32,
    )
    assert c.address == g and c.method == "regs" and c.expr == f"{g:X}h"


def test_replay_reads_the_pointers_now():
    m = Heap()
    holder, inner = HEAP + 0x400, HEAP + 0x500
    m.put(OBJ + 0x30, struct.pack("<Q", holder))
    m.put(holder + 8, struct.pack("<Q", inner))
    path, [c], _w = run(
        ins("MOV_R64_RM64", "rax", mem("rbx", 0x30)),
        ins("MOV_R64_RM64", "rax", mem("rax", 8)),
        ins("MOV_R32_RM32", "ecx", mem("rax", 0x10)),
        ins("XOR_R32_RM32", "eax", "eax"),
        ins("MOV_RM32_R32", mem("rdx"), "ecx"),
        regs={"rbx": OBJ, "rax": 0, "rdx": HUD},
        shown=I32,
        mem_=m,
    )
    assert path.end is End.SOURCE
    assert c.address == inner + 0x10 and c.method == "replay"
    assert c.expr == "[[rbx+30h]+8h]+10h"


def test_replay_depth_is_limited():
    m = Heap()
    chain = [HEAP + 0x400 + 0x40 * k for k in range(5)]
    m.put(OBJ, struct.pack("<Q", chain[0]))
    for a, b in itertools.pairwise(chain):
        m.put(a, struct.pack("<Q", b))
    loads = [ins("MOV_R64_RM64", "rax", mem("rbx"))]
    loads += [ins("MOV_R64_RM64", "rax", mem("rax")) for _ in range(3)]
    _p, cands, _w = run(
        *loads,
        ins("MOV_R32_RM32", "ecx", mem("rax", 0x10)),
        ins("MOV_RM32_R32", mem("rdx"), "ecx"),
        regs={"rbx": OBJ, "rdx": HUD},
        shown=I32,
        mem_=m,
    )
    assert cands == [], "four pointer reads is past REPLAY_DEPTH"


def test_mov_and_lea_are_followed_in_replay():
    _p, [c], _w = run(
        ins("LEA_R64_M", "rsi", mem("rbx", 0x20)),
        ins("MOV_R64_RM64", "rax", "rsi"),
        ins("MOV_R32_RM32", "ecx", mem("rax", 4)),
        ins("MOV_R64_IMM64", "rax", 0),
        ins("MOV_R64_IMM64", "rsi", 0),
        ins("MOV_RM32_R32", mem("rdx"), "ecx"),
        regs={"rbx": OBJ, "rdx": HUD, "rax": 0, "rsi": 0},
        shown=I32,
    )
    assert c.address == OBJ + 0x24 and c.expr == "rbx+20h+4h"


def test_a_call_clobbers_the_base():
    _p, cands, _w = run(
        ins("MOV_R32_RM32", "ebx", mem("rcx", 8)),
        call(0x400800),
        ins("MOV_RM32_R32", mem("rdi"), "ebx"),
        regs={"rcx": OBJ, "rdi": HUD},
        shown=I32,
    )
    assert cands == [], "rcx after a call isn't what the load used"


def test_vector_lane_adds_to_the_address():
    _p, [c], _w = run(
        ins("VEX_VMOVUPS_XMM_XMMM128", "xmm0", mem("rsi")),
        ins("VEX_VMOVUPS_XMMM128_XMM", mem("rdi"), "xmm0"),
        regs={"rsi": OBJ, "rdi": HUD - 8},
        lane=8,
    )
    assert c.address == OBJ + 8 and c.expr == "rsi+8h"


def test_share_gives_the_max_address():
    _path, [c], _w = run(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("DIVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x14)),
        ins("MOVSS_XMMM32_XMM", mem("rdi"), "xmm0"),
        regs={"rbx": OBJ, "rdi": HUD},
    )
    assert c.address == OBJ + 0x10 and c.max_address == OBJ + 0x14


def test_block_copy():
    code = asm(CODE, rep_movsb())
    win, _c = window(rep_movsb())
    m = Heap()
    path = follow(win, 0, m.read, m.is_const, I32)
    hit = WatchHit(CODE, {"rsi": OBJ + 5, "rdi": HUD + 5})
    [c] = candidates(win, path, hit, HUD + 4, m.read)
    assert c.address == OBJ + 4 and c.method == "block" and c.expr == "watched+(rsi-rdi)"
    assert candidates(win, path, WatchHit(CODE, {}), HUD, m.read) == []
    assert ends(code, CODE) == [CODE + 2]


def test_store_address_and_no_candidates_for_other_ends():
    path, cands, win = run(ins("MOV_RM32_IMM32", mem("rdi", 0x40), 5), regs={"rdi": HUD}, shown=I32)
    assert path.end is End.CONSTANT and cands == []
    assert store_address(win.writer, WatchHit(0, {"rdi": HUD})) == HUD + 0x40
    assert store_address(win.writer, WatchHit(0, {})) is None


def test_exec_address_uses_the_registers_before_the_load():
    from cheatmaker.core.trace_flow import exec_address

    path, _c, _w = run(
        ins("MOV_R64_RM64", "rax", mem("rax", 8)),
        ins("MOV_RM64_R64", mem("rdi"), "rax"),
        regs={"rax": 0, "rdi": HUD},
        shown=ValueType.INT64,
    )
    assert exec_address(path.load, WatchHit(0, {"rax": OBJ})) == OBJ + 8
    assert exec_address(path.load, WatchHit(0, {})) is None
    g_path, _c, _w = run(
        ins("MOV_R32_RM32", "eax", rip(DATA + 0x40)),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
        regs={"rdi": HUD},
        shown=I32,
    )
    assert exec_address(g_path.load, WatchHit(0, {})) == DATA + 0x40
