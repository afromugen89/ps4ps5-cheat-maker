"""The builder's routines (cave.effect_code), run on minicpu inside the fake game.

The matrix: value type x effect x filter (and whose object) x write (lower, raise,
equal) x flags_fix x incoming flags. Each run is compared with the game's own
instruction run on the same inputs without the hook: registers, rsp and the red zone
must match; the stored value follows the effect; the counter ticks; the flags rules
hold; an object the filter rejects goes through exactly like the original.
"""

from __future__ import annotations

import math
import struct

import iced_x86 as ix
import pytest

from cheatmaker.core import cave as cv
from cheatmaker.core.cheats import value_positions
from cheatmaker.core.locker import Writer
from cheatmaker.core.values import ValueType
from cheatmaker.errors import LockFailed
from tests import fakegame as fg
from tests.fakegame import _GameMemory
from tests.minicpu import CF, OF, SF, ZF, Cpu, bits_f32, f32_bits

FULL = {"float": 250.0, "int32": 250, "int64": 250_000}
WRITES = {  # form -> (lower, raise, equal), from the start value
    "float": {"lower": 63.25, "raise": 140.5, "equal": 100.0},
    "int32": {"lower": 93, "raise": 120, "equal": 100},
    "int64": {"lower": 99_000, "raise": 130_000, "equal": 100_000},
}
FILTERS = {
    "none": None,
    "vtable": cv.Filter("vtable", vtable=fg.PLAYER_VT),
    "field": cv.Filter("field", field=8, field_value=0x40048),
}
FMT = ix.Formatter(ix.FormatterSyntax.INTEL)


def _span_target(g: fg.Game, form: str):
    f = fg.FORMS[form]
    span = cv.hook_span(lambda a, n: g.fake.peek(fg.PID, a, n), f.site, single=True)
    return span, cv.writer_target(Writer(f.site, f.code, "", 1))


def build_code(g: fg.Game, form: str, effect: cv.Effect, cave: int, **kw) -> bytes:
    span, target = _span_target(g, form)
    vtype = fg.FORMS[form].vtype
    full = kw.pop("full", vtype.encode(FULL[form]))
    return cv.effect_code(span, target, vtype, effect, cave, full=full, **kw)


def install(g: fg.Game, form: str, effect: cv.Effect, **kw) -> tuple[int, int]:
    """Write the routine and the jump the way the builder does. Returns (cave, slot)."""
    f = fg.FORMS[form]
    cave = cv.find_cave(g.session, 2 * cv.CAVE_ROOM, skip=cv.BUILD_CAVE_SKIP)
    slot = cv.find_slot(g.session)
    code = build_code(g, form, effect, cave, counter=slot, **kw)
    g.fake.poke(fg.PID, cave, code)
    g.fake.poke(fg.PID, f.site, cv.jmp_patch(f.site, cave, len(f.code)))
    return cave, slot


def red_zone(g: fg.Game) -> bytes:
    return g.fake.peek(fg.PID, fg.RSP - cv.RED_ZONE, cv.RED_ZONE)


def expected(effect: cv.Effect, vtype: ValueType, old, new, full):
    """What the value should be after the game writes ``new`` over ``old``."""
    if effect is cv.Effect.ALWAYS_FULL:
        return full
    if vtype.is_float and (math.isnan(old) or math.isnan(new)):
        return new
    if new >= old:
        return new
    if effect is cv.Effect.NEVER_DOWN:
        return old
    if vtype.is_float:
        return bits_f32(f32_bits(bits_f32(f32_bits(new * 0.5)) + bits_f32(f32_bits(old * 0.5))))
    lo, hi = vtype.int_range()
    drop = old - new
    if not lo <= drop <= hi:  # too far apart to halve: the game's write
        return new
    return old - (drop + 1) // 2


def replaced(effect: cv.Effect, old, new) -> bool:
    """Whether our store went over the game's (else the game's write stands)."""
    return effect is cv.Effect.ALWAYS_FULL or new < old


