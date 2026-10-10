"""Acceptance run before play resumes: every scriptable DONE item of docs/BACKLOG.md, live, PASS/FAIL/SKIP per item.

    uv run --project bridge python tools/dev/acceptance.py                  # every section, one game.lock hold each
    uv run --project bridge python tools/dev/acceptance.py --only rest,trade
    uv run --project bridge python tools/dev/acceptance.py --list

Each section runs in its own process under `flock -w 1800 runtime/game.lock` (other workers test between
sections), loads its own save first and verifies the load (a server Lua marker set before the load must be gone
after it, the party must be readable and the screen the game view), then reloads that save at the end, so story
state never changes. Saves are only loaded (S027-start, S026-dammon-heat), never written; the bridge runs with a
scratch runtime and a scratch memory dir (note() writes there). Results: runtime/acceptance/<section>.json + .log,
and the table printed at the end (also runtime/acceptance/table.md). Exit code = number of FAILs.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "runtime" / "acceptance"
LOCK = REPO / "runtime" / "game.lock"
BASE = "S027-start"            # the real playthrough state (after S026); loaded, never overwritten
TRADE_SAVE = "S026-dammon-heat"  # Last Light Inn, Quartermaster Talli in view
SECTIONS = ["reads", "rest", "trade", "gear", "swap", "nav", "loads", "combat"]

RESULTS: list[dict] = []
S = None  # bg3ai.server, imported in the section process
T0 = time.monotonic()
LOGF = None


def log(msg: str) -> None:
    line = f"[{time.monotonic() - T0:6.1f}s] {msg}"
    print(line, flush=True)
    if LOGF:
        LOGF.write(line + "\n")
        LOGF.flush()


def item(name: str, verdict: str, evidence: str) -> None:
    ev = re.sub(r"\s+", " ", str(evidence)).strip()[:300]
    RESULTS.append({"item": name, "verdict": verdict, "evidence": ev})
    log(f">>> {verdict:4} {name}: {ev}")


def call(name: str, **kw) -> str:
    fn = getattr(S, name)
    fn = getattr(fn, "fn", fn)
    t = time.monotonic()
    try:
        out = fn(**kw)
    except Exception as e:  # noqa: BLE001 - a tool raising is a result too
        out = f"EXCEPTION {type(e).__name__}: {e}"
    dt = time.monotonic() - t
    log(f"=== {name}({kw}) {dt:.1f}s\n{out}\n")
    return f"{out}" if not isinstance(out, str) else out


def took(name: str, **kw) -> tuple[str, float]:
    t = time.monotonic()
    out = call(name, **kw)
    return out, time.monotonic() - t


def first_line(s: str) -> str:
    return (s or "").strip().splitlines()[0] if (s or "").strip() else ""


def grab(pattern: str, s: str, flags=re.M) -> str:
    m = re.search(pattern, s or "", flags)
    return m.group(0) if m else ""


def status() -> dict:
    st = S.game.try_rpc("state.status")
    return st if isinstance(st, dict) else {}


def ui_screen() -> str:
    ui = S.game.try_rpc("ui.state", context="client")
    return (ui or {}).get("screen", "?") if isinstance(ui, dict) else "?"


def alive() -> bool:
    return subprocess.run(["pgrep", "-x", "bg3"], capture_output=True).returncode == 0


def set_marker() -> None:
    S.game.eval("Mods.BG3AI.BG3AI.__acceptance = true return 1", context="server")


def marker_gone() -> bool:
    try:
        return S.game.eval("return tostring(Mods.BG3AI.BG3AI.__acceptance)", context="server") == "nil"
    except Exception:  # noqa: BLE001
        return False


def load_verified(label: str, record: bool = False) -> bool:
    """load_game + independent checks: marker wiped (a new Lua state), party readable, game screen."""
    try:
        set_marker()
    except Exception as e:  # noqa: BLE001
        log(f"marker not set ({e}): the game may be loading")
    out, dt = took("load_game", save=label)
    ok_tool = "FAILED" not in out and "EXCEPTION" not in out
    st = {}
    for _ in range(30):
        st = status()
        if st.get("party") and ui_screen() == "game":
            break
        time.sleep(1)
    gone = marker_gone()
    party = [r.get("name") for r in st.get("party") or []]
    ok = ok_tool and gone and bool(party)
    ev = f"{first_line(out)} in {dt:.1f}s; new Lua state={gone}; region={st.get('region')} party={party} screen={ui_screen()}"
    if record or not ok:
        item(f"load_game verification ({label})", "PASS" if ok else "FAIL", ev)
    else:
        log("loaded: " + ev)
    if ok:
        time.sleep(2)  # HUD + mod settle
    return ok


def need(cond: bool, name: str, why: str) -> bool:
    if not cond:
        item(name, "SKIP", why)
    return cond


# ------------------------------------------------------------------------------------------------- sections

def sec_reads() -> None:
    if not load_verified(BASE, record=True):
        return
    st_full = call("get_status", full=True)
    al = grab(r"^ALERTS.*$", st_full)
    item("ALERTS line", "PASS" if al else "FAIL", al or "no ALERTS line in get_status(full=true)")

    st = status()
    camp = [c.get("name") for c in st.get("camp") or []]
    party_line = grab(r"^PARTY:.*$", st_full)
    if party_line:
        item("PARTY hint", "PASS", party_line)
    else:
        mk = S.game.try_rpc("nav.markers") or {}
        forced = S._party_hint_line(st, (mk or {}).get("markers") if isinstance(mk, dict) else None, force=True)
        item("PARTY hint", "PASS" if forced or not camp else "WARN",
             f"forced line: {forced!r}; camp={camp}" if forced else f"no PARTY line (camp={camp}; nothing to suggest?)")

    lvl = grab(r"^LEVEL UP.*$", st_full)
    pend = [(r.get("name"), r.get("level")) for r in (st.get("party") or []) + (st.get("camp") or []) if r.get("level_up")]
    if lvl:
        item("camp level-up line", "PASS", lvl)
    elif pend:
        item("camp level-up line", "FAIL", f"level-ups pending {pend} but no LEVEL UP line")
    else:
        item("camp level-up line", "SKIP", "nobody has a level-up pending in this save")

    mp = call("get_map", full=True)
    ids = sorted(set(re.findall(r"\b(?:WLD_\w+|WAYP_\w+|[A-Z]{3}_[A-Za-z]+_[A-Za-z_]+)\b", mp)))
    wps = len(re.findall(r"(?im)waypoint", mp))
    item("get_map names only", "PASS" if not ids and len(mp) > 40 else "FAIL",
         f"ids={ids[:5]}; {len(mp)} chars; first: {first_line(mp)}")

    mem = OUT / "memory"
    (mem / "sessions").mkdir(parents=True, exist_ok=True)
    log_path = mem / "sessions" / "S999.md"
    log_path.write_text("# S999 acceptance scratch log\n")
    out, dt = took("note", text="acceptance check line one\n[quest] second line", tag="lesson")
    body = log_path.read_text()
    ok = "[lesson] acceptance check line one" in body and "[quest] second line" in body and dt < 0.5
    item("note()", "PASS" if ok else "FAIL", f"{first_line(out)} in {dt * 1000:.0f} ms; log tail: {body.strip().splitlines()[-1]}")

    scr = ui_screen()
    if need(scr == "game", "ESCAPE guard", f"screen is {scr}, not the plain game view"):
        out, dt = took("press_key", key="ESCAPE")
        after = ui_screen()
        ok = "refused" in out.lower() and after == "game"
        item("ESCAPE guard", "PASS" if ok else "FAIL", f"{first_line(out)} ({dt:.1f}s); screen after={after}")

    out, dt = took("feed_gale", item="Moonlantern")
    after = ui_screen()
    ok = ("FAILED" in out or "REFUSED" in out or "not open" in out.lower()) and after == "game" and dt < 20
    item("feed_gale dry run (no donation window)", "PASS" if ok else "FAIL",
         f"{first_line(out)} ({dt:.1f}s); screen after={after} (the window itself only opens from Gale's dialogue)")

    talk = grab(r"^TALK:.*$", st_full)
    item("TALK line ('!' companion)", "PASS" if talk else "SKIP", talk or "no companion has a '!' in this save")

    load_verified(BASE)


def sec_rest() -> None:
    if not load_verified(BASE):
        return
    st0 = status()
    out, dt = took("short_rest")
    if "done" in out:
        hp = grab(r"^HP (unchanged|\w+ \d+→).*$", out)
        item("short_rest (HP before→after)", "PASS" if hp else "FAIL", f"{dt:.1f}s; {hp or first_line(out)}")
    else:
        item("short_rest (HP before→after)", "FAIL" if "short rest" not in out.lower() else "SKIP", f"{dt:.1f}s; {first_line(out)}")

    out, dt = took("camp", action="go")
    if "FAILED" in out and "already at camp" not in out:
        item("camp go", "FAIL", f"{dt:.1f}s; {first_line(out)}")
        load_verified(BASE)
        return
    time.sleep(2)
    out, dt = took("long_rest")
    refused = "partial" in out.lower() and "FAILED" in out
    if refused:
        scr = ui_screen()
        w, wdt = took("wait_for", target="combat_started", timeout_s=10)
        ok = scr == "rest" and "not waiting" in w and wdt < 2
        item("wait_for on the rest screen", "PASS" if ok else "FAIL", f"screen={scr}; {first_line(w)} ({wdt:.2f}s)")
        out, dt = took("long_rest", partial=True)
    else:
        item("wait_for on the rest screen", "SKIP", "long_rest didn't stop at the supplies check (enough supplies)")
    clicks = len(re.findall(r"(?i)hand.?click|click\(", out))
    ok = ("done" in out or "rested" in out) and "FAILED" not in out
    item("long_rest no-click", "PASS" if ok else "FAIL", f"{dt:.1f}s; {first_line(out)} {grab(r'(?i)^.*(supplies|partial|full).*$', out)}")
    al = grab(r"^ALERTS.*$", call("get_status", full=True))
    item("ALERTS after rest", "PASS" if al else "INFO", al or "no ALERTS (all healthy)")

    st = status()
    night = S.game.try_rpc("ui.travel_block", context="client")
    log(f"travel_block after rest: {night}")
    out, dt = took("fast_travel", waypoint="Last Light Inn")
    time.sleep(2)
    st = status()
    ui = S.game.try_rpc("ui.state", context="client") or {}
    party = st.get("party") or []
    lead = (party[0].get("pos") if party else None) or [0, 0, 0]
    spread = max((sum((a - b) ** 2 for a, b in zip(r.get("pos") or lead, lead)) ** 0.5 for r in party), default=0)
    ok = "FAILED" not in out and str(ui.get("region", "")).lower() != "campsite" and spread < 25
    item("fast_travel + camp leave (night camp)", "PASS" if ok else "FAIL",
         f"{dt:.1f}s; {first_line(out)}; region={ui.get('region')} party spread={spread:.0f} m")
    load_verified(BASE)


def sec_trade() -> None:
    if not load_verified(TRADE_SAVE):
        return
    lk = call("look", full=True)
    m = re.search(r"(e\d+) Quartermaster Talli", lk)
    if not m:  # Talli stands ~33 m S of the save spot, inside the inn (S02x logs: (-31.3,27.5,130.2))
        tr, dt = took("travel_to", x=-31.0, y=27.5, z=132.5)
        w = ""
        for _ in range(3):
            w = call("wait_for", target="queue", timeout_s=60, lead_s=0)
            if "still waiting" not in w:
                break
        lk = call("look", full=True)
        m = re.search(r"(e\d+) Quartermaster Talli", lk)
        item("travel_to indoors (Last Light Inn, to Talli)", "PASS" if m else "FAIL",
             f"{dt:.1f}s to start; {first_line(tr)} | {first_line(w)}; Talli in view={bool(m)}")
    if not need(bool(m), "trade names + sell junk", "Talli not in view in " + TRADE_SAVE):
        return
    out = call("talk_to", npc=m.group(1))
    opt = re.search(r"^\s*(\d+)\. Let's see your wares", out, re.M)
    if not opt:
        g = call("get_dialogue")
        opt = re.search(r"^\s*(\d+)\. Let's see your wares", g, re.M)
    if not need(bool(opt), "trade names + sell junk", "no 'Let's see your wares' answer: " + first_line(out)):
        load_verified(TRADE_SAVE)
        return
    wares = call("choose_dialogue", option=int(opt.group(1)))
    if len(wares) < 400:  # choose_dialogue says only "screen=trade": list the wares
        wares = call("trade")
    nameless = re.findall(r"(?m)^\s*-?\s*(?:e\d+ )?price=\d+", wares)
    sup = grab(r"supply=\d+", wares)
    ok = not nameless and bool(sup) and len(wares) < 3500
    item("trade wares named + compact list", "PASS" if ok else "FAIL",
         f"{len(wares)} chars, nameless rows={len(nameless)}, {sup or 'no supply=N'}; {first_line(wares)}")
    junk = call("get_inventory", filter="junk")
    g0 = status().get("gold")
    out, dt = took("trade", sell="junk")
    g1 = status().get("gold")
    ok = "FAILED" not in out and (isinstance(g0, int) and isinstance(g1, int) and g1 > g0 or "nothing" in out.lower())
    item("trade sell=junk (one call)", "PASS" if ok else "FAIL", f"{dt:.1f}s; gold {g0}→{g1}; {first_line(out)}")
    esc = call("press_key", key="ESCAPE")
    load_verified(TRADE_SAVE)


def sec_gear() -> None:
    if not load_verified(BASE):
        return
    hands = ('local o={} for _,r in pairs(Osi.DB_Players:Get(nil)) do local c=r[1]; local it=Osi.GetEquippedItem(c,"Melee Main Weapon") '
             'o[#o+1]=Osi.ResolveTranslatedString(Osi.GetDisplayName(c)).."="..(it and Osi.ResolveTranslatedString(Osi.GetDisplayName(it)) or "-") end '
             'return table.concat(o,"; ")')
    before = S.game.eval(hands, context="server")
    gear = call("get_inventory", filter="gear")
    # Job push: equip_best runs several short jobs; with the push each needs ~1 job.poll (was ~10 per 1 s job).
    from bg3ai import jobpush
    counts: dict[str, int] = {}
    orig = S.game.rpc

    def counting(op, *a, **k):
        counts[op] = counts.get(op, 0) + 1
        return orig(op, *a, **k)

    jp = jobpush.shared(S.game.host, S.game.port)
    jp.ensure()
    S.game.rpc = counting
    try:
        out, dt = took("equip_best")
    finally:
        S.game.rpc = orig
    jobs = sum(v for k, v in counts.items() if k.startswith("act."))
    polls = counts.get("job.poll", 0)
    ok = jp.seen and jobs and polls <= jobs + 3
    item("job push latency", "PASS" if ok else "FAIL",
         f"equip_best {dt:.2f}s: {jobs} jobs, {polls} job.polls, {sum(counts.values())} RPCs; listener heard marks={jp.seen}")
    time.sleep(1.5)
    after = S.game.eval(hands, context="server")
    holder = re.search(r"(\w+)=Moonlantern", after or "")
    caster_ok = holder and holder.group(1) in ("Gale", "Shadowheart", "Wyll")
    ok = "FAILED" not in out and dt < 5 and (caster_ok or "Moonlantern" not in (before or ""))
    item("equip_best + lantern plan", "PASS" if ok else "FAIL",
         f"{dt:.1f}s; before [{before}] after [{after}]; {first_line(out)}")
    load_verified(BASE)


def sec_swap() -> None:
    if not load_verified(BASE):
        return
    st = status()
    party = [r.get("name") for r in st.get("party") or []]
    camp = [c.get("name") for c in st.get("camp") or []]
    if not need("Wyll" in camp and "Gale" in party, "swap_party in/out", f"party={party} camp={camp}"):
        return
    out, dt = took("swap_party", into="Wyll", out="Gale")
    p1 = [r.get("name") for r in status().get("party") or []]
    ok1 = "Wyll" in p1 and "Gale" not in p1
    out2, dt2 = took("swap_party", into="Gale", out="Wyll")
    p2 = [r.get("name") for r in status().get("party") or []]
    ok2 = "Gale" in p2 and "Wyll" not in p2
    item("swap_party in/out", "PASS" if ok1 and ok2 else "FAIL",
         f"in {dt:.1f}s → {p1}; back {dt2:.1f}s → {p2}; {first_line(out)} | {first_line(out2)}")
    st_full = call("get_status", full=True)
    item("PARTY hint after swaps", "INFO", grab(r"^PARTY:.*$", st_full) or "no PARTY line")
    load_verified(BASE)


def sec_nav() -> None:
    if not load_verified(BASE):
        return
    st = status()
    region = str((S.game.try_rpc("ui.state", context="client") or {}).get("region", ""))
    if region.lower() == "campsite":
        call("camp", action="leave")
    me = (status().get("party") or [{}])[0]
    p = me.get("pos") or [0, 0, 0]
    # Multi-leg walk: an explored spot ~80-150 m away (route's closest-visited answer), else the waypoint list.
    fr = call("get_frontier")
    pts = [(float(a), float(b), float(c)) for a, b, c in re.findall(r"\((-?[\d.]+),\s*(-?[\d.]+),\s*(-?[\d.]+)\)", fr)]
    far = [q for q in pts if 40 < ((q[0] - p[0]) ** 2 + (q[2] - p[2]) ** 2) ** 0.5 < 160]
    if need(bool(far), "travel_to multi-leg", "no frontier point 40-160 m away to walk to: " + first_line(fr)):
        q = far[0]
        out, dt = took("travel_to", x=q[0], y=q[1], z=q[2])
        end = time.monotonic() + 180
        w = ""
        while time.monotonic() < end:
            w = call("wait_for", target="queue", timeout_s=60, lead_s=0)
            if "still waiting" not in w:
                break
        me2 = (status().get("party") or [{}])[0].get("pos") or p
        left = ((me2[0] - q[0]) ** 2 + (me2[2] - q[2]) ** 2) ** 0.5
        legs = len(re.findall(r"(?i)leg", out + w))
        ok = left < 6 and "INTERRUPTED" not in w
        item("travel_to multi-leg", "PASS" if ok else "FAIL",
             f"{((q[0]-p[0])**2+(q[2]-p[2])**2)**0.5:.0f} m trip, {left:.1f} m left at the end; {first_line(out)} | {first_line(w)}")

    # Interior via entrance: the Mausoleum room from its gate is REFUSED naming the entrance, then interact.
    ft, dt = took("fast_travel", waypoint="Moonrise Towers")
    if not need("FAILED" not in ft, "travel_to into an interior via entrance", "fast_travel Moonrise Towers failed: " + first_line(ft)):
        load_verified(BASE)
        return
    t = time.monotonic()
    out = call("travel_to", x=-183.0, y=38.0, z=99.0)
    end = time.monotonic() + 240
    while time.monotonic() < end:
        w = call("wait_for", target="queue", timeout_s=60, lead_s=0)
        if "still waiting" not in w:
            break
    legs = len(re.findall(r"(?m)^leg \d+:", w))
    arr = grab(r"arrived \([\d.]+m from it\)", w)
    item("travel_to multi-leg (waypoint → Mausoleum gate)", "PASS" if arr and legs >= 2 else "FAIL",
         f"{first_line(out)}; {legs} legs, {arr or first_line(w)} in {time.monotonic() - t:.0f}s")
    ladder_from_top()
    if S.game.try_rpc("state.combat", {}) and (S.game.try_rpc("state.combat") or {}).get("active"):
        item("travel_to into an interior via entrance", "SKIP", "a fight started on the way (reloaded)")
        load_verified(BASE)
        return
    t = time.monotonic()
    ref = call("travel_to", x=-286.0, y=-31.0, z=-847.5)
    rdt = time.monotonic() - t
    m = re.search(r"(e\d+)[^\n]{0,30}Mausoleum Entrance", ref + "\n" + call("get_frontier"))
    ok = "REFUSED" in ref and "interact" in ref
    item("travel_to other floor → entrance named", "PASS" if ok else "FAIL", f"{rdt:.2f}s; {first_line(ref)}")
    if need(bool(m), "travel_to into an interior via entrance", "no Mausoleum Entrance handle near the gate"):
        out, dt = took("interact", target=m.group(1))
        time.sleep(3)
        lv = status()
        tr = call("travel_to", x=-283.0, y=-31.0, z=-847.0)
        for _ in range(4):
            w = call("wait_for", target="queue", timeout_s=60, lead_s=0)
            if "still waiting" not in w:
                break
        pos = (status().get("party") or [{}])[0].get("pos") or [0, 0, 0]
        d = ((pos[0] + 283) ** 2 + (pos[2] + 847) ** 2) ** 0.5
        item("travel_to into an interior via entrance", "PASS" if d < 8 else "FAIL",
             f"interact entrance {dt:.1f}s ({first_line(out)}); then {d:.1f} m from the room spot")
        if d < 15:
            nb = call("get_nearby", radius=25, kinds="button")
            tags = re.findall(r"\(under ([^)]+)\)", nb)
            trap = "TRAP spotted" in w  # a perception roll: the walk stops short and fewer buttons are in sight
            item("buttons listed (Mausoleum)", "PASS" if len(tags) >= (1 if trap else 2) else "FAIL",
                 f"{len(re.findall(r'Button', nb))} buttons, tags={tags[:4]}" + ("; the walk stopped at a spotted trap" if trap else ""))
        else:
            item("buttons listed (Mausoleum)", "SKIP", "did not reach the puzzle room")
    load_verified(BASE)


KNOTTED_ROOTS = (-194.8, 31.2, 80.6)  # the ladder (its foot) below the Mausoleum gate spot (-183, 38.2, 99)
LADDER_TOP = (-194.9, 35.6, 79.1)     # where its Use stands at the top (live 14:2x)


def ladder_from_top() -> None:
    """At the Mausoleum gate: the Knotted Roots ladder's foot is ~7 m below; interact climbs down from the top.
    The ACTIVE character must be the one who climbs (vis 13:3x: Gale climbed instead of Claude)."""
    def heights() -> tuple[str, dict]:
        st = status()
        return st.get("active"), {r.get("name"): float((r.get("pos") or [0, 0, 0])[1]) for r in st.get("party") or []}

    def me_pos() -> list:
        st = status()
        return next((r.get("pos") for r in st.get("party") or [] if r.get("uuid") == st.get("active")), None) or [0, 0, 0]

    p0 = me_pos()
    raw = S.game.try_rpc("state.nearby", {"radius": 40, "kinds": ["ladder"]}) or {}
    lads = [it for it in (raw.get("items") or []) if it.get("pos") and p0[1] - it["pos"][1] >= 2.5]
    if not lads:  # in view from the gate only at some angles: step toward the ladder's top first
        tr = call("travel_to", x=LADDER_TOP[0] + 2.0, y=LADDER_TOP[1], z=LADDER_TOP[2] - 2.0)
        if "started" in tr:
            call("wait_for", target="queue", timeout_s=40, lead_s=0)
        p0 = me_pos()
        raw = S.game.try_rpc("state.nearby", {"radius": 40, "kinds": ["ladder"]}) or {}
        lads = [it for it in (raw.get("items") or []) if it.get("pos") and p0[1] - it["pos"][1] >= 2.5]
    if not need(bool(lads), "ladder from the top", f"no ladder foot >= 2.5 m below the party at {p0} "
                f"({len(raw.get('items') or [])} ladders seen)"):
        return
    lad = min(lads, key=lambda it: (it["pos"][0] - p0[0]) ** 2 + (it["pos"][2] - p0[2]) ** 2)
    h = S.H.get(lad["uuid"], lad.get("name"))
    act, y0 = heights()
    name = next((r.get("name") for r in status().get("party") or [] if r.get("uuid") == act), "?")
    out, dt = took("interact", target=h)
    foot = float(lad["pos"][1])
    ys, t_c = y0, time.monotonic()
    for _ in range(24):  # the climb runs on after the interact job ends (followers climb too)
        _, ys = heights()
        if ys.get(name, 99) < foot + 1.5:
            break
        time.sleep(0.5)
    t_c = time.monotonic() - t_c
    climbed = [n for n in ys if ys[n] < foot + 1.5]
    ok = "FAILED" not in out and "REFUSED" not in out and name in climbed
    item("ladder from the top (active character climbs)", "PASS" if ok else "FAIL",
         f"{lad.get('name')} (foot y {foot:.1f}) {dt:.1f}s + {t_c:.1f}s; active={name}; at the foot={climbed}; heights "
         + ", ".join(f"{n} {y0.get(n, 0):.1f}->{ys[n]:.1f}" for n in ys) + f"; {first_line(out)}")
    # back to the gate for the entrance check (up the ladder or round by the path)
    call("travel_to", x=-183.0, y=38.0, z=99.0)
    for _ in range(3):
        if "still waiting" not in call("wait_for", target="queue", timeout_s=40, lead_s=0):
            break


def bg3le_log() -> Path | None:
    files = sorted(glob.glob(str(REPO / "runtime" / "logs" / "bg3le-*.log.*")), key=os.path.getmtime)
    for f in reversed(files):
        try:
            if "guarded" in Path(f).read_text(errors="replace"):
                return Path(f)
        except OSError:
            pass
    return Path(files[-1]) if files else None


def sec_loads() -> None:
    lg = bg3le_log()
    cnt = lambda: len(re.findall(r"cleared \(\d+ so far\)", lg.read_text(errors="replace"))) if lg else -1
    c0 = cnt()
    times = []
    for i in range(3):
        t = time.monotonic()
        ok = load_verified(BASE, record=(i == 0))
        times.append(time.monotonic() - t)
        if not alive():
            item("crash guard (3 reloads)", "FAIL", f"game died on reload {i + 1}")
            return
        if not ok:
            item("crash guard (3 reloads)", "FAIL", f"reload {i + 1} not verified")
            return
    crash = grep_crash(lg)
    item("crash guard (3 reloads)", "PASS" if not crash else "FAIL",
         f"3 verified reloads {', '.join(f'{x:.0f}s' for x in times)}; game alive; guard cleared count {c0}→{cnt()} ({lg.name if lg else 'no log'}){'; ' + crash if crash else ''}")


def grep_crash(lg: Path | None) -> str:
    if not lg:
        return ""
    m = re.findall(r".*crash: signal.*", lg.read_text(errors="replace"))
    return m[-1][:120] if m else ""


SHADOW_PACK = (-236.0, 31.0, 12.0)  # Wraith, Shadow Mastiff, Shadows and Shadow-Cursed townsfolk W of the Moonrise road


def counted(name: str, **kw) -> tuple[str, float, dict]:
    """A tool call with every RPC it makes counted by op (job push: one job.poll per short job)."""
    counts: dict[str, int] = {}
    orig = S.game.rpc

    def counting(op, *a, **k):
        counts[op] = counts.get(op, 0) + 1
        return orig(op, *a, **k)

    S.game.rpc = counting
    try:
        out, dt = took(name, **kw)
    finally:
        S.game.rpc = orig
    return out, dt, counts


def sec_combat() -> None:
    """A real fight (reloaded after): the shadow pack W of the Moonrise road. Opens it by walking into their sight,
    then plays party turns with combat_turn: melee approach + attack by the SHOWN ability name, the RPC count with
    the job push, and the names in the action lists."""
    if not load_verified(BASE):
        return
    from bg3ai import jobpush
    jp = jobpush.shared(S.game.host, S.game.port)
    jp.ensure()
    ft = call("fast_travel", waypoint="Moonrise Towers")
    if not need("FAILED" not in ft, "combat_turn on a safe fight", "fast_travel Moonrise Towers failed: " + first_line(ft)):
        load_verified(BASE)
        return
    call("travel_to", x=SHADOW_PACK[0] + 18.0, y=SHADOW_PACK[1], z=SHADOW_PACK[2] + 18.0)
    end = time.monotonic() + 120
    cs = {}
    while time.monotonic() < end:
        cs = S.game.try_rpc("state.combat") or {}
        if cs.get("active"):
            break
        w = call("wait_for", target="queue", timeout_s=20, lead_s=0)
        if "still waiting" not in w:
            break
    cs = S.game.try_rpc("state.combat") or {}
    opener_u = None
    if not cs.get("active"):
        # The pack doesn't notice a party walking up (live 12:5x): open the fight like a player, with an attack.
        nb = S.game.try_rpc("state.nearby", {"radius": 40}) or {}
        chars = nb.get("characters") or []
        log("in view: " + "; ".join(f"{c.get('name')} {c.get('relation')} dead={c.get('dead')}" for c in chars[:12]))
        foes = [c for c in chars if c.get("relation") == "hostile" and not c.get("dead")]
        if foes:
            opener_u = foes[0]["uuid"]
            fh = S.H.get(foes[0]["uuid"], foes[0].get("name"))
            for _ in range(3):
                ua = call("use_ability", ability="Ranged Attack", target=fh, who="Claude")
                said = re.search(r"(?m)^(COMBAT: the attack started a fight.*|combat: (NOT started|none|not started).*)$", ua)
                if "FAILED" not in first_line(ua):
                    item("opener says whether combat started", "PASS" if said else "FAIL",
                         said.group(1)[:160] if said else first_line(ua))
                time.sleep(2.0)
                cs = S.game.try_rpc("state.combat") or {}
                dbg = S.game.eval(f'local h=Osi.GetHostCharacter() local u="{foes[0]["uuid"]}" return "host combat="'
                                  '..tostring(Osi.IsInCombat(h)).." foe combat="..tostring(Osi.IsInCombat(u))'
                                  '.." enemy="..tostring(Osi.IsEnemy(u,h)).." hp="..tostring(Osi.GetHitpoints(u))'
                                  '.." canfight="..tostring(Osi.CanFight and Osi.CanFight(u))', context="server")
                log(f"after the attack: state.combat active={cs.get('active')}; {dbg}")
                if cs.get("active"):
                    break
    if not need(bool(cs.get("active")), "combat_turn on a safe fight", "no fight started walking to the shadow pack"):
        load_verified(BASE)
        return
    gc = call("get_combat_state")
    ids = re.findall(r"\((?:Target|Projectile|Shout|Zone|Rush)_\w+\)", gc)
    named = re.findall(r"(?m)^\s*(?:ACTIONS|actions).*$", gc)
    item("ability names in the action lists", "PASS" if not ids else "FAIL",
         f"{len(gc)} chars, ids shown={ids[:4]}; {named[0][:160] if named else first_line(gc)}")
    from bg3ai import turn as T
    played = 0
    end = time.monotonic() + 150
    while time.monotonic() < end and played < 3:
        cs = S.game.try_rpc("state.combat") or {}
        if not cs.get("active"):
            break
        if ui_screen() == "reaction":
            call("react", use=False)
            continue
        st = status()
        party = {r.get("uuid"): r for r in st.get("party") or []}
        now = [u for u in T.acting(cs) if u in party]
        if not now:  # enemies acting
            time.sleep(0.5)
            continue
        who_u = now[0]
        nb = S.game.try_rpc("state.nearby", {"radius": 40}) or {}
        foes = [c for c in nb.get("characters") or [] if c.get("relation") == "hostile" and not c.get("dead")]
        here = party[who_u].get("pos") or [0, 0, 0]
        if foes:
            foe = min(foes, key=lambda c: sum((a - b) ** 2 for a, b in zip(c.get("pos") or [9e9] * 3, here)))
        elif opener_u:
            # Nothing in view (a Shadow in Shadow Blend): plan on the opener's target anyway, like an agent
            # planning on what it last saw; combat_turn must re-aim or say nobody is in sight (round 2).
            foe = {"uuid": opener_u, "name": "opener target (unseen)"}
        else:
            call("wait", seconds=3)
            continue
        fh = S.H.get(foe["uuid"], foe.get("name"))
        plan = json.dumps({"move": {"target": fh, "within": 1.5}, "on_reaction": "skip",
                           "action": [{"ability": "Main Hand Attack", "target": fh},
                                      {"ability": "Ranged Attack", "target": fh}, "Dash"]})
        out, dt, counts = counted("combat_turn", plan=plan, who=S.H.get(who_u))
        log(f"RPCs by op: {dict(sorted(counts.items(), key=lambda kv: -kv[1]))}")
        jobs = sum(v for k, v in counts.items() if k.startswith("act."))
        polls = counts.get("job.poll", 0)
        rpcs = sum(counts.values())
        moved = bool(re.search(r"(?m)^move \S+: done", out))
        hit = bool(re.search(r"(MainHandAttack|Main Hand Attack|Ranged Attack) e\d+: done", out))
        dashed_on = bool(re.search(r"Shout_Dash: done", out)) and len(re.findall(r"(?m)^move ", out)) >= 2
        unknown = "has no action called 'Main Hand Attack'" in out  # Ranged Attack: only with a ranged weapon
        # round 2 fixes: no melee sent into a sure range miss after a short move; no Dash walking toward a target
        # that vanished (re-aimed at the nearest visible hostile instead)
        range_miss = bool(re.search(r"MainHandAttack \S+: FAILED — target is out of range", out))
        lost_walk = "move " in out and "target is not visible; last seen" in out
        played += 1
        ok = (not unknown and (moved or hit) and polls <= 2 * jobs + 2 and "EXCEPTION" not in out
              and "FAILED: it is not" not in out and not range_miss and not lost_walk)
        if "melee is out of reach" in out or "the plan now aims at" in out:
            log("round-2 lines: " + " | ".join(l for l in out.splitlines()
                                               if "melee is out of reach" in l or "now aims" in l or "skipped (" in l))
        item(f"combat_turn #{played} (melee approach, names, job push)", "PASS" if ok else "FAIL",
             f"{party[who_u].get('name')} {dt:.1f}s; {rpcs} RPCs ({jobs} act jobs, {polls} job.polls; push heard={jp.seen}); "
             f"moved={moved} attacked={hit} dash+walk={dashed_on} range_miss={range_miss} lost_walk={lost_walk}; vs {foe.get('name')}; "
             + " | ".join(l for l in out.splitlines()[:4]))
    if not played:
        item("combat_turn on a safe fight", "SKIP", "the fight ended before a party turn")
    load_verified(BASE)


FUNCS = {"reads": sec_reads, "rest": sec_rest, "trade": sec_trade, "gear": sec_gear, "swap": sec_swap,
         "nav": sec_nav, "loads": sec_loads, "combat": sec_combat}


def run_section(name: str) -> int:
    global S, LOGF
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("BG3AI_PLAYTHROUGH", "claude-run-2")
    os.environ.setdefault("BG3AI_SAVE_PREFIX", "Claude-")
    os.environ["BG3AI_RUNTIME"] = str(OUT / "rt")
    os.environ["BG3AI_MEMORY_DIR"] = str(OUT / "memory")
    os.environ["BG3AI_SESSION"] = "S999"
    (OUT / "memory" / "sessions").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(REPO / "bridge" / "src"))
    from bg3ai import server  # noqa: E402
    S = server
    LOGF = open(OUT / f"{name}.log", "w")
    log(f"section {name} start {time.strftime('%H:%M:%S')}")
    try:
        if not alive():
            item(f"section {name}", "FAIL", "the game is not running")
        else:
            FUNCS[name]()
    except Exception as e:  # noqa: BLE001
        import traceback
        log(traceback.format_exc())
        item(f"section {name}", "FAIL", f"script error {type(e).__name__}: {e}")
    (OUT / f"{name}.json").write_text(json.dumps(RESULTS, indent=1))
    log(f"section {name} end {time.strftime('%H:%M:%S')}")
    return sum(r["verdict"] == "FAIL" for r in RESULTS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--section", default="")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        print(" ".join(SECTIONS))
        return 0
    if a.section:
        return run_section(a.section)
    names = [s for s in (a.only.split(",") if a.only else SECTIONS) if s]
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in names:
        print(f"--- section {name}: waiting for {LOCK.name} ({time.strftime('%H:%M:%S')})", flush=True)
        subprocess.run(["flock", "-w", "1800", str(LOCK), "timeout", "900", sys.executable, __file__, "--section", name])
        f = OUT / f"{name}.json"
        rows += json.loads(f.read_text()) if f.exists() else [{"item": f"section {name}", "verdict": "FAIL", "evidence": "no result file"}]
    table = ["| item | verdict | evidence |", "|---|---|---|"] + [
        f"| {r['item']} | {r['verdict']} | {r['evidence'].replace('|', '/')} |" for r in rows]
    (OUT / "table.md").write_text("\n".join(table) + "\n")
    print("\n".join(table))
    return sum(r["verdict"] == "FAIL" for r in rows)


if __name__ == "__main__":
    raise SystemExit(main())
