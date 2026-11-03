"""Check V1 (variant read, bp/variant-read 05981389): do spell stats read ModifierList == "SpellData" live?

    uv run --project bridge python tools/dev/live_v010/variant_read.py [--cast]

1. Sweep: every SpellData stats entry with a non-empty ContainerSpells; for each listed id, Ext.Stats.Get(id)
   .ModifierList. Prints the totals (containers, ids, ids whose ModifierList is SpellData / something else / no
   entry) and the first 15 bad ids. GuardCast.container_spells (GuardCast.lua:518-531) refuses a list with any
   non-SpellData id, so every bad id here is a container whose casts would show the fallback text.
2. The three named lists: every container whose name has LayOnHands / Command / DisguiseSelf, with its ids and
   their ModifierList.
3. --cast: use_ability on each of those containers (who = the first party member that has it; read from
   act.prepared / the hotbar is not needed: the guard refuses before any cast) -> must be refused with
   "choose a variant: <ids>" (real spell ids, no mesh/garbage text, not the "did not read as spell names" text).
"""

from __future__ import annotations

import sys

from common import call, lua_json

SWEEP = r"""
local names = Ext.Stats.GetStats("SpellData")
local out = { containers = 0, ids = 0, spelldata = 0, other = 0, missing = 0, bad = {}, named = {} }
for _, name in ipairs(names) do
    local st = Ext.Stats.Get(name)
    local raw = st and tostring(st.ContainerSpells or "") or ""
    if raw ~= "" then
        out.containers = out.containers + 1
        local rows = {}
        for id in raw:gmatch("[^;]+") do
            out.ids = out.ids + 1
            local v = Ext.Stats.Get(id)
            local ok, kind = pcall(function() return v and v.ModifierList end)
            kind = ok and kind or nil
            if kind == "SpellData" then out.spelldata = out.spelldata + 1
            elseif v == nil then out.missing = out.missing + 1
            else out.other = out.other + 1 end
            if kind ~= "SpellData" and #out.bad < 15 then
                out.bad[#out.bad + 1] = name .. " -> " .. id .. " = " .. tostring(kind)
            end
            rows[#rows + 1] = id .. "=" .. tostring(kind)
        end
        if name:find("LayOnHands") or name:find("Command") or name:find("DisguiseSelf") then
            out.named[name] = table.concat(rows, ", ")
        end
    end
end
return Ext.Json.Stringify(out)
"""

WHO_HAS = r"""
local spell = "%s"
for _, row in ipairs(Osi.DB_PartyMembers:Get(nil) or {}) do
    local u = row[1]
    local ok, has = pcall(Osi.HasSpell, u, spell)
    if ok and has == 1 then return Ext.Json.Stringify({ who = u:sub(-36) }) end
end
return Ext.Json.Stringify({ who = false })
"""


def main(argv: list[str]) -> int:
    r = lua_json(SWEEP, "server", "V1-sweep")
    if "error" in r:
        print("SWEEP FAILED:", r["error"])
        return 2
    print(
        f"containers {r['containers']}, listed ids {r['ids']}: SpellData {r['spelldata']}, other {r['other']}, "
        f"no stats entry {r['missing']}"
    )
    for b in r.get("bad") or []:
        print("  BAD", b)
    for name, rows in sorted((r.get("named") or {}).items()):
        print(f"  {name}: {rows}")
    verdict = "PASS" if r["ids"] and r["spelldata"] == r["ids"] else "LOOK"
    print(f"V1 sweep: {verdict} (PASS = every listed id reads SpellData)")
    if "--cast" in argv:
        for name in sorted(r.get("named") or {}):
            who = lua_json(WHO_HAS % name, "server").get("who")
            if not who:
                print(f"  {name}: no party member has it, skipped")
                continue
            call("V1-cast", "use_ability", {"ability": name, "who": who})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
