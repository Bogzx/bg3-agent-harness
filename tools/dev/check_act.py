"""Scripted live checks for the action layer and the think-while-acting pipeline (worker act).

Run against a loaded game (the AI's own save, never the user's), after a hot reload of the mod:

    uv run --project bridge python tools/dev/check_act.py                 # read-only checks only
    uv run --project bridge python tools/dev/check_act.py move queue      # named checks (these act!)
    uv run --project bridge python tools/dev/check_act.py combat          # on the active character's turn
    uv run --project bridge python tools/dev/check_act.py use --item e12  # drinks/uses that item

Read-only (default): latency, approach, forms.
Acting:
  move     move_to an entity (verified approach point, dist ~2.2, async ETA), a void point (refused),
           talk/interact approach order (approach first, Use/Pickup after; check the log).
  combat   one turn: move 2 m toward the nearest hostile, attack it, end_turn (waits for the party's next
           turn); prints after.movement / after.action and the guard's charges.
  jump     jump 2 m ahead (UseSpellAtPosition 5 vs 6 args), then a too-far jump (refused by the cap).
  use      manage_inventory use on --item (Osi.Use on a carried potion).
  queue    queue two short walks (queue_done), then a long walk + a synthetic teleport_blocked event
           (queue_interrupted with why/left, job cancelled), cancel_queue, and the step-to-step gap.
Each check prints PASS / FAIL / INFO lines; the exit code is the number of FAILs.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bridge" / "src"))

from bg3ai import server as S  # noqa: E402

G = S.game
FAILS = 0


def say(kind: str, msg: str) -> None:
    global FAILS
    if kind == "FAIL":
        FAILS += 1
    print(f"{kind:5} {msg}")


def lua(code: str):
    """Evaluate Lua in the server context and return the decoded JSON value."""
    import json
    raw = G.eval("return Ext.Json.Stringify((function() " + code + " end)())", context="server")
    return json.loads(raw) if raw not in ("", "nil") else None


def status() -> dict:
    return G.rpc("state.status")


def active() -> str:
    return status()["active"]


def nearby(kind: str = "characters") -> list[dict]:
    res = G.rpc("state.nearby", {"radius": 30})
    return res.get(kind) or []


def until(pred, timeout: float = 15.0, step: float = 0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


# ---------------------------------------------------------------------------
# read-only
# ---------------------------------------------------------------------------

def check_latency() -> None:
    for op, ctx in (("sys.ping", "server"), ("act.watch", "server"), ("ui.state", "client")):
        ms = []
        for _ in range(15):
            t = time.perf_counter()
            G.rpc(op, {}, context=ctx)
            ms.append((time.perf_counter() - t) * 1000)
        med = statistics.median(ms)
        say("PASS" if med < 100 else "FAIL", f"latency {op}: median {med:.0f} ms, max {max(ms):.0f} ms (goal <100)")


def check_approach() -> None:
    """Live test 2: floor heights next to doors/chests, and how long one approach resolve takes."""
    me = active()
    items = [i for i in nearby("items") if i.get("kind") in ("door", "container")][:5]
    if not items:
        say("INFO", "approach: no door/container in view; skipped")
    for it in items:
        r = lua(f"""
            local G = BG3AI.Guard
            local t0 = Ext.Utils.MonotonicTime()
            local ok, p = pcall(G.approach_point, "{me}", "{it['uuid']}", 1.8)
            local t = G.pos("{it['uuid']}")
            local hs = t and Ext.Level.GetHeightsAt(t[1] + 1.0, t[3]) or nil
            return {{ ok = ok, plan = ok and p or tostring(p.reason or p), ms = Ext.Utils.MonotonicTime() - t0,
                      heights = hs }}""")
        tag = "PASS" if r and r.get("ok") else "INFO"
        say(tag, f"approach {it.get('name')} ({it.get('kind')}, {it.get('dist')}m): {r}")


def check_forms() -> None:
    """Live test 4 (read-only part): Bound radius readable? Which Osiris forms exist?"""
    me = active()
    r = lua(f"""local e = Ext.Entity.Get("{me}")
        local ok, rad = pcall(function() return e.Bound.Bound.Radius end)
        return {{ ok = ok, radius = ok and rad or tostring(rad), guard = BG3AI.Guard.radius("{me}"),
                  jump_max = BG3AI.Guard.jump_max("{me}") }}""")
    say("PASS" if r and r.get("ok") else "FAIL", f"Bound.Bound.Radius / jump_max: {r}")


# ---------------------------------------------------------------------------
# acting
# ---------------------------------------------------------------------------

def check_move() -> None:
    chars = [c for c in nearby() if not c.get("dead") and c.get("relation") != "hostile" and (c.get("dist") or 0) > 4]
    if chars:
        c = chars[0]
        h = S.H.get(c["uuid"], c.get("name"))
        out = S.move_to(target=h)
        say("PASS" if "started j" in out and "ETA" in out else "FAIL", f"async move_to {c.get('name')}: {out.splitlines()[0]}")
        out = S.wait_for(out.split("started ")[1].split(",")[0], timeout_s=30)
        first = out.splitlines()[0]
        dist = None
        if "dist=" in first:
            dist = float(first.split("dist=")[1].split()[0].rstrip(","))
        say("PASS" if "done" in first and dist is not None and dist <= 3.0 else "FAIL",
            f"move_to entity arrived: {first}")
    else:
        say("INFO", "move: no non-hostile character 4+ m away; entity walk skipped")
    p = G.rpc("state.status")["party"][0].get("pos") or [0, 0, 0]
    void = [p[0], p[1] - 40.0, p[2]]  # 40 m below the floor
    out = S.move_to(x=void[0], y=void[1], z=void[2], wait=True)
    say("PASS" if "FAILED" in out else "FAIL", f"void point refused: {out.splitlines()[0]}")
    talkable = [c for c in nearby() if c.get("can_talk")]
    if talkable:
        c = talkable[0]
        out = S.talk_to(S.H.get(c["uuid"], c.get("name")))
        say("INFO", f"talk_to {c.get('name')}: {out.splitlines()[0]} (close the dialogue yourself)")


def check_combat() -> None:
    me = active()
    cs = G.rpc("state.combat")
    if not cs.get("active") or cs.get("current") != me:
        say("INFO", "combat: not the active character's turn; skipped")
        return
    foes = sorted((c for c in nearby() if c.get("relation") == "hostile" and not c.get("dead")),
                  key=lambda c: c.get("dist") or 99)
    if not foes:
        say("INFO", "combat: no visible hostile")
        return
    foe = foes[0]
    fh = S.H.get(foe["uuid"], foe.get("name"))
    out = S.do_actions('[{"tool":"move_to","target":"%s"},'
                       '{"tool":"use_ability","ability":"Target_MainHandAttack","target":"%s"}]' % (fh, fh),
                       ignore="hostile")
    print(out)
    say("INFO", "check NOW movement/action, movement_charged_by_guard, charged_by_guard above")
    out = S.end_turn()
    say("PASS" if "end_turn: done" in out else "FAIL", f"end_turn: {' | '.join(out.splitlines()[:2])}")


def check_jump() -> None:
    st = status()
    me = st["active"]
    p = next(c for c in st["party"] if c["uuid"] == me).get("pos")
    out = S.jump_to(p[0] + 2.0, p[1], p[2])
    say("PASS" if "done" in out else "FAIL", f"jump 2 m (UseSpellAtPosition form): {out.splitlines()[0]}")
    out = S.jump_to(p[0] + 25.0, p[1], p[2])
    say("PASS" if "too far to jump" in out else "FAIL", f"jump 25 m refused: {out.splitlines()[0]}")


def check_use(item: str | None) -> None:
    if not item:
        say("FAIL", "use: pass --item <id of a carried potion>")
        return
    out = S.manage_inventory("use", item)
    say("PASS" if "done" in out else "FAIL", f"use {item}: {out.splitlines()[0]}")


def check_queue() -> None:
    st = status()
    me = st["active"]
    p = next(c for c in st["party"] if c["uuid"] == me).get("pos")
    # 1) two short walks back to back: queue_done, and the gap between them
    t0 = time.monotonic()
    out = S.queue_actions('[{"tool":"walk_toward","direction":"N","max_m":3},'
                          '{"tool":"move_to","x":%s,"y":%s,"z":%s}]' % (p[0], p[1], p[2]))
    ret_ms = (time.monotonic() - t0) * 1000
    say("PASS" if "queued 2" in out and ret_ms < 300 else "FAIL", f"queue returns at once ({ret_ms:.0f} ms): {out}")
    out = S.wait_for("queue", timeout_s=30)
    say("PASS" if "done (2 steps)" in out else "FAIL", f"queue_done: {out.splitlines()[0]}")
    log = S.RUNTIME / "actions.jsonl"
    if log.exists():
        import json
        recs = [json.loads(x) for x in log.read_text().splitlines()[-6:]]
        steps = [r for r in recs if r["tool"] in ("walk_toward", "move_to")][-2:]
        if len(steps) == 2:
            gap = steps[1]["t"] - steps[0]["t"] - steps[1]["ms"] / 1000
            say("PASS" if gap < 0.5 else "FAIL", f"step-to-step gap ~{gap:.2f} s (goal: no LLM round trip)")
    # 2) interrupt: a long walk, then a (synthetic, dev-only) teleport_blocked event
    out = S.queue_actions('[{"tool":"walk_toward","direction":"S","max_m":20},{"tool":"wait","seconds":1},'
                          '{"tool":"move_to","x":%s,"y":%s,"z":%s}]' % (p[0], p[1], p[2]))
    time.sleep(0.8)
    lua('BG3AI.RPC.push("teleport_blocked", { uuid = "check_act synthetic" }) return true')
    out = S.wait_for("queue_interrupted", timeout_s=10)
    ok = "queue_interrupted" in out and "teleport" in out and "left" in out
    say("PASS" if ok else "FAIL", "queue_interrupted on teleport_blocked: " + " | ".join(out.splitlines()[:4]))
    # 3) cancel_queue stops the running walk
    S.queue_actions('[{"tool":"walk_toward","direction":"N","max_m":20},{"tool":"wait","seconds":2}]')
    time.sleep(0.6)
    out = S.cancel_queue()
    say("PASS" if "stopped walk_toward" in out else "FAIL", f"cancel_queue: {out.splitlines()[0]}")
    S.move_to(x=p[0], y=p[1], z=p[2], wait=True)  # back to the start


CHECKS = {"latency": check_latency, "approach": check_approach, "forms": check_forms, "move": check_move,
          "combat": check_combat, "jump": check_jump, "queue": check_queue}


def main() -> int:
    args = sys.argv[1:]
    item = None
    if "--item" in args:
        i = args.index("--item")
        item = args[i + 1]
        del args[i:i + 2]
    names = args or ["latency", "approach", "forms"]
    S.get_status()  # registers party names/handles
    for n in names:
        print(f"== {n}")
        try:
            if n == "use":
                check_use(item)
            else:
                CHECKS[n]()
        except Exception as e:  # keep going; report
            say("FAIL", f"{n}: {type(e).__name__}: {e}")
    print(f"{FAILS} failure(s)")
    return FAILS


if __name__ == "__main__":
    sys.exit(main())
