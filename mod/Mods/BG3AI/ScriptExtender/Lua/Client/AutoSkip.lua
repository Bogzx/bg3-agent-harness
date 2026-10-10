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
--   * Load-time movies (the New Game intro after "Venture Forth", LIVE BUG
--     2026-10-09: >2.5 min unskipped) play while the game state is
--     StartLoading/LoadLevel/...: GE.OnMovieShow pushes the same "Movie" state
--     (AlwaysOnTopOverlay, HideStatesBelow) over the Loading state, so the
--     loading screen "root" is still a child, hidden or under the overlay.
--     The engine game state "Movie" counts too. These get a shim ESCAPE every
--     LOAD_RETRY_MS for as long as they stay up (the intro needed two).
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
--   * A node without subtitle text (cinematic shots, a door opening, the
--     mind flayer's reveal: S002 sat ~20 s on these in TUT_Helm_MindflayerCall
--     because AutoSkip needed text) is only skipped while the game says the
--     line is playing (ActiveDialogue.ShowAnswers == false) with no answers or
--     continue prompt, after NOTEXT_GRACE_MS, every NOTEXT_RETRY_MS while it
--     lasts (up to NOTEXT_MAX_TRIES).
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
K.LINE_MAX_TRIES = 8    -- a text line: command, engine SPACE, then shim SPACE (see lineTick)
K.LINE_WANT_MS = 400    -- the skipper honours a line's shim-SPACE request only this fresh
K.LOAD_RETRY_MS = 2000  -- load-time movies: shim ESCAPE at most this often, while it lasts
K.LOAD_MAX_TRIES = 120  -- ~4 min of tries
K.STALE_MS = 500        -- no tick for this long (loading may stall it): autoskip.status probes itself
K.LINE_GRACE_MS = 250   -- let a new line render before skipping it
K.NOTEXT_GRACE_MS = 600 -- a text-less node: longer, so a line whose text is still rendering is not lost
K.NOTEXT_RETRY_MS = 1000
K.NOTEXT_MAX_TRIES = 60 -- text-less nodes chain under one key: keep skipping for about a minute
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
    -- Two named reads, not GetAllProperties of the whole player VM every tick.
    local p = UI.pick(UI.prop(dc, "CurrentPlayer"), { "IsCurrentTurn", "CanEndTurn" })
    return p.IsCurrentTurn == true or p.CanEndTurn == true
end

-- Game states during which the loading screen is up (a movie can play over it).
local LOAD_STATES = {
    StartLoading = true, StopLoading = true, LoadLevel = true, LoadModule = true, LoadSession = true,
    SwapLevel = true, PrepareRunning = true, Init = true, Idle = true,
}
K.LOAD_STATES = LOAD_STATES

-- What is on screen, from the names of ContentRoot's children and the game
-- state: "movie" (a Bink movie in a running game or the main menu: only
-- movie-safe widgets), "load" (a movie over a loading screen, or the engine
-- state Movie), or false. rootHidden() is only called when it decides.
function K.classify(names, gs, rootHidden)
    if gs == "Movie" then return "load" end
    if #names == 0 then return false end
    local overlay, root, other = false, false, false
    for _, n in ipairs(names) do
        if n == "AlwaysOnTopOverlay" then overlay = true end
        if n == "root" then root = true
        elseif not MOVIE_OK[n] then other = true end
    end
    if other then return false end
    if gs == "Running" or gs == "Menu" then
        return (not root) and "movie" or false
    end
    if LOAD_STATES[gs] then
        -- The Movie state's overlay, or the loading screen hidden by it.
        if overlay then return "load" end
        if root and rootHidden and rootHidden() then return "load" end
    end
    return false
end

