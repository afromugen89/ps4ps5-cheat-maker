import logging
import sys

from cheatmaker import logs
from cheatmaker.settings import Settings


def test_settings_default_when_missing():
    s = Settings.load()
    assert s.last_ip == "" and s.output_format == "json" and not s.first_run_done


def test_settings_roundtrip_remembers_ip():  # AC-1.3
    Settings(last_ip="192.168.1.9", first_run_done=True).save()
    s = Settings.load()
    assert s.last_ip == "192.168.1.9" and s.first_run_done


def test_settings_broken_file_gives_defaults():
    p = Settings.path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert Settings.load() == Settings()


def test_settings_bad_format_and_unknown_keys():
    p = Settings.path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"output_format": "mc4", "extra": 1, "last_ip": "1.2.3.4"}', encoding="utf-8")
    s = Settings.load()
    assert s.output_format == "json" and s.last_ip == "1.2.3.4"


def _flush():
    for h in logging.getLogger().handlers:
        h.flush()


def _remove_handlers():
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_cheatmaker", False):
            root.removeHandler(h)
            h.close()


def test_log_line_has_context_and_rotation_settings():  # AC-9.1, AC-9.3
    try:
        path = logs.setup_logging()
        logs.setup_logging()  # idempotent
        handlers = [h for h in logging.getLogger().handlers if getattr(h, "_cheatmaker", False)]
        assert len(handlers) == 1
        assert handlers[0].maxBytes == 10 * 1024 * 1024 and handlers[0].backupCount == 5
        logs.game_context.set("CUSA01234", "01.05")
        logging.getLogger("t").error("action=write reason=boom")
        _flush()
        line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
        assert "game=CUSA01234" in line and "ver=01.05" in line and "app=0.1.0" in line
        assert "action=write reason=boom" in line
        assert "action=write" in logs.read_latest_log()
    finally:
        logs.game_context.set(None, None)
        _remove_handlers()


def test_crash_hook_writes_crash_entry(monkeypatch):  # AC-9.5
    called = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: called.append(a))
    try:
        path = logs.setup_logging()
        logs.install_crash_hook()
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        _flush()
        text = path.read_text(encoding="utf-8")
        assert "action=crash" in text and "kaboom" in text
        assert called, "previous hook still runs"
    finally:
        _remove_handlers()


def test_read_latest_log_missing_file():
    assert logs.read_latest_log() == ""


def test_build_notice_hidden_setting():
    s = Settings.load()
    assert s.build_notice_hidden is False
    s.build_notice_hidden = True
    s.save()
    assert Settings.load().build_notice_hidden is True
    Settings.path().write_text('{"build_notice_hidden": "yes"}', encoding="utf-8")
    assert Settings.load().build_notice_hidden is False, "not a yes/no: shown again"
