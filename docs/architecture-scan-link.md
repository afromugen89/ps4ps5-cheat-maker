# Scan link: searches on their own PS5 connection (2026-11-21)

Cause: a long snapshot (86-156 s/type) holds the client's only socket lock; UI-thread reads/writes and the freezer wait, so the window hangs and cheats stop being written.

Design (architect, option A + backstop):
- Ps5DebugNgClient owns `_link` (second Ps5DebugNgClient, opened lazily, authed, copies _turbo/_engines/scan_connections/fast_scan). Public scan methods (scan_start, scan_next, scan_rebase, scan_results, scan_restrict, scan_cancel, find_pointers) forward to it; scan_counts/scan_way_counts read link state lock-free ({} with no link, never open one).
- Main connection keeps read/write/read_many/maps/processes/foreground_app/version/watch.
- Link can't open -> warn, fall back to main (old behaviour).
- close() closes link. Link ConnectionLost -> search error, drop link, session stays connected. _reconnect() must not touch the link.
- scan_cancel from UI: try link lock non-blocking; busy -> mark discarded, detach, close its sockets; worker maps errors to ScanCancelled.
- Connection cap: pool capped so total <= 11 (+1 transient cancel).
- Backstop: on the main thread, main lock acquire timeout ~1.5 s -> ConsoleBusy(ReadFailed) "PS5 is busy, try again"; not a lost connection.
- Tests 1-9: gated FakeNgServer snapshot, multi-type, pointer pass, cancel while gated, link drop, reconnect during search, close, backstop, UI-level.
