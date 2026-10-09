"""Client for the Script Extender's Lua debugger TCP protocol (LuaDebug).

Both runtimes we target speak it:

* bg3le (native Linux): src/debug_server.cpp. Listens on 127.0.0.1:9998 by default
  (BG3LE_DEBUG_PORT changes the port, BG3LE_DEBUG=0 turns it off) and reports protocol version 4.
* bg3se (Windows build under Proton): BG3Extender/Lua/Debugger/LuaDebugMessages.cpp, enabled with
  "EnableLuaDebugger": true in ScriptExtenderSettings.json; port 9998 by default; protocol version 5
  (LuaDebugMessages.h: ProtocolVersion = 5).

Wire format
-----------
Each packet is a little-endian uint32 holding the TOTAL packet size, the 4-byte length field included,
followed by one protobuf message (bg3le debug_server.cpp send_packet(): "Framing is a native
little-endian uint32 holding the TOTAL packet size"; bg3se Osiris/Debugger/DebugInterface.cpp:
packetSize = length + 4). bg3le rejects packets larger than 16 MiB.

Messages, from BG3Extender/Lua/Debugger/LuaDebug.proto (identical in bg3se main and in bg3le's
vendor/bg3se copy):

    message DebuggerToBackend {          // client -> game
      uint32 seq_no = 1;
      uint32 reply_seq_no = 2;
      oneof msg {
        DbgConnectRequest connect = 3;          // { uint32 protocol_version = 1; }
        DbgUpdateSettings updateSettings = 4;   // { bool break_on_error = 1; bool break_on_generic_error = 2; }
        DbgContinue continue = 6;               // { DbgContext context = 1; Action action = 2; } CONTINUE = 0
        DbgEvaluate evaluate = 7;               // { DbgContext context = 1; string expression = 2;
      }                                         //   int32 frame = 3; uint32 flags = 4; }
    }
    message BackendToDebugger {          // game -> client
      uint32 seq_no = 1;
      uint32 reply_seq_no = 2;
      oneof msg {
        BkConnectResponse connectResponse = 3;          // { uint32 protocol_version = 1; }
        BkBreakpointTriggered breakpointTriggered = 4;  // { ...; DbgContext context = 3; string message = 4; }
        BkEvaluateResponse evaluateResponse = 5;        // { MsgValue result = 1; string error_message = 2; }
        BkDebugOutput debugOutput = 8;                  // { string message = 1; Severity severity = 2; }
        BkResult results = 9;                           // { StatusCode status_code = 1; }
      }
    }
    message MsgValue { MsgValueType type_id = 1;
                       oneof value { bool boolval = 2; int64 intval = 3; double floatval = 4; string stringval = 5; } }
    enum DbgContext { SERVER = 0; CLIENT = 1; }
    enum MsgValueType { NIL = 0; BOOLEAN = 1; INTEGER = 2; FLOAT = 3; STRING = 4; TABLE = 5; FUNCTION = 6;
                        USERDATA = 7; THREAD = 8; UNKNOWN = 9; NONE = 11; }
    enum StatusCode { SUCCESS = 0; NOT_IN_PAUSE = 1; NO_DEBUGGEE = 2; INVALID_CONTINUE_ACTION = 3; IN_PAUSE = 4;
                      EVAL_ENGINE_NOT_READY = 5; EVAL_FAILED = 6; NO_SUCH_FILE = 7; }

bg3le uses exactly these numbers (debug_server.cpp: kMsgConnect = 3, kMsgEvaluate = 7, kBkConnectResponse = 3,
kBkEvaluateResponse = 5, kBkDebugOutput = 8, kValueTypeString = 4; DbgEvaluate "context=1, expression=2,
frame=3, flags=4") and ignores every other client message. DbgEvaluate.flags are bg3se's EvalFlags
(LuaDebugMessages.h): EvaluateAsStatement = 1, DisallowVariableRefs = 2; bg3le ignores them.

Semantics worth knowing
-----------------------
* Version negotiation: bg3le answers any version with 4 and carries on. bg3se answers with 5 and then
  disconnects if the client asked for anything else, so on a mismatch we reconnect using the version the
  server advertised.
* bg3le runs each evaluation on the story thread (server) or the client game thread and gives up after
  10 s ("timed out waiting for the story thread; is the game paused or still loading?"). The server
  context only exists once a save is loaded; at the main menu use the client context.
* bg3le compiles "return <code>" first and falls back to <code> as a statement, then returns every
  result tostring()'d and tab-joined, always as a string. bg3se returns one typed value.
* bg3se can stop the game on a Lua error and wait for the debugger. We turn that off on connect and
  auto-continue if a breakpoint is ever reported, so this client can never leave the game paused.
"""

