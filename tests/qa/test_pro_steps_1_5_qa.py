"""QA round for Cheat Maker Pro R1 steps 1-5 (PR #37): scanner model, .cmproj format 5,
the CheatRunner / PC only badge, .mc4 and the NASM tooling.
Spec docs/spec-pro.md (§5, AC-S2, AC-S3, PR-2, PR-12); design docs/architecture-pro.md.
Report: qa/reports/2026-11-22-pro-steps-1-5.md.

Bugs are strict xfail tests named after their BUG id; remove the marker once fixed.
The guided-export byte pins were taken from origin/main (3f409f7) with the same inputs,
so a change in them is a change in what guided users get in their cheat files.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import itertools
import json
import random
import struct
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from cheatmaker import formats, paths
from cheatmaker.console.ps5debug_ng import Ps5DebugNgClient
from cheatmaker.core import project as pj
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.core.pro.badge import (
    CHEATRUNNER,
    PC_ONLY,
    WRITTEN_ONCE,
    badge,
    pc_only,
    runner_cheats,
)
from cheatmaker.core.pro.model import Variable
from cheatmaker.core.values import (
    F32_MAX,
    GUIDED_TYPES,
    Compare,
    ValueType,
    as_between,
    f32,
    matches,
    value_for,
)
from cheatmaker.errors import CheatFileError, ProjectFileError
from cheatmaker.formats import _aes, mc4, shn
from tests.conftest import HEAP_BASE, PID, build_fake
from tests.unit.test_nasm_tooling import _exe, _fetch_tool, _zip
from tests.unit.test_nasm_tooling import no_nasm as no_nasm
from tests.unit.test_ps5debug_ng import FakeNgServer

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
K = LocationKind
V = ValueType


# -- guided export bytes (regression vs origin/main) ------------------------------------
# sha256 of formats.dumps(...) on origin/main 3f409f7; HEAD must give the same bytes.
MAIN_GOLDEN_SHA = {
    "CUSA02396_01.01.json->json": (
        "82637fc2428b0c035cee9421ec533e0b9aa8992e9a9b2c6281b8db7523e72d98"
    ),
    "CUSA02396_01.01.json->shn": (
        "018c62f0087ac35e9a77044511bc6278ff1ec70bdc242976f69c04ed807cc2de"
    ),
    "CUSA02396_01.01.json+special->json": (
        "75004afc801235f310b194bf3cc879dca292156b69e4c120440437e5c79bc7c1"
    ),
    "CUSA02396_01.01.json+special->shn": (
        "6b4ecb9ffbbe7bdcd5a644425b8208e8dada317e8afc9d3df5604aeaf02b2851"
    ),
}


def _with_specials(cf: CheatFile) -> CheatFile:
    cf.title = 'Ratchet & Clank <"x">'
    cf.credits = ["a&b", "<c>", '"d"']
    for c in cf.cheats:
        c.name = c.name + ' & <"q">'
    return cf


def _golden_digests() -> dict[str, str]:
    out = {}
    for p in sorted(GOLDEN.glob("*")):
        if p.suffix not in (".json", ".shn"):
            continue
        for special in (False, True):
            cf = formats.load_path(p)
            if special:
                cf = _with_specials(cf)
            for fmt in ("json", "shn"):
                key = f"{p.name}{'+special' if special else ''}->{fmt}"
                out[key] = hashlib.sha256(formats.dumps(cf, fmt)).hexdigest()
    return out


def test_golden_shn_and_json_bytes_equal_main():  # TC-40
    got = _golden_digests()
    for key, sha in MAIN_GOLDEN_SHA.items():
        assert got[key] == sha, key


def _guided_mix() -> CheatFile:
    L = Location
    cheats = [
        Cheat(
            "Inf Health & <more>",
            [L(K.MODULE_VALUE, 0x1000, b"\x0f\x27\x00\x00", b"\x64\x00\x00\x00")],
            freeze=True,
            value_type=V.INT32,
        ),
        Cheat(
            'Money "x"',
            [L(K.MODULE_VALUE, 0x2000, b"\x00\x00\xc8\x42", b"\x00\x00\x80\x3f")],
            value_type=V.FLOAT,
        ),
        Cheat("Code", [L(K.CODE_PATCH, 0x5E1536C, b"\x90" * 3, b"\x89\x4b\x08")]),
        Cheat(
            "Player only",
            [
                L(K.CODE_PATCH, 0x100, b"\xe9\x00\x00\x00\x00", b"\x89\x08\x90\x90\x90"),
                L(K.CODE_PATCH, 0x9000, b"\xc7\x00\x10\x27\x00\x00", b"\xcc" * 6),
            ],
            value_type=V.INT32,
            value_at=[(1, 2), (1, 3), (1, 4), (1, 5)],
        ),
        Cheat("Heap", [L(K.SESSION_ADDRESS, 0x2000001000, b"\x01", b"\x00")], freeze=True),
        Cheat(
            "Anchored",
            [L(K.SESSION_ADDRESS, 0, b"\x01\x00\x00\x00", b"\x01\x00\x00\x00")],
            freeze=True,
            anchor=("obj1", "Health"),
        ),
        Cheat(
            "Locked",
            [L(K.CODE_PATCH, 0x300, b"\x90\x90", b"\x29\xc8")],
            locked=L(K.SESSION_ADDRESS, 0x2000002000, b"\x05\0\0\0", b"\x05\0\0\0", vtype=V.INT32),
        ),
        Cheat("Empty", []),
        Cheat(
            "Combined",
            [
                L(K.CODE_PATCH, 0x400, b"\x90", b"\xcc", part="a"),
                L(K.SESSION_ADDRESS, 0, b"\x01", b"\x01", anchor=("obj2", "f"), part="b"),
            ],
        ),
    ]
    return CheatFile('Game & <Co> "q"', "CUSA12345", "01.00", cheats, "eboot.bin", ["me & you"])


def test_guided_mix_export_bytes_equal_main():  # TC-41, TC-60 of restart-combined unchanged
    cf = _guided_mix()
    assert [c.name for c in cf.saveable_cheats()] == [
        "Inf Health & <more>",
        'Money "x"',
        "Code",
        "Player only",
        "Locked",
    ]
    digests = {fmt: hashlib.sha256(formats.dumps(cf, fmt)).hexdigest() for fmt in ("json", "shn")}
    assert digests == {
        "json": "b15c15d6d6b327770e78f3d859d6825e51ce133c0e943e7724655301c7d263b3",
        "shn": "d0548eeff2a9ff5e3aa1ace24218c83830b844be143521ca368679cbb947718e",
    }


# -- scanner model ----------------------------------------------------------------------
_TARGETS = [
    0,
    -0.0,
    0.5,
    -0.5,
    1,
    -1,
    100.5,
    0.1,
    1e-45,
    -1e-45,
    1e-40,
    5e-324,
    1e-310,
    3.4e38,
    -3.4e38,
    F32_MAX,
    -F32_MAX,
    1e308,
    -1e308,
    sys.float_info.max,
    255,
    256,
    -129,
    2**63 - 1,
    2**63,
    2**64 - 1,
    2**64,
    -(2**63),
]


def _candidates(vt: ValueType) -> list[bytes]:
    if vt.is_float:
        xs = [0.0, -0.0, 1e-45, -1e-45, 1e-40, 0.1, 0.5, -0.5, 1.0, 100.5, 3.4e38]
        xs += [F32_MAX, -F32_MAX, float("inf"), float("-inf"), float("nan")]
        if vt is V.DOUBLE:
            xs += [5e-324, -5e-324, 1e-310, 1e308, sys.float_info.max, -sys.float_info.max]
        out = []
        for x in xs:
            with contextlib.suppress(OverflowError, ValueError):
                out.append(vt.encode(x))
        return out
    lo, hi = vt.int_range()
    xs = [lo, lo + 1, hi, hi - 1, 0, 1, -1, 100, 101, 255, 256, -128, -129]
    return [vt.encode(x) for x in xs if lo <= x <= hi]


@pytest.mark.parametrize("vt", list(ValueType))
@pytest.mark.parametrize("cmp", [Compare.GREATER, Compare.LESS])
def test_as_between_keeps_exactly_what_matches_keeps(vt, cmp):  # TC-01..TC-06
    """The console range (ValueBetween) and the PC compare agree at every type end."""
    for t in _TARGETS:
        rng = as_between(vt, cmp, t)
        if rng is not None:
            lo, hi = rng
            assert lo <= hi, (t, rng)
            if vt is V.FLOAT:
                assert f32(lo) == lo and f32(hi) == hi, (t, rng)
            if not vt.is_float:
                tlo, thi = vt.int_range()
                assert isinstance(lo, int) and isinstance(hi, int)
                assert tlo <= lo <= hi <= thi
        for raw in _candidates(vt):
            cur = vt.decode(raw)
            by_range = rng is not None and rng[0] <= cur <= rng[1]
            assert matches(vt, cmp, raw, None, t) == by_range, (t, cur, rng)
        assert (value_for(vt, cmp, t) is None) == (rng is None)


def test_guided_lists_have_no_unsigned_types():  # TC-07
    assert GUIDED_TYPES == (V.INT8, V.INT16, V.INT32, V.INT64, V.FLOAT, V.DOUBLE)
    assert list(ValueType)[:6] == list(GUIDED_TYPES), "guided combo indexes still hold"


def _diff_run(seed: int, types: tuple[ValueType, ...], snapshot: bool) -> None:
    """The real client over FakeNgServer and FakeConsole give the same matches and values
    for greater / less (and exact) narrowing, at every step."""
    rnd = random.Random(seed)
    srv = FakeNgServer(snapshot=snapshot)
    ref = build_fake()
    addrs = [HEAP_BASE + 0x400 + 8 * i for i in range(40)]
    t0 = types[0]

    def rand_raw() -> bytes:
        if t0.is_float:
            pick = [0.0, -0.0, 0.5, 1.0, 99.5, 100.0, 100.5, 101.0, -3.0, 1e30, 1e-45]
            return t0.encode(rnd.choice(pick)).ljust(8, b"\0")
        lo, hi = t0.int_range()
        pick = [lo, hi, 0, 1, 99, 100, 101, 200, max(lo, -1), min(hi, 255)]
        return t0.encode(rnd.choice(pick)).ljust(8, b"\0")

    def poke(a: int, raw: bytes) -> None:
        srv.fake.poke(PID, a, raw)
        ref.poke(PID, a, raw)

    for a in addrs:
        poke(a, rand_raw())
    cl = Ps5DebugNgClient.connect("127.0.0.1", srv.port, timeout=3)
    try:
        regions = [m for m in cl.maps(PID) if m.writable and m.start <= HEAP_BASE < m.end]
        multi = len(types) > 1
        if multi or snapshot:
            cl.scan_start(PID, regions, types if multi else t0, Compare.UNKNOWN, None)
            ref.scan_start(PID, regions, types if multi else t0, Compare.UNKNOWN, None)
            # FakeNgServer's snapshot keeps fewer rows than FakeConsole: compare from there
            ref.scan_restrict(cl.scan_results(100_000))
        else:
            cmp = rnd.choice([Compare.GREATER, Compare.LESS])
            v = rnd.choice([0, 0.5, 100, 100.5, -1, 255])
            assert cl.scan_start(PID, regions, t0, cmp, v) == ref.scan_start(
                PID, regions, t0, cmp, v
            )

        def rows(c) -> list[tuple[int, bytes]]:
            return sorted((h.address, h.value) for h in c.scan_results(100_000))

        assert rows(cl) == rows(ref)
        for _ in range(4):
            for a in addrs:
                if rnd.random() < 0.4:
                    poke(a, rand_raw())
            cmp = rnd.choice([Compare.GREATER, Compare.LESS, Compare.GREATER, Compare.EXACT])
            v = rnd.choice([0, 0.5, 100, 100.5, -1, 255, -0.5, 1e30, -1e30])
            if cmp is Compare.EXACT:
                v = rnd.choice([0, 1, 100, 255])
            n = cl.scan_next(cmp, v)
            assert n == ref.scan_next(cmp, v), (cmp, v)
            assert rows(cl) == rows(ref), (cmp, v)
            if n == 0:
                break
    finally:
        cl.close()


@pytest.mark.parametrize("seed", range(3))
@pytest.mark.parametrize(
    ("types", "snapshot"),
    [
        ((V.INT32,), False),
        ((V.INT32,), True),
        ((V.UINT8,), False),
        ((V.UINT8,), True),
        ((V.UINT32,), True),
        ((V.FLOAT,), False),
        ((V.FLOAT,), True),
        ((V.DOUBLE,), True),
        ((V.INT32, V.FLOAT), True),
        ((V.UINT16, V.INT16), True),
    ],
)
def test_ng_and_fake_agree_on_greater_less(seed, types, snapshot):  # TC-08, TC-09
    _diff_run(seed, types, snapshot)


def test_paging_covers_every_match_once():  # TC-10
    srv = FakeNgServer(snapshot=True)
    srv.three_value_rows = True
    cl = Ps5DebugNgClient.connect("127.0.0.1", srv.port, timeout=3)
    try:
        regions = [m for m in cl.maps(PID) if m.writable]
        cl.scan_start(PID, regions, (V.INT32, V.FLOAT), Compare.UNKNOWN, None)
        whole = cl.scan_results(100_000)
        pages = list(itertools.chain.from_iterable(cl.scan_results(7, o) for o in range(0, 70, 7)))
        assert [(h.address, h.vtype) for h in pages] == [(h.address, h.vtype) for h in whole[:70]]
        assert cl.scan_results(10, len(whole)) == []
    finally:
        cl.close()


@pytest.mark.parametrize(
    ("vt", "cmp", "value", "expected"),
    [
        (V.FLOAT, Compare.LESS, 10**400, (-F32_MAX, F32_MAX)),
        (V.DOUBLE, Compare.LESS, 10**400, (-sys.float_info.max, sys.float_info.max)),
        (V.FLOAT, Compare.GREATER, 10**400, None),
        (V.DOUBLE, Compare.GREATER, -(10**400), (-sys.float_info.max, sys.float_info.max)),
    ],
)
def test_bug02_greater_less_with_a_huge_whole_number(vt, cmp, value, expected):  # TC-05
    """parse_number("1" + "0" * 400) is accepted, so the Pro scanner can be given it."""
    assert as_between(vt, cmp, value) == expected


# -- .cmproj format 5 -------------------------------------------------------------------
def _fmt4() -> dict:
    d = json.loads((GOLDEN / "PPSA01285_03.000.005.cmproj").read_text(encoding="utf-8"))
    d["format_version"] = 4
    return d


def test_format_4_saved_by_this_app_differs_only_in_version(tmp_path):  # TC-20, AC-S3
    src = tmp_path / "a.cmproj"
    src.write_text(json.dumps(_fmt4(), indent=1), encoding="utf-8")
    p = pj.load(src)
    assert p is not None and p.pro_cheats == []
    out = tmp_path / "b.cmproj"
    pj.save(out, p)
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved.pop("format_version") == 5
    original = _fmt4()
    original.pop("format_version")
    assert saved == original
    assert "pro_cheats" not in saved


@pytest.mark.parametrize("ver", [6, 99])
def test_newer_format_is_refused_and_can_be_set_aside(tmp_path, ver):  # TC-21
    d = _fmt4()
    d["format_version"] = ver
    path = tmp_path / "x.cmproj"
    path.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ProjectFileError, match=f"newer Cheat Maker \\(format {ver}\\)"):
        pj.load(path)
    moved = pj.set_aside(path)
    assert moved is not None and moved.exists() and not path.exists()


def _with_variable(tmp_path, var: dict) -> Path:
    d = _fmt4()
    d["format_version"] = 5
    d["pro_cheats"] = [{"id": "p1", "name": "x", "entries": [], "variables": [var]}]
    path = tmp_path / "v.cmproj"
    path.write_text(json.dumps(d), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "var",
    [
        {"name": "m", "type": "int8", "value": 200},
        {"name": "m", "type": "uint8", "value": -1},
        {"name": "m", "type": "int32", "value": 1.5},
        {"name": "Hook", "type": "int32", "value": 1},
        {"name": "m", "type": "float", "value": True},
        {"name": "m", "type": "nope", "value": 1},
    ],
)
def test_bad_variables_make_a_project_file_error(tmp_path, var):  # TC-22
    with pytest.raises(ProjectFileError):
        pj.load(_with_variable(tmp_path, var))


@pytest.mark.parametrize(
    "var",
    [
        {"name": "mult", "type": "float", "value": 1e39},
        {"name": "mult", "type": "float", "value": -1e39},
        {"name": "big", "type": "int64", "value": 10**400},
    ],
)
def test_bug01_out_of_range_variable_is_a_project_file_error(tmp_path, var):  # TC-23
    with pytest.raises(ProjectFileError):
        pj.load(_with_variable(tmp_path, var))


def test_bug01_variable_constructor_raises_value_error():  # TC-23
    with pytest.raises(ValueError):
        Variable("mult", V.FLOAT, 5e38)


# -- badge ------------------------------------------------------------------------------
RAW = struct.pack("<i", 100)


def _all_guided_shapes() -> list[Cheat]:
    """Every shape guided mode builds: kinds x freeze x anchors x combined x locked."""
    out = []
    locs = {
        "code": Location(K.CODE_PATCH, 0x100, b"\x90\x90", b"\x89\x08"),
        "static": Location(K.MODULE_VALUE, 0x1010, RAW, RAW),
        "heap": Location(K.SESSION_ADDRESS, 0x2000_0040, RAW, RAW),
        "learned": Location(K.SESSION_ADDRESS, 0, RAW, RAW, anchor=("obj1", "Health")),
    }
    for n in range(0, 3):
        for combo in itertools.product(locs, repeat=n):
            for freeze in (False, True):
                lines = [locs[k] for k in combo]
                out.append(Cheat("c", list(lines), freeze=freeze))
                if combo and all(k == "heap" for k in combo):  # Find it next time
                    out.append(Cheat("f", list(lines), freeze=freeze, anchor=("obj1", "Health")))
                if combo and combo[0] == "code":
                    out.append(Cheat("l", list(lines), locked=locs["heap"]))
    return out


def test_export_set_equals_saveable_for_every_guided_shape():  # TC-30
    shapes = _all_guided_shapes()
    assert len(shapes) > 40
    assert runner_cheats(shapes) == [c for c in shapes if c.saveable]
    for c in shapes:
        b = badge(c)
        assert b.runs_on_console == c.saveable
        assert (WRITTEN_ONCE in b.notes) == (b.target == CHEATRUNNER and c.freeze)
        if b.target == PC_ONLY:
            assert b.reasons and "Needs Cheat Maker running" in b.tooltip
        else:
            assert b.reasons == ()


def test_save_dialog_list_names_every_left_out_cheat():  # TC-31
    cf = _guided_mix()
    left = [c.name for c, _b in pc_only(cf.cheats)]
    assert left == ["Heap", "Anchored", "Empty", "Combined"]
    assert {c.name for c in cf.saveable_cheats()}.isdisjoint(left)


# -- mc4 --------------------------------------------------------------------------------
@pytest.mark.parametrize("path", sorted(GOLDEN.glob("*.[js][sh]*")), ids=lambda p: p.name)
def test_mc4_equals_sanitised_shn_for_every_golden_file(path):  # TC-50, AC-12.2, AC-12.4
    cf = _with_specials(formats.load_path(path))
    data = mc4.dumps(cf)
    plain = mc4.decrypt(data)
    assert plain == shn.dumps(cf, sanitise=True)
    root = ET.fromstring(plain.decode("utf-8").split("?>", 1)[1])
    for el in root.iter():
        for v in el.attrib.values():
            assert not set('&<>"') & set(v), v
    back = mc4.loads(data)
    assert back.title == "Ratchet + Clank ('x')"
    assert [c.name for c in back.cheats] == [
        c.name.translate(str.maketrans('&<>"', "+()'")) for c in cf.saveable_cheats()
    ]
    # the plain .shn still escapes (unchanged from earlier versions)
    plain_shn = shn.dumps(cf)
    assert b"&amp;" in plain_shn and b"&lt;" in plain_shn
    assert [c.name for c in shn.loads(plain_shn).cheats] == [c.name for c in cf.saveable_cheats()]


def test_mc4_without_special_characters_equals_plain_shn():  # TC-51
    cf = formats.load_path(GOLDEN / "CUSA10454_01.00.json")
    assert mc4.decrypt(mc4.dumps(cf)) == shn.dumps(cf)


def test_mc4_round_trips_non_ascii_names():  # TC-52
    cf = formats.load_path(GOLDEN / "CUSA02396_01.01.json")
    cf.title = "ラチェット＆クランク 🎮"
    cf.cheats[0].name = "Ünïcödé “quotes” ＜＞"
    back = mc4.loads(mc4.dumps(cf))
    assert back.title == cf.title and back.cheats[0].name == cf.cheats[0].name


@pytest.mark.parametrize(
    "wrap",
    [
        lambda b: b"\r\n".join(b[i : i + 76] for i in range(0, len(b), 76)) + b"\r\n",
        lambda b: b"  " + b + b"\n\n",
        lambda b: b.decode(),
    ],
    ids=["crlf-76", "spaces", "str"],
)
def test_mc4_reads_wrapped_base64(wrap):  # TC-53
    cf = formats.load_path(GOLDEN / "CUSA02396_01.01.json")
    assert len(mc4.loads(wrap(mc4.dumps(cf))).cheats) == len(cf.cheats)


@pytest.mark.parametrize("bom", [b"\xff\xfe", b"\xfe\xff"], ids=["le", "be"])
def test_mc4_reads_utf16_plaintext_with_either_bom(bom):  # TC-54
    cf = formats.load_path(GOLDEN / "CUSA02396_01.01.json")
    text = shn.dumps(cf).decode("utf-8")
    plain = bom + text.encode("utf-16-le" if bom == b"\xff\xfe" else "utf-16-be")
    assert len(mc4.loads(mc4.encrypt(plain)).cheats) == len(cf.cheats)


def test_mc4_garbage_only_ever_raises_cheatfileerror():  # TC-55
    cf = formats.load_path(GOLDEN / "CUSA02396_01.01.json")
    enc = mc4.dumps(cf)
    raw = base64.b64decode(enc)
    plain = mc4.decrypt(enc)
    rnd = random.Random(7)
    for _ in range(600):
        kind = rnd.randrange(6)
        if kind == 0:
            data = bytes(rnd.randrange(256) for _ in range(rnd.randrange(200)))
        elif kind == 1:
            data = enc[: rnd.randrange(len(enc))]
        elif kind == 2:
            b = bytearray(raw)
            b[rnd.randrange(len(b))] ^= 1 << rnd.randrange(8)
            data = base64.b64encode(bytes(b))
        elif kind == 3:
            data = base64.b64encode(
                bytes(rnd.randrange(256) for _ in range(16 * rnd.randint(1, 8)))
            )
        elif kind == 4:
            p = bytearray(plain)
            p[rnd.randrange(len(p))] = rnd.randrange(256)
            data = mc4.encrypt(bytes(p))
        else:
            data = mc4.encrypt(plain[: rnd.randrange(len(plain))])
        with contextlib.suppress(CheatFileError):
            mc4.loads(data)


def test_mc4_external_entity_is_refused():  # TC-56
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE t [<!ENTITY x SYSTEM "file:///etc/hostname">]>'
        b'<Trainer Game="&x;" Moder="" Cusa="CUSA1" Version="1.00" Process="eboot.bin">'
        b'<Cheat Text="a"><Cheatline><Offset>10</Offset><Section>0</Section>'
        b"<ValueOn>01</ValueOn><ValueOff>00</ValueOff></Cheatline></Cheat></Trainer>"
    )
    with pytest.raises(CheatFileError):
        mc4.loads(mc4.encrypt(xxe))


def test_aes_round_trip_every_length():  # TC-57
    for n in range(0, 70):
        data = bytes(range(n))
        enc = _aes.cbc_encrypt(mc4.KEY, mc4.IV, _aes.pkcs7_pad(data))
        assert _aes.pkcs7_unpad(_aes.cbc_decrypt(mc4.KEY, mc4.IV, enc)) == data


def test_bug03_mc4_with_a_utf8_bom_is_read(tmp_path):  # TC-58
    cf = formats.load_path(GOLDEN / "CUSA02396_01.01.json")
    path = tmp_path / "CUSA02396_01.01.mc4"
    path.write_bytes(b"\xef\xbb\xbf" + mc4.dumps(cf))
    assert len(formats.load_path(path).cheats) == len(cf.cheats)


def test_bug04_utf16_json_error_names_json(tmp_path):  # TC-59
    text = (GOLDEN / "CUSA02396_01.01.json").read_bytes().decode("utf-8-sig")
    path = tmp_path / "CUSA02396_01.01.json"
    path.write_bytes(text.encode("utf-16"))
    with pytest.raises(CheatFileError) as exc:
        formats.load_path(path)
    assert "XML" not in str(exc.value)


# -- NASM tooling -----------------------------------------------------------------------
def test_configured_folder_is_not_an_assembler(no_nasm, monkeypatch, caplog):  # TC-70
    folder = no_nasm / "dir"
    folder.mkdir()
    bundled = _exe(no_nasm / "bundled")
    assert paths.nasm_exe(str(folder)) == bundled
    assert "nasm_path_missing" in caplog.text


def test_order_configured_then_bundled_then_path(no_nasm, monkeypatch):  # TC-71
    on_path = _exe(no_nasm / "onpath")
    monkeypatch.setattr(paths.shutil, "which", lambda name: str(on_path))
    assert paths.nasm_exe() == on_path
    bundled = _exe(no_nasm / "bundled")
    assert paths.nasm_exe() == bundled
    mine = _exe(no_nasm / "mine")
    assert paths.nasm_exe(str(mine)) == mine
    assert paths.nasm_exe("") == bundled


@pytest.mark.parametrize(
    "members",
    [
        {"/abs/nasm.exe": b"MZ", "C:\\x\\LICENSE": b"L"},
        {"NASM.EXE": b"MZ", "License": b"L"},
        {"nasm-3.02/nasm.exe": b"MZ", "nasm-3.02/LICENSE": b"L", "other/nasm.exe": b"EVIL"},
    ],
    ids=["absolute", "case", "first-wins"],
)
def test_extract_writes_only_into_dest(tmp_path, members):  # TC-72
    tool = _fetch_tool()
    out = tmp_path / "out"
    written = tool.extract(_zip(members), out)
    assert sorted(p.name for p in written) == ["LICENSE", "nasm.exe"]
    assert all(p.parent == out for p in written)
    assert (out / "nasm.exe").read_bytes() == b"MZ"


def test_directory_named_nasm_exe_is_not_taken(tmp_path):  # TC-73
    tool = _fetch_tool()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("nasm.exe/", b"")
        zf.writestr("LICENSE", b"L")
    with pytest.raises(tool.FetchError, match=r"nasm\.exe"):
        tool.extract(buf.getvalue(), tmp_path / "out")


def test_network_error_is_reported_not_raised(monkeypatch, tmp_path, capsys):  # TC-74
    tool = _fetch_tool()
    monkeypatch.setattr(tool, "NASM_ZIP_SHA256", "0" * 64)

    def fail(url):
        raise OSError("connection reset")

    monkeypatch.setattr(tool, "download", fail)
    assert tool.main(["--dest", str(tmp_path / "out")]) == 2
    assert "connection reset" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_verified_but_broken_zip_is_reported(monkeypatch, tmp_path, capsys):  # TC-75
    tool = _fetch_tool()
    junk = b"not a zip"
    monkeypatch.setattr(tool, "NASM_ZIP_SHA256", hashlib.sha256(junk).hexdigest())
    monkeypatch.setattr(tool, "download", lambda url: junk)
    assert tool.main(["--dest", str(tmp_path / "out")]) == 2
    assert capsys.readouterr().err.startswith("fetch_nasm:")


def test_bug01_notes_with_an_out_of_range_variable_survive_in_the_app(win, console, qtbot):
    """TC-24: like find-again TC-28, but the file is unreadable because of a Pro variable."""
    from tests import fakegame as fg
    from tests.qa.test_find_again_qa import GOOD_HEAD, add_health_cheat, connect, learn_ui

    doc = json.dumps(
        {
            **GOOD_HEAD,
            "format_version": 5,
            "objects": [],
            "pro_cheats": [
                {
                    "id": "p1",
                    "name": "Damage x",
                    "variables": [{"name": "mult", "type": "float", "value": 1e39}],
                }
            ],
        }
    )
    path = pj.project_path(fg.TITLE, fg.VERSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    with qtbot.captureExceptions() as errors:
        connect(win)
    learn_ui(win, add_health_cheat(win, console.game))
    kept = [p for p in path.parent.iterdir() if p.read_text(encoding="utf-8") == doc]
    assert kept, "the unreadable notes must survive somewhere"
    assert errors == [], "a plain message, not an exception in the Qt event loop"


# == Re-test of 3e8e54e (BUG-01..04 fixes, offset and name checks) =====================
@pytest.mark.parametrize("vt", list(ValueType))
@pytest.mark.parametrize("cmp", [Compare.GREATER, Compare.LESS])
@pytest.mark.parametrize("value", [10**400, -(10**400)], ids=["huge", "-huge"])
def test_rt_huge_targets_agree_with_matches_for_every_type(vt, cmp, value):  # TC-05b
    rng = as_between(vt, cmp, value)
    assert (value_for(vt, cmp, value) is None) == (rng is None)
    keeps_all = (cmp is Compare.LESS) == (value > 0)
    assert (rng is not None) == keeps_all
    for raw in _candidates(vt):
        cur = vt.decode(raw)
        by_range = rng is not None and rng[0] <= cur <= rng[1]
        assert matches(vt, cmp, raw, None, value) == by_range, (cur, rng)


@pytest.mark.parametrize(
    ("vt", "value"),
    [(V.FLOAT, F32_MAX), (V.DOUBLE, sys.float_info.max), (V.UINT64, 2**64 - 1), (V.INT8, -128)],
)
def test_rt_variables_at_the_type_ends_still_load(vt, value):  # TC-23b
    assert Variable("v", vt, value).encoded() == vt.encode(value)


@pytest.mark.parametrize(
    ("vt", "value"),
    [(V.FLOAT, 10**400), (V.DOUBLE, -(10**400)), (V.INT64, 1e20), (V.UINT8, 256)],
)
def test_rt_variables_past_the_type_ends_are_value_errors(vt, value):  # TC-23b
    with pytest.raises(ValueError):
        Variable("v", vt, value)


@pytest.mark.parametrize("name", ["rax", "RAX", "r8d", "xmm15", "byte", "DWORD", "rel", "ptr"])
def test_rt_register_and_assembler_names_are_refused(name):  # TC-25
    with pytest.raises(ValueError, match="register or assembler word"):
        Variable(name, V.INT32, 1)


@pytest.mark.parametrize("name", ["health", "mult", "add", "mov", "rax_copy", "_byte"])
def test_rt_ordinary_names_are_still_allowed(name):  # TC-25 (mnemonics assemble as labels)
    assert Variable(name, V.INT32, 1).name == name


def test_rt_offsets_below_2_48_only(tmp_path):  # TC-26
    from cheatmaker.core.pro.model import MAX_OFFSET, ProEntry

    assert MAX_OFFSET == 1 << 48
    ProEntry("bytes", MAX_OFFSET - 1, b"\x90", b"\xcc")
    ProEntry("cave", 0x100, b"\x90" * 5, cave=MAX_OFFSET - 1)
    for kw in ({"offset": MAX_OFFSET}, {"offset": 0x100, "cave": MAX_OFFSET}):
        with pytest.raises(ValueError, match="below"):
            ProEntry("cave", kw["offset"], b"\x90" * 5, cave=kw.get("cave"))
    d = _fmt4()
    d["format_version"] = 5
    d["pro_cheats"] = [
        {
            "id": "p1",
            "name": "x",
            "entries": [{"kind": "bytes", "offset": hex(MAX_OFFSET), "off": "90", "on": "cc"}],
        }
    ]
    path = tmp_path / "o.cmproj"
    path.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ProjectFileError):
        pj.load(path)


@pytest.mark.parametrize(
    "wrap",
    [
        lambda b: b"\xef\xbb\xbf" + b"\r\n".join(b[i : i + 76] for i in range(0, len(b), 76)),
        lambda b: b"\xef\xbb\xbf\r\n" + b + b"\r\n",
    ],
    ids=["bom-crlf-76", "bom-newline"],
)
def test_rt_mc4_bom_with_wrapped_base64(tmp_path, wrap):  # TC-58b
    cf = formats.load_path(GOLDEN / "CUSA02396_01.01.json")
    path = tmp_path / "x.mc4"
    path.write_bytes(wrap(mc4.dumps(cf)))
    assert len(formats.load_path(path).cheats) == len(cf.cheats)


@pytest.mark.parametrize(
    ("data", "fmt"),
    [
        ('<?xml version="1.0"?><Trainer/>'.encode("utf-16"), "shn"),
        (b"\xfe\xff" + "  <Trainer/>".encode("utf-16-be"), "shn"),
        ('{"name": "x"}'.encode("utf-16"), "json"),
        (b"\xef\xbb\xbf  {}", "json"),
        (b"\xef\xbb\xbfQUJD", None),
        (b"\xff\xfe", None),
        (b"", None),
    ],
)
def test_rt_sniff_looks_past_any_bom(data, fmt):  # TC-59b
    assert formats.sniff(data) == fmt


def test_rt_utf16_shn_still_loads_under_a_json_name(tmp_path):  # TC-59c
    text = (GOLDEN / "CUSA15436_1.05.shn").read_bytes()
    path = tmp_path / "CUSA15436_1.05.json"
    path.write_bytes(text)
    assert formats.load_path(path).cheats
