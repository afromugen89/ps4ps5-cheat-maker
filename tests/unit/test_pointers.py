import struct

from cheatmaker.core.pointers import (
    Chain,
    _windows,
    chains_path,
    find_chains,
    load_chains,
    recheck_session,
    resolve_live,
    save_chains,
    scan_session,
)
from tests.conftest import DATA_BASE, HEALTH_ADDR, HEAP_BASE, MODULE_BASE, PID

STATIC = DATA_BASE + 0x20  # module data: pointer to an object
OBJ = HEAP_BASE + 0x400  # heap object: pointer to the "player" block holding health


def plant_chain(fake, target=HEALTH_ADDR):
    fake.poke(PID, STATIC, struct.pack("<Q", OBJ - 0x10))  # [base+0x1020]+0x10 = OBJ
    fake.poke(PID, OBJ, struct.pack("<Q", target - 0x40))  # [OBJ]+0x40 = target


def test_finds_two_level_chain_and_resolves(session, fake):
    plant_chain(fake)
    chains = scan_session(session, HEALTH_ADDR, depth=3)
    want = Chain(STATIC - MODULE_BASE, (0x10, 0x40))
    assert want in chains
    assert want.label() == "[[base+0x1020]+0x10]+0x40"
    assert resolve_live(session, want) == HEALTH_ADDR
    assert len(fake.pointer_passes) >= 2, "one console pass per level"


def test_chain_survives_a_move_after_restart(session, fake):
    plant_chain(fake)
    chains = scan_session(session, HEALTH_ADDR)
    moved = HEAP_BASE + 0x800  # "restart": health lives somewhere else now
    fake.poke(PID, moved, struct.pack("<i", 100))
    plant_chain(fake, moved)
    kept = recheck_session(session, chains, moved)
    assert Chain(STATIC - MODULE_BASE, (0x10, 0x40)) in kept
    assert all(resolve_live(session, c) == moved for c in kept)


def test_no_chain_when_nothing_points_there(session):
    assert scan_session(session, HEALTH_ADDR, depth=2) == []


def test_unreadable_pointer_resolves_to_none(session, fake):
    fake.poke(PID, STATIC, struct.pack("<Q", 0x10))  # points at nothing mapped
    assert resolve_live(session, Chain(STATIC - MODULE_BASE, (0, 0))) is None


def test_save_and_load_per_game_version(tmp_path):
    c = [Chain(0x1020, (0x10, 0x40)), Chain(0x8, (0x0,))]
    path = chains_path("PPSA01285", "03.000.005")
    assert path.name == "PPSA01285_03.000.005.json"
    save_chains(path, "PPSA01285", "03.000.005", c)
    assert load_chains(path) == c


def test_far_apart_nodes_get_separate_passes():
    gb = 1 << 30
    w = _windows([0x1000, 0x2000, 5 * gb, 5 * gb + 0x10], 0x100)
    assert w == [(0xF00, 0x2000), (5 * gb - 0x100, 5 * gb + 0x10)]


def test_find_chains_depth_limit():
    # target <- A <- B <- static: depth 2 can't reach the static root.
    T, A, B, S = 0x9000_0000, 0x8000_0000, 0x7000_0000, 0x40_1000
    mem = {A: T, B: A, S: B}

    def refs(lo, hi, keep, _p):
        return [(h, v) for h, v in mem.items() if lo <= v <= hi and keep(v)]

    assert find_chains(T, 0x40_0000, 0x50_0000, refs, depth=2) == []
    assert find_chains(T, 0x40_0000, 0x50_0000, refs, depth=3) == [Chain(0x1000, (0, 0, 0))]


def test_node_cap_keeps_the_nearest_not_the_first(monkeypatch):
    """Bug fix: the 20k cap kept the first holders in memory order, so a far-away
    holder low in memory could push out the real (short) link."""
    from cheatmaker.core import pointers

    monkeypatch.setattr(pointers, "MAX_NODES_PER_LEVEL", 1)
    target, root, base = 0x9000_0000, 0x40_1000, 0x40_0000
    far, near = 0x5000_0000, 0x6000_0000  # far is first in memory order
    mem = {far: target - 0xF00, near: target - 0x8, root: near - 0x10}

    def refs(low, high, keep, _progress):
        return [(a, v) for a, v in sorted(mem.items()) if low <= v <= high and keep(v)]

    chains = find_chains(target, base, base + 0x10000, refs, depth=2)
    assert Chain(root - base, (0x10, 0x8)) in chains


def test_keep_nearest_orders_by_offset_then_address():
    from cheatmaker.core.pointers import keep_nearest

    link = {0x30: (0, 0x100), 0x10: (0, 0x8), 0x20: (0, 0x8), 0x40: (0, 0x0)}
    assert keep_nearest([0x30, 0x10, 0x20, 0x40], link, 3) == [0x40, 0x10, 0x20]
