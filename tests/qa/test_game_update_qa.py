"""QA round for "bring notes and cheats over to a new game version" (2026-11-11).

Spec docs/spec-game-update.md (AC-U1..U13), report qa/reports/2026-11-11-game-update.md.
Each test names its test case (TC-xx). Open bugs are strict xfails named BUG-xx: remove
the marker once the bug is fixed (the test then passes).

Runs on the fake PS5 (tests/fakegame.py, Layout V1 -> V2) with the app's own windows.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from cheatmaker.core import carry as cr
from cheatmaker.core import project as pj
from cheatmaker.core import refind as rf
from cheatmaker.core import updater as up
from cheatmaker.core.session import GameInfo
from cheatmaker.settings import Settings
from cheatmaker.ui import hints
from cheatmaker.ui.cheats_view import COL_ON
from tests import fakegame as fg
from tests.ui.test_carry_over import App, Console, _cancel_with, new_path, old_files
from tests.unit import test_carry as tc

ROOT = Path(__file__).resolve().parents[2]
NEW = fg.V2.version


@pytest.fixture
def make(qtbot, monkeypatch):
    """One app run per call (tests/ui/test_carry_over.py's App), short watches."""
    monkeypatch.setattr(rf.ah, "WATCH_SECONDS", 1)
    monkeypatch.setattr(rf.ah, "PROVE_SECONDS", 1)
    monkeypatch.setattr(rf, "SLOT_WAIT_S", 0.5)
    apps: list[App] = []

    def _make(console: Console) -> App:
        app = App(qtbot, console)
        apps.append(app)
        return app

    yield _make
    for a in apps:
        a.ctl.shutdown()


def tick(app, row: int = 0) -> None:
    app.w.cheats.table.item(row, COL_ON).setCheckState(Qt.CheckState.Checked)


def row_of(app, name: str) -> int:
    return next(i for i, c in enumerate(app.w.cheats.cheats) if c.name == name)


def heap_writes(game: fg.Game) -> list:
    """Writes into the game's data (objects), not its code (hooks)."""
    return [w for w in game.fake.writes if fg.HEAP <= w[1] < fg.HEAP + fg.HEAP_SIZE]


# == AC-U1 versions and sources ==============================================================
def test_tc01_version_compare_rules():
    """TC-01: number by number, leading zeros dropped, text as text, closest older."""
    k = cr.version_key
    assert k("03.000.005") < k("03.001.000")
    assert k("1.05") == k("01.05")
    assert k("1.9") < k("1.10") < k("2")
    assert k("01.000") < k("01.000a") < k("01.000b")
    for v in ("03.000.004", "03.000.005", "02.999.999", "03.001.001"):
        tc.save(tc.notes(v))
    src = cr.find_source(fg.TITLE, "03.001.000")
    assert src is not None and src.project.version == "03.000.005" and not src.newer


def test_tc02_odd_version_text_never_crashes():
    """TC-02: version text from the console or a notes file is compared, never a crash.
    "²" is a digit to str.isdigit() but not to int(): version_key raises ValueError,
    so check_offer (on Connect) and find_source raise instead of offering nothing."""
    for v in ("1.0²", "１.０"):  # superscript two; full-width digits
        cr.version_key(v)
        cr.usable_version(v)
    tc.save(tc.notes("1.0²"))
    cr.find_source(fg.TITLE, "03.001.000")


def test_tc03_other_title_platform_or_region_is_never_a_source():
    """TC-03: a PS4 (CUSA) or other-region title with the same numbers is never offered."""
    for title in ("CUSA01285", "PPSA01286"):
        tc.save(tc.notes("03.000.005", title=title))
    assert cr.find_source(fg.TITLE, "03.001.000") is None
    assert cr.list_sources(fg.TITLE, "03.001.000") == []


def test_tc04_unreadable_and_newer_format_notes_are_skipped():
    """TC-04: a broken or newer-format old notes file is skipped (no crash), others
    still offered."""
    folder = pj.project_path(fg.TITLE, "x").parent
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{fg.TITLE}_03.000.009.cmproj").write_text("{", encoding="utf-8")
    d = tc.notes("03.000.008").to_json()
    d["format_version"] = pj.FORMAT_VERSION + 1
    (folder / f"{fg.TITLE}_03.000.008.cmproj").write_text(json.dumps(d))
    tc.save(tc.notes("03.000.004"))
    src = cr.find_source(fg.TITLE, "03.001.000")
    assert src is not None and src.project.version == "03.000.004"


def test_tc05_not_now_is_per_pair_the_next_update_is_offered(make):
    """TC-05: Not now hides 03.000.005>03.001.000 only; a later update is offered."""
    tc.learned_v1(1)
    app = make(Console())
    app.connect()
    app.bar.not_now_btn.click()
    app.ctl.shutdown()
    later = make(Console(layout=dataclasses.replace(fg.V2, version="03.002.000")))
    later.connect()
    assert later.bar.mode == "offer"
    assert "the game is now 03.002.000" in later.bar.label.text()


def test_tc06_broken_declined_setting_resets_and_offers(make):
    """TC-06: a settings file whose carry_declined isn't a list of text resets to []."""
    path = Settings.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = json.loads(path.read_text()) if path.exists() else {}
    doc["carry_declined"] = [1, None]
    path.write_text(json.dumps(doc))
    assert Settings.load().carry_declined == []
    tc.learned_v1(1)
    app = make(Console())
    app.connect()
    assert app.bar.mode == "offer"


# == AC-U7 old notes, format 4 ===============================================================
def test_tc07_old_notes_byte_identical_after_a_failed_bring_over(make):
    """TC-07: the hook can't come over (code changed): old notes and old fingerprints
    are byte for byte the same; the new file is format 4 with carried_from."""
    tc.learned_v1(1)
    up.save_signatures(up.signatures_path(fg.TITLE, fg.VERSION), fg.TITLE, fg.VERSION, {})
    before = old_files()
    app = make(Console(layout=dataclasses.replace(fg.V2, changed=True)))
    app.connect()
    app.bring()
    assert old_files() == before
    raw = json.loads(new_path().read_text())
    assert raw["format_version"] == 4 and raw["carried_from"] == fg.VERSION
    assert raw["objects"][0]["check"] is True


def test_tc08_format_3_notes_are_brought_over(make):
    """TC-08: old notes saved by the previous app (format 3, no format-4 keys) come over."""
    p = tc.learned_v1(3)
    d = p.to_json()
    d["format_version"] = 3
    for o in d["objects"]:
        for a in o["anchors"]:
            a.pop("signature_unique", None)
    pj.project_path(fg.TITLE, fg.VERSION).write_text(json.dumps(d))
    app = make(Console(run=3))
    app.connect()
    assert app.bar.mode == "offer"
    app.bring()
    assert app.w.status.text().startswith("Brought over 1 of 1 cheats")


# == AC-U8 the question: No writes nothing ===================================================
def test_tc09_no_writes_nothing_and_the_question_comes_back(make):
    """TC-09: No -> nothing written to the value, the plain hint, still "check it";
    the next tick asks again (each run until Yes)."""
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    app.bring()
    finder = app.w.cheats.finder
    replies = [False, False]
    finder.ask_confirm = lambda text: app.asked.append(text) or replies.pop(0)
    game = app.console.game
    game.fake.writes.clear()
    hp = game.health()
    tick(app)
    assert heap_writes(game) == [] and game.health() == hp
    assert app.w.status.text() == hints.CONFIRM_NO_CARRIED.format(name="Health")
    c = app.w.cheats.cheats[0]
    assert not c.enabled
    assert pj.load(new_path()).objects[0].check
    app.w.cheats.fill()
    tick(app)
    assert len(app.asked) == 2 and "brought over from version 03.000.005" in app.asked[1]
    assert heap_writes(game) == []


def test_tc10_yes_writes_at_the_new_place_and_clears_check(make):
    """TC-10: Yes -> written at the V2 field (+8), check cleared and saved."""
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    app.bring()
    tick(app)
    assert app.console.game.health() == 999.0
    assert not pj.load(new_path()).objects[0].check


# == AC-U4 field shift for fields that aren't hooked ==========================================
def test_tc11_other_fields_shift_like_the_hooked_one(make):
    """TC-11: Health 32 (not hooked) moves by the same +8 as Health."""
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    app.bring()
    obj = pj.load(new_path()).objects[0]
    assert obj.field("Health").off == fg.HP + 8 and obj.field("Health 32").off == fg.HP32 + 8


# == AC-U2 / design: one code read at a time =================================================
def test_tc12_bring_over_read_takes_the_one_code_read_slot(make, monkeypatch):
    """TC-12: while Bring them over reads the code, ctl.code_reading is set (so the
    fingerprint learners wait), and Advanced doesn't start a second read while another
    learner is reading."""
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    seen = []
    real = up.read_code_image

    def read(session, progress=None, cancel=None):
        seen.append(app.ctl.code_reading)
        return real(session, progress, cancel)

    monkeypatch.setattr(cr.up, "read_code_image", read)
    app.bring()
    assert seen == [True], "the bring-over's own read holds the slot"


def test_tc13_advanced_waits_while_another_code_read_runs(make, monkeypatch):
    """TC-13: a learner holds the read (ctl.code_reading): Advanced must not read too."""
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    reads = []
    real = up.read_code_image
    monkeypatch.setattr(
        cr.up, "read_code_image", lambda s, p=None, c=None: reads.append(1) or real(s, p, c)
    )
    app.ctl.code_reading = True  # NoteFingerprints / the cheat-file learner is reading
    app.w.carry.pick_version = lambda versions: versions[0]
    app.w.carry.open_advanced()
    assert reads == []


def test_tc14_learner_never_starts_while_carrying(make, monkeypatch):
    """TC-14: a notes save during a bring-over doesn't start the background learner."""
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    seen_running = []
    real_sleep = app._sleep

    def sleep(s):
        app.w.cheats.finder.notes_changed.emit()
        seen_running.append(app.w.fingerprints.running)
        real_sleep(s)

    app.w.carry.sleep = sleep
    app.bring()
    assert seen_running and not any(seen_running)


# == AC-U7 Advanced merge ====================================================================
def _merge_setup(extra_cheat: pj.ProjectCheat | None = None):
    old = tc.learned_v1(3)
    old.objects.append(tc.obolites())
    old.cheats.append(pj.ProjectCheat("Rich", "obj2", "Obolites", "set", 99999))
    if extra_cheat is not None:
        old.cheats.append(extra_cheat)
    tc.save(old)
    have = tc.health([], vtable=None)
    mine = pj.Project(
        fg.TITLE, NEW, objects=[have], cheats=[pj.ProjectCheat("Infinite health", "obj1", "Health")]
    )
    pj.save(new_path(), mine)
    return old


def test_tc15_merge_keeps_new_named_cheats_on_values_already_here(make):
    """TC-15 (AC-U7): Advanced adds cheats whose *names* aren't in the new notes. "Health
    500" (new name, on Health, which the new notes already hold) must be added, or at
    least named in the summary; today it vanishes without a word."""
    _merge_setup(pj.ProjectCheat("Health 500", "obj1", "Health", "set", 500.0))
    app = make(Console(run=3, static=True))
    app.connect()
    texts = []
    app.ctl.message.connect(texts.append)
    app.w.carry.pick_version = lambda versions: versions[0]
    app.w.carry.open_advanced()
    names = [c.name for c in pj.load(new_path()).cheats]
    assert "Health 500" in names or any("Health 500" in t for t in texts)
    shown = [c.name for c in app.w.cheats.cheats]
    assert "Health 500" in shown and "Infinite health" in shown, "a row for each cheat"


def test_tc16_merged_values_question_names_their_own_version(make):
    """TC-16: notes brought over from 03.000.004 earlier; Advanced now merges Obolites from
    03.000.005. Obolites' question must say 03.000.005."""
    _merge_setup()
    mine = pj.load(new_path())
    mine.carried_from = "03.000.004"
    pj.save(new_path(), mine)
    app = make(Console(run=3, static=True))
    app.connect()
    app.w.carry.pick_version = lambda versions: versions[0]
    app.w.carry.open_advanced()
    assert app.w.status.text().startswith("Brought over"), app.w.status.text()
    tick(app, row_of(app, "Rich"))
    asked = [t for t in app.asked if "Obolites" in t]
    assert asked and "from version 03.000.005" in asked[0], asked


def test_tc17_merge_never_overwrites_values_already_here(make):
    """TC-17: Advanced into notes with values: the value already here is unchanged."""
    _merge_setup()
    before = pj.load(new_path()).objects[0].to_json()
    app = make(Console(run=3, static=True))
    app.connect()
    app.w.carry.pick_version = lambda versions: versions[0]
    app.w.carry.open_advanced()
    saved = pj.load(new_path())
    assert saved.objects[0].to_json()["fields"] == before["fields"]
    assert [o.label for o in saved.objects] == ["Health", "Obolites"]


# == AC-U3 / AC-U13 Cancel and docs ===========================================================
def _c21_row(test_id: str) -> str:
    text = (ROOT / "docs" / "console-tests.md").read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if line.startswith(f"| {test_id} |"))


def test_tc18_c21e_pass_line_matches_cancel_during_the_slot_check(make, monkeypatch, caplog):
    """TC-18 (AC-U13): docs/console-tests.md C21e says Cancel during "Checking the spare
    memory" logs action=hook_remove. Run that exact step and compare with the doc."""
    tc.learned_v1(3)
    caplog.set_level("INFO")
    app = make(Console(run=3))
    app.connect()
    _cancel_with(monkeypatch, app, "prove")
    app.bring()
    assert app.w.status.text() == hints.CARRY_CANCELLED
    if "hook_remove" in _c21_row("C21e"):
        assert "action=hook_remove" in caplog.text


def test_tc19_cancel_in_the_slot_check_leaves_nothing(make, monkeypatch):
    """TC-19 (AC-U3): Cancel during the slot check: no jump, no new file, offer back."""
    tc.learned_v1(3)
    before = old_files()
    app = make(Console(run=3))
    app.connect()
    _cancel_with(monkeypatch, app, "prove")
    app.bring()
    assert app.console.game.fake.peek(fg.PID, fg.V2.reader, 10) == fg.V2.vmovss_r
    assert not new_path().exists() and old_files() == before and app.bar.mode == "offer"


# == wording (AC-U3, architecture: no "vtable", "cave", "pointer") ===========================
JARGON = re.compile(r"vtable|\bcave|pointer|signature|\bslot\b|\bhook", re.I)


def test_tc20_carry_texts_are_plain_words():
    """TC-20: every bring-over hint and reason the player can read is plain words."""
    texts = [
        hints.CONFIRM_CARRIED,
        hints.CARRIED_FROM,
        hints.CONFIRM_NO_CARRIED,
        hints.CARRY_TEST_HINT,
        hints.KEEPS_CHECK,
        hints.KEEPS_TEST,
        hints.CARRY_OFFER,
        hints.CARRY_OFFER_NEWER,
        *hints.CARRY_STEPS.values(),
        hints.CARRY_CANCELLED,
        hints.CARRY_NEEDS_GAME,
        hints.CARRY_NO_SOURCES,
        hints.CARRY_PICK_VERSION,
        hints.ADVANCED_BRING,
        hints.CARRY_BUSY,
        cr.CODE_CHANGED,
        cr.NOTHING_TOUCHED,
        cr.NOT_FOUND,
        cr.DATA_VALUE,
        cr.OTHER_MODULE,
        cr.PART_FAILED,
        cr.NO_OBJECT,
        cr.NOT_PICKED,
        cr.HOOK_FILE_FIRST,
        cr.PC_OFF_AGAIN,
    ]
    bad = [t for t in texts if JARGON.search(t)]
    assert bad == []


def test_tc21_summary_wording_matches_the_spec_example():
    """TC-21 (AC-U3): the v2-style summary line."""
    r = cr.CarryReport(
        carried=["Health", "Obolites", "Ammo"],
        check=["Health", "Obolites"],
        failed={"Shield": cr.CODE_CHANGED.format(label="it")},
    )
    assert r.summary("03.001.000") == (
        "Brought over 3 of 4 cheats for version 03.001.000. Check them: Health, Obolites. "
        "Not brought over: Shield (the game's code changed where it was found)."
    )


# == repeating a failed bring-over (spec "Not in this spec": the player uses Advanced) =========
def test_tc22_advanced_repeats_a_failed_bring_over(make):
    """TC-22: nobody took a hit during the 10 s collect, so Health came over with no way
    to be found. The spec says the player repeats a failed bring-over with Advanced; today
    Advanced skips Health (its name is in the new notes) and says "Brought over 0 of 0
    values", and Health stays unfindable."""
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    app.w.carry.sleep = lambda _s: None  # the player didn't fight
    app.bring()
    assert "nothing in the game touched it while we watched" in app.w.status.text()
    assert pj.load(new_path()).objects[0].anchors == []
    app.w.carry.sleep = app._sleep  # now the player fights
    app._fight = None
    app.w.carry.pick_version = lambda versions: versions[0]
    app.w.carry.open_advanced()
    assert pj.load(new_path()).objects[0].anchors, app.w.status.text()


# == AC-U9 Update for this version with the notes' hook in the file ==========================
def test_tc23_update_with_hooks_keeps_a_moved_cheat_whose_name_repeats():
    """TC-23: a file with the find-it-again hook and two cheats both named "No death": one
    moves, one can't (its code is gone). v2 alone keeps the one that moved; with the hook
    lines (AC-U9) both used to disappear from the updated file (BUG-07)."""
    from cheatmaker.core.cheats import Cheat, Location, LocationKind

    old = tc.learned_v1(1)
    cf, sigs = tc.hook_file(old)
    find, nop, keep = cf.cheats
    gone = Cheat(
        "No death",
        [Location(LocationKind.CODE_PATCH, 0x3F00, b"\x90" * 6, bytes.fromhex("112233445566"))],
    )
    cf.cheats = [find, gone, nop, keep]
    g = fg.build(1, layout=tc.SHIFTED)
    image = up.read_code_image(g.session)
    rest, _lines = cr.split_hook_cheats(cf, old)
    v2_only, _r = up.update_file(rest, sigs, image, NEW)
    assert [c.name for c in v2_only.cheats] == ["No death"], "v2 keeps the one that moved"
    _plan, new, _rep, _inst = tc.run_carry(old, g, pick=lambda _n: g.player)
    new.objects[0].check = False  # confirmed: the PC-off line is rebuilt too (review m9)
    out, _rep = cr.update_with_hooks(g.session, cf, sigs, image, NEW, old, new)
    assert [c.name for c in out.cheats] == ["Find Health again", "No death", "Health (PC off)"]


# == AC-U4 field shift: fields before the hooked one ==========================================
def test_tc24_field_shift_never_gives_a_negative_offset():
    """TC-24: the hooked field moved 0x20 *down* (0x2AC -> 0x28C). A second field at +8
    becomes -0x18: before the object. A cheat on it would write into the memory in front
    of the object after one Yes."""
    from cheatmaker.core.values import ValueType

    old = tc.learned_v1(3)
    old.objects[0].fields.append(pj.Field("Ident", 8, ValueType.INT32, None, 0x40048))
    g = fg.build(3, layout=dataclasses.replace(fg.V2, field_shift=-0x20))
    _plan, new, _report, _ = tc.run_carry(old, g)
    offs = {f.name: f.off for f in new.objects[0].fields}
    assert offs["Health"] == fg.HP, "none moves when one can't (review m-d)"
    assert all(off >= 0 for off in offs.values()), offs


# == wording of reasons from the slot check (AC-U3 summary line) ==============================
def test_tc25_slot_check_reason_reads_as_part_of_the_summary(make, monkeypatch):
    """TC-25: the game writes to the spare memory during the 60 s check. The summary line
    must stay one v2-style sentence: "Not brought over: Infinite health (<reason>)."."""
    from cheatmaker.errors import FindFailed

    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()

    def busy(*_a, **_kw):
        raise FindFailed("The game is using the spare memory Cheat Maker needs. Restart it.")

    monkeypatch.setattr(cr.ah, "prove_slot", busy)
    app.bring()
    text = app.w.status.text()
    assert text.startswith("Brought over 0 of 1 cheats")
    reason = text.split("Infinite health (", 1)[1]
    assert reason[0].islower() and ".)" not in reason, text


# == AC-U8 combined cheat with a value not found: nothing written ============================
def test_tc26_combined_cheat_with_a_part_not_found_writes_nothing(make):
    """TC-26: a brought-over combined cheat whose second value has no way to be found:
    no question, no write, the part is named."""
    from cheatmaker.core.values import ValueType

    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    app.bring()
    p = app.ctl.project
    p.objects.append(
        pj.ObjectSpec(
            "obj2",
            "Shield",
            None,
            [pj.Field("Shield", 0, ValueType.INT32, (0, 100), 50)],
            [],
            check=True,
        )
    )
    p.cheats.append(
        pj.ProjectCheat(
            "God",
            parts=[
                pj.ProjectPart("health", "obj1", "Health", 999.0),
                pj.ProjectPart("shield", "obj2", "Shield", 100),
            ],
        )
    )
    app.w.cheats.finder.save()
    app.w.cheats.finder.ensure_cheats()
    app.w.cheats.fill()
    game = app.console.game
    game.fake.writes.clear()
    app.asked.clear()
    tick(app, row_of(app, "God"))
    assert app.asked == [] and heap_writes(game) == []
    assert "shield isn't found right now" in app.w.status.text()


# == re-test after the QA fixes (2026-11-11, second round) ===================================
def test_tc28_update_keeps_the_moved_twin_while_the_pc_off_line_waits_for_yes():
    """TC-28 (BUG-07 re-test, review m9): with the value *not* confirmed yet, the moved
    "No death" still stays; only the PC-off line waits for the Yes, with a plain reason."""
    from cheatmaker.core.cheats import Cheat, Location, LocationKind

    old = tc.learned_v1(1)
    cf, sigs = tc.hook_file(old)
    find, nop, keep = cf.cheats
    gone = Cheat(
        "No death",
        [Location(LocationKind.CODE_PATCH, 0x3F00, b"\x90" * 6, bytes.fromhex("112233445566"))],
    )
    cf.cheats = [find, gone, nop, keep]
    g = fg.build(1, layout=tc.SHIFTED)
    image = up.read_code_image(g.session)
    _plan, new, _rep, _inst = tc.run_carry(old, g, pick=lambda _n: g.player)
    assert new.objects[0].check
    out, rep = cr.update_with_hooks(g.session, cf, sigs, image, NEW, old, new)
    assert [c.name for c in out.cheats] == ["Find Health again", "No death"]
    assert out.cheats[1].locations[0].offset != 0x3F00, "the one that moved, not the gone one"
    assert rep.failed["Health (PC off)"] == cr.CONFIRM_FIRST.format(label="Health")


def test_tc29_new_reasons_and_texts_are_plain_words():
    """TC-29 (BUG-08/09 re-test): the reasons added by the fixes read inside the summary's
    brackets (plain words, lower-case start, no full stop) and "Not brought over: search
    for it" has no jargon."""
    reasons = [cr.FIELDS_MOVED, cr.NO_ROOM, cr.ROOM_USED, cr.NOT_PUT_IN]
    assert [t for t in [*reasons, hints.KEEPS_LOST] if JARGON.search(t)] == []
    assert all(t[0].islower() and not t.endswith(".") for t in reasons)


@pytest.mark.parametrize(
    ("step", "reason"), [("plan", cr.NO_ROOM), ("install", cr.NOT_PUT_IN)], ids=["plan", "install"]
)
def test_tc30_plan_and_install_errors_are_short_reasons_and_logged(
    make, monkeypatch, caplog, step, reason
):
    """TC-30 (BUG-09 re-test, other steps): no room for the hook, or the hook can't go in:
    the summary names a short reason; the app's own message goes to the log only."""
    from cheatmaker.errors import FindFailed, LockFailed

    tc.learned_v1(3)
    caplog.set_level("INFO")
    app = make(Console(run=3))
    app.connect()
    exc = LockFailed("No free room. Close it.") if step == "plan" else FindFailed("Boom. Retry.")

    def boom(*_a, **_kw):
        raise exc

    monkeypatch.setattr(cr.ah, "plan_hook" if step == "plan" else "install", boom)
    app.bring()
    text = app.w.status.text()
    assert text.endswith(f"Not brought over: Infinite health ({reason})."), text
    assert f"step={step} result=failed error={exc}" in caplog.text
    assert app.console.game.fake.peek(fg.PID, fg.V2.reader, 10) == fg.V2.vmovss_r
    assert not app.ctl.code_reading and app.w.carry.run is None


def test_tc31_a_field_moved_before_the_object_fails_the_value_without_a_hook(caplog):
    """TC-31 (BUG-08 re-test): the cheat on the value fails with the new reason, the value
    has no way to be found (no hook put in), and the log names the field."""
    from cheatmaker.core.values import ValueType

    caplog.set_level("INFO")
    old = tc.learned_v1(3)
    old.objects[0].fields.append(pj.Field("Ident", 8, ValueType.INT32, None, 0x40048))
    lay = dataclasses.replace(fg.V2, field_shift=-0x20)
    g = fg.build(3, layout=lay)
    _plan, new, report, installed = tc.run_carry(old, g)
    assert report.failed == {"Infinite health": cr.FIELDS_MOVED}
    assert installed == [] and new.objects[0].anchors == [] and new.objects[0].check
    assert g.fake.peek(fg.PID, lay.reader, len(lay.vmovss_r)) == lay.vmovss_r, "no jump"
    assert "action=carry_fields object=obj1" in caplog.text and "fields=Ident" in caplog.text


def test_tc32_advanced_again_keeps_the_players_new_cheat_on_the_lost_value(make):
    """TC-32 (BUG-06 re-test): after a failed bring-over the player adds "Health 500" on
    the lost Health; Advanced brings Health again in its place: one Health, with a way to
    be found, and the player's cheat still on it."""
    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    app.w.carry.sleep = lambda _s: None  # nobody took a hit
    app.bring()
    p = app.ctl.project
    p.cheats.append(pj.ProjectCheat("Health 500", "obj1", "Health", "set", 500.0))
    app.w.cheats.finder.save()
    app.w.carry.sleep = app._sleep
    app._fight = None
    app.w.carry.pick_version = lambda versions: versions[0]
    app.w.carry.open_advanced()
    saved = pj.load(new_path())
    assert [o.label for o in saved.objects] == ["Health"], [o.id for o in saved.objects]
    assert saved.objects[0].anchors and saved.objects[0].id == "obj1"
    assert {c.name: c.object for c in saved.cheats} == {
        "Infinite health": "obj1",
        "Health 500": "obj1",
    }
    assert not app.ctl.code_reading


def test_tc33_advanced_never_replaces_a_value_still_waiting_to_be_seen(make):
    """TC-33 (BUG-06 boundary): a value whose new hook is in but not seen yet (still
    "check it", with its hook) is not a failed one: Advanced keeps it as it is."""
    tc.learned_v1(3)
    app = make(Console(run=3, capture_ticks=False))
    app.connect()
    app.bring()
    before = pj.load(new_path()).objects[0].to_json()
    assert before["anchors"] and before["check"]
    app.w.carry.pick_version = lambda versions: versions[0]
    app.w.carry.open_advanced()
    saved = pj.load(new_path())
    assert [o.to_json() for o in saved.objects] == [before]


@pytest.mark.parametrize("how", ["error", "link", "game"])
def test_tc34_the_code_read_is_given_back_when_the_read_fails(make, monkeypatch, how):
    """TC-34 (BUG-02 re-test): the read fails, the link drops or the game restarts while
    Bring them over reads the code: the one code read is free again, nothing is saved."""
    from cheatmaker.errors import CheatMakerError, ConnectionLost

    tc.learned_v1(3)
    app = make(Console(run=3))
    app.connect()
    other = GameInfo(99, fg.TITLE, "Returnal", NEW, "eboot.bin", fg.BASE, fg.BASE + 0x6000)
    real = up.read_code_image

    def read(session, progress=None, cancel=None):
        assert app.ctl.code_reading
        if how == "error":
            raise CheatMakerError("The game's code can't be read.")
        if how == "link":
            raise ConnectionLost("The PS5 stopped answering.")
        app.ctl.game_changed.emit(other)
        return real(session, progress, cancel)

    monkeypatch.setattr(cr.up, "read_code_image", read)
    app.bring()
    assert not app.ctl.code_reading, "the one code read is given back"
    assert app.w.carry.run is None and not app.w.cheats.finder.carrying
    assert not new_path().exists()


def test_tc35_cancel_while_the_player_picks_ends_the_run(make):
    """TC-35 (review fix, rollback during a pick): Cancel pressed while the pick dialog is
    open: the run stops before anything goes in, the offer is back, the read is free."""
    tc.learned_v1(1)  # player and enemies: a pick
    before = old_files()
    app = make(Console())
    app.connect()
    picks = []

    def pick(obj, options):
        picks.append(obj.label)
        app.w.carry.cancel()
        return app.console.game.player

    app.w.cheats.finder.ask_pick = pick
    app.bring()
    assert picks == ["Health"]
    assert app.w.status.text() == hints.CARRY_CANCELLED
    assert app.w.carry.run is None and app.bar.mode == "offer"
    assert app.console.game.fake.peek(fg.PID, fg.V2.reader, 10) == fg.V2.vmovss_r
    assert not new_path().exists() and old_files() == before and not app.ctl.code_reading


def test_tc36_link_drops_while_the_player_picks(make):
    """TC-36 (review fix): the link drops while the pick dialog is open: no crash, the run
    is over, nothing saved, the read is free."""
    tc.learned_v1(1)
    app = make(Console())
    app.connect()

    def pick(obj, options):
        app.console.game.fake.disconnect()
        return app.console.game.player

    app.w.cheats.finder.ask_pick = pick
    app.bring()
    assert app.w.carry.run is None and not app.w.cheats.finder.carrying
    assert not new_path().exists() and not app.ctl.code_reading


def test_tc41_the_field_log_line_names_the_real_shift(caplog):
    """TC-41 (BUG-08 fix, log): the console tester reads action=carry_fields to see how far
    the value moved; the shift must be the hooked field's (-0x20 = -32), not 0."""
    from cheatmaker.core.values import ValueType

    caplog.set_level("INFO")
    old = tc.learned_v1(3)
    old.objects[0].fields.append(pj.Field("Ident", 8, ValueType.INT32, None, 0x40048))
    g = fg.build(3, layout=dataclasses.replace(fg.V2, field_shift=-0x20))
    tc.run_carry(old, g)
    assert "action=carry_fields object=obj1 shift=-32 result=failed" in caplog.text


# -- make_old_notes (C21b-C21g dry run) ------------------------------------------------------
def _make_old_notes(monkeypatch, *args) -> int:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "make_old_notes", ROOT / "tools" / "make_old_notes.py"
    )
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(sys, "argv", ["make_old_notes", *args])
    return tool.main()


