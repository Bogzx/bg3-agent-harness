"""No test reaches the real game's Lua debug port (or its input shim's socket) unless it is marked `live` (2026-11-02,
D385; the shim since 2026-11-03: the runner takes the shim's input at a game's start, gio.take_input).

bg3le listens on 127.0.0.1:9998 ($BG3LE_DEBUG_PORT, bridge settings.py) and the test machine (dev2) runs the live
game there while tests run. A plain TestCase that used the bridge's real core.game sent act.watch to the live game of
a paid run (test_batch_args SameText, 295748bc), and much code connects to 9998 by default without reading the env
(skipper.start_background, bg3play BridgeStatus, snapshots.run, port probes). So the guard is at the socket:
socket.socket.connect and connect_ex refuse the guarded ports before any packet leaves (LivePortError, a
ConnectionRefusedError, so the code under test sees "no game"), and record the attempt; the pytest plugin
(pytest_profile) fails the test that made it. temp_profile also points $BG3LE_DEBUG_PORT at a dead port.
"""

from __future__ import annotations

import errno
import socket
from collections.abc import Iterable
from typing import Any, cast

from bg3harness.testing.profile import DEAD_PORT

LIVE_PORT = 9998  # bg3le's default (bg3ai.luadebug.DEFAULT_PORT)
SHIM_SOCKET = "\0bg3ai-input"  # the live game's input shim (bg3ai.input.DEFAULT_SOCKET, an abstract unix socket)


class LivePortError(ConnectionRefusedError):
    pass


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
ports: frozenset[int] = frozenset()
allowed = False  # true while a test marked live runs
hits: list[str] = []  # the refused attempts not yet reported


def guarded_ports(env_port: str | None) -> frozenset[int]:
    """9998, and the port the shell's $BG3LE_DEBUG_PORT named before the test profile replaced it (not the dead one:
    a nested pytest run, or a shell that already exports it)."""
    extra: set[int] = {int(env_port)} if env_port and env_port.isdigit() and env_port != DEAD_PORT else set()
    return frozenset({LIVE_PORT} | extra)


def _host_port(address: object) -> tuple[object, object] | None:
    """(host, port) of an AF_INET/AF_INET6 address; None for a unix socket's path or name."""
    if not isinstance(address, tuple):
        return None
    t = cast("tuple[object, ...]", address)
    return (t[0], t[1]) if len(t) >= 2 else None


def blocked(address: object) -> bool:
    if address == SHIM_SOCKET:  # a test moving the live game's cursor or pressing its keys
        return not allowed
    hp = _host_port(address)
    return not allowed and hp is not None and hp[1] in ports


def _refuse(address: object) -> str:
    if address == SHIM_SOCKET:
        msg = "connect to @bg3ai-input refused: a test reached the real game's input shim (mark it live to opt in)"
        hits.append(msg)
        return msg
    host, port = _host_port(address) or ("?", "?")
    msg = f"connect to {host}:{port} refused: a test reached the real game's debug port (mark it live to opt in)"
    hits.append(msg)
    return msg


def _connect(self: socket.socket, address: Any) -> None:  # Any: typeshed's socket._Address
    if blocked(address):
        raise LivePortError(errno.ECONNREFUSED, _refuse(address))
    return _real_connect(self, address)


def _connect_ex(self: socket.socket, address: Any) -> int:
    if blocked(address):
        _refuse(address)
        return errno.ECONNREFUSED
    return _real_connect_ex(self, address)


def install(guard: Iterable[int]) -> None:
    global ports
    ports = frozenset(guard)
    socket.socket.connect = _connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]


def take_hits() -> list[str]:
    """The refused attempts since the last call, each once (a client retries)."""
    out = list(dict.fromkeys(hits))
    hits.clear()
    return out
