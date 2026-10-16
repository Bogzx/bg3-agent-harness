-- Offline tests for Client/Hotbar.lua (worker se, audit ACT-8 client half): the hotbar cell read, the
-- targeting state read, the tick-button confirm and the cancel. UI and Ext are stubbed with plain tables.
-- Run: tools/dev/test_hotbar.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local pass, fail = 0, 0
local function ok(cond, name)
    if cond then pass = pass + 1; print("ok   " .. name) else fail = fail + 1; print("FAIL " .. name) end
end

local ops = {}
BG3AI = { RPC = { register = function(n, f) ops[n] = f end } }

-- Collections: Lua arrays. Objects: tables. Elements: {Name=, kids=, DataContext=, IsMouseOver=, ...}.
local widgets = {}
local commands = {}
local UI = {}
BG3AI.UI = UI
function UI.get(o, k) if type(o) ~= "table" then return nil end return o[k] end
function UI.path(o, p)
    for part in p:gmatch("[^%.]+") do
        if type(o) ~= "table" then return nil end
        o = o[tonumber(part) or part]
    end
    return o
end
function UI.items(c, max)
    local i, n = 0, type(c) == "table" and #c or 0
    if max and n > max then n = max end
    return function() i = i + 1 if i <= n then return i, c[i] end end
