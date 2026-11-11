"""Bring notes to a new game version: the pure part (spec-game-update AC-U1, U4, U5, U7)."""

from __future__ import annotations

import dataclasses
import json
import struct

import pytest

from cheatmaker.core import anchor_hook as ah
from cheatmaker.core import carry as cr
from cheatmaker.core import project as pj
from cheatmaker.core import updater as up
from cheatmaker.core.locker import insn_text
from cheatmaker.core.values import ValueType
from tests import fakegame as fg

READER_OFF = fg.READER - fg.BASE


# -- helpers ------------------------------------------------------------------------------
def image_of(layout: fg.Layout = fg.V1) -> up.CodeImage:
    g = fg.build(layout=layout)
    return up.read_code_image(g.session)


def v1_sig() -> str:
    img = image_of()
    return up.learn_at(img, [(READER_OFF, len(fg.VMOVSS_R))])[READER_OFF].text()


def hook(**kw) -> pj.HookAnchor:
    base = dict(
        hook=READER_OFF,
        insn="vmovss xmm0,[r12+2ACh]",
        orig=fg.VMOVSS_R,
        reg="r12",
        disp=fg.HP,
        role="reader",
        filter=pj.HookFilter("vtable"),
        cave=0x3D00,
        slot=0x4FF0,
        signature=v1_sig(),
        sig_unique=True,
    )
    base.update(kw)
    return pj.HookAnchor(**base)


def health(anchors=None, vtable=fg.PLAYER_VT - fg.BASE) -> pj.ObjectSpec:
    return pj.ObjectSpec(
        "obj1",
        "Health",
        vtable,
        [
            pj.Field("Health", fg.HP, ValueType.FLOAT, (0, pj.CLASS_FLOAT_LIMIT), 100.0),
            pj.Field("Health 32", fg.HP32, ValueType.INT32, (0, 2**31 - 1), 100),
        ],
        anchors if anchors is not None else [hook(), pj.VtableAnchor()],
    )


def obolites() -> pj.ObjectSpec:
    return pj.ObjectSpec(
        "obj2",
        "Obolites",
        None,
        [pj.Field("Obolites", 0, ValueType.INT32, (0, 43210), fg.STATIC_VALUE)],
        [pj.StaticAnchor(fg.DATA + 0x40 - fg.BASE)],
    )


def notes(version: str = fg.VERSION, title: str = fg.TITLE, objects=None) -> pj.Project:
    objects = [health(), obolites()] if objects is None else objects
    cheats = [
        pj.ProjectCheat("Infinite health", "obj1", "Health", "freeze", 999.0),
        pj.ProjectCheat("Rich", "obj2", "Obolites", "set", 99999),
    ]
    return pj.Project(title, version, objects=objects, cheats=cheats)


def save(p: pj.Project) -> None:
    pj.save(pj.project_path(p.title_id, p.version), p)


# -- versions and sources (AC-U1) -----------------------------------------------------------
def test_version_key_compares_numbers_as_numbers_and_text_as_text():
    k = cr.version_key
    assert k("03.000.005") < k("03.001.000") < k("03.010.000")
    assert k("1.05") == k("01.05") and k("1.5") == k("01.005")
    assert k("1.9") < k("1.10")
    assert k("1.05a") < k("1.05b")
    assert k("1.05") < k("1.05a"), "a text part comes after the plain number"
    assert sorted(["2.0", "1.10", "1.9", "v1.2"], key=k)[-1] == "v1.2", "text after numbers"


@pytest.mark.parametrize("v", ["", "  ", "unknown", "UNKNOWN", "..."])
def test_unknown_or_empty_versions_are_refused(v):
    assert not cr.usable_version(v)
    assert cr.find_source(fg.TITLE, v) is None
    assert cr.list_sources(fg.TITLE, v) == []


def test_find_source_takes_the_closest_older_version():
    for v in ("03.000.004", "03.000.005", "02.000.000", "04.000.000"):
        save(notes(v))
    src = cr.find_source(fg.TITLE, "03.001.000")
    assert src is not None and src.project.version == "03.000.005" and not src.newer
    assert [p.version for p in cr.list_sources(fg.TITLE, "03.001.000")] == [
        "03.000.005",
        "03.000.004",
        "02.000.000",
        "04.000.000",
    ]


def test_same_version_other_title_and_empty_notes_are_never_a_source(tmp_path):
    save(notes("03.000.005", title="PPSA99999"))
    save(notes("03.000.004", objects=[]))  # no objects
    save(notes("3.1.0"))  # the running version, written another way
    folder = pj.project_path(fg.TITLE, "x").parent
    (folder / f"{fg.TITLE}_03.000.001.cmproj").write_text("{")  # unreadable: skipped
    assert cr.find_source(fg.TITLE, "03.001.000") is None
    assert cr.find_source("PPSA99999", "03.001.000").project.title_id == "PPSA99999"


