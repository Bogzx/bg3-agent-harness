"""A build's reference, live (website repo ops/logs/arena-4v4-builds.md S3; its briefs hold the 4v4 live sheet): a hired
hireling at the tier's party XP, respecced by the build's script through the game's own pages, given the tier's gear and
prepared spells; make_arena_save.py reference-read then stores its profile. $0, no model.

    P="uv run --project bridge python tools/dev/arena_reference.py"
    $P plan <builds.json> <build id>                 # offline: what the pages will be asked, per step
    $P respec <builds.json> <build id> --who <uuid>  # StartRespec, cc_apply, every level-up, the spell picks by click
    $P dress <builds.json> <build id> --who <uuid>   # its items out, the tier's in, its prepared spells
    $P read-levels --who <uuid>                      # level, classes, max HP, race
    $P setup-check <arena.json>                      # arena.setup on the running game: each twin against its reference

The hire and the party XP are probe_route_h.py's (`hire --class X`, `xp --amount N`). Fail closed: a page that doesn't
open, a pick the page refuses or a problem in a job's result stops the step with exit 1 and the reads so far; nothing is
retried and nothing guessed. The respec page's flow above level 1 (P4) has not run live: the first run's answer goes in
the log before anything is built on it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arena_builds as ab
import make_arena_save as mas
import probe_route_h as rh

REPO = Path(__file__).resolve().parents[2]
ITEM_WAIT_S = 5.0
FINISH_WAIT_S = 15.0  # the respec applied: page closed and RespecCompleted, as probe_route_h's EVENT_WAIT_S
POLL_S = 0.25
JOB_WAIT_S = 120.0
CLICK_PASSES = 3  # a respec's spell clicks: own picks off and the plan's on, then the page read again
CLICK_BUDGET_S = 180.0  # a Wizard's 9 picks out and 9 in; leveling's 45 s is for the MCP tool's timeout, not this run
# The page's label for a subclass (pick() takes a unique substring): what's left of the game's name.
SUBCLASS_LABEL = {
    "BattleMaster": "Battle Master",
    "LifeDomain": "Life",
    "EvocationSchool": "Evocation",
    "Thief": "Thief",
    "TotemWarriorPath": "Wildheart",
    "Hunter": "Hunter",
    "LightDomain": "Light",
    "AbjurationSchool": "Abjuration",
    "LoreCollege": "Lore",
    "ValorCollege": "Valor",
}  # what ui_plans refuses to guess: a subclass not listed here


class RefStepError(RuntimeError):
    """A step can't go on: its text says what was read."""


def _label(sub: str | None) -> str:
    if sub not in SUBCLASS_LABEL:
        raise RefStepError(f"no page label for subclass {sub} (SUBCLASS_LABEL)")
    return SUBCLASS_LABEL[sub]


def _spell_kinds(cls: str, sub: str | None, level: int, static: dict[str, Any]) -> list[tuple[int, str, bool]]:
    """Per SelectSpells selector of the rows up to `level`, in the kit's order: (level, list id, cantrips?). A class's
    cantrip selector is the one flagged AlwaysPrepared (a spellbook's isn't)."""
    out = []
    for r in mas.selector_rows(cls, sub, level, static, cls):
        for name, args in mas.selectors(r["row"]["Selectors"]):
            if name == "SelectSpells":
                out.append((r["level"], args[0], len(args) > 6 and args[6] == "AlwaysPrepared"))
    return out


