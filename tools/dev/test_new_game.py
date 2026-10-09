#!/usr/bin/env python3
"""Drive the new-game flow (or a level up) step by step over LuaDebug, printing every step's result.

Client-context ops from mod/.../Client/Progression.lua:
    (splash screen)                  "Press any key" after a fresh launch: SPACE through the input shim
    ui.new_game {difficulty}         main menu -> New Game -> difficulty -> character creation (job)
    ui.cc_options {}                 what character creation offers
    ui.cc_apply {plan, finish}       apply a plan (job; run here with finish=false)
    ui.cc_name / ui.cc_finish        name the character (bg3ai.naming, shim typing if needed), then
                                     FinishCreating, refused unless the name checks out
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
from bg3ai.input import InputClient, InputError  # noqa: E402
from bg3ai.luadebug import LuaDebugClient, LuaDebugError  # noqa: E402
from bg3ai.naming import NamingError, ensure_name  # noqa: E402

DEFAULT_PLAN = {
    # A simple, robust first character: custom human fighter, soldier, game-recommended abilities.
    "race": "Human",
    "class": "Fighter",
    "background": "Soldier",
    "abilities": "recommended",
    "name": "Claude",  # set and verified by bg3ai.naming before "Venture forth" (saves are owned by this name)
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


def name_character(d: Driver, name: str) -> bool:
    """Type the character's name on the naming page and check the box shows it (bg3ai.naming)."""
    d.header(f"name the character '{name}'")
    try:
        with InputClient() as inp:
            inp.agent(True)
            res = ensure_name(lambda op, a: d.rpc(op, a), inp, name, log=lambda m: print(f"    {m}"))
    except (NamingError, InputError, OSError, RuntimeError, LuaDebugError, TimeoutError) as exc:
        print(f"    FAILED: {exc}")
        d.ok = False
        return False
    d.show(res)
    return True


def pass_splash(rpc, press, timeout: float = 60.0, interval: float = 1.5,
                log=lambda m: None, clock=time.monotonic, sleep=time.sleep) -> int:
    """After a fresh launch the game waits on "Press any key to continue" (page SplashScreen) and
    ui.new_game timed out there. Press SPACE through the input shim (real SDL input) until the page is gone;
    ui.new_game's main_menu stage also injects SPACE through the engine as a second route.
    rpc(op, args) runs a client op; press() sends one key. Returns the number of presses; raises
    TimeoutError if the splash screen is still up after `timeout` s."""
    deadline = clock() + timeout
    presses = 0
    while True:
        try:
            up = any(w.get("n") == "SplashScreen" and w.get("vis") for w in rpc("ui.widgets", {}) or [])
        except (RuntimeError, LuaDebugError, TimeoutError, ConnectionError) as exc:
            log(f"(ui.widgets: {exc})")
            up = None
        if up is False:
            if presses:
                log(f"splash screen passed after {presses} key press(es)")
            return presses
        if clock() > deadline:
            raise TimeoutError(f"splash screen still up after {timeout:.0f} s ({presses} presses)")
        if up:
            press()
            presses += 1
        sleep(interval)


def shim_space() -> None:
    with InputClient() as inp:
        inp.key("SPACE")


# The controlled character's display name, as play.py reads it (CHARACTER_NAME_LUA).
INGAME_NAME_LUA = (
    'local c = (Ext.Entity.GetAllEntitiesWithComponent("ClientControl") or {})[1] '
    'local dn = c and c.DisplayName '
    'if not dn then return "" end '
    'return Ext.Loca.GetTranslatedString(dn.NameKey.Handle.Handle) or ""')


def check_ingame_name(d: Driver, name: str, timeout: float = 120.0) -> None:
    """After the load: the character in the world must carry the name (fails the run otherwise)."""
    d.header(f"in-game character is '{name}'")
    deadline = time.time() + timeout
    got = ""
    while time.time() < deadline:
        try:
            got = d.lua.evaluate(INGAME_NAME_LUA, context="client", timeout=10).strip()
        except (LuaDebugError, TimeoutError, ConnectionError) as exc:
            print(f"    ({exc})")
            got = ""
        if got:
            break
        time.sleep(3)
    print(f"    in-game name: {got!r}")
    if got.casefold() != name.casefold():
        print(f"    FAILED: expected '{name}'")
        d.ok = False


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

    # The intro movie plays while the new game loads (and after "Venture forth"): skip it like the MCP
    # server does (idles if the server or runner already runs the skipper; BG3AI_MOVIE_SKIP=0 turns it off).
    from bg3ai.skipper import start_background
    start_background(args.port, lambda m: print(f"    [autoskip] {m}", flush=True))

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
        d.header("pass the splash screen (input shim)")
        try:
            pass_splash(d.rpc, shim_space, log=lambda m: print(f"    {m}"))
        except (TimeoutError, InputError, OSError) as exc:
            print(f"    FAILED: {exc}")
            return 1
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

    # Apply without finishing: the name is set and verified first, and only ui.cc_finish (which re-checks
    # it) presses "Venture forth". A character finished as "Tav" breaks save ownership (--save-owner).
    plan = plan or DEFAULT_PLAN
    st = d.job("apply the plan", "ui.cc_apply", {"plan": plan, "finish": False}, timeout=150)
    if not st or st.get("status") != "done":
        print("\nSOME STEPS FAILED (not finishing)")
        return 1
    want = plan.get("name")
    if want is not None:
        if not name_character(d, want):
            print(f"\nNAMING FAILED: the character is not '{want}'; not finishing")
            return 1
    after = d.run("character creation after applying", "ui.cc_options") if args.no_finish else None
    if after:
        summarize_cc(after)
    if not args.no_finish:
        if d.run("venture forth (name re-checked)", "ui.cc_finish", {"name": want} if want else {}) is None:
            print("\nSOME STEPS FAILED (not finished)")
            return 1
        # The tadpole movie, the load and the intro movie (skipped above): wait for the game to run.
        for _ in range(60):
            time.sleep(5)
            try:
                st = d.rpc("ui.state", timeout=10)
                print(f"    screen: {st.get('screen')} {st.get('game_state') or ''}".rstrip())
                if st.get("screen") not in ("loading", "movie"):
                    print("    pages:", [w["n"] for w in d.rpc("ui.widgets") if w.get("vis")])
                    break
            except Exception as exc:  # loading screens stall evals; keep going
                print(f"    (ui.state: {exc})")
        if want is not None:
            check_ingame_name(d, want)
    print("\nALL STEPS OK" if d.ok else "\nSOME STEPS FAILED")
    return 0 if d.ok else 1


if __name__ == "__main__":
    sys.exit(main())
