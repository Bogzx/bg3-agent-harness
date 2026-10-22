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
function UI.childCount(e) return type(e) == "table" and #(e.kids or {}) or 0 end
function UI.child(e, i) return (e.kids or {})[i] end
function UI.typeName(e) return type(e) == "table" and e.Type or nil end
local dupes = {}   -- extra "HotBar" pages listed before widgets.HotBar (a hidden duplicate)
function UI.widgets()
    local out = {}
    for _, w in ipairs(dupes) do out[#out + 1] = { name = w.Name, el = w } end
    for n, w in pairs(widgets) do out[#out + 1] = { name = n, el = w } end
    return out
end
function UI.cachedByName(w, n)
    local hits = UI.findAll(widgets[w], function(e) return e.Name == n end)
    return hits[1]
end
local params = {}
function UI.command(o, name, param)
    if type(o) ~= "table" or o[name] == nil then return false, "no command '" .. name .. "'" end
    commands[#commands + 1] = name
    params[#params + 1] = param
    if o[name] == "fail" then return false, "command cannot execute now" end
    return true
end

local entities = {}
local clockMs = nil
local stats = { Target_Bless = { DisplayName = "hBless;1" }, Projectile_MagicMissile = { DisplayName = "hMM;2" } }
local loca = { hBless = "Bless", hMM = "Magic Missile" }
BG3AI.Util = { loca = function(h) return h and loca[tostring(h):gsub(";%d+$", "")] or nil end }
Ext = { Utils = { MonotonicTime = function() if clockMs == nil then error("no clock") end return clockMs end },
        Stats = { Get = function(id) return stats[id] end },
        Entity = { Get = function(u) return entities[u] end,
                   GetAllEntitiesWithComponent = function(c)
                       local out = {}
                       for _, e in pairs(entities) do if e[c] then out[#out + 1] = e end end
                       return out
                   end } }

dofile(LUA .. "/Shared/HotbarMap.lua")
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

-- 16. LIVE shape (window #1, 2026-10-16): the slot VMs carry no PrototypeID, the buttons no DataContext, one
-- HotbarItemsList per deck; the ids come from the character's HotbarContainer (Element.Slot = SlotIndex).
local HM = BG3AI.HotbarMap
local function vslot(i) return { SlotIndex = i, Content = { IsContainer = false }, CanUse = true } end
local lcommon = { HotBarType = "CommonHotBar", CellColumns = 2, NumberOfBars = 1, SlotList = { vslot(0), vslot(1) } }
local lclass = { HotBarType = "ClassHotBar", CellColumns = 2, NumberOfBars = 2,
                 SlotList = { vslot(0), vslot(1), vslot(2), vslot(3) } }
local lpass = { HotBarType = "PassivesHotBar", CellColumns = 16, NumberOfBars = 2, SlotList = { vslot(0) } }
local ldc = { CurrentPlayer = { SelectedCharacter = { PlayerCharacterProperties = { KeyboardHotBars = { lcommon, lclass, lpass } } },
                                UIData = { ActiveTask = { PreviewType = "4294967295" } } },
              CurrentShownDeck = { HotBarType = "CommonHotBar" }, CancelTaskCommand = true }
local lcells = {}
local function ldeck(bar)
    local kids = {}
    for _, s in ipairs(bar.SlotList) do
        local b = { Name = "contentContainer", ActualWidth = 88, ActualHeight = 88 }   -- DataContext nil (live)
        lcells[#lcells + 1] = b
        kids[#kids + 1] = { Name = "ListBoxItem", kids = { b } }
    end
    return { Name = "ConcertinaItem", kids = { { Name = "HotbarItemsList", kids = { { Name = "Container", kids = kids } } } } }
end
local lconc = { Name = "Concertina", kids = { ldeck(lcommon), ldeck(lclass), ldeck(lpass) } }
widgets.HotBar = { Name = "HotBar", DataContext = ldc, kids = { lconc } }
local function el(slot, id) return { Slot = slot, SpellId = { Prototype = id, OriginatorPrototype = id } } end
local hc = { ActiveContainer = "DefaultBarContainer", Containers = { DefaultBarContainer = {
    { Index = 0, Controller = 0, Name = "", Elements = { el(0, "Target_MainHandAttack"), el(1, "Projectile_Jump") } },
    { Index = 1, Controller = 0, Name = "", Elements = { el(0, "Target_Guidance"), el(3, "Target_Bless"), { Slot = 2, Item = "x" } } },
    { Index = 2, Controller = 0, Name = "", Elements = {} } } } }
entities["c3"] = { ClientControl = {}, HotbarContainer = hc }
entities["c1"].ClientControl = nil
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", char = "c3" })
ok(r.found and r.deck == "ClassHotBar" and r.index == 4 and r.slot == 3 and r.ids == "map" and r.ids_from == "entity",
   "live shape: Bless found by the entity's hotbar component")
ok(r.rendered == true and r.cell and r.cell.w == 88, "live shape: button matched by position (no DataContext)")
lcells[3 + 3].IsMouseOver = true      -- common has 2 buttons: class index 4 = the 6th button
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", char = "c3" })
ok(r.over == true and r.at and r.at.deck == "ClassHotBar" and r.at.index == 4 and r.at.spell == "Target_Bless", "live shape: over + at by position")
lcells[6].IsMouseOver = nil
lcells[1].IsMouseOver = true
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless" })      -- no char: the ClientControl entity's map
ok(r.found and r.ids_from == "entity" and r.at and r.at.deck == "CommonHotBar" and r.at.spell == "Target_MainHandAttack",
   "live shape: no char -> the selected (ClientControl) entity; at = Main Hand Attack")
lcells[1].IsMouseOver = nil
r = ops["ui.hotbar_slot"]({ spell = "Target_MainHandAttack", char = "c3" })
ok(r.found and r.deck == "CommonHotBar" and r.index == 1 and r.rendered, "live shape: Main Hand Attack on the common deck")

-- 17. no component on the client entity: the bridge's map (args.map, string slot keys from JSON) is used
entities["c3"].HotbarContainer = nil
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", char = "c3" })
ok(r.found == false and r.ids == "none" and r.why:find("no spell ids: no HotbarContainer", 1, true), "no ids: why says so")
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", char = "c3", map = { ClassHotBar = { ["3"] = "Target_Bless" } } })
ok(r.found and r.index == 4 and r.ids == "map" and r.ids_from == "arg", "args.map from the bridge")
entities["c3"].HotbarContainer = hc

-- 18. a VM that does carry ids wins over the map (the old offline shape); no map read then
lclass.SlotList[4].Content.PrototypeID = "Target_Bless"
entities["c3"].HotbarContainer = nil
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", char = "c3" })
ok(r.found and r.ids == "vm" and r.ids_from == nil, "VM ids: no map needed")
lclass.SlotList[4].Content.PrototypeID = nil
entities["c3"].HotbarContainer = hc

-- 19. ListBox -> deck by ItemsSource when the counts tie (Common and Class both 17 live)
local tA = { HotBarType = "CommonHotBar", CellColumns = 1, NumberOfBars = 1, SlotList = { vslot(0) } }
local tB = { HotBarType = "ClassHotBar", CellColumns = 1, NumberOfBars = 1, SlotList = { vslot(0) } }
local bA, bB = { Name = "contentContainer" }, { Name = "contentContainer" }
ldc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.KeyboardHotBars = { tA, tB }
widgets.HotBar = { Name = "HotBar", DataContext = ldc, kids = {   -- the Class ListBox comes first in the tree
    { Name = "HotbarItemsList", ItemsSource = tB.SlotList, kids = { bB } },
    { Name = "HotbarItemsList", ItemsSource = tA.SlotList, kids = { bA } } } }
bB.IsMouseOver = true
r = ops["ui.hotbar_slot"]({ spell = "Target_Guidance", char = "c3" })
ok(r.at and r.at.deck == "ClassHotBar" and r.found and r.over == true, "ListBox matched by ItemsSource, not tree order")
bB.IsMouseOver = nil

-- 20. HotbarMap.fromComponent: active container, keyboard bar over a controller one, Name over Index
local m, info = HM.fromComponent({ ActiveContainer = "Other", Containers = {
    Other = { { Index = 1, Controller = 1, Elements = { el(5, "Target_Wrong") } },
              { Index = 1, Controller = 0, Elements = { el(5, "Target_Right") } },
              { Index = 9, Name = "CustomHotBar", Elements = { el(0, "Shout_Custom") } } },
    DefaultBarContainer = { { Index = 0, Elements = { el(0, "Target_NotThis") } } } } })
ok(info.container == "Other" and m.ClassHotBar["5"] == "Target_Right" and m.CustomHotBar["0"] == "Shout_Custom"
   and m.CommonHotBar == nil, "fromComponent: active container, Controller 0 wins, Name wins")
m, info = HM.fromComponent({ Containers = { DefaultBarContainer = { { Index = 0, Elements = { el(2, "A") } } } } })
ok(info.container == "DefaultBarContainer" and m.CommonHotBar["2"] == "A" and info.slots == 1, "fromComponent: DefaultBarContainer without ActiveContainer")
ok(next((HM.fromComponent({}))) == nil, "fromComponent: no containers -> empty")
local fm, why = HM.forChar("nobody")
ok(fm == nil and why == "no entity", "forChar: unknown uuid")

-- 21. cast state, live shape: the task object with nothing in hand is not "armed"
widgets.HotBar = { Name = "HotBar", DataContext = ldc, kids = { lconc } }
r = ops["ui.cast_state"]({ char = "c3" })
ok(r.task == true and r.armed == false and r.preview == "4294967295" and r.armed_by == nil, "live: ActiveTask with PreviewType none = not armed")
ldc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.CurrentSpellTask = { TargetingType = "Target" }
r = ops["ui.cast_state"]({ char = "c3" })
ok(r.armed == true and r.armed_by == "spell_task", "a CurrentSpellTask = armed")
ldc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.CurrentSpellTask = nil
ldc.CurrentPlayer.UIData.ActiveTask.TargetValues = { Remaining = 2 }
r = ops["ui.cast_state"]({ char = "c3" })
ok(r.armed == true and r.armed_by == "counter" and r.remaining == 2, "the cursor counter = armed")
ldc.CurrentPlayer.UIData.ActiveTask.TargetValues = nil
-- live6 se: after Dimension Door landed only the move cursor's preview was left (MoveTo): nothing in hand
ldc.CurrentPlayer.UIData.ActiveTask.PreviewType = "MoveTo"
r = ops["ui.cast_state"]({ char = "c3" })
ok(r.armed == false and r.armed_by == nil and r.preview == "MoveTo",
   "live6: the move cursor's MoveTo preview = not armed")
ldc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.CurrentSpellTask = { TargetingType = "Teleportation" }
r = ops["ui.cast_state"]({ char = "c3" })
ok(r.armed == true and r.armed_by == "spell_task", "a teleport's landing step (MoveTo + its spell task) = armed")
ldc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.CurrentSpellTask = nil
ldc.CurrentPlayer.UIData.ActiveTask.PreviewType = "Spell"
ok(ops["ui.cast_state"]({ char = "c3" }).armed_by == "preview", "a Spell preview alone = armed")
ldc.CurrentPlayer.UIData.ActiveTask.PreviewType = "4294967295"
ok(not pcall(ops["ui.cast_confirm"], {}), "confirm refused: the task object alone is not a spell in hand")
commands = {}
r = ops["ui.cast_cancel"]({})
ok(r.done and r.was_armed == false and commands[1] == "CancelTaskCommand", "cancel with the idle task: command sent, was_armed false")

-- 21b. S091 (11/11 `IN HAND: a spell`): the live shape names no spell (no PrototypeID on the active slot's VM, no char
-- from the bridge): the active cell's SlotIndex in the HUD character's hotbar map names it, by its shown name
local savedBars = ldc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.KeyboardHotBars   -- 19's two decks
ldc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.KeyboardHotBars = { lcommon, lclass, lpass }
ldc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.CurrentSpellTask = { TargetingType = "Target" }
ldc.CurrentActiveSlot = lclass.SlotList[4]                     -- SlotIndex 3 = Bless on the class deck
r = ops["ui.cast_state"]({})
ok(r.armed and r.spell == "Target_Bless" and r.spell_name == "Bless" and r.char == nil,
   "S091: the active cell named from the ClientControl entity's map (no char, no UISettings)")
BG3AI.UISettings = { selected = function() return "c3" end }   -- the HUD's selected character (UISettings.selected)
r = ops["ui.cast_state"]({})
ok(r.spell == "Target_Bless" and r.char == "c3", "S091: no char -> the HUD's selected character")
entities["c3"].HotbarContainer = nil                           -- no client map: the bridge passes the server's
r = ops["ui.cast_state"]({})
ok(r.armed and r.spell == nil and r.spell_name == nil and r.char == "c3", "S091: no map anywhere -> unnamed, char given")
r = ops["ui.cast_state"]({ char = "c3", map = { ClassHotBar = { ["3"] = "Projectile_MagicMissile" },
                                                CommonHotBar = { ["3"] = "Target_Wrong" } } })
ok(r.spell == "Projectile_MagicMissile" and r.spell_name == "Magic Missile", "S091: args.map names it (its deck, not another's)")
-- a proxy of the active slot that isn't the list's own (tostring differs): the deck it shows, else IsActive
ldc.CurrentActiveSlot = { SlotIndex = 3 }
ldc.CurrentShownDeck = { HotBarType = "ClassHotBar" }
r = ops["ui.cast_state"]({ char = "c3", map = { ClassHotBar = { ["3"] = "Projectile_MagicMissile" } } })
ok(r.spell == "Projectile_MagicMissile", "S091: an unmatched proxy -> the shown deck's cell")
lcommon.SlotList[2].IsActive = true
ldc.CurrentActiveSlot = { SlotIndex = 1 }
r = ops["ui.cast_state"]({ char = "c3", map = { CommonHotBar = { ["1"] = "Projectile_Jump" }, ClassHotBar = { ["1"] = "X" } } })
ok(r.spell == "Projectile_Jump", "S091: the slot marked IsActive picks the deck over the shown one")
lcommon.SlotList[2].IsActive = nil
ldc.CurrentShownDeck = { HotBarType = "CommonHotBar" }
-- nothing in hand: no name read, even with an active cell
ldc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.CurrentSpellTask = nil
r = ops["ui.cast_state"]({ char = "c3", map = { CommonHotBar = { ["1"] = "Projectile_Jump" } } })
ok(r.armed == false and r.spell == nil, "S091: not armed -> no spell named from the active cell")
ldc.CurrentActiveSlot = nil
entities["c3"].HotbarContainer = hc
BG3AI.UISettings = nil
ldc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.KeyboardHotBars = savedBars

-- 22. live window #2 (19:49): 39 probes never saw a cell. The shown widget over a hidden duplicate; the IsOverUI
-- flag; a hovered ListBoxItem names its cell when the button's own IsMouseOver stays false; the probe cache.
local sb = { HotBarType = "ClassHotBar", CellColumns = 2, NumberOfBars = 1, SlotList = { vslot(0), vslot(1) } }
local sdc = { CurrentPlayer = { SelectedCharacter = { PlayerCharacterProperties = { KeyboardHotBars = { sb } } },
                                UIData = { Cursor = { IsOverUI = false } } } }
local sbtn = { { Name = "contentContainer", ActualWidth = 88, ActualHeight = 88 },
               { Name = "contentContainer", ActualWidth = 88, ActualHeight = 88 } }
local sitems = { { Type = "ListBoxItem", kids = { { Name = "Border", kids = { sbtn[1] } } } },
                 { Type = "ListBoxItem", kids = { { Name = "Border", kids = { sbtn[2] } } } } }
local slist = { Name = "HotbarItemsList", ItemsSource = sb.SlotList, kids = { { Name = "Panel", kids = sitems } } }
local sconc = { Name = "Concertina", kids = { slist } }
local shown = { Name = "HotBar", DataContext = sdc, kids = { sconc } }
local hidden = { Name = "HotBar", IsVisible = false, DataContext = ldc, kids = { lconc } }
dupes = { hidden }
widgets.HotBar = shown
local smap = { ClassHotBar = { ["0"] = "Target_Guidance", ["1"] = "Target_Bless" } }
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", map = smap })
ok(r.found and r.deck == "ClassHotBar" and r.index == 2 and r.ids_from == "arg", "the shown HotBar, not the hidden first one")
ok(r.over == false and r.at == nil and r.over_ui == false and r.hover == nil, "cursor off the HUD: over_ui false, no chain")
-- the cursor on Bless's ListBoxItem: the item and its ancestors hover, the button's own flag stays false
sdc.CurrentPlayer.UIData.Cursor.IsOverUI = true
sconc.IsMouseOver, slist.IsMouseOver, slist.kids[1].IsMouseOver, sitems[2].IsMouseOver = true, true, true, true
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", map = smap })
ok(r.over_ui == true and r.over_hud == true, "over_ui + over_hud")
ok(r.at and r.at.deck == "ClassHotBar" and r.at.index == 2 and r.at.via == "item" and r.over == true,
   "a hovered ListBoxItem names its cell (via=item) -> over the wanted cell")
ok(r.hover and r.hover[#r.hover] == "ListBoxItem" and r.hover[1] == "Concertina", "hover chain names, deepest last")
r = ops["ui.hotbar_slot"]({ spell = "Target_Guidance", map = smap })
ok(r.at and r.at.index == 2 and r.over == false, "item hover: not over another wanted cell")
-- on the list's panel between items: the chain stops above any single button -> no cell, chain reported
sitems[2].IsMouseOver = nil
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", map = smap })
ok(r.at == nil and r.over == false and r.hover and r.hover[#r.hover] == "Panel", "between cells: no at, chain ends on the panel")
-- the cache: a 2nd read within CACHE_MS reuses the walk (a changed slot list is not seen), only with cache=true
clockMs = 1000
local calls, realDecks = 0, H.decks
H.decks = function(...) calls = calls + 1 return realDecks(...) end
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", map = smap, cache = true })
local c1 = calls
clockMs = 2000
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", map = smap, cache = true })
ok(c1 >= 1 and calls == c1 and r.cached == true and r.found and r.ids_from == "arg", "cache=true: 2nd read reuses the walk")
clockMs = 1000 + H.CACHE_MS + 1
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", map = smap, cache = true })
ok(calls > c1 and r.cached == nil, "cache expires after CACHE_MS")
local c2 = calls
r = ops["ui.hotbar_slot"]({ spell = "Target_Bless", map = smap })
ok(calls > c2 and r.cached == nil, "no cache flag: always a fresh walk")
H.decks, clockMs, dupes = realDecks, nil, {}
sdc.CurrentPlayer.UIData.Cursor.IsOverUI = false

-- 20. A7: the upcast level picker (live L4 shape: Dimension Door, levels 1-3 fake, 4 real, 5 the _5 upcast)
local ups = { { IsFake = true, CanUse = false }, { IsFake = true, CanUse = false }, { IsFake = true, CanUse = false },
              { IsFake = false, CanUse = true }, { IsFake = false, CanUse = true } }
local udc = { CurrentActiveSlot = { Content = { IsContainer = true, SpellUpcast = ups } }, IsSelectingUpcastedSpell = true,
              UseSlotCommand = true }
local savedHB = widgets.HotBar
widgets.HotBar = { Name = "HotBar", DataContext = udc, kids = {} }
commands, params = {}, {}
r = ops["ui.hotbar_upcast"]({})
ok(r.done and r.level == 4 and r.index == 4 and r.levels == 5 and r.open, "upcast: the lowest real level (4)")
ok(commands[1] == "UseSlotCommand" and params[1] == ups[4], "upcast: UseSlotCommand with that level's VM")
commands, params = {}, {}
r = ops["ui.hotbar_upcast"]({ level = 5 })
ok(r.level == 5 and params[1] == ups[5], "upcast: an asked level")
local okl, errl = pcall(ops["ui.hotbar_upcast"], { level = 2 })
ok(not okl and tostring(errl):find("level 2 is greyed out", 1, true), "upcast: a fake level is refused")
ups[4].CanUse, ups[5].CanUse = false, false
okl, errl = pcall(ops["ui.hotbar_upcast"], {})
ok(not okl and tostring(errl):find("no spell slot left", 1, true), "upcast: all greyed out")
udc.CurrentActiveSlot = nil
okl, errl = pcall(ops["ui.hotbar_upcast"], {})
ok(not okl and tostring(errl):find("picker is not open", 1, true), "upcast: no picker")
widgets.HotBar = savedHB

-- 21. A30: ui.hotbar_use = the cell's own click command (UseSlotCommand, CommandParameter = the slot VM)
local savedHB21 = widgets.HotBar
widgets.HotBar = { Name = "HotBar", DataContext = dc, kids = { conc, tabs } }
dc.UseSlotCommand = true
commands, params = {}, {}
r = ops["ui.hotbar_use"]({ spell = "Target_Bless" })
ok(r.done and r.deck == "ClassHotBar" and r.index == 4 and r.spell == "Target_Bless", "use: Bless's cell")
ok(commands[1] == "UseSlotCommand" and params[1] == class.SlotList[4], "use: UseSlotCommand with the slot VM")
r = ops["ui.hotbar_use"]({ spell = "Target_EnhanceAbility" })
ok(r.container == true and params[2] == class.SlotList[5], "use: a container cell (its picker opens)")
okl, errl = pcall(ops["ui.hotbar_use"], { spell = "Shout_Shield" })
ok(not okl and tostring(errl):find("greyed out", 1, true) and #commands == 2, "use: a greyed cell is not sent")
okl, errl = pcall(ops["ui.hotbar_use"], { spell = "Target_Haste" })
ok(not okl and tostring(errl):find("not on the hotbar", 1, true), "use: not on the hotbar")
-- the VMs carry no ids (LIVE G2): the bridge's map names the cells
local saved_ids = {}
for _, s in ipairs(class.SlotList) do saved_ids[s] = s.Content.PrototypeID; s.Content.PrototypeID = nil end
for _, s in ipairs(common.SlotList) do saved_ids[s] = s.Content.PrototypeID; s.Content.PrototypeID = nil end
r = ops["ui.hotbar_use"]({ spell = "Target_Bless", map = { ClassHotBar = { ["13"] = "Target_Bless" } } })
ok(r.done and r.index == 4 and params[3] == class.SlotList[4], "use: the map names the cell")
for s, id in pairs(saved_ids) do s.Content.PrototypeID = id end
dc.UseSlotCommand = "fail"
okl, errl = pcall(ops["ui.hotbar_use"], { spell = "Target_Bless" })
ok(not okl and tostring(errl):find("UseSlotCommand: command cannot execute now", 1, true), "use: a refused command")
dc.UseSlotCommand = nil
widgets.HotBar = savedHB21

print(string.format("%d passed, %d failed", pass, fail))
if fail > 0 then os.exit(1) end
