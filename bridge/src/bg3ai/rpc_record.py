"""Fixture recorder: BG3AI_RPC_RECORD=<file> appends every Lua eval the bridge sends and the game's answer as JSONL.

One line per eval (Game.eval is the only place the bridge talks Lua to the game). The two RPC shapes the bridge sends
are decoded into their mod ops: Game.rpc's `return Mods.BG3AI.BG3AI.rpc([=[{"op":..,"args":..}]=])` and Game.multi's
`local R=Mods.BG3AI.BG3AI.rpc return '['..R([=[..]=])..','..R([=[..]=])..']'`. A line:

    {"v": 1, "t": 1.234, "ms": 61.2, "ctx": "server", "calls": [{"op": "state.status", "args": {}}],
     "replies": [{"ok": true, "result": {...}}]}

`t` is seconds since the recorder started, `ms` the eval's round trip. A Lua error gives `"error": "<message>"` and
no replies; an eval that is not an RPC (e.g. the GetGameState probe) keeps its `code` and the raw `reply` string.
Every string is scrubbed of absolute paths (home, saves, the repo) before it is written, so fixtures can be committed.
The fake mod (packages/harness/src/bg3harness/testing) replays these files by request.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ENV = "BG3AI_RPC_RECORD"
VERSION = 1

_SINGLE = re.compile(r"^return Mods\.BG3AI\.BG3AI\.rpc\(\[(=*)\[(.*)\]\1\]\)$", re.S)
_MULTI_HEAD = "local R=Mods.BG3AI.BG3AI.rpc return '['.."
_MULTI_PART = re.compile(r"R\(\[(=*)\[(.*?)\]\1\]\)", re.S)
# Absolute paths: POSIX ones under the usual roots, ~/..., and Windows drive paths (bg3se under Proton reports C:\...).
# A folder may hold spaces and apostrophes ("Larian Studios", "Baldur's Gate 3") when another separator follows it; the
# last component may not (a sentence goes on after it).
_SEG = r"[^\s\"'<>|:;,)\]}\\/]+"
_INNER = r"[^\"<>|:;,)\]}\\/\n]+?(?=[\\/])"
_ABS_PATH = re.compile(
    r"(?:~|/(?:home|data|tmp|root|mnt|media|run|var|opt|usr|srv|Users|nix|proc)(?=[\\/])|\b[A-Za-z]:(?=[\\/]))"
    rf"(?:[\\/](?:{_INNER}|{_SEG}))+"
)
PATH_MARK = "<path>"


def decode(code: str) -> tuple[list[dict[str, Any]], bool] | None:
    """([{op, args}, ...], is_multi) for an RPC eval, None for any other Lua."""
    code = code.strip()
    m = _SINGLE.match(code)
    if m:
        return [_call(m.group(2))], False
    if code.startswith(_MULTI_HEAD):
        return [_call(p.group(2)) for p in _MULTI_PART.finditer(code)], True
    return None


def _call(payload: str) -> dict[str, Any]:
    req = json.loads(payload)
    return {"op": req.get("op"), "args": req.get("args") or {}}


def scrub(value: Any) -> Any:
    """`value` with every absolute path inside a string replaced by <path>/<last component>."""
    if isinstance(value, str):
        return _ABS_PATH.sub(lambda m: f"{PATH_MARK}/{re.split(r'[\\/]', m.group(0))[-1]}", value)
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {scrub(k): scrub(v) for k, v in value.items()}
    return value


def entry(code: str, context: str, ms: float, t: float, result: str | None, error: str | None) -> dict:
    """One fixture line (before scrubbing)."""
    line: dict[str, Any] = {"v": VERSION, "t": round(t, 3), "ms": round(ms, 1), "ctx": context}
    decoded = decode(code)
    if decoded is None:
        line["code"] = code
    else:
        line["calls"], multi = decoded
        if multi:
            line["multi"] = True
    if error is not None:
        line["error"] = error
        return line
    if decoded is None:
        line["reply"] = result
        return line
    try:
        parsed = json.loads(result or "")
    except json.JSONDecodeError:
        line["reply"] = result  # "nil" (the mod isn't loaded) or something the bridge will refuse anyway
        return line
    line["replies"] = parsed if multi else [parsed]
    return line


class Recorder:
    """Appends fixture lines to one file; safe from the bridge's many RPC threads."""

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.monotonic):
        self.path = Path(path)
        self._clock = clock
        self._t0 = clock()
        self._lock = threading.Lock()

    def now(self) -> float:
        return self._clock()

    def record(
        self, code: str, context: str, t0: float, t1: float, result: str | None = None, error: str | None = None
    ) -> None:
        line = scrub(entry(code, context, (t1 - t0) * 1000.0, t0 - self._t0, result, error))
        text = json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(text)


def from_env(environ: Mapping[str, str] = os.environ) -> Recorder | None:
    """The recorder BG3AI_RPC_RECORD asks for, or None (off by default)."""
    path = environ.get(ENV, "").strip()
    return Recorder(path) if path else None