def test_only_newer_notes_are_offered_as_newer():
    save(notes("03.002.000"))
    save(notes("03.003.000"))
    src = cr.find_source(fg.TITLE, "03.001.000")
    assert src is not None and src.newer and src.project.version == "03.002.000"


def test_no_notes_folder_means_no_source():
    assert cr.find_source(fg.TITLE, "03.001.000") is None


# -- the hooked instruction in the new code (AC-U4) ----------------------------------------
def test_site_exact_on_the_same_version():
    site = cr.find_hook_site(hook(), image_of())
    assert site == cr.HookSite(READER_OFF, fg.VMOVSS_R, "vmovss xmm0,[r12+2ACh]", fg.HP, "exact")


def test_site_loose_on_v2_reads_the_new_field():
    site = cr.find_hook_site(hook(), image_of(fg.V2))
    assert site.how == "loose" and site.offset == READER_OFF + 0x80
    assert site.disp == 0x2B4 and site.code == fg.V2.vmovss_r
    assert site.text == "vmovss xmm0,[r12+2B4h]"


def test_site_from_original_bytes_only_without_a_signature():
    moved = dataclasses.replace(fg.V2, field_shift=0)
    site = cr.find_hook_site(hook(signature=""), image_of(moved))
    assert site.how == "original" and site.offset == READER_OFF + 0x80 and site.disp == fg.HP
    unchecked = cr.find_hook_site(hook(signature="AA BB CC DD", sig_unique=False), image_of(moved))
    assert unchecked.how == "original", "an unchecked fingerprint that finds nothing"
    with pytest.raises(cr.CarryFailed):  # a checked one never falls back
        cr.find_hook_site(hook(signature="AA BB CC DD", sig_unique=True), image_of(moved))
    with pytest.raises(cr.CarryFailed):  # the field moved: the original bytes aren't there
        cr.find_hook_site(hook(signature=""), image_of(fg.V2))
    with pytest.raises(cr.CarryFailed):  # under 6 bytes: never alone
        cr.find_hook_site(hook(signature="", orig=fg.VMOVSS_R[:5]), image_of(moved))


def test_site_fails_when_the_code_changed():
    changed = dataclasses.replace(fg.V2, changed=True)
    with pytest.raises(cr.CarryFailed, match="the game's code changed where Health was found"):
        cr.find_hook_site(hook(), image_of(changed), "Health")


def test_site_fails_on_more_than_one_hit():
    img = image_of(fg.V2)
    twice = up.CodeImage([*img.blocks, (0x100000, img.blocks[0][1])])
    with pytest.raises(cr.CarryFailed) as err:
        cr.find_hook_site(hook(), twice)
    assert err.value.reason == "the game's code changed where it was found"
    with pytest.raises(cr.CarryFailed):
        cr.find_hook_site(hook(signature="", sig_unique=False), twice)


@pytest.mark.parametrize(
    "kw",
    [
        {"role": "writer"},  # the match reads, the hook wrote
        {"reg": "rbx"},  # another object register
        {"reg": "nonsense"},
        {"orig": fg.VMOVSS_W},  # another operation (store, not load)
    ],
)
def test_site_fails_on_role_register_or_operation_mismatch(kw):
    with pytest.raises(cr.CarryFailed):
        cr.find_hook_site(hook(**kw), image_of(fg.V2))


def test_insn_text_is_the_advanced_text():
    import iced_x86 as ix

    insn = ix.Decoder(64, fg.VMOVSS_W, ip=0).decode()
    assert insn_text(insn) == "vmovss [r12+2ACh],xmm1"


# -- the new notes' objects -----------------------------------------------------------------
def test_carried_object_is_a_marked_copy_with_new_paths():
    old = health([hook(), pj.ChainAnchor(0x20, (0x10, fg.HP), "Health", runs_seen=4)])
    new = cr.carried_object(old)
    assert new.check and not old.check
    assert new.anchors_of(pj.ChainAnchor)[0].runs_seen == 1
    assert old.anchors_of(pj.ChainAnchor)[0].runs_seen == 4, "the old notes are untouched"
    new.fields[0].off = 1
    assert old.fields[0].off == fg.HP


def test_shift_fields_and_reset_class():
    obj = health()
    cr.shift_fields(obj, 8)
    assert [f.off for f in obj.fields] == [fg.HP + 8, fg.HP32 + 8]
    cr.reset_class(obj)
    assert obj.vtable is None and obj.relearn_class
    assert not obj.anchors_of(pj.VtableAnchor) and obj.hook is not None
    assert obj.fields[0].range == (0, 10_000), "normal range around the last value"
    plain = obolites()
    cr.reset_class(plain)
    assert not plain.relearn_class, "nothing to learn again without a class"


