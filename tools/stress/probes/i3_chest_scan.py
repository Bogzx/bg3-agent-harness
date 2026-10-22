"""I3 container loot is generated before any open (live2 §3, settled): every container within <radius> m with a
treasure table that is in the world (not OffStage) already carries HasGeneratedTreasure. PASS: all of them do
(and there is at least one); the scan is kept in chest_scan.json.

    i3_chest_scan.py [<radius m>=60]
"""

from __future__ import annotations

import json

from probelib import Probe, Verdict, arg, main

SCAN = r"""local h=Osi.GetHostCharacter(); local hp=Ext.Entity.Get(h).Transform.Transform.Translate
local out={}
for _,e in ipairs(Ext.Entity.GetAllEntitiesWithComponent("InventoryOwner")) do
  if e.ServerItem and e.Transform then
    local p=e.Transform.Transform.Translate
    local d=math.sqrt((p[1]-hp[1])^2+(p[3]-hp[3])^2)
    if d < %s then
      local u=e.Uuid.EntityUuid
      local inv={} pcall(function() local v=e.ServerItem.Template.InventoryList; v=v.Value or v
        for _,x in ipairs(v) do inv[#inv+1]=tostring(x) end end)
      local n=0 pcall(function() for _,iv in ipairs(e.InventoryOwner.Inventories) do
        for _,_ in pairs(iv.InventoryContainer.Items) do n=n+1 end end end)
      out[#out+1]={u=u, name=Ext.Loca.GetTranslatedString(Osi.GetDisplayName(u) or ""), d=math.floor(d*10)/10,
        inv=inv, n=n, opened=Osi.IsOpened(u), gen=e.HasGeneratedTreasure~=nil, flags=tostring(e.ServerItem.Flags)}
    end
  end
end
return Ext.Json.Stringify(out)"""


def treasure(row: dict) -> bool:
    return any(x and x != "Empty" for x in row.get("inv") or [])


def judge(rows: list[dict]) -> Verdict:
    tables = [r for r in rows if treasure(r)]
    on = [r for r in tables if "OffStage" not in str(r.get("flags"))]
    missing = [r for r in on if not r.get("gen")]
    opened = sum(1 for r in on if r.get("opened") == 1)
    what = (
        f"{len(rows)} containers, {len(tables)} with a treasure table, {len(on)} on stage "
        f"({opened} opened), {len(on) - len(missing)} generated"
    )
    if not on:
        return "FAIL", f"{what}: nothing to judge"
    if missing:
        return "FAIL", f"{what}; not generated: " + ", ".join(f"{r['name']} {r['d']} m" for r in missing[:6])
    return "PASS", what


def run(p: Probe) -> Verdict:
    rows = json.loads(p.lua(SCAN % float(arg(p, 0, "60"))))
    rows = rows if isinstance(rows, list) else []
    (p.out / "chest_scan.json").write_text(json.dumps(rows, indent=1))
    return judge(rows)


if __name__ == "__main__":
    raise SystemExit(main(run))