def _sign(value, vtype: ValueType) -> bool:
    raw = vtype.encode(value)
    return bool(raw[-1] & 0x80)


@pytest.mark.parametrize("flags_fix", [False, True], ids=["plain", "flags_fix"])
@pytest.mark.parametrize("write", ["lower", "raise", "equal"])
@pytest.mark.parametrize(
    ("filt", "who"),
    [
        ("none", "player"),
        ("none", "enemy"),  # no filter: everyone gets the effect
        ("vtable", "player"),
        ("vtable", "enemy"),
        ("field", "player"),
        ("field", "enemy"),
    ],
)
@pytest.mark.parametrize("effect", list(cv.Effect), ids=lambda e: e.name)
@pytest.mark.parametrize("form", list(fg.FORMS))
def test_routine_matrix(form, effect, filt, who, write, flags_fix):
    f = fg.FORMS[form]
    new = WRITES[form][write]
    for rflags in (0x202, 0x202 | CF | ZF | SF | OF):
        ref = fg.build()
        obj_ref = ref.player if who == "player" else ref.enemies[0]
        old = ref.value(obj_ref, form)
        want_cpu = ref.hit(obj_ref, new, form, rflags)
        game_value = ref.value(obj_ref, form)

        g = fg.build()
        obj = g.player if who == "player" else g.enemies[0]
        _cave, slot = install(g, form, effect, only_me=FILTERS[filt], flags_fix=flags_fix)
        zone = red_zone(g)
        cpu = g.hit(obj, new, form, rflags)

        assert cpu.snapshot() == want_cpu.snapshot(), "registers and rsp as the game left them"
        assert red_zone(g) == zone == red_zone(ref), "red zone untouched"
        count, last = struct.unpack("<QQ", g.fake.peek(fg.PID, slot, 16))
        if who == "enemy" and filt != "none":  # filtered out: exactly the original
            assert g.value(obj, form) == game_value
            assert cpu.rflags == want_cpu.rflags
            assert (count, last) == (0, 0), "no tick for other objects"
            continue
        want = expected(effect, f.vtype, old, game_value, FULL[form])
        assert g.value(obj, form) == want, f"{old} -> {game_value}: want {want}"
        assert count == 1
        assert last == int.from_bytes(f.vtype.encode(game_value), "little"), "game's new value"
        ours = replaced(effect, old, game_value)
        if ours and flags_fix:  # flags from the value we stored
            stored = g.value(obj, form)
            assert cpu.flag("ZF") is (f.vtype.encode(stored) == bytes(f.vtype.size))
            assert cpu.flag("SF") is _sign(stored, f.vtype)
            assert cpu.flag("OF") is False
        else:  # the game's own instruction's flags
            assert cpu.rflags == want_cpu.rflags
        assert g.value(g.enemies[1], form) == ref.value(ref.enemies[1], form), "others alone"


@pytest.mark.parametrize(
    ("form", "old", "new", "want"),
    [
        ("int32", 10, 3, 6),  # a drop of 7 becomes 4
        ("int32", 10, 9, 9),  # a drop of 1 stays 1
        ("int32", 10, 8, 9),  # 2 -> 1
        ("int32", 5, -20, -8),  # through zero: a drop of 25 becomes 13
        ("int32", 2**31 - 1, -5, -5),  # too far apart to halve: the game's write
        ("int32", -(2**31), -(2**31), -(2**31)),
        ("int64", 2**40, 2**40 - 7, 2**40 - 4),
        ("int64", 2**63 - 1, -(2**63), -(2**63)),
        ("float", 100.0, 40.0, 70.0),
        ("float", 3.0e38, -3.0e38, 0.0),  # no overflow in the middle
        ("float", 100.0, float("-inf"), float("-inf")),
        ("float", 1.0, 0.75, 0.875),
    ],
)
def test_half_damage_edges(form, old, new, want):
    g = fg.build()
    g.set_value(g.player, old, form)
    install(g, form, cv.Effect.HALF)
    g.hit(g.player, new, form)
    assert g.value(g.player, form) == want


