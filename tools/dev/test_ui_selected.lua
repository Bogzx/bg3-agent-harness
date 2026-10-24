-- Offline tests for Client/UISettings.lua's HUD selection read (ui.selected, act A28): which character the HUD shows
-- when several client entities hold ClientControl (S091, the Emperor beside Claude's portrait in the Netherbrain fight).
--   lua tools/dev/test_ui_selected.lua mod/Mods/BG3AI/ScriptExtender/Lua
local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local pass, fail = 0, 0
local function ok(cond, name)
    if cond then pass = pass + 1; print("ok   " .. name) else fail = fail + 1; print("FAIL " .. name) end
end

local ops = {}
local entities = {}   -- in GetAllEntitiesWithComponent order
local hud = { CurrentPlayer = { SelectedCharacter = nil } }
Ext = {
    Entity = { GetAllEntitiesWithComponent = function(c)
        local out = {}
        for _, e in ipairs(entities) do if e[c] then out[#out + 1] = e end end
        return out
    end, Get = function(u) for _, e in ipairs(entities) do if e.Uuid.EntityUuid == u then return e end end end },
    Events = { Tick = { Subscribe = function() end } },
    Stats = { Get = function(id) return names and names[id] and { DisplayName = names[id] } or nil end },
    Utils = { GetGlobalSwitches = function() return {} end, GetGameState = function() return "Running" end },
}
local UI = {}
function UI.path(o, p)
    for part in p:gmatch("[^%.]+") do
        if type(o) ~= "table" then return nil end
        o = o[part]
    end
    return o
end
function UI.dc(n) return n == "HotBar" and hud or nil end
function UI.anyDC() return hud end
function UI.items(c, max)
    local i, n = 0, type(c) == "table" and #c or 0
    return function() i = i + 1 if i <= n then return i, c[i] end end
end
function UI.count(c) return type(c) == "table" and #c or 0 end
function UI.props(vm) return { Enabled = vm.Enabled, Ask = vm.Ask } end
-- Client/UI.lua's: a bg3le 0011 "UI busy" refusal (the call never ran) raises busy (here "BUSY ...")
function UI.pcall(f, ...)
    local r = table.pack(pcall(f, ...))
    if not r[1] and tostring(r[2]):find("UI busy", 1, true) then error("BUSY " .. tostring(r[2]), 0) end
    return table.unpack(r, 1, r.n)
end
local sent = {}
function UI.command(dc, name, vm)   -- the game applies the toggle (deferred live; at once here)
    sent[#sent + 1] = { name = name, vm = vm }
    if name == "InterruptToggleDisableCommand" then vm.Enabled = vm.Enabled == false end
    if name == "InterruptChangeAskCommand" then vm.Ask = not vm.Ask end
    return true
end
names = { Interrupt_Shield_Wizard = "hShield", Interrupt_Shield_Wizard_2 = "hShield",
                Interrupt_Shield_Wizard_3 = "hShield", Interrupt_Counterspell = "hCS" }
BG3AI = { RPC = { register = function(n, f) ops[n] = f end }, UI = UI,
          Util = { loca = function(h) return ({ hShield = "Shield", hCS = "Counterspell" })[h] end },
          UIActions = { waitJob = function(kind, ms, fn) local st, res = fn({}) return { status = st, result = res } end } }
dofile(LUA .. "/Client/UISettings.lua")
local G = BG3AI.UISettings

local function char(u, hp, max, lvl, control)
    return { Uuid = { EntityUuid = u }, Health = { Hp = hp, MaxHp = max }, EocLevel = { Level = lvl },
             ClientControl = control and {} or nil }
end
local function vm(hp, max, lvl) return { Stats = { Health = { Value = hp, Max = max }, Level = { Value = lvl } } } end

-- 1. one holder: it (the HUD VM isn't read)
local claude, emperor = char("claude", 59, 205, 12, true), char("emperor", 69, 145, 12, false)
local shadowheart = char("shadowheart", 34, 179, 12, false)
entities = { claude, emperor, shadowheart }
local u, all = G.selected()
ok(u == "claude" and #all == 1, "one ClientControl holder")
ok(ops["ui.selected"]().uuid == "claude" and ops["ui.selected"]().holders == nil, "ui.selected: one holder, no list")

-- 2. S091 08:20:23: Claude and the Emperor both hold ClientControl, the HUD shows the Emperor (Claude listed first)
emperor.ClientControl = {}
hud.CurrentPlayer.SelectedCharacter = vm(69, 145, 12)
u, all = G.selected()
ok(u == "emperor" and #all == 2, "S091: two holders -> the one the HUD's SelectedCharacter matches (not the first)")
local r = ops["ui.selected"]()
ok(r.uuid == "emperor" and #r.holders == 2, "ui.selected: the HUD's one + both holders")

-- 3. S091 08:22:46: the Emperor (first) and Shadowheart hold it, the HUD shows Shadowheart
claude.ClientControl, shadowheart.ClientControl = nil, {}
entities = { emperor, shadowheart, claude }
hud.CurrentPlayer.SelectedCharacter = vm(34, 179, 12)
ok((G.selected()) == "shadowheart", "S091: the Emperor listed first, the HUD on Shadowheart")
ok(pcall(ops["ui.reactions"], { char = "shadowheart" }), "ui.reactions accepts the HUD's character")
local okr, err = pcall(ops["ui.reactions"], { char = "emperor" })
ok(not okr and tostring(err):find("not the selected character", 1, true), "ui.reactions refuses the other holder")

-- 4. can't tell: no HUD VM, no match, two alike -> nil (the bridge then takes the server's word)
hud.CurrentPlayer.SelectedCharacter = nil
ok((G.selected()) == nil, "no HUD VM: unknown")
hud.CurrentPlayer.SelectedCharacter = vm(1, 2, 3)
ok((G.selected()) == nil, "no holder matches: unknown")
shadowheart.Health, shadowheart.EocLevel = { Hp = 69, MaxHp = 145 }, { Level = 12 }
hud.CurrentPlayer.SelectedCharacter = vm(69, 145, 12)
ok((G.selected()) == nil, "two holders alike: unknown")

-- 5. nobody holds it
entities = { char("x", 1, 1, 1, false) }
u, all = G.selected()
ok(u == nil and #all == 0 and ops["ui.selected"]().uuid == nil, "no holder: nil")

-- 6. set_reactions by name: every level's id (S092 08:48:37 {Shield: never} set Interrupt_Shield_Wizard_2 only of 6,
-- the prompt came back at 08:50:21); an id stays that one
local gale = char("gale", 177, 177, 12, true)
local ids = { "Interrupt_Counterspell", "Interrupt_Shield_Wizard_2", "Interrupt_Shield_Wizard_3", "Interrupt_Shield_Wizard" }
gale.InterruptContainer = { Interrupts = {} }
local vms = {}
for i, id in ipairs(ids) do
    gale.InterruptContainer.Interrupts[i] = { InterruptData = { Interrupt = id } }
    vms[i] = { Enabled = true, Ask = true }
end
entities = { gale }
hud.CurrentPlayer.SelectedCharacter = { PlayerCharacterProperties = { Interrupts = vms } }
sent = {}
local res = ops["ui.reactions"]({ char = "gale", set = { Shield = "never" } })
local modes = {}
for _, x in ipairs(res.result.reactions) do modes[x.id] = x.mode end
ok(res.status == "done" and modes.Interrupt_Shield_Wizard == "never" and modes.Interrupt_Shield_Wizard_2 == "never"
   and modes.Interrupt_Shield_Wizard_3 == "never" and modes.Interrupt_Counterspell == "ask",
   "S092: {Shield: never} sets all three Shield levels, not Counterspell")
ok(#sent == 3, "one toggle per Shield level")
res = ops["ui.reactions"]({ char = "gale", set = { Interrupt_Shield_Wizard_3 = "ask" } })
modes = {}
for _, x in ipairs(res.result.reactions) do modes[x.id] = x.mode end
ok(modes.Interrupt_Shield_Wizard_3 == "ask" and modes.Interrupt_Shield_Wizard == "never", "an id sets that one only")
local okn, errn = pcall(ops["ui.reactions"], { char = "gale", set = { Smite = "never" } })
ok(not okn and tostring(errn):find("no reaction 'Smite'", 1, true), "unknown name refused")

-- ui.settings: the options menu's mirror of a setting refused as busy (bg3le 0011; it never ran) raises busy, so the
-- op answers busy and is asked again, instead of reporting the setting applied with the menu's copy stale
local opts = {}
function opts.SetProperty(self, k, v)
    if self.refuse then error("bg3le: UI busy (frame lock not taken since a timeout); retry", 0) end
    self[k] = v
end
hud.CurrentPlayer.UIOptions = opts
opts.refuse = true
local oka, erra = pcall(G.apply, "subtitles", false)
ok(not oka and tostring(erra):find("^BUSY ") ~= nil and opts.ShowSubtitles == nil,
   "G.apply: a busy UI option set raises busy: " .. tostring(erra))
opts.refuse = nil
ok(G.apply("subtitles", false) == true and opts.ShowSubtitles == false, "G.apply: free UI, the option is set")
hud.CurrentPlayer.UIOptions = nil

print(string.format("%d passed, %d failed", pass, fail))
if fail > 0 then os.exit(1) end
