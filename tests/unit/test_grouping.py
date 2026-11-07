"""Grouping linked copies (spec-health-finding AC-G1 to AC-G4)."""

from __future__ import annotations

import struct

from cheatmaker.core.grouping import (
    COPIES,
    LIKELY,
    STILL,
    Member,
    group_matches,
    link_ratio,
)
from cheatmaker.core.values import FRACTION, PERCENT, ValueType

F, W = ValueType.FLOAT, ValueType.INT32  # decimal, whole number


def f32(x: float) -> float:
    """``x`` as the game's 4-byte decimal reads back."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def hist(*rows: tuple[int, list[float | None]], vt: ValueType = F) -> dict:
    return {(a, vt): [None if v is None else f32(v) for v in vals] for a, vals in rows}


HEALTH = [90.0, 65.0, 40.0]
FRAC = [0.9, 0.65, 0.4]
THIRD = [30.0, 21.667, 13.333]
SMALL = [0.3, 0.2167, 0.1333]


def kein_scan_12() -> dict:
    """Shaped like Kein's Returnal scan 12 (PPSA01285 03.000.005): health, its fraction
    and health / 3 side by side; display copies far away; values that move on their own."""
    h = hist(
        (0x12E0EA40A0, FRAC),
        (0x12E0EA40B0, HEALTH),
        (0x12E0EA40BC, THIRD),
        # UI copies of the fraction, far apart
        *[(a, FRAC) for a in (0x1121177F94, 0x132132B0D8, 0x1380E1E2AC)],
        *[(a, FRAC) for a in (0x13A1D1FAF0, 0x13A1D1FAF4, 0x13C120BEF0)],
        # copies of health / 300
        *[(a, SMALL) for a in (0x10C0F99040, 0x10C0F99348, 0x11211201D0, 0x11211282C8)],
        # unrelated values that changed in the same scans
        (0x1281176264, [293.9, 293.9, 267.4]),
        (0x128117626C, [34.45, 34.45, 21.2]),
    )
    # More display copies of the health fraction and health, as a HUD and menus keep.
    for k in range(30):
        h[(0x1400000000 + k * 0x20000, F)] = [f32(v) for v in FRAC]
    for k in range(12):
        h[(0x1500000000 + k * 0x20000, F)] = [f32(v) for v in HEALTH]
    # Two that didn't change in the recorded scans.
    h[(0x1600000000, F)] = [f32(5.0)] * 3
    h[(0x1600000100, F)] = [f32(7.5)] * 3
    return h


def test_kein_scan_12_folds_to_a_few_rows_with_real_health_first():
    history = kein_scan_12()
    assert 55 <= len(history) <= 65
    tags = {k: (FRACTION if v[0] < 1 else PERCENT) for k, v in history.items()}
    groups = group_matches(history, tags)
    assert len(groups) <= 8
    health = next(g for g in groups if any(m.address == 0x12E0EA40B0 for m in g.members))
    assert health.head.address in (0x12E0EA40B0, 0x12E0EA40A0)
    assert health.head_likely and health.label == LIKELY
    assert {m.address for m in health.members} >= {0x12E0EA40BC, 0x1121177F94, 0x10C0F99040}
    copies = {m.address for i, m in enumerate(health.members) if i not in health.likely}
    assert 0x1121177F94 in copies, "a far-away display copy is not likely real"
    unrelated = {m.address for g in groups for m in g.members if g is not health}
    assert {0x1281176264, 0x128117626C} <= unrelated
    assert [g.label for g in groups[-2:]] == [STILL, STILL], "unchanged ones come last"
    assert sum(len(g.members) for g in groups) == len(history), "nothing is dropped"


def test_link_needs_same_factor_every_scan():
    a = Member(1, F, None, (90.0, 65.0, 40.0))
    b = Member(2, F, None, (0.9, 0.65, 0.4))
    c = Member(3, F, None, (293.9, 293.9, 267.4))
    assert abs(link_ratio(a, b) - 0.01) < 1e-9
    assert link_ratio(a, c) is None
    assert link_ratio(a, Member(4, F, None, (90.0, 64.0, 40.0))) is None, "1.5% off"
    assert link_ratio(a, Member(5, F, None, (90.0, None, 40.0))) is not None, "unknown skipped"
    assert link_ratio(a, Member(6, F, None, (90.0, 0.0, 40.0))) is None, "0 on one side only"
    assert link_ratio(a, Member(7, F, None, (90.0, None, None))) is None, "needs 2 scans"
    still = Member(8, F, None, (1.0, 1.0, 1.0))
    assert link_ratio(still, Member(9, F, None, (2.0, 2.0, 2.0))) is None, "both must change"


def test_far_apart_same_values_are_display_copies():
    h = hist(*[(0x1000_0000 + k * 0x20000, FRAC) for k in range(3)])
    [g] = group_matches(h)
    assert g.label == COPIES and g.copies == 2 and not g.likely


def test_close_same_values_are_not_called_display_copies():
    h = hist((0x1000, FRAC), (0x1100, FRAC))
    [g] = group_matches(h)
    assert g.label == "" and g.copies == 1


def test_likely_real_comes_first_then_smaller_groups():
    h = hist(
        *[(0x1000_0000 + k * 0x20000, [5.0, 4.0, 3.0]) for k in range(2)],  # copies, no likely
        (0x2000, HEALTH),
        (0x2010, FRAC),
        *[(0x3000_0000 + k * 0x20000, FRAC) for k in range(3)],
    )
    groups = group_matches(h)
    assert groups[0].head_likely and groups[0].head.address in (0x2000, 0x2010)
    assert groups[1].head.address == 0x1000_0000 and groups[1].copies == 1


def test_neighbour_that_changes_with_it_marks_likely_real():
    """Health next to a value that moves in the same scans at another scale (e.g. max
    shield) is likely real even when that neighbour isn't linked to it."""
    h = hist((0x5000, [100.0, 80.0, 80.0]), (0x5008, [7.0, 3.0, 3.0]), (0x9_0000, [1.0, 2.0, 3.0]))
    groups = group_matches(h)
    likely = {g.head.address for g in groups if g.head_likely}
    assert likely == {0x5000, 0x5008}


