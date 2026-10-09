"""Smoke-test the bg3 MCP tools against the running game (read-only tools only).

Prints, per tool: latency, output size in characters, and a rough token estimate (chars/4), followed by
the first lines of output. It's the quick check for both correctness and efficiency after a change.

    uv run --project bridge python tools/dev/smoke.py [--full] [tool ...]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bridge" / "src"))

from bg3ai import server as S  # noqa: E402

READ_ONLY = [
    ("get_status", {}),
    ("get_party", {}),
    ("get_nearby", {}),
    ("get_actions", {}),
    ("get_character", {}),
    ("get_inventory", {"filter": "equipped"}),
    ("get_combat_state", {}),
    ("get_journal", {}),
    ("get_map", {}),
    ("get_frontier", {}),
    ("objectives", {}),  # raw nav.markers through the OBJECTIVE renderer (see _objectives)
    ("get_dialogue", {}),
    ("ui_dump", {}),
    ("list_saves", {"limit": 3}),
    ("session_clock", {}),
]


def _objectives() -> str:
    """nav.markers straight from the mod, rendered as the agent sees it in get_map."""
    res = S.game.rpc("nav.markers", {})
    if not isinstance(res, dict):
        return f"FAILED: nav.markers returned {res!r}"
    head = f"nav.markers: {len(res.get('markers') or [])} marker(s), {res.get('ms')} ms"
    if res.get("error"):
        return f"FAILED: {head}, error={res['error']}"
    return "\n".join([head] + S.render.render_objectives(res.get("markers")))


def main() -> int:
    full = "--full" in sys.argv
    wanted = [a for a in sys.argv[1:] if not a.startswith("--")]
    total_ms = 0.0
    failures = 0
    for name, kwargs in READ_ONLY:
        if wanted and name not in wanted:
            continue
        fn = _objectives if name == "objectives" else getattr(S, name)
        t = time.perf_counter()
        try:
            out = fn(**kwargs)
        except Exception as e:  # tools should never raise; flag it loudly
            out = f"EXCEPTION {type(e).__name__}: {e}"
        ms = (time.perf_counter() - t) * 1000
        total_ms += ms
        text = out if isinstance(out, str) else repr(out)
        bad = text.startswith(("FAILED", "EXCEPTION", "NOT READY"))
        failures += bad
        print(f"{'✗' if bad else '✓'} {name:18s} {ms:7.0f} ms  {len(text):6d} chars  ~{len(text) // 4} tok")
        lines = text.splitlines()
        for line in lines if full else lines[:4]:
            print("     " + line[:160])
    print(f"total {total_ms:.0f} ms, {failures} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
