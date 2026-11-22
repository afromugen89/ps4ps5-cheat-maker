"""Pro step 3: the Pro cheat model and .cmproj format 5 (spec-pro §5, AC-S3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cheatmaker.core import project as pj
from cheatmaker.core.cheats import Cheat, Location, LocationKind
from cheatmaker.core.pro.model import ProCheat, ProEntry, Variable, new_cheat_id
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ProjectFileError

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden" / "PPSA01285_03.000.005.cmproj"
JMP = bytes.fromhex("e98f000000")


def _pro_cheat() -> ProCheat:
    return ProCheat(
        "p1",
        "Health x mult",
        [
            ProEntry(
                "cave",
                0x5E1536C,
                bytes.fromhex("f30f114810"),
                JMP,
                source="mulss xmm1, [mult]\noriginal\njmp return\n",
                cave=0x7FF0100,
                cave_bytes=bytes.fromhex("f30f590d10000000"),
                aob="F3 0F 11 48 10 ?? ??",
            ),
            ProEntry("nop", 0x5E1540A, bytes.fromhex("894b08"), comment="no ammo use"),
            ProEntry("bytes", 0x100, b"\x01\x02", b"\x03\x04", aob="01 02"),
        ],
        [Variable("mult", ValueType.FLOAT, 2.0, "damage taken x")],
        editable="mult",
        comment="from the video",
    )


# -- AC-S3: format 4 loads unchanged and saves as format 5 ------------------------------
def test_format_4_file_loads_and_saves_as_5_with_nothing_else_changed(tmp_path):  # AC-S3
    v5 = GOLDEN.read_text(encoding="utf-8")
    v4 = v5.replace('"format_version": 5,', '"format_version": 4,')
    assert v4 != v5
    path = tmp_path / "PPSA01285_03.000.005.cmproj"
    path.write_text(v4, encoding="utf-8")
    p = pj.load(path)
    golden = pj.load(GOLDEN)
    assert p.objects == golden.objects and p.cheats == golden.cheats
    assert [o.anchors for o in p.objects] == [o.anchors for o in golden.objects]
    assert p.pro_cheats == []
    pj.save(path, p)
    saved = path.read_text(encoding="utf-8")
    assert json.loads(saved)["format_version"] == 5
    assert saved == v5, "only the version changes"


def test_pro_cheats_round_trip(tmp_path):
    p = pj.load(GOLDEN)
    p.pro_cheats = [_pro_cheat(), ProCheat("p2", "Empty draft")]
    path = tmp_path / "x.cmproj"
    pj.save(path, p)
    back = pj.load(path)
    assert back == p
    assert back.pro_cheats[0].entries[1].on == b"\x90\x90\x90"
    doc = json.loads(path.read_text())
    assert list(doc)[-1] == "pro_cheats"
    cave, nop, raw = doc["pro_cheats"][0]["entries"]
    assert cave == {
        "kind": "cave",
        "hook": "0x5e1536c",
        "cave": "0x7ff0100",
        "source": "mulss xmm1, [mult]\noriginal\njmp return\n",
        "off_hook": "f30f114810",
        "on_hook": "e98f000000",
        "cave_bytes": "f30f590d10000000",
        "aob": "F3 0F 11 48 10 ?? ??",
        "comment": "",
    }
    assert nop == {
        "kind": "nop",
        "offset": "0x5e1540a",
        "off": "894b08",
        "aob": "",
        "comment": "no ammo use",
    }, "plain NOPs are implied"
    assert raw["on"] == "0304" and raw["off"] == "0102"
    assert doc["pro_cheats"][0]["variables"] == [
        {"name": "mult", "type": "float", "value": 2.0, "comment": "damage taken x"}
    ]
    assert doc["pro_cheats"][0]["editable"] == "mult"


def test_hex_with_spaces_and_unplaced_cave_load():
    e = ProEntry.from_json({"kind": "nop", "offset": "0x10", "off": "89 4b 08"})
    assert e.off == b"\x89\x4b\x08" and e.on == b"\x90" * 3
    cave = ProEntry.from_json(
        {"kind": "cave", "hook": "0x20", "cave": None, "off_hook": "f30f114810", "source": "ret"}
    )
    assert cave.cave is None and cave.on == b"" and cave.cave_bytes == b""


def test_a_nop_entry_with_its_own_nops_keeps_them():
    e = ProEntry("nop", 0x10, b"\x89\x4b\x08", b"\x0f\x1f\x00")
    assert e.to_json()["on"] == "0f1f00"
    assert ProEntry.from_json(e.to_json()) == e


# -- an older app and a newer file ------------------------------------------------------
def test_an_older_app_refuses_format_5_and_sets_it_aside(tmp_path, monkeypatch):
    p = pj.load(GOLDEN)
    p.pro_cheats = [_pro_cheat()]
    path = tmp_path / "PPSA01285_03.000.005.cmproj"
    pj.save(path, p)
    original = path.read_text()
    monkeypatch.setattr(pj, "FORMAT_VERSION", 4)  # an app from before Pro
    with pytest.raises(ProjectFileError, match=r"made by a newer Cheat Maker \(format 5\)"):
        pj.load(path)
    bak = pj.set_aside(path)
    assert bak is not None and bak.name.endswith(".cmproj.bak")
    assert bak.read_text() == original and not path.exists()


def test_format_6_is_refused_by_this_app(tmp_path):
    doc = json.loads(GOLDEN.read_text())
    doc["format_version"] = 6
    path = tmp_path / "new.cmproj"
    path.write_text(json.dumps(doc))
    with pytest.raises(ProjectFileError, match=r"newer Cheat Maker \(format 6\)"):
        pj.load(path)


# -- bad Pro cheats ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("make", "msg"),
    [
        (lambda: Variable("2x", ValueType.FLOAT, 1.0), "letters, digits"),
        (lambda: Variable("return", ValueType.FLOAT, 1.0), "already uses"),
        (lambda: Variable("v", ValueType.INT8, 300), "doesn't fit"),
        (lambda: Variable("v", ValueType.UINT8, -1), "doesn't fit"),
        (lambda: Variable("v", ValueType.INT32, 2.5), "whole number"),
        (lambda: Variable("v", ValueType.FLOAT, float("nan")), "finite"),
        (lambda: Variable("v", ValueType.FLOAT, True), "not a number"),
        (lambda: ProEntry("bytes", 0, b"\x01", b"\x01\x02"), "same length"),
        (lambda: ProEntry("bytes", 0, b"\x01"), "new bytes"),
        (lambda: ProEntry("bytes", 0, b""), "original bytes"),
        (lambda: ProEntry("bytes", 0, b"\x01", b"\x02", source="nop"), "only a cave"),
        (lambda: ProEntry("nop", 0, b"\x01", cave=5), "only a cave"),
        (lambda: ProEntry("cave", 0, b"\x01\x02\x03\x04"), "at least 5"),
        (lambda: ProEntry("cave", 0, b"\x01" * 5, cave_bytes=b"\xc3"), "cave's place"),
        (lambda: ProEntry("jmp", 0, b"\x01", b"\x02"), "unknown entry kind"),  # type: ignore[arg-type]
        (lambda: ProEntry("bytes", -1, b"\x01", b"\x02"), "negative"),
        (lambda: ProCheat("p1", "x", editable="mult"), "doesn't exist"),
        (lambda: ProCheat("p1", "x", origin="mars"), "unknown origin"),
        (lambda: ProCheat("", "x"), "needs an id"),
        (
            lambda: ProCheat(
                "p1", "x", variables=[Variable("a", ValueType.INT8, 1)] * 2, editable=None
            ),
            "share a name",
        ),
    ],
)
def test_bad_pro_cheats_are_refused(make, msg):
    with pytest.raises(ValueError, match=msg):
        make()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["pro_cheats"].append(dict(d["pro_cheats"][0])),  # same id twice
        lambda d: d["pro_cheats"][0]["variables"][0].update(type="int128"),
        lambda d: d["pro_cheats"][0]["entries"][0].update(hook=-5),
        lambda d: d["pro_cheats"][0]["entries"][1].update(off="zz"),
        lambda d: d["pro_cheats"][0].update(entries="nope"),
        lambda d: d["pro_cheats"][0].update(editable=3),
        lambda d: d.update(pro_cheats={}),
    ],
)
def test_broken_pro_cheats_make_the_file_unreadable(tmp_path, mutate):
    p = pj.load(GOLDEN)
    p.pro_cheats = [_pro_cheat()]
    doc = p.to_json()
    mutate(doc)
    path = tmp_path / "bad.cmproj"
    path.write_text(json.dumps(doc))
    with pytest.raises(ProjectFileError):
        pj.load(path)


# -- Cheat.pro and ids ------------------------------------------------------------------
def test_cheat_pro_link_is_not_part_of_equality():
    loc = Location(LocationKind.CODE_PATCH, 0x100, b"\x90", b"\x89")
    plain = Cheat("x", [loc])
    linked = Cheat("x", [loc], pro=_pro_cheat())
    assert plain == linked and plain.pro is None and linked.pro.id == "p1"


def test_variable_bytes_and_lookup():
    c = _pro_cheat()
    assert c.variable("mult").encoded() == ValueType.FLOAT.encode(2.0)
    assert Variable("n", ValueType.UINT16, 65535).encoded() == b"\xff\xff"
    with pytest.raises(KeyError):
        c.variable("nope")


def test_new_cheat_id_fills_the_first_gap():
    assert new_cheat_id([]) == "p1"
    assert new_cheat_id([ProCheat("p1", "a"), ProCheat("p3", "b")]) == "p2"
