"""v2 update-proof cheats: learn signatures on one version, move cheats to the next."""

import struct

import pytest

from cheatmaker.core import updater as up
from cheatmaker.core.cheats import Cheat, CheatFile, Location, LocationKind

HOOK = 0x277B43F
CAVE = 0x61D7DB0
VMOVSS = bytes.fromhex("C4C17A118C24AC020000")  # vmovss [r12+0x2AC], xmm1
# Celogamez's Returnal file (03.000.005): player-only cave + hook + a toggle in the cave
CAVE_ON = bytes.fromhex(
    "41817C240848000400EB2041C78424AC02000000C07944E97D365AFCC4C17A118C24AC020000"
    "E96E365AFC41837C242414EBE941C78424AC02000000000000E955365AFC"
)
HOOK_ON = bytes.fromhex("E96CC9A5039090909090")


def code_around(hook_off: int, rip_disp: int, call_rel: int) -> bytes:
    """Code at a hook site: some int3 filler, a mov, the writer, then code using
    RIP-relative data and a call (both change between builds)."""
    before = b"\xcc" * 0x3F + bytes.fromhex("4889C3")  # mov rbx, rax
    after = (
        bytes.fromhex("488B05")
        + struct.pack("<i", rip_disp)  # mov rax, [rip+disp]
        + b"\xe8"
        + struct.pack("<i", call_rel)  # call rel32
        + bytes.fromhex("4883C408C3")  # add rsp, 8 ; ret
    )
    return before + VMOVSS + after + b"\xcc" * 0x100


def image(
    hook_off: int, cave_off: int, rip_disp: int, call_rel: int, cave_bytes=b""
) -> up.CodeImage:
    site = code_around(hook_off, rip_disp, call_rel)
    site_start = hook_off - 0x42
    tail = b"\x90" * 0x40 + cave_bytes + bytes(0x400 - len(cave_bytes))
    return up.CodeImage([(site_start, site), (cave_off - 0x40, tail)])


def celogamez() -> CheatFile:
    def loc(off, on, orig):
        return Location(LocationKind.CODE_PATCH, off, on, orig)

    return CheatFile(
        "Returnal",
        "PPSA01285",
        "03.000.005",
        [
            Cheat(
                "Infinite Health/1 Hit Kill",
                [loc(CAVE, CAVE_ON, bytes(len(CAVE_ON))), loc(HOOK, HOOK_ON, VMOVSS)],
            ),
            Cheat("Infinite Health", [loc(CAVE + 9, bytes.fromhex("7520"), bytes.fromhex("EB20"))]),
        ],
    )


def test_signature_text_roundtrip_and_match():
    s = up.Signature.from_text("C4 C1 ?? 11")
    assert s.text() == "C4 C1 ?? 11" and len(s) == 4
    assert s.matches(bytes.fromhex("C4C1FF11")) and not s.matches(bytes.fromhex("C4C2FF11"))
    with pytest.raises(ValueError):
        up.Signature(b"\x01", b"")


def test_make_signature_wildcards_jumps_and_rip_relative():
    old = image(HOOK, CAVE, 0x1000, 0x2000)
    code = old.read(HOOK, 64)
    sig = up.make_signature(code, HOOK, min_len=30)
    text = sig.text()
    assert text.startswith("C4 C1 7A 11 8C 24 AC 02 00 00 48 8B 05 ?? ?? ?? ?? E8 ?? ?? ?? ??")


def test_learn_is_unique_and_ignores_cheats_being_on():
    old = image(HOOK, CAVE, 0x1000, 0x2000)
    cf = celogamez()
    sigs = up.learn(cf, old)
    assert set(sigs) == {HOOK}, "caves and lines inside caves don't need signatures"
    assert old.find(sigs[HOOK]) == [HOOK]
    # same result while the hook is switched on (live bytes = jmp cave)
    on_site = bytearray(old.blocks[0][1])
    on_site[0x42 : 0x42 + len(HOOK_ON)] = HOOK_ON
    live = up.CodeImage([(old.blocks[0][0], bytes(on_site)), old.blocks[1]])
    assert up.learn(cf, live)[HOOK] == sigs[HOOK]


