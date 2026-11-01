import logging

import pytest

from cheatmaker import errors as e
from cheatmaker.ui.messages import GENERIC, report, user_message


@pytest.mark.parametrize(
    "exc,start",
    [
        (e.ConsoleUnreachable("x"), "Can't reach the PS5. Load PS5Debug-NG"),
        (e.ConnectionLost("x"), "Lost connection to the PS5"),
        (e.GameNotFound("x"), "No game running. Start a game, then press Refresh."),
        (e.WriteFailed("refused"), "Couldn't change this value (refused)"),
        (e.CheatFileError("x"), "This cheat file can't be read."),
        (e.UploadFailed("x"), "Couldn't send the file to the PS5"),
        (e.LockFailed("nothing"), "Couldn't lock this value (nothing)"),
        (ValueError("Type a number first."), "Type a number first."),
        (RuntimeError("boom"), GENERIC),
    ],
)
def test_user_message(exc, start):  # AC-9.2
    msg = user_message(exc)
    assert msg.startswith(start) and "Traceback" not in msg


def test_report_logs_action_and_reason(caplog):  # AC-9.1
    with caplog.at_level(logging.INFO, logger="cheatmaker.errors"):
        report(e.WriteFailed("console said no"))
        report(RuntimeError("bug"), "save")
    text = caplog.text
    assert "action=write reason=WriteFailed: console said no" in text
    assert "action=save unexpected error" in text
