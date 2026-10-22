"""Dialogue choices from one play process's event log (bg3harness/events.py) as facts v1 `choice` events
(packages/harness/schemas/facts.v1.schema.json): the scene and the numbered answers the agent was last shown, the
number it picked, and the reason it stated (its text and thinking since the tool result before the call).

    python3 -m unittest runner/test_choices.py
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .clients import Event

REASON_MAX = 2000
_OPTION = re.compile(r"^  (\d+)\. (.*?)(?:  ← .*)?$")


@dataclass
class Shown:
    """The DIALOGUE block a tool result rendered: the line shown (speaker: text) and the numbered answers."""

    scene: str
    options: dict[int, str] = field(default_factory=lambda: {})


def shown(text: str) -> Shown | None:
    """The DIALOGUE block of a tool result, else None."""
    got: Shown | None = None
    for ln in text.splitlines():
        if ln.startswith("DIALOGUE "):
            got = Shown(ln[len("DIALOGUE ") :].strip())
        elif got is not None and (m := _OPTION.match(ln)):
            got.options[int(m.group(1))] = m.group(2).strip()
        elif got is not None and got.options:
            break
    return got


def _option(args: dict[str, Any] | None) -> int | None:  # clients.Event.args is a bare dict
    try:
        return int((args or {})["option"])
    except (KeyError, TypeError, ValueError):
        return None


def choices(evs: Iterable[Event]) -> list[dict[str, Any]]:
    """One {t, ev: "choice", scene, options, picked, [picked_text], [reason]} per choose_dialogue call."""
    out: list[dict[str, Any]] = []
    last = Shown("")
    said: list[str] = []
    for e in evs:
        if e.kind == "tool_result":
            last = shown(e.text) or last
            said = []
        elif e.kind in ("text", "thinking") and e.text.strip():
            said.append(e.text.strip())
        elif e.kind == "tool_call" and e.tool == "choose_dialogue":
            picked = _option(e.args)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            if picked is None:
                continue
            rec: dict[str, Any] = {
                "t": e.t,
                "ev": "choice",
                "scene": last.scene,
                "options": [{"i": n, "text": t} for n, t in sorted(last.options.items())],
                "picked": picked,
            }
            if picked in last.options:
                rec["picked_text"] = last.options[picked]
            if said:
                rec["reason"] = "\n".join(said)[:REASON_MAX]
            out.append(rec)
            said = []
    return out
