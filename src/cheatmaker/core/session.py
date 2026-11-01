"""The connected console and the attached game (AC-1, AC-2)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from cheatmaker.console.client import ConsoleClient, MemMap
from cheatmaker.core.cheats import MAIN_MODULE, LocationKind
from cheatmaker.errors import ConnectionLost, GameNotFound, UnsupportedServer
from cheatmaker.logs import game_context

log = logging.getLogger(__name__)

MIN_NG_VERSION = (1, 3)
"""PS5Debug-NG 1.3.x (we target 1.3.2+, the release that added fw 13.60). The
version command reports the protocol version, e.g. "1.3"; console test S1 checks
what 1.3.2 really reports."""

TITLE_ID_RE = re.compile(r"^(PPSA|CUSA)\d{5}$", re.IGNORECASE)

ClientFactory = Callable[[str, int], ConsoleClient]


@dataclass(frozen=True)
class GameInfo:
    pid: int
    title_id: str
    title: str
    version: str
    process: str
    module_base: int
    module_end: int

    @property
    def is_ps4(self) -> bool:
        return self.title_id.upper().startswith("CUSA")

    @property
    def kind_label(self) -> str:
        return "PS4 game" if self.is_ps4 else "PS5 game"


def clean_host(text: str) -> str:
    """Accept what people paste: "http://192.168.0.193/", "192.168.0.193:744", spaces."""
    host = text.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    if host.count(":") == 1:  # host:port (an IPv6 address has more colons)
        host = host.split(":", 1)[0]
    return host.strip()


def parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text)
    return tuple(int(n) for n in nums)


def _is_main_module(m: MemMap, process: str) -> bool:
    name = m.name.lower()
    return name in (process.lower(), MAIN_MODULE) or name.endswith(MAIN_MODULE)


class Session:
    def __init__(self, factory: ClientFactory) -> None:
        self._factory = factory
        self.client: ConsoleClient | None = None
        self.server_version = ""
        self.game: GameInfo | None = None
        self.ip = ""
        self.on_game_changed: list[Callable[[GameInfo | None], None]] = []

    # -- connection ----------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.client is not None

    def connect(self, ip: str, port: int = 744) -> str:
        """Open the connection and check the PS5Debug-NG version. Returns the version."""
        ip = clean_host(ip)
        if not ip:
            raise ValueError("Type your PS5's IP address first.")
        self.disconnect()
        client = self._factory(ip, port)
        try:
            ver = client.version()
            if parse_version(ver)[:2] < MIN_NG_VERSION:
                raise UnsupportedServer(f"PS5Debug-NG reports version {ver!r}")
        except Exception:
            client.close()
            raise
        self.client, self.server_version, self.ip = client, ver, ip
        log.info("action=connect ip=%s ng_version=%s", ip, ver)
        return ver

    def disconnect(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            finally:
                self.client = None
        self._set_game(None)

    def require_client(self) -> ConsoleClient:
        if self.client is None:
            raise ConnectionLost("not connected to the PS5")
        return self.client

    def require_game(self) -> GameInfo:
        if self.game is None:
            raise GameNotFound("no game attached")
        return self.game

    # -- game ----------------------------------------------------------------
    def refresh_game(self) -> GameInfo:
        """Find the running game (AC-2.1/2.4). Raises GameNotFound (AC-2.2)."""
        client = self.require_client()
        fg = client.foreground_app()
        pid, title_id, title, version = None, "", "", ""
        if fg is not None and TITLE_ID_RE.match(fg.title_id):
            pid, title_id, title, version = fg.pid, fg.title_id, fg.title, fg.version
        else:
            for p in client.processes():
                if TITLE_ID_RE.match(p.title_id):
                    pid, title_id, title = p.pid, p.title_id, p.name
                    break
        if pid is None:
            self._set_game(None)
            raise GameNotFound("no PPSA/CUSA process running")
        process = next((p.name for p in client.processes() if p.pid == pid), MAIN_MODULE)
        base, end = self._module_range(client.maps(pid), process)
        game = GameInfo(
            pid, title_id.upper(), title or title_id, version or "unknown", process, base, end
        )
        log.info(
            "action=detect_game pid=%d title_id=%s version=%s process=%s base=%#x",
            pid,
            game.title_id,
            game.version,
            process,
            base,
        )
        self._set_game(game)
        return game

    @staticmethod
    def _module_range(maps: list[MemMap], process: str) -> tuple[int, int]:
        mods = [m for m in maps if _is_main_module(m, process)]
        if not mods:
            # Fall back to the lowest executable map; logged so S1 can confirm names.
            exe = [m for m in maps if m.executable]
            if not exe:
                raise GameNotFound("can't find the game's main module in its memory map")
            log.warning(
                "action=detect_game no map named %s; using lowest executable map %r",
                MAIN_MODULE,
                min(exe, key=lambda m: m.start).name,
            )
            first = min(exe, key=lambda m: m.start)
            mods = [m for m in maps if m.name == first.name] or [first]
        return min(m.start for m in mods), max(m.end for m in mods)

    def _set_game(self, game: GameInfo | None) -> None:
        changed = game != self.game
        self.game = game
        game_context.set(game.title_id if game else None, game.version if game else None)
        if changed:
            for cb in list(self.on_game_changed):
                cb(game)

    # -- memory helpers ------------------------------------------------------
    def scannable_regions(self) -> list[MemMap]:
        """Readable + writable, non-code memory of the game: where values live."""
        game = self.require_game()
        return [
            m
            for m in self.require_client().maps(game.pid)
            if m.readable and m.writable and not m.executable and m.size > 0
        ]

    def classify(self, address: int) -> tuple[LocationKind, int]:
        """Is this value inside the game's own module (saveable) or on the heap?"""
        game = self.require_game()
        if game.module_base <= address < game.module_end:
            return LocationKind.MODULE_VALUE, address - game.module_base
        return LocationKind.SESSION_ADDRESS, address