def test_order_items_hooks_then_static_then_paths():
    chain = pj.ObjectSpec("c", "C", None, [], [pj.ChainAnchor(0, (0,), "x")])
    bare = pj.ObjectSpec("b", "B", None, [], [])
    objs = [bare, chain, obolites(), health()]
    assert [o.id for o in cr.order_items(objs)] == ["obj1", "obj2", "c", "b"]
    assert [cr.item_kind(o) for o in cr.order_items(objs)] == ["hook", "static", "chain", "none"]


# -- assemble (AC-U7) -------------------------------------------------------------------------
def _plan(old: pj.Project, version: str = "03.001.000") -> cr.Plan:
    new = pj.Project(old.title_id, version, old.process)
    items = [cr.Item(cr.carried_object(o), o, cr.item_kind(o)) for o in cr.order_items(old.objects)]
    return cr.Plan(old, new, None, items)


def combined(fixed: pj.ProjectPart) -> pj.ProjectCheat:
    return pj.ProjectCheat("God", parts=[pj.ProjectPart("health", "obj1", "Health", 999.0), fixed])


def test_assemble_copies_cheats_marks_check_and_keeps_no_history():
    old = notes()
    old.history.append(pj.HistoryEntry("t", "hook_slot", 1.0, True))
    plan = _plan(old)
    new, report = cr.assemble(plan, {"obj1": 0x100, "obj2": 0x200}, {})
    assert new.version == "03.001.000" and new.carried_from == fg.VERSION and new.history == []
    assert all(o.check for o in new.objects)
    assert [c.name for c in new.cheats] == ["Infinite health", "Rich"]
    assert new.cheats == old.cheats and new.cheats[0] is not old.cheats[0]
    assert report.carried == report.check == ["Infinite health", "Rich"] and not report.failed
    assert report.summary("03.001.000") == (
        "Brought over 2 of 2 cheats for version 03.001.000. Check them: Infinite health, Rich."
    )
    assert pj.load(pj.project_path(fg.TITLE, fg.VERSION)) is None, "nothing saved"


def test_assemble_keeps_an_object_not_found_and_names_why():
    old = notes()
    plan = _plan(old)
    reason = cr.CODE_CHANGED.format(label="it")
    new, report = cr.assemble(plan, {"obj1": None, "obj2": 0x200}, {"obj1": reason})
    assert [o.id for o in new.objects] == ["obj1", "obj2"], "saved, found or not"
    assert [c.name for c in new.cheats] == ["Infinite health", "Rich"]
    assert report.failed == {"Infinite health": reason} and report.carried == ["Rich"]
    assert report.summary("2").endswith(
        "Not brought over: Infinite health (the game's code changed where it was found)."
    )
    _, r2 = cr.assemble(_plan(old), {"obj2": 0x200}, {})
    assert r2.failed == {"Infinite health": cr.NOT_FOUND}


def test_combined_cheat_with_a_data_value_part_is_not_copied():
    old = notes()
    fixed = pj.ProjectPart("god flag", kind="module_value", offset=0x1A2B0, on=b"\x01", off=b"\0")
    old.cheats.append(combined(fixed))
    plan = _plan(old)
    plan.parts[2] = cr.DATA_VALUE
    new, report = cr.assemble(plan, {"obj1": 1, "obj2": 2}, {})
    assert [c.name for c in new.cheats] == ["Infinite health", "Rich"]
    assert report.failed == {"God": cr.DATA_VALUE}
    assert "God (it changes a value in the game's data" in report.summary("2")


def test_combined_cheat_takes_its_moved_parts_and_is_marked_test():
    old = notes()
    fixed = pj.ProjectPart("no death", offset=0x300, on=b"\x90\x90", off=b"\x29\x88")
    old.cheats.append(combined(fixed))
    plan = _plan(old)
    plan.parts[2] = [dataclasses.replace(fixed, offset=0x380)]
    plan.guessed_parts.add(2)
    new, report = cr.assemble(plan, {"obj1": 1, "obj2": 2}, {})
    god = new.cheats[2]
    assert god.test and god.parts[1].offset == 0x380 and god.parts[0].learned
    assert old.cheats[2].parts[1].offset == 0x300 and not old.cheats[2].test
    assert "God" in report.carried and report.guessed == ["God"]
    assert "original bytes only (test them): God." in report.summary("2")


def test_summary_without_cheats_counts_values():
    old = notes()
    old.cheats = []
    _, report = cr.assemble(_plan(old), {"obj1": 1, "obj2": None}, {})
    assert report.summary("9") == "Brought over 1 of 2 values for version 9."


