"""The Pro debugger session (architecture-pro "Console connections and slots").

``DebugSession`` owns everything about one debugger session on PS5Debug-NG:

- **Its own connection** on port 744, opened only by ``attach`` (AC-1.1), so a long
  watch never holds the socket that reads, writes and freezes use.
- **The back channel.** The PS5 dials back to the PC on port 755 and sends one
  0x4A0-byte packet per stop. The listener is bound *before* ``DEBUG_ATTACH``; a
  failed bind is "Port 755 is in use". When the PS5 hasn't dialled back
  ``BACK_CHANNEL_WAIT`` seconds after attaching, the session detaches and raises
  FirewallBlocked naming the port (AC-1.3, within 10 s in all).
- **Already debugged** (a session cut off earlier): detach, wait 2 s, try once more.
- **Keep-alive.** ``VERSION`` every ``KEEPALIVE_S`` on the debugger socket, only while
  attached, running and idle (never while a thread is stopped or a command is
  pending). ``KEEPALIVE_MISSES`` misses in a row make the session lost; it never
  reconnects by itself.
- **DR6.** Each stop carries the debug registers; bits 0-3 of DR6 say which of the
  four watch slots fired (NG 1.3.2 keeps DR6 intact: console test C-P3d).
- **Detach** clears all four slots, sends ``DEBUG_DETACH`` and closes the listener; it
  is safe to call more than once and runs in ``finally`` on every end path
  (``with DebugSession(...)``).

The command IDs and layouts match ``ps5debug_ng.py`` and ReaperPy's ``ps5_ext.py``
(MIT, credited in NOTICE); the behaviour above follows ReaperPy's debugger. All of it
is UNVERIFIED on the console until tests C-P3a..e pass.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import socket
import struct
import threading
import time
from collections.abc import Callable

from cheatmaker.console.client import DEBUG_SLOTS, DebugState, StopEvent, WatchHit, WatchKind
from cheatmaker.console.ps5debug_ng import (
    _DR7_KIND,
    _DR7_LEN,
    ATTACH_TIMEOUT,
    INTERRUPT_PORT,
    INTERRUPT_SIZE,
    REG64_OFFSET,
    REG64_SIZE,
    Cmd,
    Ps5DebugNgClient,
    Status,
    _Wire,
    parse_struct_reg,
)
from cheatmaker.errors import (
    CheatMakerError,
    ConnectionLost,
    DebuggerError,
    DebuggerLost,
    FirewallBlocked,
    PortInUse,
    ProtocolError,
)

log = logging.getLogger(__name__)

BACK_CHANNEL_WAIT = 5.0
"""Seconds after attaching for the PS5 to dial back before the firewall is blamed."""
KEEPALIVE_S = 2.0
KEEPALIVE_MISSES = 2
ALREADY_DEBUG_WAIT = 2.0
"""After an "already debugged" answer: detach, wait this long, attach once more."""
POLL_S = 0.5  # the back-channel reader checks for the end this often
FPU_OFFSET = 0xE0
FPU_SIZE = 832
DBREGS_OFFSET = 0x420
DR6_INDEX = 6
SIGKILL = 9
STOPPED_STATUS = 0x7F
"""A wait status whose low byte is 0x7F is "stopped"; the signal is the next byte."""

PORT_IN_USE = (
    "Port {port} is in use by another program, so the debugger can't start. Close the "
    "other debugger (e.g. ReaperPy) and try again."
)
FIREWALL = (
    "The PS5 didn't connect back on TCP port {port}. Windows Firewall is probably "
    "blocking it: allow Cheat Maker on private networks (or open TCP port {port}), then "
    "try again."
)
ATTACH_REFUSED = "The PS5 wouldn't attach the debugger to the game ({reason})."
GAME_CLOSED = "The game closed while the debugger was attached."
BACK_CLOSED = "The PS5 closed the debugger's back channel."
KEEPALIVE_LOST = "The debugger's connection stopped answering."
NOT_ATTACHED = "The debugger isn't attached."


def stop_signal(status: int) -> int:
    """The signal of a FreeBSD wait status, 0 when it isn't a stop."""
    return (status >> 8) & 0xFF if status & 0xFF == STOPPED_STATUS else 0


