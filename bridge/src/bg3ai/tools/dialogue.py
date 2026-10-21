"""Dialogue and roll tools: choose_dialogue, roll_check, react.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..game import GameError, JobResult
from . import core, plumbing, reporting, world_tools

# ---------------------------------------------------------------------------
# act: dialogue and rolls
# ---------------------------------------------------------------------------

def _visible_answers() -> list:
    u = core.game.try_rpc("ui.state", context="client") or {}
    dd = u.get("dialogue") if isinstance(u, dict) else None
    return [o for o in (dd or {}).get("options") or [] if isinstance(o, dict)]


def _choose_text(option: int, want: str, rows: list | None = None) -> JobResult:
    """Choose the answer that reads `want` (shown to the agent as `option`): its row on screen now (rows: the visible
    answers just read), or wheeled into view (down, then up) like a player does; the mod checks the pressed row still
    reads it."""
    from .. import clicker
    key = clicker.answer_key(want)
    i = next((o["i"] for o in (_visible_answers() if rows is None else rows) if isinstance(o, dict)
              and isinstance(o.get("i"), int) and clicker.answer_key(o.get("text")) == key), None)
    if i is None:
        inp = world_tools._inp()
        if inp is None:
            raise GameError(f"option {option} ('{want}') is outside the visible rows; scrolling the list needs the "
                            "input shim")
        i, why = clicker.scroll_to_answer(core.game, inp, _visible_answers, want)
        if i is None:
            i, why = clicker.scroll_to_answer(core.game, inp, _visible_answers, want, up=True)
        if i is None:
            raise GameError(f"option {option} ('{want}') is not in the answer list now ({why}): call get_dialogue "
                            "and choose again")
    return core.game.run_job("ui.dialogue_choose", {"i": i, "text": want}, context="client", wait_s=20)


def _choose_answer(option: int) -> JobResult:
    """ui.dialogue_choose for answer number `option` as the agent was shown it. S081 Karlach (10 hand calls,
    ~1.5 min): choose_dialogue(7) `no option 7 (6 shown)` while 7 were listed: the listing is the whole list read
    by scrolling (numbered 1..N in display order), the mod numbers its visible window. So an answer from the last
    listing of this node is chosen by its text (_choose_text). Otherwise: the number as the mod has it; an option
    outside the rendered rows (needs_scroll, audit UI-4) is wheeled into view and chosen by its text."""
    texts, node = reporting._listed.get("texts") or {}, reporting._listed.get("node")
    want = texts.get(option)
    reporting._listed.clear()   # the choice moves the dialogue on: the next listing numbers the next node
    if want:
        from .. import clicker
        u = core.game.try_rpc("ui.state", context="client") or {}
        d = u.get("dialogue") if isinstance(u, dict) else None
        rows = [o for o in (d or {}).get("options") or [] if isinstance(o, dict)]
        listed = {clicker.answer_key(t) for t in texts.values()}
        # the same menu: same line, and the rows on screen (any window of it) are among the listed answers
        if d and reporting._node(d) == node and any(clicker.answer_key(o.get("text")) in listed for o in rows):
            return _choose_text(option, want, rows)
    try:
        return core.game.run_job("ui.dialogue_choose", {"i": option}, context="client", wait_s=20)
    except GameError as e:
        if "needs_scroll" not in str(e):
            raise
        first = e

    def read() -> list:
        u = core.game.try_rpc("ui.state", context="client") or {}
        dd = u.get("dialogue") if isinstance(u, dict) else None
        return [o for o in (dd or {}).get("options") or [] if isinstance(o, dict)]

    full = reporting._complete_answers(core.game.try_rpc("ui.state", context="client") or {})
    opts = ((full.get("dialogue") or {}) if isinstance(full, dict) else {}).get("options") or []
    want = next((o.get("text") for o in opts if isinstance(o, dict) and o.get("i") == option and o.get("text")), None)
    if not want:
        raise GameError(f"{first}; its text could not be read by scrolling: call get_dialogue and choose again")
    inp = world_tools._inp()
    if inp is None:
        raise GameError(f"{first}; scrolling the list needs the input shim")
    from .. import clicker
    shown = [o["i"] for o in read() if isinstance(o.get("i"), int)]
    i, why = clicker.scroll_to_answer(core.game, inp, read, want, up=bool(shown) and option < min(shown))
    if i is None:
        raise GameError(f"option {option} ('{want}') is below the visible rows and could not be scrolled into view: {why}")
    return core.game.run_job("ui.dialogue_choose", {"i": i, "text": want}, context="client", wait_s=20)


@core.mcp.tool()
@reporting._guard
def choose_dialogue(option: int) -> str:
    """Pick dialogue answer number `option` (as numbered in the last DIALOGUE shown). Then clicks through any filler that follows (Continue, a single non-committing line) and returns the next real choice, roll, or the end. Never picks a real answer for you."""
    t0 = time.monotonic()
    r = _choose_answer(option)
    if macro_tools.AUTO_DIALOGUE and r.status == "done" and not reporting._batch["active"]:
        # Fast reflexes: click through the filler that follows the answer here and show the next real
        # choice, instead of a dialogue_advance / get_dialogue round trip per line (S002/S003).
        n, lines, stop = macro_tools._dialogue_autopilot(open_s=0.3)
        body = "\n".join([plumbing._job_text("choose_dialogue", r)]
                         + ([f"then auto-advanced {n} filler line(s):"] if n else []) + lines
                         + ([f"STOPPED: {stop}"] if stop else []))
        return reporting._finish("choose_dialogue", {"option": option}, body, t0)
    time.sleep(0.3)
    ui = core.game.try_rpc("ui.state", context="client") or {}
    body = plumbing._job_text("choose_dialogue", r) + "\n" + "\n".join(reporting._render_ui(ui))
    return reporting._finish("choose_dialogue", {"option": option}, body, t0)


def _roll_line(roll: dict, label: str = "roll") -> str:
    """One plain line for a roll result: 'roll: Persuasion (Charisma Check) DC 16: d20 4 + 7 = 11 → FAILED'.
    The raw dict hid the outcome (success was only a missing key) and FinalResult is the natural d20, not the
    total (S024: result=13 bonus=6 vs DC 18 = success)."""
    what = " ".join(x for x in (str(roll.get("skill") or ""), f"({roll['check']})" if roll.get("check") else "") if x)
    head = f"{label}: {what}" if what else label
    if roll.get("dc") is not None:
        head += f" DC {roll['dc']}"
    die, bonus = roll.get("result"), roll.get("bonus")
    if isinstance(die, (int, float)):
        if isinstance(bonus, (int, float)):
            sign = "+" if bonus >= 0 else "-"
            nums = f"d20 {die:g} {sign} {abs(bonus):g} = {die + bonus:g}"
        elif isinstance(bonus, list) and len(bonus) == 2:
            nums = f"d20 {die:g} + {bonus[0]}..{bonus[1]}"
        else:
            nums = f"d20 {die:g}"
        head += f": {nums}"
    if roll.get("advantage"):
        head += f" ({roll['advantage']})"
    head += " → SUCCESS" if roll.get("success") else " → FAILED"
    if roll.get("inspiration") is not None:
        head += f" (inspiration {roll['inspiration']})"
    return head


def _roll_step(op_args: dict, label: str, wait_s: float) -> tuple[str, dict | None]:
    """One ui.roll step; a result that carries a roll is shown as _roll_line instead of the raw dict."""
    text = plumbing._act("roll_check", "ui.roll", op_args, context="client", wait_s=wait_s)
    res = reporting._batch.get("result") if isinstance(reporting._batch.get("result"), dict) else {}
    roll = res.get("roll") if isinstance(res.get("roll"), dict) else None
    if res.get("phases"):
        core.METRICS.write({"type": "roll_phases", "t": round(time.time(), 3), "phases": res["phases"]})
    if reporting._batch.get("last") == "done" and roll and roll.get("phase") == "result":
        line = _roll_line(roll, label)
        if res.get("retriggered"):
            line += f" (start re-sent {res['retriggered']}x)"
        text = re.sub(r"\Aroll_check: done — [^\n]*", lambda _m: line, text, count=1)
    return text, roll


@core.mcp.tool()
@reporting._guard
def roll_check(boosts: str = "", reroll_on_fail: bool = False) -> str:
    """Resolve the skill-check roll on screen: optionally add boosts shown on the roll screen (comma list, e.g. "Guidance"), roll, and continue."""
    args: dict[str, Any] = {"reroll": reroll_on_fail}
    if boosts:
        args["boosts"] = [b.strip() for b in boosts.split(",") if b.strip()]
    try:
        first, roll = _roll_step(args, "roll", 20)
    except GameError as e:
        # S035 19:24:20: roll_check(boosts="Guidance") on a roll without Guidance → FAILED, and the agent rolled bare
        # in a second call. A boost the roll doesn't offer can't be added by hand either: roll without it, say so.
        if not args.get("boosts") or "no boost '" not in str(e):
            raise
        args.pop("boosts")
        first, roll = _roll_step(args, "roll", 20)
        first = f"(rolled without {boosts}: {str(e).split(' on this roll ', 1)[-1].strip('()')})\n" + first
    # ui.roll stops at the result screen (so a reroll can be chosen); "roll, and continue" means one call:
    # press Continue (or reroll a failure first) here, not in a second roll_check round trip (live S002).
    outs = [first]
    if reporting._batch.get("last") != "done" or not isinstance(roll, dict) or roll.get("phase") != "result":
        return first
    if reroll_on_fail and not roll.get("success"):
        if roll.get("inspiration") == 0:
            outs.append("(no reroll: no Inspiration points left)")
        else:
            again, _ = _roll_step({"reroll": True}, "reroll (1 Inspiration)", 25)
            res = reporting._batch.get("result") if isinstance(reporting._batch.get("result"), dict) else {}
            if res.get("reroll_ignored"):
                again = "(no reroll: the game kept the first result; the Inspiration reroll was not offered)"
            outs.append(again)
            if reporting._batch.get("last") != "done":
                return "\n".join(outs)
    outs.append(plumbing._act("roll_check", "ui.roll", {}, context="client", wait_s=30))
    outs = [re.sub(r"\n⏱[^\n]*$", "", o) for o in outs[:-1]] + outs[-1:]
    return "\n".join(outs)


@core.mcp.tool()
@reporting._guard
def react(use: bool, which: str = "") -> str:
    """Answer a reaction prompt (Shield, Counterspell, Opportunity Attack...). use=true to react. which: the option's number in REACTION options, or its name ("Shield": the first row, the lowest level; "Shield 3" or "Shield III": that slot level). A prompt with one row per level is answered by a click on that row; Do Not React has never closed one: set_reactions {"Shield": "never"} beforehand stops them."""
    args: dict[str, Any] = {"use": use}
    if which:
        args["which"] = which
        if use and not str(which).strip().isdigit():
            ui = core.game.try_rpc("ui.state", context="client") or {}
            i = _reaction_index(ui.get("reaction") if isinstance(ui, dict) else None, str(which))
            if isinstance(i, str):
                return reporting._finish("react", args, "react: FAILED — " + i, time.monotonic())
            args["which"] = i
    t0 = time.monotonic()
    out = plumbing._act("react", "ui.react", args, context="client", wait_s=15)
    res = reporting._batch.get("result")
    if use and isinstance(res, dict) and res.get("needs_click"):
        return _react_click(args, int(res["needs_click"]), t0)
    if not use and "TIMEOUT" in out.split("\n", 1)[0]:
        ui = core.game.try_rpc("ui.state", context="client") or {}
        r = ui.get("reaction") if isinstance(ui, dict) else None
        if isinstance(r, dict) and len(r.get("options") or []) > 1:
            # S092 08:46:43 / 08:47:47 (two hand clicks) and 08:55:23 (the command): Do Not React left Gale's
            # multi-level Shield prompt up every time; only a row click answered it
            out += ("\nthe prompt is still up: Do Not React has never closed a prompt with one row per level (S092: by"
                    " command and by hand); answer it with a row, react(use=true, which=<number or level>), and"
                    " set_reactions {\"<name>\": \"never\"} stops these prompts")
    return out