# -- Advanced: add to notes that already hold objects -----------------------------------------
def test_for_merge_skips_names_and_remaps_ids():
    into = pj.Project(fg.TITLE, "03.001.000", objects=[pj.ObjectSpec("obj1", "Health", None)])
    into.cheats.append(pj.ProjectCheat("Infinite health", "obj1", "Health"))
    src = notes()
    src.objects.append(pj.ObjectSpec("obj3", "Ammo", None, [pj.Field("Ammo", 0, ValueType.INT32)]))
    src.cheats.append(pj.ProjectCheat("Ammo", "obj3", "Ammo"))
    src.cheats.append(pj.ProjectCheat("Both", parts=[pj.ProjectPart("a", "obj3", "Ammo", 1)]))
    src.cheats.append(pj.ProjectCheat("Max", "obj1", "Health"))  # its object is skipped
    out = cr.for_merge(src, into)
    assert [o.label for o in out.objects] == ["Obolites", "Ammo"]
    assert [o.id for o in out.objects] == ["obj2", "obj3"]
    assert [c.name for c in out.cheats] == ["Rich", "Ammo", "Both"]
    assert out.cheats[0].object == "obj2" and out.cheats[1].object == "obj3"
    assert out.cheats[2].parts[0].object == "obj3"
    assert src.objects[1].id == "obj2" and src.cheats[0].name == "Infinite health", "untouched"
    into.objects.append(pj.ObjectSpec("obj2", "Other", None))
    again = cr.for_merge(src, into)
    assert [o.id for o in again.objects] == ["obj3", "obj4"]
    assert again.cheats[0].object == "obj3"


# -- fingerprints for the notes (AC-U2) -------------------------------------------------------
def _installed_v1():
    g = fg.build()
    cand = ah.HookCandidate(
        fg.READER, fg.VMOVSS_R, "vmovss xmm0,[r12+2ACh]", 2, "reader", "r12", fg.HP, True
    )
    anchor = ah.plan_hook(g.session, cand, fg.PLAYER_VT - fg.BASE)
    obj = health([anchor, pj.VtableAnchor()])
    assert ah.install(g.session, obj) == "installed"
    return g, obj


def test_learn_note_signatures_replaces_an_unchecked_one_with_the_hook_in():
    g, obj = _installed_v1()
    obj.hook.signature, obj.hook.sig_unique = "C4 C1", False
    p = notes(objects=[obj])
    img = up.read_code_image(g.session)
    assert img.read(READER_OFF, 1) == b"\xe9", "the hook is in"
    changed, parts = cr.learn_note_signatures(p, img, have={})
    assert changed == 1 and parts == {}
    h = p.objects[0].hook
    assert h.sig_unique and h.signature.startswith("C4 C1 7A 10 84 24 AC 02 00 00")
    assert img.overlay([(h.hook, h.orig)]).find(up.Signature.from_text(h.signature), 2) == [
        READER_OFF
    ]
    again, _ = cr.learn_note_signatures(p, img, have={})
    assert again == 0, "a checked, matching fingerprint is kept"
    assert cr.hooks_to_learn(p) == []


def test_learn_note_signatures_relearns_a_checked_one_that_no_longer_matches_once():
    p = notes(objects=[health([hook(signature="AA BB", sig_unique=True)])])
    img = image_of()
    assert cr.hooks_to_learn(p) == [] and cr.hooks_to_learn(p, img) == [p.objects[0].hook]
    changed, _ = cr.learn_note_signatures(p, img, have={})
    assert changed == 1 and p.objects[0].hook.signature == v1_sig()


def test_learn_note_signatures_leaves_a_hook_that_cant_be_unique():
    img = image_of()
    twice = up.CodeImage([*img.blocks, (0x100000, img.blocks[0][1])])
    p = notes(objects=[health([hook(signature="", sig_unique=False)])])
    changed, _ = cr.learn_note_signatures(p, twice, have={})
    assert changed == 0 and not p.objects[0].hook.sig_unique


def test_learn_note_signatures_returns_missing_code_parts_with_the_patch_on():
    g = fg.build()
    nop = pj.ProjectPart("no death", offset=fg.SUB_SITE - fg.BASE, on=b"\x90" * 6, off=fg.SUB_W)
    cave = pj.ProjectPart("cave", offset=0x3E00, on=b"\xc3", off=b"\0")
    p = notes()
    p.cheats.append(combined(nop))
    p.cheats.append(pj.ProjectCheat("C", parts=[pj.ProjectPart("a", "obj1", "Health", 1), cave]))
    g.fake.poke(fg.PID, fg.SUB_SITE, b"\x90" * 6)  # the cheat is on
    img = up.read_code_image(g.session)
    _, parts = cr.learn_note_signatures(p, img, have={})
    assert set(parts) == {fg.SUB_SITE - fg.BASE}, "caves need no fingerprint"
    assert parts[fg.SUB_SITE - fg.BASE].text().startswith("29 88 B0 02 00 00")
    _, none = cr.learn_note_signatures(p, img, have=parts)
    assert none == {}
    path = up.signatures_path(fg.TITLE, fg.VERSION)
    up.save_signatures(path, fg.TITLE, fg.VERSION, parts)
    assert cr.learn_note_signatures(p, img)[1] == {}, "read from the signatures file"
    assert cr.part_rooms(p) == [(0x3E00, 0x3E01)]


