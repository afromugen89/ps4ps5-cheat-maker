"""A small assembler for test fixtures, over iced-x86's BlockEncoder (the same library
that decodes, so a fixture can't drift from what the analyser sees).

Instructions are built with ``ins(code, *operands)``: ``code`` is an ``iced_x86.Code``
name (``"MOVSS_XMM_XMMM32"``), registers are names (``"xmm0"``), memory is ``mem(...)``
or ``rip(target)``, and plain ints are immediates. Branches and calls take an absolute
target: ``call(0x400200)``, ``jmp(...)``, ``jcc("JE_REL32_64", ...)``.

``asm(ip, *insns)`` lays them out from ``ip``; BlockEncoder fixes up branch targets and
RIP-relative displacements (checked by tests/unit/test_asm.py).

    code = asm(0x400100,
               ins("MOVSS_XMM_XMMM32", "xmm0", mem("rbx", 0x10)),
               ins("MULSS_XMM_XMMM32", "xmm0", rip(K100)),
               ins("MOVSS_XMMM32_XMM", mem("rdi", 0x40), "xmm0"))
"""

from __future__ import annotations

import iced_x86 as ix

_FMT = ix.Formatter(ix.FormatterSyntax.INTEL)
Operand = str | int | ix.MemoryOperand


def reg(name: str) -> int:
    """``"rax"`` -> ``iced_x86.Register.RAX``."""
    return getattr(ix.Register, name.upper())


def mem(
    base: str | None = None, disp: int = 0, index: str | None = None, scale: int = 1
) -> ix.MemoryOperand:
    """``[base + index*scale + disp]``."""
    b = reg(base) if base else ix.Register.NONE
    i = reg(index) if index else ix.Register.NONE
    # iced-x86 wants 1 (fits a byte) or 8 (64-bit code) here; 1 gives the short form
    # games use for small offsets
    if disp == 0 and b != ix.Register.NONE:
        disp_size = 0
    elif -0x80 <= disp < 0x80 and b != ix.Register.NONE:
        disp_size = 1
    else:
        disp_size = 8
    return ix.MemoryOperand(b, i, scale, disp, disp_size)


def rip(target: int) -> ix.MemoryOperand:
    """``[rip+X]`` that points at ``target`` once laid out."""
    return ix.MemoryOperand(ix.Register.RIP, displ=target)


def _code(name: str) -> int:
    return getattr(ix.Code, name)


def ins(code: str, *ops: Operand) -> ix.Instruction:
    """One instruction from its exact ``Code`` and operands (see the module doc)."""
    c = _code(code)
    kinds = "".join(
        "r" if isinstance(o, str) else "m" if isinstance(o, ix.MemoryOperand) else "i" for o in ops
    )
    args = [reg(o) if isinstance(o, str) else o for o in ops]
    Ins = ix.Instruction
    if kinds.endswith("i") and not code.endswith("IMM64") and args[-1] > 0x7FFF_FFFF:
        unsigned = {
            "ri": Ins.create_reg_u32,
            "mi": Ins.create_mem_u32,
            "rri": Ins.create_reg_reg_u32,
        }
        unsigned |= {"rmi": Ins.create_reg_mem_u32}
        return unsigned[kinds](c, *args)
    makers = {
        "": Ins.create,
        "r": Ins.create_reg,
        "m": Ins.create_mem,
        "rr": Ins.create_reg_reg,
        "rm": Ins.create_reg_mem,
        "mr": Ins.create_mem_reg,
        "ri": Ins.create_reg_i64 if code.endswith("IMM64") else Ins.create_reg_i32,
        "mi": Ins.create_mem_i32,
        "rri": Ins.create_reg_reg_i32,
        "rmi": Ins.create_reg_mem_i32,
        "rrr": Ins.create_reg_reg_reg,
        "rrm": Ins.create_reg_reg_mem,
    }
    if kinds not in makers:
        raise ValueError(f"no builder for operands {kinds!r} of {code}")
    return makers[kinds](c, *args)


def call(target: int) -> ix.Instruction:
    return ix.Instruction.create_branch(ix.Code.CALL_REL32_64, target)


def jmp(target: int) -> ix.Instruction:
    return ix.Instruction.create_branch(ix.Code.JMP_REL32_64, target)


def jcc(code: str, target: int) -> ix.Instruction:
    return ix.Instruction.create_branch(_code(code), target)


def rep_movsb() -> ix.Instruction:
    return ix.Instruction.create_rep_movsb(64)


def ret() -> ix.Instruction:
    return ix.Instruction.create(ix.Code.RETNQ)


def asm(ip: int, *insns: ix.Instruction) -> bytes:
    """The instructions encoded one after another from ``ip``."""
    enc = ix.BlockEncoder(64)
    enc.add_many(list(insns))
    return bytes(enc.encode(ip))


def listing(code: bytes, ip: int) -> list[str]:
    """Intel text of every instruction in ``code`` (what fixtures assert first)."""
    return [_FMT.format(i) for i in ix.Decoder(64, code, ip=ip)]


def ends(code: bytes, ip: int) -> list[int]:
    """Where each instruction in ``code`` ends (a data watch's RIP after it)."""
    return [i.next_ip for i in ix.Decoder(64, code, ip=ip)]
