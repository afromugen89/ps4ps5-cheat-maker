"""Plain-language messages for the player (AC-9.2) and logging of the reason (AC-9.1)."""

from __future__ import annotations

import logging

from cheatmaker import errors as e

log = logging.getLogger("cheatmaker.errors")

LOST = "Lost connection to the PS5. Check it's on, then press Connect."
UNREACHABLE = "Can't reach the PS5. Load PS5Debug-NG on the PS5, then try again."
NO_GAME = "No game running. Start a game, then press Refresh."
CANT_READ_FILE = "This cheat file can't be read."

_MESSAGES: list[tuple[type[BaseException], str]] = [
    (e.ConsoleUnreachable, UNREACHABLE),
    (e.ConnectionLost, LOST),
    (
        e.UnsupportedServer,
        "Your PS5Debug-NG is too old. Load version 1.3.2 or newer, then try again.",
    ),
    (e.GameNotFound, NO_GAME),
    (e.WriteFailed, "Couldn't change this value ({reason}). Press Refresh and search again."),
    (e.ReadFailed, "Couldn't read the game's memory. Press Refresh and try again."),
    (e.ScanCancelled, "Search cancelled."),
    (
        e.ScanNotSupported,
        "\"I don't know the number\" isn't ready for the PS5 yet. Search for a number you can "
        "see instead.",
    ),
    (e.CheatFileError, CANT_READ_FILE),
    (e.SaveFailed, "Couldn't save the file. Pick another folder and try again."),
    (
        e.UploadFailed,
        "Couldn't send the file to the PS5. Load the ftpsrv payload, or copy the saved file "
        "yourself.",
    ),
    (e.LockFailed, "Couldn't lock this value ({reason}). It still works for this session."),
    (
        e.ProtocolError,
        "The PS5 answered in a way we didn't expect. Restart PS5Debug-NG and try again.",
    ),
]

GENERIC = "Something went wrong. Press Copy log and send it with your bug report."


def user_message(exc: BaseException) -> str:
    if isinstance(exc, ValueError) and not isinstance(exc, e.CheatMakerError):
        return str(exc) or GENERIC
    for kind, text in _MESSAGES:
        if isinstance(exc, kind):
            return text.format(reason=str(exc) or "no reason given")
    return GENERIC


def report(exc: BaseException, action: str | None = None) -> str:
    """Log the technical reason and return the player's message."""
    act = action or getattr(exc, "action", "unknown")
    if isinstance(exc, ValueError | e.ScanCancelled):
        log.info("action=%s reason=%s", act, exc)
    elif isinstance(exc, e.CheatMakerError):
        log.error("action=%s reason=%s: %s", act, type(exc).__name__, exc)
    else:
        log.error("action=%s unexpected error", act, exc_info=exc)
    return user_message(exc)