def _files(version: str) -> dict:
    out = {}
    for p in (pj.project_path(fg.TITLE, version), up.signatures_path(fg.TITLE, version)):
        out[p.name] = p.read_bytes() if p.exists() else None
    return out


def test_tc37_make_old_notes_undo_after_a_real_bring_over(make, monkeypatch, capsys):
    """TC-37 (review fix, --undo record): dry run, the app brings the notes over (new notes
    and fingerprints written), --undo: every real file back byte for byte, the dry run's
    files kept as .carried, nothing deleted; a second --undo is refused."""
    tc.learned_v1(3)
    real_old = pj.project_path(fg.TITLE, "03.000.004")
    real_old.write_text("my real 03.000.004 notes")
    cur, older = _files(fg.VERSION), _files("03.000.004")
    assert _make_old_notes(monkeypatch, fg.TITLE.lower(), fg.VERSION, "03.000.004") == 0
    app = make(Console(run=3, layout=fg.V1))  # the same game: a dry run
    app.connect()
    assert app.bar.mode == "offer"
    app.bring()
    assert app.w.status.text().startswith("Brought over 1 of 1 cheats"), app.w.status.text()
    app.ctl.shutdown()
    assert _make_old_notes(monkeypatch, fg.TITLE, fg.VERSION, "03.000.004", "--undo") == 0
    assert _files(fg.VERSION) == cur and _files("03.000.004") == older
    folder = real_old.parent
    assert (folder / (pj.project_path(fg.TITLE, fg.VERSION).name + ".carried")).exists()
    assert (folder / (real_old.name + ".carried")).exists()
    capsys.readouterr()
    assert _make_old_notes(monkeypatch, fg.TITLE, fg.VERSION, "03.000.004", "--undo") == 1
    assert "No dry run was made" in capsys.readouterr().out
    assert _files(fg.VERSION) == cur


