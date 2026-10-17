-- Offline tests for Client/Spellbook.lua (A5 prepare_spells, client half): open the spellbook page, toggle the
-- cells by their own command, wait for each to flip, close the page; the refusals it makes itself; the probe.
-- UI, Ext and RPC are stubbed with plain tables; the job's on_tick is driven by hand with a fake clock.
-- Run: tools/dev/test_spellbook.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"
local pass, fail = 0, 0
local function ok(cond, name)
    if cond then pass = pass + 1; print("ok   " .. name) else fail = fail + 1; print("FAIL " .. name) end
end

local ops, jobs, keys = {}, {}, {}
local clock = 0
local RPC = { register = function(n, f) ops[n] = f end, now_ms = function() return clock end }
function RPC.start_job(kind, spec)
    local job = spec or {}
    job.id, job.kind, job.status = #jobs + 1, kind, "running"
    jobs[job.id] = job
    return job
end
function RPC.finish_job(job, status, result, err)
    if job.status ~= "running" then return end
    job.status, job.result, job.error = status, result, err
end
BG3AI = { RPC = RPC }

-- Collections: Lua arrays. Objects: tables.
local panel = { Name = "CharacterPanel", IsVisible = false, Metadata = "ToggleInventory" }
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
function UI.isVisible(e) return e.IsVisible ~= false end
function UI.widgets() return { { name = "HotBar", el = { IsVisible = true } }, { name = "CharacterPanel", el = panel } } end
function UI.typeName(o) return type(o) == "table" and o.Type or nil end
function UI.props(o)
    local out = {}
    if type(o) ~= "table" then return out end
    for k, v in pairs(o) do if type(v) ~= "table" and type(v) ~= "function" then out[k] = v end end
    return out
