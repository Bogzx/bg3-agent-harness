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

    # Job push: count RPCs per op on a short walk (state reverts with the reload at the end).
    from bg3ai import jobpush
    counts: dict[str, int] = {}
    orig = S.game.rpc

    def counting(op, *a, **k):
        counts[op] = counts.get(op, 0) + 1
        return orig(op, *a, **k)

    me = st.get("party")[0]
    pos = me.get("pos") or [0, 0, 0]
    jp = jobpush.shared(S.game.host, S.game.port)
    jp.ensure()
    S.game.rpc = counting
    try:
        t = time.monotonic()
        out = call("move_to", x=pos[0] + 2.0, y=pos[1], z=pos[2], wait=True)
        dt = time.monotonic() - t
    finally:
        S.game.rpc = orig
    polls = counts.get("job.poll", 0)
    ok = jp.seen and polls <= 4 and "done" in out
    item("job push latency", "PASS" if ok else ("WARN" if "done" in out else "FAIL"),
         f"move_to 2 m {dt:.2f}s, job.poll x{polls}, rpcs={sum(counts.values())}, listener heard marks={jp.seen}; {first_line(out)}")
    load_verified(BASE)


def sec_rest() -> None:
    if not load_verified(BASE):
        return
    st0 = status()
    out, dt = took("short_rest")
    if "done" in out:
        hp = grab(r"(?i)^.*HP.*→.*$", out)
        item("short_rest (HP before→after)", "PASS" if hp else "FAIL", f"{dt:.1f}s; {hp or first_line(out)}")
    else:
        item("short_rest (HP before→after)", "FAIL" if "short rest" not in out.lower() else "SKIP", f"{dt:.1f}s; {first_line(out)}")

    out, dt = took("camp", action="go")
    if "FAILED" in out:
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
    if "trade" not in wares.lower():
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
    out, dt = took("equip_best")
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
    item("PARTY hint after swaps", "INFO", grab(r"^PARTY.*$", st_full) or "no PARTY line")
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

    # Ladder from the top: a ladder within 40 m whose foot is >= 2.5 m below the leader.
    call("get_nearby", radius=40, kinds="ladder")
    raw = S.game.try_rpc("state.nearby", {"radius": 40, "kinds": ["ladder"]}) or {}
    me = (status().get("party") or [{}])[0]
    y0 = (me.get("pos") or [0, 0, 0])[1]
    lads = [it for it in (raw.get("items") or []) if "ladder" in str(it.get("name", "")).lower() and it.get("pos")]
    below = [it for it in lads if y0 - it["pos"][1] >= 2.5]
    if need(bool(below), "ladder from the top", f"no ladder foot >= 2.5 m below the party within 40 m ({len(lads)} ladders seen)"):
        h = S.H.get(below[0]["uuid"], below[0].get("name"))
        out, dt = took("interact", target=h)
        y1 = (status().get("party") or [{}])[0].get("pos", [0, 0, 0])[1]
        item("ladder from the top", "PASS" if "FAILED" not in out and y1 < y0 - 2 else "FAIL",
             f"{dt:.1f}s; y {y0:.1f}->{y1:.1f}; {first_line(out)}")

    # Interior via entrance: the Mausoleum room from its gate is REFUSED naming the entrance, then interact.
    ft, dt = took("fast_travel", waypoint="Moonrise Towers")
    if not need("FAILED" not in ft, "travel_to into an interior via entrance", "fast_travel Moonrise Towers failed: " + first_line(ft)):
        load_verified(BASE)
        return
    out = call("travel_to", x=-183.0, y=38.0, z=99.0)
    end = time.monotonic() + 240
    while time.monotonic() < end:
        w = call("wait_for", target="queue", timeout_s=60, lead_s=0)
        if "still waiting" not in w:
            break
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
            item("buttons listed (Mausoleum)", "PASS" if len(tags) >= 2 else "FAIL",
                 f"{len(re.findall(r'Button', nb))} buttons, tags={tags[:4]}")
        else:
            item("buttons listed (Mausoleum)", "SKIP", "did not reach the puzzle room")
    load_verified(BASE)


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


def sec_combat() -> None:
    if not load_verified(BASE):
        return
    cs = S.game.try_rpc("state.combat") or {}
    nb = S.game.try_rpc("state.nearby", {"radius": 30}) or {}
    hostiles = [c.get("name") for c in (nb.get("characters") or []) if c.get("relation") == "hostile" and not c.get("dead")]
    item("combat_turn on a safe fight", "SKIP",
         f"no safe fight in reach of {BASE} (combat active={bool(cs.get('active'))}, visible hostiles={hostiles[:4]}); "
         "starting one would change story state: verify at the next session's first fight")


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