def parse_stop(packet: bytes) -> StopEvent:
    """One 0x4A0-byte back-channel packet: lwpid and status at 0, the thread name at
    [8:48], ``struct reg`` at 0x30, savefpu at 0xE0, and ``dbregs`` (16 x u64) at 0x420
    with DR6 at index 6."""
    if len(packet) != INTERRUPT_SIZE:
        raise ProtocolError(f"stop packet is {len(packet)} bytes, expected {INTERRUPT_SIZE}")
    lwpid, status = struct.unpack_from("<II", packet, 0)
    name = packet[8:REG64_OFFSET].split(b"\x00", 1)[0].decode("utf-8", "replace")
    regs = parse_struct_reg(packet[REG64_OFFSET : REG64_OFFSET + REG64_SIZE])
    dbregs = struct.unpack_from("<16Q", packet, DBREGS_OFFSET)
    return StopEvent(
        lwpid,
        name,
        WatchHit(regs["rip"], regs, raw=bytes(packet)),
        fpu=bytes(packet[FPU_OFFSET : FPU_OFFSET + FPU_SIZE]),
        dr6=dbregs[DR6_INDEX],
        signal=stop_signal(status),
    )


def watch_body(slot: int, addr: int, size: int, kind: WatchKind) -> bytes:
    """``DEBUG_SET_WATCHPOINT``'s 24-byte body (index, enabled, length code, breaktype,
    address). ValueError for a slot, size or kind x86 can't watch."""
    if not 0 <= slot < DEBUG_SLOTS:
        raise ValueError(f"watch slot {slot} isn't 0-{DEBUG_SLOTS - 1}")
    if size not in _DR7_LEN:
        raise ValueError(f"can't watch {size} bytes")
    if kind not in _DR7_KIND:
        raise ValueError(f"unknown watch kind {kind!r}")
    if kind == "execute" and size != 1:
        raise ValueError("an execute breakpoint covers 1 byte")
    return struct.pack("<IIIIQ", slot, 1, _DR7_LEN[size], _DR7_KIND[kind], addr)


def _clear_body(slot: int) -> bytes:
    return struct.pack("<IIIIQ", slot, 0, 0, 0, 0)


