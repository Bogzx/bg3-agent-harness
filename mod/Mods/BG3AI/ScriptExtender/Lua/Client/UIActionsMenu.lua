-- UIActions, part 3 of 3 (UIActions.lua loads the parts in order; split for the 1000-line cap):
-- menus: load, quickload/quicksave, save.
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local A = BG3AI.UIActions
local I = A.inner
local check, try, waitJob = I.check, I.try, I.waitJob

------------------------------------------------------------------------
-- Menus
--
-- ui.menu {action, dry?}
--   open_menu / close_menu : Escape (game menu), checked against ui.state.
--   continue               : main menu "Continue" (loads the latest save).
--   load {name, owner?, index?, date?} : loads a named save (job, see MENU.load);
--                            without a name it only opens the Load page.
--   quickload              : QuickLoadCommand.
--   quicksave              : QuickSaveCommand.
--   save {name}            : named save, end to end (job, see MENU.save).
--   save_confirm {name?}   : continue a save after a shim key (needs_key).
--   Saving needs UIActions.SAVE_ENABLED, loading LOAD_ENABLED (both false:
--   the user's own save is loaded). dry=true only checks CanExecute.
------------------------------------------------------------------------

local function menuOpen()
    local w = UI.widget("GameMenu")
    return w ~= nil and UI.isVisible(w)
end

local MENU = {}

function MENU.open_menu(args)
    if menuOpen() then return { open = true } end
    if args.dry then return { can = true } end
    Ext.Input.InjectKeyPress("ESCAPE")
    return waitJob("ui.menu", 3000, function() if menuOpen() then return "done", { open = true } end end)
end

function MENU.close_menu(args)
    if not menuOpen() then return { open = false } end
    if args.dry then return { can = true } end
    Ext.Input.InjectKeyPress("ESCAPE")
    return waitJob("ui.menu", 3000, function() if not menuOpen() then return "done", { open = false } end end)
end

function MENU.continue(args)
    if not args.dry and not A.LOAD_ENABLED then error("loading is disabled (UIActions.LOAD_ENABLED=false)") end
    local dc = UI.dc("MainMenu") or error("not at the main menu")
    check(UI.command(dc, "ContinueGameCommand", nil, args.dry))
    return { done = not args.dry, can = true }
end

-- Load a named save, end to end (verified live, S001-end).
-- The Load page (widget "LoadGame", gui::DCSavegames) lists saves grouped by
-- playthrough (an Expander per character name) newest first. bg3le can't
-- read a save's title (Run.Text is not marshalled) nor pass the Load
-- button's CustomEvent parameter, so:
--   1. OpenLoadGameDialog (in game or at the main menu)
--   2. expand the playthrough(s) named `owner` (folder prefix before "-")
--   3. select a save (LSListBoxItem.IsSelected): `index` (1 = newest of that
--      playthrough) first, then, if `date` is given, verify the info pane's
--      date ("9/10/2026 22:45") against it, scanning outward from the hint
--      on mismatch (A.loadOrder)
--   4. RETURN (= UIAccept = LoadSaveBtn); a confirm box gets RETURN too
--   5. done once the page closes or the game state leaves Running. The load
--      itself resets this Lua state; the caller waits for Running again.
-- args: name (folder), owner?, index?, date? = {y, m, d, H, M} (local time).
-- The order to try a playthrough's n saves in: the hinted index first (clamped to the list), then outward from it
-- (h+1, h-1, h+2, h-2, ...). live5: a hint off by one on a 494-save page scanned from item 1 at ~0.4 s per item and
-- ran out of the 30 s job; outward, an off-by-few hint costs a few items.
function A.loadOrder(index, n)
    local h = math.max(1, math.min(index, n))
    local out = n > 0 and { h } or {}
    for d = 1, n do
        if h + d <= n then out[#out + 1] = h + d end
        if h - d >= 1 then out[#out + 1] = h - d end
    end
    return out
end

-- The playthrough groups on the load page: { {el, hdr}, ... } in display order.
function A.loadGroups(w)
    local out = {}
    for _, ex in ipairs(UI.findAll(w, function(e) return UI.typeName(e) == "Expander" end, { prune = true })) do
        local hdr = UI.allText(ex, " ", 80)
        out[#out + 1] = { el = ex, hdr = hdr and hdr:match("^%s*(.-)%s*$") or "" }
    end
    return out
end

-- L7 22:08 (acceptance helm item): "no playthrough named 'Marek Dunhallow' on the load page" right after loads from
-- Claude's group (51 saves, expanded by those loads; Marek's group is the next one down) though the same load worked
-- at 18:40. GUESS: the page's list only builds the groups in view, and an expanded group above pushes the others
-- out. A player collapses that group to see the ones under it: groups expanded and not `want` are collapsed.
-- -> the collapsed headers.
function A.collapseOthers(groups, want)
    local done = {}
    for _, g in ipairs(groups) do
        local h = g.hdr:lower()
        if not (want and (h == want or h:find(want, 1, true) == 1)) and UI.prop(g.el, "IsExpanded") == true then
            pcall(g.el.SetProperty, g.el, "IsExpanded", false)
            done[#done + 1] = g.hdr
        end
    end
    return done
end

local function loadItems(owner)
    local w = UI.widget("LoadGame")
    if not w then return nil, {} end
    local want = owner and owner:lower()
    local out, matched = {}, {}
    for _, g in ipairs(A.loadGroups(w)) do
        local ex, hdr = g.el, g.hdr
        if not want or hdr:lower() == want or hdr:lower():find(want, 1, true) == 1 then
            matched[#matched + 1] = ex
            if UI.prop(ex, "IsExpanded") ~= true then pcall(ex.SetProperty, ex, "IsExpanded", true) end
            for _, it in ipairs(UI.findAll(ex, function(e)
                return UI.typeName(e) == "ls.LSListBoxItem"
                    and UI.typeName(UI.prop(e, "DataContext")) == "ls.VMSavegame"
            end, { prune = true })) do
                out[#out + 1] = it
            end
        end
    end
    return w, out, matched
end

-- Info pane date of the selected save -> {a, b, y, H, M} or nil.
local function paneDate(w)
    local el = UI.find(w, function(e)
        return UI.name(e) == "Date" and UI.typeName(e) == "TextBlock"
    end)
    local s = el and UI.text(el)
    if not s then return nil end
    local a, b, y, H, M = s:match("(%d+)%D(%d+)%D(%d%d%d%d)%D+(%d+):(%d+)")
    if not a then return nil, s end
    H = tonumber(H)
    if s:find("[Pp]%.?[Mm]") and H < 12 then H = H + 12 end
    if s:find("[Aa]%.?[Mm]") and H == 12 then H = 0 end
    return { tonumber(a), tonumber(b), tonumber(y), H, tonumber(M) }, s
end

-- Pane date vs want {y,m,d,H,M}: either day/month order, +-1 minute.
local function dateMatches(p, want)
    if not p or not want then return false end
    if p[3] ~= tonumber(want.y) then return false end
    local m, d = tonumber(want.m), tonumber(want.d)
    if not ((p[1] == d and p[2] == m) or (p[1] == m and p[2] == d)) then return false end
    local a, b = p[4] * 60 + p[5], tonumber(want.H) * 60 + tonumber(want.M)
    return math.abs(a - b) <= 1
end

-- Live window #1 (10-16 18:34, right after the launch): load_game failed at once with "no command
-- 'OpenLoadGameDialog' on ls.LoadingScreen": the loading screen (widget "root", DC ls.LoadingScreen, see AutoSkip)
-- was still up and UI.anyDC() handed its DC out. Wait for it to go, then use a DC that has the command.
function A.loadingScreenUp()
    for _, w in ipairs(UI.widgets()) do
        if w.name == "root" or w.dc == "ls.LoadingScreen" then return true end
    end
    return false
end

-- A DataContext that can run `cmd` now: MainMenu's, else any page's but a loading screen's. nil when none.
-- Named reads (UI.get): an unknown name on GetProperty logs an error.
function A.commandDC(cmd)
    local function can(dc)
        local c = dc and UI.get(dc, cmd)
        return c ~= nil and UI.execute(c, nil, true)
    end
    local mm = UI.dc("MainMenu")
    if can(mm) then return mm end
    for _, w in ipairs(UI.widgets()) do
        local dc = w.dc ~= "ls.LoadingScreen" and w.name ~= "root" and try(function() return w.el.DataContext end) or nil
        if dc and UI.get(dc, "CurrentPlayer") ~= nil and can(dc) then return dc end
    end
    return nil
end

A.LOADING_WAIT_MS = 25000   -- the job's own limit is 30 s

function MENU.load(args)
    if not args.dry and not A.LOAD_ENABLED then error("loading is disabled (UIActions.LOAD_ENABLED=false)") end
    local name = args.name
    if args.dry or not name or name == "" then
        if not UI.widget("LoadGame") or not UI.isVisible(UI.widget("LoadGame")) then
            if A.loadingScreenUp() then error("a loading screen is up: the load page can't open yet", 0) end
            check(UI.command(A.commandDC("OpenLoadGameDialog") or UI.dc("MainMenu") or UI.anyDC(), "OpenLoadGameDialog", nil, args.dry))
        end
        return { done = not args.dry, can = true, note = (not args.dry) and "load page opened (no save named)" or nil }
    end
    local owner = args.owner or tostring(name):match("^(.-)%-%d+__") or tostring(name):match("^([^%-_]+)")
    local index = tonumber(args.index) or 1
    local want = type(args.date) == "table" and args.date or nil
    local stage, since, tries, cand = "open", RPC.now_ms(), 0, nil
    local collapsed
    local function enter(st) stage, since, tries = st, RPC.now_ms(), 0 end
    return waitJob("ui.menu.load", 30000, function(job)
        local now = RPC.now_ms()
        -- live6: a load from the Lower City timed out at "verify" with no word on how far the scan got
        job.partial = { stage = stage, name = name, picked = job.picked, tried = job.tried, saves = job.n_items,
            pane = job.pane }
        local st = tostring(try(Ext.Utils.GetGameState) or "")
        local page = UI.widget("LoadGame")
        local pageUp = page ~= nil and UI.isVisible(page)
        if stage == "open" then
            if pageUp then enter("select") return nil end
            if tries == 0 and A.loadingScreenUp() then
                -- not pressed yet: wait for the loading screen (the 4 s page timer starts after it)
                if now - since > A.LOADING_WAIT_MS then
                    return "failed", { stage = stage, loading = true,
                        error = "a loading screen stayed up " .. math.floor(A.LOADING_WAIT_MS / 1000) .. " s: the load page can't open" }
                end
                job.partial.waiting = "loading screen"
                return nil
            end
            if tries == 0 then
                tries, since = 1, now
                local dc = A.commandDC("OpenLoadGameDialog") or UI.dc("MainMenu") or UI.anyDC()
                local ok, err = UI.command(dc, "OpenLoadGameDialog", nil, false)
                if not ok then return "failed", { stage = stage, error = "could not open the load page: " .. tostring(err) } end
            elseif now - since > 4000 then
                return "failed", { stage = stage, error = "the load page did not open" }
            end
            return nil
        end
        if stage == "select" or stage == "verify" then
            if not pageUp then return "failed", { stage = stage, error = "the load page closed" } end
            local w, items, matched = loadItems(owner)
            if #matched == 0 then
                -- S025 08:35: failed 1 s after the page opened, the same save loaded on the next call: the
                -- playthrough list fills a few frames after the page shows. Wait like the save list below.
                if now - since > 1500 and not collapsed and owner then
                    collapsed = A.collapseOthers(A.loadGroups(w), owner:lower())
                    if #collapsed > 0 then since = now return nil end
                end
                if now - since > 3000 then
                    local seen = {}
                    for _, g in ipairs(A.loadGroups(w)) do if #seen < 8 then seen[#seen + 1] = g.hdr end end
                    return "failed", { stage = stage, groups = seen, collapsed = collapsed,
                        error = "no playthrough named '" .. tostring(owner) .. "' on the load page (after 3s"
                            .. (collapsed and #collapsed > 0 and ("; collapsed " .. table.concat(collapsed, ", ")) or "")
                            .. "; groups shown: " .. (#seen > 0 and table.concat(seen, " | ") or "none") .. ")" }
                end
                return nil
            end
            if #items == 0 then
                if now - since > 3000 then return "failed", { stage = stage, error = "no saves listed for '" .. tostring(owner) .. "'" } end
                return nil -- expanding: items appear on the next frames
            end
            if stage == "select" then
                if not cand then
                    cand = A.loadOrder(index, #items)
                    job.n_items = #items
                end
                local i = table.remove(cand, 1)
                if not i then return "failed", { stage = stage, error = "no save on the page matches the date of '" .. name .. "'", saves = #items } end
                pcall(items[i].SetProperty, items[i], "IsSelected", true)
                job.picked, job.tried = i, (job.tried or 0) + 1
                enter("verify")
                return nil
            end
            -- verify: the info pane follows the selection a frame or two later
            if not want then enter("accept") return nil end
            local p, raw = paneDate(w)
            job.pane = raw
            if dateMatches(p, want) then enter("accept") return nil end
            if now - since < 400 then return nil end
            enter("select")
            return nil
        end
        -- accept / confirm: RETURN until the page closes or the state changes
        if st ~= "Running" and st ~= "Menu" and st ~= "" then
            return "done", { loading = true, name = name, picked = job.picked, pane = job.pane, state = st }
        end
        local box = UI.widget("Dialog_box")
        local boxUp = box ~= nil and UI.isVisible(box)
        if not pageUp and not boxUp then
            if now - since > 300 then
                return "done", { loading = true, name = name, picked = job.picked, pane = job.pane, state = st }
            end
            return nil
        end
        if boxUp and stage ~= "confirm" then enter("confirm") end
        if now - since > 3000 then
            return "failed", { needs_key = "RETURN", stage = stage, name = name,
                error = "in-game RETURN did not load the save; send RETURN via the input shim" }
        end
        if tries < 2 and now - (job.lastKey or 0) > 1200 then
            tries = tries + 1
            job.lastKey = now
            pcall(Ext.Input.InjectKeyPress, "RETURN")
        end
        return nil
    end)
end

function MENU.quickload(args)
    if not args.dry and not A.LOAD_ENABLED then error("loading is disabled (UIActions.LOAD_ENABLED=false)") end
    check(UI.command(UI.anyDC(), "QuickLoadCommand", nil, args.dry))
    return { done = not args.dry, can = true }
end

function MENU.quicksave(args)
    if not args.dry and not A.SAVE_ENABLED then error("saving is disabled (UIActions.SAVE_ENABLED=false)") end
    check(UI.command(UI.anyDC(), "QuickSaveCommand", nil, args.dry))
    return { done = not args.dry, can = true }
end

-- Named save, end to end (untested live; written during S001).
--   1. OpenSaveGameDialogCommand            -> page "SaveGame"
--   2. RETURN = UIAccept = NewSaveBtn        -> page "SaveGamePopup"
--      (NewSaveBtn's command is CustomEvent "OpenNewSaveDialog", a string
--      parameter bg3le can't pass; with no save selected the button is
--      bound to UIAccept)
--   3. name -> LSTextBox "SaveName".Text and DC.SavegameNameInput (string
--      properties are boxed by bg3le, so no typing is needed)
--   4. RETURN = UIAccept = AcceptBtn         -> DoSaveWithName(SaveName.Text)
--   5. an overwrite prompt (Dialog_box) is confirmed
--   6. settles when SaveGamePopup/SaveGame/Dialog_box are gone and the
--      "Saving" indicator is off.
-- If an injected RETURN doesn't land within 2.5 s, the job ends with
-- {needs_key:"RETURN", stage}; the bridge sends RETURN through the input
-- shim and calls ui.menu{action:"save_confirm"} to continue.
A.pendingSave = A.pendingSave or nil

local function visible(name)
    local w = UI.widget(name)
    return w ~= nil and UI.isVisible(w), w
end

local function saveNameBox()
    local _, pop = visible("SaveGamePopup")
    if not pop then return nil, nil, "save name dialog not open" end
    local box = UI.byName(pop, "SaveName", { hidden = true })
    if not box then return nil, pop, "no SaveName box" end
    return box, pop
end

-- A string property's value: a Lua string, or a boxed value whose ToString() is the string (a class name such as
-- 'ls.LSTextBox' is not). nil = not a string.
local function unboxString(v)
    if type(v) == "string" then return v end
    if v == nil or type(v) ~= "userdata" and type(v) ~= "table" then return nil end
    local s = try(function() return v:ToString() end)
    if type(s) ~= "string" or s:match("^ls%.") or s:match("^Noesis%.") or s:match("^gui%.") then return nil end
    return s
end
A.unboxString = unboxString

-- The name box's text and where it was read (result read_via). Not UI.text: a TextBox's ToString() is its class
-- name, so S056/S057 read 'ls.LSTextBox' back and every save failed "the save name box keeps reading
-- 'ls.LSTextBox'". Then the Text property read nil on every acceptance probe (read=None, renamed=None:
-- BACKLOG "save-name probe reads None"), which also left the S052 default-name check blind. Sources, in order:
-- the box's Text (property, then proxy field), with deep=true a TextBox inside the LSTextBox's template (probe
-- only: a tree walk), then the dialog DC's SavegameNameInput (what the box is bound to). nil = unreadable.
local function boxText(box, pop, deep)
    if box == nil then return nil end
    local v = unboxString(UI.prop(box, "Text"))
    if v then return v, "text" end
    if UI.get then
        v = unboxString(UI.get(box, "Text"))
        if v then return v, "field" end
    end
    if deep and UI.findAll and UI.typeName then
        local own = UI.typeName(box)
        for _, e in ipairs(UI.findAll(box, function(e)
            local t = UI.typeName(e) or ""
            return t ~= own and t:find("TextBox", 1, true) ~= nil
        end, { hidden = true })) do
            v = unboxString(UI.prop(e, "Text"))
            if v then return v, "inner" end
        end
    end
    local dc = pop and try(function() return pop.DataContext end)
    v = dc and unboxString(UI.prop(dc, "SavegameNameInput"))
    if v then return v, "dc" end
    return nil
end

local function saveNameNow(deep)
    local box, pop = saveNameBox()
    return boxText(box, pop, deep)
end

local function setSaveName(name)
    local box, pop, why = saveNameBox()
    if not box then return false, why end
    local ok = pcall(box.SetProperty, box, "Text", name)
    local dc = try(function() return pop.DataContext end)
    if dc then pcall(dc.SetProperty, dc, "SavegameNameInput", name) end
    local now = boxText(box, pop)
    if not ok then return false, "could not set the name" end
    return true, now
end

-- S052 05:21:17: 'S052-lab-fight2' was written as '…__Steel Watch Foundry - Lab Level - 16h 14m': the name was set
-- once when the dialog appeared and RETURN followed; the dialog filled in its default name after us. Before each
-- RETURN the box must read our name (set again otherwise) and have held it SAVE_NAME_SETTLE_MS.
A.SAVE_NAME_SETTLE_MS = 150
A.SAVE_NAME_TRIES = 5

local function savingIndicator()
    local w = UI.widget("AlwaysOnTopOverlay")
    local panel = w and UI.byName(w, "SavingPanel", { hidden = true })
    return panel ~= nil and UI.isVisible(panel)
end

-- The client's own Save state (~1 s: the game is frozen meanwhile) is when a
-- save really happens: it starts 0.3-1 s after the name is accepted (live
-- S015/S017). The driver is done once the game has been through it and is
-- back; the SavingPanel overlay lingers ~3 s past it (S015: menu_s 4.5 s), and
-- finishing before it (S017: 0.1 s before) let the agent act into the freeze.
-- Seen from GameStateChanged (the job may not tick during Save) and polled.
A.SAVE_STATE_WAIT_MS = 3000  -- no Save state this long after the accept: done anyway
A.saveState = { started = 0, ended = 0 }
local function clientState()
    return tostring(try(Ext.Utils.GetGameState) or "")
end
pcall(function()
    Ext.Events.GameStateChanged:Subscribe(function(e)
        local from, to = tostring(e.FromState), tostring(e.ToState)
        if to == "Save" then A.saveState.started = RPC.now_ms() end
        if from == "Save" then A.saveState.ended = RPC.now_ms() end
    end)
end)

-- opts.probe: name check only (acceptance): open, set the name, read the box back after the settle, then ESCAPE
-- out without accepting; nothing is written. Result {probe=true, read=<box text>, matches=<read == name>}.
-- opts.final: the bridge's continuation after its input-shim key (save_confirm): a key that still doesn't advance
-- ends the save, its menus closed.
local function saveDriver(name, kind, opts)
    local probe = opts and opts.probe
    local final = opts and opts.final
    if not probe then A.pendingSave = { name = name, started = RPC.now_ms() } end
    local stage, stageSince, keyTries = nil, RPC.now_ms(), 0
    local t0 = RPC.now_ms()
    local ms = {}  -- stage -> ms since start (returned as result.ms: where a save's time goes)
    local function enter(st)
        if st ~= stage then
            stage, stageSince, keyTries = st, RPC.now_ms(), 0
            if ms[st] == nil then ms[st] = stageSince - t0 end
        end
    end
    return waitJob(kind, 30000, function(job)
        local now = RPC.now_ms()
        local popup = visible("SaveGamePopup")
        local page = visible("SaveGame")
        local box = visible("Dialog_box")
        job.partial = { stage = stage, name = name }
        local function esc()
            if now - (job.lastEsc or 0) > 900 then
                job.lastEsc = now
                job.escTries = (job.escTries or 0) + 1
                pcall(Ext.Input.InjectKeyPress, "ESCAPE")
            end
            if (job.escTries or 0) > 6 then
                if job.failRes then
                    job.failRes.error = job.failRes.error .. " (and the save menu did not close)"
                    return "failed", job.failRes
                end
                return "failed", { probe = true, stage = stage, read = job.probeRead, error = "the save menu did not close" }
            end
            return nil
        end
        -- S056 07:17: a failed name check left the save page open; the agent finished it by hand (ui_dump, RETURN
        -- x3) and the save landed later anyway. A failure closes our menus first, then reports.
        local function failClosed(res)
            job.failRes = res
            enter("fail_close")
            return esc()
        end
        if stage == "fail_close" then
            if popup or page or visible("GameMenu") then return esc() end
            A.pendingSave = nil
            return "failed", job.failRes
        end
        if stage == "probe_close" then
            if popup or page or visible("GameMenu") then return esc() end
            return "done", { probe = true, name = name, read = job.probeRead, read_via = job.probeVia,
                matches = job.probeRead == name, renamed = job.renamed, ms = ms }
        end
        local function key(k)
            -- one in-game RETURN per stage (retried once); then ask the bridge
            if now - stageSince > 2500 then
                local why = "in-game " .. k .. " did not advance '" .. tostring(stage) .. "'"
                -- S096 11:36:30: the Emperor's conversation took the screen; the shim's RETURN didn't advance it
                -- either, and SAVE FAILED left the SaveGame page open behind the talk (panels_open=[SaveGame]).
                if final then
                    return failClosed({ stage = stage, name = name, error = why .. ", nor did the input shim's" })
                end
                return "failed", { needs_key = k, stage = stage, name = name,
                    error = why .. "; send it via the input shim, then ui.menu{action:'save_confirm'}" }
            end
            if keyTries < 2 and now - (job.lastKey or 0) > 900 then
                keyTries = keyTries + 1
                job.lastKey = now
                pcall(Ext.Input.InjectKeyPress, k)
            end
        end
        -- Audit UI-6: the "new" RETURN relies on no save being selected (RETURN = UIAccept = NewSaveBtn). With a
        -- row selected it acts on that save, and the box that comes up is its overwrite (or load/delete) question,
        -- not ours: the next "new" RETURN would confirm it. A box before our name was accepted is answered No and
        -- the save fails, closed; only accept/overwrite confirm a box.
        if box and (stage == nil or stage == "open" or stage == "new") and not job.foreignBox then
            job.foreignBox = true
            local w = UI.widget("Dialog_box")
            local btn = UI.findButton(w, "no") or UI.findButton(w, "cancel") or UI.findButton(w, "back")
            if btn then A.pressButton(btn, false) end
            return failClosed({ stage = stage or "open", name = name, foreign_box = true,
                error = "a confirmation box came up before the save name (a save row was selected: RETURN acted "
                    .. "on it, e.g. an overwrite); answered No, nothing saved" })
        end
        if box and (stage == "accept" or stage == "overwrite") then
            enter("overwrite")
            -- "overwrite existing save?" -> the confirming button
            local w = UI.widget("Dialog_box")
            local btn = UI.findButton(w, "overwrite") or UI.findButton(w, "yes") or UI.findButton(w, "save")
                or UI.findButton(w, "ok") or UI.findButton(w, "confirm")
            if btn and keyTries == 0 then
                keyTries = 1
                local ok = A.pressButton(btn, false)
                if ok then return nil end
            end
            return key("RETURN")
        end
        if popup then
            if stage ~= "accept" then
                local ok, err = setSaveName(name)
                if not ok then return failClosed({ stage = "name", error = err }) end
                job.typed = err
                job.nameAt, job.nameSets = now, 1
                enter("accept")
                return nil
            end
            local cur, via = saveNameNow()
            if cur ~= nil and cur ~= name then
                if job.nameSets >= A.SAVE_NAME_TRIES then
                    return failClosed({ stage = "name", name = name, typed = cur,
                        error = "the save name box keeps reading '" .. tostring(cur) .. "' instead of '" .. name .. "'" })
                end
                setSaveName(name)
                job.nameAt, job.nameSets = now, job.nameSets + 1
                job.renamed = (job.renamed or 0) + 1
                stageSince = now   -- the RETURN timeout counts from the last good name
                return nil
            end
            if now - job.nameAt < A.SAVE_NAME_SETTLE_MS then return nil end
            if probe then
                job.probeRead, job.probeVia = saveNameNow(true)
                enter("probe_close")
                return esc()
            end
            if via ~= "text" and via ~= "field" and now - (job.blindAt or 0) > 800 then
                -- the box itself can't be read (the DC may still hold our name under a default the dialog wrote
                -- into the box, S052): set it again right before each RETURN
                job.blindAt = now
                setSaveName(name)
                job.blind = (job.blind or 0) + 1
            end
            return key("RETURN")
        end
        if page then
            if stage == "accept" or stage == "overwrite" or stage == "done" then
                -- back on the list after saving: close it
                enter("close")
                return key("ESCAPE")
            end
            enter("new")
            return key("RETURN")
        end
        -- no save UI visible
        if stage == nil then
            local ok, err = UI.command(UI.anyDC(), "OpenSaveGameDialogCommand", nil, false)
            if not ok then return "failed", { stage = "open", error = err } end
            enter("open")
            return nil
        end
        if stage == "open" then
            if now - stageSince > 4000 then return "failed", { stage = "open", error = "save page did not open" } end
            return nil
        end
        -- accept/overwrite/close passed and nothing is open: wait for the
        -- game's Save state to come and go (see A.saveState). The write itself
        -- is confirmed by the bridge on disk, in the background (S009: this
        -- floor was 600 ms of ~1.2 s; 250 ms still lets the game menu come
        -- back between pages).
        if clientState() == "Save" then
            job.sawSave = true
            enter("saving")
            return nil
        end
        local accepted = t0 + (ms.accept or ms.overwrite or (now - t0))
        local saved = job.sawSave or A.saveState.ended >= t0
        if not saved then
            if now - accepted < A.SAVE_STATE_WAIT_MS then
                if savingIndicator() then enter("saving") end
                return nil
            end
            job.noSaveState = true
            if ms.no_save_state == nil then ms.no_save_state = now - t0 end
        elseif ms.saved == nil then
            ms.saved = now - t0
        end
        if now - stageSince < 250 then return nil end
        -- leave the game menu (the save page returns to it) so the game is back
        if visible("GameMenu") then
            if not job.menuEsc or (now - job.menuEsc > 1200 and (job.menuTries or 0) < 2) then
                job.menuEsc = now
                job.menuTries = (job.menuTries or 0) + 1
                pcall(Ext.Input.InjectKeyPress, "ESCAPE")
            end
            return nil
        end
        A.pendingSave = nil
        ms.done = now - t0
        return "done", { saved = true, name = name, typed = job.typed, ms = ms,
            no_save_state = job.noSaveState, renamed = job.renamed, blind_sets = job.blind }
    end)
end
A.saveDriver = saveDriver

function MENU.save(args)
    if not args.dry and not args.probe and not A.SAVE_ENABLED then error("saving is disabled (UIActions.SAVE_ENABLED=false)") end
    local name = args.name
    if args.dry then
        check(UI.command(UI.anyDC(), "OpenSaveGameDialogCommand", nil, true))
        return { can = true }
    end
    if args.probe then
        return saveDriver(tostring(name or "probe"):gsub("[^%w%-_ ]", "_"):sub(1, 40), "ui.menu.save", { probe = true })
    end
    if not name or name == "" then
        check(UI.command(UI.anyDC(), "OpenSaveGameDialogCommand", nil, false))
        return { done = true, note = "save page opened (no name given)" }
    end
    name = tostring(name):gsub("[^%w%-_ ]", "_"):sub(1, 40)
    return saveDriver(name, "ui.menu.save")
end

-- Continue a named save after the bridge sent a key via the input shim.
function MENU.save_confirm(args)
    if not A.SAVE_ENABLED then error("saving is disabled (UIActions.SAVE_ENABLED=false)") end
    local name = args.name or (A.pendingSave and A.pendingSave.name)
    if not name then error("no pending save; call ui.menu{action:'save', name}") end
    return saveDriver(name, "ui.menu.save", { final = true })
end

RPC.register("ui.menu", function(args)
    local fn = MENU[args.action or ""]
    if not fn then
        local names = {}
        for k in pairs(MENU) do names[#names + 1] = k end
        table.sort(names)
        error("action must be one of " .. table.concat(names, ", "))
    end
    return fn(args)
end)
