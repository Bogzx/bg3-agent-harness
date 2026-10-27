-- Live probe (night N3 follow-up, read-only): what marks a merchant? The Trader tag misses Mohan (Lower City), so this
-- reads, per non-party character within __R__ m of the leader (or whose name contains __PAT__), the trade treasure a
-- merchant generates its wares from: the character's CustomTradeTreasure and its template's / original template's
-- TradeTreasures (bg3le RootTemplates.h CharacterTemplate.TradeTreasures; {"Empty"} on a non-merchant per bg3se's
-- CharacterTests.lua), next to the Trader tag and Osi.CanTrade. Same placeholders as night-live/trader/reads.lua, so
-- its reads.py runs it (point `code = open(...)` at this file).
local R = __R__
local NAMEPAT = __PAT__
local function pc(f, ...) local ok, v = pcall(f, ...) if ok then return v end return "ERR:" .. tostring(v) end
local function nm(u) return pc(function() return Ext.Loca.GetTranslatedString(Osi.GetDisplayName(u)) end) end
local function list(t)
    if type(t) ~= "table" and type(t) ~= "userdata" then return tostring(t) end
    local out = {}
    pcall(function() for _, v in ipairs(t) do out[#out + 1] = tostring(v) end end)
    return table.concat(out, "|")
end
local lead = Osi.DB_PartyMembers:Get(nil)[1][1]
local lx, ly, lz = Osi.GetPosition(lead)
local out = {}
for _, e in ipairs(Ext.Entity.GetAllEntitiesWithComponent("ServerCharacter")) do
    local u = e.Uuid and e.Uuid.EntityUuid
    if u then
        local x, y, z = Osi.GetPosition(u)
        local d = x and math.sqrt((x - lx) ^ 2 + (y - ly) ^ 2 + (z - lz) ^ 2) or 1e9
        local n = nm(u)
        if (d <= R or (NAMEPAT ~= "" and tostring(n):lower():find(NAMEPAT, 1, true))) and Osi.IsPartyMember(u, 0) ~= 1 then
            local sc = e.ServerCharacter
            out[#out + 1] = {
                name = n, uuid = u, d = math.floor(d * 10) / 10,
                trader_tag = e.Trader ~= nil, cantrade = pc(Osi.CanTrade, u),
                custom_trade = pc(function() return tostring(sc.CustomTradeTreasure) end),
                tpl = pc(function() return tostring(sc.Template.Name) .. " " .. tostring(sc.Template.Id) end),
                tpl_trade = pc(function() return list(sc.Template.TradeTreasures) end),
                orig_trade = pc(function() return list(sc.OriginalTemplate.TradeTreasures) end),
                tpl_dialog = pc(function() return tostring(sc.Template.DefaultDialog) end),
                osi_template = pc(Osi.GetTemplate, u),
            }
        end
    end
end
table.sort(out, function(a, b) return a.d < b.d end)
return Ext.Json.Stringify(out)
