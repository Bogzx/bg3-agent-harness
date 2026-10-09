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

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local S = BG3AI.UIState
local A = BG3AI.UIActions
local K = BG3AI.AutoSkip or {}
BG3AI.AutoSkip = K

K.PERIOD_MS = 500       -- 2 Hz (reduced after the S001 crash)
K.RETRY_MS = 700
K.MAX_TRIES = 4
K.LINE_GRACE_MS = 250   -- let a new line render before skipping it
K.stats = K.stats or { lines = 0, movies = 0, seen = 0 }
K.state = { next = 0 }

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
-- running and no loading screen is up.
local function movieShowing(widgets)
    if #widgets == 0 then return false end
    for _, w in ipairs(widgets) do
        if not MOVIE_OK[w.name] then return false end
    end
    local gs = tostring(try(Ext.Utils.GetGameState) or "")
    return gs == "Running"
end

local function press(key)
    local ok = pcall(Ext.Input.InjectKeyPress, key)
    return ok
end

function K.tick()
    local now = RPC.now_ms()
    local st = K.state
    if now < st.next then return end
    st.next = now + K.PERIOD_MS
    if not K.enabled() then return end
    -- Stay off the tree right after our own UI commands.
    if UI.quiet(3) then return end

    local widgets = UI.widgets()
    local wmap = {}
    for _, w in ipairs(widgets) do
        if UI.isVisible(w.el) then wmap[w.name] = wmap[w.name] or w end
        if w.name == "root" or w.dc == "ls.LoadingScreen" then return end -- loading
    end
    for name in pairs(wmap) do
        if DECISION_PAGES[name] then st.movie = nil return end
    end

    -- (a) movies
    if movieShowing(widgets) then
        local m = st.movie
        if not m then
            m = { since = now, tries = 0, last = 0 }
            st.movie = m
        end
        -- 1st try: in-game key injection. Later tries (movie still up after
        -- RETRY_MS): ask the bridge to send ESCAPE through the SDL input shim
        -- (event "needs_skip_key"); se verified the shim's ESCAPE skips Bink
        -- movies. The bridge should re-check ui.state (screen "movie") before
        -- sending, since a stray ESCAPE in game opens the menu.
        if now - m.since > 400 and m.tries < K.MAX_TRIES and now - m.last > K.RETRY_MS then
            m.tries = m.tries + 1
            m.last = now
            if m.tries == 1 then
                press("ESCAPE")
                K.stats.movies = K.stats.movies + 1
                RPC.push("movie_skipped", {})
            else
                RPC.push("needs_skip_key", { key = "ESCAPE", reason = "movie", try = m.tries })
            end
        end
        return
    end
    st.movie = nil

    -- (b) dialogue lines
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

if not K.subscribed then
    K.subscribed = true
    Ext.Events.Tick:Subscribe(function()
        local ok, err = pcall(K.tick)
        if not ok then
            K.errors = (K.errors or 0) + 1
            if K.errors <= 5 then Ext.Utils.PrintWarning("[BG3AI] autoskip: " .. tostring(err)) end
        end
    end)
end

return K
