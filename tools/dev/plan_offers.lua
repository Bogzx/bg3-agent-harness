-- Read-only: what each member's next level-ups offer (Progression rows by class/subclass Name + Level).
-- Called by tools/dev/check_plans.py with MEMBERS = { uuid, ... } and AHEAD = 2 prepended. Returns JSON.
local function loca(h) local ok, s = pcall(Ext.Loca.GetTranslatedString, h); return (ok and s ~= "") and s or nil end
local function spellName(id)
    local st = Ext.Stats.Get(id)
    return st and loca(st.DisplayName) or id, st and tonumber(st.Level) or nil
end
local function passiveName(id)
    local st = Ext.Stats.Get(id)
    return st and loca(st.DisplayName) or id
end
local rows = {}
for _, pid in ipairs(Ext.StaticData.GetAll("Progression")) do
    local pr = Ext.StaticData.Get(pid, "Progression")
    if pr then
        local k = tostring(pr.Name) .. ":" .. tostring(pr.Level)
        rows[k] = rows[k] or {}
        table.insert(rows[k], pr)
    end
end
local out = {}
for _, u in ipairs(MEMBERS) do
    local e = Ext.Entity.Get(u)
    local m = { uuid = u, name = loca(Osi.GetDisplayName(u)) or u, classes = {}, known = {}, levels = {} }
    pcall(function() for _, s in pairs(e.SpellBook.Spells) do m.known[#m.known + 1] = (spellName(s.Id.Prototype)) end end)
    local total = 0
    for _, c in pairs(e.Classes.Classes) do
        local cd = Ext.StaticData.Get(c.ClassUUID, "ClassDescription")
        local sd = c.SubClassUUID and Ext.StaticData.Get(c.SubClassUUID, "ClassDescription")
        m.classes[#m.classes + 1] = { class = cd and cd.Name, sub = sd and sd.Name, level = c.Level }
        total = total + c.Level
        for step = 1, AHEAD do
            local lv = c.Level + step
            local L = { level = total - c.Level + lv, spells = {}, passives = {}, skills = 0, feat = false }
            for _, nm in ipairs({ cd and cd.Name, sd and sd.Name }) do
                for _, pr in ipairs(nm and rows[nm .. ":" .. lv] or {}) do
                    for _, s in pairs(pr.SelectSpells or {}) do
                        local l = Ext.StaticData.Get(s.SpellUUID, "SpellList")
                        local opts = {}
                        for _, sid in pairs(l and l.Spells or {}) do
                            local n, sl = spellName(sid)
                            opts[#opts + 1] = { name = n, level = sl }
                        end
                        L.spells[#L.spells + 1] = { amount = s.Amount, list = l and l.Name or "", options = opts }
                    end
                    for _, s in pairs(pr.SelectPassives or {}) do
                        local l = Ext.StaticData.Get(s.UUID, "PassiveList")
                        local opts = {}
                        for _, p in pairs(l and l.Passives or {}) do opts[#opts + 1] = passiveName(p) end
                        L.passives[#L.passives + 1] = { amount = s.Amount, list = l and l.Name or "", options = opts }
                    end
                    for _, s in pairs(pr.SelectSkills or {}) do L.skills = L.skills + (s.Amount or 0) end
                    if pr.AllowImprovement then L.feat = true end
                end
            end
            m.levels[#m.levels + 1] = L
        end
    end
    out[#out + 1] = m
end
return Ext.Json.Stringify(out)
