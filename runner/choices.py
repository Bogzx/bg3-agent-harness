"""Dialogue choices from one play process's event log (runner/events.py) as facts v1 `choice` events
(packages/harness/schemas/facts.v1.schema.json): the scene and the numbered answers the agent was last shown, the
number it picked, and the reason it stated (its text and thinking since the tool result before the call).

    python3 -m unittest runner/test_choices.py
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from clients import Event

REASON_MAX = 2000
_OPTION = re.compile(r"^  (\d+)\. (.*?)(?:  ← .*)?$")


def shown(text: str) -> dict | None:
    """{scene, options: {n: text}} from a tool result that rendered a DIALOGUE block, else None."""
    scene, options = None, {}
    for ln in text.splitlines():
        if ln.startswith("DIALOGUE "):
            scene, options = ln[len("DIALOGUE "):].strip(), {}
        elif scene is not None and (m := _OPTION.match(ln)):
            options[int(m.group(1))] = m.group(2).strip()
        elif scene is not None and options:
            break
    return {"scene": scene, "options": options} if scene is not None else None


def choices(evs: Iterable[Event]) -> list[dict]:
    """One {t, ev: "choice", scene, options, picked, [picked_text], [reason]} per choose_dialogue call."""
    out: list[dict] = []
    last: dict | None = None
    said: list[str] = []
    for e in evs:
        if e.kind == "tool_result":
            last = shown(e.text) or last
            said = []
        elif e.kind in ("text", "thinking") and e.text.strip():
            said.append(e.text.strip())
        elif e.kind == "tool_call" and e.tool == "choose_dialogue":
            try:
                picked = int((e.args or {}).get("option"))
            except (TypeError, ValueError):
                continue
            rec = {"t": e.t, "ev": "choice", "scene": (last or {}).get("scene") or "",
                   "options": [{"i": n, "text": t} for n, t in sorted((last or {}).get("options", {}).items())],
                   "picked": picked}
            text = (last or {}).get("options", {}).get(picked)
            if text is not None:
                rec["picked_text"] = text
            if said:
                rec["reason"] = "\n".join(said)[:REASON_MAX]
            out.append(rec)
            said = []
    return out
