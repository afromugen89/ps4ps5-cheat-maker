"""Pro step 5: pure-Python AES-256-CBC and .mc4 files (spec-pro PR-12, AC-12.2, AC-12.4)."""

from __future__ import annotations

import base64
import logging
import xml.etree.ElementTree as ET

import pytest

from cheatmaker import formats
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind
from cheatmaker.core.values import ValueType
from cheatmaker.errors import CheatFileError
from cheatmaker.formats import _aes, goldhen_json, mc4, shn

# -- NIST SP 800-38A F.2 (CBC) and FIPS-197 C known-answer vectors -----------------------
IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
PLAIN = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710"
)
CBC_VECTORS = [
    (  # F.2.1 CBC-AES128
        "2b7e151628aed2a6abf7158809cf4f3c",
        "7649abac8119b246cee98e9b12e9197d"
        "5086cb9b507219ee95db113a917678b2"
        "73bed6b8e3c1743b7116e69e22229516"
        "3ff1caa1681fac09120eca307586e1a7",
    ),
    (  # F.2.3 CBC-AES192
        "8e73b0f7da0e6452c810f32b809079e562f8ead2522c6b7b",
        "4f021db243bc633d7178183a9fa071e8"
        "b4d9ada9ad7dedf4e5e738763f69145a"
        "571b242012fb7ae07fa9baac3df102e0"
        "08b0e27988598881d920a9e64f5615cd",
    ),
    (  # F.2.5 CBC-AES256: the mode .mc4 uses
        "603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4",
        "f58c4c04d6e5f1ba779eabfb5f7bfbd6"
        "9cfc4e967edb808d679f777bc6702c7d"
        "39f23369a9d9bacfa530e26304231461"
        "b2eb05e2c39be9fcda6c19078c6a9d1b",
    ),
]


@pytest.mark.parametrize(("key", "cipher"), CBC_VECTORS)
def test_nist_sp800_38a_cbc_vectors(key, cipher):
    k, c = bytes.fromhex(key), bytes.fromhex(cipher)
    assert _aes.cbc_encrypt(k, IV, PLAIN) == c
    assert _aes.cbc_decrypt(k, IV, c) == PLAIN


@pytest.mark.parametrize(
    ("key", "cipher"),
    [
        ("000102030405060708090a0b0c0d0e0f", "69c4e0d86a7b0430d8cdb78070b4c55a"),
        ("000102030405060708090a0b0c0d0e0f1011121314151617", "dda97ca4864cdfe06eaf70a0ec0d7191"),
        (
            "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
            "8ea2b7ca516745bfeafc49904b496089",
        ),
    ],
)
def test_fips197_block_vectors(key, cipher):
    aes = _aes.AES(bytes.fromhex(key))
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    assert aes.encrypt_block(plain).hex() == cipher
    assert aes.decrypt_block(bytes.fromhex(cipher)) == plain


@pytest.mark.parametrize(
    ("call", "msg"),
    [
        (lambda: _aes.AES(b"short"), "16, 24 or 32"),
        (lambda: _aes.cbc_encrypt(b"k" * 32, b"iv", b"x" * 16), "IV"),
        (lambda: _aes.cbc_encrypt(b"k" * 32, b"i" * 16, b"x" * 15), "multiple of 16"),
        (lambda: _aes.cbc_decrypt(b"k" * 32, b"i" * 16, b"x" * 17), "multiple of 16"),
        (lambda: _aes.pkcs7_unpad(b""), "padding"),
        (lambda: _aes.pkcs7_unpad(b"a" * 15 + b"\x00"), "padding"),
        (lambda: _aes.pkcs7_unpad(b"a" * 14 + b"\x01\x02"), "padding"),
        (lambda: _aes.pkcs7_unpad(b"a" * 15 + b"\x11"), "padding"),
    ],
)
def test_aes_refuses_bad_input(call, msg):
    with pytest.raises(ValueError, match=msg):
        call()


@pytest.mark.parametrize("n", [0, 1, 15, 16, 17, 31, 32])
def test_pkcs7_round_trip(n):
    data = bytes(range(n))
    padded = _aes.pkcs7_pad(data)
    assert len(padded) % 16 == 0 and len(padded) > n
    assert _aes.pkcs7_unpad(padded) == data


