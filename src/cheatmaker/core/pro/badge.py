"""Where a cheat can run (spec-pro §5, AC-S2): CheatRunner, or only with the PC app.

A pure function of the compiled ``Cheat`` (architecture-pro decision 3). CheatRunner
only writes fixed bytes inside the game's module, once, when a cheat is turned on;
anything else needs Cheat Maker running. Cheat files (export) hold only CheatRunner
cheats; the rest are listed with their reasons.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cheatmaker.core.cheats import Cheat
from cheatmaker.core.project import ChainAnchor, Project

CHEATRUNNER = "CheatRunner"
PC_ONLY = "PC only"
Target = Literal["CheatRunner", "PC only"]

HEAP_ADDRESS = "heap address"
FREEZE = "freeze"
WRITTEN_ONCE = "written once (CheatRunner can't freeze)"
"""Note on a CheatRunner cheat that freezes a value in the game's module: CheatRunner
writes it once when turned on (decision: option C, keeps guided TC-60 unchanged)."""
POINTER_PATH = "pointer path"
FOUND_AGAIN = "found again each run"
NOTHING = "nothing to write"


@dataclass(frozen=True)
class Badge:
    target: Target
    reasons: tuple[str, ...] = ()
    """Why a cheat is PC only, in a fixed order; empty for CheatRunner."""
    notes: tuple[str, ...] = ()
    """What changes when CheatRunner runs a CheatRunner cheat (e.g. ``WRITTEN_ONCE``)."""

    @property
    def runs_on_console(self) -> bool:
        return self.target == CHEATRUNNER

    @property
    def tooltip(self) -> str:
        if self.runs_on_console:
            text = "Fixed bytes in the game's code or data: CheatRunner runs it with the PC off."
            return text + "".join(f"\nNote: {n}." for n in self.notes)
        return "Needs Cheat Maker running: " + ", ".join(self.reasons) + "."


def badge(cheat: Cheat, project: Project | None = None) -> Badge:
    """The cheat's badge. ``project`` (the game's .cmproj) tells a pointer path from
    the other ways a value is found again; without it they all say "found again".

    CheatRunner = ``saveable and not found_again``. A freeze alone doesn't make a cheat
    PC only: a frozen value in the game's module is written once by CheatRunner (noted);
    a freeze is listed as a reason only when the cheat is PC only anyway."""
    reasons = []
    if not cheat.locations:
        reasons.append(NOTHING)
    if cheat.anchor is None and any(loc.moving for loc in cheat.locations):
        reasons.append(HEAP_ADDRESS)
    if cheat.anchor is not None or cheat.learned_parts:
        reasons.append(POINTER_PATH if _only_chains(cheat, project) else FOUND_AGAIN)
    if reasons:
        if cheat.freeze:
            reasons.insert(
                reasons.index(HEAP_ADDRESS) + 1 if HEAP_ADDRESS in reasons else 0, FREEZE
            )
        return Badge(PC_ONLY, tuple(reasons))
    return Badge(CHEATRUNNER, notes=(WRITTEN_ONCE,) if cheat.freeze else ())


def _only_chains(cheat: Cheat, project: Project | None) -> bool:
    """Every object the cheat is found again by is reached by memory paths alone."""
    if project is None:
        return False
    for oid in cheat.objects:
        try:
            anchors = project.object(oid).anchors
        except KeyError:
            return False
        if not anchors or not all(isinstance(a, ChainAnchor) for a in anchors):
            return False
    return bool(cheat.objects)


def runner_cheats(cheats: list[Cheat]) -> list[Cheat]:
    """The cheats a cheat file can hold, in order."""
    return [c for c in cheats if badge(c).runs_on_console]


def pc_only(cheats: list[Cheat], project: Project | None = None) -> list[tuple[Cheat, Badge]]:
    """The cheats a cheat file leaves out, each with its badge (the export dialog's list)."""
    out = []
    for c in cheats:
        b = badge(c, project)
        if not b.runs_on_console:
            out.append((c, b))
    return out
