"""Shared bits of the bench-v0.10 live-check scripts (ops/briefs/v010-live.md in the website repo).

Every script calls the bridge tools in-process against the running game (tools/dev/server_view S, as smoke.py and
the v09 probe drivers did), never a model. Every call is appended to <repo>/runtime/probes/v010/calls.jsonl
(label, tool, args, t0, seconds, text) and printed. Run from the repo root of the checkout the game runs from:

    uv run --project bridge python tools/dev/live_v010/<script>.py ...
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "bridge" / "src"))
sys.path.insert(0, str(REPO / "tools" / "dev"))
OUT = Path(os.environ.get("V010_OUT") or REPO / "runtime" / "probes" / "v010")
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "calls.jsonl"


def server():
    """tools/dev/server_view's S (imported late: bg3ai's import reads the env)."""
    from server_view import S

    return S


def text_of(out: Any) -> str:
    if isinstance(out, str):
        return out
    if hasattr(out, "data") and isinstance(out.data, (bytes, bytearray)):
        return f"<image {len(out.data)} bytes>"
    return json.dumps(out, default=str)


def rec(label: str, tool: str, args: Any, out: Any, t0: float) -> str:
    dt = time.time() - t0
    text = text_of(out)
    print(f"=== {label} {tool} {json.dumps(args, default=str) if args else ''} ({dt:.2f} s)\n{text}\n", flush=True)
    with LOG.open("a") as f:
        f.write(
            json.dumps(
                {"label": label, "tool": tool, "args": args, "t0": round(t0, 2), "seconds": round(dt, 3), "text": text},
                default=str,
            )
            + "\n"
        )
    return text


def call(label: str, tool: str, args: dict | None = None) -> Any:
    """One bridge tool call (S.<tool>(**args)), logged; an exception is logged and returned as text."""
    t0 = time.time()
    try:
        out = getattr(server(), tool)(**(args or {}))
    except Exception as e:
        out = f"EXCEPTION {type(e).__name__}: {e}"
    rec(label, tool, args, out, t0)
    return out


_LD = None


def lua(code: str, context: str = "server", label: str = "", timeout: float = 15.0) -> str:
    """Lua through the Script Extender debugger (bg3ai.luadebug), logged when label is given."""
    global _LD
    from bg3ai import luadebug

    if _LD is None:
        _LD = luadebug.LuaDebugClient()
        _LD.connect()
    t0 = time.time()
    try:
        out = _LD.evaluate(code, context=context, timeout=timeout)
    except Exception as e:
        out = f"EXCEPTION {type(e).__name__}: {e}"
    if label:
        rec(label, f"lua:{context}", code if len(code) < 200 else code[:200] + "...", out, t0)
    return out


def lua_json(code: str, context: str = "server", label: str = "") -> Any:
    """Lua that returns Ext.Json.Stringify(...): parsed, or {"error": text}."""
    out = lua(code, context, label)
    try:
        return json.loads(out)
    except (TypeError, ValueError):
        return {"error": out}


def save_image(out: Any, name: str) -> str:
    """A screenshot tool result (mcp Image) written to OUT/<name>.jpg; the path, or why not."""
    data = getattr(out, "data", None)
    if not isinstance(data, (bytes, bytearray)):
        return f"no image ({text_of(out)[:120]})"
    p = OUT / f"{name}.jpg"
    p.write_bytes(data)
    return str(p)
