"""The game side of the LuaDebug wire protocol, as much of it as the bridge uses.

Framing and message numbers are bg3le's (third_party/bg3le/src/debug_server.cpp; the client side and the .proto
excerpt are in bridge/src/bg3ai/luadebug.py): each packet is a little-endian uint32 holding the TOTAL packet size
(the 4 length bytes included) followed by one protobuf message.

    client -> game  DebuggerToBackend { seq_no = 1; connect = 3 {protocol_version = 1};
                                        evaluate = 7 {context = 1 (SERVER 0 / CLIENT 1); expression = 2} }
    game -> client  BackendToDebugger { reply_seq_no = 2; connectResponse = 3 {protocol_version = 1};
                                        evaluateResponse = 5 {result = 1 MsgValue; error_message = 2};
                                        debugOutput = 8 {message = 1} }
    MsgValue { type_id = 1 (STRING = 4); stringval = 5 }

Like bg3le, the game answers any connect with protocol version 4, ignores every other client message, and returns
every evaluation result as a string. Pure functions only; the socket loop is in fakemod.py.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

PROTOCOL_VERSION = 4
MAX_PACKET = 16 << 20
_STRING = 4


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def uint_field(num: int, value: int) -> bytes:
    return varint(num << 3) + varint(value)


def bytes_field(num: int, value: bytes) -> bytes:
    return varint((num << 3) | 2) + varint(len(value)) + value


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7


def fields(buf: bytes) -> dict[int, list[int | bytes]]:
    """Decode one protobuf message into {field number: [values]} (varint and length-delimited fields only)."""
    out: dict[int, list[int | bytes]] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, pos = _read_varint(buf, pos)
            out.setdefault(num, []).append(val)
        elif wire == 2:
            n, pos = _read_varint(buf, pos)
            out.setdefault(num, []).append(buf[pos : pos + n])
            pos += n
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return out


def first(msg: dict[int, list[int | bytes]], num: int, default: int | bytes) -> int | bytes:
    vals = msg.get(num)
    return vals[0] if vals else default


def frame(msg: bytes) -> bytes:
    return struct.pack("<I", len(msg) + 4) + msg


@dataclass(frozen=True)
class Request:
    """One client message: a connect, an evaluate, or something the game ignores (kind "other")."""

    kind: str
    seq: int
    context: str = "server"
    code: str = ""


def parse_request(body: bytes) -> Request:
    msg = fields(body)
    seq = int(first(msg, 1, 0))
    if 3 in msg:
        return Request("connect", seq)
    if 7 in msg:
        ev = fields(bytes(first(msg, 7, b"")))
        ctx = "client" if first(ev, 1, 0) else "server"
        return Request("evaluate", seq, ctx, bytes(first(ev, 2, b"")).decode("utf-8"))
    return Request("other", seq)


def connect_response(seq: int) -> bytes:
    return uint_field(2, seq) + bytes_field(3, uint_field(1, PROTOCOL_VERSION))


def evaluate_response(seq: int, result: str = "", error: str = "") -> bytes:
    """A Lua error carries only error_message; a result is always a STRING MsgValue (bg3le tostring()s it)."""
    if error:
        resp = bytes_field(2, error.encode("utf-8"))
    else:
        resp = bytes_field(1, uint_field(1, _STRING) + bytes_field(5, result.encode("utf-8")))
    return uint_field(2, seq) + bytes_field(5, resp)


def debug_output(message: str) -> bytes:
    """A Lua print as bg3le forwards it to every attached debugger (jobpush listens for these)."""
    return bytes_field(8, bytes_field(1, message.encode("utf-8")))
