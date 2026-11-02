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
        [pj.Field("Gold", 0x10, ValueType.INT32)],
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
