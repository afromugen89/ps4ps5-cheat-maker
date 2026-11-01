"""Pointer scan: find paths from the game's fixed memory to a value that moves.

Returnal puts health at a new heap address every run, but the game's own module
(loaded at the same base each time) holds pointers that lead to it. A chain is::

    address = module_base + base_offset
    for off in offsets:
        address = read_u64(address) + off

The search runs backwards from the target, one level at a time. Each level asks the
console for every 8-byte value that points at most ``max_offset`` below one of the
current nodes ("who points near here?"). Holders inside the module are roots; the
others become the next level's nodes.

Chains are only useful for live Set / Freeze: GoldHEN / CheatRunner files hold plain
byte patches at fixed offsets, so a pointer chain can't be saved into them.
"""

from __future__ import annotations

import bisect
import json
import logging
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cheatmaker.console.client import ProgressFn
from cheatmaker.core.session import Session
from cheatmaker.errors import ReadFailed, ScanCancelled

log = logging.getLogger(__name__)

DEFAULT_DEPTH = 3
DEFAULT_MAX_OFFSET = 0x1000
MAX_NODES_PER_LEVEL = 20_000
"""More nodes than this per level and the search would take hours; keep the nearest."""
MAX_SPAN = 1 << 30
"""One console pass covers node addresses this far apart at most (see _windows)."""
MAX_WINDOWS = 3
MAX_CHAINS = 2_000

FindRefs = Callable[[int, int, Callable[[int], bool], ProgressFn | None], list[tuple[int, int]]]
"""(low, high, keep, progress) -> [(holder_address, value)] for 8-byte values in
[low, high] that ``keep(value)`` accepts."""

ReadU64 = Callable[[int], int]


@dataclass(frozen=True)
class Chain:
    base_offset: int  # from the module base
    offsets: tuple[int, ...]

    @property
    def depth(self) -> int:
        return len(self.offsets)

    def resolve(self, module_base: int, read_u64: ReadU64) -> int | None:
        addr = module_base + self.base_offset
        try:
            for off in self.offsets:
                ptr = read_u64(addr)
                if ptr == 0:
                    return None
                addr = ptr + off
        except ReadFailed:
            return None
        return addr

    def label(self) -> str:
        """Cheat Engine style: [[base+0x123]+0x10]+0x8"""
        text = f"base+{self.base_offset:#x}"
        for off in self.offsets:
            text = f"[{text}]+{off:#x}"
        return text

    def to_json(self) -> dict:
        return {"base": f"{self.base_offset:#x}", "offsets": [f"{o:#x}" for o in self.offsets]}

    @classmethod
    def from_json(cls, d: dict) -> Chain:
        return cls(int(d["base"], 16), tuple(int(o, 16) for o in d["offsets"]))


def _windows(nodes: list[int], max_offset: int) -> list[tuple[int, int]]:
    """Group sorted node addresses into console passes. Each pass asks for values in
    [low, high]; nodes far apart go in separate passes so one pass doesn't return
    every pointer in the game. Keeps the passes holding the most nodes."""
    groups: list[list[int]] = []
    for n in nodes:
        if groups and n - groups[-1][0] <= MAX_SPAN:
            groups[-1].append(n)
        else:
            groups.append([n])
    groups.sort(key=len, reverse=True)
    if len(groups) > MAX_WINDOWS:
        log.info("action=pointer_scan dropped %d far-away node groups", len(groups) - MAX_WINDOWS)
    return sorted((g[0] - max_offset, g[-1]) for g in groups[:MAX_WINDOWS])