from __future__ import annotations

import argparse
import itertools
import queue
import socket
import struct
import sys
import threading
from dataclasses import dataclass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9998
BG3LE_PROTOCOL_VERSION = 4
BG3SE_PROTOCOL_VERSION = 5
MAX_PACKET = 16 << 20

# DebuggerToBackend
_D_SEQ, _D_CONNECT, _D_UPDATE_SETTINGS, _D_CONTINUE, _D_EVALUATE = 1, 3, 4, 6, 7
# BackendToDebugger
_B_REPLY_SEQ, _B_CONNECT, _B_BREAKPOINT, _B_EVALUATE, _B_OUTPUT, _B_RESULT = 2, 3, 4, 5, 8, 9

CONTEXTS = {"server": 0, "client": 1}
EVAL_AS_STATEMENT = 1
EVAL_DISALLOW_REFS = 2

_VALUE_TYPES = {0: "nil", 1: "boolean", 2: "integer", 3: "float", 4: "string", 5: "table",
                6: "function", 7: "userdata", 8: "thread", 9: "unknown", 11: "none"}
_STATUS_CODES = {0: "SUCCESS", 1: "NOT_IN_PAUSE", 2: "NO_DEBUGGEE", 3: "INVALID_CONTINUE_ACTION",
                 4: "IN_PAUSE", 5: "EVAL_ENGINE_NOT_READY", 6: "EVAL_FAILED", 7: "NO_SUCH_FILE"}
_SEVERITIES = {0: "info", 1: "warning", 2: "error", 3: "debug"}


class LuaDebugError(Exception):
    """The game refused or failed an evaluation; the message is the game's own."""


@dataclass(frozen=True)
class DebugOutput:
    message: str
    severity: str  # info | warning | error | debug


# ---- protobuf, by hand: only varint (0) and length-delimited (2) fields are written ----

def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _uint_field(num: int, value: int) -> bytes:
    return _varint(num << 3) + _varint(value)


def _bytes_field(num: int, value: bytes) -> bytes:
    return _varint((num << 3) | 2) + _varint(len(value)) + value


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(buf: bytes) -> dict[int, list[int | bytes]]:
    """Decode one message into {field number: [values]}; varints as int, length-delimited as bytes."""
    out: dict[int, list[int | bytes]] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        num, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire == 2:
            size, pos = _read_varint(buf, pos)
            value = buf[pos:pos + size]
            if len(value) != size:
                raise ValueError("truncated field")
            pos += size
        elif wire == 1:
            value = buf[pos:pos + 8]
            pos += 8
        elif wire == 5:
            value = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.setdefault(num, []).append(value)
    return out


def _first(fields: dict, num: int, default=None):
    values = fields.get(num)
    return values[-1] if values else default


def _text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else ""


def _value_to_str(raw: bytes | None) -> str:
    """MsgValue -> the string a Lua tostring() would give, as far as the message carries it."""
    if raw is None:
        return ""
    v = _fields(raw)
    type_id = _first(v, 1, 0)
    if 5 in v:
        return _text(_first(v, 5))
    if 3 in v:
        n = _first(v, 3)
        return str(n - (1 << 64) if n >= 1 << 63 else n)
    if 4 in v:
        return repr(struct.unpack("<d", _first(v, 4))[0])
    if 2 in v:
        return "true" if _first(v, 2) else "false"
    # proto3 leaves zero values out: an empty string, 0, 0.0 or false arrive as a bare type id.
    return {0: "nil", 1: "false", 2: "0", 3: "0.0", 4: "", 11: ""}.get(type_id, f"<{_VALUE_TYPES.get(type_id, type_id)}>")


class _Pending:
    __slots__ = ("seq", "event", "result", "error")

    def __init__(self, seq: int) -> None:
        self.seq = seq
        self.event = threading.Event()
        self.result: str | None = None
        self.error: BaseException | None = None