def test_one_value_seen_as_two_types_is_not_its_own_neighbour():
    h = {(0x40, F): [f32(1.5), f32(2.5)], (0x40, W): [1069547520, 1075838976]}
    groups = group_matches(h)
    assert not any(g.likely for g in groups)


def test_member_factors_are_relative_to_the_head():
    h = hist((0x12E0EA40A0, FRAC), (0x12E0EA40B0, HEALTH))
    [g] = group_matches(h)
    assert g.factors[0] == 1.0
    other = g.factors[1]
    assert abs(other - 100) < 0.01 or abs(other - 0.01) < 1e-6


def test_empty_and_single():
    assert group_matches({}) == []
    [g] = group_matches(hist((0x10, [1.0, 2.0])))
    assert g.copies == 0 and g.label == ""


# -- the same shape through the FakeConsole and the scanner -----------------------------
def kein_scans() -> dict[int, list[float]]:
    """Address -> value at each of 3 scans (all went down), shaped like Kein's scan 12."""
    rows = {
        0x12E0EA40A0: FRAC,
        0x12E0EA40B0: HEALTH,
        0x12E0EA40BC: THIRD,
        **{a: FRAC for a in (0x1121177F94, 0x132132B0D8, 0x1380E1E2AC)},
        **{a: FRAC for a in (0x13A1D1FAF0, 0x13A1D1FAF4, 0x13C120BEF0)},
        **{a: SMALL for a in (0x10C0F99040, 0x10C0F99348, 0x11211201D0, 0x11211282C8)},
        0x1281176264: [300.0, 293.9, 267.4],
        0x128117626C: [40.0, 34.45, 21.2],
        0x1700000000: [10.0, 9.0, 8.0],  # a timer
        0x1700100000: [5.5, 3.25, -1.0],  # physics
    }
    rows.update({0x1400000000 + k * 0x20000: FRAC for k in range(30)})
    rows.update({0x1500000000 + k * 0x20000: HEALTH for k in range(12)})
    return rows


def kein_fake():
    """A FakeConsole holding those values in small regions (nearby ones share one)."""
    from tests.conftest import PID, build_fake

    fake = build_fake("PPSA01285")
    scans = kein_scans()
    addrs = sorted(scans)
    start = addrs[0]
    for a, b in zip([None, *addrs], [*addrs, None], strict=True):
        if a is not None and (b is None or b - a > 0x1000):
            fake.add_region(PID, "[anon]", start, a + 4 - start)
        if b is not None and (a is None or b - a > 0x1000):
            start = b
    return fake, scans


def poke_scan(fake, scans: dict[int, list[float]], i: int) -> None:
    from tests.conftest import PID

    for a, vals in scans.items():
        fake.poke(PID, a, struct.pack("<f", vals[i]))


def test_kein_scan_12_on_the_fake_console_shows_few_rows():
    from cheatmaker.core.scanner import Scanner
    from cheatmaker.core.session import Session
    from cheatmaker.core.values import Compare

    fake, scans = kein_fake()
    s = Session(lambda ip, port: fake)
    s.connect("192.168.1.50")
    s.refresh_game()
    sc = Scanner(s)
    poke_scan(fake, scans, 0)
    sc.first_scan(None, F)
    for i in (1, 2):
        poke_scan(fake, scans, i)
        sc.next_scan(Compare.DECREASED)
    assert sc.count == len(scans) == 59
    groups = sc.groups()
    assert len(groups) <= 8
    first = groups[0] if groups[0].copies else next(g for g in groups if g.copies)
    assert first.head.address in (0x12E0EA40B0, 0x12E0EA40A0) and first.head_likely
    assert sum(len(g.members) for g in groups) == 59


def test_close_values_that_move_differently_are_not_linked():
    a = Member(1, F, None, (900.0, 880.0, 860.0))
    b = Member(2, F, None, (905.0, 870.0, 860.0))  # within 1% each scan, moves differently
    assert link_ratio(a, b) is None
    c = Member(3, F, None, (f32(0.9), f32(0.88), f32(0.86)))
    assert link_ratio(a, c) is not None, "float rounding still links a real fraction"
