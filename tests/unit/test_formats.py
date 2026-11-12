from pathlib import Path

import pytest

from cheatmaker import formats
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.errors import CheatFileError
from cheatmaker.formats import goldhen_json, shn

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden"


@pytest.mark.parametrize("name", sorted(p.name for p in GOLDEN.glob("*.json")))
def test_real_json_files_load_and_roundtrip(name):  # AC-6.6
    cf = formats.load_path(GOLDEN / name)
    assert cf.title_id.startswith("CUSA") and cf.cheats
    again = goldhen_json.loads(goldhen_json.dumps(cf))
    assert [(c.name, c.locations) for c in again.cheats] == [
        (c.name, c.locations) for c in cf.cheats
    ]
    assert again.version == cf.version and again.title == cf.title


def test_real_json_exact_content():
    cf = formats.load_path(GOLDEN / "CUSA02396_01.01.json")
    hearts = cf.cheats[2]
    assert hearts.name == "Infinite Hearts"
    assert hearts.locations[0] == Location(
        LocationKind.CODE_PATCH, 0xF51976, b"\x90\x90", b"\x89\x08"
    )


def test_real_shn_loads_and_roundtrips():
    cf = formats.load_path(GOLDEN / "CUSA15436_1.05.shn")
    assert cf.title_id == "CUSA15436" and cf.version == "01.05"
    assert cf.cheats[0].name == "Add Items & Money"
    assert cf.cheats[1].locations[0].on == b"\x90" * 6
    again = shn.loads(shn.dumps(cf))
    assert [(c.name, c.locations) for c in again.cheats] == [
        (c.name, c.locations) for c in cf.cheats
    ]


def _sample():
    return CheatFile(
        "Spider <Man> & Co",
        "PPSA01234",
        "01.002",
        [
            Cheat(
                "Infinite Health",
                [Location(LocationKind.CODE_PATCH, 0xAB7B38, b"\x90" * 4, b"\xc5\xfa\x58\xc1")],
            ),
            Cheat(
                "Gold",
                [
                    Location(
                        LocationKind.MODULE_VALUE, 0x10, b"\x9f\x86\x01\x00", b"\x92\x09\x00\x00"
                    )
                ],
            ),
            Cheat("Heap only", [Location(LocationKind.SESSION_ADDRESS, 0x2000_0000, b"\1", b"\0")]),
        ],
    )


def test_json_output_shape_matches_goldhen():  # AC-6.1, AC-6.6
    import json

    doc = json.loads(goldhen_json.dumps(_sample()))
    assert list(doc) == ["name", "id", "version", "process", "mods", "credits"]
    assert doc["process"] == "eboot.bin" and doc["id"] == "PPSA01234"
    assert [m["name"] for m in doc["mods"]] == ["Infinite Health", "Gold"]  # heap one skipped
    assert doc["mods"][0] == {
        "name": "Infinite Health",
        "type": "checkbox",
        "memory": [{"offset": "AB7B38", "on": "90909090", "off": "C5FA58C1"}],
    }


def test_shn_output_shape_and_escaping():
    text = shn.dumps(_sample()).decode("utf-8")
    assert text.startswith('<?xml version="1.0" encoding="utf-16"?>\r\n<Trainer ')
    assert "<ValueOn>90-90-90-90</ValueOn>" in text and "<Section>0</Section>" in text
    assert 'Game="Spider &lt;Man&gt; &amp; Co"' in text and "Heap only" not in text
    assert shn.loads(text.encode("utf-8")).title == "Spider <Man> & Co"


def test_shn_accepts_real_utf16():
    data = shn.dumps(_sample()).decode("utf-8").encode("utf-16")
    assert shn.loads(data).title_id == "PPSA01234"


def test_dumps_refuses_when_nothing_saveable():
    cf = CheatFile(
        "G",
        "PPSA00001",
        "01.000",
        [Cheat("h", [Location(LocationKind.SESSION_ADDRESS, 1, b"\1", b"\0")])],
    )
    for fmt in ("json", "shn"):
        with pytest.raises(ValueError):
            formats.dumps(cf, fmt)


@pytest.mark.parametrize(
    "ext,content",
    [
        ("json", b"{not json"),
        ("json", b"[]"),
        ("json", b'{"id":"X","version":"1"}'),
        ("json", b'{"id":"X","version":"1","mods":[{"name":"a","memory":[]}]}'),
        (
            "json",
            b'{"id":"X","version":"1","mods":[{"memory":[{"offset":"ZZ","on":"90","off":"00"}]}]}',
        ),
        (
            "json",
            b'{"id":"X","version":"1","mods":[{"memory":[{"offset":"1","on":"9090","off":"00"}]}]}',
        ),
        ("json", b"\xff\xfe\x00garbage"),
        ("shn", b"<Trainer"),
        ("shn", b"<Other/>"),
        ("shn", b'<Trainer Cusa="C"/>'),
        ("shn", b'<Trainer Cusa="C" Version="1"><Cheat Text="a"/></Trainer>'),
        (
            "shn",
            b'<Trainer Cusa="C" Version="1"><Cheat Text="a"><Cheatline><Offset>1</Offset>'
            b"<Section>x</Section><ValueOn>90</ValueOn><ValueOff>00</ValueOff></Cheatline>"
            b"</Cheat></Trainer>",
        ),
        ("mc4", b"anything"),
    ],
)
def test_broken_files_raise_cheatfileerror(tmp_path, ext, content):  # AC-7.3
    p = tmp_path / f"bad.{ext}"
    p.write_bytes(content)
    with pytest.raises(CheatFileError):
        formats.load_path(p)


def test_missing_file_raises_cheatfileerror(tmp_path):
    with pytest.raises(CheatFileError):
        formats.load_path(tmp_path / "nope.json")


def test_unknown_format_dumps():
    with pytest.raises(ValueError):
        formats.dumps(_sample(), "mc4")


def test_linked_pool_and_places_are_never_written_or_compared():  # spec-linked-values L2
    from dataclasses import replace

    from cheatmaker.core.cheats import combine
    from cheatmaker.core.linked import LinkedGroup, PoolMember
    from cheatmaker.core.values import ValueType

    pool = LinkedGroup((PoolMember(0x2000_0000, ValueType.FLOAT, 1.0),))
    plain = _sample()
    marked = _sample()
    marked.cheats = [replace(c, linked=pool, places=2) for c in marked.cheats]
    assert marked.cheats == plain.cheats, "in-memory fields don't count for equality"
    assert goldhen_json.dumps(marked) == goldhen_json.dumps(plain)
    assert shn.dumps(marked) == shn.dumps(plain)
    again = goldhen_json.loads(goldhen_json.dumps(marked))
    assert all(c.linked is None and c.places == 1 for c in again.cheats)
    both = combine(marked.cheats[:2])
    assert both.linked is None and both.places == 1, "Combine makes a cheat with no pool"
