"""Shared by every section: the saves, the run plan and ownership, the result log, the bridge server (S) and the
helpers the sections call (tool calls, status reads, verified loads)."""

from __future__ import annotations

import glob
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "tools" / "dev" / "acceptance.py"  # the entry point; each section runs as `SCRIPT --section <name>`
OUT = REPO / "runtime" / "acceptance"
LOCK = REPO / "runtime" / "game.lock"
BASE = "S027-start"  # the real playthrough state (after S026); loaded, never overwritten
TRADE_SAVE = "S026-dammon-heat"  # Last Light Inn, Quartermaster Talli in view
DOOR_SAVE = "S050-end"  # Lower City, beside the locked Steel Watch Foundry door (S050 04:12 lockpick hover_miss x4)
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
# Estimated seconds per section (the 2026-10-11/13 full runs, test box: max seen, + today's new items), for the plan.
EST_S = {
    "reads": 65,
    "setup": 30,
    "act": 140,
    "vis": 115,
    "gear": 60,
    "swap": 85,
    "rest": 85,
    "trade": 135,
    "nav": 195,
    "combat": 220,
    "loads": 80,
    "se": 350,
    "strict": 150,
}
# Who fixes a section's FAILs (docs/ORCHESTRATOR.md ownership): tools/dev/live_window.py prints the failures per owner.
# An item dict may carry its own "owner" to override this.
SECTION_OWNERS = {
    "reads": "vis",
    "setup": "setup",
    "act": "act",
    "vis": "vis",
    "gear": "act",
    "swap": "act",
    "rest": "act",
    "trade": "act",
    "nav": "vis",
    "combat": "act",
    "loads": "se",
    "se": "se",
    "strict": "setup",
}
# tools/dev/live_window.py holds runtime/game.lock for its whole window and sets this: the sections then run without
# their own flock (a second flock on the same file would wait for the window's hold forever).
LOCK_HELD_ENV = "BG3AI_GAME_LOCK_HELD"
# What each save must be for its sections (checked offline by --check from the save's SaveInfo.json).
SAVE_NEEDS = {
    BASE: {
        "level": "SCL_Main_A",
        "party_has": ["Claude", "Gale", "Shadowheart"],
        "party_not": ["Wyll"],
        "party_min": 3,
        "used_by": "every section (swap: Gale in, Wyll in camp; nav/combat: fast travel to Moonrise Towers)",
    },
    HOTBAR_SAVE: {
        "level": "CTY_Main_A",
        "party_has": ["Claude"],
        "party_min": 3,
        "used_by": "se/act hotbar casts (Claude the Paladin's Bless on 3; Dimension Door if Gale is in); "
        "act prepare_spells (Bless prepared and unprepared on Claude, A5); strict (load 2, then camp "
        "+ long_rest from Act 3)",
    },
    SCROLL_SAVE: {
        "party_has": ["Karlach"],
        "party_min": 3,
        "used_by": "act point scroll aimed at the ground (A9: Karlach's Scroll of Misty Step)",
    },
    TRADE_SAVE: {
        "level": "SCL_Main_A",
        "near": ((-26.8, 27.4, 164.8), 8),
        "used_by": "trade (Last Light Inn; Talli ~34 m S at (-31.3, 27.5, 130.2))",
    },
    MYSTRA_SAVE: {
        "level": "CTY_Main_A",
        "near": ((103.4, 44.5, -17.8), 8),
        "used_by": "vis Mystra statue probe (Stormshore Tabernacle, the statue ~2 m from the party)",
    },
    DOOR_SAVE: {
        "level": "CTY_Main_A",
        "near": ((-233.0, 4.5, -180.6), 8),
        "used_by": "se (Lower City, the locked Steel Watch Foundry door ~1.6 m away; the older S050-end "
        "folder stands 31 m off: load_game takes the newest, the right one)",
    },
}

RESULTS: list[dict] = []


class ServerRef:
    """S: the bridge server (tools/dev/server_view.py's S), bound by run_section in the section process, which is the
    only one that imports the bridge. Every section module holds this one object, so reads and writes (S.game,
    S.SAVE_PREFIX = ...) reach the bound server, as they did when S was one module's global."""

    def __init__(self) -> None:
        object.__setattr__(self, "_target", None)

    def bind(self, target: Any) -> None:
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_target"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_target"), name, value)


S = ServerRef()  # bound to bg3ai's server view in the section process

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
    except Exception as e:
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
            rows.append(
                (l.get("kind") not in ("door", "exit", "waypoint"), abs(d(q) - 100), tuple(float(x) for x in q[:3]))
            )
    return (min(rows)[2], None) if rows else None


ROUTE_KEYS = (
    "reachable",
    "length_m",
    "needs_jump",
    "drop",
    "end_off_m",
    "unknown_reason",
    "no_path_reason",
    "far",
    "too_far",
    "ms",
)


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
            cg = cg or next(
                (ln.split("::", 1)[1] for ln in (d / "cgroup").read_text().splitlines() if ln.startswith("0::")), None
            )
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
        for f, name in (
            ("memory.current", "cg_current"),
            ("memory.peak", "cg_peak"),
            ("memory.swap.current", "cg_swap"),
            ("memory.swap.peak", "cg_swap_peak"),
        ):
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
    except Exception:
        return False


def load_verified(label: str, record: bool = False) -> bool:
    """load_game + independent checks: marker wiped (a new Lua state), party readable, game screen."""
    try:
        set_marker()
    except Exception as e:
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


def bg3le_log() -> Path | None:
    files = sorted(glob.glob(str(REPO / "runtime" / "logs" / "bg3le-*.log.*")), key=os.path.getmtime)
    for f in reversed(files):
        try:
            if "guarded" in Path(f).read_text(errors="replace"):
                return Path(f)
        except OSError:
            pass
    return Path(files[-1]) if files else None


def grep_crash(lg: Path | None) -> str:
    if not lg:
        return ""
    m = re.findall(r".*crash: signal.*", lg.read_text(errors="replace"))
    return m[-1][:120] if m else ""


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

    decks = [
        f"{d}[{' '.join(f'{k}={v}' for k, v in sorted((m or {}).items(), key=lambda kv: key(kv[0])))}]"
        for d, m in sorted(srv["map"].items())
    ]
    head = " ".join(f"{k}={srv[k]}" for k in ("container", "bars", "slots") if k in srv)
    return (head + " " if head else "") + " ".join(decks)


def lua_server(code: str) -> str:
    return str(S.game.eval(code, context="server"))
