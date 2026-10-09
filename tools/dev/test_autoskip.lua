-- Offline tests for Client/AutoSkip.lua (worker se): movie probe timing, the
-- shim-key requests the bridge's skipper acts on (autoskip.status), safety
-- rules (decision pages, loading, quiet, disabled) and dialogue line skips.
-- Ext, RPC, UI, UIState and UIActions are stubbed.
-- Run: tools/dev/test_autoskip.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local clock = 1000
local keys = {}
local events = {}
local tickers = {}
local stateHandlers = {}
local gameState = "Running"
local widgets = {}      -- list of {name=, dc=, visible=}
local quiet = false
local commands = {}
local dialogue = nil    -- what UIState.dialogue returns

Ext = {
    Utils = {
        MonotonicTime = function() return clock end,
        GetGameState = function() return gameState end,
        PrintWarning = function(m) print("WARN " .. m) end,
    },
    Input = { InjectKeyPress = function(k) keys[#keys + 1] = k end },
    Events = {
        Tick = { Subscribe = function(_, fn) tickers[#tickers + 1] = fn end },
        GameStateChanged = { Subscribe = function(_, fn) stateHandlers[#stateHandlers + 1] = fn end },
    },
}

local ops = {}
BG3AI = {
    RPC = {
        now_ms = function() return clock end,
        push = function(kind, data) events[#events + 1] = { kind = kind, data = data } return #events end,
        register = function(name, fn) ops[name] = fn end,
    },
    UI = {
        contentRoot = function() return { cr = true } end,
        childCount = function() return #widgets end,
        child = function(_, i) return widgets[i] end,
        name = function(w) return w.name end,
        widgets = function()
            local out = {}
            for i, w in ipairs(widgets) do out[i] = { name = w.name, dc = w.dc, el = w, index = i } end
            return out
        end,
        isVisible = function(el) return el.visible ~= false end,
        quiet = function() return quiet end,
        command = function(dc, name) commands[#commands + 1] = name return true end,
        props = function() return {} end,
        prop = function() return nil end,
    },
    UIState = {
        activeDialogue = function() return true end,
        dialogue = function() return dialogue end,
    },
    UIActions = { config = {} },
}

dofile(LUA .. "/Client/AutoSkip.lua")
local K = BG3AI.AutoSkip

local failures, passed = 0, 0
local function check(name, cond, extra)
    if cond then passed = passed + 1 else
        failures = failures + 1
        print("FAIL " .. name .. (extra and (": " .. tostring(extra)) or ""))
    end
end

local function run(ms, step)
    step = step or 16
    local stop = clock + ms
    while clock < stop do
        clock = clock + step
        for _, t in ipairs(tickers) do t() end
    end
end

local function reset()
    keys, events, commands = {}, {}, {}
    K.state = { next = 0, nextMovie = 0 }
    K.want = nil
    widgets, dialogue, quiet, gameState = {}, nil, false, "Running"
    BG3AI.UIActions.config = {}
    run(50)
end

local function count(list, v)
    local n = 0
    for _, x in ipairs(list) do if x == v or (type(x) == "table" and x.kind == v) then n = n + 1 end end
    return n
end

local MOVIE = { { name = "AlwaysOnTopOverlay" }, { name = "ScreenFade" } }

-- 1. a movie: engine ESCAPE within ~0.3 s, then shim requests every RETRY_MS
reset()
widgets = MOVIE
run(120)
check("no key before confirm", #keys == 0, #keys)
run(200)
check("engine ESCAPE after confirm", count(keys, "ESCAPE") == 1, #keys)
check("movie_skipped event", count(events, "movie_skipped") == 1)
check("no shim request yet", K.want == nil)
local st = ops["autoskip.status"]()
check("status shows movie", st.movie ~= nil and st.movie.tries == 1)
run(K.ENGINE_GRACE_MS + 50)
check("shim request after grace", K.want ~= nil and K.want.seq ~= nil and K.want.key == "ESCAPE")
local first = K.want and K.want.seq
check("needs_skip_key event", count(events, "needs_skip_key") == 1)
st = ops["autoskip.status"]()
check("status carries want", st.want ~= nil and st.want.seq == first)
run(K.RETRY_MS + 150)
check("second shim request", K.want ~= nil and K.want.seq == first + 1, K.want and K.want.seq)
run(5000)
check("tries capped", K.state.movie.tries == K.MAX_TRIES, K.state.movie.tries)
check("engine ESCAPE only once", count(keys, "ESCAPE") == 1)
widgets = {}
run(150)
check("movie gone clears want", K.want == nil and ops["autoskip.status"]().movie == nil)

-- 2. latency budget: first shim request within 500 ms (ticks every 16 ms here) of the movie appearing
reset()
local t0 = clock
widgets = MOVIE
while K.want == nil and clock - t0 < 3000 do run(16) end
check("shim request within 500 ms", clock - t0 <= 500, clock - t0)

-- 3. never a movie when anything else is on screen, loading, or not Running
for _, case in ipairs({
    { "decision page", { { name = "ScreenFade" }, { name = "ActiveRoll" } }, "Running" },
    { "loading", { { name = "root", dc = "ls.LoadingScreen" } }, "Running" },
    { "not running", MOVIE, "Paused" },
    { "empty", {}, "Running" },
}) do
    reset()
    widgets, gameState = case[2], case[3]
    run(2000)
    check(case[1] .. ": no key", #keys == 0 and K.want == nil, #keys)
end

-- 4. disabled / quiet
reset()
BG3AI.UIActions.config = { autoskip = false }
widgets = MOVIE
run(2000)
check("disabled: nothing", #keys == 0 and K.want == nil and ops["autoskip.status"]().on == false)
reset()
quiet = true
widgets = MOVIE
run(2000)
check("quiet: nothing", #keys == 0 and K.want == nil)

-- 5. game-state change triggers an immediate probe
reset()
K.state.nextMovie = clock + 100000
for _, h in ipairs(stateHandlers) do h() end
check("state change resets probe", K.state.nextMovie == 0)

-- 6. dialogue lines: skipped after the grace, never with answers
reset()
widgets = { { name = "Dialogue", dc = "gui.DCDialog", DataContext = { dc = true } } }
dialogue = { speaker = "Lae'zel", text = "Tsk'va." }
run(200)
check("line not skipped before grace", #commands == 0)
run(400)
check("line skipped", count(commands, "SelectorSkipCommand") == 1, #commands)
check("dialogue_line event", count(events, "dialogue_line") == 1)
reset()
widgets = { { name = "Dialogue", dc = "gui.DCDialog", DataContext = { dc = true } } }
dialogue = { speaker = "Lae'zel", text = "Choose.", options = { {}, {} } }
run(3000)
check("answers: no skip", #commands == 0 and #keys == 0)

print(string.format("%d passed, %d failed", passed, failures))
if failures > 0 then os.exit(1) end