@pytest.mark.parametrize(
    ("old", "new", "want"),
    [
        (100.0, float("nan"), "nan"),  # NaN: the game's write
        (float("nan"), 5.0, 5.0),
        (0.0, -0.0, -0.0),  # equal: the game's write
        (-0.0, -1.0, -0.0),  # lower: the old bits, exactly
        (100.0, float("-inf"), 100.0),
    ],
)
def test_never_down_decimal_edges(old, new, want):
    g = fg.build()
    g.set_value(g.player, old)
    install(g, "float", cv.Effect.NEVER_DOWN)
    g.hit(g.player, new)
    got = g.health()
    if want == "nan":
        assert math.isnan(got)
    else:
        assert struct.pack("<f", got) == struct.pack("<f", want)


def test_sub_writer_jle_sees_the_stored_value_with_flags_fix():
    """``sub [rax+2B0h],ecx ; jle dead``: a killing blow the routine refuses must not
    send the game down the "dead" branch when flags_fix is on; without it the game's
    own flags (from the killing value) stand."""
    for flags_fix, dead in ((True, False), (False, True)):
        g = fg.build()
        install(g, "int32", cv.Effect.NEVER_DOWN, flags_fix=flags_fix)
        cpu = g.hit(g.player, -5, "int32")
        assert g.value(g.player, "int32") == 100
        assert cpu.condition(ix.ConditionCode.LE) is dead


def test_flags_fix_changes_a_carry_the_writer_left_alone():
    """Why classify skips ``inc [m] ; jbe`` (FLAGS_BEFORE): the jbe reads CF from before
    the inc, and when the routine stores its value the flags_fix ``cmp`` sets CF = 0,
    sending the game the other way."""
    g = fg.build()
    site = fg.BASE + 0x600
    inc = bytes.fromhex("ff80b0020000")  # inc dword [rax+2B0h]
    g.fake.poke(fg.PID, site, inc + bytes.fromhex("7600c3"))
    w = Writer(site, inc, "", 1)
    span = cv.hook_span(lambda a, n: g.fake.peek(fg.PID, a, n), site, single=True)
    target = cv.writer_target(w)

    def run() -> Cpu:
        cpu = Cpu(_GameMemory(g.fake), rflags=0x202 | CF)
        cpu["rsp"], cpu["rax"] = fg.RSP, g.player
        return cpu.run(site, site + len(inc))

    assert run().condition(ix.ConditionCode.BE), "the game's own run: CF=1 stays"
    cave = cv.find_cave(g.session, 2 * cv.CAVE_ROOM, skip=cv.BUILD_CAVE_SKIP)
    full = ValueType.INT32.encode(250)
    code = cv.effect_code(
        span, target, ValueType.INT32, cv.Effect.ALWAYS_FULL, cave, full=full, flags_fix=True
    )
    g.fake.poke(fg.PID, cave, code)
    g.fake.poke(fg.PID, site, cv.jmp_patch(site, cave, len(inc)))
    assert not run().condition(ix.ConditionCode.BE), "with flags_fix the old CF is lost"


def test_wide_vtable_filter_runs_through_a_scratch_register():
    g = fg.build()
    wide = 0x8_0040_1000
    g.fake.poke(fg.PID, g.player, struct.pack("<Q", wide))
    install(g, "float", cv.Effect.NEVER_DOWN, only_me=cv.Filter("vtable", vtable=wide))
    g.hit(g.player, 10.0)
    g.hit(g.enemies[0], 10.0)
    assert g.health() == 100.0 and g.value(g.enemies[0]) == 10.0


