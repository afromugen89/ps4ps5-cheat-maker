"""A Returnal-like game for the v3 "find it again" tests, in two runs.

Same code every run (module at 0x400000), but the player object is somewhere else
on the heap after a "restart". Two instructions touch health: a per-frame reader
and the damage writer, both ``[r12+0x2AC]``. Enemies of another class (vtable) go
through the same instructions.

``run_routines`` acts as the game's CPU for our capture routine: when a hook site
holds a ``jmp`` into a routine, it decodes the routine and does what it says for every
object the instruction touches (filter, store into the slot, bump the counter,
optional keep-value store). So the tests check the encoded bytes, not a stub.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import iced_x86 as ix

from cheatmaker.console.client import WatchHit
from cheatmaker.console.fake import PROT_R, PROT_W, PROT_X, FakeConsole
from cheatmaker.core.session import Session

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

    @property
    def health_addr(self) -> int:
        return self.player + HP

    def health(self) -> float:
        return struct.unpack("<f", self.fake.peek(PID, self.health_addr, 4))[0]

    def set_health(self, v: float, obj: int | None = None) -> None:
        self.fake.poke(PID, (obj or self.player) + HP, struct.pack("<f", v))

    def move_player(self, new: int) -> None:
        """A new biome: the player object is rebuilt elsewhere, the old one freed."""
        old = self.player
        self.fake.poke(PID, new, self.fake.peek(PID, old, 0x300))
        self.fake.poke(PID, old, bytes(0x300))
        self.touched = [new if o == old else o for o in self.touched]
        self.player = new


def _object(vt: int, ident: int, hp: float) -> bytes:
    obj = bytearray(0x300)
    obj[0:8] = struct.pack("<Q", vt)
    obj[8:12] = struct.pack("<I", ident)
    obj[HP : HP + 4] = struct.pack("<f", hp)
    return bytes(obj)


def build(run: int = 1, *, chain: bool = False, connect: bool = True) -> Game:
    fake = FakeConsole()
    fake.add_process(PID, "eboot.bin", TITLE, "Returnal", VERSION)
    code = bytearray(b"\xcc" * 0x1000) + bytearray(CODE_SIZE - 0x1000)
    code[0x100 : 0x100 + 10] = VMOVSS_W
    code[0x200 : 0x200 + 10] = VMOVSS_R
    fake.add_region(PID, "executable", BASE, bytes(code), PROT_R | PROT_X)
    fake.add_region(PID, "executable", DATA, DATA_SIZE, PROT_R | PROT_W)
    fake.add_region(PID, "executable", RODATA, 0x1000, PROT_R)
    fake.add_region(PID, "[anon]", HEAP, HEAP_SIZE, PROT_R | PROT_W)
    player, enemies = PLAYER_AT[run], ENEMIES_AT[run]
    fake.poke(PID, player, _object(PLAYER_VT, 0x40048, 100.0))
    for e in enemies:
        fake.poke(PID, e, _object(ENEMY_VT, 0x30010, 250.0))
    if chain:  # [[base+STATIC]+0x10]+HP leads to health (a "simple" game)
        holder = HEAP + 0xF000
        fake.poke(PID, STATIC_PTR, struct.pack("<Q", holder - 0x10))
        fake.poke(PID, holder, struct.pack("<Q", player))
    session = Session(lambda ip, port: fake)
    if connect:
        session.connect("1.2.3.4")
        session.refresh_game()
    game = Game(fake, session, run, player, tuple(enemies), [*enemies, player])
    fake.read_hits[game.health_addr] = [
        WatchHit(READER + 10, {"r12": player, "rip": READER + 10}),
        WatchHit(READER + 10, {"r12": player, "rip": READER + 10}),
    ]
    fake.write_hits[game.health_addr] = [WatchHit(WRITER + 10, {"r12": player})]
    fake.game_ticks.append(lambda: run_routines(game))
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
    for site in (WRITER, READER):
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
