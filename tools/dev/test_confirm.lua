-- Offline tests for confirmation boxes (worker se): UIActions ui.confirm /
-- pickConfirm / the ui.command "Yes" fallback, and Services' long rest
-- answering "End the day?" + "Someone wants to talk, rest anyway?" (live S005), and the re-run after
-- the bridge clicks a dead box (live S018: no second CampTravel while the rest screen opens).
-- Ext, RPC, UI and UIState are stubbed.
-- Run: tools/dev/test_confirm.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

clock = 1000
keys, commands = {}, {}
widgets = {}      -- name -> {name, visible, buttons={{label,name,onPress}}, texts={}}
inCamp = true

function W(name, texts, buttons)
    local w = { name = name, visible = true, texts = texts or {}, buttons = {} }
    for i, b in ipairs(buttons or {}) do
        w.buttons[i] = { isButton = true, label = b[1], name = b[2], onPress = b[3], parent = w }
    end
    widgets[name] = w
    return w
end

Ext = {
    Utils = { MonotonicTime = function() return clock end, GetGameState = function() return "Running" end },
    Input = { InjectKeyPress = function(k) keys[#keys + 1] = k end },
    IO = { LoadFile = function() return nil end },
    Json = { Parse = function() return {} end }, Require = function(p) return dofile(LUA .. "/" .. p) end, -- + parts
}

jobs = {}
ops = {}
local nextId = 0
BG3AI = {
    RPC = {
        now_ms = function() return clock end,
        register = function(name, fn) ops[name] = fn end,
        push = function() end,
        start_job = function(kind, spec)
            nextId = nextId + 1
            local job = spec or {}
            job.id, job.kind, job.started, job.status = nextId, kind, clock, "running"
            jobs[nextId] = job
            return job
        end,
        finish_job = function(job, status, result, err)
            if job.status ~= "running" then return end
            job.status, job.result, job.error = status, result, err
        end,
    },
    UI = {
        widgets = function()
            local out, names = {}, {}
            for n in pairs(widgets) do names[#names + 1] = n end
            table.sort(names)
            for _, n in ipairs(names) do out[#out + 1] = { name = n, el = widgets[n] } end
            return out
        end,
        widget = function(n) return widgets[n] end,
        isVisible = function(el) return el.visible ~= false end,
        isButton = function(e) return e.isButton == true end,
        findAll = function(el, pred)
            local out = {}
            for _, b in ipairs(el.buttons or {}) do if pred(b) then out[#out + 1] = b end end
            return out
        end,
        allText = function(e) return e.label end,
        name = function(e) return e.name end,
        press = function(el, dry)
            if dry then return true end
            if el.onPress then el.onPress(el) end
            return true
        end,
        props = function() return {} end,
        prop = function(e, k) if k == "IsMouseOver" and type(e) == "table" then return e.over end return nil end,
        path = function(dc, p)
            if p == "CurrentPlayer.SelectedCharacter.PlayerCharacterProperties.IsInCamp" then return inCamp end
            return nil
        end,
        dc = function(n) return widgets[n] and { dc = n } or { dc = "HotBar" } end,
        anyDC = function() return { dc = "any" } end,
        command = function(dc, name, param, dry)
            if dc.dc == "any" or dc.dc == "HotBar" then
                if name == "CampTravel" then
                    commands[#commands + 1] = name
                    if not dry then
                        -- "End the day?" then "Someone wants to talk" then the rest screen
                        W("Dialog_box", { "[ForceUpdate]" }, {
                            { "Yes", "YesBtn", function(b)
                                W("Dialog_box", { "[ForceUpdate]", "Someone wants to talk. Rest anyway?" }, {
                                    { "Yes", nil, function() widgets.Dialog_box = nil W("MakeCamp", {}, {}) end },
                                    { "No", nil, function() widgets.Dialog_box = nil end } })
                            end },
                            { "No", "NoBtn", function() widgets.Dialog_box = nil end } })
                    end
                    return true
                end
            end
            return false, "no command '" .. tostring(name) .. "' on ls.Widget"
        end,
        byName = function() return nil end,
        findButton = function() return nil end,
        execute = function() return true end,
    },
    UIState = {
        popup = function(el)
            local p = {}
            if el.texts[1] then p.title = el.texts[1] end
            if el.texts[2] then p.text = el.texts[2] end
            local b = {}
            for _, x in ipairs(el.buttons) do b[#b + 1] = x.label end
            if #b > 0 then p.buttons = b end
            return p
        end,
        SCREENS = {},
    },
    Util = { comp = function() return nil end, dist = function() return 0 end },
}

dofile(LUA .. "/Client/UIActions.lua")
dofile(LUA .. "/Client/Services.lua")
A = BG3AI.UIActions

local fails, passes = 0, 0
function ok(cond, msg)
    if cond then passes = passes + 1 else fails = fails + 1 print("FAIL " .. msg) end
end
function run(id, ms)
    local job = jobs[id]
    for _ = 1, (ms or 5000) / 50 do
        clock = clock + 50
        if job.status ~= "running" then break end
        if clock > job.deadline_at then job.status = "timeout" break end
        job.on_tick(job)
    end
    return job
end
local _start = BG3AI.RPC.start_job
BG3AI.RPC.start_job = function(kind, spec)
    local j = _start(kind, spec)
    j.deadline_at = clock + (j.timeout_ms or 20000)
    return j
end

-- ---- the tests: these files, in order, on the stubs above (split for the 1000-line cap) ----
local DIR = arg[0]:match("^(.*)/[^/]*$") or "."
for _, part in ipairs({ "test_confirm_boxes.lua", "test_confirm_map.lua",
    "test_confirm_panels.lua" }) do dofile(DIR .. "/" .. part) end

print(("%d passed, %d failed"):format(passes, fails))
os.exit(fails == 0 and 0 or 1)
