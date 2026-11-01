"""Cheat templates (AC-5.1, AC-5.2): turn a found value into a named cheat."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.values import ValueType


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    freeze: bool
    hint: str
    default: Callable[[float | int, ValueType], float | int]


def _keep(current: float | int, vt: ValueType) -> float | int:
    return current


def _big(cap: int) -> Callable[[float | int, ValueType], float | int]:
    def default(current: float | int, vt: ValueType) -> float | int:
        return vt.clamp(max(cap, current))

    return default


TEMPLATES: tuple[Template, ...] = (
    Template(
        "infinite_health", "Infinite Health", True, "Keeps your health where it is now.", _keep
    ),
    Template(
        "infinite_money",
        "Infinite Money",
        True,
        "Sets your money high and keeps it there.",
        _big(999_999),
    ),
    Template("infinite_ammo", "Infinite Ammo", True, "Keeps your ammo where it is now.", _keep),
    Template(
        "max_skill_points",
        "Max Skill Points",
        False,
        "Gives you lots of skill points once.",
        _big(99),
    ),
    Template(
        "set_number", "Set to a Number", False, "Sets the value to a number you choose.", _keep
    ),
)

BY_KEY = {t.key: t for t in TEMPLATES}


def make_cheat(
    template: Template,
    location_kind: LocationKind,
    offset: int,
    vtype: ValueType,
    current: bytes,
    value: float | int | None = None,
) -> Cheat:
    """Build a cheat from a found value.

    ``offset`` is a module offset for MODULE_VALUE, or the live address for
    SESSION_ADDRESS. ``current`` (the value now) becomes the "off" bytes.
    """
    if location_kind is LocationKind.CODE_PATCH:
        raise ValueError("templates hold values; code patches come from Lock it")
    cur = vtype.decode(current)
    target = template.default(cur, vtype) if value is None else value
    loc = Location(location_kind, offset, vtype.encode(target), bytes(current[: vtype.size]))
    return Cheat(
        name=template.name,
        locations=[loc],
        template=template.key,
        freeze=template.freeze,
        value_type=vtype,
        hint=template.hint,
    )