# -- .mc4 -------------------------------------------------------------------------------
def _project_file() -> CheatFile:
    health = Cheat(
        "Infinite Health",
        [Location(LocationKind.CODE_PATCH, 0xAB7B38, b"\x90" * 4, bytes.fromhex("C5FA58C1"))],
    )
    gold = Cheat(
        "Gold",
        [Location(LocationKind.MODULE_VALUE, 0x1010, b"\x3f\x42\x0f\x00", b"\x64\x00\x00\x00")],
        value_type=ValueType.INT32,
    )
    heap = Cheat("Heap", [Location(LocationKind.SESSION_ADDRESS, 0x2000_0000, b"\1", b"\0")])
    return CheatFile("Test Game", "PPSA01234", "01.000", [health, gold, heap])


def test_mc4_is_base64_of_aes_of_the_shn():
    cf = _project_file()
    data = mc4.dumps(cf)
    raw = base64.b64decode(data, validate=True)
    assert len(raw) % 16 == 0
    plain = _aes.pkcs7_unpad(_aes.cbc_decrypt(mc4.KEY, mc4.IV, raw))
    assert plain == shn.dumps(cf, sanitise=True)
    assert len(mc4.KEY) == 32 and len(mc4.IV) == 16


def test_exported_mc4_imported_again_equals_the_shn(tmp_path):  # AC-12.2
    cf = _project_file()
    path = tmp_path / cf.file_name("mc4")
    path.write_bytes(formats.dumps(cf, "mc4"))
    assert path.name == "PPSA01234_01.000.mc4"
    sanitised = shn.dumps(cf, sanitise=True)
    assert mc4.decrypt(path.read_bytes()) == sanitised, "byte for byte"
    assert sanitised == formats.dumps(cf, "shn"), 'no & < > " here: same as plain .shn'
    back = formats.load_path(path)
    from_shn = shn.loads(formats.dumps(cf, "shn"))
    assert back == from_shn
    assert [c.name for c in back.cheats] == ["Infinite Health", "Gold"], "heap one left out"
    assert back.cheats[1].value_type is ValueType.INT32 and back.cheats[1].value == 999999


def test_xml_characters_in_mc4_are_replaced_and_plain_shn_escapes_them(tmp_path):  # AC-12.4
    cf = _project_file()
    cf.title = 'Ratchet & Clank: <Rift> "Apart"'
    cf.credits = ["Kein & co"]
    cf.cheats[0].name = 'God & "mode" <on>'
    data = formats.dumps(cf, "mc4")
    plain = mc4.decrypt(data)
    assert plain == shn.dumps(cf, sanitise=True)  # AC-12.2
    root = ET.fromstring(plain.decode("utf-8").split("?>", 1)[1])
    assert root.get("Game") == "Ratchet + Clank: (Rift) 'Apart'"
    assert root.get("Moder") == "Kein + co"
    assert root.find("Cheat").get("Text") == "God + 'mode' (on)"
    assert b"&" not in plain and b"&lt;" not in plain
    path = tmp_path / "x.mc4"
    path.write_bytes(data)
    back = formats.load_path(path)
    assert back.title == "Ratchet + Clank: (Rift) 'Apart'"
    assert back.cheats[0].name == "God + 'mode' (on)"
    # plain .shn is unchanged from before Pro: escaped, names kept exactly
    text = formats.dumps(cf, "shn")
    assert b"""Game='Ratchet &amp; Clank: &lt;Rift&gt; "Apart"'""" in text
    assert shn.loads(text).cheats[0].name == 'God & "mode" <on>'
    assert shn.loads(text).title == cf.title
    j = goldhen_json.loads(formats.dumps(cf, "json"))
    assert j.cheats[0].name == 'God & "mode" <on>', "JSON keeps the name as it is"