def test_project_file_untouched_by_reading_sources():
    save(notes())
    path = pj.project_path(fg.TITLE, fg.VERSION)
    before = path.read_bytes()
    cr.find_source(fg.TITLE, "03.001.000")
    cr.list_sources(fg.TITLE, "03.001.000")
    assert path.read_bytes() == before
    assert json.loads(before)["version"] == fg.VERSION


# == live steps on the fake game, V1 notes -> V2 running (AC-U3..U7, AC-U11) ==============
class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def learned_v1(run: int = 1, filt: str = "vtable") -> pj.Project:
    """Real old notes: Find it next time on V1, then fingerprints learned (AC-U2)."""
    from cheatmaker.core import refind as rf

    g = fg.build(run)
    obj = rf.learn(
        g.session,
        g.health_addr,
        ValueType.FLOAT,
        "Health",
        "obj1",
        watch_seconds=1,
        prove_seconds=1,
    ).obj
    obj.fields.append(pj.Field("Health 32", fg.HP32, ValueType.INT32, (0, 2**31 - 1), 100))
    if filt == "field":  # a Player-only guess, no class
        obj.vtable = None
        obj.anchors = [a for a in obj.anchors if not isinstance(a, pj.VtableAnchor)]
        obj.hook.filter = pj.HookFilter("field", 8, 0x40048)
    p = notes(objects=[obj])
    p.cheats = p.cheats[:1]
    cr.learn_note_signatures(p, up.read_code_image(g.session), have={})
    assert obj.hook.sig_unique
    save(p)
    return p


def run_carry(old, game, *, pick=None, reserved=(), rounds=3, cancel=None):
    """The steps the UI driver runs, in order (pick: what the player picks)."""
    s = game.session
    plan = cr.prepare(s, old, reserved=reserved)
    found: dict[str, int | None] = {}
    installed: list[pj.ObjectSpec] = []
    for it in plan.items:
        if it.site is not None:
            try:
                classes = cr.collect_classes(
                    s, it, seconds=1, sleep=game.fight(rounds), reserved=plan.reserved
                )
                choice = cr.choose(s, classes, it)
                if isinstance(choice, cr.NeedPick):
                    choice = cr.answer(s, classes, choice, pick(choice) if pick else None)
                cr.place_hook(
                    s,
                    it,
                    choice,
                    reserved=plan.reserved,
                    prove_seconds=1,
                    cancel=cancel,
                    on_install=lambda o, _st: installed.append(o),
                )
            except cr.CarryFailed as exc:
                cr.drop_hook(it, exc.reason)
        c = Clock()
        out = cr.locate(s, it.obj, sleep=c.sleep, clock=c)
        addr = out.address if out.found else None
        if addr is None and it.obj in installed:
            ah.remove(s, it.obj, lambda _s: None)
        cr.settle(s, it.obj, addr)
        found[it.obj.id] = addr
    new, report = cr.assemble(plan, found, {})
    return plan, new, report, installed


def test_v1_to_v2_one_class_is_taken_and_the_field_moves():
    old = learned_v1(3)  # run 3: no enemies, one class
    before = pj.project_path(fg.TITLE, fg.VERSION).read_bytes()
    g = fg.build(3, layout=fg.V2)
    plan, new, report, installed = run_carry(old, g)
    [obj] = new.objects
    assert obj.field("Health").off == 0x2B4 and obj.field("Health 32").off == fg.HP32 + 8
    assert obj.vtable == fg.V2.player_vt - fg.BASE and obj.check and not obj.relearn_class
    h = obj.hook
    assert h.hook == fg.V2.reader - fg.BASE and h.disp == 0x2B4 and h.orig == fg.V2.vmovss_r
    assert h.filter.kind == "vtable" and obj.anchors_of(pj.VtableAnchor)
    assert installed == [obj] and g.fake.peek(fg.PID, fg.V2.reader, 1) == b"\xe9"
    assert report.carried == ["Infinite health"] and not report.failed
    assert new.carried_from == fg.VERSION and new.version == "03.001.000"
    assert plan.items[0].site.how == "loose" and plan.items[0].shift == 8
    assert pj.project_path(fg.TITLE, fg.VERSION).read_bytes() == before, "old notes unchanged"
    assert old.objects[0].field("Health").off == fg.HP, "the old notes object is untouched"


