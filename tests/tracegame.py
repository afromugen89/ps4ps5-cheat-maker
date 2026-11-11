"""A fake game for "Find the real one behind it" (docs/architecture-trace.md, Tests).

Memory like a game's: module code (read + execute, int3 padding between routines),
read-only module data (constants such as 100.0), writable module data (globals),
heap objects and a stack. Routines assembled with tests/asm.py are placed in the code
and **run on tests/minicpu.py before every console read**, so screen copies are
written again and again from the real value as a game does every frame: a test value
written to a copy is put back, and copies follow a test value written to the real one.

Each time a routine's writer instruction runs, its trap is recorded into
``fake.write_hits[address it wrote]`` with the registers the code really left (the
general registers, ``rip``, ``rsp`` and ``rflags``: no XMM, as the console sends
none), so "base register reused, replay the loads" cases are honest.

    g = TraceGame()
    real = g.alloc(0x40)
    g.set(real + 0x10, F, 0.87)
    hud = g.alloc(0x10)
    g.routine(ins(...load [rbx+10h]...), ins(...store [rdi]...), regs={"rbx": real, "rdi": hud})

Cases the CPU can't run (``rep movsb``) use ``g.every_read(fn)`` and hand-made hits.
"""

from __future__ import annotations

import struct
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import iced_x86 as ix

from cheatmaker.console.client import WatchHit
from cheatmaker.console.fake import PROT_R, PROT_W, PROT_X, FakeConsole
from cheatmaker.core.session import Session
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ReadFailed
from tests.asm import asm
from tests.minicpu import Cpu, CpuFault

PID = 41
TITLE, VERSION = "PPSA09999", "01.000.000"
BASE = 0x400000
CODE_SIZE = 0x4000
SLOT = 0x200  # each routine gets its own slot; int3 fills the rest
RODATA = BASE + CODE_SIZE  # read-only module data: constants
DATA = RODATA + 0x1000  # writable module data: globals
DATA_SIZE = 0x1000
HEAP = 0x2000_0000
HEAP_SIZE = 0x10000
STACK = 0x7FFF_0000
STACK_SIZE = 0x4000
RSP = STACK + 0x3000
HITS_KEPT = 16  # a watch reports at most 16 hits
_GPRS = ("rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp")
_GPRS += tuple(f"r{i}" for i in range(8, 16))


@dataclass
class Routine:
    start: int
    stop: int  # the CPU runs from start until rip == stop
    writer: int  # the instruction whose traps are recorded
    regs: dict[str, int]
    insns: list[ix.Instruction] = field(default_factory=list, repr=False)
    runs: int = 0

    @property
    def writer_end(self) -> int:
        """The RIP a data watch reports for a writer in the routine (just past it)."""
        return next(i.next_ip for i in self.insns if i.ip == self.writer)

    @property
    def text(self) -> list[str]:
        fmt = ix.Formatter(ix.FormatterSyntax.INTEL)
        return [fmt.format(i) for i in self.insns]


class _Memory:
    """minicpu's view of the game (no write log, no ticks)."""

    def __init__(self, fake: FakeConsole) -> None:
        self.fake = fake

    def read(self, addr: int, size: int) -> bytes:
        try:
            return self.fake.peek(PID, addr, size)
        except ReadFailed as exc:
            raise CpuFault(str(exc)) from None

    def write(self, addr: int, data: bytes) -> None:
        try:
            self.fake.poke(PID, addr, data)
        except ReadFailed as exc:
            raise CpuFault(str(exc)) from None