class DebugSession:
    """See the module docstring. Thread use: one worker drives it (attach, watches,
    ``next_stop``, ``resume``, detach); the session's own threads read the back channel
    and send the keep-alive."""

    def __init__(
        self,
        host: str,
        port: int,
        pid: int,
        *,
        listen_port: int = INTERRUPT_PORT,
        timeout: float = 8.0,
        back_wait: float = BACK_CHANNEL_WAIT,
        keepalive_s: float = KEEPALIVE_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.host, self.port, self.pid = host, port, pid
        self.listen_port = listen_port  # tests use 0: any free port
        self.listen_bound = 0  # the port really listened on
        self.timeout = timeout
        self.back_wait = back_wait
        self.keepalive_s = keepalive_s
        self.sleep = sleep
        self.state: DebugState = "detached"
        self._w: _Wire | None = None
        self._listener: socket.socket | None = None
        self._back: socket.socket | None = None
        self._attached = False
        self._dead = False  # the debugger socket failed: nothing more is sent on it
        self._cmd = threading.Lock()  # one command at a time on the debugger socket
        self._stops: queue.Queue[StopEvent | DebuggerLost] = queue.Queue()
        self._waiting = 0  # stops reported and not resumed yet
        self._count = threading.Lock()  # guards _waiting and state (reader vs worker)
        self._end = threading.Event()
        self._threads: list[threading.Thread] = []
        self._misses = 0
        self._lost_why = KEEPALIVE_LOST
        self.slots: dict[int, tuple[int, int, WatchKind]] = {}

    # -- context manager: detach in finally ------------------------------------------
    def __enter__(self) -> DebugSession:
        self.attach()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.detach()

    # -- attach ----------------------------------------------------------------------------
    def attach(self) -> None:
        if self.state != "detached":
            raise DebuggerError("The debugger is already attached.")
        started = time.monotonic()
        try:
            self._listen()
            self._w = _Wire(Ps5DebugNgClient._open(self.host, self.port, self.timeout))
            self._attach_command()
            self._accept_back()
        except BaseException:
            self._close()
            raise
        self._end.clear()
        self._stops = queue.Queue()
        self._lost_why = KEEPALIVE_LOST
        self.state = "running"
        for target in (self._read_back, self._keep_alive):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        log.info(
            "action=pro_debug_attach pid=%d port=%d took_ms=%d",
            self.pid,
            self.listen_bound,
            (time.monotonic() - started) * 1000,
        )

    def _listen(self) -> None:
        """Bind the back-channel port before attaching (the PS5 dials in at once)."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("0.0.0.0", self.listen_port))
            listener.listen(1)
        except OSError as exc:
            listener.close()
            log.warning("action=pro_debug_listen port=%d reason=%s", self.listen_port, exc)
            raise PortInUse(PORT_IN_USE.format(port=self.listen_port)) from exc
        self._listener = listener
        self.listen_bound = listener.getsockname()[1]

    def _attach_command(self) -> None:
        for attempt in (1, 2):
            status = self._request(Cmd.DEBUG_ATTACH, struct.pack("<I", self.pid), ATTACH_TIMEOUT)
            if status == Status.SUCCESS:
                self._attached = True
                return
            if status == Status.ALREADY_DEBUG and attempt == 1:
                log.info("action=pro_debug_attach result=already_debugged retry=1")
                self._request(Cmd.DEBUG_DETACH)
                self.sleep(ALREADY_DEBUG_WAIT)
                continue
            raise DebuggerError(ATTACH_REFUSED.format(reason=_status_name(status)))

    def _accept_back(self) -> None:
        assert self._listener is not None
        self._listener.settimeout(self.back_wait)
        try:
            conn, peer = self._listener.accept()
        except OSError as exc:  # includes the timeout
            log.warning(
                "action=pro_debug_attach result=no_back_channel port=%d reason=%s",
                self.listen_bound,
                exc,
            )
            raise FirewallBlocked(FIREWALL.format(port=self.listen_port)) from exc
        conn.settimeout(POLL_S)
        self._back = conn
        log.info("action=pro_debug_back_channel peer=%s", peer[0])

    # -- commands ------------------------------------------------------------------------
    def _request(self, cmd: int, body: bytes = b"", timeout: float | None = None) -> int:
        """Send one command and return its status. A dropped socket is DebuggerLost
        (never ConnectionLost: the main connection is fine)."""
        w = self._w
        if w is None or self._dead:
            raise DebuggerError(NOT_ATTACHED)
        with self._cmd:
            try:
                old = w.sock.gettimeout()
                if timeout is not None:
                    w.sock.settimeout(timeout)
                try:
                    w.request(cmd, body)
                    return w.status()
                finally:
                    with contextlib.suppress(OSError):
                        w.sock.settimeout(old)
            except (ConnectionLost, OSError) as exc:
                self._dead = True
                self._lose(KEEPALIVE_LOST)
                raise DebuggerLost(f"{KEEPALIVE_LOST} ({exc})") from exc

    def _command(self, cmd: int, body: bytes, what: str) -> None:
        if self.state == "detached":
            raise DebuggerError(NOT_ATTACHED)
        if self.state == "lost":
            raise DebuggerLost(self._lost_why)
        status = self._request(cmd, body)
        if status != Status.SUCCESS:
            raise DebuggerError(f"{what}: the PS5 answered {_status_name(status)}")

    def set_watch(self, slot: int, addr: int, size: int, kind: WatchKind) -> None:
        body = watch_body(slot, addr, size, kind)
        self._command(Cmd.DEBUG_SET_WATCHPOINT, body, "set watch")
        self.slots[slot] = (addr, size, kind)
        log.info("action=pro_debug_watch slot=%d addr=%#x size=%d kind=%s", slot, addr, size, kind)

    def clear_watch(self, slot: int) -> None:
        if not 0 <= slot < DEBUG_SLOTS:
            raise ValueError(f"watch slot {slot} isn't 0-{DEBUG_SLOTS - 1}")
        self._command(Cmd.DEBUG_SET_WATCHPOINT, _clear_body(slot), "clear watch")
        self.slots.pop(slot, None)

    def resume(self) -> None:
        self._command(Cmd.DEBUG_CONTINUE, struct.pack("<I", 0), "continue")
        with self._count:
            self._waiting = max(0, self._waiting - 1)
            if self._waiting == 0 and self.state == "stopped":
                self.state = "running"

    # -- stops -----------------------------------------------------------------------------
    def next_stop(self, timeout: float) -> StopEvent | None:
        if self.state == "detached":
            raise DebuggerError(NOT_ATTACHED)
        try:
            item = self._stops.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if isinstance(item, DebuggerLost):
            self._stops.put(item)  # every later call says the same
            raise item
        if item.signal == SIGKILL:
            self._lose(GAME_CLOSED)
            raise DebuggerLost(GAME_CLOSED)
        return item

    def _read_back(self) -> None:
        conn = self._back
        if conn is None:
            return
        buf = b""
        while not self._end.is_set():
            try:
                chunk = conn.recv(INTERRUPT_SIZE - len(buf))
            except TimeoutError:
                continue
            except OSError as exc:
                if not self._end.is_set():
                    log.warning("action=pro_debug_back_channel result=lost reason=%s", exc)
                    self._lose(BACK_CLOSED)
                return
            if not chunk:
                if not self._end.is_set():
                    self._lose(BACK_CLOSED)
                return
            buf += chunk
            if len(buf) < INTERRUPT_SIZE:
                continue
            try:
                stop = parse_stop(buf)
            except CheatMakerError as exc:  # never kill the reader on one bad packet
                log.warning("action=pro_debug_stop result=bad_packet reason=%s", exc)
            else:
                with self._count:
                    self._waiting += 1
                    if self.state == "running":
                        self.state = "stopped"
                self._stops.put(stop)
            buf = b""

    def _lose(self, why: str) -> None:
        with self._count:
            if self.state in ("lost", "detached"):
                return
            self.state = "lost"
            self._lost_why = why
        log.warning("action=pro_debug_lost pid=%d reason=%s", self.pid, why)
        self._stops.put(DebuggerLost(why))

    # -- keep-alive ---------------------------------------------------------------------------
    def _keep_alive(self) -> None:
        while not self._end.wait(self.keepalive_s):
            if self.state != "running" or self._waiting:
                continue  # never while a thread is stopped (AC-11.2)
            if not self._cmd.acquire(blocking=False):
                continue  # a command is on its way: that proves the link too
            try:
                ok = self._ping()
            finally:
                self._cmd.release()
            self._misses = 0 if ok else self._misses + 1
            if self._misses >= KEEPALIVE_MISSES:
                self._lose(KEEPALIVE_LOST)
                return

    def _ping(self) -> bool:
        w = self._w
        if w is None:
            return False
        try:
            w.request(Cmd.VERSION)
            w.recv(w.u32())
        except (ConnectionLost, OSError) as exc:
            log.info("action=pro_debug_keepalive result=miss reason=%s", exc)
            return False
        return True

    # -- detach --------------------------------------------------------------------------------
    def detach(self) -> None:
        """Clear all four slots, ``DEBUG_DETACH``, close the listener. Best effort and
        safe to repeat: whatever fails is logged, the sockets are always closed."""
        if self.state == "detached" and self._w is None:
            return
        self._end.set()
        try:
            if self._attached and not self._dead:
                for slot in range(DEBUG_SLOTS):
                    self._quiet(Cmd.DEBUG_SET_WATCHPOINT, _clear_body(slot))
        finally:
            self._close()
        log.info("action=pro_debug_detach pid=%d", self.pid)

    def _quiet(self, cmd: int, body: bytes = b"") -> None:
        try:
            self._request(cmd, body)
        except CheatMakerError as exc:
            log.warning("action=pro_debug_detach step=%s reason=%s", Cmd(cmd).name, exc)

    def _close(self) -> None:
        """Detach if attached (also after a firewall timeout), then close everything."""
        self._end.set()
        if self._attached and self._w is not None and not self._dead:
            with contextlib.suppress(CheatMakerError, OSError):
                self._request(Cmd.DEBUG_DETACH)
        self._attached = False
        for s in (self._back, self._listener):
            if s is not None:
                with contextlib.suppress(OSError):
                    s.close()
        if self._w is not None:
            self._w.close()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2)
        self._back = self._listener = self._w = None
        self._threads = []
        self.slots.clear()
        self._waiting = 0
        self._dead = False
        self.state = "detached"


def _status_name(status: int) -> str:
    return Status(status).name if status in Status._value2member_map_ else f"{status:#010x}"