def test_two_classes_need_a_pick():
    old = learned_v1(1)
    g = fg.build(1, layout=fg.V2)
    asked = []

    def pick(need):
        asked.append(need)
        return g.player

    _plan, new, report, _ = run_carry(old, g, pick=pick)
    [need] = asked
    assert need.kind == "class" and len(need.options) == 2
    assert {o.address for o in need.options} >= {g.player}
    assert new.objects[0].vtable == fg.V2.player_vt - fg.BASE
    assert report.carried == ["Infinite health"]


def test_no_pick_fails_the_hook_and_keeps_the_object():
    old = learned_v1(1)
    g = fg.build(1, layout=fg.V2)
    _plan, new, report, installed = run_carry(old, g, pick=lambda _n: None)
    assert installed == [] and g.fake.peek(fg.PID, fg.V2.reader, 10) == fg.V2.vmovss_r
    [obj] = new.objects
    assert obj.anchors == [] and obj.vtable is None and obj.relearn_class, "saved, no way"
    assert report.failed == {"Infinite health": cr.NOT_PICKED}


def test_the_old_class_is_taken_on_a_dry_run():
    old = learned_v1(1)
    dry = dataclasses.replace(fg.V1, version="03.000.004")
    g = fg.build(1, layout=dry)
    plan, new, report, _ = run_carry(old, g, pick=lambda _n: pytest.fail("no pick on a dry run"))
    obj = new.objects[0]
    assert plan.items[0].site.how == "exact" and plan.items[0].shift == 0
    assert obj.vtable == fg.PLAYER_VT - fg.BASE and obj.field("Health").off == fg.HP
    assert obj.hook.hook == old.objects[0].hook.hook
    assert (obj.hook.cave, obj.hook.slot) != (old.objects[0].hook.cave, old.objects[0].hook.slot)
    assert report.carried == ["Infinite health"]


def test_nothing_touched_fails_plainly():
    old = learned_v1(3)
    g = fg.build(3, layout=fg.V2)
    _plan, new, report, installed = run_carry(old, g, rounds=0)
    assert installed == [] and report.failed == {
        "Infinite health": "nothing in the game touched it while we watched"
    }
    assert new.objects[0].anchors == []
    assert g.fake.peek(fg.PID, fg.V2.reader, 10) == fg.V2.vmovss_r, "collect unhooked"


def test_player_only_guess_is_made_again():
    old = learned_v1(1, filt="field")
    g = fg.build(1, layout=fg.V2)
    _plan, new, report, _ = run_carry(old, g, pick=lambda _n: pytest.fail("one player"))
    h = new.objects[0].hook
    assert h.filter.kind == "field" and new.objects[0].vtable is None
    (mine,) = struct.unpack("<I", g.fake.peek(fg.PID, g.player + h.filter.off, 4))
    assert mine == h.filter.value
    for e in g.enemies:
        (theirs,) = struct.unpack("<I", g.fake.peek(fg.PID, e + h.filter.off, 4))
        assert theirs != h.filter.value
    assert report.carried == ["Infinite health"]


def test_player_only_guess_with_no_match_fails():
    old = learned_v1(1, filt="field")
    old.objects[0].hook.filter = pj.HookFilter("field", 8, 0x12345)
    g = fg.build(1, layout=fg.V2)
    _plan, _new, report, installed = run_carry(old, g)
    assert installed == [] and "nothing in the game touched" in report.failed["Infinite health"]


def test_slot_written_by_the_game_fails_the_proof_and_nothing_goes_in():
    from cheatmaker.console.client import WatchHit

    old = learned_v1(3)
    g = fg.build(3, layout=fg.V2)
    plan = cr.prepare(g.session, old)
    it = plan.items[0]
    classes = cr.collect_classes(g.session, it, seconds=1, sleep=g.fight(), reserved=plan.reserved)
    choice = cr.choose(g.session, classes, it)
    from cheatmaker.core import cave as cv

    g.session.reserved_rooms = list(plan.reserved)
    g.fake.write_hits[cv.find_slot(g.session)] = [WatchHit(fg.BASE + 0x500)]
    with pytest.raises(cr.CarryFailed, match="uses the spare memory"):
        cr.place_hook(g.session, it, choice, reserved=plan.reserved, prove_seconds=1)
    assert g.fake.peek(fg.PID, fg.V2.reader, 10) == fg.V2.vmovss_r