def test_routine_runs_through_the_game_s_hits_queue():
    g = fg.build(capture_ticks=False)
    _cave, slot = install(g, "float", cv.Effect.NEVER_DOWN)
    g.play((g.player, 80.0, "float"), (g.player, 120.0, "float"), (g.player, 90.0, "float"))
    for _ in range(3):
        g.fake.read(fg.PID, g.health_addr, 4)
    assert g.health() == 120.0
    assert struct.unpack("<Q", g.fake.peek(fg.PID, slot, 8))[0] == 3


# -- the code itself ---------------------------------------------------------------------
LEGACY = {
    "lea",
    "pushfq",
    "popfq",
    "push",
    "pop",
    "cmp",
    "jne",
    "jge",
    "jae",
    "jp",
    "jo",
    "jle",
    "jmp",
    "mov",
    "movss",
    "movups",
    "movd",
    "ucomiss",
    "mulss",
    "addss",
    "sub",
    "inc",
    "shr",
    "neg",
    "add",
}


@pytest.mark.parametrize("effect", list(cv.Effect), ids=lambda e: e.name)
@pytest.mark.parametrize("form", list(fg.FORMS))
def test_only_legacy_sse_and_one_scratch_register(form, effect):
    g = fg.build()
    code = build_code(
        g, form, effect, 0x403000, counter=0x404DF0, only_me=FILTERS["field"], flags_fix=True
    )
    insns = list(ix.Decoder(64, code, ip=0x403000))
    writer = ix.Decoder(64, fg.FORMS[form].code).decode()
    ours = [i for i in insns if FMT.format(i) != FMT.format(writer)]
    assert len(insns) - len(ours) == 2, "the game's writer: once per path"
    words = {FMT.format(i).split()[0] for i in ours}
    assert words <= LEGACY, words - LEGACY
    assert len(code) <= cv.CAVE_ROOM
    gprs = {
        ix.RegisterExt.full_register(i.op_register(k))
        for i in ours
        for k in range(i.op_count)
        if i.op_kind(k) == ix.OpKind.REGISTER and ix.RegisterExt.is_gpr(i.op_register(k))
    }
    obj = writer.memory_base
    scratch = ix.Register.RBX if obj == ix.Register.RAX else ix.Register.RAX
    assert gprs <= {ix.Register.RSP, scratch}, gprs


def _normalized(insns: list[ix.Instruction]) -> list[str]:
    """Instruction texts with branch targets as instruction numbers, so two builds
    that differ only by some lines compare equal once those lines are taken out."""
    index = {i.ip: k for k, i in enumerate(insns)}
    branches = (ix.FlowControl.CONDITIONAL_BRANCH, ix.FlowControl.UNCONDITIONAL_BRANCH)
    return [
        f"{FMT.format(i).split()[0]} #{index.get(i.near_branch_target, 'back')}"
        if i.flow_control in branches
        else FMT.format(i)
        for i in insns
    ]


@pytest.mark.parametrize("filt", ["none", "vtable"])
@pytest.mark.parametrize("effect", list(cv.Effect), ids=lambda e: e.name)
@pytest.mark.parametrize("form", list(fg.FORMS))
def test_saved_build_is_the_test_build_minus_the_counter_lines(form, effect, filt):
    g = fg.build()
    cave, slot = 0x403000, 0x404DF0
    kw = {"only_me": FILTERS[filt], "flags_fix": form == "int32"}
    test = list(ix.Decoder(64, build_code(g, form, effect, cave, counter=slot, **kw), ip=cave))
    saved = list(ix.Decoder(64, build_code(g, form, effect, cave, counter=None, **kw), ip=cave))
    counter_at = [
        k
        for k, i in enumerate(test)
        if i.is_ip_rel_memory_operand and i.ip_rel_memory_address in (slot, slot + 8)
    ]
    assert len(counter_at) == 2 and test[counter_at[0]].mnemonic == ix.Mnemonic.INC
    lines = range(counter_at[0], counter_at[1] + 1)
    assert len(lines) == 3, "inc, load the game's value, store it"
    without = [i for k, i in enumerate(test) if k not in lines]
    assert _normalized(without) == _normalized(saved)
    assert not any(i.is_ip_rel_memory_operand for i in saved), "nothing points at the slot"