end
local toggles = {}
local stuck = {}   -- names whose toggle does nothing
function UI.command(o, name, param)
    if type(o) ~= "table" or o[name] == nil then return false, "no command '" .. name .. "'" end
    toggles[#toggles + 1] = param.Name or param._tag
    if not stuck[param.Name or param._tag] then
        param.Activated = not param.Activated
        local stat = o.book.PreparedSpellsStat
        stat.Value = stat.Value + (param.Activated and 1 or -1)
    end
    return true
end

Ext = { Input = { InjectKeyPress = function(k) keys[#keys + 1] = k
    -- the ToggleSpells key: closed -> open on the spellbook; open on another page -> switch; on spells -> close
    if not panel.IsVisible then panel.IsVisible, panel.Metadata = true, "ToggleSpells"
    elseif panel.Metadata ~= "ToggleSpells" then panel.Metadata = "ToggleSpells"
    else panel.IsVisible = false end
end } }

dofile(LUA .. "/Client/Spellbook.lua")
local SB = BG3AI.Spellbook

local function cell(name, on) return { Name = name, Activated = on, NeedsActivation = true } end
local bless, cure, shield, command = cell("Bless", false), cell("Cure Wounds", true), cell("Shield of Faith", true),
    cell("Command", false)
local book = { Class = { MustPrepareSpells = true, ClassDisplayName = "Paladin" },
               PreparedSpellsStat = { Value = 2, Max = 3 },
               ActionGroups = { { Actions = { bless, cure, shield, command } } },
               ManuallyPreparedActions = { cure, shield } }
local oath = { Class = { MustPrepareSpells = false }, PreparedSpellsStat = { Value = 0, Max = 0 },
               ActionGroups = { { Actions = { cell("Smite", true) } } } }
local dc = { Type = "gui::DCCharacterPanel", ToggleCharacterAction = true, book = book,
             CurrentPlayer = { SelectedCharacter = { Name = "Claude", PlayerCharacterProperties = { SpellBooks = { oath, book } } } } }
panel.DataContext = dc

local function run(args, ticks)
    local r = ops["ui.prepare_spells"](args)
    local job = jobs[r.job]
    for _ = 1, ticks or 60 do
        if job.status ~= "running" then break end
        clock = clock + 50
        job.on_tick(job)
    end
    return job
end
local function reset()
    panel.IsVisible, panel.Metadata = false, "ToggleInventory"
    keys, toggles = {}, {}
end

-- 1. closed panel: K opens it, Bless is prepared by its cell's command, K closes it again
reset()
local j = run({ name = "Claude", add = { { id = "Target_Bless", name = "Bless" } } })
ok(j.status == "done", "prepare Bless: done (" .. tostring(j.error) .. ")")
ok(bless.Activated == true and toggles[1] == "Bless" and #toggles == 1, "Bless toggled once by ToggleCharacterAction")
ok(j.result.toggled[1].ok == true and j.result.toggled[1].op == "add", "result: Bless add ok")
ok(j.result.before.value == 2 and j.result.after.value == 3 and j.result.after.max == 3, "stat before 2/3 after 3/3")
ok(j.result.book == "Paladin" and j.result.shows == "Claude", "the preparing book (not the oath one), Claude shown")
ok(#keys == 2 and keys[1] == "K" and keys[2] == "K" and j.result.opened and j.result.closed and not panel.IsVisible,
   "opened with K and closed with K")

-- 2. full book: an add the UI can't take is refused by the op itself; removes run first and free the slot
reset()
j = run({ name = "Claude", add = { { name = "Command" } } })
ok(j.status == "done" and j.result.toggled[1].ok == false and j.result.toggled[1].why:find("full %(3/3"),
   "full book: Command refused (" .. tostring(j.result.toggled[1].why) .. ")")
ok(command.Activated == false and #toggles == 0, "nothing clicked when full")
reset()
j = run({ name = "Claude", remove = { { name = "Bless" } }, add = { { name = "Command" } } })
ok(j.status == "done" and toggles[1] == "Bless" and toggles[2] == "Command", "remove first, then add")
ok(bless.Activated == false and command.Activated == true and book.PreparedSpellsStat.Value == 3, "swap applied")

-- 3. a cell that doesn't flip, a missing cell, an already-prepared one
reset()
stuck["Bless"] = true
j = run({ name = "Claude", remove = { { name = "Command" } }, add = { { name = "Bless" }, { name = "Fireball" }, { name = "cure wounds" } } })
stuck["Bless"] = nil
local by = {}
for _, t in ipairs(j.result.toggled) do by[t.name] = t end
ok(by.Command.ok == true, "Command removed")
ok(by.Bless.ok == false and by.Bless.why:find("didn't change"), "stuck cell: clicked, no change")
ok(by.Fireball.ok == false and by.Fireball.why:find("no spell cell named Fireball"), "missing cell named")
ok(by["cure wounds"].ok == true and by["cure wounds"].why == "already prepared", "already prepared: no click")
ok(j.status == "done" and not panel.IsVisible, "still closes the page")

-- 4. the page already open on the spellbook: no key, left open; another character shown: refused
reset()
panel.IsVisible, panel.Metadata = true, "ToggleSpells"
j = run({ name = "Claude", remove = { { name = "Bless" } } })
ok(j.status == "done" and #keys == 0 and panel.IsVisible and j.result.opened == false, "open page: used as is, left open")
reset()
j = run({ name = "Shadowheart", add = { { name = "Bless" } } })
ok(j.status == "failed" and j.error:find("shows Claude, not Shadowheart") and not panel.IsVisible,
   "wrong character shown: failed, page closed (" .. tostring(j.error) .. ")")

-- 5. open on the inventory page: K switches to the spellbook; the panel was open before, so it stays open
reset()
panel.IsVisible, panel.Metadata = true, "ToggleInventory"
j = run({ name = "Claude", add = { { name = "Bless" } } })
ok(j.status == "done" and #keys == 1 and panel.Metadata == "ToggleSpells" and panel.IsVisible,
   "inventory page: switched to the spellbook, left open")

-- 5b. the page opens but reads the old Metadata for a while: no 2nd K (it would close the page)
reset()
Ext.Input.InjectKeyPress = (function(inner) return function(k)
    inner(k)
    if panel.IsVisible then panel.Metadata = "ToggleInventory" end   -- lags behind
end end)(Ext.Input.InjectKeyPress)
local lagged = run({ name = "Claude", add = { { name = "Bless" } } }, 20)
ok(#keys == 1 and panel.IsVisible, "lagging Metadata: one K, the page stays open")
panel.Metadata = "ToggleSpells"
for _ = 1, 40 do if lagged.status ~= "running" then break end clock = clock + 50 lagged.on_tick(lagged) end
ok(lagged.status == "done", "then it reads the page once Metadata catches up")
Ext.Input.InjectKeyPress = function(k) keys[#keys + 1] = k
    if not panel.IsVisible then panel.IsVisible, panel.Metadata = true, "ToggleSpells"
    elseif panel.Metadata ~= "ToggleSpells" then panel.Metadata = "ToggleSpells"
    else panel.IsVisible = false end
end

-- 6. the page never opens
reset()
local inject = Ext.Input.InjectKeyPress
Ext.Input.InjectKeyPress = function(k) keys[#keys + 1] = k end
j = run({ name = "Claude", add = { { name = "Bless" } } }, 100)
Ext.Input.InjectKeyPress = inject
ok(j.status == "failed" and j.error:find("didn't open") and #keys == 1, "no page: failed after one K")

-- 7. probe: read-only, names the books, cells and the command's CanExecute
reset()
dc.ToggleCharacterAction = { CanExecute = function(_, vm) return vm.NeedsActivation == true end }
j = run({ name = "Claude", probe = true })
local p = j.result.probe
ok(j.status == "done" and #toggles == 0 and not panel.IsVisible, "probe: no toggles, page closed")
ok(p.toggle_cmd == true and p.selected == "Claude" and #p.books == 2 and p.books[2].stat.Max == 3, "probe: books + stat")
ok(p.books[2].groups[1].cells[1].props.Name == "Bless" and p.books[2].groups[1].cells[1].can_toggle == true,
   "probe: cell props + CanExecute")
ok(#p.preparing == 1 and p.preparing[1].class == "Paladin", "probe: one preparing book summarised")

-- 8. live L4 shape (A7): the cells carry no Name (ActionId "Sentinel"); matched by their look in the state the server
-- says the spell is in. Bless (off) and Heroism (on) look the same: level 1 Enchantment, concentration, Action.
local function nameless(tag, lvl, school, conc, cost, on, needs)
    return { _tag = tag, ActionId = "Sentinel", SpellSlotLevel = lvl, SpellSchool = school, IsConcentrationSpell = conc,
             MainCost = cost, Activated = on, NeedsActivation = needs ~= false }
end
local pfe = nameless("PfEaG", 1, "Abjuration", true, "Action", true, false)
local nbless = nameless("Bless", 1, "Enchantment", true, "Action", false)
local hero = nameless("Heroism", 1, "Enchantment", true, "Action", true)
local duel = nameless("CompelledDuel", 1, "Enchantment", true, "BonusAction", true)
local ncmd = nameless("Command", 1, "Enchantment", false, "Action", true)
local blind = nameless("BlindingSmite", 3, "Evocation", false, "BothActions", true)
local lbook = { Class = { MustPrepareSpells = true }, PreparedSpellsStat = { Value = 4, Max = 12 },
                ActionGroups = { {}, { Actions = { pfe, ncmd, duel, nbless, hero } }, {}, { Actions = { blind } } },
                ManuallyPreparedActions = { ncmd, duel, hero, blind } }
local base = { Class = {}, PreparedSpellsStat = { Value = -1, Max = -1 },
               ActionGroups = { { Actions = { nameless("ItemSpell", 1, "Enchantment", true, "Action", false, false) } } } }
dc.book = lbook
dc.CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.SpellBooks = { base, {}, lbook }
local BLESS = { id = "Target_Bless", name = "Bless", level = 1, twins = 0,
                look = { icon = "Spell_Enchantment_Bless", school = "Enchantment", conc = true, cost = "Action" } }
local HERO = { id = "Target_Heroism", name = "Heroism", level = 1, twins = 0,
               look = { icon = "Spell_Enchantment_Heroism", school = "Enchantment", conc = true, cost = "Action" } }
reset()
j = run({ name = "Claude", add = { BLESS } })
ok(j.status == "done" and #toggles == 1 and toggles[1] == "Bless" and nbless.Activated == true and hero.Activated,
   "nameless cells: Bless's cell by its look among the unprepared ones, not Heroism (" .. tostring(toggles[1]) .. ")")
ok(j.result.before.value == 4 and j.result.after.value == 5 and j.result.after.max == 12, "page stat 4/12 -> 5/12")
-- removing Bless while Heroism (its look-alike) is prepared too: the server sends twins = 1 -> refused
reset()
j = run({ name = "Claude", remove = { { id = "Target_Bless", name = "Bless", level = 1, twins = 1, look = BLESS.look } } })
ok(#toggles == 0 and j.result.toggled[1].why:find("1 other spell%(s%) look the same as Bless"),
   "remove with a prepared look-alike: refused (" .. tostring(j.result.toggled[1].why) .. ")")
reset()
nbless.Activated = false
j = run({ name = "Claude", remove = { HERO } })
ok(#toggles == 1 and toggles[1] == "Heroism" and hero.Activated == false, "remove Heroism: the prepared look-alike")
hero.Activated, lbook.PreparedSpellsStat.Value = true, 4
-- twins: the server says another unprepared spell looks the same -> refused, nothing clicked
reset()
j = run({ name = "Claude", add = { { id = "Target_Bane", name = "Bane", level = 1, twins = 1, look = BLESS.look } } })
ok(#toggles == 0 and j.result.toggled[1].ok == false and j.result.toggled[1].why:find("can't tell its cell apart"),
   "twins: refused, nothing clicked (" .. tostring(j.result.toggled[1].why) .. ")")
-- the page shows two look-alikes the server didn't expect: refused
reset()
local extra = nameless("Bane", 1, "Enchantment", true, "Action", false)
lbook.ActionGroups[2].Actions[6] = extra
j = run({ name = "Claude", add = { BLESS } })
ok(#toggles == 0 and j.result.toggled[1].why:find("2 cells look like Bless"), "2 look-alike cells: refused")
lbook.ActionGroups[2].Actions[6] = nil
-- an always-prepared cell is never a candidate; a wrong level isn't either
reset()
j = run({ name = "Claude", add = { { id = "Target_PfE", name = "Protection", level = 1, twins = 0,
                                      look = { school = "Abjuration", conc = true, cost = "Action" } } } })
ok(#toggles == 0 and j.result.toggled[1].why:find("no spell cell like Protection"), "always-prepared cell skipped")
-- icons readable (GUESS S4): the icon decides, even with twins
reset()
nbless.Icon = { ImageSource = { UriSource = "Assets/Icons/Spell_Enchantment_Bless.png" } }
hero.Icon = { ImageSource = { UriSource = "Assets/Icons/Spell_Enchantment_Heroism.png" } }
extra.Icon = { ImageSource = { UriSource = "Assets/Icons/Spell_Enchantment_Bane.png" } }
lbook.ActionGroups[2].Actions[6] = extra
j = run({ name = "Claude", add = { { id = "Target_Bane", name = "Bane", level = 1, twins = 1,
                                      look = { icon = "Spell_Enchantment_Bane", school = "Enchantment", conc = true,
                                               cost = "Action" } } } })
ok(#toggles == 1 and toggles[1] == "Bane" and extra.Activated, "icon read: Bane's cell by its icon despite twins")
ok(SB.iconOf(nbless) == "Spell_Enchantment_Bless", "iconOf strips the path and extension")
-- probe: one compact line per cell
reset()
j = run({ name = "Claude", probe = true })
local lines = j.result.probe.preparing[1].cells
ok(#lines == 7 and lines[1] == "L1 Abjuration conc Action on always" and
   lines[3] == "L1 Enchantment conc BonusAction on", "probe: a look line per cell (" .. tostring(lines[3]) .. ")")
ok(j.result.probe.books[3].groups[2].cells[4].icon.name == "Spell_Enchantment_Bless", "probe: the icon read per cell")

print(string.format("%d passed, %d failed", pass, fail))
if fail > 0 then os.exit(1) end
