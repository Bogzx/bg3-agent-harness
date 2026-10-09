-- AutoSkip (owner: worker "io"): always skip movies, cinematics and voiced
-- dialogue lines, so the agent never waits on them. ON by default; toggle
-- with ui.config{autoskip=false} (persisted with the other gates).
--
-- How things look on the client (from Public/Game/GUI/StateMachine.xaml and
-- Mods/MainUI/GUI/Pages/Dialogue.xaml):
--   * Bink movies (intro, act transitions) push the UI state "Movie": only
--     AlwaysOnTopOverlay + ScreenFade are under ContentRoot, no HUD at all,
--     and the game state is Running (a loading screen is the widget "root"
--     with an ls.LoadingScreen DC). Skip: ESCAPE.
--   * Dialogues and cinematic (timeline) dialogues are the Dialogue page.
--     While a line plays without answers, the page binds UISkipDialog to
--     DC.SelectorSkipCommand(PlayerId); we run that command (parameter nil,
--     bg3le cannot box the id) and fall back to SPACE, the default
--     UISkipDialog key.
--
-- Safety rules (hard):
--   * Never act while answers, a continue prompt, a roll, a reaction prompt,
--     a popup or any other decision page is up.
--   * SPACE is only injected while the Dialogue page is visible, an
--     ActiveDialogue exists, no answers are shown, and it is not our
--     combat turn (Space is End Turn).
--   * Each line or movie gets at most MAX_TRIES presses, RETRY_MS apart.
--
-- Every new dialogue line seen (skipped or not) is pushed as a client event
-- "dialogue_line" {speaker?, text, options?=n}, so the agent still reads all
-- of it; skipped movies push "movie_skipped".
--
-- Latency (se, machine 2): movies are probed every FAST_MS from the tick with
-- a cheap check (ContentRoot child names only, no DataContext reads), and at
-- once after a game-state change. A movie seen on two probes gets the engine
-- ESCAPE; if it is still up ENGINE_GRACE_MS later, a shim key is wanted.
-- The bridge's skipper (bridge/src/bg3ai/skipper.py) polls the cheap op
-- autoskip.status and sends that key through the input shim, so a movie is
-- gone within about half a second. Dialogue lines run every PERIOD_MS.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local S = BG3AI.UIState
local A = BG3AI.UIActions
local K = BG3AI.AutoSkip or {}
BG3AI.AutoSkip = K

K.PERIOD_MS = 250       -- dialogue lines (was 500 after the S001 crash; bg3le now holds references)
K.FAST_MS = 100         -- movie probe
K.MOVIE_CONFIRM_MS = 100 -- a movie seen on two probes is real, not a transition
K.ENGINE_GRACE_MS = 200 -- after the engine ESCAPE, before asking the bridge for a shim key
K.RETRY_MS = 700        -- between shim keys, and between line skips
K.MAX_TRIES = 4
K.LINE_GRACE_MS = 250   -- let a new line render before skipping it
K.stats = K.stats or { lines = 0, movies = 0, seen = 0 }
K.state = { next = 0, nextMovie = 0 }
-- What the bridge should press through the shim: {seq, key, reason, try, at}
-- while a movie stays up; nil otherwise. seq grows with every request.
K.want = nil
K.wantSeq = K.wantSeq or 0

local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
    return nil
end

function K.enabled()
    return not (A and A.config and A.config.autoskip == false)
end

-- Pages that are decisions or menus: never skip anything while one is up.
local DECISION_PAGES = {
    ActiveRoll = true, ReactionDecisionPopup = true, Dialog_box = true, SaveGamePopup = true,
    ModalTutorial = true, TutorialPopup = true, CharacterLevelUp = true, Trade = true, Pickpocket = true,
    LongRest = true, MakeCamp = true, ContainerWidget = true, CharacterCreation = true,
    CharacterRespec = true, CharacterFullRespec = true, MainMenu = true, GameMenu = true,
    SaveGame = true, LoadGame = true, GameOptions = true, SplitItem = true, RewardPanel = true,
    LearnSpells = true, TadpolePowersTree = true, Book = true, DMSettings = true,
}

-- Widgets that may exist during a movie.
local MOVIE_OK = {
    AlwaysOnTopOverlay = true, ScreenFade = true, CrossplayOverlay = true, ModDownloadingWidget = true,
}

local function isOurCombatTurn(dc)
    local p = UI.props(UI.prop(dc, "CurrentPlayer"))
    return p.IsCurrentTurn == true or p.CanEndTurn == true
end

-- "movie" when only movie-safe widgets exist (and at least one), the game is
-- running and no loading screen is up (a loading screen is the widget "root",
-- which is not movie-safe).
local function movieShowing(widgets)
    if #widgets == 0 then return false end
    for _, w in ipairs(widgets) do
        if not MOVIE_OK[w.name] then return false end
    end
    local gs = tostring(try(Ext.Utils.GetGameState) or "")
    return gs == "Running"
end