def test_cancel_in_collect_prove_and_locate_leaves_no_jump():
    import threading

    from cheatmaker.errors import ScanCancelled

    old = learned_v1(3)
    g = fg.build(3, layout=fg.V2)
    plan = cr.prepare(g.session, old)
    it = plan.items[0]
    stop = threading.Event()
    stop.set()
    with pytest.raises(ScanCancelled):
        cr.collect_classes(g.session, it, seconds=1, cancel=stop, sleep=g.fight())
    assert g.fake.peek(fg.PID, fg.V2.reader, 10) == fg.V2.vmovss_r
    classes = cr.collect_classes(g.session, it, seconds=1, sleep=g.fight(), reserved=plan.reserved)
    choice = cr.choose(g.session, classes, it)
    with pytest.raises(ScanCancelled):
        cr.place_hook(g.session, it, choice, reserved=plan.reserved, prove_seconds=1, cancel=stop)
    assert g.fake.peek(fg.PID, fg.V2.reader, 10) == fg.V2.vmovss_r, "not put in"
    put = []
    cr.place_hook(
        g.session,
        it,
        choice,
        reserved=plan.reserved,
        prove_seconds=1,
        on_install=lambda o, s: put.append(s),
    )
    assert put == ["installed"]
    c = Clock()
    g.capture_ticks = False  # the slot stays empty: locate waits until Cancel
    out = cr.locate(g.session, it.obj, cancel=stop, sleep=c.sleep, clock=c)
    assert not out.found and c.t < 1, "stopped at once"
    ah.remove(g.session, it.obj, lambda _s: None)  # what the driver's rollback does
    assert g.fake.peek(fg.PID, fg.V2.reader, 10) == fg.V2.vmovss_r


def test_nothing_is_written_before_place_hook():
    old = learned_v1(1)
    old.cheats.append(
        combined(pj.ProjectPart("nop", offset=fg.SUB_SITE - fg.BASE, on=b"\x90" * 6, off=fg.SUB_W))
    )
    g = fg.build(1, layout=fg.V2)
    g.fake.writes.clear()
    cr.prepare(g.session, old)
    assert g.fake.writes == []


# -- static values and memory paths (AC-U5) ------------------------------------------------
def static_notes() -> pj.Project:
    p = notes(objects=[obolites()])
    p.cheats = p.cheats[1:]
    return p


def test_static_at_the_same_offset_is_kept_without_any_hook_or_code_read():
    g = fg.build(layout=fg.V2, static=True)
    g.fake.watched.clear()
    plan, new, report, installed = run_carry(static_notes(), g)
    assert plan.image is None, "no hooks, no code parts: the code isn't read"
    assert installed == [] and g.fake.watched == [] and g.fake.writes == []
    [obj] = new.objects
    assert obj.anchors == [pj.StaticAnchor(fg.DATA + 0x40 - fg.BASE)] and obj.check
    assert report.carried == ["Rich"]


def test_static_that_moved_fails_and_is_saved_with_no_way():
    moved = dataclasses.replace(fg.V2, static_off=fg.DATA + 0x60)
    g = fg.build(layout=moved, static=True)
    g.fake.poke(fg.PID, fg.DATA + 0x40, struct.pack("<i", -5))  # something else is there now
    _plan, new, report, _ = run_carry(static_notes(), g)
    assert new.objects[0].anchors == [] and report.failed == {"Rich": cr.NOT_FOUND}


def chain_notes(extra=()) -> pj.Project:
    chain = pj.ChainAnchor(fg.STATIC_PTR - fg.BASE, (0x10, fg.HP), "Health", runs_seen=3)
    obj = health([chain, *extra], vtable=fg.PLAYER_VT - fg.BASE)
    obj.fields = obj.fields[:1]
    obj.anchors.append(pj.VtableAnchor())
    return notes(objects=[obj])


def test_chain_that_still_leads_there_is_kept_as_new():
    dry = dataclasses.replace(fg.V1, version="03.000.006")
    g = fg.build(layout=dry, chain=True)
    p = chain_notes()
    p.cheats = p.cheats[:1]
    _plan, new, report, installed = run_carry(p, g)
    [obj] = new.objects
    [chain] = obj.anchors_of(pj.ChainAnchor)
    assert chain.runs_seen == 1 and installed == []
    assert obj.vtable is None and obj.relearn_class and not obj.anchors_of(pj.VtableAnchor)
    assert obj.field("Health").range == (0, 10_000)
    assert report.carried == ["Infinite health"]
    assert p.objects[0].anchors_of(pj.ChainAnchor)[0].runs_seen == 3


def test_chain_that_leads_nowhere_is_dropped():
    moved = dataclasses.replace(fg.V2, chain_root=fg.DATA + 0x28)
    g = fg.build(layout=moved, chain=True)
    p = chain_notes()
    p.cheats = p.cheats[:1]
    _plan, new, report, _ = run_carry(p, g)
    assert new.objects[0].anchors == [] and report.failed == {"Infinite health": cr.NOT_FOUND}


