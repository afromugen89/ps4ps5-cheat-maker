"""Runs the encoded capture routine on a real x86-64 CPU emulator (unicorn) and compares
it with running only the original instruction: every register, the flags, the red zone
and the object must match; the slot must hold the object (or stay empty for enemies).

unicorn isn't a project dependency: this file is skipped unless it is installed
(``pip install unicorn``). QA ran it on 2026-11-02 (see qa/reports).
"""

from __future__ import annotations

import random
import struct

import pytest

from cheatmaker.core import cave as cv
from cheatmaker.core.locker import Writer

uc = pytest.importorskip("unicorn")
x86 = pytest.importorskip("unicorn.x86_const")

GPRS = ["RAX", "RBX", "RCX", "RDX", "RSI", "RDI", "RBP", *(f"R{i}" for i in range(8, 16))]
FORMS = {
    "writer vmovss [r12+2ACh],xmm1": "C4C17A118C24AC020000",
    "reader vmovss xmm0,[r12+2ACh]": "C4C17A108424AC020000",
    "reader mov rax,[rax+2ACh]": "488b80ac020000",
    "writer sub [rax+2ACh],ecx": "2988ac020000",
    "reader mov eax,[rbp+2ACh]": "8b85ac020000",
    "reader mov eax,[r15+2ACh]": "418b87ac020000",
}


def _run(base, orig, patched, filt, player, flags, value=None):
    site, cave, slot = base + 0x1000, base + 0x3000, base + 0x5000
    heap = 0x2000_0000 if base < 1 << 32 else 0x9_0000_0000
    vt_player, vt_enemy = base + 0x6100, base + 0x6200
    span = cv.decode_at(orig + b"\x90" * 16, site)[:1]
    target = cv.writer_target(Writer(site, orig, "", 1))
    code = cv.capture_code(
        span, target, slot, cave, filt, vtable=vt_player, field=8, field_value=0x40048, value=value
    )
    mu = uc.Uc(uc.UC_ARCH_X86, uc.UC_MODE_64)
    for start in (base, heap):
        mu.mem_map(start, 0x20000)
    mu.mem_write(site, (cv.jmp_patch(site, cave, len(orig)) if patched else orig) + b"\xf4")
    mu.mem_write(cave, code)
    obj = heap + 0x100
    o = bytearray(0x300)
    o[0:8] = struct.pack("<Q", vt_player if player else vt_enemy)
    o[8:12] = struct.pack("<I", 0x40048 if player else 0x30010)
    o[0x2AC:0x2B0] = struct.pack("<f", 100.0)
    mu.mem_write(obj, bytes(o))
    rnd = random.Random(7)
    rsp = heap + 0x18000
    canary = bytes(rnd.getrandbits(8) for _ in range(256))
    mu.mem_write(rsp - 128, canary)
    for r in GPRS:
        mu.reg_write(getattr(x86, f"UC_X86_REG_{r}"), rnd.getrandbits(64))
    mu.reg_write(getattr(x86, f"UC_X86_REG_{target.base_name.upper()}"), obj)
    mu.reg_write(x86.UC_X86_REG_RSP, rsp)
    mu.reg_write(x86.UC_X86_REG_EFLAGS, flags)
    mu.emu_start(site, site + len(orig), count=200)
    regs = {r: mu.reg_read(getattr(x86, f"UC_X86_REG_{r}")) for r in [*GPRS, "RSP", "EFLAGS"]}
    regs["XMM0"] = mu.reg_read(x86.UC_X86_REG_XMM0)
    return {
        "regs": regs,
        "redzone": bytes(mu.mem_read(rsp - 128, 256)),
        "obj": bytes(mu.mem_read(obj, 0x300)),
        "slot": struct.unpack("<QQ", bytes(mu.mem_read(slot, 16))),
        "at": obj,
    }


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("base", [0x400000, 0x8_0000_0000], ids=["low", "above4GiB"])
@pytest.mark.parametrize("filt", ["vtable", "field", "none"])
@pytest.mark.parametrize("player", [True, False], ids=["player", "enemy"])
@pytest.mark.parametrize("flags", [0x202, 0xAC3])
def test_tc17_capture_routine_is_invisible_to_the_game(form, base, filt, player, flags):
    if filt == "none" and not player:
        pytest.skip("no filter: every object is captured")
    orig = bytes.fromhex(FORMS[form])
    want = _run(base, orig, False, filt, player, flags)
    got = _run(base, orig, True, filt, player, flags)
    assert got["regs"] == want["regs"]
    assert got["redzone"] == want["redzone"] and got["obj"] == want["obj"]
    assert got["slot"] == ((got["at"], 1) if player or filt == "none" else (0, 0))


@pytest.mark.parametrize("flags", [0x202, 0xAC3])
def test_tc18_keep_routine_on_sub_leaves_flags_as_the_game_expects(flags):
    orig = bytes.fromhex(FORMS["writer sub [rax+2ACh],ecx"])
    want = _run(0x400000, orig, False, "vtable", True, flags)
    got = _run(0x400000, orig, True, "vtable", True, flags, value=struct.pack("<i", 999))
    assert struct.unpack("<i", got["obj"][0x2AC:0x2B0])[0] == 999
    assert got["regs"]["EFLAGS"] & 0x8D5 == want["regs"]["EFLAGS"] & 0x8D5