-- The cheap probe: names of ContentRoot's children (plus the loading
-- screen's visibility while loading), no DataContext reads.
function K.probeMovie()
    local gs = tostring(try(Ext.Utils.GetGameState) or "")
    if gs == "Movie" then return "load" end
    local cr = UI.contentRoot()
    if not cr then return false end
    local names, rootEl = {}, nil
    for i = 1, UI.childCount(cr) do
        local w = UI.child(cr, i)
        if w then
            local n = UI.name(w) or "?"
            names[#names + 1] = n
            if n == "root" then rootEl = w end
        end
    end
    return K.classify(names, gs, function() return rootEl ~= nil and not UI.isVisible(rootEl) end)
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
    local kind = K.probeMovie()
    if not kind then
        st.movie = nil
        if K.want and K.want.reason ~= "line" then K.want = nil end
        return false
    end
    local m = st.movie
    if not m or m.kind ~= kind then
        m = { since = now, tries = 0, last = 0, kind = kind }
        st.movie = m
        return true
    end
    local load = kind == "load"
    local maxTries = load and K.LOAD_MAX_TRIES or K.MAX_TRIES
    if now - m.since < K.MOVIE_CONFIRM_MS or m.tries >= maxTries then return true end
    if m.tries == 0 then
        -- 1st try: the engine's key injection (Bink may only react to real input).
        m.tries = 1
        m.last = now
        press("ESCAPE")
        K.stats.movies = K.stats.movies + 1
        RPC.push("movie_skipped", { kind = kind })
    elseif now - m.last >= (m.tries == 1 and K.ENGINE_GRACE_MS or (load and K.LOAD_RETRY_MS or K.RETRY_MS)) then
        -- Still up: the bridge sends ESCAPE through the SDL input shim (se
        -- verified it skips Bink movies); it re-checks autoskip.status first,
        -- since a stray ESCAPE in game opens the menu. A load-time movie may
        -- have several parts: keep asking every LOAD_RETRY_MS.
        m.tries = m.tries + 1
        m.last = now
        want("ESCAPE", load and "load_movie" or "movie", m.tries, now)
    end
    return true
end

function K.tick()
    local now = RPC.now_ms()
    local st = K.state
    K.lastTick = now
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
    -- A line's shim-SPACE request lives one dialogue tick: re-made below only while that line still plays.
    if K.want and K.want.reason == "line" then K.want = nil end

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
    if not d or d.changing then st.line = nil return end
    local text = d.text
    if type(text) ~= "string" or not text:find("%S") then text = nil end
    -- No text: only a node the game reports as playing (ShowAnswers == false) and
    -- with nothing to choose. Anything else (opening, closing, answers coming) waits.
    if not text and not (d.listening and not d.options and not d.continue) then st.line = nil return end

    local key = text and ((d.speaker or "") .. "|" .. text) or "#notext"
    local line = st.line
    if not line or line.key ~= key then
        line = { key = key, since = now, tries = 0, last = 0, notext = not text }
        st.line = line
        if text then
            K.stats.seen = K.stats.seen + 1
            line.ev = RPC.push("dialogue_line", { speaker = d.speaker, text = text, options = d.options and #d.options or nil })
        end
    end

    -- Choices are the agent's: answers or a continue prompt mean stop.
    if d.options or d.continue then return end
    -- Act only on a node seen unchanged on two consecutive ticks.
    local grace = line.notext and K.NOTEXT_GRACE_MS or K.LINE_GRACE_MS
    if now - line.since < math.max(grace, K.PERIOD_MS) then return end
    local maxTries = line.notext and K.NOTEXT_MAX_TRIES or K.LINE_MAX_TRIES
    local gap = line.notext and K.NOTEXT_RETRY_MS or K.RETRY_MS
    if line.tries >= maxTries or now - line.last < gap then return end
    if isOurCombatTurn(dc) then return end

    line.tries = line.tries + 1
    line.last = now
    -- Escalate while the same line stays up (S023: Mizora's and Halsin's cinematic lines played at voice
    -- pace: SelectorSkipCommand "succeeded" with no effect, so SPACE never came; ~4 min of waits): the
    -- page's skip command, then the engine's SPACE, then SPACE through the bridge's input shim (real input).
    local ok
    if line.tries == 1 or (line.notext and line.tries % 2 == 1) then
        ok = UI.command(dc, "SelectorSkipCommand", nil, false)
        if not ok then ok = press("SPACE") end
    elseif line.tries == 2 or line.notext then
        ok = press("SPACE")
    else
        want("SPACE", "line", line.tries, now)
        ok = true
    end
    if ok and line.tries == 1 then
        if line.notext then K.stats.cinematic = (K.stats.cinematic or 0) + 1
        else K.stats.lines = K.stats.lines + 1 end
    end
end

function K.view()
    local cin = K.stats.cinematic or 0
    return { on = K.enabled(), skipped = K.stats.lines + K.stats.movies + cin, lines = K.stats.lines,
             movies = K.stats.movies, cinematic = cin > 0 and cin or nil }
end

-- autoskip.status: the bridge's skipper polls this (cheap: no UI reads).
-- -> {on, now, movie?={age, tries, kind}, want?={seq,key,reason,try,at}, stats}
-- {debug=true} adds {gs, widgets=[ContentRoot child names], probe} (for live checks).
RPC.register("autoskip.status", function(args)
    local now = RPC.now_ms()
    -- While loading, the client tick may not run: let the skipper's poll drive the movie probe.
    if K.enabled() and now - (K.lastTick or 0) > K.STALE_MS and now >= (K.state.nextMovie or 0) then
        K.state.nextMovie = now + K.FAST_MS
        local ok, err = pcall(K.movieTick, now)
        if not ok then K.errors = (K.errors or 0) + 1 end
        K.polled = (K.polled or 0) + 1
    end
    local m = K.state.movie
    local res = {
        on = K.enabled(), now = now,
        movie = m and { age = now - m.since, tries = m.tries, kind = m.kind } or nil,
        want = K.want,
        line = K.state.line and { notext = K.state.line.notext or nil, tries = K.state.line.tries,
                                  age = now - K.state.line.since } or nil,
        stats = { lines = K.stats.lines, movies = K.stats.movies, seen = K.stats.seen, errors = K.errors or 0,
                  cinematic = K.stats.cinematic or 0, polled = K.polled },
    }
    if type(args) == "table" and args.debug then
        res.gs = tostring(try(Ext.Utils.GetGameState) or "")
        local names = {}
        local cr = UI.contentRoot()
        for i = 1, cr and UI.childCount(cr) or 0 do
            local w = UI.child(cr, i)
            if w then names[#names + 1] = UI.name(w) or "?" end
        end
        res.widgets = names
        res.probe = K.probeMovie() or false
    end
    return res
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
