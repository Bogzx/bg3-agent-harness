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
printed = {}

Ext = {
    Utils = {
        MonotonicTime = function() return clock end,
        GetGameState = function() return gameState end,
        PrintWarning = function(m) print("WARN " .. m) end,
        Print = function(m) printed[#printed + 1] = m end,
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
        pick = function() return {} end,
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
    K.lastTick = clock
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

-- 6b. S023: a cinematic line the skip command doesn't skip (Mizora, Halsin): command, then the engine
--     SPACE, then shim SPACE requests (want reason=line) until it goes; a new line or answers clear it
reset()
widgets = { { name = "Dialogue", dc = "gui.DCDialog", DataContext = { dc = true } } }
dialogue = { speaker = "Mizora", text = "Clause G, Section Nine." }
run(600)
check("stuck line: command first", count(commands, "SelectorSkipCommand") == 1 and #keys == 0, #keys)
run(700)
check("stuck line: then engine SPACE", count(keys, "SPACE") == 1, #keys)
local sawWant = nil
for _ = 1, 60 do
    run(16)
    local w = ops["autoskip.status"]().want
    if w and w.reason == "line" then sawWant = w break end
end
check("stuck line: then a shim SPACE request", sawWant and sawWant.key == "SPACE" and sawWant.try == 3,
    sawWant and sawWant.try)
local st = ops["autoskip.status"]()
check("stuck line: status carries the line", st.line and st.line.tries == 3 and st.movie == nil)
check("stuck line: movie probe keeps the line request", K.want ~= nil)
run(300)
check("stuck line: request lives one tick", K.want == nil or K.want.try == 4, K.want and K.want.try)
dialogue = { speaker = "Mizora", text = "Choose.", options = { {}, {} } }
run(300)
check("answers: line request cleared", K.want == nil)
run(5000)
check("answers: no more requests", K.want == nil and count(keys, "SPACE") == 1)
reset()
widgets = { { name = "Dialogue", dc = "gui.DCDialog", DataContext = { dc = true } } }
dialogue = { speaker = "Halsin", text = "Oak father's blessings to you." }
run(20000)
check("stuck line: capped at LINE_MAX_TRIES", K.state.line.tries == K.LINE_MAX_TRIES, K.state.line.tries)

-- 7. a movie over the loading screen (New Game intro: game state StartLoading, the Movie state's
--    overlay above the loading screen "root"): engine ESCAPE, then shim requests every LOAD_RETRY_MS
--    for as long as it lasts (past MAX_TRIES)
local LOADMOVIE = { { name = "root", dc = "ls.LoadingScreen" }, { name = "AlwaysOnTopOverlay" }, { name = "ScreenFade" } }
reset()
widgets, gameState = LOADMOVIE, "StartLoading"
run(300)
check("load movie: engine ESCAPE", count(keys, "ESCAPE") == 1, #keys)
check("load movie: status kind", (ops["autoskip.status"]().movie or {}).kind == "load")
run(K.ENGINE_GRACE_MS + 50)
check("load movie: shim request", K.want ~= nil and K.want.reason == "load_movie")
local lseq = K.want and K.want.seq
run(K.RETRY_MS + 150)
check("load movie: no request at RETRY_MS", K.want and K.want.seq == lseq)
run(K.LOAD_RETRY_MS - K.RETRY_MS)
check("load movie: request at LOAD_RETRY_MS", K.want and K.want.seq == lseq + 1, K.want and K.want.seq)
run(K.LOAD_RETRY_MS * (K.MAX_TRIES + 2))
check("load movie: keeps asking past MAX_TRIES", K.state.movie.tries > K.MAX_TRIES + 2, K.state.movie.tries)
widgets = { { name = "root", dc = "ls.LoadingScreen" }, { name = "ScreenFade" } }
run(150)
check("load movie gone clears want", K.want == nil and K.state.movie == nil)

-- 8. a plain loading screen is never a movie, in any loading state
for _, gs in ipairs({ "StartLoading", "LoadLevel", "PrepareRunning", "Paused" }) do
    reset()
    widgets, gameState = { { name = "root", dc = "ls.LoadingScreen" }, { name = "ScreenFade" } }, gs
    run(5000)
    check("loading screen " .. gs .. ": no key", #keys == 0 and K.want == nil, #keys)
end
reset()
widgets, gameState = { { name = "root" }, { name = "AlwaysOnTopOverlay" }, { name = "MessageBox" } }, "LoadLevel"
run(3000)
check("loading + other page: no key", #keys == 0 and K.want == nil)

-- 9. the loading screen hidden by the Movie state (no overlay child) counts; engine state Movie counts
reset()
widgets, gameState = { { name = "root", visible = false }, { name = "ScreenFade" } }, "LoadLevel"
run(600)
check("hidden loading screen: movie", count(keys, "ESCAPE") == 1 and K.want ~= nil)
reset()
widgets, gameState = {}, "Movie"
run(600)
check("engine state Movie: movie", count(keys, "ESCAPE") == 1 and K.want ~= nil)

-- 10. classify: the main menu's intro movie; the main menu itself is not one
check("menu movie", K.classify({ "AlwaysOnTopOverlay", "ScreenFade" }, "Menu") == "movie")
check("main menu", K.classify({ "MainMenu", "ScreenFade" }, "Menu") == false)
check("running + root", K.classify({ "root", "AlwaysOnTopOverlay" }, "Running") == false)

-- 11. no ticks while loading: autoskip.status drives the probe itself
reset()
widgets, gameState = LOADMOVIE, "StartLoading"
local sawWant = false
for _ = 1, 10 do
    clock = clock + 150  -- the skipper's poll, no Tick in between
    local r = ops["autoskip.status"]({})
    if r.want and r.movie then sawWant = true end
end
check("stale tick: status probes", sawWant and count(keys, "ESCAPE") == 1, #keys)
local dbg = ops["autoskip.status"]({ debug = true })
check("debug status", dbg.gs == "StartLoading" and #dbg.widgets == 3 and dbg.probe == "load")

-- 12. text-less cinematic nodes (S002 TUT_Helm_MindflayerCall: ~20 s of shots without subtitles):
--     skipped while ShowAnswers == false, repeatedly while they last; never without the listening flag,
--     never with answers or a continue prompt, and no empty dialogue_line events
local DLG = { { name = "Dialogue", dc = "gui.DCDialog", DataContext = { dc = true } } }
reset()
widgets = DLG
dialogue = { listening = true }
run(K.NOTEXT_GRACE_MS - 100)
check("notext: not before grace", #commands == 0, #commands)
run(K.PERIOD_MS * 2)
check("notext: skipped", count(commands, "SelectorSkipCommand") == 1, #commands)
check("notext: no dialogue_line", count(events, "dialogue_line") == 0)
run(K.NOTEXT_RETRY_MS * 5)
check("notext: keeps skipping past MAX_TRIES (command and engine SPACE in turn)", count(commands, "SelectorSkipCommand") + count(keys, "SPACE") > K.MAX_TRIES and count(keys, "SPACE") >= 1, #commands)
check("notext: stat", K.view().cinematic == 1 and ops["autoskip.status"]().line.notext == true)
-- then the mind flayer's line appears: pushed and skipped like any line
dialogue = { speaker = "Mind Flayer", text = "Thrall. Connect the nerves of the transponder.", listening = true }
local before = #commands
run(600)
check("text after notext: event", count(events, "dialogue_line") == 1)
check("text after notext: skipped", #commands == before + 1, #commands - before)
-- whitespace-only text counts as none
reset()
widgets = DLG
dialogue = { text = "  ", listening = true }
run(1500)
check("blank text: skipped as notext", count(commands, "SelectorSkipCommand") >= 1 and count(events, "dialogue_line") == 0)
for _, case in ipairs({
    { "notext without listening flag", {} },
    { "notext with answers", { listening = true, options = { {}, {} } } },
    { "notext with continue", { continue = true } },
    { "notext changing", { listening = true, changing = true } },
}) do
    reset()
    widgets = DLG
    dialogue = case[2]
    run(5000)
    check(case[1] .. ": no skip", #commands == 0 and #keys == 0, #commands)
end
-- a decision page over a text-less node
reset()
widgets = { DLG[1], { name = "ActiveRoll" } }
dialogue = { listening = true }
run(3000)
check("notext + roll: no skip", #commands == 0)
-- a text line with a continue prompt is still the agent's
reset()
widgets = DLG
dialogue = { speaker = "Lae'zel", text = "Go on.", continue = true }
run(3000)
check("continue: no skip", #commands == 0 and count(events, "dialogue_line") == 1)

-- slow ticks are recorded with their phase (bg3le's 907 ms Tick at loads)
K.slow = nil
K.phase = "widgets"
K.noteSlow(10)
check("fast tick: not recorded", K.slow == nil)
K.noteSlow(120)
K.phase = "movie"
K.noteSlow(80)
check("slow tick: max + phase kept", K.slow and K.slow.n == 2 and K.slow.max == 120 and K.slow.phase == "widgets")
check("slow tick: printed", #printed == 2 and printed[1]:find("120 ms in widgets", 1, true) ~= nil, printed[1])
check("status carries slow", ops["autoskip.status"]({}).slow == K.slow)

print(string.format("%d passed, %d failed", passed, failures))
if failures > 0 then os.exit(1) end
