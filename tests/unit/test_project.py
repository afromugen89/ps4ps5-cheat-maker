""".cmproj model: golden file round trip, anchors, plausibility, bad files."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from cheatmaker.core import project as pj
from cheatmaker.core.values import ValueType
from cheatmaker.errors import ProjectFileError

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden" / "PPSA01285_03.000.005.cmproj"


def test_golden_file_loads_and_saves_byte_for_byte(tmp_path):
    p = pj.load(GOLDEN)
    assert p is not None and p.title_id == "PPSA01285" and p.version == "03.000.005"
    player = p.object("player")
    assert player.vtable == 0x5A1B2C0
    assert player.field("Health") == pj.Field("Health", 0x2AC, ValueType.FLOAT, (0, 10000))
    hook = player.hook
    assert hook is not None and hook.hook == 0x277B43F and hook.reg == "r12"
    assert hook.orig == bytes.fromhex("C4C17A118C24AC020000") and hook.role == "writer"
    assert hook.filter == pj.HookFilter("vtable") and hook.slot == 0x7F3FFF0
    [chain] = player.anchors_of(pj.ChainAnchor)
    assert chain.offsets[-1] == 0x2AC and chain.runs_seen == 3 and chain.field == "Health"
    assert player.has_proven_chain
    assert p.cheats == [pj.ProjectCheat("Infinite health", "player", "Health", "freeze", 999)]
    assert p.history[0].method == "hook_slot" and p.history[0].verified
    out = tmp_path / "x.cmproj"
    pj.save(out, p)
    assert out.read_text(encoding="utf-8") == GOLDEN.read_text(encoding="utf-8")


def test_every_anchor_kind_round_trips(tmp_path):
    obj = pj.ObjectSpec(
        "obj1",
        "Gold",
        None,
        [pj.Field("Gold", 0x10, ValueType.INT32, last=2450)],
        [
            pj.StaticAnchor(0x1234),
            pj.ChainAnchor(0x20, (0x10, 0x40), "Gold"),
            pj.HookAnchor(
                0x100,
                "mov eax,[rbx+10h]",
                b"\x8b\x43\x10\x90\x90",
                "rbx",
                0x10,
                "reader",
                pj.HookFilter("field", 0x8, 0x40048),
                0x3000,
                0x4FF0,
            ),
            pj.VtableAnchor(),
        ],
    )
    p = pj.Project("CUSA12345", "01.05", objects=[obj])
    path = pj.project_path("cusa12345", "01.05")
    assert path.name == "CUSA12345_01.05.cmproj" and path.parent.name == "projects"
    pj.save(path, p)
    assert pj.load(path) == p
    assert not obj.has_proven_chain


def test_missing_file_is_none_and_broken_file_raises(tmp_path):
    assert pj.load(tmp_path / "none.cmproj") is None
    bad = tmp_path / "bad.cmproj"
    for text in ("{", '{"format": "other"}', '{"format": "cheatmaker-project"}'):
        bad.write_text(text)
        with pytest.raises(ProjectFileError):
            pj.load(bad)
    doc = json.loads(GOLDEN.read_text())
    doc["format_version"] = 99
    bad.write_text(json.dumps(doc))
    with pytest.raises(ProjectFileError, match="newer"):
        pj.load(bad)
    doc["format_version"] = 1
    doc["objects"][0]["anchors"][0]["kind"] = "magic"
    bad.write_text(json.dumps(doc))
    with pytest.raises(ProjectFileError, match="unknown anchor"):
        pj.load(bad)


def test_field_plausible():
    f = pj.Field("Health", 0, ValueType.FLOAT, (0, 10000))
    assert f.plausible(struct.pack("<f", 87.5))
    assert not f.plausible(struct.pack("<f", -1.0))
    assert not f.plausible(struct.pack("<f", float("nan")))
    assert not f.plausible(b"\0")
    assert pj.Field("Gold", 0, ValueType.INT32).plausible(struct.pack("<i", -5)), "no range"


def test_ids_history_cap_and_put_object():
    p = pj.Project("PPSA01285", "1")
    assert p.new_object_id() == "obj1"
    p.put_object(pj.ObjectSpec("obj1", "A", None))
    p.put_object(pj.ObjectSpec("obj1", "B", None))
    assert [o.label for o in p.objects] == ["B"] and p.new_object_id() == "obj2"
    for i in range(pj.MAX_HISTORY + 5):
        p.remember(pj.HistoryEntry(str(i), "static", 0.0, True))
    assert len(p.history) == pj.MAX_HISTORY and p.history[-1].at == str(pj.MAX_HISTORY + 4)
    with pytest.raises(KeyError):
        p.object("nope")


def test_format_1_hooks_are_dropped_on_load(tmp_path):  # re-review: no PC-off room
    doc = json.loads(GOLDEN.read_text())
    doc["format_version"] = 1
    old = tmp_path / "old.cmproj"
    old.write_text(json.dumps(doc))
    p = pj.load(old)
    player = p.object("player")
    assert player.hook is None and player.anchors_of(pj.ChainAnchor)
    assert p.dropped_hooks == ["player"]
    assert player.field("Health").range == (0, pj.CLASS_FLOAT_LIMIT), "BUG-12: no 10,000 cap"
    pj.save(old, p)
    assert json.loads(old.read_text())["format_version"] == pj.FORMAT_VERSION
    assert pj.load(old).dropped_hooks == []


@pytest.mark.parametrize(
    "patch",
    [
        lambda d: d["cheats"][0].update(value="abc"),
        lambda d: d["cheats"][0].update(value=True),
        lambda d: d["objects"][0]["fields"][0].update(range="0-10"),
        lambda d: d["objects"][0]["fields"][0].update(off=1.5),
        lambda d: d["objects"][0].update(anchors={"kind": "static"}),
        lambda d: d.update(title_id=7),
    ],
)
def test_wrong_types_are_a_project_error(tmp_path, patch):  # BUG-05
    doc = json.loads(GOLDEN.read_text())
    patch(doc)
    bad = tmp_path / "bad.cmproj"
    bad.write_text(json.dumps(doc))
    with pytest.raises(ProjectFileError):
        pj.load(bad)


def test_set_aside_keeps_every_unreadable_file(tmp_path):  # BUG-06
    path = tmp_path / "g.cmproj"
    assert pj.set_aside(path) is None
    for n, want in ((1, "g.cmproj.bak"), (2, "g.cmproj.bak2")):
        path.write_text(f"v{n}")
        assert pj.set_aside(path).name == want and not path.exists()
    assert (tmp_path / "g.cmproj.bak").read_text() == "v1"


# -- format 3: combined cheats (spec-restart-combined AC-5) ----------------------------
def _combined() -> pj.ProjectCheat:
    return pj.ProjectCheat(
        "Health",
        action="freeze",
        parts=[
            pj.ProjectPart("health", "player", "Health", 999.0),
            pj.ProjectPart("fraction", "player", "Fraction", 1.0),
            pj.ProjectPart(
                "god flag",
                kind="module_value",
                offset=0x1A2B0,
                on=b"\x01",
                off=b"\x00",
                vtype=ValueType.INT8,
            ),
        ],
    )


def test_format_3_combined_cheat_round_trips(tmp_path):  # AC-5
    p = pj.load(GOLDEN)
    p.cheats.append(_combined())
    path = tmp_path / "x.cmproj"
    pj.save(path, p)
    doc = json.loads(path.read_text())
    assert doc["format_version"] == pj.FORMAT_VERSION
    entry = doc["cheats"][1]
    assert entry == {
        "name": "Health",
        "action": "freeze",
        "parts": [
            {"name": "health", "object": "player", "field": "Health", "value": 999.0},
            {"name": "fraction", "object": "player", "field": "Fraction", "value": 1.0},
            {
                "name": "god flag",
                "kind": "module_value",
                "module": "eboot.bin",
                "section": 0,
                "offset": "0x1a2b0",
                "on": "01",
                "off": "00",
                "type": "int8",
            },
        ],
    }
    assert doc["cheats"][0] == {  # a single cheat is saved exactly as before
        "name": "Infinite health",
        "object": "player",
        "field": "Health",
        "action": "freeze",
        "value": 999,
    }
    back = pj.load(path)
    assert back == p
    assert back.cheats[1].anchors == [("player", "Health"), ("player", "Fraction")]
    assert back.cheats[0].anchors == [("player", "Health")]
    assert back.cheats[1].parts[2].vtype is ValueType.INT8 and not back.cheats[1].parts[2].learned


@pytest.mark.parametrize("old", [1, 2, 3])
def test_formats_1_to_3_load_the_same_and_save_as_4(tmp_path, old):  # AC-5, AC-U10
    doc = json.loads(GOLDEN.read_text())
    doc["format_version"] = old
    path = tmp_path / "old.cmproj"
    path.write_text(json.dumps(doc))
    p = pj.load(path)
    assert p.cheats == [pj.ProjectCheat("Infinite health", "player", "Health", "freeze", 999)]
    if old >= 2:
        assert p == pj.load(GOLDEN), "every object and cheat as before"
    assert p.carried_from is None and not any(o.check or o.relearn_class for o in p.objects)
    hook = p.object("player").hook  # format 1 drops hooks
    assert hook is None or not hook.sig_unique, "older hooks are learned again (AC-U2)"
    pj.save(path, p)
    saved = path.read_text()
    assert json.loads(saved)["format_version"] == 4
    for key in ("carried_from", '"check"', "relearn_class", "signature_unique", '"test"'):
        assert key not in saved, "format 4 keys are written only when set"


def test_format_5_is_from_a_newer_app(tmp_path):  # AC-5: an older app's rule, one step up
    doc = json.loads(GOLDEN.read_text())
    doc["format_version"] = pj.FORMAT_VERSION + 1
    path = tmp_path / "new.cmproj"
    path.write_text(json.dumps(doc))
    with pytest.raises(ProjectFileError, match="newer Cheat Maker"):
        pj.load(path)


@pytest.mark.parametrize(
    "part",
    [
        {"name": "x", "kind": "session_address", "offset": "0x10", "on": "01", "off": "00"},
        {"name": "x", "kind": "code_patch", "offset": "0x10", "on": "0102", "off": "00"},
        {"name": "x", "kind": "code_patch", "offset": "0x10", "on": "", "off": ""},
        {"name": "x", "kind": "code_patch", "offset": "0x10", "on": "zz", "off": "00"},
        {"name": "x", "object": "player", "field": "Health", "value": "full"},
        {"name": "x", "object": "player", "value": 1},
    ],
)
def test_bad_parts_are_a_project_error(tmp_path, part):
    doc = json.loads(GOLDEN.read_text())
    doc["cheats"].append({"name": "Health", "action": "freeze", "parts": [part]})
    bad = tmp_path / "bad.cmproj"
    bad.write_text(json.dumps(doc))
    with pytest.raises(ProjectFileError):
        pj.load(bad)
    doc["cheats"][-1]["parts"] = []
    bad.write_text(json.dumps(doc))
    with pytest.raises(ProjectFileError, match="no parts"):
        pj.load(bad)


# -- format 4: notes brought over from another game version (spec-game-update AC-U7) ----
def test_format_4_keys_round_trip(tmp_path):
    p = pj.load(GOLDEN)
    p.carried_from = "03.000.004"
    player = p.object("player")
    player.check = player.relearn_class = True
    player.carried_from = "03.000.003"  # this value came from another version (Advanced)
    player.hook.sig_unique = True
    combined = _combined()
    combined.test = True
    p.cheats.append(combined)
    path = tmp_path / "x.cmproj"
    pj.save(path, p)
    doc = json.loads(path.read_text())
    assert doc["carried_from"] == "03.000.004"
    assert list(doc)[:5] == ["format", "format_version", "title_id", "version", "carried_from"]
    obj = doc["objects"][0]
    assert obj["check"] is True and obj["relearn_class"] is True
    assert obj["carried_from"] == "03.000.003"
    assert all("carried_from" not in o for o in doc["objects"][1:]), "only when set"
    assert obj["anchors"][0]["signature_unique"] is True
    assert doc["cheats"][1]["test"] is True and "test" not in doc["cheats"][0]
    assert pj.load(path) == p


@pytest.mark.parametrize(
    "patch",
    [
        lambda d: d.update(carried_from=5),
        lambda d: d["objects"][0].update(check="yes"),
        lambda d: d["objects"][0].update(relearn_class=1),
        lambda d: d["objects"][0].update(carried_from=3),
        lambda d: d["objects"][0]["anchors"][0].update(signature_unique="true"),
        lambda d: d["cheats"].append(
            {
                "name": "x",
                "action": "set",
                "test": 1,
                "parts": [{"name": "a", "object": "o", "field": "f", "value": 1}],
            }
        ),
    ],
)
def test_format_4_keys_of_the_wrong_type_are_a_project_error(tmp_path, patch):
    doc = json.loads(GOLDEN.read_text())
    patch(doc)
    bad = tmp_path / "bad.cmproj"
    bad.write_text(json.dumps(doc))
    with pytest.raises(ProjectFileError):
        pj.load(bad)
