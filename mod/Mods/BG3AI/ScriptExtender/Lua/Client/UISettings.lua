-- Game settings, reaction preferences and popup dismissal for AI play
-- (owner: worker "io").
--
-- ui.settings {key=value, ...} -> {settings}
--   Writes whitelisted engine switches (Ext.Utils.GetGlobalSwitches(), the
--   values the Options menu edits) and their UI mirrors (CurrentPlayer.UIOptions).
--   The values persist in bg3ai/config.json ("settings") and are re-applied at
--   bootstrap and whenever the game reaches Running, so the user's own
--   profile options file is never written. No args: returns current values.
--   Works at the main menu and in game (the switches are global).
--   The Options pages can't be driven directly on bg3le: they open via
--   CustomEvent "OpenOptions" (a string parameter bg3le can't pass) and
--   Ext.UI.SetState is a deprecated no-op there.
--
-- ui.reactions {char, set?:{<name or id>: "never"|"always"|"ask"}}
--   -> {char, reactions:[{id, name, mode}]}
--   Reaction prompts per character (the character sheet's Reactions toggles:
--   InterruptToggleDisableCommand / InterruptChangeAskCommand(VMInterrupt) on
--   the shared DC). Works on the selected character (ClientControl); select
--   it first with act.select. "never" = disabled, "always" = enabled without
--   asking, "ask" = prompt.
--
-- ui.dismiss {dry?} -> {closed:[{page, how, title?}], decisions?:[...]}
--   Closes non-decision popups: tutorials, info message boxes (single
--   button), reward panels, book/examine panels, the Larian sign-up panel.
--   Decision popups (message boxes with 2+ buttons) are never touched; they
--   are returned under "decisions" and reported by ui.state as
--   popup.decision=true, blocking=true; answer them with ui.press.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local S = BG3AI.UIState
local A = BG3AI.UIActions
local G = BG3AI.UISettings or {}
BG3AI.UISettings = G

local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
    return nil
end

------------------------------------------------------------------------
-- Settings
------------------------------------------------------------------------

-- agent name -> { switch = GlobalSwitches field, ui = UIOptions mirror?, kind, min?, max? }
G.SETTINGS = {
    show_tutorials       = { switch = "ShowTutorials", kind = "boolean" },
    subtitles            = { switch = "ShowSubtitles", ui = "ShowSubtitles", kind = "boolean" },
    display_speaker      = { switch = "DisplaySpeaker", ui = "DisplaySpeaker", kind = "boolean" },
    skip_larian_signup   = { switch = "SkipLarianSignUp", kind = "boolean" },
    skip_splash          = { switch = "SkipSplashScreen", kind = "boolean" },
    dialogue_wait        = { switch = "NodeWaitTimeMultiplier", kind = "number", min = 0.1, max = 3 },
    auto_continue        = { switch = "TimelinesAutoContinue", kind = "boolean" },
    hold_to_end_turn     = { switch = "HoldToEndTurn", ui = "HoldToEndTurn", kind = "boolean" },
    camera_shake         = { switch = "CameraShakeEnabled", kind = "boolean" },
    closeup_dialog_camera = { switch = "GameCameraEnableCloseUpDialog", kind = "boolean" },
    camera_auto_rotate   = { switch = "GameCameraAutoRotateEnabled", kind = "boolean" },
    combat_feed          = { switch = "ShowCombatFeed", ui = "ShowCombatFeed", kind = "boolean" },
    text_background      = { switch = "ShowTextBackground", ui = "ShowTextBackground", kind = "boolean" },
    north_facing_minimap = { switch = "NorthFacingMinimap", ui = "NorthFacingMinimap", kind = "boolean" },
    mute_unfocused       = { switch = "MuteSoundWhenNotFocused", kind = "boolean" },
}

local function switches()
    return try(Ext.Utils.GetGlobalSwitches)
end

local function uiOptions()
    local dc = UI.anyDC()
    return dc and UI.path(dc, "CurrentPlayer.UIOptions") or nil
end

function G.current()
    local sw = switches()
    local out = {}
    if not sw then return out end
    for name, def in pairs(G.SETTINGS) do
        local v = try(function() return sw[def.switch] end)
        if v ~= nil then out[name] = v end
    end
    return out
end

-- Apply one setting; returns ok, err.
function G.apply(name, value)
    local def = G.SETTINGS[name]
    if not def then return false, "unknown setting '" .. tostring(name) .. "'" end
    if def.kind == "boolean" then
        if type(value) ~= "boolean" then return false, name .. " must be true/false" end
    else
        value = tonumber(value)
        if not value then return false, name .. " must be a number" end
        if def.min and value < def.min then value = def.min end
        if def.max and value > def.max then value = def.max end
    end
    local sw = switches()
    if not sw then return false, "global switches unavailable" end
    local ok, err = pcall(function() sw[def.switch] = value end)
    if not ok then return false, "cannot set " .. def.switch .. ": " .. tostring(err) end
    if def.ui then
        local o = uiOptions()
        if o then pcall(o.SetProperty, o, def.ui, value) end
    end
    return true
end

function G.applySaved()
    local cfg = A and A.config or {}
    local n = 0
    for name, value in pairs(cfg.settings or {}) do
        if G.apply(name, value) then n = n + 1 end
    end
    return n
end

RPC.register("ui.settings", function(args)
    local cfg = A.config or {}
    cfg.settings = cfg.settings or {}
    local errors, changed = {}, false
    for name, value in pairs(args) do
        if name ~= "dry" then
            local ok, err = G.apply(name, value)
            if ok then
                cfg.settings[name] = value
                changed = true
            else
                errors[#errors + 1] = err
            end
        end
    end
    if changed then
        A.config = cfg
        local ok, err = pcall(Ext.IO.SaveFile, A.CONFIG_PATH, Ext.Json.Stringify(cfg))
        if not ok or err == false then errors[#errors + 1] = "could not persist: " .. tostring(err) end
    end
    local names = {}
    for k in pairs(G.SETTINGS) do names[#names + 1] = k end
    table.sort(names)
    return { settings = G.current(), errors = #errors > 0 and errors or nil, available = args.list and names or nil }
end)

------------------------------------------------------------------------
-- Reactions
------------------------------------------------------------------------

local function controlledUuid()
    local l = try(Ext.Entity.GetAllEntitiesWithComponent, "ClientControl") or {}
    local e = l[1]
    return e and try(function() return e.Uuid.EntityUuid end) or nil
end

-- Interrupt ids in InterruptContainer order, with their display names.
local function interruptIds(uuid)
    local e = try(Ext.Entity.Get, uuid)
    local out = {}
    local list = e and try(function() return e.InterruptContainer.Interrupts end) or {}
    for _, ie in ipairs(list) do
        local id = try(function() return ie.InterruptData.Interrupt end)
        local name
        local st = id and try(Ext.Stats.Get, id)
        if st then name = BG3AI.Util.loca(try(function() return st.DisplayName end)) end
        out[#out + 1] = { id = id, name = name or id }
    end
    return out
end

local function modeOf(vm)
    local p = UI.props(vm)
    if p.Enabled == false then return "never" end
    if p.Ask == true then return "ask" end
    return "always"
end

local function reactionList(uuid)
    local dc = UI.anyDC() or error("UI not ready")
    local vms = UI.path(dc, "CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.Interrupts")
    local ids = interruptIds(uuid)
    local out = {}
    for i, vm in UI.items(vms, 64) do
        local info = ids[i] or {}
        out[#out + 1] = { i = i, id = info.id, name = info.name, mode = modeOf(vm), vm = vm }
    end
    return out, dc, #ids == UI.count(vms)
end

RPC.register("ui.reactions", function(args)
    local sel = controlledUuid()
    local uuid = args.char or sel
    if uuid ~= sel then error("char is not the selected character; select it first (act.select)") end
    local list, dc, aligned = reactionList(uuid)
    local function view(l)
        local out = {}
        for _, x in ipairs(l) do out[#out + 1] = { id = x.id, name = x.name, mode = x.mode } end
        return out
    end
    if args.set then
        local targets = {}
        if not aligned then error("reaction list and interrupt ids differ in length; refusing to guess") end
        for key, want in pairs(args.set) do
            if want ~= "never" and want ~= "always" and want ~= "ask" then error("mode must be never/always/ask") end
            local k = tostring(key):lower()
            local r
            for _, x in ipairs(list) do
                if (x.id and x.id:lower() == k) or (x.name and x.name:lower() == k) then r = x break end
            end
            if not r then error("no reaction '" .. tostring(key) .. "'") end
            targets[r.i] = want
            if r.mode == want then goto continue end
            -- Enabled first, then Ask (Ask only matters while enabled).
            local wantEnabled = want ~= "never"
            local p = UI.props(r.vm)
            if (p.Enabled ~= false) ~= wantEnabled then
                assert(UI.command(dc, "InterruptToggleDisableCommand", r.vm, args.dry))
            end
            if wantEnabled and (p.Ask == true) ~= (want == "ask") then
                assert(UI.command(dc, "InterruptChangeAskCommand", r.vm, args.dry))
            end
            ::continue::
        end
        if not args.dry then
            -- The commands are deferred (applied a frame or two later): settle
            -- when every target mode is reached.
            return A.waitJob("ui.reactions", 3000, function(job)
                local now = reactionList(uuid)
                job.partial = { char = uuid, reactions = view(now) }
                for i, want in pairs(targets) do
                    if not now[i] or now[i].mode ~= want then return nil end
                end
                return "done", { char = uuid, reactions = view(now) }
            end)
        end
    end
    return { char = uuid, reactions = view(list) }
end)

------------------------------------------------------------------------
-- Popups
------------------------------------------------------------------------

-- Is a message box a decision (2+ buttons) or info (0-1 button)?
function G.isDecision(popup)
    return popup ~= nil and popup.buttons ~= nil and #popup.buttons >= 2
end

local OK_LABELS = { "ok", "close", "continue", "got it", "understood", "confirm", "skip", "accept" }

local function singleButton(el)
    local btns = UI.findAll(el, UI.isButton, { prune = true })
    local labeled = {}
    for _, b in ipairs(btns) do
        local l = UI.allText(b)
        if l then labeled[#labeled + 1] = { el = b, label = l } end
    end
    return labeled
end

-- Closers per page: list of {how, fn(el, dc, dry) -> ok, err}.
local function cmd(name) return function(_, dc, dry) return UI.command(dc, name, nil, dry) end end
local function cmdPath(path) return function(_, dc, dry)
    local c = UI.path(dc, path)
    if not c then return false, "no " .. path end
    return UI.execute(c, nil, dry)
end end
local function esc(_, _, dry)
    if dry then return true end
    return pcall(Ext.Input.InjectKeyPress, "ESCAPE")
end

G.CLOSERS = {
    ModalTutorial = { { "FinishTutorial", cmd("FinishTutorial") }, { "escape", esc } },
    TutorialPopup = { { "escape", esc } },
    RewardPanel = { { "GetRewardsAndClose", cmdPath("GetRewardsAndClose.ActionCommand") } },
    Book = { { "escape", esc } },
    Examine = { { "escape", esc } },
    SignUp = { { "CancelBack", cmd("CancelBack") }, { "escape", esc } },
    Eula = {},  -- needs an explicit accept: left to the agent/runner
}

local function closePage(name, w, dry)
    local dc = try(function() return w.el.DataContext end)
    for _, c in ipairs(G.CLOSERS[name] or {}) do
        local ok = c[2](w.el, dc, dry)
        if ok then return c[1] end
    end
    return nil
end

RPC.register("ui.dismiss", function(args)
    local closed, decisions, failed = {}, {}, {}
    local wmap = UI.widgetMap()
    for name, w in pairs(wmap) do
        if UI.isVisible(w.el) then
            if name == "Dialog_box" then
                local p = S.popup(w.el)
                if G.isDecision(p) then
                    decisions[#decisions + 1] = { page = name, title = p.title, text = p.text, buttons = p.buttons }
                else
                    local btns = singleButton(w.el)
                    local ok = btns[1] and UI.press(btns[1].el, args.dry)
                    if ok then
                        closed[#closed + 1] = { page = name, how = "press:" .. btns[1].label, title = p.title }
                    else
                        failed[#failed + 1] = name
                    end
                end
            elseif G.CLOSERS[name] then
                local p = S.popup(w.el)
                local how = closePage(name, w, args.dry)
                if how then
                    closed[#closed + 1] = { page = name, how = how, title = p.title }
                else
                    failed[#failed + 1] = name
                end
            end
        end
    end
    -- Side tutorial notifications (non-modal) live on the notification DC.
    local nw = wmap.NotificationWidget
    if nw then
        local dc = try(function() return nw.el.DataContext end)
        local side = UI.byName(nw.el, "SideTutorials")
        if side and UI.childCount(side) > 0 and dc then
            local ok = UI.command(dc, "HideUnifiedTutorialCommand", nil, args.dry)
            if ok then closed[#closed + 1] = { page = "SideTutorials", how = "HideUnifiedTutorialCommand" } end
        end
    end
    return {
        closed = closed,
        decisions = #decisions > 0 and decisions or nil,
        failed = #failed > 0 and failed or nil,
        dry = args.dry or nil,
    }
end)

------------------------------------------------------------------------
-- Re-apply saved settings at bootstrap and when the game (re)starts running.
------------------------------------------------------------------------

pcall(G.applySaved)
if not G.subscribed then
    G.subscribed = true
    local last = nil
    Ext.Events.Tick:Subscribe(function()
        local gs = tostring(try(Ext.Utils.GetGameState) or "")
        if gs ~= last then
            last = gs
            if gs == "Running" or gs == "Menu" then pcall(G.applySaved) end
        end
    end)
end

return G