@pytest.mark.parametrize("form", list(fg.FORMS))
def test_always_full_keeps_full_editable(form):
    g = fg.build()
    vtype = fg.FORMS[form].vtype
    at = value_positions(
        lambda v: [build_code(g, form, cv.Effect.ALWAYS_FULL, 0x403000, full=v)], vtype.size
    )
    assert at is not None and len(at) == vtype.size
    code = build_code(g, form, cv.Effect.ALWAYS_FULL, 0x403000, full=vtype.encode(321))
    assert vtype.decode(bytes(code[b] for _line, b in at)) == 321


def test_refusals():
    g = fg.build()
    span, target = _span_target(g, "float")
    with pytest.raises(LockFailed, match="isn't supported yet"):
        cv.effect_code(span, target, ValueType.DOUBLE, cv.Effect.NEVER_DOWN, 0x403000)
    with pytest.raises(LockFailed, match="full value"):
        cv.effect_code(span, target, ValueType.FLOAT, cv.Effect.ALWAYS_FULL, 0x403000)
    with pytest.raises(LockFailed, match="more than this value"):  # S4: 4-byte store
        cv.effect_code(span, target, ValueType.INT64, cv.Effect.NEVER_DOWN, 0x403000)
    other = cv.Target(target.base, target.disp + 4)
    with pytest.raises(LockFailed, match="doesn't write the value"):
        cv.effect_code(span, other, ValueType.FLOAT, cv.Effect.NEVER_DOWN, 0x403000)
    xadd = bytes.fromhex("0FC188B0020000")  # xadd [rax+2B0h],ecx: also writes ecx
    xspan = cv.decode_at(xadd, 0x401000)
    xt = cv.writer_target(Writer(0x401000, xadd, "", 1))
    with pytest.raises(LockFailed, match="safely"):
        cv.effect_code(xspan, xt, ValueType.INT32, cv.Effect.NEVER_DOWN, 0x403000)
    rsp_src = bytes.fromhex("4889A3B0020000")  # mov [rbx+2B0h],rsp
    rspan = cv.decode_at(rsp_src, 0x401000)
    rt = cv.writer_target(Writer(0x401000, rsp_src, "", 1))
    with pytest.raises(LockFailed, match="stack"):
        cv.effect_code(rspan, rt, ValueType.INT64, cv.Effect.NEVER_DOWN, 0x403000)
    with pytest.raises(ValueError):
        cv.effect_code(
            span, target, ValueType.FLOAT, cv.Effect.HALF, 0x403000, only_me=cv.Filter("magic")
        )


# -- where the rooms go ------------------------------------------------------------------
def test_build_rooms_go_below_the_other_caves_and_skip_open_cheats():
    g = fg.build()
    top = cv.find_cave(g.session, cv.CAVE_ROOM)
    room = cv.find_cave(g.session, 2 * cv.CAVE_ROOM, skip=cv.BUILD_CAVE_SKIP)
    assert cv.BUILD_CAVE_SKIP == 5 * cv.CAVE_ROOM
    assert room + 2 * cv.CAVE_ROOM <= top - 4 * cv.CAVE_ROOM, "clear of the 5 rooms above"
    lo = room - fg.BASE
    moved = cv.find_cave(
        g.session, 2 * cv.CAVE_ROOM, skip=cv.BUILD_CAVE_SKIP, extra_reserved=[(lo, lo + 8)]
    )
    assert moved + 2 * cv.CAVE_ROOM <= room, "an open cheat's code line is never reused"
    g.session.reserved_rooms = [(lo - 0x100, lo + 0x100)]
    assert cv.find_cave(g.session, 2 * cv.CAVE_ROOM, skip=cv.BUILD_CAVE_SKIP) < room
