"""vtable re-find (architecture v3, option C): find an object again by its class.

A C++ object starts with a pointer to its class's vtable, which lives at a fixed
offset in the game's module. After a restart one exact 8-byte search for that value
lists every live object of the class (the player, sometimes a few more). No game
code is changed. ``refind`` then verifies each hit (fields plausible) and asks the
player when more than one passes (AC-12.3).

On Returnal one exact 8-byte search took about 150 s (C3), so this is the fallback
after the hook slot, not the first try.
"""

from __future__ import annotations

import logging
import threading

from cheatmaker.console.client import ProgressFn
from cheatmaker.core.session import Session
from cheatmaker.core.values import Compare, ValueType

log = logging.getLogger(__name__)

MAX_RESULTS = 2_000
"""More objects of one class than this and it isn't a "you" object: stop listing."""


def find_by_vtable(
    session: Session,
    vtable: int,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> list[int]:
    """Addresses (8-byte aligned, lowest first) whose first 8 bytes are the vtable at
    module offset ``vtable``. Uses the console's search session, so it ends any search
    the player had open (``Session.take_search`` tells the Scanner first)."""
    game = session.require_game()
    client = session.require_client()
    value = game.module_base + vtable
    session.take_search()
    try:
        n = client.scan_start(
            game.pid,
            session.scannable_regions(),
            ValueType.INT64,
            Compare.EXACT,
            value,
            progress,
            cancel,
        )
        hits = sorted(
            h.address for h in client.scan_results(min(n, MAX_RESULTS)) if h.address % 8 == 0
        )
    finally:
        client.scan_cancel()
    log.info("action=vtable_scan vtable=%#x matches=%d aligned=%d", value, n, len(hits))
    return hits


__all__ = ["MAX_RESULTS", "find_by_vtable"]
