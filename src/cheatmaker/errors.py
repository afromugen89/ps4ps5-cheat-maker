"""Exceptions shared by every layer. The UI turns them into plain text (ui/messages.py)."""

from __future__ import annotations


class CheatMakerError(Exception):
    """Base class. ``action`` names what was being done, for the log (AC-9.1)."""

    action = "unknown"


class ConsoleUnreachable(CheatMakerError):
    action = "connect"


class ConnectionLost(CheatMakerError):
    action = "connection"


class UnsupportedServer(CheatMakerError):
    """PS5Debug-NG is older than the minimum we tested against."""

    action = "connect"


class ProtocolError(CheatMakerError):
    action = "protocol"


class ReadFailed(CheatMakerError):
    action = "read"


class WriteFailed(CheatMakerError):
    action = "write"


class ScanCancelled(CheatMakerError):
    action = "scan"


class ScanNotSupported(CheatMakerError):
    action = "scan"


class GameNotFound(CheatMakerError):
    action = "detect_game"


class CheatFileError(CheatMakerError):
    action = "file_load"


class SaveFailed(CheatMakerError):
    action = "file_save"


class UploadFailed(CheatMakerError):
    action = "upload"


class LockFailed(CheatMakerError):
    action = "lock"