def test_update_moves_hook_cave_and_toggle_to_new_version():
    old = image(HOOK, CAVE, 0x1000, 0x2000)
    cf = celogamez()
    sigs = up.learn(cf, old)
    new_hook, new_cave_area = HOOK + 0x1234, CAVE + 0x8000
    new = image(new_hook, new_cave_area, 0x1F00, 0x2400)  # rel values changed too
    moved, report = up.update_file(cf, sigs, new, "03.000.006")
    assert moved.version == "03.000.006"
    assert report.moved == ["Infinite Health/1 Hit Kill", "Infinite Health"] and not report.failed
    cave, hook = moved.cheats[0].locations
    toggle = moved.cheats[1].locations[0]
    assert hook.offset == new_hook and hook.off == VMOVSS
    # hook jumps to the new cave
    assert hook.on[0] == 0xE9 and hook.on[5:] == b"\x90" * 5
    assert hook.offset + 5 + struct.unpack("<i", hook.on[1:5])[0] == cave.offset
    assert new.read(cave.offset, len(CAVE_ON)) == bytes(len(CAVE_ON)), "cave goes on spare zeros"
    assert cave.off == bytes(len(CAVE_ON)) and cave.offset % 16 == 0
    # every jmp back from the cave lands right after the new hook
    for at in (0x17, 0x26, 0x3F):
        rel = struct.unpack("<i", cave.on[at + 1 : at + 5])[0]
        assert cave.offset + at + 5 + rel == new_hook + 10
    # short jumps inside the cave are unchanged; the toggle moved with the cave
    assert cave.on[9:11] == CAVE_ON[9:11]
    assert toggle.offset == cave.offset + 9 and toggle.on == bytes.fromhex("7520")
    assert toggle.off == bytes.fromhex("EB20")


def test_update_without_signatures_uses_original_bytes():
    new = image(HOOK + 0x50, CAVE, 0x1F00, 0x2400)
    moved, report = up.update_file(celogamez(), {}, new, "03.000.006")
    assert moved.cheats[0].locations[1].offset == HOOK + 0x50
    assert report.guessed == ["Infinite Health/1 Hit Kill"]


def test_update_reports_missing_ambiguous_and_unfollowable_code():
    cf = celogamez()
    old = image(HOOK, CAVE, 0x1000, 0x2000)
    sigs = up.learn(cf, old)
    # writer gone from the new version
    gone = up.CodeImage([(0x1000, b"\xcc" * 0x200), old.blocks[1]])
    moved, report = up.update_file(cf, sigs, gone, "2")
    assert moved.cheats == [] and "not found" in report.failed["Infinite Health/1 Hit Kill"]
    # writer twice
    twice = up.CodeImage([old.blocks[0], (0x5000000, old.blocks[0][1]), old.blocks[1]])
    _, report = up.update_file(cf, sigs, twice, "2")
    assert "more than one place" in report.failed["Infinite Health/1 Hit Kill"]
    # a patch that calls into game code the app didn't move
    call_game = Cheat(
        "calls game",
        [Location(LocationKind.CODE_PATCH, HOOK, b"\xe8\x00\x10\x00\x00" + b"\x90" * 5, VMOVSS)],
    )
    cf2 = CheatFile("R", "PPSA01285", "1", [call_game])
    _, report = up.update_file(cf2, up.learn(cf2, old), old, "2")
    assert "can't follow" in report.failed["calls game"]
    assert "Not moved: calls game" in report.summary("2")


def test_session_and_value_cheats_are_not_moved():
    cf = CheatFile(
        "R",
        "PPSA01285",
        "1",
        [
            Cheat("heap", [Location(LocationKind.SESSION_ADDRESS, 0x1234, b"\x01", b"\x00")]),
            Cheat("static", [Location(LocationKind.MODULE_VALUE, 0x10, b"\x01", b"\x00")]),
        ],
    )
    _, report = up.update_file(cf, {}, image(HOOK, CAVE, 1, 2), "2")
    assert report.failed == {
        "heap": "this session only",
        "static": "it changes saved values, not code",
    }


def test_signatures_saved_per_version_and_merged(tmp_path):
    p = tmp_path / "s.json"
    up.save_signatures(p, "PPSA01285", "1", {1: up.Signature.from_text("AA ??")})
    up.save_signatures(p, "PPSA01285", "1", {2: up.Signature.from_text("BB")})
    assert {k: v.text() for k, v in up.load_signatures(p).items()} == {1: "AA ??", 2: "BB"}
    assert up.load_signatures(tmp_path / "none.json") == {}