def find_chains(
    target: int,
    module_base: int,
    module_end: int,
    find_refs: FindRefs,
    depth: int = DEFAULT_DEPTH,
    max_offset: int = DEFAULT_MAX_OFFSET,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> list[Chain]:
    """Chains from the module to ``target``, shortest first."""
    link: dict[int, tuple[int, int]] = {}  # holder -> (node it points near, offset)
    roots: list[int] = []
    frontier = [target]
    seen = {target}
    for level in range(depth):
        if cancel is not None and cancel.is_set():
            raise ScanCancelled("pointer scan cancelled")
        nodes = sorted(frontier)

        def nearest(value: int, nodes: list[int] = nodes) -> int | None:
            i = bisect.bisect_left(nodes, value)
            if i < len(nodes) and nodes[i] - value <= max_offset:
                return nodes[i]
            return None

        def keep(value: int, nearest=nearest) -> bool:
            return nearest(value) is not None

        windows = _windows(nodes, max_offset)
        refs: list[tuple[int, int]] = []
        for w, (lo, hi) in enumerate(windows):

            def sub(p: float, w: int = w, level: int = level, nw: int = len(windows)) -> None:
                if progress:
                    progress((level + (w + p) / nw) / depth)

            refs.extend(find_refs(max(lo, 0), hi, keep, sub))
        nxt: list[int] = []
        for holder, value in refs:
            if holder in seen:
                continue
            node = nearest(value)
            if node is None:
                continue
            seen.add(holder)
            link[holder] = (node, node - value)
            if module_base <= holder < module_end:
                roots.append(holder)
            else:
                nxt.append(holder)
        log.info(
            "action=pointer_level level=%d nodes=%d windows=%d refs=%d roots=%d next=%d",
            level + 1,
            len(nodes),
            len(windows),
            len(refs),
            len(roots),
            len(nxt),
        )
        if len(nxt) > MAX_NODES_PER_LEVEL:
            nxt = nxt[:MAX_NODES_PER_LEVEL]
        frontier = nxt
        if not frontier:
            break
    if progress:
        progress(1.0)
    chains = []
    for root in roots:
        offsets, node = [], root
        while node != target:
            nxt_node, off = link[node]
            offsets.append(off)
            node = nxt_node
        chains.append(Chain(root - module_base, tuple(offsets)))
    chains.sort(key=lambda c: (c.depth, sum(c.offsets)))
    return chains[:MAX_CHAINS]


def recheck(chains: list[Chain], target: int, module_base: int, read_u64: ReadU64) -> list[Chain]:
    """After a restart: keep the chains that still lead to the (new) target."""
    cache: dict[int, int] = {}

    def cached(addr: int) -> int:
        if addr not in cache:
            cache[addr] = read_u64(addr)
        return cache[addr]

    return [c for c in chains if c.resolve(module_base, cached) == target]


def u64_reader(read: Callable[[int, int], bytes]) -> ReadU64:
    return lambda addr: struct.unpack("<Q", read(addr, 8))[0]


def scan_session(
    session: Session,
    target: int,
    depth: int = DEFAULT_DEPTH,
    max_offset: int = DEFAULT_MAX_OFFSET,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> list[Chain]:
    """Pointer scan on the attached game (each level is one console pass)."""
    client = session.require_client()
    game = session.require_game()
    regions = session.scannable_regions()

    def refs(low: int, high: int, keep, sub: ProgressFn | None) -> list[tuple[int, int]]:
        return client.find_pointers(game.pid, regions, low, high, keep, sub, cancel)

    chains = find_chains(
        target, game.module_base, game.module_end, refs, depth, max_offset, progress, cancel
    )
    log.info("action=pointer_scan target=%#x chains=%d", target, len(chains))
    return chains


def resolve_live(session: Session, chain: Chain) -> int | None:
    client = session.require_client()
    game = session.require_game()
    return chain.resolve(game.module_base, u64_reader(lambda a, n: client.read(game.pid, a, n)))


def recheck_session(session: Session, chains: list[Chain], target: int) -> list[Chain]:
    client = session.require_client()
    game = session.require_game()
    reader = u64_reader(lambda a, n: client.read(game.pid, a, n))
    kept = recheck(chains, target, game.module_base, reader)
    log.info("action=pointer_recheck target=%#x before=%d after=%d", target, len(chains), len(kept))
    return kept


def chains_path(title_id: str, version: str) -> Path:
    from cheatmaker.paths import app_data_dir

    return app_data_dir() / "pointers" / f"{title_id}_{version}.json"


# -- saved lists: one file per game version ------------------------------------
def save_chains(path: Path, title_id: str, version: str, chains: list[Chain]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"title_id": title_id, "version": version, "chains": [c.to_json() for c in chains]}
    path.write_text(json.dumps(doc, indent=1))


def load_chains(path: Path) -> list[Chain]:
    if not path.exists():
        return []
    return [Chain.from_json(d) for d in json.loads(path.read_text())["chains"]]


__all__ = [
    "Chain",
    "chains_path",
    "find_chains",
    "load_chains",
    "recheck",
    "recheck_session",
    "resolve_live",
    "save_chains",
    "scan_session",
    "u64_reader",
]
