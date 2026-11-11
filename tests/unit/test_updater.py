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


# -- bring notes to a new game version (spec-game-update) ---------------------------------
def test_overlay_puts_the_games_own_bytes_back_and_leaves_the_original_alone():
    old = image(HOOK, CAVE, 0x1000, 0x2000)
    on = bytearray(old.blocks[0][1])
    on[0x42 : 0x42 + len(HOOK_ON)] = HOOK_ON
    live = up.CodeImage([(old.blocks[0][0], bytes(on)), old.blocks[1]])
    clean = live.overlay([(HOOK, VMOVSS), (0x10, b"\x01" * 4)])  # outside the code: ignored
    assert clean.read(HOOK, len(VMOVSS)) == VMOVSS
    assert live.read(HOOK, len(HOOK_ON)) == HOOK_ON, "a copy: the image itself is unchanged"
    assert clean.blocks[1] == old.blocks[1]
    end = old.blocks[1][0] + len(old.blocks[1][1])
    edge = live.overlay([(end - 2, b"\xaa\xbb\xcc\xdd")])  # only the part inside the code
    assert edge.read(end - 2, 4) == b"\xaa\xbb"


def test_zero_run_defaults_are_unchanged_and_reserved_and_skip_are_kept():
    img = up.CodeImage([(0x1000, b"\xcc" * 0x10 + bytes(0x400))])
    end = 0x1000 + 0x410
    first = img.zero_run(0x40)
    assert first == img.zero_run(0x40, reserved=(), skip=0) == end - 0x50, "as before"
    taken = (first, end)
    moved = img.zero_run(0x40, reserved=[taken])
    assert moved + 0x40 <= taken[0] and moved >= 0x1010, "a reserved room counts as in use"
    assert img.zero_run(0x40, skip=0x100) + 0x40 <= end - 0x100, "the last bytes are left alone"
    with pytest.raises(up.UpdateError):
        img.zero_run(0x40, reserved=[(0x1000, end - 0x100)], skip=0xC0)
    assert img.zero_run(0x40, reserved=[(0, 0x1000), (end, end + 0x100)]) == first, (
        "ranges outside the code change nothing"
    )


def test_loose_opens_only_the_first_instructions_displacement():
    sig = up.Signature.from_text("C4 C1 7A 11 8C 24 AC 02 00 00 48 8B 5C 24 30")
    lo = up.loose(sig)
    assert lo.text() == "C4 C1 7A 11 8C 24 ?? ?? ?? ?? 48 8B 5C 24 30"
    assert lo.matches(bytes.fromhex("C4C17A118C24B4020000488B5C2430"))
    assert not sig.matches(bytes.fromhex("C4C17A118C24B4020000488B5C2430"))
    assert up.loose(up.Signature.from_text("48 89 C3 C3")) is None, "no displacement"
    rip = up.Signature.from_text("48 8B 05 ?? ?? ?? ?? C3")
    assert up.loose(rip) is None, "a RIP-relative one is already open"


def test_learn_at_is_unique_on_the_clean_image_and_skips_what_cant_be():
    old = image(HOOK, CAVE, 0x1000, 0x2000)
    sigs = up.learn_at(old, [(HOOK, len(VMOVSS))])
    assert old.find(sigs[HOOK]) == [HOOK] and len(sigs[HOOK]) >= up.SIG_MIN
    twice = up.CodeImage([old.blocks[0], (0x5000000, old.blocks[0][1])])
    assert up.learn_at(twice, [(HOOK, 10)]) == {}, "never unique: left out"
    assert up.learn_at(old, [(0x10, 10)]) == {}, "not code"
    on = bytearray(old.blocks[0][1])
    on[0x42 : 0x42 + len(HOOK_ON)] = HOOK_ON
    live = up.CodeImage([(old.blocks[0][0], bytes(on)), old.blocks[1]])
    clean = live.overlay([(HOOK, VMOVSS)])
    assert up.learn_at(clean, [(HOOK, 10)]) == sigs, "same with the hook on, once overlaid"


def test_update_file_places_caves_outside_reserved_rooms_and_skip():
    cf = celogamez()
    old = image(HOOK, CAVE, 0x1000, 0x2000)
    sigs = up.learn(cf, old)
    new = image(HOOK + 0x1234, CAVE + 0x8000, 0x1F00, 0x2400)
    plain, _ = up.update_file(cf, sigs, new, "2")
    cave = plain.cheats[0].locations[0]
    room = (cave.offset, cave.offset + len(CAVE_ON))
    moved, report = up.update_file(cf, sigs, new, "2", reserved=[room], cave_skip=0x40)
    assert not report.failed
    got = moved.cheats[0].locations[0]
    assert got.offset + len(CAVE_ON) <= room[0] or got.offset >= room[1]
    block_end = new.blocks[1][0] + len(new.blocks[1][1])
    assert got.offset + len(CAVE_ON) <= block_end - 0x40
    rel = struct.unpack("<i", moved.cheats[0].locations[1].on[1:5])[0]
    assert HOOK + 0x1234 + 5 + rel == got.offset, "the jump goes to the new room"


def test_failed_at_names_the_first_bad_line():
    cf = celogamez()
    old = image(HOOK, CAVE, 0x1000, 0x2000)
    sigs = up.learn(cf, old)
    gone = up.CodeImage([(0x1000, b"\xcc" * 0x200), old.blocks[1]])
    _, report = up.update_file(cf, sigs, gone, "2")
    assert report.failed_at["Infinite Health/1 Hit Kill"] == HOOK, "the patch, not its cave"
    cf3 = CheatFile(
        "R",
        "PPSA01285",
        "1",
        [Cheat("static", [Location(LocationKind.MODULE_VALUE, 0x10, b"\x01", b"\x00")])],
    )
    _, report = up.update_file(cf3, {}, old, "2")
    assert report.failed_at == {"static": 0x10}
    _, ok = up.update_file(cf, sigs, image(HOOK + 4, CAVE, 1, 2), "2")
    assert ok.failed_at == {}
