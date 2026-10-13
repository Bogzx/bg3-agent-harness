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
DOOR_SAVE = "S050-end"         # Lower City, beside the locked Steel Watch Foundry door (S050 04:12 lockpick hover_miss x4)
SECTIONS = ["reads", "rest", "trade", "gear", "swap", "nav", "loads", "combat", "setup", "se", "act", "vis"]

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
    rest_texts = out + "\n" + call("look")

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
    # S044 23:50:48 fairness LEAK: `rest_flow step=ready_ok:MOO_Assault_ReadyCheck` in look EVENTS (se/vis 02:5x)
    leak = re.findall(r"rest_flow|\w*ReadyCheck\w*|WaypointTravel_\w+", rest_texts + "\n" + out)
    item("no ready-check ids in rest/travel results", "PASS" if not leak else "FAIL",
         f"leaks={sorted(set(leak))[:5]}" if leak else "long_rest, look and fast_travel texts carry no rest_flow line/engine id")
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
    ok = out.startswith("trade: done") and (isinstance(g0, int) and isinstance(g1, int) and g1 > g0
                                            or "nothing" in out.lower())
    tail = " ".join(re.findall(r"(?m)^\((?:sold|\d+ barters)[^\n]*", out))   # "(3 barters of up to 25 items)" "(sold N …)"
    item("trade sell=junk (one call)", "PASS" if ok else "FAIL", f"{dt:.1f}s; gold {g0}→{g1}; {first_line(out)} {tail}")
    esc = call("press_key", key="ESCAPE")
    # [se] S057 07:40:25: camp() right after closing a trade failed "cannot do that during a conversation" ×2 (the
    # trader's goodbye line); camp now waits ≤ 6 s for a line with no dialogue window, then goes. Reloaded after.
    out, dt = took("camp")
    if "during a conversation" in out or "camp: done" in out or "at_camp" in out:
        ok = "during a conversation" not in out and "FAILED" not in out
        item("camp right after closing a trade (goodbye line)", "PASS" if ok else "FAIL",
             f"{first_line(out)[:200]} ({dt:.1f}s; ESCAPE before: {first_line(esc)[:60]})")
    else:
        item("camp right after closing a trade (goodbye line)", "SKIP", f"camp blocked otherwise: {first_line(out)[:200]}")
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
        plan = json.dumps({"move": {"target": fh, "within": 1.5}, "on_reaction": "skip", "end": True,
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
              and "FAILED: it is not" not in out and not range_miss and not lost_walk
              and "unknown plan key" not in out)  # act 2026-10-11: "end" inside a plan (S041 22:36:18)
        if "melee is out of reach" in out or "the plan now aims at" in out:
            log("round-2 lines: " + " | ".join(l for l in out.splitlines()
                                               if "melee is out of reach" in l or "now aims" in l or "skipped (" in l))
        item(f"combat_turn #{played} (melee approach, names, job push)", "PASS" if ok else "FAIL",
             f"{party[who_u].get('name')} {dt:.1f}s; {rpcs} RPCs ({jobs} act jobs, {polls} job.polls; push heard={jp.seen}); "
             f"moved={moved} attacked={hit} dash+walk={dashed_on} range_miss={range_miss} lost_walk={lost_walk}; vs {foe.get('name')}; "
             + " | ".join(l for l in out.splitlines()[:4]))
    if not played:
        item("combat_turn on a safe fight", "SKIP", "the fight ended before a party turn")
    melee_probe()
    load_verified(BASE)


MELEE_PROBE_M = (2.4, 2.1, 1.9, 1.6)  # centre-to-centre distances swung from, one party turn each


def melee_probe() -> None:
    """ACT-3 (S065 ×9 'first melee refused, no reason given'): the centre distance the engine accepts for a melee
    swing cast WithoutMove (use_ability in combat). Our guard lets one through up to weapon range + both Stand radii
    (2.5 m for two medium creatures); combat_turn's melee move stops at ≤2.1 m. One swing per party turn: walk to d m
    from the nearest visible hostile (on the line toward it), measure, swing, end the turn. Record-only (PASS when
    measured): the rows say where the engine's reach really ends; set G.radius / A.MELEE_STOP_SLACK from them."""
    from bg3ai import turn as T
    rows: list[str] = []
    todo = list(MELEE_PROBE_M)
    end = time.monotonic() + 150
    while todo and time.monotonic() < end:
        cs = S.game.try_rpc("state.combat") or {}
        if not cs.get("active"):
            break
        if ui_screen() == "reaction":
            call("react", use=False)
            continue
        party = {r.get("uuid"): r for r in status().get("party") or []}
        now = [u for u in T.acting(cs) if u in party and not party[u].get("summon")]
        if not now:
            time.sleep(0.5)
            continue
        who_u, wh = now[0], S.H.get(now[0])
        here = party[who_u].get("pos")
        nb = S.game.try_rpc("state.nearby", {"radius": 30}) or {}
        foes = [c for c in nb.get("characters") or [] if c.get("relation") == "hostile" and not c.get("dead")
                and c.get("pos")]
        if not here or not foes:
            call("end_turn", who=wh, wait=False)
            continue
        foe = min(foes, key=lambda c: sum((a - b) ** 2 for a, b in zip(c["pos"], here)))
        fp, fh = foe["pos"], S.H.get(foe["uuid"], foe.get("name"))
        d = todo[0]
        flat = ((here[0] - fp[0]) ** 2 + (here[2] - fp[2]) ** 2) ** 0.5 or 1.0
        spot = (fp[0] + (here[0] - fp[0]) * d / flat, here[1], fp[2] + (here[2] - fp[2]) * d / flat)
        call("move_to", x=spot[0], y=spot[1], z=spot[2], who=wh, wait=True)
        me = next((r.get("pos") for r in status().get("party") or [] if r.get("uuid") == who_u), None)
        nb2 = S.game.try_rpc("state.nearby", {"radius": 30}) or {}
        fp2 = next((c.get("pos") for c in nb2.get("characters") or [] if c.get("uuid") == foe["uuid"]), fp)
        dist = sum((a - b) ** 2 for a, b in zip(me or here, fp2)) ** 0.5
        out = call("use_ability", ability="Target_MainHandAttack", target=fh, who=wh)
        first = first_line(out)
        verdict = ("swung" if ": done" in first or "done —" in first else
                   "ENGINE-REFUSED" if "no reason given" in out else
                   "guard-refused" if "out of range" in out else "other: " + first[:80])
        rows.append(f"aimed {d}m measured {dist:.2f}m centre → {verdict}")
        log(f"melee probe: {rows[-1]} ({party[who_u].get('name')} vs {foe.get('name')})")
        todo.pop(0)
        call("end_turn", who=wh, wait=False)
    item("melee reach probe (engine-accepted centre distance, ACT-3)", "PASS" if rows else "SKIP",
         "; ".join(rows) if rows else "no party turn with a hostile in view")


def party_pos() -> dict:
    return {r.get("uuid"): r.get("pos") for r in status().get("party") or [] if r.get("pos")}


def moved_m(a: dict, b: dict) -> float:
    return max((sum((x - y) ** 2 for x, y in zip(a[u], b[u])) ** 0.5 for u in a if u in b), default=0.0)


def close_targeting() -> str:
    """Leave a targeting mode (or an accidental game menu) with ESCAPE, by ui.key: the guarded press_key refuses it."""
    for _ in range(3):
        S.game.try_rpc("ui.key", {"key": "ESCAPE"}, context="client")
        time.sleep(0.5)
        if ui_screen() != "game_menu":
            break
    return ui_screen()


def sec_se() -> None:
    """se overnight batch (2026-10-10 23:xx): click hover/unknown button, reactions on an unselected member, the
    game-process check of load_game, Noesis counters, and a read-only CursorText probe in the Jump targeting."""
    if not load_verified(BASE):
        return
    # patch 0009 (S060 09:11 crash): the first Ext.StaticData read finds the resource manager; a false positive at
    # region offset 0xeb5xx killed the game twice (`index < size_`). PASS = the read answers, the game lives, and the
    # manager the log names is the real one (0x15axxx / "recorded offset") with no crash after it.
    lg = bg3le_log()
    try:
        out = lua_server('local ok, all = pcall(Ext.StaticData.GetAll, "ActionResource") '
                         'return tostring(ok) .. " " .. tostring(ok and all and #all or all)')
    except Exception as e:  # noqa: BLE001
        out = f"eval error {e}"
    txt = lg.read_text(errors="replace") if lg else ""
    found = re.findall(r"static data: resource manager (?:at (0x[0-9a-f]+)(.*)|not found.*)", txt)
    where = found[-1] if found else None
    real = bool(where) and (where[0][-5:-3] == "5a" or "recorded offset" in where[1])
    crash = grep_crash(lg)
    item("static data manager is the real one (patch 0009)",
         "PASS" if alive() and out.startswith("true") and real and not crash else "FAIL",
         f"GetAll(ActionResource) → {out[:80]}; log: {('at ' + where[0] + where[1][:40]) if where else 'no find line'}"
         f" ({lg.name if lg else 'no log'}){'; ' + crash if crash else ''}")
    # click(button="none"): S039 21:47:42 it committed a left click (a 5 m jump under Z). 06:27 FAIL was the load's
    # settle (hover 2.3 s after load_game, `level_changed` in the same result; 0.0 m 1 s later): wait until the party
    # stands still, then judge the controlled character only (the one a click would move); followers are evidence.
    def still_party(max_s: float = 8.0) -> dict:
        prev, calm, end = party_pos(), 0, time.monotonic() + max_s
        while time.monotonic() < end and calm < 2:
            time.sleep(0.5)
            now = party_pos()
            calm = calm + 1 if moved_m(prev, now) <= 0.05 else 0
            prev = now
        return prev

    def per_member(a: dict, b: dict) -> str:
        names = {r.get("uuid"): r.get("name") for r in status().get("party") or []}
        return ", ".join(f"{names.get(u, u[:6])} {moved_m({u: a[u]}, {u: b[u]}):.1f}" for u in a if u in b)

    def hover_check(name: str, button: str, want: str) -> None:
        sel = status().get("active")
        p0 = still_party()
        out, dt = took("click", x=0.5, y=0.62, button=button)
        time.sleep(1.5)
        p1 = party_pos()
        mv = moved_m({sel: p0[sel]}, {sel: p1[sel]}) if sel in p0 and sel in p1 else moved_m(p0, p1)
        scr = ui_screen()
        ok = want in out and mv < 0.3 and scr == "game"
        item(name, "PASS" if ok else "FAIL", f"{first_line(out)} ({dt:.1f}s); controlled character moved {mv:.1f} m "
             f"(all: {per_member(p0, p1)}); screen={scr}")

    hover_check("click button=none hovers only", "none", "nothing clicked")
    hover_check("click unknown button refused", "middle", "unknown button")

    # Save name box read-back (S056 ×4, S057: every save_game FAILED "the save name box keeps reading 'ls.LSTextBox'":
    # the check read ToString(), a TextBox's class name). ui.menu save probe: open, set the name, read the Text
    # property back after the settle, ESCAPE out; nothing is written (checked on disk too).
    t = time.time()
    r = S.game.run_job("ui.menu", {"action": "save", "name": "acc-name-probe", "probe": True}, context="client", wait_s=25)
    time.sleep(1.0)
    res = r.result if isinstance(r.result, dict) else {}
    scr = ui_screen()
    stray = [d.name for d in S.SAVE_DIR.iterdir() if d.stat().st_mtime >= t] if S.SAVE_DIR.is_dir() else []
    ok = r.status == "done" and res.get("matches") is True and scr == "game" and not stray
    item("save name box reads our name (probe, no save)", "PASS" if ok else "FAIL",
         f"{r.status} {r.error or ''}; read={res.get('read')!r} matches={res.get('matches')} renamed={res.get('renamed')}; "
         f"screen after={scr}; save folders written={stray or 'none'}")

    # set_reactions on a member that is not selected (S041 22:41:22 "char is not the selected character")
    st = status()
    sel = st.get("active")
    other = next((r for r in st.get("party") or [] if r.get("uuid") != sel), None)
    if need(other is not None, "reactions on an unselected member", "party of one"):
        out, dt = took("set_reactions", who=other.get("name"))
        ok = "not the selected character" not in out and "FAILED" not in out and "EXCEPTION" not in out
        item("reactions on an unselected member", "PASS" if ok else "FAIL",
             f"{other.get('name')} (selected was {sel}): {first_line(out)[:160]} ({dt:.1f}s)")
        if sel:
            S.game.run_job("act.select", {"char": sel}, wait_s=5)

    # load_game's crash fast path: the 60 MB Steam-runtime parent must not count, the game must
    from bg3ai import metrics as MX
    pid = MX.game_pid()
    rss = MX.rss_mb(pid)
    alive_now = S._game_process_alive()
    item("load_game game-process check", "PASS" if alive_now and (rss or 0) >= S.GAME_ALIVE_MIN_MB else "FAIL",
         f"_game_process_alive={alive_now}; biggest bg3 pid={pid} rss={rss} MB (min {S.GAME_ALIVE_MIN_MB})")

    # Noesis parked/leaked: sys.mem (client) + the bg3le minute line
    m = S.game.try_rpc("sys.mem", context="client") or {}
    nz = m.get("noesis") if isinstance(m, dict) else None
    lg = bg3le_log()
    line = ""
    for _ in range(14):  # the minute line: up to ~70 s
        txt = lg.read_text(errors="replace") if lg else ""
        hits = re.findall(r".*mem: client lua .*\| noesis gen.*", txt)
        if hits:
            line = hits[-1]
            break
        time.sleep(5)
    ok = isinstance(nz, dict) and isinstance(nz.get("parked"), int) and bool(line)
    item("Noesis parked observable", "PASS" if ok else "FAIL",
         f"sys.mem noesis={nz}; log: {line.strip()[:140] or 'no minute line with noesis (' + (lg.name if lg else 'no log') + ')'}")

    # CursorText probe for the hand-aimed jumps item (BACKLOG [se] M): what does the game show next to the cursor
    # while the Jump targeting (Z) hovers a near and a far point? Read-only: hover, read, ESCAPE; nothing is cast.
    # 01:06 run: CursorText had no text items and the props log was cut at 300 chars → now: every visible widget's
    # texts before Z vs while hovering (whatever widget holds "Too far"/"Invalid" shows up as NEW), and the full
    # CursorText props one level down, unshortened, in se.log.
    def texts() -> dict:
        d = S.game.try_rpc("ui.dump", {"text_only": True, "max": 400}, context="client") or {}
        return {w.get("w"): [x.get("t") for x in w.get("items") or [] if isinstance(x, dict) and x.get("t")]
                for w in (d.get("widgets") or []) if isinstance(w, dict)}

    def cursor_props() -> dict:
        pr = S.game.try_rpc("ui.props", {"widget": "CursorText", "commands": True}, context="client") or {}
        sub = {}
        for k in list((pr.get("objects") or {}) if isinstance(pr, dict) else {})[:15]:
            sub[k] = S.game.try_rpc("ui.props", {"widget": "CursorText", "path": k}, context="client")
        return {"props": pr, "objects": sub}

    # 04:30 run: IsScreenFadeActive was still true right after the load: wait for the fade, else Z may do nothing.
    def player_vals() -> dict:
        out = {}
        # 09:0x read-only probe in S059: CurrentPlayer.UIData.Cursor (Distance, CursorObscuredState, …) and
        # UIData.CastInfo (a collection, empty outside targeting) are where a targeting verdict could live.
        for path in ("CurrentPlayer", "CurrentPlayer.SelectedCharacter", "CurrentPlayer.UIData",
                     "CurrentPlayer.UIData.Cursor", "CurrentPlayer.UIData.CastInfo.1", "CurrentPlayer.UIData.HitChanceDesc"):
            pr = S.game.try_rpc("ui.props", {"widget": "CursorText", "path": path}, context="client")
            for k, v in ((pr or {}).get("values") or {}).items() if isinstance(pr, dict) else []:
                out[f"{path}.{k}"] = v
        return out

    for _ in range(20):
        if not player_vals().get("CurrentPlayer.IsScreenFadeActive"):
            break
        time.sleep(0.5)
    before = texts()
    vals0 = player_vals()
    call("press_key", key="Z")
    time.sleep(0.8)
    vals1 = player_vals()
    changed = {k: [vals0.get(k), v] for k, v in vals1.items() if vals0.get(k) != v}
    if not changed:
        # 06:27: press_key Z (the mod's InjectKeyPress) changed nothing: try the real SDL key through the input shim
        try:
            S._inp().key("Z")
            time.sleep(0.8)
            vals1 = player_vals()
            changed = {k: [vals0.get(k), v] for k, v in vals1.items() if vals0.get(k) != v}
            log(f"PROBE shim key Z: changed {json.dumps(changed)}")
        except Exception as e:  # noqa: BLE001
            log(f"PROBE shim key Z failed: {e}")
    log("PROBE ui.state after Z (is the Jump targeting on?): "
        + json.dumps(S.game.try_rpc("ui.state", context="client"))[:1500])
    log(f"PROBE player/selected values changed by Z: {json.dumps(changed)}")
    new_texts = []
    for x, y in ((0.55, 0.6), (0.9, 0.2)):
        call("click", x=x, y=y, button="none")
        time.sleep(0.6)
        now = texts()
        fresh = {w: [t for t in ts if t not in (before.get(w) or [])] for w, ts in now.items()}
        fresh = {w: ts for w, ts in fresh.items() if ts}
        new_texts.append(f"@{x},{y}: {json.dumps(fresh)[:400]}")
        log(f"PROBE new widget texts while targeting @{x},{y}: {json.dumps(fresh)}")
        try:  # what a person sees there (is the jump arc / cursor text on?)
            img = getattr(S.screenshot, "fn", S.screenshot)(scale=0.5)
            shot = OUT / f"se-jump-probe-{x}-{y}.jpg"
            shot.write_bytes(img.data)
            log(f"PROBE screenshot {shot}")
        except Exception as e:  # noqa: BLE001
            log(f"PROBE screenshot failed: {e}")
        hv = player_vals()
        log(f"PROBE UIData/Cursor/CastInfo values changed while hovering @{x},{y}: "
            + json.dumps({k: [vals1.get(k), v] for k, v in hv.items() if vals1.get(k) != v}))
        log(f"PROBE CursorText props @{x},{y}: {json.dumps(cursor_props())}")
        log(f"PROBE CursorText dump @{x},{y}: " + json.dumps(S.game.try_rpc(
            "ui.dump", {"root": "CursorText", "hidden": True, "text_only": False, "depth": 30, "max": 300},
            context="client")))
    p0 = party_pos()
    after = close_targeting()
    time.sleep(1.0)
    mv = moved_m(p0, party_pos())
    got = any(t.split(": ", 1)[1] != "{}" for t in new_texts)
    item("CursorText probe (Jump targeting)", "PASS" if got and mv < 0.5 else ("FAIL" if mv >= 0.5 else "INFO"),
         f"new texts while targeting={got}; party moved {mv:.1f} m; screen after={after}; " + " | ".join(new_texts)[:220]
         + " (full: se.log PROBE lines)")
    load_verified(BASE)
    # Lockpick on the S050 door (S050 04:11-04:12 + S051: 8x "hover_miss hovered_instead=[(an object with no id)]";
    # S047 02:57: "nothing happened" while the game walked the actor to its lockpick spot). Opens the roll only:
    # ESCAPE, then the base save is reloaded; nothing is rolled or saved.
    if load_verified(DOOR_SAVE):
        # S055 06:46:20 Lower City: "the map didn't open (pressed M,M,M,M,M over 6.2s)" (the Act 3 map shows slowly;
        # the 1.2 s re-press closed it) + check_all "screen map after closing the map (left open?)".
        t = time.monotonic()
        r = S.game.run_job("ui.waypoints", {}, context="client", wait_s=20)
        dt = time.monotonic() - t
        time.sleep(1.5)
        res = r.result if isinstance(r.result, dict) else {}
        scr = ui_screen()
        n = len(res.get("waypoints") or [])
        ok = r.status == "done" and n > 0 and res.get("map_presses") == 1 and scr == "game"
        item("map opens once in the Lower City (slow map)", "PASS" if ok else "FAIL",
             f"{r.status} {r.error or ''} in {dt:.1f}s; waypoints={n} map_presses={res.get('map_presses')}; "
             f"screen 1.5 s after closing={scr}")
        # screenshot(map=true) (S055 x2, S056 x3: M + screenshot + ESCAPE by hand): one call opens, shoots, closes.
        # Judged on the map (opened, closed after); a frame that doesn't present (08:0x: the acceptance window's
        # screenshots failed) is reported, not failed.
        t = time.monotonic()
        try:
            img = getattr(S.screenshot, "fn", S.screenshot)(scale=0.25, map=True)
            shot = "map image" if not isinstance(img, str) else img
        except Exception as e:  # noqa: BLE001
            shot = f"capture error: {e}"
        dt = time.monotonic() - t
        time.sleep(1.0)
        scr = ui_screen()
        if isinstance(shot, str) and "didn't open" in shot:
            item("screenshot(map=true) opens + closes the map", "FAIL", f"{shot[:200]} ({dt:.1f}s)")
        else:
            item("screenshot(map=true) opens + closes the map", "PASS" if scr == "game" else "FAIL",
                 f"{shot[:160]} in {dt:.1f}s; screen 1 s after={scr} (game = closed again)")
        near = call("get_nearby", kinds="door", radius=20)
        # S054 05:54:51: no open/closed on doors → interact closed an open door (a scene started). State.lua: doors
        # carry open/closed (Osi.IsOpened); the locked Foundry door must read "closed".
        drows = re.findall(r"^.*<door>.*$", near, re.M)
        ok = bool(drows) and all(re.search(r"\b(open|closed)\b", r) for r in drows)
        item("doors show open/closed in get_nearby", "PASS" if ok else ("SKIP" if not drows else "FAIL"),
             " | ".join(drows)[:240] or "no door within 20 m")
        doors = re.findall(r"^(?:ITEM\s+|\+\s*)?(e\d+) ([^<\n]+?) <door>[^\n]*\blocked\b", near, re.M)  # "ITEM e8 Steel … <door> 1.6m locked"
        if need(bool(doors), "lockpick a door (no-id hover, walk wait)", f"no locked door within 20 m of {DOOR_SAVE}"):
            h, name = doors[0]
            out, dt = took("lockpick", target=h)
            ok = "opened=roll" in out or "unlocked" in out
            ev = (f"{h} {name.strip()}: {first_line(out)[:200]} ({dt:.1f}s) "
                  + grab(r"hover_via=\w+|no_id_objects=\[[^\]]*\]|walked_m=[\d.]+", out))
            item("lockpick a door (no-id hover, walk wait)", "PASS" if ok else "FAIL", ev)
            if ui_screen() == "roll":
                call("press_key", key="ESCAPE")
        load_verified(BASE)



FUNCS = {"reads": sec_reads, "rest": sec_rest, "trade": sec_trade, "gear": sec_gear, "swap": sec_swap,
         "nav": sec_nav, "loads": sec_loads, "combat": sec_combat, "se": sec_se}


def sec_setup() -> None:
    """setup's fixes (impl-setup parts 34-35, 41) on the loaded base save: ramwatch's anon+swap kill
    signal, the story scorer naming a new journal step by its text, enemy statuses as lesson terms."""
    sys.path.insert(0, str(REPO / "runner"))
    import ramwatch
    from bg3ai import lessons, progress
    # 1. ramwatch: the watchdog that holds runtime/ramwatch.lock writes anon+swap (else it runs old code)
    pids = ramwatch.game_pids()
    a_s, rss = sum(ramwatch.anon_swap_mb(p) for p in pids), sum(ramwatch.rss_mb(p) for p in pids)
    try:
        last = (REPO / "runtime" / "ram.log").read_text(errors="replace").strip().splitlines()[-1]
    except (OSError, IndexError):
        last = ""
    fresh = "anon+swap=" in last and time.time() - os.path.getmtime(REPO / "runtime" / "ram.log") < 180
    item("ramwatch anon+swap kill signal", "PASS" if (a_s > 1000 and fresh) else "FAIL",
         f"game pids {pids}: anon+swap {a_s} MB, RSS {rss} MB (kill at {ramwatch.KILL_ANON_SWAP_MB}); ram.log: {last[-110:]}")
    if not load_verified(BASE):
        return
    # 2. scorer: drop one journal step from a baseline built on the live journal → that step's text is the event
    progress._events["seq"] = None
    got = progress.read_game(lambda code, ctx: S.game.eval(code, context=ctx))
    quests = (got or {}).get("journal")
    qs = quests.get("quests", quests) if isinstance(quests, dict) else quests
    q = next((q for q in qs or [] if isinstance(q, dict) and len(q.get("steps") or []) >= 3), None)
    if need(q is not None, "scorer names the new journal step", "no quest with 3+ steps in the live journal"):
        import copy
        older = copy.deepcopy(got)
        oqs = [x for x in (older["journal"].get("quests", older["journal"]) if isinstance(older["journal"], dict)
                           else older["journal"]) if isinstance(x, dict) and x.get("title") == q["title"]]
        # the quest's FIRST step (a count-based diff would name the last one), taken out of every quest with this
        # title (Daughter of Darkness is two journal quests: the party's and Shadowheart's own)
        gone = q["steps"][0]
        for oq in oqs:
            oq["steps"] = [x for x in oq["steps"] if progress._step_key(str(x)) != progress._step_key(str(gone))]
        seen, _ = progress.diff(None, older)
        _, evs = progress.diff(seen, got)
        whats = [e["what"] for e in evs if e["kind"] == "quest_step"]
        want = f"{q['title']}: {progress._step_key(str(gone))}"
        item("scorer names the new journal step", "PASS" if whats == [want] else "FAIL",
             f"removed step 1 of '{q['title']}' ({len(q['steps'])} steps, {len(oqs)} quest(s) with that title); "
             f"events {whats[:3]}")
    # 4. temporary followers (S053 Zanner Toobin): the flag never marks the permanent party (it would stop equip_best
    #    dressing them); a follower, if one is in the party, carries it in both reads
    inv = S.game.try_rpc("state.inventory", {}) or {}
    rows = [r for r in status().get("party") or [] if isinstance(r, dict) and not r.get("summon")]
    flagged = [r.get("name") for r in rows if r.get("follower")]
    mem_flag = [u for u, m in (inv.get("members") or {}).items() if (m or {}).get("follower")]
    origins = {"Claude", "Shadowheart", "Karlach", "Wyll", "Gale", "Astarion", "Lae'zel", "Halsin", "Minthara",
               "Jaheira", "Minsc"}
    wrong = [n for n in flagged if n in origins]
    item("temporary followers flagged, the party not", "PASS" if not wrong and len(mem_flag) == len(flagged) else "FAIL",
         f"party {[r.get('name') for r in rows]}; follower-flagged {flagged or 'none'} (members read {len(mem_flag)})")
    # 3. lessons: nearby characters' status names reach the lesson terms (a trigger with a plural/past ending)
    nb = S.game.try_rpc("state.nearby", {"kinds": ["characters"], "radius": 40}) or {}
    rows = [c for c in nb.get("characters") or [] if c.get("statuses")]
    if need(bool(rows), "enemy statuses as lesson terms", "no character with a status in view at the base save"):
        c = rows[0]
        sname = next(x.get("name") for x in c["statuses"] if x.get("name"))
        enemies = S._with_statuses([{"uuid": c["uuid"], "name": c.get("name"), "relation": "hostile"}])
        tmp = OUT / "rt" / "lessons-accept.md"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        trig = re.sub(r"(ed|es|s)$", "", sname) if len(sname) > 5 else sname   # 'Mirrored' → 'Mirror'
        tmp.write_text(f"- WHEN {trig}: acceptance → status term (S999) [seen]\n")
        out = lessons.Store(tmp).fight_lines(enemies, [])
        item("enemy statuses as lesson terms", "PASS" if "acceptance" in out else "FAIL",
             f"{c.get('name')} statuses {enemies[0]['statuses'][:4]}; trigger '{trig}' → {first_line(out) or 'no LESSONS'}")

    # 5. set_party: unchaining a member who already walks alone is done, not FAILED (S062 ×3); chained back after
    rows = [r for r in status().get("party") or [] if isinstance(r, dict) and not r.get("summon") and not r.get("avatar")
            and not r.get("dead")]
    if need(bool(rows), "set_party unchain twice = done", "no living companion in the party"):
        who = rows[0].get("name")
        first, again = call("set_party", action="unchain", who=who), call("set_party", action="unchain", who=who)
        back = call("set_party", action="chain", who=who)
        item("set_party unchain twice = done", "PASS" if again.startswith("set_party: done") and "already" in again
             else "FAIL", f"{who}: 1st {first_line(first)[:80]} | 2nd {first_line(again)[:80]} | chain {first_line(back)[:60]}")
    # 6. level_up selects the member first (S062): the helper makes a companion the controlled one; given back after
    if rows:
        who = rows[0].get("uuid")
        prev = S._active()
        got = S._select_for_level_up(who)
        now = S._active()
        S.game.run_job("act.select", {"char": prev}, wait_s=5)
        time.sleep(0.3)
        item("level_up selects the member first", "PASS" if got == prev and now == who and S._active() == prev
             else "FAIL", f"active {prev} → helper returned {got}, active then {now} (want {who}), back {S._active()}")


FUNCS["setup"] = sec_setup


def lua_server(code: str) -> str:
    return str(S.game.eval(code, context="server"))


def sec_act() -> None:
    """act overnight batch (2026-10-10/11): the hot-reloaded Lua is live (jump FELL report, CastSpellFailed reason,
    missing ranged weapon named, one-decimal range refusal), react by name, swap_party's camp block answer,
    Revivify's two-step spot."""
    if not load_verified(BASE):
        return
    g = "local G=Mods.BG3AI.BG3AI.Guard local A=Mods.BG3AI.BG3AI.Actions "
    # jump fall/death report + CastSpellFailed reason (S038 20:44:25 fall "done"; S024-S041 "the action failed")
    v = lua_server(g + 'return tostring(A.JUMP_FALL_M).."|"..tostring(A.cast_failed_why and '
                   'A.cast_failed_why(Osi.GetHostCharacter(), nil))')
    item("act Lua live: jump FELL + cast refused reason",
         "PASS" if v.startswith("4") and "the game refused the cast" in v else "FAIL", v[:200])
    # Guard.within: one decimal when the distance rounds to the max (S028 "40m …, max 40m"). G.pos is wrapped for
    # the call only, so a synthetic 40.3 m target needs no walk.
    v = lua_server(g + 'local h=Osi.GetHostCharacter() local real=G.pos '
                   'G.pos=function(u) local p=real(h) if u=="__far" then return {p[1]+40.3,p[2],p[3]} end return real(u) end '
                   'local ok,e=pcall(G.within,h,"__far",40,"target") G.pos=real '
                   'return ok and "no refusal" or (G.is_refusal(e) and e.reason or tostring(e))')
    item("range refusal with one decimal at the max", "PASS" if "(40.3m from this character, max 40m)" in v else "FAIL",
         v[:200])
    # a missing Ranged Attack names the missing ranged weapon (S031 17:04 Gale)
    rows = []
    for r in status().get("party") or []:
        u = r.get("uuid")
        v = lua_server(g + f'local ok,e=pcall(G.known_spell,"{u}","Projectile_MainHandAttack") '
                       'return ok and "has it" or (G.is_refusal(e) and e.reason or tostring(e))')
        rows.append((r.get("name"), v))
    missing = [(n, v) for n, v in rows if v != "has it"]
    if not missing:
        item("missing ranged weapon named", "SKIP", "every member has a ranged weapon: " + ", ".join(n for n, _ in rows))
    else:
        item("missing ranged weapon named", "PASS" if all("no ranged weapon equipped" in v for _, v in missing)
             else "FAIL", "; ".join(f"{n}: {v[:90]}" for n, v in missing))
    # react by name with no prompt: answers at once, no Lua error (S034 19:03:14 react(which="Shield"))
    out, dt = took("react", use=True, which="Shield")
    item("react by name", "PASS" if "no reaction prompt on screen" in out and dt < 3 else "FAIL",
         f"{first_line(out)} ({dt:.1f}s)")
    # a name by one whole word ("Aylin" for "Dame Aylin", S045 00:15) resolves
    nb = S.game.try_rpc("state.nearby", {"radius": 40, "kinds": ["characters"]}) or {}
    two = [c for c in nb.get("characters") or [] if len(str(c.get("name") or "").split()) >= 2]
    if not two:
        item("name by a whole word", "SKIP", "no character with a two-word name in view")
    else:
        c = two[0]
        word = str(c["name"]).split()[-1].strip("'")
        out = call("look_at", target=word)
        item("name by a whole word", "PASS" if "unknown target" not in out else "FAIL",
             f"look_at({word!r}) for {c['name']!r}: {first_line(out)[:160]}")
    # Revivify is two-step (S057/S058 0 of 14): the live stats say so, and the queued UseSpell task takes the
    # ground spot (TargetPos2). Probe on a living companion: the task is queued, given its spot, then purged.
    v = lua_server(g + 'local h=Osi.GetHostCharacter() local o '
                   'for _,r in pairs(Osi.DB_PartyMembers:Get(nil)) do if not G.same(r[1],h) then o=G.guid(r[1]) break end end '
                   'if not A.two_step_spell then return "no A.two_step_spell" end '
                   'if not A.two_step_spell("Teleportation_Revivify") then return "stats: not two-step" end '
                   'if not o then return "no companion" end '
                   'Osi.UseSpell(h,"Teleportation_Revivify",o,"NULL_00000000-0000-0000-0000-000000000000",1) '
                   'local ok,why=A.set_task_pos2(h,"Teleportation_Revivify",A.place_spot(h,o)) '
                   'Osi.PurgeOsirisQueue(h) return ok and "set" or ("not set: "..tostring(why))')
    item("revivify two-step spot set", "PASS" if v == "set" else "FAIL", v[:200])
    # Dimension Door is a two-step teleport (S059 x3 refused): who goes (the caster / an ally within 2 m), then the
    # landing (AreaRadius). Live stats agree, and a landing in the air above the host is refused with a reason.
    v = lua_server(g + 'local h=Osi.GetHostCharacter() local sp="Teleportation_DimensionDoor" '
                   'if not A.landing_ok then return "no A.landing_ok" end '
                   'if not A.two_step_spell(sp) then return "stats: not two-step" end '
                   'if A.revive_spell(sp) then return "stats: read as a revive" end '
                   'local r=A.landing_range(sp) local p=G.pos(h) '
                   'local ok,e=pcall(A.landing_ok,h,sp,{p[1],p[2]+30,p[3]}) '
                   'return "range="..tostring(r).." air="..(ok and "accepted" or (G.is_refusal(e) and e.reason or tostring(e)))')
    item("dimension door two-step landing", "PASS" if v.startswith("range=") and "air=accepted" not in v
         and "range=nil" not in v else "FAIL", v[:200])
    # swap_party where the camp button is greyed by the area: the reason at once (S039 21:48:34)
    b = S.game.try_rpc("ui.travel_block", context="client") or {}
    if b.get("error") != "Location":
        item("swap_party camp block answers at once", "SKIP", f"camp travel allowed here (travel_block={b})")
    else:
        camp = [c.get("name") for c in status().get("camp") or []]
        party = [r.get("name") for r in status().get("party") or [] if not r.get("avatar")]
        out, dt = took("swap_party", into=camp[0] if camp else "Gale", out=party[-1] if party else "")
        item("swap_party camp block answers at once",
             "PASS" if "(Location)" in out and dt < 3 else "FAIL", f"{first_line(out)[:200]} ({dt:.1f}s)")
    load_verified(BASE)


FUNCS["act"] = sec_act


def sec_vis() -> None:
    """vis overnight batch (2026-10-10/11), read-only on the base save (no jump is cast): the drop extends the jump
    reach, a climb of 10 m is refused, Feather Fall known, route to a point with no floor says why, o1 as a target,
    an empty name search lists who is in view, the journal lists open quests the game never 'accepted'."""
    if not load_verified(BASE):
        return
    g = "local G=Mods.BG3AI.BG3AI.Guard local N=Mods.BG3AI.BG3AI.Nav local h=Osi.GetHostCharacter() "
    # 1. high-ground jump reach (S040: the game's jumps 18-28 m over 18-23 m drops; ours capped at 8.5 m)
    v = lua_server(g + 'local p=G.pos(h) local r,flat=G.jump_reach(h,p,{p[1],p[2]-10,p[3]}) '
                   'return string.format("%.1f|%.1f|%s", r, flat, tostring(G.HIGH_GROUND_JUMP_MULT))')
    try:
        r, flat, mult = v.split("|")
        ok = abs(float(r) - float(flat) - 10) < 0.2
    except ValueError:
        ok = False
    item("vis: jump reach grows with the drop", "PASS" if ok else "FAIL", f"reach|flat|mult for a 10 m drop = {v}")
    # 2. a landing 10 m above the jumper is refused by the Guard (checked through check_cast, nothing is cast)
    v = lua_server(g + 'local p=G.pos(h) local ok,e=pcall(G.check_cast,h,"Projectile_Jump",nil,{p[1]+2,p[2]+10,p[3]},nil,true) '
                   'return ok and "no refusal" or (G.is_refusal(e) and e.reason or tostring(e))')
    item("vis: jump 10 m up refused", "PASS" if v != "no refusal" and "attempt to" not in v else "FAIL", v[:200])
    # 3. Feather Fall read (FEATHER_FALL; true only in the Shadowfell or under the spell)
    v = lua_server(g + 'return tostring(N.featherFall(h)).."|"..tostring(N.safeDrop(h))')
    item("vis: Feather Fall status read", "PASS" if re.fullmatch(r"(true\|60(\.0)?|false\|4(\.0)?)", v) else "FAIL", v)
    # 4. route to a point with no floor near it: NO WALKABLE PATH says why (S039 21:44 "pathfinding unavailable")
    p = (status().get("party") or [{}])[0].get("pos") or [0, 0, 0]
    out, dt = took("route", x=p[0] + 4, y=p[1] + 60, z=p[2])
    ok = "NO WALKABLE PATH: no" in out
    item("vis: route to a point in the air says why", "PASS" if ok else "FAIL", f"{dt:.1f}s; {out[:200]}")
    # 5. an objective handle as a target (S041 talk_to(npc="o1"))
    call("get_map")
    try:
        u = S._uuid("o1")
        item("vis: o1 as a target", "PASS", f"o1 -> {S.H.get(u)} ({S._name_of(u) if hasattr(S, '_name_of') else u})")
    except ValueError as e:
        item("vis: o1 as a target", "PASS" if "objective marker" in str(e) or "no objective o1" in str(e) else "FAIL",
             str(e)[:200])
    # 6. an empty name search names what was asked and who is in view (S042 kinds="Jaheira,characters")
    out = call("get_nearby", radius=60, kinds="Zzyzxq,characters")
    item("vis: empty name search says so", "PASS" if "No 'zzyzxq' in view within 60m" in out else "FAIL", out[:200])
    # 6b. no level ids in get_explored / look_at (S040 `level=SCL_Main_A`, S046 look_at `region=SCL_Main_A`)
    ex = call("get_explored")
    me_id = S.H.get((status().get("party") or [{}])[0].get("uuid") or "", None)
    la = call("look_at", target=me_id) if me_id else ""
    ids = sorted(set(re.findall(r"\b[A-Z]{3}_Main(?:_A)?\b|\bTUT_Avernus_C\b", ex + la)))
    item("vis: no level ids in get_explored / look_at", "PASS" if not ids and ex else "FAIL",
         f"ids={ids}; explored: {first_line(ex)[:120]}; look_at: {first_line(la)[:80]}")
    # 6c. the nav kill list: the current level not on it unless a jump really crashed the game (04:39 relaunch
    #     false positive on CTY_Main_A); nav.route in budget on an unreachable goal far below (check_all 03:50 258 ms)
    lg = S.game.try_rpc("nav.level_guard", {}) or {}
    here = lg.get("here")
    item("vis: current level not on the nav kill list", "PASS" if not here or "jump" in str(here.get("why")) else "FAIL",
         f"level={lg.get('level')} here={here} slow={list((lg.get('slow') or {}) or [])}")
    p = (status().get("party") or [{}])[0].get("pos") or [0, 0, 0]
    r = S.game.try_rpc("nav.route", {"pos": [p[0] + 60, p[1] - 40, p[2]], "connectors": True}) or {}
    ms = r.get("ms") or 0
    item("vis: nav.route on a far-below goal in budget", "PASS" if ms < 150 else "FAIL",
         f"{ms} ms; reachable={r.get('reachable')} lower={'yes' if r.get('lower') else 'no'} skipped={r.get('lower_skipped')}")
    # 7. journal: open quests without QuestIsAccepted (Find the Nightsong, companion quests) are listed
    jt = call("get_journal")  # also loads State.quests
    v = lua_server('local who=Osi.GetHostCharacter() local n,names=0,{} '
                   'for _,q in ipairs(Mods.BG3AI.BG3AI.State.quests or {}) do '
                   '  if q.visible and Osi.QuestIsAccepted(who,q.id)~=1 and Osi.QuestIsClosed(q.id)~=1 then '
                   '    for _,st in ipairs(q.steps) do if st.id and Mods.BG3AI.BG3AI.State.stepUnlocked(who,q,st.id) then '
                   '      n=n+1 names[#names+1]=Ext.Loca.GetTranslatedString(q.title) or q.id break end end end end '
                   'return n.."|"..table.concat(names,";")')
    n, _, names = v.partition("|")
    if need(n.isdigit() and int(n) > 0, "vis: journal lists unaccepted open quests",
            f"no open quest without QuestIsAccepted at the base save ({v[:120]})"):
        first = names.split(";")[0]
        item("vis: journal lists unaccepted open quests", "PASS" if first and first in jt else "FAIL",
             f"{n} such quests ({names[:120]}); '{first}' in get_journal: {first in jt}")
    # 8. wedged (S058 08:00, stuck in a nook after a pick_up): on open ground nav.wedged says no; with every path
    #    refused (N.path stubbed for one call, restored at once) it names a jump spot next to a party member
    w = S.game.try_rpc("nav.wedged", {}) or {}
    v = lua_server(g + 'local P=N.path N.path=function() return {reachable=false} end '
                   'local ok,w=pcall(N.wedged,h,500) N.path=P '
                   'if not ok then return "error "..tostring(w) end if not w then return "nil" end '
                   'return tostring(w.wedged).."|"..tostring(w.near).."|"..tostring(w.dist_m).."|"..tostring(w.in_reach)')
    parts = v.split("|")
    ok = w.get("wedged") is False and parts[0] == "true"  # near=nil only when no member stands within 12 m
    item("vis: wedged detection (open ground no; all paths refused -> jump next to a member)",
         "PASS" if ok else "FAIL", f"open ground: {w}; stubbed: {v[:160]}")
    # 9. look_at on a door/container listed a moment ago and now out of sight (S055 e1174, S056 e603): examinable,
    #    out_of_sight (V.itemVisible stubbed false for one call, restored at once)
    fr = call("get_frontier")
    ids = re.findall(r"\b(e\d+) [^<\n]*<(?:door|container)>", fr)
    if need(bool(ids), "vis: look_at on a door seen a moment ago", "no door/container in get_frontier at the base save"):
        u = S._uuid(ids[0])
        v = lua_server('local B=Mods.BG3AI.BG3AI local V=B.Visibility local f=V.itemVisible '
                       'V.itemVisible=function() return false end '
                       f'local ok,o=pcall(B.RPC.ops["state.entity"],{{uuid="{u}"}}) V.itemVisible=f '
                       'if not ok then return "error "..tostring(o) end return "out_of_sight="..tostring(o.out_of_sight)')
        item("vis: look_at on a door seen a moment ago", "PASS" if v == "out_of_sight=true" else "FAIL",
             f"{ids[0]}: {v[:160]}")
    # 10. objective ids survive a re-sort (S059 08:53 route(objective="o3") walked to another objective): the live
    #     markers fetched, then the same list in reverse order (as after the party moved): o<first> is still it
    ms = S._objective_markers()
    if need(len(ms) >= 2, "vis: objective ids survive a re-sort", f"{len(ms)} objective markers at the base save"):
        oid, label = ms[0].get("oid"), S.render.objective_label(ms[0])
        S._set_objectives(list(reversed([{k: v for k, v in m.items() if k != "oid"} for m in ms])))
        got = S.render.objective_label(S.render.find_objective(S._objectives["last"], f"o{oid}"))
        item("vis: objective ids survive a re-sort", "PASS" if got == label else "FAIL",
             f"o{oid} = {label!r} before, {got!r} after the reverse; ids {[m.get('oid') for m in S._objectives['last']]}")
    # 11. pulled-up ladder (S059 08:56 e683, foot 5.7 m above the floor): N.ladderRaised reads the floor under a foot
    #     6 m above the party's feet as raised ~6 m and the party's own spot as not raised; no ladder in view whose
    #     foot a walk reaches is flagged (false positive)
    v = lua_server(g + 'local p=G.pos(h) return tostring(N.ladderRaised({p[1],p[2]+6,p[3]}))'
                   '.."|"..tostring(N.ladderRaised(p))')
    up, _, here = v.partition("|")
    try:
        ok = abs(float(up) - 6) <= 0.7 and here == "nil"
    except ValueError:
        ok = False
    fr = S.game.try_rpc("nav.frontier", {"ways": False, "hazards": False, "radius": 40}) or {}
    bad = []
    for l in fr.get("leads") or []:
        if l.get("kind") == "ladder" and l.get("raised_m"):
            r = S.game.try_rpc("nav.path", {"pos": l.get("pos")}) or {}  # the foot itself
            if r.get("reachable") is True:
                bad.append(f"{l.get('name')} raised {l['raised_m']} but walkable")
    lads = [l for l in fr.get("leads") or [] if l.get("kind") == "ladder"]
    item("vis: pulled-up ladder read (foot above the floor)", "PASS" if ok and not bad else "FAIL",
         f"6 m up -> {up}, here -> {here}; ladders in view {len(lads)}, false raised: {bad}")
    # 12. a name search finds party members (S057 07:49 / S058 07:52 kinds="Shadowheart,Wyll" → "No 'shadowheart'"
    #     while both lay dead nearby): the last party member's name → a PARTY line with its id
    rows = status().get("party") or []
    nm = (rows[-1].get("name") or "") if rows else ""
    if need(bool(nm), "vis: name search finds party members", "no party in state.status"):
        out = call("get_nearby", radius=25, kinds=nm)
        item("vis: name search finds party members", "PASS" if f"PARTY " in out and nm in out else "FAIL", out[:200])
    load_verified(BASE)


FUNCS["vis"] = sec_vis


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