def _react_click(args: dict, which: int, t0: float) -> str:
    """ui.react said this prompt answers only to a click on its row (one row per spell level): click it with the
    mouse, hover-checked (reactclick)."""
    from .. import reactclick

    inp = world_tools._inp()
    if inp is None:
        return reporting._finish("react", args, "react: FAILED — this prompt answers only to a click on its row, and the input"
                       " shim is not available", t0)
    c = reactclick.click_row(lambda o, a: core.game.rpc(o, a, context="client"), inp, which)
    if c.get("closed") or c.get("gone"):
        text = "react: done — " + reactclick.describe(c)
    elif c.get("shown"):
        text = "react: clicked — " + reactclick.describe(c) + ": look before answering again (EVENTS say if it cast)"
    else:
        text = "react: FAILED — " + reactclick.describe(c)
    return reporting._finish("react", args, text, t0)


_ROMAN = re.compile(r"^(?:I|II|III|IV|V|VI|VII|VIII|IX)$")
_ROMAN_N = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9}


def _reaction_index(r: dict | None, name: str) -> int | str:
    """react(which="Shield"): the option's number. S034 19:03:14: "no reaction option Shield (3 shown)" — ui.react
    takes a number and the options carried no names (decision=Undecided i=1..3); the prompt text named them
    ("… | 76/81 | Shield | II | Shield | II | III | Shield | III | Do Not React"). Named options first, else the
    reaction names in the prompt text in display order (slot-level numerals and HP left out). str = why not."""
    if not isinstance(r, dict):
        return "no reaction prompt on screen"
    opts = [o for o in r.get("options") or [] if isinstance(o, dict)]
    want = name.strip().lower()
    # A25: the rows carry their spell level (UIState.reactionRows; S088 Gale's Shield at levels I-VI): "Shield 3",
    # "Shield III", "Shield L3", "Shield level 3" pick that row; a plain name the first (lowest) one
    lv = re.fullmatch(r"(.*?)\s*\(?\s*(?:l|lvl|level)?\s*(\d|i{1,3}|iv|vi{0,3}|ix)\s*\)?", want)
    level = None
    if lv and lv.group(1) and any(want.startswith(str(o.get("name") or "?").lower()) for o in opts):
        want, level = lv.group(1).strip(), _ROMAN_N.get(lv.group(2).upper()) or int(lv.group(2))
    for o in opts:
        if want and want in str(o.get("name") or "").lower() and level in (None, o.get("level") or 1):
            return int(o.get("i") or 1)
    if level is not None and any(want in str(o.get("name") or "").lower() for o in opts):
        shown = ", ".join(f"{o.get('i')}={o.get('name')} (level {o.get('level') or 1})" for o in opts)
        return (f"no reaction option {name!r} on this prompt ({shown}); answer with its number, or the name alone for "
                "the first")
    toks = [t.strip() for t in str(r.get("prompt") or "").split("|")]
    if any("choose how to react" in t.lower() for t in toks):
        toks = toks[next(k for k, t in enumerate(toks) if "choose how to react" in t.lower()) + 1:]
    # S088 06:16:18: Gale's "+1/1" (temporary HP on the portrait) read as a 7th name for 6 rows
    names = [t for t in toks if t and not _ROMAN.match(t) and not re.fullmatch(r"[+\-]?[\d/ ]+", t)
             and t.lower() != "do not react"]
    hits = [k for k, t in enumerate(names) if want and want in t.lower()]
    if hits and (len(names) == len(opts) or len(set(n.lower() for n in names)) == 1):
        return hits[0] + 1
    shown = ", ".join(f"{k + 1}={t}" for k, t in enumerate(names)) or "no names shown"
    return (f"no reaction option {name!r} on this prompt ({len(opts)} options: {shown}); "
            "answer with its number, react(use=true, which=\"1\"), or react(use=false)")


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    combat, macro_tools,
)