class LuaDebugClient:
    """A connection to the game's Lua debug server.

    Plain blocking socket plus one reader thread; safe to call evaluate() from several threads.

        with LuaDebugClient() as lua:
            print(lua.evaluate("return Ext.Utils.GameVersion()", context="client"))
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 protocol_version: int = BG3LE_PROTOCOL_VERSION, connect_timeout: float = 5.0,
                 output_maxsize: int = 10000):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.requested_version = protocol_version
        self.server_version: int | None = None
        self.output: queue.Queue[DebugOutput] = queue.Queue(maxsize=output_maxsize)

        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._seq = itertools.count(1)
        self._closed = threading.Event()

    # ---- connection ----

    def connect(self) -> int:
        """Connect and negotiate the protocol version. Returns the server's version."""
        version = self.requested_version
        for _attempt in range(2):
            self._open()
            server_version = self._handshake(version)
            if server_version == version:
                break
            # bg3se drops a client whose version differs; bg3le doesn't care. Either way, retry with
            # the server's own version so both see a matching handshake.
            self.close()
            version = server_version
        else:
            raise ConnectionError(f"protocol negotiation failed (server speaks {server_version})")
        self.server_version = server_version
        # No break-on-error: a Lua error must never leave the game paused waiting for us (bg3se only).
        self._send(_bytes_field(_D_UPDATE_SETTINGS, b""))
        return server_version

    def _open(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._closed.clear()
        self._reader = threading.Thread(target=self._read_loop, args=(sock,), name="luadebug-reader",
                                        daemon=True)
        self._reader.start()

    def _handshake(self, version: int) -> int:
        body = _uint_field(1, version)
        pending = self._request(_bytes_field(_D_CONNECT, body))
        if not pending.event.wait(self.connect_timeout):
            self._forget(pending)
            raise TimeoutError(f"no connect response from {self.host}:{self.port}")
        if pending.error is not None:
            raise pending.error
        return int(pending.result or 0)

    def close(self) -> None:
        self._closed.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)
        self._reader = None
        self._fail_all(ConnectionError("connection closed"))

    def __enter__(self) -> LuaDebugClient:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closed.is_set()

    # ---- evaluation ----

    def evaluate(self, code: str, context: str = "client", timeout: float = 15.0,
                 statement: bool | None = None) -> str:
        """Run Lua in the game and return its result as a string.

        context: "client" (always available, also at the main menu) or "server" (once a save is loaded).
        statement: how bg3se should compile the code: True for a chunk ("return x", several lines),
        False for a bare expression ("1+1"). None guesses: a chunk if it starts with "return" or has more
        than one line. bg3le ignores this and handles both forms itself.
        Raises LuaDebugError with the game's message on a Lua error or a refusal, TimeoutError when no
        answer arrives within `timeout` (keep it above bg3le's own 10 s limit), ConnectionError on disconnect.
        """
        if context not in CONTEXTS:
            raise ValueError(f"context must be 'client' or 'server', not {context!r}")
        if statement is None:
            stripped = code.strip()
            statement = stripped.startswith("return") or "\n" in stripped
        flags = EVAL_DISALLOW_REFS | (EVAL_AS_STATEMENT if statement else 0)
        body = b""
        if CONTEXTS[context]:  # proto3: SERVER = 0 is the default and is left out
            body += _uint_field(1, CONTEXTS[context])
        body += _bytes_field(2, code.encode("utf-8"))
        body += _uint_field(4, flags)

        pending = self._request(_bytes_field(_D_EVALUATE, body))
        if not pending.event.wait(timeout):
            self._forget(pending)
            raise TimeoutError(f"no answer to evaluate within {timeout} s")
        if pending.error is not None:
            raise pending.error
        return pending.result or ""

    def drain_output(self) -> list[DebugOutput]:
        """Everything the game has printed since the last call (bg3le also replays recent status on attach)."""
        lines = []
        while True:
            try:
                lines.append(self.output.get_nowait())
            except queue.Empty:
                return lines

    # ---- plumbing ----

    def _request(self, payload: bytes) -> _Pending:
        seq = next(self._seq)
        pending = _Pending(seq)
        with self._pending_lock:
            self._pending[seq] = pending
        try:
            self._send(_uint_field(_D_SEQ, seq) + payload)
        except BaseException:
            self._forget(pending)
            raise
        return pending

    def _forget(self, pending: _Pending) -> None:
        with self._pending_lock:
            self._pending.pop(pending.seq, None)

    def _send(self, message: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionError("not connected")
        frame = struct.pack("<I", len(message) + 4) + message
        with self._send_lock:
            sock.sendall(frame)

    def _resolve(self, seq: int, result: str | None = None, error: BaseException | None = None) -> None:
        with self._pending_lock:
            pending = self._pending.pop(seq, None)
        if pending is None:
            return  # answer to a request that already timed out
        pending.result, pending.error = result, error
        pending.event.set()

    def _fail_all(self, error: BaseException) -> None:
        with self._pending_lock:
            pending, self._pending = list(self._pending.values()), {}
        for p in pending:
            p.error = error
            p.event.set()

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _read_loop(self, sock: socket.socket) -> None:
        reason: BaseException = ConnectionError("server closed the connection")
        try:
            while True:
                header = self._recv_exact(sock, 4)
                if header is None:
                    break
                (total,) = struct.unpack("<I", header)
                if total < 4 or total > MAX_PACKET:
                    reason = ConnectionError(f"bad packet size {total}")
                    break
                payload = self._recv_exact(sock, total - 4) if total > 4 else b""
                if payload is None:
                    break
                try:
                    self._dispatch(_fields(payload))
                except ValueError as exc:
                    reason = ConnectionError(f"malformed packet: {exc}")
                    break
        finally:
            if self._sock is sock:
                self._sock = None
                self._closed.set()
            self._fail_all(reason)

    def _dispatch(self, msg: dict) -> None:
        reply = _first(msg, _B_REPLY_SEQ, 0)
        if _B_CONNECT in msg:
            version = _first(_fields(_first(msg, _B_CONNECT)), 1, 0)
            self._resolve(reply, result=str(version))
        elif _B_EVALUATE in msg:
            resp = _fields(_first(msg, _B_EVALUATE))
            error = _text(_first(resp, 2, b""))
            if error:
                self._resolve(reply, error=LuaDebugError(error))
            else:
                self._resolve(reply, result=_value_to_str(_first(resp, 1)))
        elif _B_RESULT in msg:
            code = _first(_fields(_first(msg, _B_RESULT)), 1, 0)
            if code:  # bg3se reports refusals (EVAL_ENGINE_NOT_READY, ...) this way
                self._resolve(reply, error=LuaDebugError(_STATUS_CODES.get(code, f"status {code}")))
        elif _B_OUTPUT in msg:
            out = _fields(_first(msg, _B_OUTPUT))
            line = DebugOutput(_text(_first(out, 1, b"")), _SEVERITIES.get(_first(out, 2, 0), "info"))
            try:
                self.output.put_nowait(line)
            except queue.Full:
                try:
                    self.output.get_nowait()  # drop the oldest
                except queue.Empty:
                    pass
                self.output.put_nowait(line)
        elif _B_BREAKPOINT in msg:
            # bg3se paused a context; resume it at once (DbgContinue{context, action=CONTINUE}).
            context = _first(_fields(_first(msg, _B_BREAKPOINT)), 3, 0)
            body = _uint_field(1, context) if context else b""
            try:
                self._send(_uint_field(_D_SEQ, next(self._seq)) + _bytes_field(_D_CONTINUE, body))
            except OSError:
                pass
        # Everything else (context updates, mod lists, debuggerReady) is not ours to act on.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bg3ai.luadebug",
                                     description="Evaluate Lua in the running game over the LuaDebug protocol.")
    parser.add_argument("code", help='Lua to run, e.g. "return 1+1" ("-" reads stdin)')
    parser.add_argument("--server", action="store_true",
                        help="run in the server context (needs a loaded save); default is the client context")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--output", action="store_true", help="also print debug output received meanwhile")
    args = parser.parse_args(argv)

    code = sys.stdin.read() if args.code == "-" else args.code
    try:
        with LuaDebugClient(args.host, args.port) as client:
            result = client.evaluate(code, context="server" if args.server else "client", timeout=args.timeout)
            if args.output:
                for line in client.drain_output():
                    print(f"[{line.severity}] {line.message}", file=sys.stderr)
    except LuaDebugError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, TimeoutError) as exc:
        print(f"connection: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
