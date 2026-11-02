"""Find it next time / Find it again (architecture v3 §5): the one place that knows the
order of the methods and the rules for trusting an answer.

**Learn** ("Find it next time", once per game version) classifies a found value and
records the cheapest anchors that can hold:

* inside the game's module: **static**, nothing else needed;
* otherwise the instructions that touch it give the object and field offset, the
  object's class (vtable) is noted, a capture **hook** is planned and its slot proved,
  and optionally a short **pointer chain** search runs. A chain that still leads to
  the value after a restart ("proven") means the hook isn't needed: simple games stay
  simple.

**Find again** tries, in order: static → proven chains → hook slot → unproven
chains → vtable scan. Every answer is **verified** before anything may be written:
the object lies in writable non-code memory, starts with the saved vtable, and each
field holds a plausible value. If two methods give different objects, nothing is
written and the player picks (AC-12.3). If all fail, the caller opens the guided
search (AC-12.4). Each attempt logs one line (AC-9).
"""

from __future__ import annotations

import bisect
import logging
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from cheatmaker.console.client import MemMap, ProgressFn
from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import cave as cv
from cheatmaker.core import pointers as ptr
from cheatmaker.core import vtable_find as vf
from cheatmaker.core.project import (
    ChainAnchor,
    Field,
    HistoryEntry,
    HookFilter,
    ObjectSpec,
    Project,
    StaticAnchor,
    VtableAnchor,
)
from cheatmaker.core.session import Session
from cheatmaker.core.values import ValueType
from cheatmaker.errors import CheatMakerError, FindFailed, LockFailed, ReadFailed, ScanCancelled

log = logging.getLogger(__name__)

SLOT_WAIT_S = 10.0  # AC-12.1: found within 10 s by the game's code
SLOT_POLL_S = 0.25
MAX_CHAINS_KEPT = 20
MAX_OPTIONS = 4
COLLECT_SECONDS = 10.0

Sleep = Callable[[float], None]
Clock = Callable[[], float]


# -- verify ---------------------------------------------------------------------------
@dataclass
class MapIndex:
    """Writable, non-code maps of the game, for "is this a real object?" checks."""

    maps: list[MemMap]

    def __post_init__(self) -> None:
        self.maps = sorted(
            (m for m in self.maps if m.readable and m.writable and not m.executable),
            key=lambda m: m.start,
        )
        self._starts = [m.start for m in self.maps]

    def holds(self, addr: int, size: int) -> bool:
        i = bisect.bisect_right(self._starts, addr) - 1
        return i >= 0 and addr + size <= self.maps[i].end


def object_span(obj: ObjectSpec) -> int:
    return max([8, *(f.off + f.vtype.size for f in obj.fields)])


def verify(
    session: Session, obj: ObjectSpec, addr: int, maps: MapIndex | None = None
) -> str | None:
    """None if ``addr`` is believable as ``obj``, else the reason (for the log)."""
    game = session.require_game()
    client = session.require_client()
    if addr <= 0 or addr % 4:
        return "not an object address"
    maps = maps or MapIndex(client.maps(game.pid))
    if not maps.holds(addr, object_span(obj)):
        return "not in the game's writable memory"
    try:
        if obj.vtable is not None:
            (vt,) = struct.unpack("<Q", client.read(game.pid, addr, 8))
            if vt != game.module_base + obj.vtable:
                return "different kind of object"
        for f in obj.fields:
            if not f.plausible(client.read(game.pid, addr + f.off, f.vtype.size)):
                return f"{f.name} doesn't look right"
    except ReadFailed:
        return "can't be read"
    return None


# -- find again -------------------------------------------------------------------------
@dataclass(frozen=True)
class Found:
    method: str  # static | chain | hook_slot | vtable_scan
    address: int  # the object's start


@dataclass
class Outcome:
    status: str  # found | disagree | failed
    address: int | None = None
    method: str = ""
    seconds: float = 0.0
    options: list[Found] = field(default_factory=list)  # when they disagree
    changed: bool = False  # the object's anchors were updated (save the project)

    @property
    def found(self) -> bool:
        return self.status == "found"


METHOD_WORDS = {
    "static": "it doesn't move",
    "chain": "a memory path",
    "hook_slot": "the game's code",
    "vtable_scan": "a memory search",
    "picked": "your pick",
}


def _chain(a: ChainAnchor) -> ptr.Chain:
    return ptr.Chain(a.base, a.offsets)


def _resolve_chain(session: Session, obj: ObjectSpec, a: ChainAnchor, read_u64) -> int | None:
    try:
        f = obj.field(a.field)
    except KeyError:
        return None
    end = _chain(a).resolve(session.require_game().module_base, read_u64)
    return None if end is None else end - f.off


