"""A Returnal-like game for the v3 "find it again" tests, in two runs.

Same code every run (module at 0x400000), but the player object is somewhere else
on the heap after a "restart". Two instructions touch health: a per-frame reader
and the damage writer, both ``[r12+0x2AC]``. Enemies of another class (vtable) go
through the same instructions.

``run_routines`` acts as the game's CPU for our capture routine: when a hook site
holds a ``jmp`` into a routine, it decodes the routine and does what it says for every
object the instruction touches (filter, store into the slot, bump the counter,
optional keep-value store). So the tests check the encoded bytes, not a stub.

A ``Layout`` is one game version: ``V1`` is today's game (the module constants are its
values), ``V2`` the version after an update, whose code moved 0x80 bytes and whose
health fields moved 8 bytes inside every object (the hooked instruction now reads
``[r12+2B4h]``), with its classes elsewhere (spec-game-update AC-U11).

``Game.hit`` is the game taking damage: it runs one writer (``FORMS``: a decimal, a
whole number written by ``sub [rax+2B0h],ecx ; jle``, an 8-byte whole number) on
tests/minicpu.py, instruction by instruction, starting at the game's own code. If a
hook's ``jmp`` is there, our routine runs as the PS5 would run it. ``Game.play`` queues
hits that happen one per console read, like a fight going on while the app polls.

``build(linked=True)`` adds what Make it work's linked values need (spec-linked-values,
Returnal-shaped): a second object holding the health fraction (health / 100) with its own
writer (``vmovss [rbx+40h],xmm1``), a percent the game works out from the fraction and
two HUD copies it works out from health, both on every console read. ``Game.damage``
takes a hit in both places; ``Game.slow_reads`` makes every read cost console time.
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
from tests.minicpu import Cpu, CpuFault, f32_bits

PID = 31
TITLE, VERSION = "PPSA01285", "03.000.005"
BASE = 0x400000
CODE_SIZE = 0x4000  # code, then spare zeros for caves
DATA = BASE + CODE_SIZE  # module data (last writable module map)
DATA_SIZE = 0x1000
RODATA = DATA + DATA_SIZE  # read-only module data holding the vtables
PLAYER_VT = RODATA + 0x100
ENEMY_VT = RODATA + 0x200
HEAP = 0x2000_0000
HEAP_SIZE = 0x10000
HP = 0x2AC
WRITER = BASE + 0x100  # vmovss [r12+2ACh],xmm1
READER = BASE + 0x200  # vmovss xmm0,[r12+2ACh]
VMOVSS_W = bytes.fromhex("C4C17A118C24AC020000")
VMOVSS_R = bytes.fromhex("C4C17A108424AC020000")
STATIC_PTR = DATA + 0x20  # a pointer chain root (only in the "simple" layout)
HP32 = 0x2B0  # whole-number health (4 bytes) in every object
HP64 = 0x2B8  # whole-number money (8 bytes) in every object
SUB_SITE = BASE + 0x300  # sub [rax+2B0h],ecx ; jle (the game checks for death)
SUB_W = bytes.fromhex("2988B0020000")
JLE_DEAD = bytes.fromhex("7E02")  # jle +2: to the "dead" code
MOV64_SITE = BASE + 0x400  # mov [rbx+2B8h],rcx
MOV64_W = bytes.fromhex("48898BB8020000")
STATIC_VALUE = 4321  # "Obolites": a whole number at a fixed place in the module's data
# Linked values (build(linked=True)): the fraction's own object, writer and copies.
FRAC_OBJ = HEAP + 0xA000
FRAC = 0x40  # the fraction (a decimal, health / MAX_HP) in its object
FRAC_SITE = BASE + 0x900  # vmovss [rbx+40h],xmm1
VMOVSS_FRAC = bytes.fromhex("C5FA118B40000000")
PERCENT = HEAP + 0xD000  # 0-100, worked out from the fraction on every read
HUD_COPIES = (HEAP + 0xE000, HEAP + 0xE010)  # health, worked out from it on every read
MAX_HP = 100.0
READ_LATENCY_S = 0.036  # one console request on a real PS5 (console tests)
STACK = 0x7FFF_0000
STACK_SIZE = 0x4000
RSP = STACK + 0x3000


@dataclass(frozen=True)
class Form:
    """One way the game writes a value: ``kind`` is ``mov`` (stores the new value
    from ``src``) or ``sub`` (``src`` holds the damage)."""

    site: int
    code: bytes
    obj: str  # register holding the object
    src: str
    vtype: ValueType
    offset: int
    kind: str = "mov"

    @property
    def end(self) -> int:
        return self.site + len(self.code)


FORMS = {
    "float": Form(WRITER, VMOVSS_W, "r12", "xmm1", ValueType.FLOAT, HP),
    "int32": Form(SUB_SITE, SUB_W, "rax", "ecx", ValueType.INT32, HP32, "sub"),
    "int64": Form(MOV64_SITE, MOV64_W, "rbx", "rcx", ValueType.INT64, HP64),
}
FRACTION_FORM = Form(FRAC_SITE, VMOVSS_FRAC, "rbx", "xmm1", ValueType.FLOAT, FRAC)  # linked
GAME_REGS = {  # what the game's other registers hold when it writes (any values work)
    "rdx": 0x1111_2222_3333_4444,
    "rsi": 0x5555,
    "rdi": 0x7777_0000,
    "rbp": RSP + 0x40,
    "r8": 8,
    "r9": 9,
    "r10": 10,
    "r11": 0xAAAA_BBBB,
    "r13": 13,
    "r14": 14,
    "r15": 15,
    "xmm0": 0x0123_4567_89AB_CDEF_FEDC_BA98_7654_3210,
    "xmm2": 0x2222,
}


def _with_disp(code: bytes, disp: int) -> bytes:
    """``code`` whose trailing 4 bytes (a disp32 field number) are ``disp``."""
    return code[:-4] + struct.pack("<i", disp)


READER_OTHER_REG = bytes.fromhex("C4C17A108426")  # vmovss xmm0,[r14+disp32] (same length)


@dataclass(frozen=True)
class Layout:
    """One version of the game's module. ``code_shift``: where the four instructions
    sit; ``field_shift``: where HP, HP32, HP64 sit in every object (the ident at +8
    never moves); ``vt_shift``: where the classes sit; ``static_off``: address of the
    int32 Obolites in module data; ``chain_root``: the pointer-chain root; ``changed``:
    the reader uses another base register (r14, not r12)."""

    version: str
    code_shift: int = 0
    field_shift: int = 0
    vt_shift: int = 0
    static_off: int = DATA + 0x40
    chain_root: int = STATIC_PTR
    changed: bool = False

    @property
    def writer(self) -> int:
        return WRITER + self.code_shift

    @property
    def reader(self) -> int:
        return READER + self.code_shift

    @property
    def sub_site(self) -> int:
        return SUB_SITE + self.code_shift

    @property
    def mov64_site(self) -> int:
        return MOV64_SITE + self.code_shift

    @property
    def hp(self) -> int:
        return HP + self.field_shift

    @property
    def hp32(self) -> int:
        return HP32 + self.field_shift

    @property
    def hp64(self) -> int:
        return HP64 + self.field_shift

    @property
    def player_vt(self) -> int:
        return PLAYER_VT + self.vt_shift

    @property
    def enemy_vt(self) -> int:
        return ENEMY_VT + self.vt_shift

    @property
    def vmovss_w(self) -> bytes:
        return _with_disp(VMOVSS_W, self.hp)

    @property
    def vmovss_r(self) -> bytes:
        head = READER_OTHER_REG if self.changed else VMOVSS_R[:-4]
        return head + struct.pack("<i", self.hp)

    @property
    def sub_w(self) -> bytes:
        return _with_disp(SUB_W, self.hp32)

    @property
    def mov64_w(self) -> bytes:
        return _with_disp(MOV64_W, self.hp64)

    @property
    def forms(self) -> dict[str, Form]:
        return {
            "float": Form(self.writer, self.vmovss_w, "r12", "xmm1", ValueType.FLOAT, self.hp),
            "int32": Form(
                self.sub_site, self.sub_w, "rax", "ecx", ValueType.INT32, self.hp32, "sub"
            ),
            "int64": Form(self.mov64_site, self.mov64_w, "rbx", "rcx", ValueType.INT64, self.hp64),
        }


V1 = Layout(VERSION)
V2 = Layout("03.001.000", code_shift=0x80, field_shift=8, vt_shift=0x40)

PLAYER_AT = {1: HEAP + 0x100, 2: HEAP + 0x4400, 3: HEAP + 0x8800}
ENEMIES_AT = {1: (HEAP + 0x800, HEAP + 0xC00), 2: (HEAP + 0x1000, HEAP + 0x5000), 3: ()}


@dataclass
class Game:
    fake: FakeConsole
    session: Session
    run: int
    player: int
    enemies: tuple[int, ...]
    touched: list[int] = field(default_factory=list)  # objects the hooked code sees
    capture_ticks: bool = True  # run_routines before each read (capture routines only)
    script: deque[tuple[int, float | int, str]] = field(default_factory=deque)
    last_cpu: Cpu | None = None  # the CPU after the last hit (registers, flags)
    layout: Layout = V1

    @property
    def health_addr(self) -> int:
        return self.player + self.layout.hp

    def health(self) -> float:
        return struct.unpack("<f", self.fake.peek(PID, self.health_addr, 4))[0]

    def set_health(self, v: float, obj: int | None = None) -> None:
        self.fake.poke(PID, (obj or self.player) + self.layout.hp, struct.pack("<f", v))

    def form(self, name: str) -> Form:
        """One of the layout's forms, or "fraction" (build(linked=True))."""
        return FRACTION_FORM if name == "fraction" else self.layout.forms[name]

    def value(self, obj: int, form: str = "float") -> float | int:
        f = self.form(form)
        return f.vtype.decode(self.fake.peek(PID, obj + f.offset, f.vtype.size))

    def set_value(self, obj: int, v: float | int, form: str = "float") -> None:
        f = self.form(form)
        self.fake.poke(PID, obj + f.offset, f.vtype.encode(v))

    def hit(self, obj: int, new: float | int, form: str = "float", rflags: int = 0x202) -> Cpu:
        """The game writes ``new`` into ``obj`` with the ``form`` writer, starting at its
        own code (so through our hook if one is there). Returns the CPU just after
        the writer: its registers and flags are what the game's next code sees."""
        f = self.form(form)
        cpu = Cpu(_GameMemory(self.fake), rflags=rflags)
        for name, v in GAME_REGS.items():
            cpu[name] = v
        cpu["rsp"] = RSP
        cpu[f.obj] = obj
        if f.vtype is ValueType.FLOAT:
            cpu[f.src] = f32_bits(float(new))
        elif f.kind == "sub":
            cpu[f.src] = int(self.value(obj, form)) - int(new)
        else:
            cpu[f.src] = int(new)
        self.last_cpu = cpu.run(f.site, f.end)
        return cpu

    def play(self, *hits: tuple[int, float | int, str]) -> None:
        """Queue hits ``(obj, new, form)``; one happens before each console read."""
        self.script.extend(hits)

    def _play_tick(self) -> None:
        if self.script:
            self.hit(*self.script.popleft())

    def show(self, obj: int) -> Cpu:
        """The HUD reads ``obj``'s health with the game's reader, starting at its own
        code (so through a hook there)."""
        lay = self.layout
        cpu = Cpu(_GameMemory(self.fake))
        for name, v in GAME_REGS.items():
            cpu[name] = v
        cpu["rsp"] = RSP
        cpu["r14" if lay.changed else "r12"] = obj
        return cpu.run(lay.reader, lay.reader + len(lay.vmovss_r))

    def fight(self, rounds: int = 1) -> Callable[[float], None]:
        """A ``sleep`` for ``cv.collect_objects``: each of the first ``rounds`` calls
        hits the player and every enemy once through the game's writer, each losing 1
        health, and the HUD shows each through the reader (so through a hook at either);
        later calls do nothing."""
        left = [rounds]

        def sleep(_seconds: float) -> None:
            if left[0] <= 0:
                return
            left[0] -= 1
            for obj in (self.player, *self.enemies):
                self.hit(obj, float(self.value(obj)) - 1.0)
                self.show(obj)

        return sleep

    # -- linked values (build(linked=True)) ---------------------------------------
    @property
    def fraction_addr(self) -> int:
        return FRAC_OBJ + FRAC

    def fraction(self) -> float:
        return float(self.value(FRAC_OBJ, "fraction"))

    def damage(self, amount: float) -> None:
        """A hit as the game takes it: health and its fraction each lose ``amount``
        through their own writer (so through a hook at either)."""
        self.hit(self.player, self.health() - amount)
        self.hit(FRAC_OBJ, self.fraction() - amount / MAX_HP, "fraction")

    def recalc(self) -> None:
        """What the game works out every frame: the percent from the fraction, the HUD
        copies from health."""
        self.fake.poke(PID, PERCENT, struct.pack("<f", self.fraction() * 100.0))
        for addr in HUD_COPIES:
            self.fake.poke(PID, addr, struct.pack("<f", self.health()))

    def slow_reads(self, advance: Callable[[float], None], seconds: float = READ_LATENCY_S) -> None:
        """Every console read from now on takes ``seconds`` of the fake clock."""
        self.fake.game_ticks.append(lambda: advance(seconds))

    def move_player(self, new: int) -> None:
        """A new biome: the player object is rebuilt elsewhere, the old one freed."""
        old = self.player
        self.fake.poke(PID, new, self.fake.peek(PID, old, 0x300))
        self.fake.poke(PID, old, bytes(0x300))
        self.touched = [new if o == old else o for o in self.touched]
        self.player = new


