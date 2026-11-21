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


class ConsoleBusy(ReadFailed):
    """The UI asked the PS5 for something while its connection was busy for longer than
    the UI may wait. Not a lost connection: the player can just try again."""


class ScanLinkLost(ReadFailed):
    """The connection a search runs on dropped. Only the search is gone; the session
    (reads, writes, frozen cheats) is still connected."""

    action = "scan"


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


class ProjectFileError(CheatFileError):
    """A ``.cmproj`` (find-it-again data) can't be read."""

    action = "project_load"


class FindFailed(CheatMakerError):
    """Find it next time / Find it again couldn't do it. The message is plain text."""

    action = "find_again"


class BuildFailed(CheatMakerError):
    """Make it work couldn't make a cheat. The message is plain text for the player."""

    action = "build"