def ui_plans(build: dict[str, Any], level: int, static: dict[str, Any]) -> dict[str, Any]:
    """What the pages are asked, with the game's ids where a display name is needed (`names` lists them; the run
    translates them in the game): {cc: ui.cc_apply's plan, level_ups: [{level, plan}], names: [ids]}."""
    cls, sub = build["class"], build.get("subclass")
    script = build["script"]
    cc, ups = script["cc"], script["level_ups"]
    kinds = _spell_kinds(cls, sub, level, static)
    lists = {1: list(cc.get("spells", []))}
    for u in ups:
        lists[u["level"]] = list(u.get("spells", []))
    names: list[str] = []

    def spells_of(lv: int, plan: dict[str, Any]) -> None:
        for (_, _, cantrips), ids in zip([k for k in kinds if k[0] == lv], lists.get(lv, []), strict=True):
            plan.setdefault("cantrips" if cantrips else "spells", []).extend(ids)
            names.extend(ids)

    plan: dict[str, Any] = {"class": cls, "abilities": cc["abilities"]}
    if "ability_bonus" in cc:
        plan["bonus"] = cc["ability_bonus"]
    if static["classes"][cls]["levels"]["1"].get("subclasses"):
        plan["subclass"] = _label(sub)
    if cc.get("skills"):
        plan["class_skills"] = list(cc["skills"])
    if cc.get("race_skills"):
        plan["race_skills"] = list(cc["race_skills"])
    if cc.get("expertise"):
        plan["expertise"] = list(cc["expertise"])
    if cc.get("passives"):
        plan["passives"] = [x for ids in cc["passives"] for x in ids]
        names.extend(plan["passives"])
    spells_of(1, plan)
    if cc.get("race_spells"):
        plan.setdefault("cantrips", []).extend(cc["race_spells"])
        names.extend(cc["race_spells"])
    out = []
    for u in ups:
        lv = u["level"]
        p: dict[str, Any] = {}
        row = static["classes"][cls]["levels"][str(lv)]
        if row.get("subclasses"):
            p["subclass"] = _label(sub)
        if u.get("passives"):
            p["passives"] = [x for ids in u["passives"] for x in ids]
            names.extend(p["passives"])
        for kind in ("skills", "expertise"):
            picks = [x for ids in u.get(kind, []) for x in ids]
            if picks:
                p[kind] = picks
        spells_of(lv, p)
        imp = u.get("improvement")
        if imp:
            if "asi" in imp:
                p["abilities"] = imp["asi"]
            else:
                p["feat"] = imp["feat"]
                if imp.get("abilities"):
                    p["abilities"] = imp["abilities"]
                if imp.get("passives"):
                    p["passives"] = p.get("passives", []) + imp["passives"]
                    names.extend(imp["passives"])
                if imp.get("spells"):
                    p["spells"] = p.get("spells", []) + imp["spells"]
                    names.extend(imp["spells"])
        out.append({"level": lv, "plan": p})
    return {"cc": plan, "level_ups": out, "names": sorted(set(names))}


