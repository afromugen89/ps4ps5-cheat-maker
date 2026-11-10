"""Groups are ready when the matches are listed: values are kept from RECORD_LIMIT
matches down (not only from GROUP_LIMIT), and "Same" scans add nothing to wait for."""

from __future__ import annotations

import struct

import pytest

from cheatmaker.core import scanner as scanner_mod
from cheatmaker.core.grouping import group_matches
from cheatmaker.core.scanner import GROUP_LIMIT, RECORD_LIMIT, Scanner
from cheatmaker.core.values import Compare, ValueType
from tests.conftest import HEAP_BASE, PID

F = ValueType.FLOAT
HP = HEAP_BASE + 0x400  # health, max 300
HP100 = HEAP_BASE + 0x404  # health / 100, next to it
FRAC = HEAP_BASE + 0x408  # health / 300
PCT = HEAP_BASE + 0x40C  # health as a percent
NOISE = [HEAP_BASE + 0x800, HEAP_BASE + 0x810]  # other values that also go down


def f32(v: float) -> float:
    return struct.unpack("<f", struct.pack("<f", v))[0]


def show(fake, hp: float, noise: tuple[float, float]) -> None:
    for a, v in ((HP, hp), (HP100, hp / 100), (FRAC, hp / 300), (PCT, hp / 3)):
        fake.poke(PID, a, struct.pack("<f", v))
    for a, v in zip(NOISE, noise, strict=True):
        fake.poke(PID, a, struct.pack("<f", v))


def test_record_limit_is_well_above_the_group_limit():
    assert RECORD_LIMIT >= 10 * GROUP_LIMIT


def test_groups_show_as_soon_as_few_enough_are_left(session, fake, monkeypatch):
    """Kein's report: the list came up, but the copies only folded into groups after
    several more scans. The changes made while there were still too many to group
    now count, so the groups are there the moment the count drops low enough."""
    monkeypatch.setattr(scanner_mod, "GROUP_LIMIT", 4)
    show(fake, 250, (90.0, 400.0))
    sc = Scanner(session)
    assert sc.first_scan(None, F) > 4 and sc.recorded == 1, "values kept from the start"
    show(fake, 190, (80.0, 300.0))
    assert sc.next_scan(Compare.DECREASED) == 6 and sc.groups() == [], "too many to group"
    show(fake, 170, (70.0, 300.0))
    assert sc.next_scan(Compare.DECREASED) == 5 and sc.recorded == 3
    show(fake, 170, (60.0, 300.0))  # the other value moves: Same drops it
    assert sc.next_scan(Compare.UNCHANGED) == 4
    assert sc.changes_to_group == 0
    [g] = sc.groups()
    assert {m.address for m in g.members} == {HP, HP100, FRAC, PCT}


def test_same_scans_are_not_kept(session, fake):
    show(fake, 250, (90.0, 400.0))
    sc = Scanner(session)
    sc.first_scan(None, F)
    show(fake, 190, (80.0, 300.0))
    sc.next_scan(Compare.DECREASED)
    assert sc.recorded == 2 and sc.changes_to_group == 1
    for _ in range(3):
        sc.next_scan(Compare.UNCHANGED)
    assert sc.recorded == 2, "nothing moved: nothing new to group with"
    assert all(len(v) == 2 for v in sc.values.values())
    assert sc.changes_to_group == 1, "Same never brings the groups closer"
    show(fake, 170, (70.0, 300.0))
    sc.next_scan(Compare.DECREASED)
    assert sc.recorded == 3 and sc.changes_to_group == 0
    assert any(len(g.members) == 4 for g in sc.groups())


def test_too_many_to_record_starts_again(session, fake, monkeypatch):
    monkeypatch.setattr(scanner_mod, "RECORD_LIMIT", 3)
    sc = Scanner(session)
    sc.first_scan(None, F)
    assert sc.recorded == 0 and sc.values == {}


@pytest.mark.parametrize("order", [(190, 170, 190), (190, 170, 150)])
def test_returnal_health_forms_group_together(order):
    """Kein's Returnal scans 57 to 60 (max health 300): health / 100, the fraction, the
    percent and the raw value are one group; 100.7 and 426.4 move in the same scans but
    by a different factor, so they stay out of it."""
    h = {}
    hp = [float(v) for v in order]
    forms = {
        0x1121187A54: [v / 100 for v in hp],  # 1.9, 1.7
        0x13A1CCED00: [v / 100 for v in hp],
        0x10C0F89430: [v / 300 for v in hp],  # 0.633, 0.567
        0x14A0E66354: [v / 300 for v in hp],
        0x12E0EA49D0: hp,  # 190, 170
        0x12E0EA49DC: [v / 3 for v in hp],  # 63.3, 56.7
    }
    for a, vals in forms.items():
        h[(a, F)] = [f32(v) for v in vals]
    h[(0x1281176AF8, F)] = [f32(v) for v in (100.7, 98.7, 100.7 if order[2] > 170 else 96.7)]
    h[(0x12C0FB5CC4, F)] = [f32(v) for v in (426.4, 424.4, 426.4 if order[2] > 170 else 422.4)]
    groups = group_matches(h)
    health = next(g for g in groups if g.head.address in forms)
    assert {m.address for m in health.members} == set(forms)
    assert health.label == "likely real", "health sits next to its percent"