def test_settle_keeps_only_ways_that_lead_to_the_address():
    g = fg.build(chain=True)
    good = pj.ChainAnchor(fg.STATIC_PTR - fg.BASE, (0x10, fg.HP), "Health", runs_seen=5)
    bad = pj.ChainAnchor(fg.STATIC_PTR - fg.BASE, (0x18, fg.HP), "Health")
    obj = health([pj.StaticAnchor(0x10), good, bad, pj.VtableAnchor()])
    cr.settle(g.session, obj, g.player)
    assert obj.anchors == [good] and good.runs_seen == 1
    assert obj.vtable is None and obj.relearn_class, "no hook: its class is learned at the Yes"
    hooked = health()
    cr.settle(g.session, hooked, g.player)
    assert hooked.anchors == health().anchors, "a placed hook and its class stay"
    cr.settle(g.session, hooked, None)
    assert hooked.anchors == [] and hooked.vtable is None


# -- fixed code parts (AC-U6) and rooms ------------------------------------------------------
CAVE_OFF = fg.CODE_SIZE - 0x900  # an old cave in the spare zeros


def cave_cheat() -> pj.ProjectCheat:
    """A combined cheat: a learned part, a jump over the game's sub and a cave."""
    site = fg.SUB_SITE - fg.BASE
    jmp = b"\xe9" + struct.pack("<i", CAVE_OFF - (site + 5)) + b"\x90"
    back = b"\xe9" + struct.pack("<i", site + 6 - (CAVE_OFF + 6 + 5))
    cave = fg.SUB_W + back
    return pj.ProjectCheat(
        "God",
        parts=[
            pj.ProjectPart("health", "obj1", "Health", 999.0),
            pj.ProjectPart("jump", offset=site, on=jmp, off=fg.SUB_W),
            pj.ProjectPart("cave", offset=CAVE_OFF, on=cave, off=bytes(len(cave))),
        ],
    )


def parts_notes() -> pj.Project:
    p = learned_v1(3)
    p.cheats.append(cave_cheat())
    g = fg.build(3)
    _n, sigs = cr.learn_note_signatures(p, up.read_code_image(g.session), have={})
    up.save_signatures(up.signatures_path(fg.TITLE, fg.VERSION), fg.TITLE, fg.VERSION, sigs)
    save(p)
    return p


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def test_code_part_moved_by_its_fingerprint_with_rooms_kept_apart():
    old = parts_notes()
    sig_file = up.signatures_path(fg.TITLE, fg.VERSION)
    sig_before = sig_file.read_bytes()
    shifted = dataclasses.replace(fg.V2, field_shift=0)  # code moved, the sub didn't change
    g = fg.build(3, layout=shifted)
    open_line = (fg.CODE_SIZE - 0x1000, fg.CODE_SIZE - 0xE00)  # a cheat open in My cheats
    _plan, new, report, _ = run_carry(old, g, reserved=[open_line])
    god = new.cheats[1]
    jump, cave = god.parts[1], god.parts[2]
    assert god.test and report.carried == ["Infinite health", "God"] and not report.guessed
    assert jump.offset == fg.SUB_SITE - fg.BASE + 0x80 and jump.off == fg.SUB_W
    assert jump.offset + 5 + struct.unpack("<i", jump.on[1:5])[0] == cave.offset
    assert cave.offset + 6 + 5 + struct.unpack("<i", cave.on[7:11])[0] == jump.offset + 6
    part_room = (cave.offset, cave.offset + len(cave.on))
    h = new.objects[0].hook
    new_rooms = ah.reserved_rooms(new.objects)
    old_rooms = [*ah.reserved_rooms(old.objects), (CAVE_OFF, CAVE_OFF + len(cave.on))]
    for room in new_rooms:
        assert not _overlap(room, part_room)
        assert not _overlap(room, open_line)
        assert not any(_overlap(room, o) for o in old_rooms), "never the old offsets"
    assert not _overlap(part_room, open_line)
    assert not any(_overlap(part_room, o) for o in old_rooms)
    assert h.hook == shifted.reader - fg.BASE
    assert sig_file.read_bytes() == sig_before, "the old fingerprints are only read"


def test_code_part_fails_when_the_code_changed():
    old = parts_notes()
    g = fg.build(3, layout=fg.V2)  # the sub now reads +2B8h: its fingerprint doesn't match
    _plan, new, report, _ = run_carry(old, g)
    assert [c.name for c in new.cheats] == ["Infinite health"]
    assert report.failed["God"].startswith("part jump couldn't be found (code at +")
    assert report.carried == ["Infinite health"]


def test_code_part_in_another_module_fails():
    old = learned_v1(3)
    pc = cave_cheat()
    pc.parts[1].module = "libother.prx"
    old.cheats.append(pc)
    _plan, _new, report, _ = run_carry(old, fg.build(3, layout=fg.V2))
    assert report.failed["God"] == cr.OTHER_MODULE
