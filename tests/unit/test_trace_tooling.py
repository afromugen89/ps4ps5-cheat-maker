"""The trace fixtures' own tooling: tests/asm.py lays out what it is told (RIP-relative
constants included), and tests/tracegame.py runs routines like a game's frames."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.core.values import ValueType
from tests.asm import asm, call, ends, ins, jcc, jmp, listing, mem, rep_movsb, ret, rip
from tests.tracegame import BASE, PID, RSP, SLOT, TraceGame

F, I32 = ValueType.FLOAT, ValueType.INT32
IP = 0x400100


def test_asm_rip_relative_points_at_the_target():
    target = 0x405000
    code = asm(
        IP,
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MULSS_XMM_XMMM32", "xmm0", rip(target)),
        ins("CVTTSS2SI_R32_XMMM32", "eax", "xmm0"),
        ins("MOV_RM32_R32", mem("rdi"), "eax"),
    )
    assert listing(code, IP) == [
        "movss xmm0,[rbx+10h]",
        "mulss xmm0,[405000h]",
        "cvttss2si eax,xmm0",
        "mov [rdi],eax",
    ]
    # the displacement is relative to the next instruction: 0x405000 - end of mulss
    mulss_end = ends(code, IP)[1]
    (disp,) = struct.unpack_from("<i", code, mulss_end - IP - 4)
    assert mulss_end + disp == target


def test_asm_branches_immediates_and_forms():
    code = asm(
        IP,
        call(0x400300),
        jmp(0x400400),
        jcc("JE_REL32_64", IP),
        ret(),
        rep_movsb(),
        ins("MOV_R32_IMM32", "eax", 0xC2C80000),  # above 2**31: the unsigned form
        ins("IMUL_R32_RM32_IMM32", "eax", "ecx", 100),
        ins("MOV_RM32_IMM32", mem("rdi", 8), 5),
        ins("MOV_R32_RM32", "ecx", mem(None, 0x1000)),
        ins("MOV_R32_RM32", "ecx", mem("rbx", 0x10, "rcx", 4)),
        ins("SUB_RM32_R32", mem("rax", 0x2B0), "ecx"),
    )
    assert listing(code, IP) == [
        "call 0000000000400300h",
        "jmp 0000000000400400h",
        "je short 0000000000400100h",
        "ret",
        "rep movsb [rdi],[rsi]",
        "mov eax,0C2C80000h",
        "imul eax,ecx,64h",
        "mov dword ptr [rdi+8],5",
        "mov ecx,[1000h]",
        "mov ecx,[rbx+rcx*4+10h]",
        "sub [rax+2B0h],ecx",
    ]
    with pytest.raises(ValueError):
        ins("MOVSS_XMM_XMMM32", mem("rax"), mem("rbx"))


def test_routine_refreshes_its_copy_on_every_read_and_records_traps():
    g = TraceGame()
    real = g.alloc(0x40)
    hud = g.alloc(0x10)
    g.set(real + 0x10, F, 87.0)
    r = g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
        ins("MOVSS_XMMM32_XMM", mem("rdi", 4), "xmm0"),
        regs={"rbx": real, "rdi": hud},
    )
    assert r.text == ["movss xmm0,[rbx+10h]", "movss [rdi+4],xmm0"]
    assert r.start == BASE + SLOT and g.get(hud + 4, F) == 87.0
    g.fake.write(PID, hud + 4, F.encode(1.0))  # a test value on the copy...
    assert g.fake.read(PID, hud + 4, 4) == F.encode(87.0)  # ...is put back next frame
    g.set(real + 0x10, F, 50.0)
    assert F.decode(g.fake.read(PID, hud + 4, 4)) == 50.0  # the copy follows
    hits = g.fake.watch(PID, hud + 4, 4, "write", 1.0)
    assert hits and all(h.rip == r.writer_end for h in hits)
    assert hits[-1].regs["rbx"] == real and hits[-1].regs["rdi"] == hud
    assert hits[-1].regs["rsp"] == RSP and "xmm0" not in hits[-1].regs
    assert len(g.fake.write_hits[hud + 4]) <= 16
    assert g.fake.watch(PID, hud + 6, 2, "write", 1.0), "any byte of the store"


def test_constants_globals_and_maps():
    g = TraceGame()
    k = g.const(F, 100.0)
    v = g.global_(4, 4)
    by = {m.start: m for m in g.fake.maps(PID)}
    rodata = next(m for m in by.values() if m.contains(k))
    data = next(m for m in by.values() if m.contains(v))
    assert not rodata.writable and data.writable and g.get(k, F) == 100.0
    game = g.session.require_game()
    assert game.module_base == BASE and game.module_end > v


def test_call_into_a_setter_records_the_return_address_on_the_stack():
    g = TraceGame()
    obj = g.alloc(0x40)
    hud = g.alloc(0x80)
    g.set(obj + 0x20, F, 12.5)
    setter = g.code(ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm1"), ret())
    r = g.routine(
        ins("MOVSS_XMM_XMMM32", "xmm1", mem("rbx", 0x20)),
        call(setter),
        regs={"rbx": obj, "rdi": hud},
        writer_ip=setter,
    )
    assert g.get(hud + 0x40, F) == 12.5
    [hit, *_] = g.fake.write_hits[hud + 0x40]
    assert hit.rip == ends(g.fake.peek(PID, setter, 8), setter)[0]
    ret_addr = struct.unpack("<Q", g.fake.peek(PID, hit.regs["rsp"], 8))[0]
    assert ret_addr == r.stop, "the return address into the caller is on the stack"


def test_changes_after_reads_and_pausing():
    g = TraceGame()
    real, hud = g.alloc(0x10), g.alloc(0x10)
    g.set(real, I32, 5)
    g.routine(
        ins("MOV_R32_RM32", "eax", mem("rcx")),
        ins("MOV_RM32_R32", mem("rdx"), "eax"),
        regs={"rcx": real, "rdx": hud},
    )
    g.change_after(2, real, I32, 9)
    assert I32.decode(g.fake.read(PID, hud, 4)) == 5
    assert I32.decode(g.fake.read(PID, hud, 4)) == 9
    g.running = False
    g.set(real, I32, 1)
    assert I32.decode(g.fake.read(PID, hud, 4)) == 9
    seen = []
    g.every_read(lambda: seen.append(1))
    g.running = True
    g.fake.read(PID, hud, 4)
    assert seen == [1]