end
function UI.name(e) return type(e) == "table" and e.Name or nil end
function UI.isVisible(e) return e.IsVisible ~= false end
function UI.widget(n) return widgets[n] end
function UI.dc(n) return widgets[n] and widgets[n].DataContext end
local function walk(e, pred, out, prune)
    if pred(e) then out[#out + 1] = e if prune then return end end
    for _, k in ipairs(e.kids or {}) do walk(k, pred, out, prune) end
end
function UI.findAll(e, pred, opts) local out = {} walk(e, pred, out, opts and opts.prune) return out end
function UI.cachedByName(w, n)
    local hits = UI.findAll(widgets[w], function(e) return e.Name == n end)
    return hits[1]
end
function UI.command(o, name)
    if type(o) ~= "table" or o[name] == nil then return false, "no command '" .. name .. "'" end
    commands[#commands + 1] = name
    if o[name] == "fail" then return false, "command cannot execute now" end
    return true
end

local entities = {}
Ext = { Entity = { Get = function(u) return entities[u] end,
                   GetAllEntitiesWithComponent = function(c)
                       local out = {}
                       for _, e in pairs(entities) do if e[c] then out[#out + 1] = e end end
                       return out
                   end } }

dofile(LUA .. "/Client/Hotbar.lua")
local H = BG3AI.Hotbar

-- A Common deck (Main Hand Attack, Jump) and a Class deck 2 rows x 3 cols (Bless at index 4, Magic Missile,
-- Shield, an Enhance Ability container).
local function act(id, extra)
    local c = { PrototypeID = id }
    for k, v in pairs(extra or {}) do c[k] = v end
    return c
end
local function slot(i, content, extra)
    local s = { SlotIndex = i, Content = content, CanUse = true }
    for k, v in pairs(extra or {}) do s[k] = v end
    return s
end
local common = { HotBarType = "CommonHotBar", CellColumns = 2, NumberOfBars = 1,
                 SlotList = { slot(0, act("Target_MainHandAttack")), slot(1, act("Projectile_Jump")) } }
local class = { HotBarType = "ClassHotBar", CellColumns = 3, NumberOfBars = 2,
                SlotList = { slot(10, act("Target_Guidance")), slot(11, act("Projectile_MagicMissile")),
                             slot(12, act("Shout_Shield"), { CanUse = false }), slot(13, act("Target_Bless")),
                             slot(14, act("Target_EnhanceAbility", { IsContainer = true })) } }
local dc = { CurrentPlayer = { SelectedCharacter = { PlayerCharacterProperties = { KeyboardHotBars = { common, class } } },
                               UIData = {} },
             CurrentShownDeck = { HotBarType = "ClassHotBar" },
             ForceCastCommand = true, CancelTaskCommand = true }

-- Elements: one LSButton "contentContainer" per slot, DataContext = the slot VM.
local cells = {}
local function deckEl(bar)
    local kids = {}
    for _, s in ipairs(bar.SlotList) do
        local b = { Name = "contentContainer", DataContext = s, ActualWidth = 88, ActualHeight = 88 }
        cells[s] = b
        kids[#kids + 1] = { Name = "HotbarItemsList", kids = { b } }
    end
    return { Name = "ConcertinaItem", kids = kids }
end
local conc = { Name = "Concertina", kids = { deckEl(common), deckEl(class) } }
local tabs = { Name = "HotbarDeckButton_Class" }
widgets.HotBar = { Name = "HotBar", DataContext = dc, kids = { conc, tabs } }

entities["c1"] = { ClientControl = {} }
entities["c2"] = {}

-- 1. a spell on the class deck
local r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", char = "c1" })
ok(r.found == true and r.deck == "ClassHotBar" and r.index == 4 and r.slot == 13, "Bless: deck, index, SlotIndex")
ok(r.cols == 3 and r.rows == 2 and r.order == 2, "deck grid and order")
ok(r.can_use == true and r.rendered == true and r.cell.w == 88, "usable, rendered, cell size")
ok(r.over == false and r.at == nil and r.over_hud == false, "cursor elsewhere")
ok(r.selected == true and r.shown_deck == "ClassHotBar", "caster selected, shown deck")
ok(r.deck_button == "HotbarDeckButton_Class", "deck tab name")
ok(r.upcast == nil and r.container == nil, "no upcast, no container")

-- 2. case-insensitive id; a not-selected caster
r = ops["ui.hotbar_slot"]({ spell = "target_bless", char = "c2" })
ok(r.found and r.index == 4 and r.selected == false, "case-insensitive, caster not selected")

-- 3. cursor on Magic Missile's cell: `at` names it; over only for the wanted cell
cells[class.SlotList[2]].IsMouseOver = true
conc.IsMouseOver = true
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless" })
ok(r.at and r.at.deck == "ClassHotBar" and r.at.index == 2 and r.at.spell == "Projectile_MagicMissile" and r.at.order == 2 and r.at.rows == 2, "at = the hovered cell (+ deck order, grid)")
ok(r.over == false and r.over_hud == true, "not over the wanted cell; over the decks")
r = ops["ui.hotbar_slot"]({ spell = "Projectile_MagicMissile" })
ok(r.over == true, "over the wanted cell")
cells[class.SlotList[2]].IsMouseOver = nil

-- 4. upcast variant: found by its base id, level reported
r = ops["ui.hotbar_slot"]({ spell = "Projectile_MagicMissile_3" })
ok(r.found and r.index == 2 and r.upcast == 3, "upcast variant -> base slot + level")

-- 5. not on the hotbar; a disabled slot; a container
r = ops["ui.hotbar_slot"]({ spell = "Target_HoldPerson" })
ok(r.found == false and r.why:find("not on the hotbar", 1, true) and r.why:find("7 slots in 2 decks", 1, true), "not on the hotbar")
r = ops["ui.hotbar_slot"]({ spell = "Shout_Shield" })
ok(r.found and r.can_use == false, "a slot that can't be used now")
r = ops["ui.hotbar_slot"]({ spell = "Target_EnhanceAbility" })
ok(r.found and r.container == true, "container flagged")

-- 6. a slot VM not rendered (a collapsed deck) -> rendered=false
local hidden = slot(15, act("Target_CureWounds"))
class.SlotList[#class.SlotList + 1] = hidden
r = ops["ui.hotbar_slot"]({ spell = "Target_CureWounds" })
ok(r.found and r.rendered == false and r.cell == nil, "a slot without a button: not rendered")
class.SlotList[#class.SlotList] = nil

-- 7. a button whose DataContext is another proxy of the same VM (tostring differs): matched by spell + SlotIndex
local b4 = cells[class.SlotList[4]]
b4.DataContext = { SlotIndex = 13, Content = act("Target_Bless") }
b4.IsMouseOver = true
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless" })
ok(r.rendered == true and r.over == true and r.at.index == 4, "proxy match by spell + SlotIndex")
b4.DataContext = class.SlotList[4]
b4.IsMouseOver = nil

-- 8. deck tab under the cursor (tabs=true only)
tabs.IsMouseOver = true
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless" })
ok(r.deck_button_over == nil, "tabs not read without tabs=true")
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", tabs = true })
ok(r.deck_button_over == "HotbarDeckButton_Class", "deck tab under the cursor")
tabs.IsMouseOver = nil

-- 9. no hotbar -> error; no decks -> why
widgets.HotBar = nil
ok(not pcall(ops["ui.hotbar_slot"], { spell = "Target_Bless" }), "no HotBar widget: error")
widgets.HotBar = { Name = "HotBar", DataContext = { CurrentPlayer = {} }, kids = {} }
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless" })
ok(r.found == false and r.why == "no hotbar decks for the selected character", "no decks")
widgets.HotBar = { Name = "HotBar", DataContext = dc, kids = { conc, tabs } }
ok(not pcall(ops["ui.hotbar_slot"], {}), "spell required")

-- 10. cast state: nothing in hand
r = ops["ui.cast_state"]({ char = "c1" })
ok(r.armed == false and r.counter == false and r.can_force == false and r.cast == nil, "nothing armed")

-- 11. Bless armed, 3 targets left, one picked on the cast entity
local task = { TargetValues = { Remaining = 1 }, CanForceCast = true, PreviewType = "Spell" }
dc.CurrentPlayer.UIData.ActiveTask = task
dc.CurrentActiveSlot = class.SlotList[4]
dc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.CurrentSpellTask = { TargetingType = "Target" }
local castE = { ClientSpellCastState = { Caster = entities["c1"], Spell = { Prototype = "Target_Bless" }, CastPhase = "None" },
                ClientSpellCastTargeting = { Targets = { {}, }, CurrentTargetIndex = 1, IsPreviewing = true } }
entities["c1"].SpellCastIsCasting = { Cast = castE }
r = ops["ui.cast_state"]({ char = "c1" })
ok(r.armed and r.counter and r.remaining == 1 and r.can_force, "armed, counter, remaining, tick enabled")
ok(r.spell == "Target_Bless" and r.targeting == "Target" and r.preview == "Spell", "spell in hand, targeting type")
ok(r.cast and r.cast.picked == 1 and r.cast.index == 1 and r.cast.spell == "Target_Bless" and r.cast.phase == "None",
   "cast entity: picked, index, phase")

-- 12. cast entity found by scan when the caster has no IsCasting
entities["c1"].SpellCastIsCasting = nil
entities["cast"] = castE
r = ops["ui.cast_state"]({ char = "c1" })
ok(r.cast and r.cast.picked == 1, "cast entity found by its Caster")
entities["cast"] = nil
r = ops["ui.cast_state"]({ char = "c2" })
ok(r.cast == nil and r.armed, "another caster: no cast entity")

-- 13. confirm: the tick button's command, only when enabled
commands = {}
r = ops["ui.cast_confirm"]({})
ok(r.done and commands[1] == "ForceCastCommand", "confirm runs ForceCastCommand")
task.CanForceCast = false
ok(not pcall(ops["ui.cast_confirm"], {}), "confirm refused while the tick is disabled")
task.CanForceCast = true
dc.ForceCastCommand = "fail"
local okc, err = pcall(ops["ui.cast_confirm"], {})
ok(not okc and tostring(err):find("ForceCastCommand", 1, true), "confirm error names the command")
dc.ForceCastCommand = true

-- 14. cancel
commands = {}
r = ops["ui.cast_cancel"]({})
ok(r.done and r.was_armed and commands[1] == "CancelTaskCommand", "cancel while armed")
dc.CurrentPlayer.UIData.ActiveTask = nil
commands = {}
r = ops["ui.cast_cancel"]({})
ok(r.done and r.was_armed == false and #commands == 0, "cancel with nothing armed: no command")
ok(not pcall(ops["ui.cast_confirm"], {}), "confirm with nothing in hand: error")

-- 15. baseSpell
ok(H.baseSpell("Projectile_MagicMissile_3") == "Projectile_MagicMissile", "baseSpell strips the level")
ok(H.baseSpell("Target_Bless") == nil, "baseSpell: no suffix")

print(string.format("%d passed, %d failed", pass, fail))
if fail > 0 then os.exit(1) end