def translate(plan: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """A plan with its ids as the game's display names; RefStepError on an id the game gave no name."""
    out = dict(plan)
    for k in ("passives", "spells", "cantrips"):
        if k in out:
            missing = [x for x in out[k] if not names.get(x)]
            if missing:
                raise RefStepError(f"no display name read for {', '.join(missing)}")
            out[k] = [names[x] for x in out[k]]
    return out


NAMES_LUA = """
local out = {}
for _, id in ipairs(%s) do
    local st = Ext.Stats.Get(id)
    out[id] = st and Ext.Loca.GetTranslatedString(st.DisplayName) or ""
end
return Ext.Json.Stringify(out)
"""


def _lua_str(x: str) -> str:
    return json.dumps(x)


def _lua_list(ids: list[str]) -> str:
    return "{" + ",".join(_lua_str(x) for x in ids) + "}"


# After the party join (S1 read these before it; a pending level-up shows a LEVEL UP line, and Sym.FIELDS has neither).
LEVEL_STATE_LUA = """
local V = Mods.BG3AI.BG3AI.Visibility
local out = {}
for _, u in ipairs(Ext.Json.Parse(%s)) do
    local e = V.entity(u)
    out[#out + 1] = { uuid = u, level = e.EocLevel.Level, available = e.AvailableLevel.Level,
        can_level_up = e.CanLevelUp ~= nil }
end
return Ext.Json.Stringify(out)
"""


def level_state(game: Any, uuids: list[str]) -> list[dict[str, Any]]:
    return json.loads(game.eval(LEVEL_STATE_LUA % _lua_str(json.dumps(uuids)), context="server"))


def setup_check(game: Any, arena: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """arena.setup on the loaded start save (S4). -> (exit code, what to log): 0 when it stood the match up (every twin
    its build's, none with a level-up pending), 1 with the error and its rows when it didn't. A match stood up is left
    unplayed: load again."""
    r = game.run_job("arena.setup", {"arena": arena, "round_cap": arena.get("round_cap")}, wait_s=JOB_WAIT_S)
    res = r.result if isinstance(r.result, dict) else {}
    keep = ("reference", "xp", "not_applied", "teams", "members", "positions", "surfaces", "spawn_order")
    rec = {"status": r.status, "error": r.error} | {k: res[k] for k in keep if k in res}
    if r.status != "done":
        return 1, rec
    twins = [u for side in res.get("teams", {}).values() for u in side]
    if twins:
        rec["level_state"] = level_state(game, twins)
        pending = [x["uuid"] for x in rec["level_state"] if x["can_level_up"] or x["available"] != x["level"]]
        if pending:
            rec["error"] = f"a level-up pending after the party join: {', '.join(pending)}"
            return 1, rec
    return 0, rec


class Live:
    """The live steps over probe_route_h's RouteH (chunks, page reads) and the bridge's Game. Tests give fakes."""

    def __init__(
        self,
        route: Any,
        game: Any,
        level_up: Callable[[str, dict[str, Any]], str],
        click: Callable[[list[dict[str, Any]]], dict[str, Any]],
        prepare: Callable[[str, list[str]], str],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.route, self.game, self.level_up, self.click, self.prepare = route, game, level_up, click, prepare
        self.sleep, self.clock = sleep, clock

    def osi(self, fn: str, *args: Any) -> Any:
        """One Osiris call; RefStepError when the game refuses it. -> its value."""
        r = self.route.ev(rh.call(fn, *args))
        if not (isinstance(r, dict) and r.get("ok")):
            raise RefStepError(f"Osi.{fn}: {r}")
        return r.get("ret")

    def names(self, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        out = self.route.ev(NAMES_LUA % _lua_list(ids))
        if not isinstance(out, dict) or "error" in out:
            raise RefStepError(f"display names: {out}")
        return out

    def cc_apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        r = self.game.run_job("ui.cc_apply", {"plan": plan, "finish": False}, context="client", wait_s=JOB_WAIT_S)
        res = r.result if isinstance(r.result, dict) else {}
        if r.status != "done":
            raise RefStepError(f"ui.cc_apply {r.status}: {r.error} ({res.get('problems')})")
        return res

    def respec(self, plans: dict[str, Any], who: str) -> dict[str, Any]:
        log: dict[str, Any] = {"who": who}
        names = self.names(plans["names"])
        cc = translate(plans["cc"], names)
        pre = self.route.ev(rh.PRE)  # installs the route's listeners; its mark is where this respec's events start
        mark = pre.get("mark") if isinstance(pre, dict) else None
        if not isinstance(mark, int | float):
            raise RefStepError(f"no event mark: {pre}")
        self.osi("StartRespec", who)
        opened, pages = self.route.page_open("CharacterRespec")
        if not opened:
            raise RefStepError(f"CharacterRespec didn't open (pages {pages})")
        res = self.cc_apply(cc)
        log["clicked"] = []
        for _ in range(CLICK_PASSES):  # a hireling's own picks off, then the plan's on; the page says when none are left
            needs = res.get("needs_clicks") or []
            if not needs:
                break
            clicks = self.click(needs)
            log["clicked"].append(clicks)
            if clicks.get("failed"):
                raise RefStepError(f"respec spell picks failed: {clicks['failed']}")
            res = self.cc_apply(cc)
        if res.get("needs_clicks"):
            raise RefStepError(f"respec spell picks still open after {CLICK_PASSES} passes: {res['needs_clicks']}")
        log["cc"] = res
        if res.get("problems") or res.get("complete") is not True:
            set_ = res.get("set") or {}
            raise RefStepError(
                f"the respec page isn't complete: {res.get('problems')} {res.get('validity')} "
                f"abilities {set_.get('abilities')} skills {set_.get('skills')}"
            )
        log["finish"] = self.game.rpc("ui.cc_finish", {}, context="client")
        log["events"] = self.applied(int(mark))
        log["level_ups"] = []
        for u in plans["level_ups"]:
            text = self.level_up(who, translate(u["plan"], names))
            log["level_ups"].append({"level": u["level"], "out": text})
            if not text.startswith("level_up: done"):
                raise RefStepError(f"level {u['level']}: {text}")
        return log

    def applied(self, mark: int) -> list[dict[str, Any]]:
        """FinishCreating only asks: the respec has landed when its page is gone and RespecCompleted fired, and a
        level-up before that reads the old character. -> the events; RefStepError with what shows instead."""

        def look() -> dict[str, Any]:
            return {"pages": self.route.ev(rh.PAGES, "client"), "events": self.route.events(mark)}

        def landed(r: dict[str, Any]) -> bool:
            gone = isinstance(r["pages"], list) and "CharacterRespec" not in r["pages"]
            return gone and any(e.get("e") == "RespecCompleted" for e in r["events"])

        ok, r = self.route.wait(look, landed, FINISH_WAIT_S)
        if not ok:
            box = self.route.ev(rh.BOX_READ, "client")
            raise RefStepError(f"respec not applied in {FINISH_WAIT_S:.0f} s: {r}, box {box}")
        return r["events"]

    def dress(self, kit: dict[str, Any], who: str) -> dict[str, Any]:
        """The hireling's items out, the kit's in, by the calls arena.mirror_kit makes, then its prepared spells by the
        spellbook page. A profile's equipment, inventory, AC and derived then compare with a twin's."""
        log: dict[str, Any] = {"removed": self.route.ev(CLEAR_LUA % _lua_str(who)), "items": []}
        # RequestDelete lands later, and a kit item of the same template would be found in the old one's place.
        gone, left = self.route.wait(
            lambda: self.route.ev(ITEMS_LEFT_LUA % _lua_str(who)),
            lambda r: isinstance(r, dict) and r.get("deleted") == 0,
            ITEM_WAIT_S,
        )
        if not gone:
            raise RefStepError(f"the hireling's items didn't go in {ITEM_WAIT_S:.0f} s: {left}")
        for it in kit["items"]:
            self.osi("TemplateAddTo", it["root"], who, it["count"], 0)
            end, item = self.clock() + ITEM_WAIT_S, None
            while not item:
                item = self.osi("GetItemByTemplateInInventory", it["root"], who)
                if not item and self.clock() >= end:
                    raise RefStepError(f"{it['item']} didn't reach {who} in {ITEM_WAIT_S:.0f} s")
                if not item:
                    self.sleep(POLL_S)
            if it["equip"]:
                self.osi("Equip", who, item, 1, 0, 1)
            log["items"].append(it["item"])
        if kit.get("prepared"):
            names = self.names(kit["prepared"])
            text = self.prepare(who, [names[x] for x in kit["prepared"]])
            log["prepared"] = text
            if "FAILED" in text or "REFUSED" in text:
                raise RefStepError(f"prepare_spells: {text}")
        return log


CLEAR_LUA = """
local who = %s
local V = Mods.BG3AI.BG3AI.Visibility
local e = V.entity(who)
local n = 0
for _, inv in ipairs(Mods.BG3AI.BG3AI.Util.items(e.InventoryOwner.Inventories)) do
    for _, it in pairs(inv.InventoryContainer.Items) do
        local iu = V.entityUuid(it.Item)
        if iu then Osi.RequestDelete(iu) n = n + 1 end
    end
end
return Ext.Json.Stringify({ deleted = n })
"""
ITEMS_LEFT_LUA = CLEAR_LUA.replace("Osi.RequestDelete(iu) ", "")  # the same walk, counting only


def _find(path: Path, build_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    doc = ab.load_builds(path)
    for b in doc["builds"]:
        if b["id"] == build_id:
            return doc, b
    raise ab.BuildError(f"{doc['name']} has no build {build_id}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("step", choices=("plan", "respec", "dress", "read-levels", "setup-check"))
    ap.add_argument("builds", type=Path, nargs="?", help="the build file (setup-check: the arena file)")
    ap.add_argument("build", nargs="?")
    ap.add_argument("--who", default="")
    ap.add_argument("--out", type=Path, default=REPO / "runtime" / "probes" / "4v4-references")
    a = ap.parse_args(argv)
    static = mas.load_static()
    try:
        if a.step == "plan":
            doc, b = _find(a.builds, a.build)
            print(json.dumps(ui_plans(b, doc["level"], static), indent=1))
            return 0
        if not a.who and a.step != "setup-check":
            ap.error(f"{a.step} needs --who")
        os.environ.setdefault("BG3AI_RUNTIME", str(REPO / "runtime" / "arena_reference"))
        sys.path.insert(0, str(REPO / "bridge" / "src"))
        from bg3ai import leveling
        from bg3ai.tools import core, progression, world_tools

        game = core.game
        if a.step == "setup-check":
            code, rec = setup_check(game, json.loads(a.builds.read_text()))
            print(json.dumps(rec, indent=1, default=str))
            return code
        live = Live(
            rh.RouteH(game, a.out),
            game,
            lambda who, plan: progression.level_up(who, plan),
            lambda needs: leveling.click_spells(
                lambda op, x: game.rpc(op, x, context="client"), world_tools._inp(), needs, budget_s=CLICK_BUDGET_S
            ),
            lambda who, add: progression.prepare_spells(who, add=add),
        )
        if a.step == "read-levels":
            p = game.rpc("arena.profile", {"uuid": a.who})
            read = {k: p.get(k) for k in ("name", "level", "classes", "max_hp", "race")}
            print(json.dumps({**read, "level_state": level_state(game, [a.who])}, indent=1))
            return 0
        doc, b = _find(a.builds, a.build)
        if a.step == "respec":
            rec = live.respec(ui_plans(b, doc["level"], static), a.who)
        else:
            kit = mas.kit_plan(
                ab.kit_of(b, doc["level"], static),
                b["class"],
                doc["level"],
                static,
                f"build {b['id']}",
                base=ab.stat_abilities()[b["class"]],
            )["kit"]
            rec = live.dress(kit, a.who)
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / f"{doc['name']}-{b['id']}-{a.step}.json").write_text(json.dumps(rec, indent=1, default=str) + "\n")
        print(json.dumps(rec, indent=1, default=str))
        return 0
    except (RefStepError, mas.SpecError) as e:
        print(f"arena_reference: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
