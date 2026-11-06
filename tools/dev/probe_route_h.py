"""Live probe route H (ops/briefs/arena/route-h-probe.md): real hirelings hired through the game's Henchmen page and
rebuilt by its respec page. The brief's hand-Lua steps, one subcommand each; $0, no model. Every step writes
<out>/<file>.json with {"step", "verdict" (PASS / FAIL / STOP), "why", the reads and calls}, and the exit code is 0 on
PASS, 1 on FAIL, 2 on STOP (the load can't go on: stop it, don't run the next step).

Run on a THROWAWAY load of the PROBE copy of crash_site_grove (it hires, respecs and spends gold), from the repo root,
the game started with BG3AI_ARENA=1 and BG3AI_SCENARIO_SETUP=1 (arena.symmetry refuses without the first):
    P="uv run --project bridge python tools/dev/probe_route_h.py"
    $P pre                                   # host, level, gold, party, max party size, hire/respec flags
    $P xp --amount 472                       # only if hire FAILs with no page: AddExplorationExperience(host)
    $P gold --amount 500                     # only if a hire FAILs on gold (the party has 33): AddGold(host)
    $P respec --who Shadowheart --tag 0-respec-companion   # step 0: StartRespec, read the page, leave it
    $P hire --class Fighter                  # step 1: PROC_StartHenchmen, pick Hireling_Fighter, Finish
    $P hire --class Sorcerer
    $P respec --who <hireling uuid> --tag 2-respec-1       # step 2
    $P sym --pair <uuid>,<uuid> --tag 3-sym-before         # step 3
    $P save --name ROUTEH_probe              # then relaunch on it, and sym again with --tag 3-sym-after
    $P transform --who <uuid> --root <uuid> --tag-uuid <uuid>   # step 4, only if 1-3 pass
`--dry-run` prints the step's plan and writes every Lua chunk it would send to <out>/lua/ (for `luac -p`), no game.
Evidence: runtime/probes/route-h/ (--out).

Fail closed: a step that can't tell what happened (a read that errors, a page that didn't open, no unique pick)
writes FAIL or STOP with the reads it has and sends nothing more; it never guesses a list entry or retries a write.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_arena_lua import INSTALL, lua  # noqa: E402

DEFAULT_OUT = REPO / "runtime" / "probes" / "route-h"
PAGE_WAIT_S = 10.0  # a page asked for by a story call is visible within this
EVENT_WAIT_S = 15.0  # HenchmanSelected / RespecCancelled within this of the page's command
POLL_S = 0.5
LEAVE_RESPEC = ("ExitCharacterRespec", "CancelRespec", "CancelCommand", "CloseCommand")  # tried in order, dry first

# The route's Osiris events (Hirelings.txt :741-747, :1170-1173; Respec.txt :20-28), logged in their own table so the
# arena probe's library stays as it is.
SERVER_LIB = (
    INSTALL
    + r"""
local B = Mods.BG3AI.BG3AI
local H = B.RouteH
if not H then
H = { log = {}, n = 0, hooks = {} }
B.RouteH = H
for _, e in ipairs({ { "HenchmanSelected", 2 }, { "HenchmanAborted", 1 }, { "CharacterMadePlayer", 1 },
    { "CharacterJoinedParty", 1 }, { "RespecCompleted", 1 }, { "RespecCancelled", 1 } }) do
    local ok, err = pcall(Ext.Osiris.RegisterListener, e[1], e[2], "after", function(...)
        H.n = H.n + 1
        local a = {}
        for i, v in ipairs({ ... }) do a[i] = tostring(v) end
        H.log[H.n] = { i = H.n, e = e[1], args = a, t = Ext.Utils.MonotonicTime() }
    end)
    H.hooks[e[1]] = ok and true or tostring(err)
end
end
local P = B.ArenaProbe
local function q(fn, ...) return P.q(fn, ...) end
local function race(u)
    local ok, r = pcall(function()
        local e = Ext.Entity.Get(u)
        return { race = tostring(e.Race and e.Race.Race), template = e.GameObjectVisual and
            tostring(e.GameObjectVisual.RootTemplateId) or nil }
    end)
    return ok and r or { error = tostring(r) }
