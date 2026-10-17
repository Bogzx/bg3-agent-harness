"""prepare_spells: change a caster's prepared spells the player's way, through the spellbook page (A5, 2026-10-17).

Since A4 only prepared spells cast (Guard.known_spell reads SpellBookPrepares; get_actions lists only those), so the
agent needs the player's way to change them:

    act.prepare_plan {char, add, remove}   server: the change checked as the UI checks it (out of combat, a class that
                                           prepares, each spell in the book, the game's limit) or {refused}
    act.select {char}                      the spellbook page shows the selected character
    ui.prepare_spells {name, add, remove}  client: open the page (K), run each spell cell's own command (what a click
                                           on the cell runs), close the page (Client/Spellbook.lua)
    act.prepared {uuids}                   server: the prepared set afterwards; the result is judged by it, not by
                                           what the page said

    from bg3ai import spellprep
    text = spellprep.prepare(game, char, ["Bless"], ["Command"], select=fn)

The status lines (get_status PREPARED, get_party) come from act.prepared too.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

SETTLE_S = 0.4   # the client cell flipped; the server's SpellBookPrepares may follow a frame or two later


def names(v: Any) -> list[str]:
    """A list, or a comma string, of spell names/ids -> clean list."""
    if v is None:
        return []
    items = v if isinstance(v, (list, tuple)) else str(v).split(",")
    return [str(x).strip() for x in items if str(x).strip()]


def _names(rows: Any) -> list[str]:
    return [str(r.get("name") or r.get("id")) for r in rows or [] if isinstance(r, dict)]


def caster_text(c: dict, always: bool = True, named: bool = True) -> str:
    """'Claude 3/4: Bless, Cure Wounds, Shield of Faith; always: Sanctuary' (always=False: '+1 always'; named=False:
    no name)."""
    mx = c.get("max")
    head = f"{c.get('count', len(c.get('prepared') or []))}/{mx if mx is not None else '?'}"
    if named:
        head = f"{c.get('name') or '?'} {head}"
    body = ", ".join(_names(c.get("prepared"))) or "none"
    al = _names(c.get("always"))
    if not al:
        tail = ""
    elif always:
        tail = "; always: " + ", ".join(al)
    else:
        tail = f" +{len(al)} always"
    unread = "" if c.get("read", True) else " (prepared list unreadable: nothing casts)"
    return f"{head}: {body}{tail}{unread}"


def status_line(casters: Any) -> str:
    """get_status: one line for every preparing caster, or '' when there is none."""
    rows = [c for c in casters or [] if isinstance(c, dict)]
    if not rows:
        return ""
    return "PREPARED (only these cast; prepare_spells out of combat changes them): " + " · ".join(
        caster_text(c, always=False) for c in rows)


def _after(game, char: str) -> dict | None:
    got = game.try_rpc("act.prepared", {"uuids": [char]}) or {}
    rows = got.get("casters") if isinstance(got, dict) else None
    return rows[0] if rows else None


def _ids(c: dict | None) -> set[str]:
    return {str(r.get("id")) for r in (c or {}).get("prepared") or [] if isinstance(r, dict)}


def prepare(game, char: str, add: list[str], remove: list[str], select: Callable[[str], str] | None = None) -> str:
    """The whole tool: plan, select, the spellbook page, the server's answer. Returns the result text."""
    plan = game.rpc("act.prepare_plan", {"char": char, "add": add, "remove": remove})
    if not isinstance(plan, dict):
        return f"prepare_spells: FAILED — no plan ({plan!r})"
    if plan.get("refused"):
        return f"prepare_spells: FAILED — {plan['refused']}"
    notes = [str(n) for n in plan.get("notes") or []]
    adds = [{"id": r.get("id"), "name": r.get("name")} for r in plan.get("add") or []]
    rems = [{"id": r.get("id"), "name": r.get("name")} for r in plan.get("remove") or []]
    if not adds and not rems:
        return "\n".join(["prepare_spells: nothing to change" + (f" ({'; '.join(notes)})" if notes else ""),
                          "PREPARED " + caster_text(plan)])
    sel = select(char) if select else ""
    r = game.run_job("ui.prepare_spells", {"name": plan.get("name"), "add": adds, "remove": rems},
                     context="client", wait_s=25)
    res = r.result if isinstance(r.result, dict) else {}
    after = _after(game, char)
    want_in = {a["id"] for a in adds}
    want_out = {x["id"] for x in rems}
    if after is not None and not (want_in <= _ids(after) and not (want_out & _ids(after))):
        time.sleep(SETTLE_S)
        after = _after(game, char) or after
    have = _ids(after)
    ui_why = {str(t.get("name")): t.get("why") for t in res.get("toggled") or [] if isinstance(t, dict)}
    done, failed = [], []
    for x, op in [(a, "prepared") for a in adds] + [(x, "unprepared") for x in rems]:
        ok = (x["id"] in have) if op == "prepared" else (after is not None and x["id"] not in have)
        if ok:
            done.append(f"{op} {x['name']}")
        else:
            why = ui_why.get(str(x["name"])) or (r.error if r.status != "done" else None) or "the spellbook didn't change it"
            failed.append(f"{x['name']} not {op}: {why}")
    head = "prepare_spells: done" if not failed else (
        "prepare_spells: PARTLY done" if done else "prepare_spells: FAILED")
    bits = []
    if done:
        bits.append(", ".join(done))
    if failed:
        bits.append("; ".join(failed))
    if notes:
        bits.append("; ".join(notes))
    lines = [head + " — " + "; ".join(bits)]
    if after is not None:
        lines.append("PREPARED " + caster_text(after))
    else:
        lines.append("PREPARED (could not read the prepared list afterwards)")
    if res.get("before") and res.get("after"):
        b, a = res["before"], res["after"]
        lines.append(f"spellbook page: {res.get('book') or '?'} {b.get('value')}/{b.get('max')} -> "
                     f"{a.get('value')}/{a.get('max')}")
    if sel:
        lines.append(sel)
    return "\n".join(lines)