def find_again(
    session: Session,
    obj: ObjectSpec,
    *,
    use_hook: bool = True,
    use_scan: bool = True,
    slot_wait: float | None = None,
    learn_chains: bool = True,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> Outcome:
    """Find ``obj`` in the running game (see the module doc for the order).
    ``slot_wait``: how long to wait for the hook to run (default SLOT_WAIT_S)."""
    if slot_wait is None:
        slot_wait = SLOT_WAIT_S
    game = session.require_game()
    client = session.require_client()
    started = clock()
    maps = MapIndex(client.maps(game.pid))
    read_u64 = ptr.u64_reader(lambda a, n: client.read(game.pid, a, n))
    got: list[Found] = []

    def attempt(method: str, addr: int | None) -> bool:
        reason = "nothing there" if not addr else verify(session, obj, addr, maps)
        log.info(
            "action=refind object=%s method=%s address=%s result=%s seconds=%.1f",
            obj.id,
            method,
            f"{addr:#x}" if addr else "-",
            reason or "verified",
            clock() - started,
        )
        if reason is None and addr is not None:
            got.append(Found(method, addr))
            return True
        return False

    for a in obj.anchors_of(StaticAnchor):
        attempt("static", game.module_base + a.off)
    chains = obj.anchors_of(ChainAnchor)
    for a in (c for c in chains if c.runs_seen >= 2):
        attempt("chain", _resolve_chain(session, obj, a, read_u64))
    hook = obj.hook
    if hook is not None and use_hook:
        deadline = clock() + (0 if got else slot_wait)
        while True:
            try:
                addr, _count = ah.read_slot(session, hook)
            except ReadFailed:
                addr = 0
            if addr and attempt("hook_slot", addr):
                break
            if clock() >= deadline or (cancel is not None and cancel.is_set()):
                if not addr:
                    attempt("hook_slot", 0)
                break
            sleep(SLOT_POLL_S)
    if not got:
        for a in (c for c in chains if c.runs_seen < 2):
            attempt("chain", _resolve_chain(session, obj, a, read_u64))
    if not got and use_scan and obj.vtable is not None and obj.anchors_of(VtableAnchor):
        for addr in vf.find_by_vtable(session, obj.vtable, progress, cancel):
            attempt("vtable_scan", addr)
    seconds = clock() - started
    distinct = sorted({f.address for f in got})
    if not distinct:
        log.info("action=refind object=%s result=failed seconds=%.1f", obj.id, seconds)
        return Outcome("failed", seconds=seconds)
    if len(distinct) > 1:
        options = [next(f for f in got if f.address == a) for a in distinct][:MAX_OPTIONS]
        log.info("action=refind object=%s result=disagree options=%d", obj.id, len(distinct))
        return Outcome("disagree", seconds=seconds, options=options)
    first = got[0]
    changed = learn_chains and _update_chains(session, obj, first.address, read_u64)
    log.info(
        "action=refind object=%s result=found method=%s address=%#x seconds=%.1f",
        obj.id,
        first.method,
        first.address,
        seconds,
    )
    return Outcome("found", first.address, first.method, seconds, changed=changed)


def _update_chains(session: Session, obj: ObjectSpec, addr: int, read_u64) -> bool:
    """A chain that leads to the object again survived a restart; others are dropped.
    (Proven chains that disagree never get here: that's a disagreement.)"""
    chains = obj.anchors_of(ChainAnchor)
    if not chains:
        return False
    keep = []
    for a in chains:
        if _resolve_chain(session, obj, a, read_u64) == addr:
            a.runs_seen += 1
            keep.append(a)
    obj.anchors = [a for a in obj.anchors if not isinstance(a, ChainAnchor) or a in keep]
    log.info("action=refind_chains object=%s before=%d kept=%d", obj.id, len(chains), len(keep))
    return True


def confirm_pick(obj: ObjectSpec, addr: int) -> Outcome:
    """The player picked one of the disagreeing answers by watching it change."""
    return Outcome("found", addr, "picked")


def remember(project: Project, outcome: Outcome) -> None:
    if outcome.status in ("found", "failed"):
        project.remember(
            HistoryEntry(
                datetime.now().isoformat(timespec="seconds"),
                outcome.method or "none",
                outcome.seconds,
                outcome.found,
            )
        )


# -- learn ("Find it next time") -----------------------------------------------------------
@dataclass
class Learned:
    obj: ObjectSpec
    methods: list[str]  # what worked, in plain order: static / chain / hook_slot / vtable_scan
    notes: list[str] = field(default_factory=list)  # why something didn't work (plain)


def default_range(vtype: ValueType, value: float | int) -> tuple[float, float]:
    """Plausible values for a field: from 0 (or 10x a negative value) to 10x the value,
    at least 10,000 (spec [6]: float health 0-10,000)."""
    lo = min(0, value * 10)
    hi = max(10_000, value * 10)
    if not vtype.is_float:
        lo_t, hi_t = vtype.int_range()
        lo, hi = max(lo_t, int(lo)), min(hi_t, int(hi))
    return (lo, hi)


def widen(f: Field, value: float | int) -> None:
    """Make sure the value a cheat writes still counts as plausible."""
    if f.range is not None and not f.range[0] <= value <= f.range[1]:
        f.range = (min(f.range[0], value), max(f.range[1], value))


def learn(
    session: Session,
    address: int,
    vtype: ValueType,
    label: str,
    obj_id: str,
    *,
    watch_seconds: float = ah.WATCH_SECONDS,
    prove_seconds: float = ah.PROVE_SECONDS,
    collect_seconds: float = COLLECT_SECONDS,
    pointer_scan: bool = False,
    step: Callable[[str], None] | None = None,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> Learned:
    """Build every anchor that can hold for the value at ``address``. Raises FindFailed
    when none can (AC-11.3)."""
    game = session.require_game()
    client = session.require_client()
    say = step or (lambda _s: None)
    value = vtype.decode(client.read(game.pid, address, vtype.size))
    fld = Field(label, 0, vtype, default_range(vtype, value), value)
    if game.module_base <= address < game.module_end:
        obj = ObjectSpec(obj_id, label, None, [fld], [StaticAnchor(address - game.module_base)])
        log.info("action=learn object=%s kind=static off=%#x", obj_id, address - game.module_base)
        return Learned(obj, ["static"])
    notes: list[str] = []
    obj = ObjectSpec(obj_id, label, None, [fld])
    methods: list[str] = []
    obj_addr = address

    say("watch")
    cands = ah.find_hooks(session, address, vtype.size, watch_seconds)
    if cands:
        best = cands[0]
        obj_addr = best.object_for(address)
        fld.off = best.disp
        obj.vtable = ah.object_vtable(session, obj_addr)
        fallback = None
        if obj.vtable is None:
            say("collect")
            fallback = _field_filter(session, best, obj_addr, collect_seconds, cancel)
        try:
            hook = ah.plan_hook(session, best, obj.vtable, fallback)
            say("prove")
            ah.prove_slot(session, game.module_base + hook.slot, prove_seconds)
        except (FindFailed, LockFailed) as exc:
            notes.append(str(exc))
        else:
            obj.anchors.append(hook)
            methods.append("hook_slot")
        if obj.vtable is not None:
            obj.anchors.append(VtableAnchor())
            methods.append("vtable_scan")
    else:
        notes.append("Nothing in the game touched the value while we watched.")
    if pointer_scan:
        say("pointers")
        try:
            chains = ptr.scan_session(session, address, progress=progress, cancel=cancel)
        except ScanCancelled:
            chains = []
            notes.append("The memory path search was cancelled.")
        for c in chains[:MAX_CHAINS_KEPT]:
            obj.anchors.append(ChainAnchor(c.base_offset, c.offsets, label))
        if chains:
            methods.append("chain")
    if not obj.anchors:
        raise FindFailed(
            f"Couldn't find a way to find {label} again. It still works for this session; "
            "try Lock it to make it stick. " + " ".join(notes)
        )
    log.info(
        "action=learn object=%s address=%#x object_at=%#x field=%#x vtable=%s methods=%s",
        obj_id,
        address,
        obj_addr,
        fld.off,
        "-" if obj.vtable is None else f"{obj.vtable:#x}",
        ",".join(methods),
    )
    return Learned(obj, methods, notes)


def _field_filter(
    session: Session,
    cand: ah.HookCandidate,
    obj_addr: int,
    seconds: float,
    cancel: threading.Event | None,
) -> HookFilter:
    """No vtable: see which objects the instruction touches (the Player-only collect)
    and pick the field that marks ours. Only ours seen: no filter needed."""
    from cheatmaker.core.locker import Writer

    writer = Writer(cand.address, cand.code, cand.text, cand.hits)
    try:
        objs = cv.collect_objects(session, writer, seconds, cancel)
        others = [o for o in objs if o != obj_addr]
        if not others:
            return HookFilter("none")
        guesses = cv.compare_objects(session, obj_addr, objs)
    except CheatMakerError as exc:
        log.info("action=learn collect failed: %s", exc)
        return HookFilter("none")
    if not guesses:
        return HookFilter("none")
    g = guesses[0]
    return HookFilter("field", g.offset, g.player_value)


# -- keep it right while freezing ---------------------------------------------------------
class Tracker:
    """Where one object is right now, for the freezer (AC-12.5). Every call re-checks
    the object (vtable + fields); when that fails it re-finds with the fast methods
    only (no waiting, no memory search) and returns None until something verifies."""

    def __init__(self, session: Session, obj: ObjectSpec, address: int | None = None) -> None:
        self.session = session
        self.obj = obj
        self.address = address
        self.confirmed = False  # the player said "yes, that's it" this run (AC-12.2)
        self._lock = threading.Lock()

    def current(self) -> int | None:
        with self._lock:
            if self.address is not None and verify(self.session, self.obj, self.address) is None:
                return self.address
            old, self.address = self.address, None
            out = find_again(
                self.session, self.obj, use_scan=False, slot_wait=0, learn_chains=False
            )
            if out.found:
                self.address = out.address
                log.info(
                    "action=tracker object=%s moved from=%s to=%#x",
                    self.obj.id,
                    f"{old:#x}" if old else "-",
                    out.address,
                )
            return self.address

    def field_address(self, name: str) -> int | None:
        base = self.current()
        return None if base is None else base + self.obj.field(name).off


__all__ = [
    "Found",
    "Learned",
    "MapIndex",
    "Outcome",
    "Tracker",
    "confirm_pick",
    "default_range",
    "find_again",
    "learn",
    "remember",
    "verify",
    "widen",
]
