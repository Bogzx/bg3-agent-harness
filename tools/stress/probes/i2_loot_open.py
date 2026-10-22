"""I2 loot a container (live1 §8, L6): the nearest unopened barrel, shelf or chest, opened with Osi.Open. PASS: it
reads opened afterwards; its item count before and after is in the evidence (the contents are generated before any
open, live2 §3)."""

from __future__ import annotations

import json

from probelib import Probe, Verdict, main

FIND = r"""local h=Osi.GetHostCharacter(); local hp=Ext.Entity.Get(h).Transform.Transform.Translate
local best,bd
for _,e in ipairs(Ext.Entity.GetAllEntitiesWithComponent("InventoryOwner")) do
  if e.ServerItem and e.Transform then local p=e.Transform.Transform.Translate
   local d=math.sqrt((p[1]-hp[1])^2+(p[3]-hp[3])^2)
   local nm=Ext.Loca.GetTranslatedString(Osi.GetDisplayName(e.Uuid.EntityUuid) or "")
   if (nm=="Shelf" or nm=="Wooden Barrel" or nm:find("Chest")) and Osi.IsOpened(e.Uuid.EntityUuid)==0
     and (not bd or d<bd) then best,bd=e.Uuid.EntityUuid,d end end end
return Ext.Json.Stringify({uuid=best, dist=bd})"""
COUNT = r"""local e=Ext.Entity.Get(%s) local n=0
pcall(function() for _,inv in ipairs(e.InventoryOwner.Inventories) do for _,_ in pairs(inv.InventoryContainer.Items) do
  n=n+1 end end end)
return Ext.Json.Stringify({n=n, opened=Osi.IsOpened(%s),
  name=Ext.Loca.GetTranslatedString(Osi.GetDisplayName(%s) or "")})"""


def judge(before: dict, after: dict) -> Verdict:
    what = f"{before.get('name')}: {before.get('n')} items before, {after.get('n')} after"
    if before.get("opened") == 0 and after.get("opened") == 1:
        return "PASS", f"{what}, opened"
    return "FAIL", f"{what}; IsOpened {before.get('opened')} -> {after.get('opened')}"


def run(p: Probe) -> Verdict:
    t = json.loads(p.lua(FIND))
    if not t.get("uuid"):
        return "FAIL", "no unopened barrel, shelf or chest near the party"
    u = json.dumps(t["uuid"])
    before = json.loads(p.lua(COUNT % (u, u, u)))
    p.lua(f"Osi.Open({u}) return 1")
    p.sleep(3.0)
    return judge(before, json.loads(p.lua(COUNT % (u, u, u))))


if __name__ == "__main__":
    raise SystemExit(main(run))
