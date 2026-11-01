import pytest

from cheatmaker.console.fake import PROT_R, PROT_W, PROT_X
from cheatmaker.core.cheats import LocationKind
from cheatmaker.core.session import Session, clean_host, parse_version
from cheatmaker.errors import ConnectionLost, GameNotFound, UnsupportedServer
from tests.conftest import HEALTH_ADDR, MODULE_BASE, PID, STATIC_ADDR, build_fake


def test_detects_ps5_game(session):  # AC-2.1
    g = session.game
    assert (g.title_id, g.title, g.version, g.process) == (
        "PPSA01234",
        "Test Game",
        "01.000",
        "eboot.bin",
    )
    assert g.module_base == MODULE_BASE and g.kind_label == "PS5 game"


def test_detects_ps4_game():  # AC-2.4
    fake = build_fake("CUSA05347")
    s = Session(lambda ip, port: fake)
    s.connect("10.0.0.2")
    assert s.refresh_game().kind_label == "PS4 game"


def test_no_game(fake):  # AC-2.2
    fake.remove_process(PID)
    s = Session(lambda ip, port: fake)
    s.connect("10.0.0.2")
    with pytest.raises(GameNotFound):
        s.refresh_game()
    assert s.game is None


def test_falls_back_to_process_list(fake):
    fake.foreground_pid = None
    s = Session(lambda ip, port: fake)
    s.connect("1.1.1.1")
    assert s.refresh_game().pid == PID


def test_module_fallback_to_lowest_exec_map():
    from cheatmaker.console.fake import FakeConsole

    fake = FakeConsole()
    fake.add_process(5, "game.elf", "PPSA00002", "G")
    fake.add_region(5, "game.elf", 0x800000, b"\0" * 16, PROT_R | PROT_X)
    fake.add_region(5, "game.elf", 0x800010, b"\0" * 16, PROT_R | PROT_W)
    s = Session(lambda ip, port: fake)
    s.connect("1.1.1.1")
    g = s.refresh_game()
    assert (g.module_base, g.module_end) == (0x800000, 0x800020)


def test_no_module_at_all():
    from cheatmaker.console.fake import FakeConsole

    fake = FakeConsole()
    fake.add_process(5, "game.elf", "PPSA00002", "G")
    fake.add_region(5, "[anon]", 0x1000, b"\0" * 16)
    s = Session(lambda ip, port: fake)
    s.connect("1.1.1.1")
    with pytest.raises(GameNotFound):
        s.refresh_game()


def test_rejects_old_server(fake):
    fake.server_version = "1.2"
    s = Session(lambda ip, port: fake)
    with pytest.raises(UnsupportedServer):
        s.connect("1.1.1.1")
    assert not s.connected and not fake.connected


def test_empty_ip():
    with pytest.raises(ValueError):
        Session(lambda ip, port: None).connect("  ")


def test_require_client_when_disconnected(session):
    session.disconnect()
    with pytest.raises(ConnectionLost):
        session.require_client()
    with pytest.raises(GameNotFound):
        session.require_game()


def test_classify(session):
    assert session.classify(STATIC_ADDR) == (LocationKind.MODULE_VALUE, STATIC_ADDR - MODULE_BASE)
    assert session.classify(HEALTH_ADDR) == (LocationKind.SESSION_ADDRESS, HEALTH_ADDR)


def test_scannable_regions_skip_code(session):
    assert all(not m.executable and m.writable for m in session.scannable_regions())


def test_game_changed_callback(session, fake):
    seen = []
    session.on_game_changed.append(seen.append)
    fake.remove_process(PID)
    fake.add_process(88, "eboot.bin", "PPSA09999", "Other")
    fake.add_region(88, "eboot.bin", 0x400000, b"\0" * 16, PROT_R | PROT_X)
    session.refresh_game()
    assert seen and seen[-1].title_id == "PPSA09999"


@pytest.mark.parametrize("text,expected", [("1.3", (1, 3)), ("v1.3.2", (1, 3, 2)), ("", ())])
def test_parse_version(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "http://192.168.0.193/",
        "http://192.168.0.193:744",
        " 192.168.0.193 ",
        "192.168.0.193:744",
        "192.168.0.193",
    ],
)
def test_clean_host(text):
    assert clean_host(text) == "192.168.0.193"


def test_connect_accepts_pasted_url(fake):
    seen = []
    s = Session(lambda ip, port: seen.append(ip) or fake)
    s.connect("http://192.168.0.193/")
    assert seen == ["192.168.0.193"] and s.ip == "192.168.0.193"