-- The cheap probe: names of ContentRoot's children, nothing else.
function K.probeMovie()
    local cr = UI.contentRoot()
    if not cr then return false end
    local names = {}
    for i = 1, UI.childCount(cr) do
        local w = UI.child(cr, i)
        if w then names[#names + 1] = { name = UI.name(w) or "?" } end
    end
    return movieShowing(names)
end

local function press(key)
    local ok = pcall(Ext.Input.InjectKeyPress, key)
    return ok
end

local function want(key, reason, tries, now)
    K.wantSeq = K.wantSeq + 1
    K.want = { seq = K.wantSeq, key = key, reason = reason, try = tries, at = now }
    RPC.push("needs_skip_key", { key = key, reason = reason, try = tries, seq = K.wantSeq })
end

-- Movies: from the tick every FAST_MS. Returns true while one is showing.
function K.movieTick(now)
    local st = K.state
    if not K.probeMovie() then
        st.movie = nil
        K.want = nil
        return false
    end
    local m = st.movie
    if not m then
        m = { since = now, tries = 0, last = 0 }
        st.movie = m
        return true
    end
    if now - m.since < K.MOVIE_CONFIRM_MS or m.tries >= K.MAX_TRIES then return true end
    if m.tries == 0 then
        -- 1st try: the engine's key injection (Bink may only react to real input).
        m.tries = 1
        m.last = now
        press("ESCAPE")
        K.stats.movies = K.stats.movies + 1
        RPC.push("movie_skipped", {})
    elseif now - m.last >= (m.tries == 1 and K.ENGINE_GRACE_MS or K.RETRY_MS) then
        -- Still up: the bridge sends ESCAPE through the SDL input shim (se
        -- verified it skips Bink movies); it re-checks autoskip.status first,
        -- since a stray ESCAPE in game opens the menu.
        m.tries = m.tries + 1
        m.last = now
        want("ESCAPE", "movie", m.tries, now)
    end
    return true
end

function K.tick()
    local now = RPC.now_ms()
    local st = K.state
    if not K.enabled() then
        st.movie, K.want = nil, nil
        return
    end
    local fast = now >= st.nextMovie
    local slow = now >= st.next
    if not fast and not slow then return end
    -- Stay off the tree right after our own UI commands.
    if UI.quiet(3) then return end

    if fast then
        st.nextMovie = now + K.FAST_MS
        if K.movieTick(now) then return end
    end
    if not slow then return end
    st.next = now + K.PERIOD_MS

    local widgets = UI.widgets()
    local wmap = {}
    for _, w in ipairs(widgets) do
        if UI.isVisible(w.el) then wmap[w.name] = wmap[w.name] or w end
        if w.name == "root" or w.dc == "ls.LoadingScreen" then return end -- loading
    end
    for name in pairs(wmap) do
        if DECISION_PAGES[name] then st.line = nil return end
    end

    -- Dialogue lines
    local dw = wmap.Dialogue
    if not dw then st.line = nil return end
    local dc = try(function() return dw.el.DataContext end)
    if not dc or not S.activeDialogue(dc) then return end
    local d = S.dialogue({ Dialogue = dw })
    if not d or not d.text or d.changing then st.line = nil return end

    local key = (d.speaker or "") .. "|" .. d.text
    local line = st.line
    if not line or line.key ~= key then
        line = { key = key, since = now, tries = 0, last = 0 }
        st.line = line
        K.stats.seen = K.stats.seen + 1
        line.ev = RPC.push("dialogue_line", { speaker = d.speaker, text = d.text, options = d.options and #d.options or nil })
    end

    -- Choices are the agent's: answers or a continue prompt mean stop.
    if d.options or d.continue then return end
    -- Act only on a node seen unchanged on two consecutive ticks.
    if now - line.since < math.max(K.LINE_GRACE_MS, K.PERIOD_MS) then return end
    if line.tries >= K.MAX_TRIES or now - line.last < K.RETRY_MS then return end
    if isOurCombatTurn(dc) then return end

    line.tries = line.tries + 1
    line.last = now
    local ok = UI.command(dc, "SelectorSkipCommand", nil, false)
    if not ok then ok = press("SPACE") end
    if ok and line.tries == 1 then K.stats.lines = K.stats.lines + 1 end
end

function K.view()
    return { on = K.enabled(), skipped = K.stats.lines + K.stats.movies, lines = K.stats.lines,
             movies = K.stats.movies }
end

-- autoskip.status: the bridge's skipper polls this (cheap: no UI reads).
-- -> {on, now, movie?={age, tries}, want?={seq,key,reason,try,at}, stats}
RPC.register("autoskip.status", function()
    local now = RPC.now_ms()
    local m = K.state.movie
    return {
        on = K.enabled(), now = now,
        movie = m and { age = now - m.since, tries = m.tries } or nil,
        want = K.want,
        stats = { lines = K.stats.lines, movies = K.stats.movies, seen = K.stats.seen, errors = K.errors or 0 },
    }
end)

if not K.subscribed then
    K.subscribed = true
    Ext.Events.Tick:Subscribe(function()
        local ok, err = pcall(K.tick)
        if not ok then
            K.errors = (K.errors or 0) + 1
            if K.errors <= 5 then Ext.Utils.PrintWarning("[BG3AI] autoskip: " .. tostring(err)) end
        end
    end)
    -- Movies follow loads and level changes: probe on the next tick.
    pcall(function()
        Ext.Events.GameStateChanged:Subscribe(function() K.state.nextMovie = 0 end)
    end)
end

return K
