#!/usr/bin/env python3
"""Drive the new-game flow (or a level up) step by step over LuaDebug, printing every step's result.

Client-context ops from mod/.../Client/Progression.lua:
    ui.new_game {difficulty}         main menu -> New Game -> difficulty -> character creation (job)
    ui.cc_options {}                 what character creation offers
    ui.cc_apply {plan, finish}       apply a plan, then FinishCreating (job)
    ui.level_up_options {uuid}       / ui.level_up {uuid, plan} (job)

Usage (from the repo root, with the game running and bg3le's debug server up):
    python3 tools/dev/test_new_game.py                          # whole flow, default plan, Balanced
    python3 tools/dev/test_new_game.py --step                   # pause for Enter before each step
    python3 tools/dev/test_new_game.py --from cc                # character creation already open
    python3 tools/dev/test_new_game.py --from cc --no-finish    # apply but don't press "Venture forth"
    python3 tools/dev/test_new_game.py --only options           # just print ui.cc_options
    python3 tools/dev/test_new_game.py --plan '{"origin":"Shadowheart","abilities":"recommended"}'
    python3 tools/dev/test_new_game.py --level-up <uuid> [--plan '{"feat":"Alert"}'] [--no-finish]

Exit status: 0 when every step succeeded, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bridge" / "src"))
from bg3ai.luadebug import LuaDebugClient, LuaDebugError  # noqa: E402

DEFAULT_PLAN = {
    # A simple, robust first character: custom human fighter, soldier, game-recommended abilities.
    "race": "Human",
    "class": "Fighter",
    "background": "Soldier",
    "abilities": "recommended",
    "name": "Claude",  # bg3le cannot write CharacterName: cc_apply reports name_pending; type it with the shim
}


class Driver:
    def __init__(self, port: int, step: bool, raw: bool):
        self.lua = LuaDebugClient(port=port)
        self.lua.connect()
        self.step = step
        self.raw = raw
        self.n = 0
        self.ok = True

    def rpc(self, op: str, args: dict | None = None, timeout: float = 15.0):
        req = json.dumps({"op": op, "args": args or {}})
        if "]==]" in req:
            raise ValueError("request contains the Lua long-string terminator")
        reply = self.lua.evaluate(f"return Mods.BG3AI.BG3AI.rpc([==[{req}]==])", context="client", timeout=timeout)
        msg = json.loads(reply)
        if not msg.get("ok"):
            raise RuntimeError(msg.get("error"))
        return msg["result"]

    def header(self, title: str) -> None:
        self.n += 1
        print(f"\n=== step {self.n}: {title}")
        if self.step:
            input("    [Enter] to run ")

    def show(self, value) -> None:
        text = json.dumps(value, indent=None if self.raw else 1, ensure_ascii=False)
        print(text if len(text) < 6000 else text[:6000] + " ...(truncated)")

    def run(self, title: str, op: str, args: dict | None = None):
        self.header(f"{op} {json.dumps(args or {}, ensure_ascii=False)}  # {title}")
        try:
            res = self.rpc(op, args)
        except (RuntimeError, LuaDebugError, TimeoutError) as exc:
            print(f"    FAILED: {exc}")
            self.ok = False
            return None
        self.show(res)
        return res

    def job(self, title: str, op: str, args: dict, timeout: float):
        """Start a job op and poll it, printing progress as the stage changes."""
        res = self.run(title, op, args)
        if not res or "job" not in res:
            return res
        jid = res["job"]
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                st = self.rpc("job.poll", {"id": jid})
            except (RuntimeError, LuaDebugError, TimeoutError, ConnectionError) as exc:
                # Loading screens can stall the client thread past bg3le's 10 s; keep polling.
                print(f"    (poll: {exc})")
                if not self.lua.connected:
                    self.lua.connect()
                continue
            progress = json.dumps(st.get("result"), ensure_ascii=False)[:300]
            if progress != last:
                print(f"    .. {st['status']} {st['elapsed_ms']} ms {progress}")
                last = progress
            if st["status"] != "running":
                print(f"    -> {st['status']}" + (f": {st['error']}" if st.get("error") else ""))
                self.show(st.get("result"))
                if st["status"] != "done":
                    self.ok = False
                return st
        print(f"    -> still running after {timeout} s")
        self.ok = False
        return None


def summarize_cc(opts: dict) -> None:
    """One line per category, selected entries marked with *."""
    def line(key):
        items = opts.get(key) or []
        if not items:
            return
        names = [("*" if i.get("sel") else "") + str(i.get("name") or i.get("id") or i["i"]) + ("(off)" if i.get("off") else "")
                 for i in items]
        print(f"    {key:12s} {', '.join(names)}")
    print(f"    step={opts.get('step')} complete={opts.get('complete')} name={opts.get('name')}")
    for key in ("origins", "races", "subraces", "classes", "subclasses", "deities", "backgrounds"):
        line(key)
    ab = opts.get("abilities") or {}
    print(f"    abilities    points_left={ab.get('points_left')} "
          + " ".join(f"{s.get('a')}={s.get('v')}" for s in ab.get("scores") or []))
    for k, g in (opts.get("skills") or {}).items():
        print(f"    skills.{k:9s} {g.get('picked')} max={g.get('max')} can_pick={g.get('can_pick')}")
    for s in opts.get("spells") or []:
        print(f"    spells       {s.get('title')}: picked={s.get('picked')} available={len(s.get('available') or [])}")
    for p in opts.get("passives") or []:
        print(f"    passives     {p.get('title')}: picked={p.get('picked')} options={p.get('options')}")
    if opts.get("validity"):
        print(f"    validity     {opts['validity']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--difficulty", default="Balanced")
    ap.add_argument("--plan", help="JSON plan for ui.cc_apply / ui.level_up (default: a human fighter)")
    ap.add_argument("--from", dest="start", choices=["menu", "cc"], default="menu",
                    help="menu: start from the main menu (default); cc: character creation is already open")
    ap.add_argument("--only", choices=["options"], help="only print the options of the open page")
    ap.add_argument("--no-finish", action="store_true", help="apply the plan but do not confirm")
    ap.add_argument("--level-up", metavar="UUID", help="test level up for this party member instead")
    ap.add_argument("--step", action="store_true", help="pause before each step")
    ap.add_argument("--raw", action="store_true", help="print JSON on one line")
    ap.add_argument("--port", type=int, default=9998)
    args = ap.parse_args()
    plan = json.loads(args.plan) if args.plan else None

    d = Driver(args.port, args.step, args.raw)
    d.run("client context alive", "sys.ping")
    d.run("what is on screen", "ui.widgets")

    if args.level_up:
        opts = d.run("before opening", "ui.level_up_options", {"uuid": args.level_up})
        if args.only:
            return 0 if d.ok else 1
        if opts and not opts.get("open"):
            d.run("open the level-up page", "ui.level_up_open", {"uuid": args.level_up})
            time.sleep(3)
            d.run("level-up page", "ui.level_up_options", {"uuid": args.level_up})
        d.job("apply the plan", "ui.level_up",
              {"uuid": args.level_up, "plan": plan or {}, "finish": not args.no_finish}, timeout=120)
        return 0 if d.ok else 1

    if args.only == "options":
        d.job("read every tab's labels", "ui.cc_scan", {}, timeout=40)
        opts = d.run("character creation options", "ui.cc_options")
        if opts:
            summarize_cc(opts)
        return 0 if d.ok else 1

    if args.start == "menu":
        st = d.job("new game up to character creation", "ui.new_game", {"difficulty": args.difficulty}, timeout=330)
        if not st or st.get("status") != "done":
            return 1

    # Labels are only readable from rendered list items, one tab at a time: show every tab once first.
    d.job("read every tab's labels", "ui.cc_scan", {}, timeout=40)
    opts = d.run("character creation options", "ui.cc_options")
    if opts:
        summarize_cc(opts)
    else:
        return 1

    d.job("apply the plan", "ui.cc_apply", {"plan": plan or DEFAULT_PLAN, "finish": not args.no_finish}, timeout=150)
    after = d.run("character creation after applying", "ui.cc_options") if args.no_finish else None
    if after:
        summarize_cc(after)
    if not args.no_finish:
        for _ in range(6):
            time.sleep(5)
            try:
                print("    pages:", [w["n"] for w in d.rpc("ui.widgets") if w.get("vis")])
            except Exception as exc:  # loading screens stall evals; keep going
                print(f"    (widgets: {exc})")
    print("\nALL STEPS OK" if d.ok else "\nSOME STEPS FAILED")
    return 0 if d.ok else 1


if __name__ == "__main__":
    sys.exit(main())
