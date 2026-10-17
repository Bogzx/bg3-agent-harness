"""Acceptance run before play resumes: every scriptable DONE item of docs/BACKLOG.md, live, PASS/FAIL/SKIP per item.

    uv run --project bridge python tools/dev/acceptance.py                  # every section, one game.lock hold each
    uv run --project bridge python tools/dev/acceptance.py --only rest,trade
    uv run --project bridge python tools/dev/acceptance.py --list             # sections, order, estimated time
    uv run --project bridge --with lz4 --with zstandard python tools/dev/acceptance.py --check   # offline, no game

Each section runs in its own process under `flock -w 1800 runtime/game.lock` (other workers test between
sections), loads its own save first and verifies the load (a server Lua marker set before the load must be gone
after it, the party must be readable and the screen the game view), then reloads that save at the end, so story
state never changes. Saves are only loaded (S027-start, S026-dammon-heat, S050-end), never written; the bridge runs with a
scratch runtime and a scratch memory dir (note() writes there). Results: runtime/acceptance/<section>.json + .log,
and the table printed at the end (also runtime/acceptance/table.md). Exit code = number of FAILs.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
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
# The hotbar cast items (ACT-8 Bless x3, ACT-9 Dimension Door): BASE has no Bless on anyone's hotbar (live window #4
# probe, 2026-10-17: Shadowheart has Healing Word / Blessing of the Trickster, Claude only smites). In S065 Claude the
# Paladin (L10) cast Target_Bless and hit "a cast takes at most 2 here": the case ACT-8 fixes.
HOTBAR_SAVE = "S065-end"
# V10 (S069 20:34-20:36): Mystra's statue (Stormshore Tabernacle) never in get_nearby, interact "not visible"; saved
# right after Gale's meeting, the party within 2 m of it
MYSTRA_SAVE = "S069-mystra"
# A9: Karlach carries a Scroll of Misty Step here (S066 #143 read it on herself, then reloaded to get it back)
SCROLL_SAVE = "S066-end"
# Run order (se 2026-10-16): read-only sections first, then the ones that change the world (each reloads its save at
# the end), then the crash guard's reloads, then se, whose last items spend a spell slot (hotbar Bless) and switch
# bg3le's strict frame lock on and back: a crash there costs no other section. Every section loads its own save first,
# so a section that died half-way leaves nothing behind but process-wide state (the strict lock: restored in a
# finally; a nav kill list entry: cleared in a finally). strict (S17) runs last: strict on across 2 loads + a long
# rest, restored in a finally.
SECTIONS = ["reads", "setup", "act", "vis", "gear", "swap", "rest", "trade", "nav", "combat", "loads", "se", "strict"]
# Estimated seconds per section (the 2026-10-11/13 full runs on the test machine: max seen, + today's new items), for the plan.
EST_S = {"reads": 65, "setup": 30, "act": 140, "vis": 115, "gear": 60, "swap": 85, "rest": 85, "trade": 135,
         "nav": 195, "combat": 220, "loads": 80, "se": 350, "strict": 150}
# Who fixes a section's FAILs (docs/ORCHESTRATOR.md ownership): tools/dev/live_window.py prints the failures per owner.
# An item dict may carry its own "owner" to override this.
SECTION_OWNERS = {"reads": "vis", "setup": "setup", "act": "act", "vis": "vis", "gear": "act", "swap": "act",
                  "rest": "act", "trade": "act", "nav": "vis", "combat": "act", "loads": "se", "se": "se",
                  "strict": "setup"}
# tools/dev/live_window.py holds runtime/game.lock for its whole window and sets this: the sections then run without
# their own flock (a second flock on the same file would wait for the window's hold forever).
LOCK_HELD_ENV = "BG3AI_GAME_LOCK_HELD"
# What each save must be for its sections (checked offline by --check from the save's SaveInfo.json).
SAVE_NEEDS = {
    BASE: {"level": "SCL_Main_A", "party_has": ["Claude", "Gale", "Shadowheart"], "party_not": ["Wyll"],
           "party_min": 3, "used_by": "every section (swap: Gale in, Wyll in camp; nav/combat: fast travel to "
                                     "Moonrise Towers)"},
    HOTBAR_SAVE: {"level": "CTY_Main_A", "party_has": ["Claude"], "party_min": 3,
                  "used_by": "se/act hotbar casts (Claude the Paladin's Bless on 3; Dimension Door if Gale is in); "
                             "act prepare_spells (Bless prepared and unprepared on Claude, A5); strict (load 2, then camp "
                             "+ long_rest from Act 3)"},
    SCROLL_SAVE: {"party_has": ["Karlach"], "party_min": 3,
                  "used_by": "act point scroll aimed at the ground (A9: Karlach's Scroll of Misty Step)"},
    TRADE_SAVE: {"level": "SCL_Main_A", "near": ((-26.8, 27.4, 164.8), 8),
                 "used_by": "trade (Last Light Inn; Talli ~34 m S at (-31.3, 27.5, 130.2))"},
    MYSTRA_SAVE: {"level": "CTY_Main_A", "near": ((103.4, 44.5, -17.8), 8),
                  "used_by": "vis Mystra statue probe (Stormshore Tabernacle, the statue ~2 m from the party)"},
    DOOR_SAVE: {"level": "CTY_Main_A", "near": ((-233.0, 4.5, -180.6), 8),
                "used_by": "se (Lower City, the locked Steel Watch Foundry door ~1.6 m away; the older S050-end "
                           "folder stands 31 m off: load_game takes the newest, the right one)"},
}

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


# Spots a past run's route reached on foot (leg end dist<=0.2), per region, with the waypoint the route started from,
# for the multi-leg item: ladder_probe2/cycle-0414/cycle-0819 and windows #1-#2 "leg 3: along the route (108.5m):
# move_to: done @(-155.7,31.5,50.3)" on the Moonrise Towers -> Mausoleum walk. Walked from the S027-start camp exit
# inside the Last Light Inn (-54.2,27.5,141.8) instead (window #2), it is no route: west of the inn's Oak Door every
# leg dead-ends at (-86,28.5,130) ("no walkable ground leads further", also logs pass #9); the way out is the east
# gate (-8,28.5,119), where a Harper Branthos scene can start. So the item fast travels to the route's waypoint first.
KNOWN_WALK = {"Shadow-Cursed Lands": [((-155.7, 31.5, 50.3), "Moonrise Towers")]}


def far_walk_target(p, leads, region: str = "", lo: float = 40.0, hi: float = 160.0):
    """(spot, waypoint) for the multi-leg item: a KNOWN_WALK spot of the region with the waypoint to fast travel to
    first (a route reached it from there: the item stays the same run to run; no distance test, the waypoint sets
    it), else the frontier lead lo-hi m away (flat) from p with a pos nearest 100 m (doors, exits and waypoints
    first: on the walkable mesh) and waypoint None; None when none fits."""
    def d(q):
        return math.hypot(q[0] - p[0], q[2] - p[2])
    for key, qs in KNOWN_WALK.items():
        if key.lower() in (region or "").lower() and qs:
            q, via = qs[0]
            return tuple(float(x) for x in q), via
    rows = []
    for l in leads or []:
        q = l.get("pos") if isinstance(l, dict) else None
        if isinstance(q, (list, tuple)) and len(q) >= 3 and lo < d(q) < hi:
            rows.append((l.get("kind") not in ("door", "exit", "waypoint"), abs(d(q) - 100), tuple(float(x) for x in q[:3])))
    return (min(rows)[2], None) if rows else None


ROUTE_KEYS = ("reachable", "length_m", "needs_jump", "drop", "end_off_m", "unknown_reason", "no_path_reason", "far",
              "too_far", "ms")


def route_brief(r) -> str:
    """nav.route's answer in one line (the keys that say whether and how the game's own path gets there)."""
    if not isinstance(r, dict):
        return f"no answer ({r!r})"[:120]
    out = [f"{k}={r[k]}" for k in ROUTE_KEYS if r.get(k) is not None]
    cl = r.get("closest")
    if isinstance(cl, dict):
        out.append("closest=" + ",".join(f"{k}={cl[k]}" for k in ("pos", "path_m", "to_goal_m") if k in cl))
    return " ".join(out) or "empty"


def static_lead_ids(text: str) -> list[str]:
    """Ids of the doors/containers, then usables, listed in a get_frontier/look result (look_at out-of-sight item)."""
    ids = re.findall(r"\b(e\d+) [^<\n]*<(?:door|container)>", text or "")
    return ids + [i for i in re.findall(r"\b(e\d+) [^<\n]*<usable>", text or "") if i not in ids]


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


def game_memory(proc: Path = Path("/proc"), cgroot: Path = Path("/sys/fs/cgroup")) -> dict:
    """The harness game's memory in MB, {} without a bg3: VmRSS/VmSwap summed over the bg3 processes, and their cgroup
    (launch_game.sh's bg3ai-game-<stamp>.scope, MemoryMax 10G): current/peak/swap/swap_peak and anon/file from
    memory.stat. The scope counts page cache: L8's window scope peaked at 10G (+990M swap) while the game's RSS stayed
    at 4.9-6.4 GB, so `file` against `anon` tells a leak (anon grows section after section) from cache."""
    out: dict = {}
    cg = None
    for d in proc.iterdir():
        try:
            if not d.name.isdigit() or (d / "comm").read_text().strip() != "bg3":
                continue
            status = (d / "status").read_text()
            cg = cg or next((ln.split("::", 1)[1] for ln in (d / "cgroup").read_text().splitlines()
                             if ln.startswith("0::")), None)
        except OSError:
            continue
        for key, name in (("VmRSS", "rss"), ("VmSwap", "swap")):
            m = re.search(rf"^{key}:\s+(\d+) kB", status, re.M)
            if m:
                out[name] = out.get(name, 0) + int(m.group(1)) // 1024
    if not out:
        return {}
    base = cgroot / cg.lstrip("/") if cg else None
    if base is not None and "bg3ai-game-" in cg:
        out["scope"] = cg.rsplit("/", 1)[-1]
        for f, name in (("memory.current", "cg_current"), ("memory.peak", "cg_peak"),
                        ("memory.swap.current", "cg_swap"), ("memory.swap.peak", "cg_swap_peak")):
            try:
                out[name] = int((base / f).read_text().split()[0]) // 2**20
            except (OSError, ValueError, IndexError):
                pass
        try:
            stat = dict(ln.split() for ln in (base / "memory.stat").read_text().splitlines() if ln.count(" ") == 1)
            out.update({k: int(stat[k]) // 2**20 for k in ("anon", "file") if k in stat})
        except (OSError, ValueError):
            pass
    return out


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

    read_cost_probe()
    load_verified(BASE)


# V12: the full state.inventory rebuild (look/get_status/get_inventory; the read cache's 20 s TTL had expired at most
# reads in S066-S070) and act.prepared's prep_info (every get_status), with the stats memos emptied (the cost before
# V12) and then filled. Read-only: the memos are put back, the inventory cache rebuilds on the next read.
READ_COST_LUA = (
    "local B=Mods.BG3AI.BG3AI local S,G,V=B.State,B.Guard,B.Visibility local now=Ext.Utils.MonotonicTime "
    "local function inv() S.invCache=nil local t=now() local r=S.inventory(V.context(),{}) return now()-t,#r.items end "
    "local function prep() local t,n=now(),0 for _,u in ipairs(G.party()) do local g=G.guid(u) "
    "if g then n=n+#G.prep_info(g).prepared end end return now()-t,n end "
    "local keepS,keepG=S.statsFactsMemo,G.spellFacts S.statsFactsMemo,G.spellFacts={},{} "
    "local ic,items=inv() local pc,rows=prep() local iw=inv() local pw=prep() "
    "for k,v in pairs(S.statsFactsMemo) do keepS[k]=v end for k,v in pairs(G.spellFacts) do keepG[k]=v end "
    "S.statsFactsMemo,G.spellFacts=keepS,keepG "
    "return Ext.Json.Stringify({inv_cold=ic,inv_warm=iw,prep_cold=pc,prep_warm=pw,items=items,prepared=rows})"
)
READ_COST_ITEM = "read cost: stats memos cut the inventory + prepared reads (V12)"


def read_cost_probe() -> None:
    """PASS when the filled memos take each read to <= 60 % of its emptied-memo time (or under 15 ms)."""
    try:
        r = json.loads(lua_server(READ_COST_LUA))
    except Exception as e:  # noqa: BLE001 - a probe error is a result
        item(READ_COST_ITEM, "FAIL", f"probe error: {e}")
        return
    ok = all(r[w] <= max(0.6 * r[c], 15) for c, w in (("inv_cold", "inv_warm"), ("prep_cold", "prep_warm")))
    item(READ_COST_ITEM, "PASS" if ok else "FAIL",
         f"state.inventory rebuild {r['inv_cold']:.0f} -> {r['inv_warm']:.0f} ms ({r['items']} items); "
         f"prep_info (party) {r['prep_cold']:.0f} -> {r['prep_warm']:.0f} ms ({r['prepared']} prepared rows); "
         "emptied memo -> filled")


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


def wares_block(listing: str) -> str:
    """The WARES lines of a trade listing (not YOUR ITEMS: those are ours to offer, not to request)."""
    m = re.search(r"(?ms)^WARES .*?(?=^YOUR ITEMS|^\(|\Z)", listing)
    return m.group(0) if m else ""


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
    # [act] S067 #125 / S068 #168: the window stayed up after trade(request, confirm=true) and the agent pressed
    # ESCAPE by hand: a confirmed deal closes it now and says so. Buys the cheapest food ware (reloaded after).
    # A15 (L7 22:08): the picks come from the WARES block only (the first listing had no ware ids, and the old regex
    # took e100/e171 from YOUR ITEMS: "isn't on this side"); sell=junk's result is the newer listing
    shop = wares_block(out if "WARES" in out else wares)
    food = sorted(((int(p), e) for e, p in re.findall(r"(e\d+) [^|\n]*? (\d+)g supply=\d+", shop)), key=lambda x: x[0])
    if food:
        out, dt = took("trade", request=food[0][1], confirm=True)
        scr = ui_screen()
        ok = "closed the trade window: back in the game" in out and scr == "game"
        item("trade confirm closes the window", "PASS" if ok else "FAIL",
             f"{dt:.1f}s; screen={scr}; {first_line(out)} | "
             + " ".join(re.findall(r"(?m)^\((?:closed|closing) the trade window[^\n]*", out)))
    else:
        item("trade confirm closes the window", "SKIP", "no food ware in the listing: " + first_line(wares))
    # [act] A13 S069 20:37:23: request 2 wares (one a stack) + confirm went to the barter before the Balance's gold
    # was on our side: refused, ESCAPE + reopen. confirm now waits for an even table; the TABLE counts gold once.
    priced = [(int(p), e, " x" in t) for e, t, p in re.findall(r"(e\d+) ([^|\n]*?) (\d+)g", shop)]
    stack = min((x for x in priced if x[2]), default=None)
    single = min((x for x in priced if not x[2] and (not stack or x[1] != stack[1])), default=None)
    if stack and single and m:
        if ui_screen() != "trade":
            call("trade", npc=m.group(1))
        out, dt = took("trade", request=f"{stack[1]},{single[1]}", confirm=True)
        ok = out.startswith("trade: done") and "refused" not in out and "(no id)" not in out
        item("trade confirm: 2 wares incl. a stack, even table first (A13)", "PASS" if ok else "FAIL",
             f"{dt:.1f}s; {stack[1]} {stack[0]}g + {single[1]} {single[0]}g; {first_line(out)} | "
             + grab(r"^TABLE[^\n]*", out))
    else:
        item("trade confirm: 2 wares incl. a stack, even table first (A13)", "SKIP", "no stack + single ware listed")
    esc = call("press_key", key="ESCAPE") if ui_screen() == "trade" else "(closed already)"
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
    # Multi-leg walk: a route-proven spot of the region from its waypoint (KNOWN_WALK), else a lead 40-160 m away
    # (raw nav.frontier positions: get_frontier's text has none; window 20261016-183253: only doors 10-17 m here).
    fr = call("get_frontier")
    raw_fr = S.game.try_rpc("nav.frontier", {"radius": 160, "limit": 60}) or {}
    tgt = far_walk_target(p, raw_fr.get("leads"), status().get("region") or region)
    if need(tgt is not None, "travel_to multi-leg", "no known spot or frontier point 40-160 m away to walk to: "
            + first_line(fr)):
        q, via = tgt
        ok_start = True
        if via:
            # What the game's own path says from here (window #2: travel_to fell back to dead-end straight legs):
            # one probe, recorded for the next window's diagnosis, no walk.
            item("nav.route from the start to the multi-leg spot (no walk)", "INFO",
                 f"from {[round(float(x), 1) for x in p[:3]]}: "
                 + route_brief(S.game.try_rpc("nav.route", {"pos": list(q), "connectors": False})))
            ft = call("fast_travel", waypoint=via)
            ok_start = need("FAILED" not in ft and "REFUSED" not in ft, "travel_to multi-leg",
                            f"fast_travel {via} failed: " + first_line(ft))
            p = (status().get("party") or [{}])[0].get("pos") or p
        if ok_start:
            out, dt = took("travel_to", x=q[0], y=q[1], z=q[2])
            end = time.monotonic() + 180
            w = ""
            while time.monotonic() < end:
                w = call("wait_for", target="queue", timeout_s=60, lead_s=0)
                if "still waiting" not in w:
                    break
            me2 = (status().get("party") or [{}])[0].get("pos") or p
            left = ((me2[0] - q[0]) ** 2 + (me2[2] - q[2]) ** 2) ** 0.5
            legs = len(re.findall(r"(?m)^leg \d+:", w))
            ok = left < 6 and legs >= 2 and "INTERRUPTED" not in w
            item("travel_to multi-leg", "PASS" if ok else "FAIL",
                 f"{((q[0]-p[0])**2+(q[2]-p[2])**2)**0.5:.0f} m trip{' from ' + via if via else ''}, {legs} legs, "
                 f"{left:.1f} m left at the end; {first_line(out)} | {first_line(w)}")

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
    # act ladder 4th look (A.LADDER_WATCH_AT_USE): from the gate the job must END done (10-11 08:37/12:22 "still
    # running" at 25 s) and the climber must stay at the foot (10:43: "done climbed=down", back at the top 12 s later)
    held = []
    for _ in range(5):
        time.sleep(1.0)
        held.append(heights()[1].get(name, 99))
    stays = bool(held) and max(held) < foot + 1.5
    item("ladder climb ends done and holds (act LADDER_WATCH_AT_USE)",
         "PASS" if ok and "interact: done" in out and "climbed=" in out and stays else "FAIL",
         f"{first_line(out)}; {name} over 5 s after: " + ", ".join(f"{y:.1f}" for y in held)
         + f" (foot {foot:.1f}); FAIL with 'still running' or a climb back up: turn the flag off "
         "(Mods.BG3AI.BG3AI.Actions.LADDER_WATCH_AT_USE = false) and rerun to compare")
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
    bg_ends: list[tuple] = []  # (who, combat_turn s, background end s after it returned, queue text, turn passed)
    rts: list[tuple] = []  # (metrics record, single state.combat RPCs, FAILED lines) per combat_turn (batched checks)
    ct_raw: list[str] = []  # the raw combat_turn results (vis combat compaction check)
    probed = False
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
        if not probed and len(now) >= 2:
            probed = end_turn_probe(cs, now, party)
            if probed:
                # Live window 2026-10-16: the probe ends the whole group (avatar included); 0.2 s later the next loop
                # still saw the group acting and played Claude's turn into the hand-over (a 9 m move out of turn, then
                # "not this character's turn"). Wait for the game to hand the turn on before planning one.
                t_hand = time.monotonic()
                while time.monotonic() - t_hand < 15:
                    cs2 = S.game.try_rpc("state.combat") or {}
                    if not cs2.get("active") or not set(now) & set(T.acting(cs2)):
                        break
                    time.sleep(0.3)
                log(f"end_turn probe: group handed on after {time.monotonic() - t_hand:.1f}s")
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
        recs: list[dict] = []
        orig_td = S.METRICS.tool_done

        def tool_done(tool, *a, **k):  # the combat_turn's own metrics record (rt_n / watch_n)
            r = orig_td(tool, *a, **k)
            if tool == "combat_turn" and r and not r.get("nested"):
                recs.append(r)
            return r
        S.METRICS.tool_done = tool_done
        try:
            out, dt, counts = counted("combat_turn", plan=plan, who=S.H.get(who_u))
        finally:
            S.METRICS.tool_done = orig_td
        if recs:
            rts.append((recs[-1], counts.get("state.combat", 0), out.count("FAILED")))
        ct_raw.append(out)
        log(f"RPCs by op: {dict(sorted(counts.items(), key=lambda kv: -kv[1]))}")
        if "end_turn: ending in the background" in out:  # latency round 2026-10-16 (S.END_ASYNC)
            b0 = time.monotonic()
            while S._queues().busy(who_u) and time.monotonic() - b0 < S.END_BG_HOLD_S:
                time.sleep(0.1)
            q = S._last_queue_text(who_u)
            cs2 = S.game.try_rpc("state.combat") or {}
            bg_ends.append((party[who_u].get("name"), dt, time.monotonic() - b0, q,
                            who_u not in T.acting(cs2) or not cs2.get("active")))
        jobs = sum(v for k, v in counts.items() if k.startswith("act."))
        polls = counts.get("job.poll", 0)
        rpcs = sum(counts.values())
        # "move eN: FAILED — beyond remaining movement … → partial 8.7m toward it: done" is a move too (live window
        # 2026-10-16: Claude walked 9 m and the check said moved=False)
        moved = bool(re.search(r"(?m)^move \S+: (?:done|FAILED[^\n]*→ partial [\d.]+m[^\n]*: done)", out))
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
    if not probed:
        item("end_turn after a companion's request (probe)", "SKIP", "no shared turn group with the avatar in the "
             "turns played (the party's initiatives differed)")
    # latency (2026-10-16): the party's last end before enemy turns runs in the background; it must still end the turn
    # ("end_turn: done", incl. the HUD fallback) and the turn must pass. The numbers = what the agent no longer waits.
    if not bg_ends:
        item("combat_turn background end", "SKIP", f"no background end (END_ASYNC={S.END_ASYNC}; shared group or "
             "the next turn ours each time)")
    else:
        ok = all("end_turn" in q and "STOPPED" not in q and ": done" in q and passed for _, _, _, q, passed in bg_ends)
        item("combat_turn background end", "PASS" if ok else "FAIL",
             "; ".join(f"{n}: returned {d:.1f}s, end done {b:.1f}s later, passed={p}, {q.splitlines()[-1][:90] if q else '-'}"
                       for n, d, b, q, p in bg_ends))
    # act 2026-10-16 "combat_turn serial RPCs": the step checks ride on the watcher's read after each step (one round
    # trip with its events + act.watch); a read nobody can have changed is reused. Single state.combat RPCs left: the
    # end_turn tool's own, the NEXT line's, one after each failed alternative (the start one is in a multi).
    if rts:
        from bg3ai import actq
        ok = actq.BATCH_CHECKS and all("rt_n" in r and n <= 3 + f for r, n, f in rts)
        item("combat_turn batched step checks", "PASS" if ok else "FAIL",
             f"BATCH_CHECKS={actq.BATCH_CHECKS}; " + "; ".join(
                 f"rt_n={r.get('rt_n')} rpc_n={r.get('rpc_n')} watch_n={r.get('watch_n', 0)} single state.combat={n} "
                 f"failed={f} ms-job={r['ms'] - r.get('job_ms', 0):.0f}" for r, n, f in rts))
    combat_compaction_check([gc] + ct_raw)
    melee_probe()
    load_verified(BASE)


def combat_compaction_check(raws: list[str]) -> None:
    """vis 2026-10-16 combat compaction (compact.py COMBAT_NARRATIVE .. KIND_GROUP) on this fight's live results:
    0 lost facts (compact_check), smaller, an enemy turn's cast + damage on one line, no @(x,y,z) on move lines."""
    from bg3ai import compact, compact_check as cc
    comp, shown, probs, a, b, outs = compact.Compactor(), [], [], 0, 0, []
    for i, raw in enumerate(r for r in raws if r):
        tool = "get_combat_state" if i == 0 else "combat_turn"
        out = comp.apply(tool, raw)
        probs += [f"#{i} {p}" for p in cc.check(raw, out, comp.elided, "\n".join(shown))]
        shown.append(out)
        outs.append(out)
        a, b = a + cc.est_tokens(raw), b + cc.est_tokens(out)
    if len(outs) < 2:
        item("vis: combat compaction keeps every fact (live)", "SKIP", "no combat_turn result")
        return
    text = "\n".join(outs[1:])
    narr = re.findall(r"(?m)^\s+(?:turn|cast) e\d+[^:\n]*: [^\n]* -> e\d+[^\n]*$", text)
    pos = [ln for ln in text.split("\n") if ln.startswith("move ") and "@(" in ln] if compact.COMBAT_MOVE_POS else []
    item("vis: combat compaction keeps every fact (live)", "PASS" if not probs and b < a and not pos else "FAIL",
         f"{len(outs) - 1} combat_turn: tokens ~{a}->~{b} ({(b - a) / max(1, a):+.0%}); narrative lines "
         f"{len(narr)} e.g. {narr[:2]}; move lines with @: {pos[:1]}; problems {probs[:3]}")


def end_turn_probe(cs: dict, now: list[str], party: dict) -> bool:
    """act proposal "ignored EndTurn -> HUD at once" (naut runs 1/3: after Lae'zel's end was requested every Osiris
    EndTurn on the avatar was ignored: 3 s Lua timeout, then the HUD's End Turn in ~0.4 s; run 2's opposite order
    worked). In a shared turn group with the avatar: end a companion first, then the avatar by Osiris EndTurn alone
    (act.end_turn, no HUD fallback): ignored or not, how long; if ignored, the HUD ends it (timed). With
    BG3AI_END_HUD_FIRST=1 the end_turn tool itself must press the HUD at once. Decision for the live window:
    ignored -> flip END_HUD_FIRST on (server.END_HUD_FIRST / env), else keep it off. False: no shared group here."""
    from bg3ai.game import JobResult
    host = str(S.game.eval("return tostring(Osi.GetHostCharacter())", context="server") or "")[-36:]
    if host not in now:
        log(f"end_turn probe: the group {[party[u].get('name') for u in now]} has no avatar ({host})")
        return False
    comp = next(u for u in now if u != host)
    cn, an = party[comp].get("name"), party[host].get("name")
    t = time.monotonic()
    r1 = S.game.run_job("act.end_turn", {"char": comp}, wait_s=15)
    d1 = time.monotonic() - t
    r1s = f"{cn} end {r1.status} {d1:.1f}s requested={(r1.result or {}).get('requested')}"
    if r1.status != "done":
        item("end_turn after a companion's request (probe)", "FAIL", f"{r1s}: {r1.error}")
        return True
    cs1 = S.game.try_rpc("state.combat") or {}
    peer = S._peer_requested(cs1, host)
    if S.END_HUD_FIRST:
        out, dt = took("end_turn", who=S.H.get(host), wait=False)
        ok = "the HUD's End Turn at once" in out and "FAILED" not in out
        item("end_turn after a companion's request (probe)", "PASS" if ok else "FAIL",
             f"END_HUD_FIRST on; {r1s}; peer seen={bool(peer)}; {an} end_turn {dt:.1f}s: {first_line(out)[:160]}")
        return True
    t = time.monotonic()
    r2 = S.game.run_job("act.end_turn", {"char": host}, wait_s=15)
    d2 = time.monotonic() - t
    ignored = r2.status == "failed" and S.END_IGNORED in str(r2.error or "")
    hud = ""
    if ignored:
        t = time.monotonic()
        r3 = S._end_turn_by_hud(host, JobResult("failed", None, r2.error))
        hud = f"; HUD End Turn {r3.status} {time.monotonic() - t:.1f}s"
    if r2.status == "done":
        # act 2026-10-16 (guard turn_over): right after the group's end the game may still show the avatar's turn
        # open; a move then must be refused, not walked out of turn (live window 18:45: 9 m free move).
        p = party[host].get("pos") or [0, 0, 0]
        t = time.monotonic()
        rm = S.game.run_job("act.move_to", {"char": host, "pos": [p[0] + 1.5, p[1], p[2]]}, wait_s=8)
        refused = rm.status == "failed" and "turn" in str(rm.error or "")
        item("no move right after the group's end (guard turn_over)", "PASS" if refused else "FAIL",
             f"{an} act.move_to {time.monotonic() - t:.1f}s after its end: {rm.status} {rm.error or rm.result}")
    decision = ("IGNORED: flip END_HUD_FIRST on (BG3AI_END_HUD_FIRST=1), saves ~3 s per such end" if ignored
                else "not ignored: keep END_HUD_FIRST off")
    item("end_turn after a companion's request (probe)", "PASS" if r2.status == "done" or ignored else "FAIL",
         f"{r1s}; peer seen={bool(peer)}; {an} Osiris EndTurn {r2.status} {d2:.1f}s ({r2.error or 'ok'}){hud} -> {decision}")
    return True


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


def server_map_line(srv) -> str:
    """hotbar.map's answer for a PROBE line, every deck in slot order. Live window #3: json.dumps(...)[:800] ended
    right where ClassHotBar 5-8 would be, so whether Bless was on the bar was not provable from the log."""
    if not isinstance(srv, dict) or not isinstance(srv.get("map"), dict):
        return json.dumps(srv)[:400]
    def key(k):
        return (0, int(k)) if str(k).isdigit() else (1, str(k))
    decks = [f"{d}[{' '.join(f'{k}={v}' for k, v in sorted((m or {}).items(), key=lambda kv: key(kv[0])))}]"
             for d, m in sorted(srv["map"].items())]
    head = " ".join(f"{k}={srv[k]}" for k in ("container", "bars", "slots") if k in srv)
    return (head + " " if head else "") + " ".join(decks)


def ui3_refused(msg: str) -> bool:
    """A mod UI-3 refusal (Client/UIActions.lua SAVELOAD_HINT): the whole message names save_game / load_game."""
    return "refused" in msg and "save_game / load_game" in msg


def hb_slot(spell: str, char: str | None) -> dict:
    """ui.hotbar_slot as the cast route reads it (bridge hotbar._slot: the server's hotbar map when the client has no
    spell ids, live window #1); {} on an error."""
    from bg3ai import hotbar as HB
    try:
        return HB._slot(S.game, spell, char) or {}
    except Exception:  # noqa: BLE001
        return {}


def close_targeting() -> str:
    """Leave a targeting mode (or an accidental game menu) with ESCAPE, by ui.key: the guarded press_key refuses it."""
    for _ in range(3):
        S.game.try_rpc("ui.key", {"key": "ESCAPE"}, context="client")
        time.sleep(0.5)
        if ui_screen() != "game_menu":
            break
    return ui_screen()


def hotbar_probe() -> None:
    """Audit ACT-8 rest (se, 2026-10-16): the hotbar cast route (bridge hotbar.py + Client/Hotbar.lua), built offline
    from HotBar.xaml with guesses G1-G10 (Hotbar.lua / hotbar.py headers). 1) read-only: the widget tree as the mod
    sees it (decks, a slot VM's spell id, a slot button's DataContext), ui.hotbar_slot for Main Hand Attack, and the
    cursor search for its cell (moves only, no click). 2) one Bless on 3 party members out of combat through the
    hotbar (spends a level-1 slot; sec_se reloads the base save after). PASS = the cast went off on 3 (BLESS on each)
    and nothing is left in hand. Full reads in se.log (PROBE hotbar lines)."""
    from bg3ai import hotbar as HB
    st = status()
    sel = st.get("active")
    try:
        tree = S.game.eval(
            "local UI = Mods.BG3AI.BG3AI.UI local dc = UI.dc('HotBar') if not dc then return 'no HotBar dc' end "
            "local bars = UI.path(dc, 'CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.KeyboardHotBars') "
            "local out = {'decks=' .. tostring(UI.count(bars))} "
            "for i, b in UI.items(bars, 8) do local sl = UI.get(b, 'SlotList') local s1 = sl and UI.count(sl) > 0 and sl:At(1) "
            "local c = s1 and UI.get(s1, 'Content') "
            "out[#out + 1] = tostring(UI.get(b, 'HotBarType')) .. ' n=' .. tostring(sl and UI.count(sl)) .. ' cols=' "
            ".. tostring(UI.get(b, 'CellColumns')) .. ' rows=' .. tostring(UI.get(b, 'NumberOfBars')) .. ' s1=' "
            ".. tostring(UI.typeName(s1)) .. '/' .. tostring(UI.get(s1, 'SlotIndex')) .. ' content=' .. tostring(UI.typeName(c)) "
            ".. ' PrototypeID=' .. tostring(UI.get(c, 'PrototypeID')) end "
            "local w = UI.widget('HotBar') local btn = w and UI.find(w, function(e) return UI.name(e) == 'contentContainer' end) "
            "out[#out + 1] = 'button dc=' .. tostring(btn and UI.typeName(UI.get(btn, 'DataContext'))) "
            ".. ' w=' .. tostring(btn and UI.get(btn, 'ActualWidth')) "
            "out[#out + 1] = 'shown=' .. tostring(UI.path(dc, 'CurrentShownDeck.HotBarType')) "
            ".. ' ActiveTask=' .. tostring(UI.path(dc, 'CurrentPlayer.UIData.ActiveTask')) "
            # window #2: a hidden duplicate "HotBar" page read in place of the shown one? (visible flag per page)
            "local hb = {} for _, x in ipairs(UI.widgets()) do if x.name == 'HotBar' then "
            "hb[#hb + 1] = tostring(UI.isVisible(x.el)) .. (x.el == w and '*' or '') end end "
            "out[#out + 1] = 'HotBar pages=' .. table.concat(hb, ',') .. ' (* = UI.widget)' "
            "return table.concat(out, ' | ')", context="client")
    except Exception as e:  # noqa: BLE001
        tree = f"eval error {e}"
    log(f"PROBE hotbar tree: {tree}")
    # window #1 (10-16 18:32): PrototypeID nil on every slot VM, button DataContext nil, ActiveTask set with nothing
    # in hand. Now: the ids come from the HotbarContainer component (Shared/HotbarMap.lua); log both contexts' maps,
    # the first slot VMs' reflected props (UI.props: view models only, never elements) and the armed signals.
    try:
        ids = S.game.eval(
            "local UI, HM = Mods.BG3AI.BG3AI.UI, Mods.BG3AI.BG3AI.HotbarMap local dc = UI.dc('HotBar') "
            "local out = {} local bars = dc and UI.path(dc, 'CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.KeyboardHotBars') "
            "local b1 = bars and bars:At(1) local sl = b1 and UI.get(b1, 'SlotList') "
            "for i = 1, 2 do local s = sl and UI.count(sl) >= i and sl:At(i) local c = s and UI.get(s, 'Content') "
            "local ks = {} for k, v in pairs(UI.props(c)) do ks[#ks + 1] = tostring(k) .. '=' .. tostring(v) end "
            "table.sort(ks) out[#out + 1] = 'vm' .. i .. ' {' .. table.concat(ks, ','):sub(1, 400) .. '}' end "
            "local m, info = Mods.BG3AI.BG3AI.Hotbar.clientMap(nil) "
            "if m then local n = 0 for d, t in pairs(m) do for s, id in pairs(t) do n = n + 1 "
            "if n <= 6 then out[#out + 1] = d .. '#' .. s .. '=' .. id end end end "
            "out[#out + 1] = 'client map: ' .. n .. ' ids, container ' .. tostring(info.container) "
            "else out[#out + 1] = 'client map: none (' .. tostring(info) .. ')' end "
            "return table.concat(out, ' | ')", context="client")
    except Exception as e:  # noqa: BLE001
        ids = f"eval error {e}"
    log(f"PROBE hotbar ids: {ids}")
    srv = S.game.try_rpc("hotbar.map", {"char": sel}, context="server") if sel else None
    log(f"PROBE hotbar server map: {server_map_line(srv)}")
    inp = S._inp()
    try:
        want = HB._slot(S.game, "Target_MainHandAttack", sel)   # = the route's read (server map when ids=none)
    except Exception as e:  # noqa: BLE001
        want = {"error": str(e)}
    cs = HB.cast_state(S.game, sel)
    pos, last = (None, {}) if not want.get("found") or inp is None else HB.locate_cell(S.game, inp, "Target_MainHandAttack", sel, want)
    log(f"PROBE hotbar slot {json.dumps(want)} | cast_state {json.dumps(cs)} | located {pos} {json.dumps(last)[:600]}")
    # window #2: 39 misses and only the last one logged. `seen` = how many probes were on the HUD (IsOverUI), on the
    # decks (Concertina), on a cell, the first spot of each, and the last IsMouseOver chain under the HotBar widget.
    seen = last.get("seen")
    if seen:
        log(f"PROBE hotbar locate seen {json.dumps(seen)}")
    ok = bool(want.get("found")) and want.get("rendered") is not False and pos is not None and cs.get("armed") is False
    item("hotbar widget tree (ACT-8, read-only)", "PASS" if ok else "FAIL",
         f"{tree[:120]}; ids={want.get('ids')} from={want.get('ids_from')} map_why={want.get('map_why')}; "
         f"MainHandAttack: found={want.get('found')} deck={want.get('deck')} index={want.get('index')} "
         f"cols={want.get('cols')} rows={want.get('rows')} rendered={want.get('rendered')} cell={want.get('cell')}; "
         f"located at {pos} in {last.get('probes')} probes"
         + (f" (seen ui={seen.get('over_ui')} decks={seen.get('over_hud')} cells={seen.get('cells')} "
            f"chain={(seen.get('hover') or {}).get('chain')})" if seen else "")
         + f"; armed={cs.get('armed')} by={cs.get('armed_by')} "
         f"preview={cs.get('preview')} spell_task={cs.get('spell_task')}")
    if not ok:  # the guesses the clicks rely on broke: don't click blind into the world with a spell in hand
        item("hotbar cast: Bless on 3 allies (ACT-8)", "SKIP", "the read-only hotbar item failed (see its PROBE line)")
        return
    # 2) Bless x3 out of combat
    party = [r.get("uuid") for r in st.get("party") or []]
    pick, why = pick_hotbar_caster(party, sel)
    caster, spell, status_id, n = pick or (None, None, None, 0)
    if need(caster is not None and not st.get("in_combat") and inp is not None,
            "hotbar cast: Bless on 3 allies (ACT-8)",
            f"caster with a usable ally multi-target spell={caster}, party={len(party)}, "
            f"in_combat={st.get('in_combat')}; {why}"):
        targets = ([caster] + [u for u in party if u != caster])[:n]
        r = HB.hotbar_cast(S.game, inp, caster, spell, targets)
        time.sleep(2.5)
        on = []
        for u in targets:
            try:
                on.append(lua_server(f'return tostring(Osi.HasActiveStatus("{u}", "{status_id}"))') == "1")
            except Exception:  # noqa: BLE001
                on.append(False)
        cs = HB.cast_state(S.game, caster)
        ok = r.ok and r.picked == len(targets) and all(on) and not cs.get("armed") and ui_screen() == "game"
        if ok:
            HOTBAR_PICK.update(caster=caster, spell=spell, status=status_id, n=n)
        item("hotbar cast: Bless on 3 allies (ACT-8)", "PASS" if ok else "FAIL",
             f"{spell} by {S.H.get(caster)} on {len(targets)}: {json.dumps(r.as_dict())[:200]}; {status_id} on {on}; "
             f"in hand after={cs.get('armed')}; screen={ui_screen()}")
        if not ok:
            close_targeting()
    if sel:
        S.game.run_job("act.select", {"char": sel}, wait_s=5)


HOTBAR_ITEM = "use_ability Bless on 3 via the hotbar route (ACT-8 wiring)"

# Spells for the N-target hotbar items: they target allies (castable out of combat on the party without a fight),
# take more than 2 targets at their base level (the Osiris route's limit), and leave a status to check. Bless: on
# HOTBAR_SAVE's hotbars (Claude the Paladin casts it in claude-run-2 S029-S065; S027-start has it on nobody's).
# Aid / Mass Healing Word are Shouts (an area, no target clicks) and prove nothing about the click route.
HOTBAR_ALLY_SPELLS = (("Target_Bless", "BLESS", 3),)
# Seen on the maps only to say what else the party could cast (all hostile: not for an out-of-combat item).
HOTBAR_MULTI_OTHER = ("Target_Bane", "Projectile_MagicMissile", "Projectile_ScorchingRay", "Projectile_EldritchBlast",
                      "Target_HoldPerson")
HOTBAR_PICK: dict = {}   # se's passed pick (caster, spell, status, n): act's wiring item casts the same


# A member's SpellBook / SpellContainer entries whose id contains NEEDLE, with the prepared state the guard reads
# (Guard.prepared_ids / Guard.unprepared, A4 2026-10-17): {book: [{id, orig, src, prep, prepared, unprepared}],
# container: [{id, src, prep, in, linked}], prepared_read} | {why}. Read-only.
SPELL_STATE_LUA = (
    "local G = Mods.BG3AI.BG3AI.Guard local e = Ext.Entity.Get(U) local needle = NEEDLE "
    "if not e then return Ext.Json.Stringify({why = 'no entity'}) end "
    "if not (G and G.prepared_ids and G.unprepared) then return Ext.Json.Stringify({why = 'the mod has no "
    "Guard.prepared_ids (pre-A4)'}) end "
    "local s = tostring local prep = G.prepared_ids(e) local out = {book = {}, container = {}, "
    "prepared_read = prep ~= false} "
    "pcall(function() for _, x in ipairs(e.SpellBook.Spells) do local id = s(x.Id.Prototype) "
    "if id:find(needle, 1, true) then local o = s(x.Id.OriginatorPrototype) out.book[#out.book + 1] = {id = id, "
    "orig = o, src = s(x.Id.SourceType), prep = s(x.PrepareType), prepared = (prep and prep[o]) == true, "
    "unprepared = G.unprepared(x, prep)} end end end) "
    "pcall(function() for _, m in ipairs(e.SpellContainer.Spells) do local id = s(m.SpellId.OriginatorPrototype) "
    "if id:find(needle, 1, true) then out.container[#out.container + 1] = {id = id, src = s(m.SpellId.SourceType), "
    "prep = s(m.PrepareType), ['in'] = s(m.ContainerSpell), linked = m.LinkedSpellContainer == true} end end end) "
    "return Ext.Json.Stringify(out)")


def spell_state(u: str, needle: str) -> dict:
    """SPELL_STATE_LUA for one member; {why} on an error."""
    try:
        v = json.loads(lua_server(f"local U, NEEDLE = '{u}', '{needle}' " + SPELL_STATE_LUA))
        return v if isinstance(v, dict) else {"why": f"not a table: {v!r}"[:80]}
    except Exception as e:  # noqa: BLE001
        return {"why": f"read error {e}"[:120]}


def prep_reason(state: dict, spell: str, on_map: bool) -> str:
    """Why `spell` is or isn't castable from a member's hotbar, from spell_state (A4: S065-end had no Bless on any
    hotbar map, yet Claude cast it in S065 through the Osiris route, whose prepared check never ran)."""
    if state.get("why"):
        return f"spell state unread ({state['why']})"
    rows = [b for b in state.get("book") or [] if str(b.get("id", "")).lower() == spell.lower()]
    if not rows:
        return "not in the spellbook" + (" but on the hotbar map" if on_map else "")
    if not state.get("prepared_read"):
        return "in the spellbook; the prepared list did not read"
    b = rows[0]
    if b.get("unprepared"):
        return (f"in the spellbook ({b.get('prep')}), not prepared: not castable, and the game keeps it off the "
                "hotbar" + ("; yet the map has it" if on_map else ""))
    if on_map:
        return f"prepared ({b.get('prep')}) and on the hotbar map"
    return (f"castable ({b.get('prep')}, prepared={b.get('prepared')}) but not on the server hotbar map: the bar lost "
            "it or Shared/HotbarMap.lua misses a bar (see PROBE hotbar server map)")


def pick_hotbar_caster(party: list, sel: str | None) -> tuple[tuple | None, str]:
    """(caster, spell, status, targets) for the N-target hotbar items, from each member's SERVER hotbar map (no HUD
    switching: live window #3 found no Bless 0.4 s after each act.select, the HUD still showing the previous member).
    The selected member first. Then the caster is selected and the HUD read polled until it shows the cell
    (hotbar._slot_after_select). Returns (None, why) with what every member has when nobody fits."""
    from bg3ai import hotbar as HB
    order = ([sel] if sel in party else []) + [u for u in party if u != sel]
    seen = []
    for spell, status_id, n in HOTBAR_ALLY_SPELLS:
        if len(party) < n:
            continue
        for u in order:
            m = HB.server_map(S.game, u)
            if not HB.map_has(m, spell):
                continue
            if u != sel:
                S.game.run_job("act.select", {"char": u}, wait_s=5)
            r = HB._slot_after_select(S.game, spell, u)
            log(f"PROBE hotbar caster {S.H.get(u)}: {spell} on the map; HUD read found={r.get('found')} "
                f"can_use={r.get('can_use')} selected={r.get('selected')} deck={r.get('deck')} index={r.get('index')}")
            if r.get("found") and r.get("can_use") is not False:
                return (u, spell, status_id, n), ""
            seen.append(f"{S.H.get(u)} has {spell} but the HUD read found={r.get('found')} can_use={r.get('can_use')}")
    for u in order:
        m = HB.server_map(S.game, u)
        have = [sp for sp in tuple(s for s, _, _ in HOTBAR_ALLY_SPELLS) + HOTBAR_MULTI_OTHER if HB.map_has(m, sp)]
        n_ids = sum(len(v or {}) for v in (m or {}).values())
        line = f"{S.H.get(u)}: {n_ids} ids, multi-target {have or 'none'}" if m else f"{S.H.get(u)}: no map"
        for spell, _, _ in HOTBAR_ALLY_SPELLS:   # why each ally spell is missing: unprepared, or the map
            line += f"; {spell} {prep_reason(spell_state(u, spell), spell, HB.map_has(m, spell))}"
        seen.append(line)
    why = "; ".join(seen)
    log(f"PROBE hotbar caster: none fits ({why})")
    return None, why


def hotbar_use_ability() -> bool:
    """act 2026-10-16, audit ACT-8 wiring: use_ability with 3 targets goes the hotbar route (act.cast {ui=true} checks
    and watches the cast, hotbar.hotbar_cast clicks it). HOTBAR_CAST_ENABLED is off by default: turned on for this one
    call, only after se's direct item passed. PASS = 'use_ability: done' with via=hotbar, 'hotbar: 3 of 3', BLESS on
    all 3, nothing charged by the guard, nothing in hand. Decision for the live window: PASS -> set
    hotbar.HOTBAR_CAST_ENABLED = True. True = it cast (the caller reloads)."""
    from bg3ai import hotbar as HB
    se_ok = any(r["item"] == "hotbar cast: Bless on 3 allies (ACT-8)" and r["verdict"] == "PASS" for r in RESULTS)
    if not need(se_ok, HOTBAR_ITEM, "se's 'hotbar cast: Bless on 3 allies (ACT-8)' did not PASS"):
        return False
    st = status()
    party = [r.get("uuid") for r in st.get("party") or []]
    # the spell and caster se's direct item proved (the same reload: the same party and hotbars)
    caster, spell, status_id, n = (HOTBAR_PICK.get(k) for k in ("caster", "spell", "status", "n"))
    if not need(caster in party and len(party) >= (n or 3), HOTBAR_ITEM,
                f"caster={caster} (se's pick), party={len(party)}"):
        return False
    targets = ([caster] + [u for u in party if u != caster])[:n]
    saved, HB.HOTBAR_CAST_ENABLED = HB.HOTBAR_CAST_ENABLED, True
    try:
        out, dt = took("use_ability", ability=spell, targets=[S.H.get(u) for u in targets], who=S.H.get(caster))
    finally:
        HB.HOTBAR_CAST_ENABLED = saved
    time.sleep(1.0)
    blessed = [lua_server(f'return tostring(Osi.HasActiveStatus("{u}", "{status_id}"))') == "1" for u in targets]
    cs = HB.cast_state(S.game, caster)
    ok = ("use_ability: done" in out and "via=hotbar" in out and f"hotbar: {n} of {n}" in out and all(blessed)
          and "charged_by_guard" not in out and not cs.get("armed") and ui_screen() == "game")
    item(HOTBAR_ITEM, "PASS" if ok else "FAIL",
         f"{spell} by {S.H.get(caster)}: {dt:.1f}s; {status_id} on {blessed}; in hand={cs.get('armed')}; "
         + " | ".join(l for l in out.splitlines() if l.startswith(("use_ability", "hotbar", "targets")))[:200])
    if not ok:
        close_targeting()
    return True


TWOSTEP_ITEM = "Dimension Door 10 m via the hotbar (ACT-9 two-step route)"
DD_SPELL = "Teleportation_DimensionDoor"


# Read-only: the container cell's slot VM and Content VM props, each collection prop's first items (the sub-bar's
# variant cells, if the VM carries them), on the selected member's HUD (live window 20261017-152016: e3
# Shadowheart's Teleportation_DimensionDoor read IsContainer at ClassHotBar index 26; its stats have no
# ContainerSpells, so which variants the game boxed there is unknown).
CONTAINER_VM_LUA = (
    "local UI = Mods.BG3AI.BG3AI.UI local dc = UI.dc('HotBar') local out = {} "
    "local function dump(o) local ks = {} for k, v in pairs(UI.props(o)) do "
    "ks[#ks + 1] = tostring(k) .. '=' .. tostring(v):sub(1, 60) end table.sort(ks) return table.concat(ks, ',') end "
    "local bars = dc and UI.path(dc, 'CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.KeyboardHotBars') "
    "for _, bar in UI.items(bars, 16) do if tostring(UI.get(bar, 'HotBarType')) == DECK then "
    "for i, slot in UI.items(UI.get(bar, 'SlotList'), 400) do if i == INDEX then local c = UI.get(slot, 'Content') "
    "out[#out + 1] = 'slot{' .. dump(slot):sub(1, 600) .. '}' out[#out + 1] = 'content{' .. dump(c):sub(1, 1200) .. '}' "
    "for k, v in pairs(UI.props(c)) do if tostring(v):find('Collection', 1, true) then "
    "out[#out + 1] = tostring(k) .. '[' .. tostring(UI.count(v)) .. ']' "
    "for j, x in UI.items(v, 4) do out[#out + 1] = '  ' .. tostring(k) .. '#' .. j .. '{' .. dump(x):sub(1, 400) .. '}' "
    "end end end end end end end "
    "return #out > 0 and table.concat(out, ' | ') or ('no cell ' .. DECK .. ' #' .. INDEX .. ' on the selected HUD')")


def container_probe(u: str, cell: dict) -> str:
    """What a container cell holds: the server's spell entries for the spell (SpellBook / SpellContainer:
    ContainerSpell, LinkedSpellContainer) and the cell's VMs (CONTAINER_VM_LUA, after selecting `u`). Read-only."""
    from bg3ai import hotbar as HB
    srv = spell_state(u, "DimensionDoor")
    try:
        if cell.get("selected") is False:
            S.game.run_job("act.select", {"char": u}, wait_s=5)
            HB._slot_after_select(S.game, DD_SPELL, u)
        vm = str(S.game.eval(f"local DECK, INDEX = '{cell.get('deck')}', {int(cell.get('index') or 0)} "
                             + CONTAINER_VM_LUA, context="client"))
    except Exception as e:  # noqa: BLE001
        vm = f"eval error {e}"
    return f"server {json.dumps(srv)[:1500]} | hud {vm[:3000]}"


def floor_point(p0: list, dist: float) -> list | None:
    """A floor point at p0's height (within 1 m), `dist` m away, well on screen; None when no direction has one."""
    for k in range(8):
        a = k * math.pi / 4
        q = [p0[0] + dist * math.cos(a), p0[1], p0[2] + dist * math.sin(a)]
        hs = lua_server(f"local ok,h=pcall(Ext.Level.GetHeightsAt,{q[0]},{q[2]}) "
                        "if not ok or type(h)~='table' then return '' end local o={} "
                        "for _,v in ipairs(h) do o[#o+1]=string.format('%.2f',v) end return table.concat(o,',')")
        ys = [float(v) for v in hs.split(",") if v.strip()] if hs and "error" not in hs else []
        y = min(ys, key=lambda v: abs(v - p0[1]), default=None)
        if y is None or abs(y - p0[1]) > 1.0:
            continue
        sp = S.game.try_rpc("ui.screen_pos", {"pos": [q[0], y, q[2]]}, context="client") or {}
        nd = sp.get("ndc") or [9, 9]
        if sp.get("on_screen") and abs(nd[0]) < 0.8 and abs(nd[1]) < 0.8:
            return [round(q[0], 2), round(y, 2), round(q[2], 2)]
    return None


def twostep_dd_probe() -> bool:
    """act 2026-10-16, audit ACT-9: a two-step spell by the player's clicks (bridge twostep.py: hotbar cell, who goes =
    the caster, then the landing on the ground, the cursor checked by ui.ground_check before the click). Out of
    combat in BASE, to a floor point ~10 m from the caster that is on screen. HOTBAR_CAST_ENABLED is on for this one
    call, only after se's read-only hotbar item passed. PASS = 'use_ability: done', the caster now <= 1.5 m from the
    landing, nothing in hand, screen game. The PROBE line logs ui.ground_check at the landing (GUESS T1: cursor
    "Cast"/"CastWarning"). A7: a container cell (live L4: the upcast level picker) is cast through the picker's lowest
    real level. SKIP when nobody has Dimension Door on the hotbar, it is greyed out, or the picker has no usable level.
    True = it cast (the caller reloads)."""
    from bg3ai import hotbar as HB
    ro_ok = any(r["item"] == "hotbar widget tree (ACT-8, read-only)" and r["verdict"] == "PASS" for r in RESULTS)
    if not need(ro_ok, TWOSTEP_ITEM, "se's 'hotbar widget tree (ACT-8, read-only)' did not PASS"):
        return False
    st = status()
    if not need(not (S.game.try_rpc("state.combat") or {}).get("active"), TWOSTEP_ITEM, "in combat"):
        return False
    party = {r.get("uuid"): r for r in st.get("party") or [] if r.get("uuid")}
    caster, cell, greyed = None, {}, []
    for u in party:
        if not HB.map_has(HB.server_map(S.game, u), DD_SPELL):
            continue
        # A7 (live L4): read the member's OWN hud. Unselected, ui.hotbar_slot reads the selected character's decks with
        # this member's id map: e5 Shadowheart's DD index 26 was Claude's Blinding Smite there (no container flag).
        r = hb_slot(DD_SPELL, u)
        if r.get("selected") is False:
            S.game.run_job("act.select", {"char": u}, wait_s=5)
            r = HB._slot_after_select(S.game, DD_SPELL, u)
        if not r.get("found"):
            continue
        if r.get("can_use") is False:
            greyed.append(u)
            continue
        caster, cell = u, r
        break
    if caster is None:
        why = (f"{', '.join(str(S.H.get(u)) for u in greyed)} has {DD_SPELL} greyed out (no level-4+ slot left)"
               if greyed else f"nobody in the party has {DD_SPELL} on the hotbar (level 7+ Gale, or a save with it)")
        item(TWOSTEP_ITEM, "SKIP", why + ": run SESSION-WATCH 'two-step by the hotbar' instead")
        return False
    if cell.get("container") or cell.get("upcast"):
        # the cell opens the upcast level picker (live L4); the route picks the lowest real level
        log(f"PROBE twostep container {S.H.get(caster)}: {container_probe(caster, cell)}")
    p0 = party[caster].get("pos")
    land = floor_point(p0, 10)
    if not need(land is not None, TWOSTEP_ITEM, "no floor point 10 m away on screen around the caster"):
        return False
    sp = S.game.try_rpc("ui.screen_pos", {"pos": land}, context="client") or {}
    log(f"PROBE twostep landing {land} screen {json.dumps(sp)}")
    saved, HB.HOTBAR_CAST_ENABLED = HB.HOTBAR_CAST_ENABLED, True
    try:
        out, dt = took("use_ability", ability=DD_SPELL, x=land[0], y=land[1], z=land[2], who=S.H.get(caster))
    finally:
        HB.HOTBAR_CAST_ENABLED = saved
    time.sleep(1.0)
    p1 = party_pos().get(caster) or p0
    off = math.dist([p1[0], p1[2]], [land[0], land[2]])
    cs = HB.cast_state(S.game, caster)
    land_look = ""
    try:
        rows = (S.RUNTIME / "hotbar_cast.jsonl").read_text().splitlines()
        log(f"PROBE twostep result {rows[-1] if rows else '-'}")
        # A17: what the cursor showed at the landing (L8: no landing click was made at all, `one_step`)
        last = json.loads(rows[-1]) if rows else {}
        land_look = (f"; landing looks={last.get('looks')} clicks={last.get('ground_clicks')} "
                     f"one_step={last.get('one_step')} first_s={last.get('first_s')}")
    except (OSError, ValueError):
        pass
    ok = "use_ability: done" in out and off <= 1.5 and not cs.get("armed") and ui_screen() == "game"
    if not ok and "no spell slot left" in out and not cs.get("armed"):
        # the picker greyed every level (a save without the slot): not a route failure
        item(TWOSTEP_ITEM, "SKIP", f"{S.H.get(caster)}: the level picker had no usable level (no slot left); "
             + " | ".join(l for l in out.splitlines() if l.startswith(("use_ability", "hotbar")))[:200])
        return False
    item(TWOSTEP_ITEM, "PASS" if ok else "FAIL",
         f"{dt:.1f}s; now {off:.1f} m from the landing (moved {math.dist(p0, p1):.1f} m); in hand={cs.get('armed')}; "
         + " | ".join(l for l in out.splitlines() if l.startswith(("use_ability", "hotbar", "the spell")))[:220]
         + land_look[:200])
    if not ok:
        close_targeting()
    return True


def strict_lock_probe() -> None:
    """audit UI-5 (patch 0010): the running bg3le has the strict frame-lock mode; the counters read. Then one strict
    minute: strict on, a burst of UI reads (ui.state x40), strict back to what it was (in a finally: the mode is
    process-wide, a load doesn't reset it). PASS = no crash, the reads answer (a refusal reads as a missing field, not
    an error), refusals counted. Needs the rebuilt libbg3le.so. Runs LAST in the last section: a crash costs nothing."""
    try:
        before = S.game.eval("local f = Ext._Internal.UiStrict if not f then return 'missing' end "
                             "local on, n = f() local _, lt, ut = Ext._Internal.UiSerialize() "
                             "return tostring(on) .. ' ' .. n .. ' ' .. lt .. ' ' .. ut", context="client")
    except Exception as e:  # noqa: BLE001
        before = f"eval error {e}"
    if not need(before.split()[0] in ("true", "false"), "bg3le strict frame lock (UI-5, patch 0010)",
                f"UiStrict: {before} (the game runs a bg3le without 0010: relaunch after tools/build_bg3le.sh)"):
        return
    was = before.split()[0]
    errs, t, after = 0, time.monotonic(), "not restored"
    try:
        S.game.eval("Ext._Internal.UiStrict(true) return 1", context="client")
        for _ in range(40):
            u = S.game.try_rpc("ui.state", context="client")
            errs += 0 if isinstance(u, dict) and u.get("screen") else 1
    finally:
        dt = time.monotonic() - t
        try:
            after = S.game.eval(f"Ext._Internal.UiStrict({was}) local on, n = Ext._Internal.UiStrict() "
                                "local _, lt, ut = Ext._Internal.UiSerialize() "
                                "return tostring(on) .. ' ' .. n .. ' ' .. lt .. ' ' .. ut", context="client")
        except Exception as e:  # noqa: BLE001
            after = f"restore failed: {e}"
    lg = bg3le_log()
    crash = grep_crash(lg)
    ok = alive() and not crash and errs <= 2 and after.split()[0] == was
    item("bg3le strict frame lock (UI-5, patch 0010)", "PASS" if ok else "FAIL",
         f"strict/refused/lua_timeouts/ui_timeouts before={before} after={after}; 40 ui.state in {dt:.1f}s, "
         f"{errs} without a screen{'; ' + crash if crash else ''}")


STRICT_ITEM = "strict frame lock across loads + rest (S17)"


def strict_counters(set_to: bool | None = None) -> dict:
    """bg3le's strict frame lock (patch 0010), optionally switched first: {strict, refused, lua_timeouts,
    ui_timeouts} or {error}."""
    arg = "" if set_to is None else ("true" if set_to else "false")
    try:
        parts = S.game.eval("local f = Ext._Internal and Ext._Internal.UiStrict if type(f) ~= 'function' then "
                            f"return 'missing' end local on, n = f({arg}) local _, lt, ut = Ext._Internal.UiSerialize() "
                            "return tostring(on) .. ' ' .. n .. ' ' .. lt .. ' ' .. ut", context="client").split()
    except Exception as e:  # noqa: BLE001
        return {"error": f"eval error {e}"}
    if len(parts) != 4:
        return {"error": " ".join(parts) or "no reply"}
    return {"strict": parts[0] == "true", "refused": int(parts[1]), "lua_timeouts": int(parts[2]),
            "ui_timeouts": int(parts[3])}


def strict_delta(a: dict, b: dict) -> str:
    return " ".join(f"{k} +{b.get(k, 0) - a.get(k, 0)}" for k in ("refused", "lua_timeouts", "ui_timeouts"))


def sec_strict() -> None:
    """S17 (crashes S072/S077/S078, all ~7 s after the hover-click on the rest/travel [ForceUpdate] box, 99/189/33
    unserialized Lua UI calls in those launches; L8: 0 refusals in steady reads, the timeouts come from launches and
    loads): strict ON across 2 load_game + camp + long_rest (the box-click path), counters per step, strict back to
    what it was in a finally. PASS = the game lives, both loads verify, the rest is done, strict restored; refusals
    are evidence for the turn-on decision, not a FAIL. Last section: a crash here costs nothing else."""
    c0 = strict_counters()
    if not need("error" not in c0, STRICT_ITEM, f"UiStrict: {c0.get('error')} (bg3le without patch 0010?)"):
        return
    steps, ok = [], False
    try:
        cur = strict_counters(True)
        for label in (BASE, HOTBAR_SAVE):
            loaded = load_verified(label)
            nxt = strict_counters()
            steps.append(f"load {label}: {'ok' if loaded else 'FAILED'} ({strict_delta(cur, nxt)})")
            cur = nxt
            if not loaded:
                break
        else:
            out, _ = took("camp", action="go")
            time.sleep(2)
            rest, rdt = took("long_rest")
            if "partial" in rest.lower() and "FAILED" in rest:
                rest, rdt = took("long_rest", partial=True)
            nxt = strict_counters()
            rested = ("done" in rest or "rested" in rest) and "FAILED" not in rest
            steps.append(f"camp {first_line(out)[:40]}; long_rest {rdt:.1f}s {first_line(rest)[:80]} "
                         f"({strict_delta(cur, nxt)})")
            ok = rested
    finally:
        end = strict_counters(c0["strict"])
    crash = grep_crash(bg3le_log())
    ok = ok and alive() and not crash and end.get("strict") == c0["strict"]
    log(f"strict counters before {c0}, after {end}")
    item(STRICT_ITEM, "PASS" if ok else "FAIL",
         f"{' | '.join(steps)}; total {strict_delta(c0, end)}; restored strict={end.get('strict')}"
         + (f"; {crash}" if crash else ""))


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

    # audit UI-3 (se, 2026-10-16): the escape hatches don't save or load around save_game/load_game. Bridge refusals
    # (ui_action QuickLoadCommand, press_key F8), the mod's own (ui.command / ui.key straight to the client), and no
    # load happened (the server marker survives). Nothing is saved or loaded either way except on a regression.
    set_marker()
    outs = [call("ui_action", command="QuickLoadCommand", widget="*"), call("press_key", key="F8")]
    mod, mod_ok = [], []
    # the raw mod ops would really quicksave/quickload on a mod without the UI-3 refusal (not hot-reloaded): send them
    # only when the live client Lua has it
    live = S.game.eval("local A = Mods.BG3AI.BG3AI.UIActions return tostring(A and A.saveLoadName ~= nil)",
                       context="client")
    for op, args in [] if live != "true" else (("ui.command", {"widget": "*", "command": "QuickLoadCommand"}), ("ui.key", {"key": "F8"}),
                     ("ui.command", {"widget": "*", "command": "QuickSaveCommand"})):
        what = f"{op} {args.get('command') or args.get('key')}"
        try:
            mod.append(f"{what}: NOT refused {S.game.rpc(op, args, context='client')}")
            mod_ok.append(False)
        except Exception as e:  # noqa: BLE001
            # live window #1 (10-16): the verdict read the hint after a 90-char cut ("go through save_ga") and FAILed
            # three right refusals: judge the whole message, shorten only the evidence
            mod_ok.append(ui3_refused(str(e)))
            mod.append(f"{what}: {'refused' if mod_ok[-1] else 'error'} {str(e)[:70]}")
    time.sleep(3.0)
    loaded = marker_gone()
    if live != "true":
        mod.append(f"mod ops not sent: the live client Lua has no UI-3 refusal (UIActions.saveLoadName={live}): hot reload")
    ok = (live == "true" and all(o.startswith("REFUSED") for o in outs) and len(mod_ok) == 3 and all(mod_ok)
          and not loaded and alive())
    item("ui_action / press_key / ui.command don't save or load (UI-3)", "PASS" if ok else "FAIL",
         f"a load happened={loaded}; mod refusals {sum(mod_ok)}/3 | "
         + " | ".join(first_line(o)[:90] for o in outs) + " | " + " | ".join(mod))

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
    # BACKLOG "save-name probe reads None" (2026-10-16): the Text read is unboxed and falls back to an inner TextBox and
    # the dialog DC's SavegameNameInput; read_via names the source that answered (text/field/inner/dc). PASS with
    # via=dc means the box itself is still unreadable: real saves then re-set the name before each RETURN (blind_sets).
    item("save name box reads our name (probe, no save)", "PASS" if ok else "FAIL",
         f"{r.status} {r.error or ''}; read={res.get('read')!r} via={res.get('read_via')} matches={res.get('matches')} "
         f"renamed={res.get('renamed')}; screen after={scr}; save folders written={stray or 'none'}")

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
        # [act] A13 S069 20:45:31: act.select was done 1 ms after the order while another member held ClientControl;
        # done now means the member holds it (one resync via another member). Read-only on the selection.
        r = S.game.run_job("act.select", {"char": other["uuid"]}, wait_s=5)
        now = status().get("active")
        item("act.select done = the member holds ClientControl (A13)",
             "PASS" if r.status == "done" and now == other["uuid"] else "FAIL",
             f"{r.status} {r.error or ''} result={r.result}; active after={now} (wanted {other['uuid']})")
        # [act] A16 S074 23:06:52: act.select was done on the server while the HUD kept another member (ui.reactions
        # refused). Done now means the client's ClientControl (ui.selected) shows the member; a second select of the
        # same member is the server's no-op order that S074 hit.
        hud = (S.game.try_rpc("ui.selected", context="client") or {}).get("uuid")
        r2 = S.game.run_job("act.select", {"char": other["uuid"]}, wait_s=5)
        hud2 = (S.game.try_rpc("ui.selected", context="client") or {}).get("uuid")
        item("act.select done = the HUD shows the member (A16)",
             "PASS" if r.status == "done" and r2.status == "done" and hud == hud2 == other["uuid"] else "FAIL",
             f"first {r.status}, HUD after={hud}; again {r2.status} {r2.error or ''} result={r2.result}, HUD after="
             f"{hud2} (wanted {other['uuid']}; None = client Lua not hot-reloaded)")
        if sel:
            S.game.run_job("act.select", {"char": sel}, wait_s=5)

    # load_game's crash fast path: the 60 MB Steam-runtime parent must not count, the game must
    from bg3ai import metrics as MX
    pid = MX.game_pid()
    rss = MX.rss_mb(pid)
    alive_now = S._game_process_alive()
    item("load_game game-process check", "PASS" if alive_now and (rss or 0) >= S.GAME_ALIVE_MIN_MB else "FAIL",
         f"_game_process_alive={alive_now}; biggest bg3 pid={pid} rss={rss} MB (min {S.GAME_ALIVE_MIN_MB})")

    # naut helm cinematic (se, 2026-10-16): AutoSkip now skips a line playing in our combat turn (skip command +
    # engine SPACE, never shim SPACE) and autoskip.status says why a line is not being skipped; the bridge hands a
    # stuck line back after DIALOGUE_STUCK_EXIT_S and a read tool waits AUTO_DIALOGUE_READ_LINE_S. BASE has no combat
    # cinematic: this checks the live mod/bridge carry it; the skip itself is the SESSION-WATCH "helm cinematic" row.
    sw = S.game.eval("local K = Mods.BG3AI.BG3AI.AutoSkip return tostring(K and K.TURN_LINE_SKIP) .. ' ' .. "
                     "tostring(K and K.TURN_LINE_SPACE)", context="client")
    ast = S.game.try_rpc("autoskip.status", context="client") or {}
    ok = sw == "true true" and isinstance(ast, dict) and ast.get("on") is True \
        and S.DIALOGUE_STUCK_EXIT_S > 0 and S.AUTO_DIALOGUE_READ_LINE_S < S.DIALOGUE_LINE_WAIT_S
    item("AutoSkip skips lines in our combat turn (helm; switches live)", "PASS" if ok else "FAIL",
         f"TURN_LINE_SKIP/SPACE={sw} (nil nil = client Lua not hot-reloaded); autoskip on={ast.get('on')} "
         f"line={ast.get('line')}; bridge stuck_exit={S.DIALOGUE_STUCK_EXIT_S}s read_line={S.AUTO_DIALOGUE_READ_LINE_S}s")

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
    # LIVE G4 check (Hotbar.lua armedOf): with Jump in hand armed should be true, after the ESCAPE below false
    log("PROBE cast_state with Jump (Z) in hand: "
        + json.dumps(S.game.try_rpc("ui.cast_state", {"char": status().get("active")}, context="client"))[:600])
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
    log("PROBE cast_state after the ESCAPE (nothing in hand): "
        + json.dumps(S.game.try_rpc("ui.cast_state", {"char": status().get("active")}, context="client"))[:600])
    mv = moved_m(p0, party_pos())
    got = any(t.split(": ", 1)[1] != "{}" for t in new_texts)
    item("CursorText probe (Jump targeting)", "PASS" if got and mv < 0.5 else ("FAIL" if mv >= 0.5 else "INFO"),
         f"new texts while targeting={got}; party moved {mv:.1f} m; screen after={after}; " + " | ".join(new_texts)[:220]
         + " (full: se.log PROBE lines)")
    # audit ACT-8 rest: hotbar cell + Bless x3 on HOTBAR_SAVE (spends a slot: after the probes, reloaded right below);
    # the read-only hotbar item still runs on BASE when that save won't load
    hb_save = HOTBAR_SAVE if load_verified(HOTBAR_SAVE, record=True) else BASE
    hotbar_probe()
    load_verified(hb_save)
    if hotbar_use_ability():   # the same Bless x3 through use_ability (act's wiring): reloaded again
        load_verified(hb_save)
    if twostep_dd_probe():   # ACT-9: Dimension Door by the clicks (spends a slot, moves the caster): reloaded
        load_verified(hb_save)
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
        # [act] check_all S11 (19:11:17 / 19:56:53, S068 started on the map): two ui.waypoints back to back (check_all's
        # pattern), the 2nd said done right after its ESCAPE with the map still up. done = closed now: no sleep here.
        rows = []
        for _ in range(2):
            t = time.monotonic()
            r = S.game.run_job("ui.waypoints", {}, context="client", wait_s=20)
            res = r.result if isinstance(r.result, dict) else {}
            rows.append((r.status, ui_screen(), res.get("map_escapes"), res.get("map_open"), time.monotonic() - t))
        ok = all(st == "done" and sc == "game" and not mo for st, sc, _, mo, _ in rows)
        item("ui.waypoints done = map closed (twice in a row)", "PASS" if ok else "FAIL",
             "; ".join(f"{st} screen={sc} escapes={es} map_open={mo} {d:.1f}s" for st, sc, es, mo, d in rows))
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
        # 20261017-145354: the capture waited in rt/shots, the game wrote runtime/shots (BG3AI_SHOTS_DIR now)
        item("screenshot works in the acceptance window (shots dir = the game's)",
             "PASS" if shot == "map image" else "FAIL", f"{shot[:160]} dir={S._shots_dir()}")
        near = call("get_nearby", kinds="door", radius=20)
        # S054 05:54:51: no open/closed on doors → interact closed an open door (a scene started). State.lua: doors
        # carry open/closed (Osi.IsOpened); the locked Foundry door must read "closed".
        drows = re.findall(r"^.*<door>.*$", near, re.M)
        ok = bool(drows) and all(re.search(r"\b(open|closed)\b", r) for r in drows)
        item("doors show open/closed in get_nearby", "PASS" if ok else ("SKIP" if not drows else "FAIL"),
             " | ".join(drows)[:240] or "no door within 20 m")
        doors = re.findall(r"^(?:ITEM\s+|\+\s*)?(e\d+) ([^<\n]+?) <door>[^\n]*\blocked\b", near, re.M)  # "ITEM e8 Steel … <door> 1.6m locked"
        if doors:
            click_search_probe(doors[0][0], doors[0][1].strip())
        if need(bool(doors), "lockpick a door (no-id hover, walk wait)", f"no locked door within 20 m of {DOOR_SAVE}"):
            h, name = doors[0]
            out, dt = took("lockpick", target=h)
            ok = "opened=roll" in out or "unlocked" in out
            # live window #2 (19:51): a Steel Watcher saw the lockpick and the crime scene came in place of the roll.
            # The route (hover, menu row, the game's answer) worked; the tool now says so (reason=interrupted).
            caught = "reason=interrupted" in out and "menu=Lockpick" in out
            ev = (f"{h} {name.strip()}: {first_line(out)[:200]} ({dt:.1f}s) "
                  + grab(r"hover_via=\w+|no_id_objects=\[[^\]]*\]|walked_m=[\d.]+", out)
                  + (" [caught: the game's crime scene, not a roll; the walk wait is not exercised]" if caught else ""))
            ok = ok or caught
            item("lockpick a door (no-id hover, walk wait)", "PASS" if ok else "FAIL", ev)
            if ui_screen() == "roll":
                call("press_key", key="ESCAPE")
        load_verified(BASE)
    strict_lock_probe()  # last: process-wide mode, restored in a finally; nothing runs after it in this section


def click_search_probe(h: str, name: str) -> None:
    """se click_on latency (naut-r2 S001: 83 tries 12.6 s; off_screen x2 7 s): the hover search on the S050 door, new
    (HOVER_V2) then old, hover only (nothing is clicked). PASS = the new search finds the door, in no more time than
    the old one (+0.3 s) or within 2 s. The evidence names the settle mode: "cursor" = the per-try settle on the
    picking helper's cursor works live (~2 frames a try); "frames" = WindowCursorPos never matched (units?), so
    only the order/early-turn gains apply -> BACKLOG for se with the cursor values from a ui.hover_check."""
    from bg3ai import clicker
    inp = S._inp()
    name_ = "click_on hover search: new vs old (tries, s, settle)"
    if not need(inp is not None, name_, "input shim not available"):
        return
    u = S._world_uuid(h)
    saved, got = clicker.HOVER_V2, {}
    try:
        # window #1 (10-16) ran new first: its two occluded turns left old a view where the first point hit (1 try)
        # while new had started from the hinge view. Now old first, its occluded turns / zoom undone, then new:
        # both start from the same view.
        for v2 in (False, True):
            clicker.HOVER_V2 = v2
            t = time.monotonic()
            centered = clicker.center_on_actor(S.game, inp, u)
            moves = clicker.bring_into_view(S.game, inp, u)
            fn = clicker._search_v2 if v2 else clicker._search_v1
            pos, tries, last = fn(S.game, inp, u, moves, {}, lambda d: None)
            got[v2] = (pos, tries, time.monotonic() - t, last.get("settle"), (["center"] if centered else []) + moves)
            w, hh = inp.window_size()
            inp.move(w // 2, hh - 8)   # off the door before the next search
            if not v2:
                undo_search_camera(inp, u, moves)
            time.sleep(0.3)
    except Exception as e:  # noqa: BLE001
        item(name_, "FAIL", f"{h} {name}: {type(e).__name__}: {e}")
        return
    finally:
        clicker.HOVER_V2 = saved
    (p2, t2, s2, st2, m2), (p1, t1, s1, _, m1) = got[True], got[False]
    ok, why = hover_compare(got[False], got[True])
    item(name_, "PASS" if ok else "FAIL",
         f"{h} {name}: new {'hit' if p2 else 'MISS'} {t2} tries {s2:.2f}s settle={st2} camera={m2}; "
         f"old {'hit' if p1 else 'MISS'} {t1} tries {s1:.2f}s camera={m1}" + (f"; {why}" if why else ""))


OCCLUDED_TURNS = {"rotate_occluded_right": 1, "rotate_occluded_left": -2}   # clicker's turn multipliers


def undo_search_camera(inp, uuid: str, moves: list[str]) -> None:
    """Turn back a search's occluded turns and zoom-out (bring_into_view's own moves stay: the next search
    re-runs it), so the next search starts from the same view."""
    from bg3ai import clicker
    net = sum(OCCLUDED_TURNS.get(m, 0) for m in moves)
    if net:
        clicker._turn(S.game, inp, uuid, -net)
    if "zoom_out" in moves:
        w, hh = inp.window_size()
        inp.wheel(2, w // 2, hh // 2)
        clicker.settle_camera(S.game, inp, uuid)


def hover_compare(old: tuple, new: tuple) -> tuple[bool, str]:
    """(pos, tries, s, settle, camera) of the old and the new search from the same view -> (ok, why not).
    New must hit, in <= old + 0.3 s (or <= 2 s); and where old hit without an occluded turn on its first aim's
    ring or an aim's centre (<= 9 + aims-1 tries), new must not turn the camera either nor take more tries."""
    from bg3ai import clicker
    (p1, t1, s1, _, m1), (p2, t2, s2, _, m2) = old, new
    if p2 is None:
        return False, "new missed"
    if not (s2 <= s1 + 0.3 or s2 <= 2.0):
        return False, f"new slower: {s2:.2f}s vs {s1:.2f}s"
    turned = lambda m: any(x in OCCLUDED_TURNS for x in m)  # noqa: E731
    if p1 is not None and not turned(m1) and t1 <= 9 + len(clicker.AIMS) - 1:
        if turned(m2):
            return False, "new turned the camera where old hit without a turn"
        if t2 > t1:
            return False, f"new took more tries ({t2} > {t1})"
    return True, ""


FUNCS = {"reads": sec_reads, "rest": sec_rest, "trade": sec_trade, "gear": sec_gear, "swap": sec_swap,
         "nav": sec_nav, "loads": sec_loads, "combat": sec_combat, "se": sec_se}


# create_character targets after the CC pacing round (impl-setup part 45; naut 2026-10-13 run 3: cc_apply 6.1 s,
# naming 5.2 s, 22.1 s in all; runs 1-2 needed 3 calls). Since S6 (impl-setup part 56) the tool returns once the
# world has loaded, so total_ms holds the game's ~8-12 s load (world_ms): game time, shown, not judged. Judged: our
# own phases, cc_apply + naming (with the finish press) + the first look (impl-setup part 57; naut3: 1.9 s + 1.0-1.7 s
# + ~0.3-0.6 s; records before part 57 have no first_look_ms: apply + naming).
CC_MAX_APPLY_MS, CC_MAX_NAMING_MS, CC_MAX_OWN_MS = 3000, 2500, 5000


def cc_own_ms(r: dict) -> int | None:
    """Our part of a create_character record: cc_apply + naming + first look (the world load is the game's)."""
    if r.get("apply_ms") is None or r.get("naming_ms") is None:
        return None
    return r["apply_ms"] + r["naming_ms"] + (r.get("first_look_ms") or 0)


def cc_verdict(recs: list[dict]) -> tuple[str, str]:
    """The last character creation's create_character records (runtime/stage_ms.jsonl op "create_character", one
    per call; a creation = the calls within 15 min of the last one) -> (verdict, evidence)."""
    recs = [r for r in recs if r.get("op") == "create_character" and "pacing" in r]
    if not recs:
        return "SKIP", "no create_character since the CC pacing round: needs a naut-style run (new game + CC)"
    last = [r for r in recs if r.get("t", 0) >= recs[-1].get("t", 0) - 900]
    ok = [r for r in last if r.get("ok")]
    fin = ok[-1] if ok else last[-1]
    ev = (f"{len(last)} call(s) ({sum(1 for r in last if not r.get('ok'))} failed"
          + "".join(f"; {str(r.get('error'))[:60]}" for r in last if not r.get("ok"))
          + f"), pacing {fin.get('pacing')}: cc_apply {fin.get('apply_ms')} ms, naming {fin.get('naming_ms')} ms "
          f"({fin.get('how')}, {fin.get('popups')} popup(s), {fin.get('naming_stages')}), first look "
          + (f"{fin['first_look_ms']} ms" if "first_look_ms" in fin else "not logged")
          + f" = ours {cc_own_ms(fin)} ms (target <= {CC_MAX_OWN_MS}); "
          f"world load {fin.get('world_ms')} ms (game time, not judged), total {fin.get('total_ms')} ms")
    good = (len(last) == 1 and bool(ok) and (fin.get("apply_ms") or 1e9) <= CC_MAX_APPLY_MS
            and (fin.get("naming_ms") or 1e9) <= CC_MAX_NAMING_MS and (cc_own_ms(fin) or 1e9) <= CC_MAX_OWN_MS)
    return ("PASS" if good else "FAIL"), ev


def sec_setup() -> None:
    """setup's fixes (impl-setup parts 34-35, 41, 45) on the loaded base save: ramwatch's anon+swap kill
    signal, the story scorer naming a new journal step by its text, enemy statuses as lesson terms; and the last
    character creation's time and attempts (read from runtime/stage_ms.jsonl, no game action)."""
    recs = []
    try:
        for ln in (REPO / "runtime" / "stage_ms.jsonl").read_text(errors="replace").splitlines():
            try:
                recs.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    item("create_character: 1 attempt, time (last CC)", *cc_verdict(recs))
    sys.path.insert(0, str(REPO / "runner"))
    import ramwatch
    from bg3ai import lessons, progress
    # 1. ramwatch: the watchdog that holds runtime/ramwatch.lock writes anon+swap (else it runs old code)
    pids = ramwatch.game_pids()
    a_s, rss = sum(ramwatch.anon_swap_mb(p) for p in pids), sum(ramwatch.rss_mb(p) for p in pids)
    # the newest TICK line (a spike line, e.g. from the save load just before, can be the file's last line)
    t_last, last = ramwatch.last_tick(REPO / "runtime" / "ram.log") or (0.0, "no anon+swap tick line in ram.log")
    fresh = time.time() - t_last < 180
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
    # 7. S11: check_all's waypoints check left the world map open (19:11 + 19:56 2026-10-17; S068 started on
    # screen=map). Its waypoints + screen rows as the runner runs them: the game view after, 2 s later too.
    sys.path.insert(0, str(REPO / "tools" / "dev"))
    import check_all as CA
    if need(ui_screen() == "game", "check_all leaves the game view", f"screen {ui_screen()} before the check"):
        wp, scr_row = CA.check_waypoints(S.game), CA.check_screen(S.game, "game")
        time.sleep(2.0)
        scr = ui_screen()
        item("check_all leaves the game view", "PASS" if scr == "game" and scr_row.verdict != "FAIL" else "FAIL",
             f"waypoints {wp.verdict}: {wp.detail[-120:]} | screen row {scr_row.verdict}: {scr_row.detail} | "
             f"screen 2 s after={scr}")


FUNCS["setup"] = sec_setup
FUNCS["strict"] = sec_strict


def lua_server(code: str) -> str:
    return str(S.game.eval(code, context="server"))


PREPARED_ITEM = "unprepared spell refused + hidden (A4 fairness)"


def prepared_pick(states: dict) -> tuple | None:
    """(member, an unprepared spell, a prepared one or None) from {member: spell_state(member, '')}: the first
    member whose book has a spell that needs preparing and isn't prepared (a cleric / paladin / wizard)."""
    for u, stt in states.items():
        book = stt.get("book") or []
        off = [b["id"] for b in book if b.get("unprepared")]
        if off:
            on = [b["id"] for b in book if b.get("prep") == "RequiresPreparation" and b.get("prepared")]
            return u, off[0], (on[0] if on else None)
    return None


def prepared_guard_probe() -> None:
    """A4 (2026-10-17): Guard.known_spell's prepared check never ran (p.Prototype on a SpellMetaId raises in
    bg3se), so the Osiris route cast any class spell and state.actions listed the whole class list. Read-only:
    PASS = an unprepared spell is refused 'not prepared' and missing from state.actions, a prepared one passes."""
    g = "local G=Mods.BG3AI.BG3AI.Guard "
    states = {r.get("uuid"): spell_state(r.get("uuid"), "") for r in status().get("party") or [] if r.get("uuid")}
    bad = {S.H.get(u): s["why"] for u, s in states.items() if s.get("why")}
    pick = prepared_pick(states)
    books = ", ".join(f"{S.H.get(u)} {len(s.get('book') or [])}" for u, s in states.items())
    if not need(pick is not None, PREPARED_ITEM,
                f"no member has an unprepared spell (unread: {bad or 'none'}; book entries: {books})"):
        return
    u, off, on = pick
    res = {}
    for spell in [s for s in (off, on) if s]:
        res[spell] = lua_server(g + f'local ok,e=pcall(G.known_spell,"{u}","{spell}") '
                                'return ok and "ok" or (G.is_refusal(e) and e.reason or tostring(e))')
    v_off = res[off]
    v_on = res[on] if on else "ok (no prepared one to try)"
    listed = lua_server(f'local n=0 for _, a in ipairs(Mods.BG3AI.BG3AI.State.actions("{u}")) do '
                        f'if a.spell == "{off}" then n = n + 1 end end return tostring(n)')
    ok = "not prepared" in v_off and v_on.startswith("ok") and listed == "0"
    item(PREPARED_ITEM, "PASS" if ok else "FAIL",
         f"{S.H.get(u)}: {off} -> {v_off[:80]}; listed in state.actions x{listed}; prepared {on} -> {v_on[:60]}")


SPELLBOOK_PROBE_ITEM = "spellbook probe (A5, read-only)"
PREPARE_ITEM = "prepare_spells Bless on Claude: prepared, then unprepared; hotbar slot reported (A5, A15)"
PREPARE_REFUSE_ITEM = "prepare_spells honest refusals (A5)"
PREPARE_LIMIT_ITEM = "prepare_spells limit = the page's (A7, read-only)"
SPELLBOOK_ICON_ITEM = "spellbook cells by icon sheet place (A8, read-only)"
PREPARE_WHO = "Claude"     # the Paladin in HOTBAR_SAVE; Bless is on his class list, unprepared at S065-end (A4)


def _member(name: str) -> str | None:
    return next((r.get("uuid") for r in status().get("party") or [] if r.get("name") == name), None)


def spellbook_probe() -> None:
    """A5: the spellbook page as bg3le reads it, read-only (Client/Spellbook.lua GUESSES S1-S4): K opens the page,
    the selected character's preparing book has cells (A7: one look line each; A8: `icon=atlas@u,v`),
    PreparedSpellsStat reads, and the widget's DataContext has ToggleCharacterAction (CanExecute per cell). The page
    is closed again; nothing is toggled. PASS = a preparing book with cells and a max, and the command present.
    A7 also: PREPARE_LIMIT_ITEM = the server's limit (act.prepared max: SpellBookPrepares.ClassPreparedSpellCount,
    GUESS P1) equals the page's PreparedSpellsStat.Max (live L4: 12; Stats.MaxPreparedSpells read 9).
    A8: SPELLBOOK_ICON_ITEM = every spell that needs preparing finds its cell(s) by its stats Icon's place on the icon
    sheet (Ext.StaticData.GetIconUVs/GetIconAtlas, GUESS S5) in its own state: prepared -> on, unprepared -> off."""
    u = _member(PREPARE_WHO)
    if not need(u is not None, SPELLBOOK_PROBE_ITEM, f"no {PREPARE_WHO} in the party"):
        return
    # A8: the stats Icon of each spell that needs preparing, by state (Server/Guard.lua prep_info, the plan's look)
    raw_icons = lua_server(f"local G=Mods.BG3AI.BG3AI.Guard local i=G.prep_info(G.guid('{u}')) local o={{}} "
                           "for _,k in ipairs({'prepared','unprepared'}) do for _,r in ipairs(i[k]) do "
                           "o[#o+1]=k:sub(1,1)..'|'..r.name..'|'..tostring(r.look and r.look.icon) end end "
                           "return table.concat(o,';')")
    spells = [x.split("|") for x in raw_icons.split(";") if x.count("|") == 2]
    S.game.run_job("act.select", {"char": u}, wait_s=5)
    time.sleep(0.5)
    r = S.game.run_job("ui.prepare_spells", {"name": PREPARE_WHO, "probe": True,
                                             "icons": sorted({ic for _, _, ic in spells if ic != "nil"})},
                       context="client", wait_s=15)
    res = r.result if isinstance(r.result, dict) else {}
    p = res.get("probe") or {}
    icons = p.get("icons") or {}
    log("PROBE spellbook icons: " + json.dumps(icons)[:4000])
    # each icon's cells, by state, = the spells with that icon in that state (shared icons count together)
    want: dict = {}
    for k, _, ic in spells:
        on, off = want.get(ic, (0, 0))
        want[ic] = (on + (k == "p"), off + (k == "u"))
    bad = [f"{name}({'on' if k == 'p' else 'off'}): cells on/off {icons.get(ic, {}).get('on')}/"
           f"{icons.get(ic, {}).get('off')} want {want[ic][0]}/{want[ic][1]}"
           for k, name, ic in spells if (icons.get(ic, {}).get("on"), icons.get(ic, {}).get("off")) != want[ic]]
    bless = icons.get("Spell_Enchantment_Bless") or {}
    item(SPELLBOOK_ICON_ITEM, "PASS" if spells and not bad else "FAIL",
         (f"{len(spells)} spells, {len(spells) - len(bad)} matched by icon; " + ("; ".join(bad[:6]) if bad else "all")
          + f"; Bless uv={bless.get('uv')} raw={bless.get('raw')}")[:400])
    log("PROBE spellbook: " + json.dumps({"status": r.status, "error": r.error, "opened": res.get("opened"),
                                          "closed": res.get("closed"), "probe": p})[:6000])
    books = p.get("preparing") or []
    ok = r.status == "done" and bool(p.get("toggle_cmd")) and any(b.get("cells") and b.get("max") for b in books)
    item(SPELLBOOK_PROBE_ITEM, "PASS" if ok else "FAIL",
         f"{r.status} {r.error or ''}; shows={p.get('selected')} metadata={p.get('metadata')} dc={p.get('dc')} "
         f"toggle_cmd={p.get('toggle_cmd')}; preparing="
         + "; ".join(f"{b.get('class')} {b.get('value')}/{b.get('max')} {len(b.get('cells') or [])} cells"
                     for b in books) + " (full: acceptance.log PROBE spellbook)")
    for b in books:
        log(f"PROBE spellbook cells {b.get('class')}: " + " | ".join(b.get("cells") or []))
    rows = (S.game.try_rpc("act.prepared", {"uuids": [u]}) or {}).get("casters") or []
    row = rows[0] if rows else {}
    raw = lua_server(f"local p=Ext.Entity.Get('{u}').SpellBookPrepares local o={{}} "
                     "for k,v in pairs(p.ClassPreparedSpellCount) do o[#o+1]=tostring(k):sub(1,8)..'='..tostring(v) end "
                     "local f={} for k,v in pairs(p.ClassFallbackPreparedSpellCount) do "
                     "f[#f+1]=tostring(k):sub(1,8)..'='..tostring(v) end "
                     "return 'count{'..table.concat(o,',')..'} fallback{'..table.concat(f,',')..'}'")
    page = next((b.get("max") for b in books if b.get("max")), None)
    ok_l = page is not None and row.get("max") is not None and float(row["max"]) == float(page)
    item(PREPARE_LIMIT_ITEM, "PASS" if ok_l else "FAIL",
         f"page {page}; server max={row.get('max')} (class_max={row.get('class_max')}, "
         f"stats_max={row.get('stats_max')}, count={row.get('count')}); SpellBookPrepares {raw[:160]}")


def prepare_spells_refusals() -> None:
    """A5: a spell not in the book is refused by name, and a member that doesn't prepare (no act.prepared row: a
    barbarian, warlock, ...) is refused with the level-up hint; neither opens the spellbook."""
    if not need(_member(PREPARE_WHO) is not None, PREPARE_REFUSE_ITEM, f"no {PREPARE_WHO} in the party"):
        return
    out1 = call("prepare_spells", who=PREPARE_WHO, add=["Fireball"])
    preparing = {c.get("uuid") for c in (S.game.try_rpc("act.prepared", {}) or {}).get("casters") or []}
    other = next((r.get("name") for r in status().get("party") or [] if r.get("uuid") not in preparing), None)
    out2 = call("prepare_spells", who=other, add=["Bless"]) if other else ""
    ok1 = f"{PREPARE_WHO} doesn't know Fireball" in out1
    ok2 = (not other) or "doesn't prepare spells" in out2
    item(PREPARE_REFUSE_ITEM, "PASS" if ok1 and ok2 else "FAIL",
         f"Fireball: {first_line(out1)[:120]} | {other or '(every member prepares)'}: {first_line(out2)[:120]}")


def _bless_state(u: str) -> tuple[bool, bool, dict]:
    """(Bless prepared on the server, Bless on the server hotbar map, the act.prepared row). The map is read fresh
    (A15: L7 read one cached before the prepare)."""
    from bg3ai import hotbar as HB
    rows = (S.game.try_rpc("act.prepared", {"uuids": [u]}) or {}).get("casters") or []
    row = rows[0] if rows else {}
    prepared = any(r.get("id") == "Target_Bless" for r in row.get("prepared") or [])
    HB.forget_map(u)
    return prepared, HB.map_has(HB.server_map(S.game, u), "Target_Bless"), row


def _bless_slot(u: str) -> str:
    """Where the player sees Bless: the HUD deck cell (ui.hotbar_slot), or that no deck has it."""
    from bg3ai import hotbar as HB
    from bg3ai.game import GameError
    try:
        r = HB._slot(S.game, "Target_Bless", u)
    except GameError as e:
        return f"slot read failed: {e}"
    return f"{r.get('deck')} #{r.get('index')}" if r.get("found") else "on no deck"


def _wait_bless(u: str, want: bool, s: float = 3.0) -> tuple[bool, bool, dict]:
    """Until Bless is prepared == want and its hotbar presence matches too (an unslotted spell waits the whole s)."""
    end = time.monotonic() + s
    while True:
        st = _bless_state(u)
        if (st[0] == want and st[1] == want) or time.monotonic() > end:
            return st
        time.sleep(0.3)


def prepare_bless_round_trip() -> None:
    """A5: prepare_spells(Claude, add Bless) through the spellbook page -> Bless prepared on the server; then remove
    it again -> not prepared. A full book swaps out his last prepared spell for the test and puts it back.
    HOTBAR_SAVE is never saved over. A15: PASS does not assume the game slots a newly prepared spell (L7: no hotbar
    cell after the prepare, from a stale map read); where it went is reported: the server map (fresh) and the HUD
    deck after the prepare, and whether the spell swapped out left the map."""
    u = _member(PREPARE_WHO)
    if not need(u is not None, PREPARE_ITEM, f"no {PREPARE_WHO} in the party"):
        return
    was, on_map, row = _bless_state(u)
    if not need(bool(row), PREPARE_ITEM, f"{PREPARE_WHO} has no act.prepared row (no preparing class read)"):
        return
    log(f"PROBE prepare before: Bless prepared={was} on_map={on_map}; {row.get('count')}/{row.get('max')} "
        + ", ".join(r.get("name", "?") for r in row.get("prepared") or []))
    swap = []
    if not was and row.get("max") is not None and row.get("count", 0) >= row["max"] and row.get("prepared"):
        swap = [row["prepared"][-1]["name"]]
    first = {"remove": ["Bless"], "add": []} if was else {"add": ["Bless"], "remove": swap}
    out1, dt1 = took("prepare_spells", who=PREPARE_WHO, **first)
    st1 = _wait_bless(u, not was)
    slot1 = _bless_slot(u)
    from bg3ai import hotbar as HB
    swap_id = next((r.get("id") for r in row.get("prepared") or [] if swap and r.get("name") == swap[0]), None)
    swap_left = (not HB.map_has(HB.server_map(S.game, u), swap_id)) if swap_id else None
    second = {"add": ["Bless"], "remove": []} if was else {"remove": ["Bless"], "add": swap}
    out2, dt2 = took("prepare_spells", who=PREPARE_WHO, **second)
    st2 = _wait_bless(u, was)
    ok = (first_line(out1).startswith("prepare_spells: done") and st1[0] == (not was)
          and first_line(out2).startswith("prepare_spells: done") and st2[0] == was)
    item(PREPARE_ITEM, "PASS" if ok else "FAIL",
         f"1) {first_line(out1)[:110]} ({dt1:.1f}s) -> prepared={st1[0]} on_map={st1[1]} hud={slot1}; "
         f"2) {first_line(out2)[:110]} ({dt2:.1f}s) -> prepared={st2[0]} on_map={st2[1]}"
         + (f"; swapped out {swap[0]} and back ({swap_id} left the map: {swap_left})" if swap else ""))


BLOCKING_STATUS_ITEM = "cast guard reads Stunned/Fearful status types (A9, read-only)"
POINT_SCROLL_ITEM = "point scroll aimed at the ground: Misty Step 6 m (A9)"


def point_scroll_probe() -> None:
    """A9 (S066 #66 Cone of Cold, #143 Misty Step: "cast on <the reader> by a click", the x,y,z dropped): out of combat
    in SCROLL_SAVE, Karlach reads her Scroll of Misty Step at a floor point 6 m away on screen (bridge
    twostep.ground_click: the cursor checked by ui.ground_check, then the click). PASS = the result says "aimed at
    the ground" and "moved", and she stands within 1.5 m of the point. Spends the scroll in memory only (not saved)."""
    if not load_verified(SCROLL_SAVE, record=True):
        item(POINT_SCROLL_ITEM, "SKIP", f"{SCROLL_SAVE} did not load")
        return
    inv = S.game.try_rpc("state.inventory", {}) or {}
    it = next((x for x in inv.get("items") or [] if isinstance(x, dict)
               and str(x.get("name") or "").lower() == "scroll of misty step"), None)
    if not need(it is not None, POINT_SCROLL_ITEM, f"no Scroll of Misty Step carried in {SCROLL_SAVE}"):
        return
    if not need(not (S.game.try_rpc("state.combat") or {}).get("active"), POINT_SCROLL_ITEM, "in combat"):
        return
    u = it["owner"]
    p0 = party_pos().get(u)
    land = floor_point(p0, 6) if p0 else None
    if not need(land is not None, POINT_SCROLL_ITEM, "no floor point 6 m away on screen around the reader"):
        return
    out, dt = took("use_ability", ability="Scroll of Misty Step", x=land[0], y=land[1], z=land[2], who=S.H.get(u))
    time.sleep(1.0)
    p1 = party_pos().get(u) or p0
    off = math.dist([p1[0], p1[2]], [land[0], land[2]])
    head = first_line(out)
    # A17: L8 said "nothing cast" while Karlach moved 5.1 m: is the scroll still carried?
    inv2 = S.game.try_rpc("state.inventory", {}) or {}
    left = any(isinstance(x, dict) and x.get("uuid") == it.get("uuid") for x in inv2.get("items") or [])
    ok = "aimed at the ground" in head and " moved " in head and off <= 1.5 and not left
    item(POINT_SCROLL_ITEM, "PASS" if ok else "FAIL",
         f"{head[:420]} ({dt:.1f}s); {off:.1f} m from the point {land}, moved {math.dist(p0, p1):.1f} m; "
         f"scroll still carried={left}")


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
    prepared_guard_probe()
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
    # ACT-9: the TargetPos2 engine write (unproven struct mapping, HANDOFF §7) is OFF by default, and a two-step
    # spell is refused before any order. The probe below writes it, so it runs only when asked for
    # (BG3AI_PROBE_TASK_POS2=1, an attended window: watch for a crash in the next minutes).
    v = lua_server(g + 'if A.TASK_POS2==nil then return "no A.TASK_POS2" end return tostring(A.TASK_POS2)')
    item("act-9 second-pick write off by default", "PASS" if v == "false" else "FAIL", v[:200])
    if os.environ.get("BG3AI_PROBE_TASK_POS2", "0") in ("", "0"):
        item("revivify two-step spot set", "SKIP", "the TargetPos2 write is off (ACT-9); BG3AI_PROBE_TASK_POS2=1 probes it")
    else:
        # Revivify is two-step (S057/S058 0 of 14): the live stats say so, and the queued UseSpell task takes the
        # ground spot (TargetPos2). Probe on a living companion: the task is queued, given its spot, then purged.
        v = lua_server(g + 'local h=Osi.GetHostCharacter() local o '
                       'for _,r in pairs(Osi.DB_PartyMembers:Get(nil)) do if not G.same(r[1],h) then o=G.guid(r[1]) break end end '
                       'if not A.two_step_spell then return "no A.two_step_spell" end '
                       'if not A.two_step_spell("Teleportation_Revivify") then return "stats: not two-step" end '
                       'if not o then return "no companion" end '
                       'Osi.UseSpell(h,"Teleportation_Revivify",o,"NULL_00000000-0000-0000-0000-000000000000",1) '
                       'A.TASK_POS2=true local ok,why=A.set_task_pos2(h,"Teleportation_Revivify",A.place_spot(h,o)) '
                       'A.TASK_POS2=false Osi.PurgeOsirisQueue(h) return ok and "set" or ("not set: "..tostring(why))')
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
    # audit ACT-11/13/15 (offline-fixed 2026-10-16): the combat no-start timeout, G.q queries only (an action name
    # through it is refused, never run), CharacterMoveTo blocked, approval seeded per companion after the load.
    v = lua_server(g + 'local E=Mods.BG3AI.BG3AI.Events local h=Osi.GetHostCharacter() '
                   'local seeded=0 for k,_ in pairs(E.approval or {}) do if tostring(k):find("|",1,true) then seeded=seeded+1 end end '
                   'return tostring(A.CAST_NO_START_COMBAT_MS).."|"..tostring(G.query_ok and G.query_ok("IsLocked")).."|"'
                   '..tostring(G.query_ok and G.query_ok("Unlock")).."|"..(G.query_ok and tostring(G.q("Unlock",h,h)) or "old mod: not called").."|"'
                   '..tostring(G.ALLOW.CharacterMoveTo).."|"..tostring(G.BLOCK.CharacterMoveTo).."|"..tostring(seeded)')
    parts = v.split("|")
    ok = len(parts) == 7 and parts[:6] == ["3000", "true", "false", "nil", "nil", "true"]
    item("act audit ACT-11/13/15 live state", "PASS" if ok and parts[6] != "0" else ("FAIL" if not ok else "SKIP"),
         f"no_start_combat|query_ok(IsLocked)|query_ok(Unlock)|q(Unlock)|ALLOW.MoveTo|BLOCK.MoveTo|approval seeded: {v[:160]}"
         + ("" if not ok or parts[6] != "0" else " (no companion rating seeded: solo party?)"))
    # act combat target gone (BACKLOG ~937, offline 2026-10-16): a character the game doesn't know reads gone=off_stage
    # (the reloaded mod carries the field); the ladder flag is on (A.LADDER_WATCH_AT_USE).
    ts = S.game.try_rpc("act.target_state", {"uuid": "c0ffee00-0000-4000-8000-000000000bad"}) or {}
    v = lua_server(g + 'return tostring(A.LADDER_WATCH_AT_USE).."|"..tostring(A.gone_why ~= nil)')
    item("act target gone + ladder watch flag live", "PASS" if ts.get("gone") == "off_stage" and v == "true|true"
         else "FAIL", f"target_state(unknown uuid)={ts}; LADDER_WATCH_AT_USE|gone_why: {v[:80]}")
    # A9 (S066 #100/#130, S067 #32/#43/#54): the cast guard reads the caster's status types (Stunned refused, a dropped
    # order while Fearful names it). Read-only: the stats types the check keys on, and the check on the host now.
    v = lua_server(g + 'if not G.blocking_status then return "no G.blocking_status" end '
                   'local b=G.blocking_status(Osi.GetHostCharacter()) '
                   'return tostring(Ext.Stats.Get("FEARED").StatusType).."|"..tostring(Ext.Stats.Get("STUNNED").StatusType)'
                   '.."|"..tostring(b and b.name)')
    item(BLOCKING_STATUS_ITEM, "PASS" if v.startswith("FEAR|INCAPACITATED|") else "FAIL",
         f"FEARED|STUNNED StatusType|host's blocking status: {v[:160]}")
    # latency round (2026-10-16): a shared-group end is reported after 0.4 s (Lua A.END_REQUEST_AFTER_MS, was 1.5 s);
    # dialogue_advance with no conversation anywhere returns in ~0.5 s (was 2.5 s: naut "0 filler line(s)" 2.7 s).
    v = lua_server(g + 'return tostring(A.END_REQUEST_AFTER_MS)')
    out, dt = took("dialogue_advance")
    ok = v == "400" and "conversation ended" in out and dt < 1.5
    item("act latency: end hold 0.4 s + dialogue_advance idle exit", "PASS" if ok else "FAIL",
         f"END_REQUEST_AFTER_MS={v}; dialogue_advance {dt:.1f}s: {first_line(out)[:80]}; bridge END_ASYNC={S.END_ASYNC} "
         f"TURN_POLL_S={S.TURN_POLL_S} DIALOGUE_IDLE_OPEN_S={S.DIALOGUE_IDLE_OPEN_S}")
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
    # A5 prepare_spells on HOTBAR_SAVE (Claude the Paladin, L10): the read-only spellbook probe, then Bless prepared
    # and unprepared again (nothing is saved; the next section loads its own save)
    if not load_verified(HOTBAR_SAVE, record=True):
        for name in (SPELLBOOK_PROBE_ITEM, SPELLBOOK_ICON_ITEM, PREPARE_LIMIT_ITEM, PREPARE_ITEM, PREPARE_REFUSE_ITEM):
            item(name, "SKIP", f"{HOTBAR_SAVE} did not load")
    else:
        spellbook_probe()
        prepare_spells_refusals()
        prepare_bless_round_trip()
    # A6 helm ladders (live L5: it sat at the end of sec_vis, so --only act never ran it); loads its own save
    helm_ladder_probe()
    # A9 (S066 #66/#143): a point scroll is aimed at the ground at its x,y,z; loads its own save
    point_scroll_probe()


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
    #    S027-start is at camp: no door/container in get_frontier (window 20261016-183253), so a <usable> lead or a
    #    look listing counts too (V.STATIC_KINDS: the same staticSeen rule for every kind)
    fr = call("get_frontier")
    ids = static_lead_ids(fr) or static_lead_ids(call("look", full=True))
    if need(bool(ids), "vis: look_at on a door seen a moment ago",
            "no door/container/usable in get_frontier or look at the base save"):
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
    # 13. NAV-3 (audit 2026-10-13): the kill list is per playthrough (nav_guard_<host uuid>.json) and kills expire.
    #     Read-only: the save is the real playthrough's, so its file holds that playthrough's kills. Kills need the
    #     mod's wall clock (L8 2026-10-18: N.nowS read os.time, which the SE sandbox lacks: every kill undated); a
    #     load dates an undated kill, so any kill listed here must carry `t`.
    lg = S.game.try_rpc("nav.level_guard", {}) or {}
    f = str(lg.get("file") or "")
    dated = all(isinstance(v, dict) and v.get("t") for v in (lg.get("slow") or {}).values())
    now = lua_server(g + "return tostring(N.nowS())")
    skew = abs(float(now) - time.time()) if re.fullmatch(r"\d+(\.\d+)?", now) else None
    item("vis: NAV-3 kill list per playthrough, kills dated", "PASS" if f.startswith("BG3AI/nav_guard_") and
         lg.get("ttl_s") and dated and skew is not None and skew < 120 else "FAIL",
         f"file={f} ttl_s={lg.get('ttl_s')} clock={now} (skew {skew}) slow={lg.get('slow')}")
    # 14. NAV-2: on a killed level nav.frontier searches <= 2 new directions and says so (the level is put back after
    #     unless it was already off)
    lvl, was = lg.get("level"), lg.get("here")
    if need(bool(lvl), "vis: NAV-2 killed level frontier throttled", "no current level"):
        # The kill is in memory only: the file (nav_guard_<host>.json) is the real playthrough's, and
        # nav.level_guard kill/clear would save it and wipe that level's strikes. The finally rewrites the file
        # from memory in case a strike saved it with our kill meanwhile.
        key = json.dumps(lvl)
        if not was:
            lua_server(g + f'N.guardState().slow[{key}] = {{why="acceptance NAV-2", t=N.nowS()}} return 1')
        try:
            lua_server(g + "N.waysCache, N.waysCacheN = {}, 0 return 1")
            fr = S.game.try_rpc("nav.frontier", {"hazards": False}) or {}
        finally:
            if not was:
                lua_server(g + f"local gs = N.guardState() gs.slow[{key}] = nil "
                           "Ext.IO.SaveFile(N.guardFile(), Ext.Json.Stringify(gs, {Beautify = false})) return 1")
        n = len(fr.get("ways") or [])
        item("vis: NAV-2 killed level frontier throttled", "PASS" if n <= 2 and fr.get("ways_off") and
             (fr.get("ms") or 0) < 60 else "FAIL", f"ways={n} ways_off={fr.get('ways_off')} ms={fr.get('ms')}")
    # 15. NAV-6: the arrival read (ways_cached) never searches: a cold spot answers fast
    lua_server(g + "N.waysCache, N.waysCacheN = {}, 0 return 1")
    fr = S.game.try_rpc("nav.frontier", {"ways_cached": True, "hazards": False}) or {}
    item("vis: NAV-6 arrival frontier without cold searches", "PASS" if (fr.get("ms") or 999) < 30 else "FAIL",
         f"ms={fr.get('ms')} ways={len(fr.get('ways') or [])}")
    # 16. NAV-7: a warm-up's answer leaves a jump's in-flight marker alone (markers per op)
    v = lua_server(g + 'local p=G.pos(h) N.inflight("jump",p) N.inflight("ways warm",p) N.inflightDone("ways warm") '
                   'local ok=N.inflightOf("jump")~=nil N.inflightDone("jump") '
                   'return tostring(ok).."|"..tostring(N.inflightOf("jump"))')
    item("vis: NAV-7 in-flight markers per op", "PASS" if v == "true|nil" else "FAIL", v)
    # 17. result compaction (compact.py) on live results: every fact of the raw text survives (compact_check), the
    # second look is shorter (static lines, clock), and the MCP layer serves compacted plain text.
    from bg3ai import compact, compact_check as cc
    comp, shown, probs, sizes = compact.Compactor(), [], [], []
    for name, kw in (("get_status", {"full": True}), ("look", {"full": True}), ("look", {"full": True}),
                     ("get_nearby", {}), ("get_map", {}), ("get_frontier", {})):
        raw = call(name, **kw)
        out = comp.apply(name, raw)
        probs += [f"{name}: {p}" for p in cc.check(raw, out, comp.elided, "\n".join(shown))]
        shown.append(out)
        sizes.append((cc.est_tokens(raw), cc.est_tokens(out)))
    a, b = sum(x for x, _ in sizes), sum(y for _, y in sizes)
    item("vis: result compaction keeps every fact (live)", "PASS" if not probs and b < a and sizes[2][1] <= sizes[1][1]
         else "FAIL", f"tokens ~{a}->~{b}, per call {sizes}; problems {probs[:3]}")
    # 17b. render oddities of window 20261016-183253: one OBJECTIVE line per target (o1/o2), the CAMP line as text
    #      (no key=value dump), get_nearby after look full=true -> "(same as the look a moment ago: N lines)"
    st_txt = call("get_status", full=True)
    objs = [re.sub(r"^OBJECTIVE \S+ ", "", ln) for ln in st_txt.split("\n") if ln.startswith("OBJECTIVE ")]
    camp_ln = grab(r"^CAMP .*$", st_txt) or ""
    near_out = comp.apply("get_nearby", call("get_nearby"))
    ok = (len(objs) == len(set(objs)) and "ALSO camp=" not in st_txt and "=" not in camp_ln
          and (not near_out.strip() or "same as" in near_out.split("\n")[0]))
    item("vis: one OBJECTIVE line per target, CAMP line readable, repeated block referenced", "PASS" if ok else "FAIL",
         f"objective lines {len(objs)} ({len(set(objs))} distinct); {camp_ln[:140] or 'no CAMP line'}; "
         f"get_nearby after look: {first_line(near_out)[:80]}")
    tool = S.mcp._tool_manager.get_tool("look")
    item("vis: MCP results compacted plain text", "PASS" if getattr(tool.fn, "__compacted__", False)
         and tool.output_schema is None else "FAIL", f"wrapped={getattr(tool.fn, '__compacted__', False)} "
         f"output_schema={tool.output_schema}")
    # 18. EventEcho (render.py): a door interact's second UseFinished (`used`) is not shown again in the next result
    # (naut golden 21-22, 128-129: one door use = 2 identical `used` events, the 2nd in the next result)
    raw = S.game.try_rpc("state.nearby", {"radius": 8, "kinds": ["door"]}) or {}
    doors = [it for it in (raw.get("items") or []) if it.get("uuid")]
    if doors:
        h = S.H.get(doors[0]["uuid"], doors[0].get("name"))
        outs = [call("interact", target=h), call("look"), call("interact", target=h), call("look")]
        used = [ln.strip() for o in outs for ln in o.split("\n") if re.match(rf"\s+used\b.*\b{h}\b", ln)]
        item("vis: door use shows one `used` event per interact (EventEcho)", "PASS" if len(used) <= 2 else "FAIL",
             f"{h} {doors[0].get('name')}: {len(used)} used lines over 2 interacts + 2 looks: {used[:4]}; "
             f"interacts: {first_line(outs[0])} / {first_line(outs[2])}")
    else:
        item("vis: door use shows one `used` event per interact (EventEcho)", "INFO", "no door within 8 m of BASE")
    # 19. a queue_interrupted event whose last step result is multi-line arrives as ONE EVENTS line through the mod
    # (naut-r2 golden 129: `last=1. travel_to ..` then a stray `STOPPED: leg 1 failed` and a fake CHANGES block)
    who = next((r.get("uuid") for r in status().get("party") or [] if r.get("uuid")), None)
    if who:
        call("look")
        step = ("1. travel_to (1,2,3): 68m\nleg 1: move_to: FAILED — acceptance probe\nSTOPPED: leg 1 failed\n"
                "CHANGES\n  e2 Probe moved 3m W")
        S._on_queue_report(who, S.actq.Report(done=0, stopped="acceptance probe", lines=[step]))
        out = call("look")
        ev = [ln for ln in out.split("\n") if "queue_interrupted" in ln]
        stray = [ln for ln in out.split("\n") if ln.startswith("STOPPED: leg 1")]
        item("vis: queue_interrupted event on one line (live mod event path)",
             "PASS" if len(ev) == 1 and "FAILED — acceptance probe" in ev[0] and "done=0" in ev[0] and not stray
             else "FAIL", f"{ev[:1]} stray={stray[:1]}")
    # 19b. V18 (S076 Gale asleep 3 rounds): the game's own stats give each disabling status its effect and fix
    #      (read-only: StatusGroups / RemoveEvents as SE exposes them, `using` inherited)
    v = lua_server(g + 'G._disabling = {} local out = {} for _, id in ipairs({"SLEEPING_EYEBITE", "HOLD_PERSON", '
                   '"PRONE", "FRIGHTENED", "BLEEDING"}) do local i = G.disabling_info(id) '
                   'out[#out + 1] = i and (tostring(i.effect) .. "/" .. tostring(i.fix)) or "-" end '
                   'return table.concat(out, "|")')
    want = ("cannot act/an adjacent ally's Help (an action) ends it; any damage ends it|cannot act/it saves again "
            "each turn|stands up at its turn start for half its movement/an adjacent ally's Help (an action) ends "
            "it|cannot move/nil|-")
    item("vis: V18 disabling statuses read from the stats", "PASS" if v == want else "FAIL", v[:400])
    # 20. V10: Mystra's statue in get_nearby (loads its own save)
    mystra_statue_probe()
    load_verified(BASE)


# V10 (S069): the Tabernacle's god statues share one story pattern (Act3b_LOW_StormshoreTabernacle.txt
# DB_LOW_StormshoreTabernacle_SingleGods): Helm/Tyr/Selune were listed, Mystra's never was (get_nearby radius 6 from
# 1.4 m, a name search, interact as Gale: "target is not visible"). The probe reads, for Mystra's and Helm's statue,
# every step of V.itemVisible (flags, kind, CanInteract, bounds, the index, CanSee/LOS per member, the rays) and the
# server components one has and the other lacks. Read-only.
# L7 FAIL: Mystra's has a Bound but no AI bounds (V.bounds nil), so no ray rule ran; CanSee/LOS 0, every ray hit the
# statue first. V14 (V.unboundedPerceived, self-hit rays): expected PASS, with unbounded=true and aib (the raw AIBounds).
MYSTRA_STATUE = "ab6b7142-2333-4b38-9506-fb3e72157145"  # S_LOW_MystraStatue
HELM_STATUE = "858e4c3a-1575-44fa-9955-6ef6a96bff39"    # S_LOW_HelmStatue (listed in S069)
MYSTRA_ITEM = "vis: Mystra's statue listed in get_nearby next to it (V10, S069)"
STATUE_PROBE_LUA = (
    "local B=Mods.BG3AI.BG3AI local V,U=B.Visibility,B.Util local ctx=V.context() local out={} "
    "local function o(fn,...) local ok,r=pcall(fn,...) return ok and tostring(r) or 'err' end "
    "for _,u in ipairs(UUIDS) do local r={} out[u]=r local e=Ext.Entity.Get(u) "
    "if not e then r.entity='none' else "
    "local p=V.pos(e) local kind=V.itemKind(u,e) r.name=tostring(V.name(u)) r.kind=kind r.pos=p "
    "r.tpl=o(Osi.GetTemplate,u) r.onstage=o(Osi.IsOnStage,u) r.byscript=o(Osi.IsInvisibleByScript,u) "
    "r.invisible=o(Osi.IsInvisible,u) r.caninteract=o(Osi.GetCanInteract,u) r.trap=o(Osi.IsTrap,u) r.si={} "
    "for _,k in ipairs({'Invisible','Invisible2','Destroyed','OffStage','CanUse','CanBePickedUp','IsDoor',"
    "'IsLadder','IsContainer','StoryItem','IsSecretDoor'}) do "
    "local ok,v=pcall(function() return e.ServerItem[k] end) r.si[k]=ok and tostring(v) or 'err' end "
    "r.inventory=e.InventoryMember~=nil r.helper=V.helperItem(u) r.bound=e.Bound~=nil r.bounds=V.bounds(e) "
    "r.indexed=false if p then for _,x in ipairs(V.itemsAround(p,1)) do if x==u then r.indexed=true end end end "
    "r.obs={} for _,ob in ipairs(ctx.observers) do local s={name=V.name(ob.uuid),sight=ob.sight,"
    "see=o(Osi.CanSee,ob.uuid,u),los=o(Osi.HasLineOfSight,ob.uuid,u),rays={}} r.obs[#r.obs+1]=s "
    "if p and ob.pos then s.d=U.dist(ob.pos,p) local eye={ob.pos[1],ob.pos[2]+V.EYE_H,ob.pos[3]} "
    "for _,h in ipairs({0.5,1.0,1.6}) do local pt={p[1],p[2]+h,p[3]} "
    "local ok,hit=pcall(Ext.Level.RaycastClosest,eye,pt,V.RAY_TYPE,V.RAY_INCLUDE,V.RAY_EXCLUDE,0) "
    "local ray={h=h,want=U.dist(eye,pt)} s.rays[#s.rays+1]=ray "
    "if not ok then ray.err=tostring(hit) elseif hit then "
    "pcall(function() ray.at=U.dist(eye,{hit.Position[1],hit.Position[2],hit.Position[3]}) end) "
    "pcall(function() local he=hit.Shape.PhysicsObject.Entity ray.hit=he.Uuid.EntityUuid "
    "ray.hit_name=V.name(ray.hit) end) end end end end "
    "r.perceived=V.perceived(ctx,u,p) r.large=V.largePerceived(ctx,u,e,p,kind) "
    "r.fixture=V.fixturePerceived(ctx,u,e,p,kind) r.unbounded=V.unboundedPerceived and V.unboundedPerceived(ctx,u,e,p,kind) "
    "r.aib=o(function() local n,t=0,{} for k,d in pairs(e.Bound.Bound.AIBounds) do n=n+1 "
    "t[#t+1]=tostring(k)..'='..tostring(d.Height)..'/'..tostring(d.ActualHeight)..'/'..tostring(d.Radius) end "
    "return n..' '..table.concat(t,',') end) r.visible=V.itemVisible(ctx,u,e,p) "
    "r.known=V.itemKnown(ctx,u,e,p) r.guard=o(B.Guard.visible,u) "
    "local ok,names=pcall(function() return e:GetAllComponentNames(false) end) r.comps={} "
    "if ok and type(names)=='table' then for k,v in pairs(names) do "
    "r.comps[#r.comps+1]=type(v)=='string' and v or tostring(k) end end end end "
    "return Ext.Json.Stringify(out,{Beautify=false})")


def ray_brief(x: dict, u: str) -> str:
    """`h:clear`, `h:self` (the ray hit the item) or `h:<hit at>/<item at> <what it hit>` (m from the eye)."""
    if x.get("err"):
        return f"{x['h']}:err"
    if "at" not in x and "hit" not in x:
        return f"{x['h']}:clear"
    if x.get("hit") == u:
        return f"{x['h']}:self"
    return f"{x['h']}:{x.get('at') or 0:.1f}/{x.get('want') or 0:.1f} {x.get('hit_name') or x.get('hit')}"


def statue_brief(r: dict) -> str:
    """One statue's probe row, the lines that decide V.itemVisible first."""
    if not r or r.get("entity") == "none":
        return "no entity"
    obs = "; ".join(
        f"{s.get('name')} {s.get('d') or 0:.1f}m see={s.get('see')} los={s.get('los')} rays="
        + ",".join(ray_brief(x, r.get("u")) for x in s.get("rays") or [])
        for s in r.get("obs") or [])
    si = " ".join(f"{k}={v}" for k, v in (r.get("si") or {}).items() if v not in ("false", "nil"))
    return (f"{r.get('name')} kind={r.get('kind')} pos={[round(x, 1) for x in r.get('pos') or []]} "
            f"visible={r.get('visible')} known={r.get('known')} guard={r.get('guard')} | onstage={r.get('onstage')} "
            f"byscript={r.get('byscript')} invisible={r.get('invisible')} caninteract={r.get('caninteract')} "
            f"trap={r.get('trap')} si{{{si}}} inventory={r.get('inventory')} helper={r.get('helper')} "
            f"indexed={r.get('indexed')} bound={r.get('bound')} bounds={r.get('bounds')} | perceived={r.get('perceived')} "
            f"large={r.get('large')} fixture={r.get('fixture')} unbounded={r.get('unbounded')} aib={r.get('aib')} | {obs} | "
            f"tpl={r.get('tpl')}")


def mystra_statue_probe() -> None:
    if not load_verified(MYSTRA_SAVE):
        item(MYSTRA_ITEM, "SKIP", f"{MYSTRA_SAVE} did not load")
        return
    near = call("get_nearby", radius=8)
    named = call("get_nearby", radius=20, kinds="Mystra")
    raw = lua_server(f"local UUIDS={{'{MYSTRA_STATUE}','{HELM_STATUE}'}} " + STATUE_PROBE_LUA)
    try:
        rows = json.loads(raw)
    except ValueError:
        item(MYSTRA_ITEM, "FAIL", f"probe error: {raw[:600]}")
        return
    for u in rows:
        rows[u]["u"] = u
    m, h = rows.get(MYSTRA_STATUE) or {}, rows.get(HELM_STATUE) or {}
    cm, ch = set(m.get("comps") or []), set(h.get("comps") or [])
    log(f"statue probe raw: {raw}")
    listed = "Statue of Mystra" in near and "Statue of Mystra" in named
    item(MYSTRA_ITEM, "PASS" if listed else "FAIL",
         f"radius 8: {'listed' if 'Statue of Mystra' in near else 'missing'}; kinds=Mystra: {first_line(named)[:100]} "
         f"|| MYSTRA {statue_brief(m)} || HELM {statue_brief(h)} || components only Mystra: {sorted(cm - ch)}; "
         f"only Helm: {sorted(ch - cm)}")


# A6 #5 (live L3 helm probe 2026-10-17, research/impl-se.md): naut3-r3's character stopped at the helm fire, where every
# naut3 run's [interact e39, interact e40] climbed e39 on the approach walk and then back down with the Use. The save
# is another character's ("Marek Dunhallow-"), so the prefix is switched for its load only.
HELM_SAVE, HELM_PREFIX = "ACC-naut-helm", "Marek Dunhallow-"
HELM_E39 = "a8e40469-0efa-4077-b125-47058511202e"  # Arterial Mesh ladder, foot y 4.53, climbs 2.9 m up
HELM_E40 = "73876474-0791-42e6-87ea-bf16db317afe"  # Arterial Mesh ladder from e39's top to the helm deck (y 18)
HELM_ITEM = "helm ladders: approach on the foot's floor, both climb up (A6, naut3 helm-hint/helm-once)"


def helm_ladder_probe() -> None:
    before = S.SAVE_PREFIX
    S.SAVE_PREFIX = HELM_PREFIX
    try:
        if not any(d.name.endswith("__" + HELM_SAVE) for d in S._my_saves()):
            item(HELM_ITEM, "SKIP", f"no {HELM_PREFIX}*__{HELM_SAVE} save folder")
            return
        if not load_verified(HELM_SAVE):
            return
        g = "local G=Mods.BG3AI.BG3AI.Guard local A=Mods.BG3AI.BG3AI.Actions local h=Osi.GetHostCharacter() "
        ap = lua_server(g + f'local ok,p=pcall(G.approach_point,h,"{HELM_E39}",A.USE_REACH) '
                        'if not ok then return "refused: "..tostring(G.is_refusal(p) and p.reason or p) end '
                        'if p.here then return "here" end return string.format("%.2f", p.pos[2])')
        try:
            ap_ok = abs(float(ap) - 4.53) < 1.5
        except ValueError:
            ap_ok = False
        # handles, not uuids (live L5: do_actions said "unknown target e252" for the raw uuid; render.py takes handles)
        steps = [{"tool": "interact", "target": S.H.get(HELM_E39)}, {"tool": "interact", "target": S.H.get(HELM_E40)}]
        out, dt = took("do_actions", actions=json.dumps(steps))
        y = lua_server('local _,y,_=Osi.GetPosition(Osi.GetHostCharacter()) return string.format("%.2f", y)')
        climbs = re.findall(r"climbed=(\w+)", out)
        try:
            top = float(y) >= 17.0
        except ValueError:
            top = False
        item(HELM_ITEM, "PASS" if ap_ok and climbs[:2] == ["up", "up"] and top else "FAIL",
             f"approach y={ap} (foot 4.53); climbs={climbs} final y={y} in {dt:.1f}s; "
             + " | ".join(ln for ln in out.splitlines() if re.match(r"\d+\. ", ln))[:300])
    finally:
        S.SAVE_PREFIX = before


FUNCS["vis"] = sec_vis


def run_section(name: str) -> int:
    global S, LOGF
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("BG3AI_PLAYTHROUGH", "claude-run-2")
    os.environ.setdefault("BG3AI_SAVE_PREFIX", "Claude-")
    # the game's capture layer writes into the launcher's runtime/shots, not into our scratch runtime
    os.environ.setdefault("BG3AI_SHOTS_DIR", str(Path(os.environ.get("BG3AI_RUNTIME") or REPO / "runtime") / "shots"))
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


def plan(names: list[str]) -> str:
    t = 0
    rows = []
    for n in names:
        t += EST_S.get(n, 60)
        rows.append(f"  {n:<7} ~{EST_S.get(n, 60) // 60}m{EST_S.get(n, 60) % 60:02d}s  (done by ~{t // 60}m{t % 60:02d}s)")
    return "\n".join(rows) + f"\n  total ~{t // 60} min (+ waits for {LOCK.name})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--section", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true", help="offline: validate tools, RPC ops, Lua snippets, saves")
    a = ap.parse_args()
    if a.list:
        print(" ".join(SECTIONS))
        print(plan(SECTIONS))
        return 0
    if a.check:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import acceptance_check
        return acceptance_check.main_check(sys.modules[__name__])
    if a.section:
        return run_section(a.section)
    names = [s for s in (a.only.split(",") if a.only else SECTIONS) if s]
    unknown = [n for n in names if n not in FUNCS]
    if unknown:
        print(f"unknown section(s) {unknown}; sections: {' '.join(SECTIONS)}")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    print("plan:\n" + plan(names), flush=True)
    rows = []
    died = ""
    memory = [{"after": "start", "t": round(time.time()), **game_memory()}]   # memory.json: live_window's table
    (OUT / "memory.json").write_text(json.dumps(memory, indent=1))
    for name in names:
        if died:  # the game is gone: the rest can't run; say so instead of N "the game is not running" FAILs
            rows.append({"section": name, "item": f"section {name}", "verdict": "SKIP",
                         "evidence": f"not run: the game died during section {died} (relaunch, then --only {name},…)"})
            continue
        print(f"--- section {name} (~{EST_S.get(name, 60)} s): waiting for {LOCK.name} ({time.strftime('%H:%M:%S')})",
              flush=True)
        t = time.monotonic()
        started = time.time() - 1   # a result file older than this is a previous run's
        lock = [] if os.environ.get(LOCK_HELD_ENV) == "1" else ["flock", "-w", "1800", str(LOCK)]
        subprocess.run(lock + ["timeout", "900", sys.executable, __file__, "--section", name])
        print(f"--- section {name} took {time.monotonic() - t:.0f} s (est. {EST_S.get(name, 60)} s)", flush=True)
        f = OUT / f"{name}.json"
        if f.exists() and f.stat().st_mtime >= started:
            rows += [dict(r, section=name) for r in json.loads(f.read_text())]
        else:   # killed by the timeout, or crashed before writing: an older file is a previous run's
            rows.append({"section": name, "item": f"section {name}", "verdict": "FAIL", "evidence": "no result file"})
        if not alive():
            died = name
            rows.append({"section": name, "item": f"game alive after section {name}", "verdict": "FAIL",
                         "evidence": "bg3 is gone: collect runtime/logs/bg3le-*.log (crash: lines) + "
                                     f"runtime/acceptance/{name}.log"})
        memory.append({"after": name, "t": round(time.time()), **game_memory()})
        (OUT / "memory.json").write_text(json.dumps(memory, indent=1))
        print(f"--- memory after {name}: {memory[-1]}", flush=True)
    table = ["| item | verdict | evidence |", "|---|---|---|"] + [
        f"| {r['item']} | {r['verdict']} | {r['evidence'].replace('|', '/')} |" for r in rows]
    (OUT / "table.md").write_text("\n".join(table) + "\n")
    (OUT / "results.json").write_text(json.dumps(rows, indent=1))   # with each row's section (live_window.py)
    print("\n".join(table))
    return sum(r["verdict"] == "FAIL" for r in rows)


if __name__ == "__main__":
    raise SystemExit(main())