def test_tc38_make_old_notes_never_stops_half_way(monkeypatch, capsys):
    """TC-38: the current version's fingerprints file is broken. The dry run must refuse
    before changing anything ("Nothing was changed"), or finish so --undo works. Today it
    stops with a traceback after setting the real older notes aside and writing the copy,
    with no record: --undo says "No dry run was made", a new dry run says "run with --undo
    first"."""
    pj.save(
        pj.project_path(fg.TITLE, fg.VERSION),
        pj.Project(fg.TITLE, fg.VERSION, objects=[pj.ObjectSpec("o", "Gold", None)]),
    )
    sigs = up.signatures_path(fg.TITLE, fg.VERSION)
    sigs.parent.mkdir(parents=True, exist_ok=True)
    sigs.write_text("{")
    real_old = pj.project_path(fg.TITLE, "03.000.004")
    real_old.write_text("my real 03.000.004 notes")
    before = {**_files(fg.VERSION), **_files("03.000.004")}
    try:
        code = _make_old_notes(monkeypatch, fg.TITLE, fg.VERSION, "03.000.004")
    except Exception:
        code = None
    if code != 0:  # refused: nothing may have changed
        assert {**_files(fg.VERSION), **_files("03.000.004")} == before
        assert not list(real_old.parent.glob("*.keep"))
    else:  # went on: --undo puts everything back
        assert _make_old_notes(monkeypatch, fg.TITLE, fg.VERSION, "03.000.004", "--undo") == 0
        assert {**_files(fg.VERSION), **_files("03.000.004")} == before