def _object(vt: int, ident: int, hp: float, layout: Layout = V1) -> bytes:
    obj = bytearray(0x300)
    obj[0:8] = struct.pack("<Q", vt)
    obj[8:12] = struct.pack("<I", ident)
    obj[layout.hp : layout.hp + 4] = struct.pack("<f", hp)
    obj[layout.hp32 : layout.hp32 + 4] = struct.pack("<i", int(hp))
    obj[layout.hp64 : layout.hp64 + 8] = struct.pack("<q", int(hp) * 1000)
    return bytes(obj)


class _GameMemory:
    """minicpu's view of the game's memory (no write log, no game ticks)."""

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


def build(
    run: int = 1,
    *,
    chain: bool = False,
    static: bool = False,
    connect: bool = True,
    capture_ticks: bool = True,
    layout: Layout = V1,
    linked: bool = False,
) -> Game:
    """``capture_ticks=False``: only ``hit``/``play`` run hooked code (for routines
    ``run_routines`` doesn't understand, like the builder's). ``static``: Obolites
    (STATIC_VALUE) at ``layout.static_off``. ``layout``: which game version runs.
    ``linked``: the fraction object, its writer, the percent and the HUD copies."""
    fake = FakeConsole()
    fake.add_process(PID, "eboot.bin", TITLE, "Returnal", layout.version)
    code = bytearray(b"\xcc" * 0x1000) + bytearray(CODE_SIZE - 0x1000)
    lay = layout
    for site, insn in (
        (lay.writer, lay.vmovss_w),
        (lay.reader, lay.vmovss_r),
        (lay.sub_site, lay.sub_w + JLE_DEAD + b"\xc3\x90\xc3"),  # alive: ret; dead: ret
        (lay.mov64_site, lay.mov64_w),
        *([(FRAC_SITE, VMOVSS_FRAC)] if linked else []),
    ):
        code[site - BASE : site - BASE + len(insn)] = insn
    fake.add_region(PID, "executable", BASE, bytes(code), PROT_R | PROT_X)
    fake.add_region(PID, "executable", DATA, DATA_SIZE, PROT_R | PROT_W)
    fake.add_region(PID, "executable", RODATA, 0x1000, PROT_R)
    fake.add_region(PID, "[anon]", HEAP, HEAP_SIZE, PROT_R | PROT_W)
    fake.add_region(PID, "[stack]", STACK, STACK_SIZE, PROT_R | PROT_W)
    player, enemies = PLAYER_AT[run], ENEMIES_AT[run]
    fake.poke(PID, player, _object(lay.player_vt, 0x40048, 100.0, lay))
    for e in enemies:
        fake.poke(PID, e, _object(lay.enemy_vt, 0x30010, 250.0, lay))
    if chain:  # [[base+root]+0x10]+HP leads to health (a "simple" game)
        holder = HEAP + 0xF000
        fake.poke(PID, lay.chain_root, struct.pack("<Q", holder - 0x10))
        fake.poke(PID, holder, struct.pack("<Q", player))
    if static:
        fake.poke(PID, lay.static_off, struct.pack("<i", STATIC_VALUE))
    session = Session(lambda ip, port: fake)
    if connect:
        session.connect("1.2.3.4")
        session.refresh_game()
    game = Game(
        fake,
        session,
        run,
        player,
        tuple(enemies),
        [*enemies, player],
        capture_ticks=capture_ticks,
        layout=lay,
    )
    after_reader = lay.reader + len(lay.vmovss_r)
    reg = "r14" if lay.changed else "r12"
    fake.read_hits[game.health_addr] = [
        WatchHit(after_reader, {reg: player, "rip": after_reader}),
        WatchHit(after_reader, {reg: player, "rip": after_reader}),
    ]
    fake.write_hits[game.health_addr] = [WatchHit(lay.writer + 10, {"r12": player})]
    fake.game_ticks.append(lambda: game.capture_ticks and run_routines(game))
    fake.game_ticks.append(game._play_tick)
    if linked:
        frac = bytearray(0x100)
        frac[FRAC : FRAC + 4] = struct.pack("<f", game.health() / MAX_HP)
        fake.poke(PID, FRAC_OBJ, bytes(frac))
        game.recalc()
        fake.write_hits[game.fraction_addr] = [
            WatchHit(FRAC_SITE + len(VMOVSS_FRAC), {"rbx": FRAC_OBJ})
        ] * 3
        fake.game_ticks.append(game.recalc)
    return game