end
local function classes(u)
    local ok, r = pcall(function()
        local out = {}
        for _, c in ipairs(Ext.Entity.Get(u).Classes.Classes) do
            out[#out + 1] = { class = tostring(c.ClassUUID), sub = tostring(c.SubClassUUID), level = c.Level }
        end
        return out
    end)
    return ok and r or { error = tostring(r) }
end
function H.member(u)
    local f = P.facts(u)
    f.race, f.classes, f.gold = race(u), classes(u), q("GetGold", u)
    return f
end
function H.party()
    return { party = P.col1("DB_PartyMembers"), players = P.col1("DB_Players"), avatars = P.col1("DB_Avatars") }
end
function H.since(i)
    local out = {}
    for k = i + 1, H.n do out[#out + 1] = H.log[k] end
    return out
end
"""
)

# The client side: the open page's DataContext (UI.lua, Progression.inner.set as the CC driver uses), never an element
# kept across calls.
CLIENT_LIB = r"""
local B = Mods.BG3AI.BG3AI
local UI, PI = B.UI, B.Progression.inner
local function dc(name)
    for _, w in ipairs(UI.widgets()) do
        if w.name == name and UI.isVisible(w.el) then return w.el.DataContext end
    end
    return nil
end
local function prim(vm)
    local out = {}
    for k, v in pairs(UI.props(vm) or {}) do
        local t = type(v)
        if t == "number" or t == "boolean" or (t == "string" and #v < 120) then out[k] = v end
    end
    return out
end
local function names(vm)
    local out = {}
    for k in pairs(UI.props(vm) or {}) do out[#out + 1] = k end
    table.sort(out)
    return out
end
local function entries(coll)
    local out = {}
    for i, it in UI.items(coll, 60) do out[#out + 1] = { i = i, fields = prim(it) } end
    return out
end
"""


def server_read(code: str) -> str:
    return SERVER_LIB + "\nlocal H = Mods.BG3AI.BG3AI.RouteH\n" + code


def client_read(code: str) -> str:
    return CLIENT_LIB + "\n" + code


PRE = server_read(
    "local host = (H.party().avatars or {})[1]\n"
    "local ok, max = pcall(function() return Osi.GetMaxPartySize() end)\n"
    "local flags = {}\n"
    "for _, f in ipairs({ 'Hirelings_State_Hired_1', 'Hirelings_State_Hired_2', 'Hirelings_State_Hired_3',\n"
    "    'Hirelings_State_CCmutex' }) do flags[f] = P.q('GetFlag', f, host) end\n"
    "return P.js({ host = host and H.member(host), party = H.party(), max_party = ok and max or tostring(max),\n"
    "    flags = flags, hooks = H.hooks, mark = H.n })"
)
PAGES = client_read(
    "local out = {}\n"
    "for _, w in ipairs(UI.widgets()) do if UI.isVisible(w.el) then out[#out + 1] = w.name end end\n"
    "return Ext.Json.Stringify(out)"
)


def call(fn: str, *args: Any) -> str:
    """One Osiris call on the server, its result or error as JSON."""
    return server_read(f"return P.js(P.call({lua(fn)}, {{ {lua(list(args))} }}))")


def page_read(widget: str, colls: list[str]) -> str:
    """A page's primitive fields, every property name (does a race field exist?) and the given collections."""
    return client_read(
        f"local d = dc({lua(widget)})\n"
        "if d == nil then return Ext.Json.Stringify({ open = false }) end\n"
        "local r = { open = true, fields = prim(d), names = names(d), lists = {} }\n"
        f"for _, c in ipairs({lua(colls)}) do\n"
        "    local ok, v = pcall(function() return entries(UI.prop(d, c)) end)\n"
        "    r.lists[c] = ok and v or ('ERR ' .. tostring(v))\n"
        "end\n"
        "return Ext.Json.Stringify(r)"
    )


def command(widget: str, name: str, dry: bool) -> str:
    return client_read(
        f"local d = dc({lua(widget)})\n"
        "if d == nil then return Ext.Json.Stringify({ open = false }) end\n"
        f"local ok, err = UI.command(d, {lua(name)}, nil, {lua(dry)})\n"
        "return Ext.Json.Stringify({ open = true, ok = ok and true or false, err = err and tostring(err) or nil })"
    )


def select_origin(index: int) -> str:
    """Set SelectedOrigin to SelectableOrigins' entry `index` and read it back (the CC driver's set, PI.set)."""
    return client_read(
        "local d = dc('Henchmen')\n"
        "if d == nil then return Ext.Json.Stringify({ open = false }) end\n"
        "local it\n"
        f"for i, x in UI.items(UI.prop(d, 'SelectableOrigins'), 60) do if i == {index} then it = x end end\n"
        "if it == nil then return Ext.Json.Stringify({ open = true, ok = false, err = 'no such entry' }) end\n"
        "local ok, err = PI.set(d, 'SelectedOrigin', it)\n"
        "return Ext.Json.Stringify({ open = true, ok = ok and true or false, err = err,\n"
        "    same = PI.same(UI.prop(d, 'SelectedOrigin'), it), selected = prim(UI.prop(d, 'SelectedOrigin')) })"
    )


def pick_origin(entries: list[dict[str, Any]], cls: str) -> tuple[int | None, str]:
    """The one SelectableOrigins entry for Hireling_<cls>: an exact id first, then a field naming the class. None plus
    why when there isn't exactly one (never a guess)."""
    want = f"hireling_{cls}".lower()
    exact = [e for e in entries if any(str(v).lower() == want for v in e.get("fields", {}).values())]
    if len(exact) == 1:
        return exact[0]["i"], "exact id"
    loose = [e for e in entries if any(cls.lower() in str(v).lower() for v in e.get("fields", {}).values())]
    if len(loose) == 1 and not exact:
        return loose[0]["i"], "the one entry naming the class"
    return None, f"{len(exact)} exact and {len(loose)} entries name {cls}: no unique pick"


class RouteH:
    def __init__(
        self,
        game: Any,
        out: Path,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.game, self.out, self.sleep, self.clock = game, out, sleep, clock

    def ev(self, code: str, context: str = "server") -> Any:
        try:
            txt = self.game.eval(code, context=context, timeout=30.0)
        except Exception as e:  # a read that errors is evidence, and fails the step
            return {"error": f"{type(e).__name__}: {e}"}
        try:
            return json.loads(txt)
        except (TypeError, ValueError):
            return {"error": f"not JSON: {str(txt)[:300]}"}

    def wait(self, read: Callable[[], Any], done: Callable[[Any], bool], wait_s: float) -> tuple[bool, Any]:
        end = self.clock() + wait_s
        while True:
            r = read()
            if done(r):
                return True, r
            if self.clock() >= end:
                return False, r
            self.sleep(POLL_S)

    def page_open(self, widget: str, wait_s: float = PAGE_WAIT_S) -> tuple[bool, Any]:
        return self.wait(lambda: self.ev(PAGES, "client"), lambda r: isinstance(r, list) and widget in r, wait_s)

    def events(self, mark: int) -> list[dict[str, Any]]:
        r = self.ev(server_read("return P.js(H.since(" + str(int(mark)) + "))"))
        return r if isinstance(r, list) else []

    def write(self, name: str, rec: dict[str, Any]) -> int:
        self.out.mkdir(parents=True, exist_ok=True)
        p = self.out / f"{name}.json"
        p.write_text(json.dumps(rec, indent=1, sort_keys=True, default=str) + "\n")
        print(f"{rec['step']} {rec['verdict']}: {rec['why']} -> {p}")
        return {"PASS": 0, "FAIL": 1}.get(rec["verdict"], 2)

    def pre(self) -> int:
        r = self.ev(PRE)
        host = r.get("host") if isinstance(r, dict) else None
        if not host or "error" in r:
            return self.write("pre", {"step": "pre", "verdict": "STOP", "why": "no host read", "read": r})
        why = f"host {host.get('name')} level {host.get('level')} gold {host.get('gold')}, max party {r['max_party']}"
        return self.write("pre", {"step": "pre", "verdict": "PASS", "why": why, "read": r})

    def respec(self, who: str, tag: str) -> int:
        rec: dict[str, Any] = {"step": tag, "who": who}
        u = self.ev(server_read(f"return P.js({{ u = P.who({lua(who)}) }})"))
        if not isinstance(u, dict) or not u.get("u"):
            return self.write(tag, {**rec, "verdict": "STOP", "why": "who didn't resolve", "read": u})
        mark = (self.ev(PRE) or {}).get("mark", 0)
        rec["start"] = self.ev(call("StartRespec", u["u"]))
        if not rec["start"].get("ok"):
            return self.write(tag, {**rec, "verdict": "FAIL", "why": "StartRespec errored"})
        opened, rec["pages"] = self.page_open("CharacterRespec")
        if not opened:
            return self.write(
                tag,
                {
                    **rec,
                    "verdict": "FAIL",
                    "why": f"no CharacterRespec page within {PAGE_WAIT_S:.0f} s",
                    "events": self.events(mark),
                },
            )
        rec["page"] = self.ev(page_read("CharacterRespec", ["SelectableClasses", "SelectableSubClasses"]), "client")
        names = rec["page"].get("names", []) if isinstance(rec["page"], dict) else []
        rec["race_fields"] = [n for n in names if "race" in n.lower()]
        rec["leave"] = []
        for name in LEAVE_RESPEC:  # the first command the page has and can run now; dry first
            dry = self.ev(command("CharacterRespec", name, True), "client")
            rec["leave"].append({"command": name, "dry": dry})
            if isinstance(dry, dict) and dry.get("ok"):
                rec["leave"][-1]["run"] = self.ev(command("CharacterRespec", name, False), "client")
                break
        left, rec["pages_after"] = self.wait(
            lambda: self.ev(PAGES, "client"), lambda r: isinstance(r, list) and "CharacterRespec" not in r, PAGE_WAIT_S
        )
        rec["events"] = self.events(mark)
        if not left:
            return self.write(tag, {**rec, "verdict": "STOP", "why": "the respec page didn't close: stop the load"})
        return self.write(tag, {**rec, "verdict": "PASS", "why": f"page opened; race fields {rec['race_fields']}"})

    def hire(self, cls: str, tag: str) -> int:
        rec: dict[str, Any] = {"step": tag, "class": cls}
        rec["before"] = before = self.ev(PRE)
        host = (before.get("host") or {}).get("uuid") if isinstance(before, dict) else None
        if not host:
            return self.write(tag, {**rec, "verdict": "STOP", "why": "no host read"})
        rec["start"] = self.ev(call("PROC_StartHenchmen", host))
        if not rec["start"].get("ok"):
            return self.write(tag, {**rec, "verdict": "FAIL", "why": "PROC_StartHenchmen errored"})
        opened, rec["pages"] = self.page_open("Henchmen")
        if not opened:
            return self.write(
                tag,
                {
                    **rec,
                    "verdict": "FAIL",
                    "why": f"no Henchmen page within {PAGE_WAIT_S:.0f} s (avatar level < 3? flags in 'before')",
                    "events": self.events(before["mark"]),
                },
            )
        rec["page"] = page = self.ev(page_read("Henchmen", ["SelectableOrigins"]), "client")
        origins = (page.get("lists") or {}).get("SelectableOrigins") if isinstance(page, dict) else None
        idx, rec["pick"] = pick_origin(origins if isinstance(origins, list) else [], cls)
        if idx is None:
            rec["cancel"] = self.ev(command("Henchmen", "CancelHenchmenCreating", False), "client")
            return self.write(
                tag,
                {
                    **rec,
                    "verdict": "FAIL",
                    "why": rec["pick"] + "; cancelled (refund expected)",
                    "events": self.events(before["mark"]),
                },
            )
        rec["select"] = sel = self.ev(select_origin(idx), "client")
        if not (isinstance(sel, dict) and sel.get("ok") and sel.get("same")):
            rec["cancel"] = self.ev(command("Henchmen", "CancelHenchmenCreating", False), "client")
            return self.write(tag, {**rec, "verdict": "FAIL", "why": "SelectedOrigin didn't take; cancelled"})
        rec["finish_dry"] = dry = self.ev(command("Henchmen", "FinishHenchmenCreating", True), "client")
        if not (isinstance(dry, dict) and dry.get("ok")):
            rec["cancel"] = self.ev(command("Henchmen", "CancelHenchmenCreating", False), "client")
            return self.write(tag, {**rec, "verdict": "FAIL", "why": "FinishHenchmenCreating can't run; cancelled"})
        rec["finish"] = self.ev(command("Henchmen", "FinishHenchmenCreating", False), "client")
        got, rec["events"] = self.wait(
            lambda: self.events(before["mark"]),
            lambda r: any(e.get("e") == "HenchmanSelected" for e in r),
            EVENT_WAIT_S,
        )
        after = self.ev(PRE)
        new = (
            [
                u
                for u in (after.get("party", {}).get("party") or [])
                if u not in (before.get("party", {}).get("party") or [])
            ]
            if isinstance(after, dict)
            else []
        )
        rec["after"] = after
        rec["hired"] = [self.ev(server_read(f"return P.js(H.member({lua(u)}))")) for u in new]
        if not got or len(new) != 1:
            return self.write(
                tag,
                {
                    **rec,
                    "verdict": "FAIL",
                    "why": f"HenchmanSelected {'seen' if got else 'not seen'}, {len(new)} new party members",
                },
            )
        h = rec["hired"][0]
        return self.write(
            tag,
            {
                **rec,
                "verdict": "PASS",
                "why": f"hired {h.get('name')} {new[0]}, level "
                f"{h.get('level')}; gold {before['host'].get('gold')} -> {after['host'].get('gold')}",
            },
        )

    def host_call(self, step: str, fn: str, amount: int, key: str) -> int:
        """Osi.<fn>(host, amount), with the host's `key` (level / gold) before and after."""
        before = self.ev(PRE)
        host = (before.get("host") or {}).get("uuid")
        if not host:
            return self.write(step, {"step": step, "verdict": "STOP", "why": "no host read", "before": before})
        r = self.ev(call(fn, host, amount))
        after = self.ev(PRE)
        v = [(x.get("host") or {}).get(key) for x in (before, after)]
        return self.write(
            step,
            {
                "step": step,
                "verdict": "PASS" if r.get("ok") else "FAIL",
                "why": f"host {key} {v[0]} -> {v[1]}",
                "call": r,
                "before": before,
                "after": after,
            },
        )

    def save(self, name: str) -> int:
        """A named save through the mod's save menus (the bridge's save_game path, tools/saving.py _save_menus). A
        save that needs the input shim (needs_text / needs_key) fails: send it by hand, as the brief says."""
        rec: dict[str, Any] = {"step": "save", "name": name}
        try:
            self.game.rpc("ui.config", {"save_enabled": True, "load_enabled": True}, context="client")
            j = self.game.run_job("ui.menu", {"action": "save", "name": name}, context="client", wait_s=30)
            rec["job"] = {"status": j.status, "result": j.result, "error": j.error}
        except Exception as e:
            rec["job"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        got = rec["job"].get("result")
        res: dict[str, Any] = got if isinstance(got, dict) else {}
        ok = rec["job"]["status"] == "done" and not res.get("needs_text") and not res.get("needs_key")
        return self.write("save", {**rec, "verdict": "PASS" if ok else "FAIL", "why": f"job {rec['job']['status']}"})

    def sym(self, pair: list[str], tag: str) -> int:
        try:
            r = self.game.rpc("arena.symmetry", {"pairs": [pair]}, context="server", timeout=30.0)
        except Exception as e:  # a refusal (the differences) is the evidence
            r = {"error": f"{type(e).__name__}: {e}"}
        ok = isinstance(r, dict) and r.get("ok") is True
        return self.write(
            tag,
            {
                "step": tag,
                "pair": pair,
                "verdict": "PASS" if ok else "FAIL",
                "why": "symmetric" if ok else "differs or refused (see read)",
                "read": r,
            },
        )

    def transform(self, who: str, root: str, tag_uuid: str, tag: str) -> int:
        rec: dict[str, Any] = {"step": tag, "who": who, "root": root}
        rec["before"] = self.ev(server_read(f"return P.js(H.member({lua(who)}))"))
        rec["call"] = self.ev(call("Transform", who, root, tag_uuid))
        self.sleep(2.0)
        rec["after"] = a = self.ev(server_read(f"return P.js(H.member({lua(who)}))"))
        changed = isinstance(a, dict) and a.get("race") != rec["before"].get("race")
        return self.write(
            tag,
            {
                **rec,
                "verdict": "PASS" if rec["call"].get("ok") and changed else "FAIL",
                "why": f"race {rec['before'].get('race')} -> {a.get('race') if isinstance(a, dict) else a}",
            },
        )


PLAN = {
    "pre": "server read: host (DB_Avatars[1]) facts + race/classes/gold, DB_PartyMembers/Players/Avatars, "
    "GetMaxPartySize, Hired_1..3 + CCmutex flags; installs the route's Osiris listeners",
    "respec": "StartRespec(who); CharacterRespec visible within 10 s; read its fields, property names (race?), "
    "SelectableClasses/SubClasses; leave by the first of " + ", ".join(LEAVE_RESPEC) + " that CanExecute; page gone "
    "within 10 s else STOP",
    "hire": "pre read; PROC_StartHenchmen(host); Henchmen visible within 10 s; SelectableOrigins read; the one "
    "Hireling_<class> entry (else Cancel + FAIL); SelectedOrigin set + read back; FinishHenchmenCreating dry, then "
    "run; HenchmanSelected within 15 s; exactly one new DB_PartyMembers row; its facts",
    "xp": "AddExplorationExperience(host, --amount) (Osi.AddExperience is nil under bg3le, live 15); host level "
    "before/after",
    "gold": "AddGold(host, --amount) (crash_site's party has 33 gold; a hire or respec costs 100 in the dialogue); "
    "host gold before/after",
    "save": "ui.config save gates on; ui.menu save --name (the bridge's save_game path); FAIL when it needs the shim",
    "sym": "arena.symmetry {pairs = [[a, b]]}",
    "transform": "member read; Osi.Transform(who, root, tag); member read 2 s later: race changed?",
}


def chunks() -> dict[str, str]:
    """Every Lua chunk the steps send, with sample arguments (for --dry-run's luac check)."""
    u = "00000000-0000-0000-0000-000000000000"
    return {
        "pre": PRE,
        "pages": PAGES,
        "call": call("StartRespec", u),
        "events": server_read("return P.js(H.since(0))"),
        "who": server_read(f"return P.js({{ u = P.who({lua('Shadowheart')}) }})"),
        "member": server_read(f"return P.js(H.member({lua(u)}))"),
        "page_read": page_read("Henchmen", ["SelectableOrigins"]),
        "command": command("Henchmen", "FinishHenchmenCreating", True),
        "select_origin": select_origin(3),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("step", choices=list(PLAN))
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--who", default="")
    ap.add_argument("--class", dest="cls", default="")
    ap.add_argument("--pair", default="")
    ap.add_argument("--root", default="")
    ap.add_argument("--tag-uuid", default="")
    ap.add_argument("--tag", default="")
    ap.add_argument("--amount", type=int, default=0)
    ap.add_argument("--name", default="ROUTEH_probe")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.dry_run:
        print(f"{a.step}: {PLAN[a.step]}")
        d = a.out / "lua"
        d.mkdir(parents=True, exist_ok=True)
        for name, code in chunks().items():
            (d / f"{name}.lua").write_text(code + "\n")
        print(f"Lua chunks -> {d} (check: luac -p {d}/*.lua)")
        return 0
    need = {
        "xp": ["amount"],
        "gold": ["amount"],
        "respec": ["who"],
        "hire": ["cls"],
        "sym": ["pair"],
        "transform": ["who", "root", "tag_uuid"],
    }
    for k in need.get(a.step, []):
        if not getattr(a, k):
            ap.error(f"{a.step} needs --{k.replace('_', '-').replace('cls', 'class')}")
    sys.path.insert(0, str(REPO / "bridge" / "src"))
    from bg3ai.game import Game  # the bridge's RPC layer; imported here so tests need no bridge

    r = RouteH(Game(), a.out)
    if a.step == "pre":
        return r.pre()
    if a.step == "respec":
        return r.respec(a.who, a.tag or "respec")
    if a.step == "hire":
        return r.hire(a.cls, a.tag or f"1-hire-{a.cls.lower()}")
    if a.step == "xp":
        return r.host_call("xp", "AddExplorationExperience", a.amount, "level")
    if a.step == "gold":
        return r.host_call("gold", "AddGold", a.amount, "gold")
    if a.step == "save":
        return r.save(a.name)
    if a.step == "sym":
        return r.sym(a.pair.split(","), a.tag or "sym")
    return r.transform(a.who, a.root, a.tag_uuid, a.tag or "4-transform")


if __name__ == "__main__":
    sys.exit(main())