# == new bugs found in the re-test ===========================================================
def test_tc39_merge_onto_a_value_without_the_cheats_field(make):
    """TC-39 (BUG-03 fix, new edge): the new notes' Health has only the "Health" field; the
    old "HP32 max" cheat is on "Health 32". Advanced may add the field, or leave the cheat
    out and name it; it must not count it as brought over and save a cheat on a field the
    value doesn't have (it never shows in My cheats)."""
    _merge_setup(pj.ProjectCheat("HP32 max", "obj1", "Health 32", "set", 5000))
    mine = pj.load(new_path())
    mine.objects[0].fields = [f for f in mine.objects[0].fields if f.name == "Health"]
    pj.save(new_path(), mine)
    app = make(Console(run=3, static=True))
    app.connect()
    app.w.carry.pick_version = lambda versions: versions[0]
    app.w.carry.open_advanced()
    saved = pj.load(new_path())
    shown = [c.name for c in app.w.cheats.cheats]
    broken = [
        c.name
        for c in saved.cheats
        if c.object is not None and c.field not in {f.name for f in saved.object(c.object).fields}
    ]
    assert broken == [], f"saved on a missing field: {broken}"
    text = app.w.status.text()
    assert "HP32 max" in shown or "Not brought over: HP32 max" in text, (shown, text)


def test_tc40_merge_keeps_object_ids_unique_with_repeated_names():
    """TC-40 (BUG-06 fix, new edge): the old notes hold two values both named "Ammo" (two
    cheats with one name, as TC-23); the new notes hold a lost "Ammo". for_merge must not
    give both old values the lost one's id (ids are how cheats find their value)."""
    from cheatmaker.core.values import ValueType

    def ammo(obj_id: str, off: int) -> pj.ObjectSpec:
        return pj.ObjectSpec(
            obj_id,
            "Ammo",
            None,
            [pj.Field("Ammo", 0, ValueType.INT32, (0, 999), 30)],
            [pj.StaticAnchor(off)],
        )

    src = pj.Project(fg.TITLE, fg.VERSION, objects=[ammo("obj1", 0x40), ammo("obj2", 0x80)])
    lost = ammo("obj1", 0x40)
    lost.anchors, lost.check = [], True
    into = pj.Project(fg.TITLE, NEW, objects=[lost])
    merged = cr.for_merge(src, into)
    ids = [o.id for o in merged.objects]
    assert len(ids) == len(set(ids)), ids