# -- the game's CPU, for our routine only ----------------------------------------------
def _routine(fake: FakeConsole, site: int) -> list[ix.Instruction] | None:
    head = fake.peek(PID, site, 5)
    if head[0] != 0xE9:
        return None
    cave = site + 5 + struct.unpack("<i", head[1:5])[0]
    return list(ix.Decoder(64, fake.peek(PID, cave, 256), ip=cave))


def run_routines(game: Game) -> None:
    """Every hooked site in the game runs once for every touched object."""
    for site in (game.layout.writer, game.layout.reader):
        insns = _routine(game.fake, site)
        if insns is None:
            continue
        for obj in game.touched:
            _run(game.fake, insns, obj)


def _run(fake: FakeConsole, insns: list[ix.Instruction], obj: int) -> None:
    passed = True
    for ins in insns:
        m = ins.mnemonic
        if m == ix.Mnemonic.CMP:
            off = ins.memory_displacement
            size = 8 if ins.memory_size == ix.MemorySize.UINT64 else 4
            raw = fake.peek(PID, obj + off, size)
            have = struct.unpack("<Q" if size == 8 else "<I", raw)[0]
            want = ins.immediate(1) & ((1 << (8 * size)) - 1)
            passed = have == want
        elif m == ix.Mnemonic.JNE and not passed:
            return
        elif m == ix.Mnemonic.MOV and ins.memory_base == ix.Register.RIP:
            fake.poke(PID, ins.memory_displacement, struct.pack("<Q", obj))
        elif m == ix.Mnemonic.INC:
            addr = ins.memory_displacement
            (n,) = struct.unpack("<Q", fake.peek(PID, addr, 8))
            fake.poke(PID, addr, struct.pack("<Q", n + 1))
        elif m == ix.Mnemonic.MOV and ins.op1_kind == ix.OpKind.IMMEDIATE32:
            fake.poke(PID, obj + ins.memory_displacement, struct.pack("<I", ins.immediate(1)))
        elif m == ix.Mnemonic.JMP:
            return