def test_reads_reaperpy_style_mc4():
    """The plaintext ReaperPy's build_mc4 makes (utf-16 declared, utf-8 bytes, CRLF, no
    final newline, its own sanitised names, a Section per line)."""
    text = (
        b'<?xml version="1.0" encoding="utf-16"?>\r\n'
        b'<Trainer xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        b'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" Game="Test + Game" '
        b'Moder="Made with ReaperPy" Cusa="PPSA01234" Version="01.000" Process="eboot.bin">\r\n'
        b"  <Items />\r\n"
        b"  <Cheat Text=\"Infinite 'Health' (x) + y\">\r\n"
        b"    <Cheatline>\r\n"
        b"      <Offset>AB7B38</Offset>\r\n"
        b"      <Section>0</Section>\r\n"
        b"      <ValueOn>90-90-90-90</ValueOn>\r\n"
        b"      <ValueOff>C5-FA-58-C1</ValueOff>\r\n"
        b"    </Cheatline>\r\n"
        b"  </Cheat>\r\n"
        b"</Trainer>"
    )
    data = base64.b64encode(_aes.cbc_encrypt(mc4.KEY, mc4.IV, _aes.pkcs7_pad(text)))
    wrapped = b"\r\n".join(data[i : i + 76] for i in range(0, len(data), 76)) + b"\r\n"
    cf = mc4.loads(wrapped)
    assert cf.title == "Test + Game" and cf.title_id == "PPSA01234" and cf.version == "01.000"
    [c] = cf.cheats
    assert c.name == "Infinite 'Health' (x) + y"
    assert c.locations[0].offset == 0xAB7B38 and c.locations[0].off == bytes.fromhex("C5FA58C1")


@pytest.mark.parametrize("filler", [b" ", b"\x00"])
def test_mc4_padded_with_spaces_or_nuls_is_still_read(caplog, filler):
    text = shn.dumps(_project_file())
    text += filler * (-len(text) % 16 or 16)  # never valid PKCS#7
    data = base64.b64encode(_aes.cbc_encrypt(mc4.KEY, mc4.IV, text))
    plain = mc4.decrypt(data)
    assert not plain.endswith((b"\x00", b" ", b"\n"))
    assert mc4.loads(data).title_id == "PPSA01234"
    assert "action=mc4_load reason=bad padding" in caplog.text


@pytest.mark.parametrize("bom", [b"", b"\xff\xfe"])
@pytest.mark.parametrize("pkcs7", [True, False])
def test_utf16_le_mc4_is_read(bom, pkcs7):
    text = bom + shn.dumps(_project_file()).decode("utf-8").encode("utf-16-le")
    if pkcs7:
        text = _aes.pkcs7_pad(text)
    else:
        text += b"\x00" * (-len(text) % 16 or 16)
    cf = mc4.loads(base64.b64encode(_aes.cbc_encrypt(mc4.KEY, mc4.IV, text)))
    assert cf.title_id == "PPSA01234"
    assert [c.name for c in cf.cheats] == ["Infinite Health", "Gold"]


@pytest.mark.parametrize(
    ("data", "msg"),
    [
        (b"not base64 at all!", "base64"),
        (b"", "length"),
        (base64.b64encode(b"x" * 15), "length"),
        (base64.b64encode(b"x" * 32), "decrypt"),
        (
            base64.b64encode(_aes.cbc_encrypt(b"k" * 32, mc4.IV, _aes.pkcs7_pad(b"<Trainer/>"))),
            "decrypt",
        ),
    ],
)
def test_broken_mc4_raises_cheatfileerror(data, msg):
    with pytest.raises(CheatFileError, match=msg):
        mc4.loads(data)


def test_mc4_refuses_when_nothing_can_be_saved():
    cf = CheatFile("G", "PPSA00001", "01.000", [_project_file().cheats[2]])
    with pytest.raises(ValueError):
        formats.dumps(cf, "mc4")


# -- content sniffing -------------------------------------------------------------------
def test_wrong_extension_is_read_by_its_content(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    cf = _project_file()
    xml_as_mc4 = tmp_path / "a.mc4"
    xml_as_mc4.write_bytes(formats.dumps(cf, "shn"))
    json_as_shn = tmp_path / "b.shn"
    json_as_shn.write_bytes(formats.dumps(cf, "json"))
    utf16_as_json = tmp_path / "c.json"
    utf16_as_json.write_bytes(formats.dumps(cf, "shn").decode("utf-8").encode("utf-16"))
    for p in (xml_as_mc4, json_as_shn, utf16_as_json):
        assert [c.name for c in formats.load_path(p).cheats] == ["Infinite Health", "Gold"]
    assert "action=load_file name=a.mc4 ext=mc4 content=shn" in caplog.text


@pytest.mark.parametrize(
    ("data", "fmt"),
    [
        (b"\xef\xbb\xbf  <?xml", "shn"),
        (b"\r\n<Trainer", "shn"),
        (b"\xff\xfe<\x00", "shn"),
        (b' {"mods": []}', "json"),
        (b"U2FsdGVk", None),
        (b"", None),
    ],
)
def test_sniff(data, fmt):
    assert formats.sniff(data) == fmt
