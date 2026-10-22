-- Offline tests for Client/UIState.lua's ui.state, the read behind every get_status and every busy check (315 calls
-- in the recorded fixtures): loading and movie screens, modal priority, blocking, the panels besides the HUD, a
-- popup's decision vs dismiss, game over, the subtitle and region lines. test_save.lua covers our own save's menus
-- and the credits/splash/main-menu screens.
-- Ext, RPC and UI are stubbed (pages are plain tables); UIState.lua is real.
-- Run: tools/dev/test_ui_state.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local gameState, pages, region, skipper, movie -- reset per test
Ext = { Utils = { GetGameState = function() return gameState end } }

local function page(content, extra)
    local el = { visible = true, content = content or {} }
    for k, v in pairs(extra or {}) do el[k] = v end
    return el
end

BG3AI = {
    RPC = { register = function() end, now_ms = function() return 0 end },
    UI = {
        widgetMap = function()
            local m = {}
            for n, el in pairs(pages) do m[n] = { el = el } end
            return m
        end,
        isVisible = function(el) return el.visible ~= false end,
        content = function(el) return el.content or {} end,
        cachedByName = function(w, n) if w == "Minimap" and n == "LocationHolder" then return region end end,
        allText = function(e) return e.text end,
        prop = function(obj, k) return obj and obj[k] end,
        props = function() return {} end,
        clean = function(v) return v end,
        get = function() return nil end,
        findAll = function() return {} end,
    },
}
dofile(LUA .. "/Client/UIState.lua")
local S = BG3AI.UIState

local failures, count = 0, 0
local function test(name, fn)
    count = count + 1
    gameState, pages, region, skipper, movie = "Running", { HotBar = page(), Minimap = page() }, nil, nil, nil
    BG3AI.AutoSkip = { view = function() return skipper or { on = true, skipped = 0 } end,
        probeMovie = function() return movie end }
    local ok, err = pcall(fn)
    if ok then print("ok   " .. name) else failures = failures + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b, what)
    if a ~= b then error((what or "value") .. ": expected " .. tostring(b) .. ", got " .. tostring(a), 2) end
end
local function state(args) return S.state(args or {}) end

test("exploring: screen game, not blocking, the region line, no panels", function()
    region = { text = "Blighted Village" }
    local st = state()
    eq(st.screen, "game", "screen")
    eq(st.blocking, nil, "blocking")
    eq(st.region, "Blighted Village", "region")
    eq(st.panels_open, nil, "panels_open")
    eq(st.autoskip, nil, "autoskip shown only when off or it skipped")
end)

test("a load: no pages, or a game state other than Running/Paused/Menu, is loading and blocking", function()
    pages = {}
    local st = state()
    eq(st.screen, "loading", "no pages")
    eq(st.blocking, true, "blocking")
    pages = { HotBar = page() }
    gameState = "LoadLevel"
    st = state()
    eq(st.screen, "loading", "LoadLevel")
    eq(st.game_state, "LoadLevel", "game_state")
    gameState = "Menu"
    pages = {}
    eq(state().screen, "main_menu", "Menu with no pages")
end)

test("a movie: over the loading screen (AutoSkip's probe), or only the movie-safe widgets while Running", function()
    gameState, movie = "LoadLevel", true
    eq(state().screen, "movie", "intro over the load")
    gameState, movie = "Running", nil
    pages = { ScreenFade = page(), AlwaysOnTopOverlay = page() }
    local st = state()
    eq(st.screen, "movie", "Bink movie")
    eq(st.blocking, true, "blocking")
end)

test("modal priority: a reaction prompt over the dialogue is the reaction", function()
    pages.Dialogue = page()
    pages.ReactionDecisionPopup = page()
    local real = S.reaction
    S.reaction = function() return { prompt = "Shield?" } end -- its content is not under test here
    local st = state()
    S.reaction = real
    eq(st.screen, "reaction", "screen")
    eq(st.page, "ReactionDecisionPopup", "page")
    eq(st.reaction.prompt, "Shield?", "the reaction's content")
    eq(st.blocking, true, "blocking")
end)

test("a hidden page doesn't count", function()
    pages.Dialogue = page(nil, { visible = false })
    eq(state().screen, "game", "screen")
end)

test("panels besides the HUD are listed, sorted; the HUD and the screen's own page are not", function()
    pages.Inventory = page()
    pages.Character = page()
    pages.CombatLog = page()
    pages.JournalMap = page()
    local st = state()
    eq(st.screen, "map", "screen")
    eq(table.concat(st.panels_open, ","), "Character,Inventory", "panels_open")
end)

test("a popup with two buttons is a decision; with one it is dismissable", function()
    pages.Dialog_box = page({ { t = "Leave the area?" }, { t = "Your party will follow." },
        { b = "Yes" }, { b = "No" } })
    local st = state()
    eq(st.screen, "popup", "screen")
    eq(st.popup.title, "Leave the area?", "title")
    eq(st.popup.text, "Your party will follow.", "text")
    eq(st.popup.decision, true, "decision")
    eq(st.popup.dismissable, nil, "dismissable")
    pages.Dialog_box = page({ { t = "Item received" }, { b = "OK" } })
    st = state()
    eq(st.popup.dismissable, true, "dismissable")
    eq(st.popup.decision, nil, "decision")
end)

test("a party wipe's box (load buttons, no way back) is game_over", function()
    pages.Dialog_box = page({ { t = "Your party has fallen" }, { b = "Load Last Save" }, { b = "Main Menu" } })
    local st = state()
    eq(st.screen, "game_over", "screen")
    eq(st.blocking, true, "blocking")
    pages.Dialog_box = page({ { t = "Quit?" }, { b = "Load Game" }, { b = "Cancel" } })
    eq(state().screen, "popup", "a box with Cancel is not game over")
end)

test("a split-stack box says split", function()
    pages.SplitItem = page({ { t = "Split" }, { b = "Confirm" } })
    eq(state().popup.split, true, "split")
end)

test("the subtitle line from OverheadInfo's DataContext", function()
    pages.OverheadInfo = page(nil, { DataContext = { CurrentSubtitle = "Help me!", CurrentSpeaker = "Mol" } })
    local st = state()
    eq(st.subtitle.text, "Help me!", "text")
    eq(st.subtitle.speaker, "Mol", "speaker")
    eq(st.screen, "game", "the HUD's OverheadInfo is no panel")
end)

test("autoskip off (a run setting) is reported", function()
    skipper = { on = false, skipped = 0 }
    local st = state()
    eq(st.autoskip and st.autoskip.on, false, "autoskip.on")
end)

print(string.format("%d/%d passed", count - failures, count))
if failures > 0 then os.exit(1) end