class TraceGame:
    def __init__(self, *, connect: bool = True) -> None:
        self.fake = FakeConsole()
        self.fake.add_process(PID, "eboot.bin", TITLE, "Trace Test", VERSION)
        self.fake.add_region(PID, "executable", BASE, b"\xcc" * CODE_SIZE, PROT_R | PROT_X)
        self.fake.add_region(PID, "executable", RODATA, 0x1000, PROT_R)
        self.fake.add_region(PID, "executable", DATA, DATA_SIZE, PROT_R | PROT_W)
        self.fake.add_region(PID, "[anon]", HEAP, HEAP_SIZE, PROT_R | PROT_W)
        self.fake.add_region(PID, "[stack]", STACK, STACK_SIZE, PROT_R | PROT_W)
        self.routines: list[Routine] = []
        self.reads = 0
        self.running = True  # False: the game is paused (nothing refreshes)
        self._next_code = BASE + SLOT
        self._next = {"heap": HEAP + 0x100, "rodata": RODATA + 0x10, "data": DATA + 0x10}
        self._extra: list[Callable[[], None]] = []
        self._later: deque[tuple[int, Callable[[], None]]] = deque()
        self.fake.game_ticks.append(self.tick)
        self.session = Session(lambda ip, port: self.fake)
        if connect:
            self.session.connect("1.2.3.4")
            self.session.refresh_game()

    # -- memory --------------------------------------------------------------------
    def _take(self, area: str, size: int, align: int) -> int:
        at = (self._next[area] + align - 1) & ~(align - 1)
        self._next[area] = at + size
        return at

    def alloc(self, size: int, align: int = 0x10) -> int:
        """A heap object (zero-filled)."""
        return self._take("heap", size, align)

    def const(self, vtype: ValueType, value: float | int) -> int:
        """A constant in read-only module data (``mulss xmm0,[rip+K100]``)."""
        at = self._take("rodata", vtype.size, vtype.size)
        self.fake.poke(PID, at, vtype.encode(value))
        return at

    def global_(self, size: int = 8, align: int = 8) -> int:
        """A variable in writable module data (a global)."""
        return self._take("data", size, align)

    def set(self, addr: int, vtype: ValueType, value: float | int) -> None:
        self.fake.poke(PID, addr, vtype.encode(value))

    def get(self, addr: int, vtype: ValueType) -> float | int:
        return vtype.decode(self.fake.peek(PID, addr, vtype.size))

    def set_ptr(self, addr: int, target: int) -> None:
        self.fake.poke(PID, addr, struct.pack("<Q", target))

    # -- code ------------------------------------------------------------------------
    def code(self, *insns: ix.Instruction) -> int:
        """Place code in its own slot without running it (a callee); returns its start."""
        start = self._next_code
        raw = asm(start, *insns)
        if len(raw) >= SLOT or start + SLOT > BASE + CODE_SIZE:
            raise ValueError("routine too long for a slot")
        self.fake.poke(PID, start, raw)
        self._next_code += SLOT
        return start

    def routine(
        self,
        *insns: ix.Instruction,
        regs: dict[str, int],
        writer: int | None = None,
        writer_ip: int | None = None,
    ) -> Routine:
        """Place code and run it before every read. The traps recorded are those of
        instruction number ``writer`` (default the last one), or of the instruction at
        ``writer_ip`` (one in a callee placed with ``code``)."""
        start = self.code(*insns)
        decoded = list(ix.Decoder(64, self.fake.peek(PID, start, SLOT), ip=start))
        decoded = decoded[: len(insns)]
        w = decoded[-1 if writer is None else writer].ip if writer_ip is None else writer_ip
        r = Routine(start, decoded[-1].next_ip, w, dict(regs), decoded)
        self.routines.append(r)
        self._run(r)
        return r

    def every_read(self, fn: Callable[[], None]) -> None:
        """Something else the game does before every read (hand-made cases)."""
        self._extra.append(fn)

    def after_reads(self, n: int, fn: Callable[[], None]) -> None:
        """``fn`` happens once, ``n`` reads from now (the player changes the number)."""
        self._later.append((self.reads + n, fn))

    def change_after(self, n: int, addr: int, vtype: ValueType, value: float | int) -> None:
        self.after_reads(n, lambda: self.set(addr, vtype, value))

    # -- the game running --------------------------------------------------------
    def tick(self) -> None:
        self.reads += 1
        while self._later and self._later[0][0] <= self.reads:
            self._later.popleft()[1]()
        if not self.running:
            return
        for r in self.routines:
            self._run(r)
        for fn in self._extra:
            fn()

    def _run(self, r: Routine) -> None:
        cpu = Cpu(_Memory(self.fake))
        cpu["rsp"] = RSP
        for name, value in r.regs.items():
            cpu[name] = value
        cpu.rip = r.start
        while cpu.rip != r.stop:
            if cpu.steps >= 1000:
                raise CpuFault("routine still running after 1000 instructions")
            ip = cpu.rip
            insn = cpu.fetch(ip)
            store = _store(cpu, insn)
            cpu.step()
            if ip == r.writer and store is not None:
                self.record(*store, WatchHit(cpu.rip, _regs(cpu)))
        r.runs += 1

    def record(self, addr: int, size: int, hit: WatchHit) -> None:
        """A trap for a write of ``size`` bytes at ``addr``: a watch on any byte of it
        sees it (the fake looks hits up by the watched address)."""
        for a in range(addr, addr + size):
            hits = self.fake.write_hits.setdefault(a, [])
            hits.append(hit)
            del hits[:-HITS_KEPT]


def _regs(cpu: Cpu) -> dict[str, int]:
    return {n: cpu[n] for n in _GPRS} | {"rip": cpu.rip, "rflags": cpu.rflags}


def _store(cpu: Cpu, insn: ix.Instruction) -> tuple[int, int] | None:
    """(address, size) that ``insn`` writes through its first operand, or None."""
    if insn.op_count and insn.op_kind(0) == ix.OpKind.MEMORY:
        return cpu.address(insn), ix.MemorySizeInfo(insn.memory_size).size
    return None
