-- Services, part 3 of 4 (Services.lua loads the parts in order; split for the 1000-line cap):
-- waypoints and fast travel.
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local U = BG3AI.Util
local SV = BG3AI.Services
local I = SV.inner
local hotbarDC, try, visibleWidget, waitJob = I.hotbarDC, I.try, I.visibleWidget, I.waitJob
local handleConfirm, selected, nightCamp, screenNow = I.handleConfirm, I.selected, I.nightCamp, I.screenNow

------------------------------------------------------------------------
-- Waypoints and fast travel (the map's waypoint list)
------------------------------------------------------------------------

local function mapWidget() return visibleWidget("JournalMap") end

-- { {region, name, button}, ... } from the open map, in display order.
local function mapWaypoints(w)
    local list = UI.byName(w, "Waypoints", { hidden = true })
    if not list then return {} end
    local out, region = {}, nil
    local buttons = UI.findAll(list, function(e) return UI.name(e) == "WaypointEntryButton" end, { prune = true })
    local bi = 0
    for _, entry in ipairs(UI.content(list, { max = 4000 })) do
        if entry.t then
            region = entry.t
        elseif entry.n == "WaypointEntryButton" then
            bi = bi + 1
            out[#out + 1] = { region = region, name = entry.b, button = buttons[bi], off = entry.off }
        end
    end
    if #out == 0 then
        -- Panel not laid out / collapsed: read each button's own text (hidden too),
        -- else its view model's name.
        for _, b in ipairs(buttons) do
            local parts = {}
            for _, t in ipairs(UI.findAll(b, function(e) return UI.typeName(e) == "TextBlock" end, { hidden = true, max = 300 })) do
                parts[#parts + 1] = UI.text(t)
            end
            local name = #parts > 0 and table.concat(parts, " ") or nil
            if not name then
                local vm = try(function() return b.DataContext end)
                for _, k in ipairs({ "Name", "DisplayName", "Title", "Label" }) do
                    local v = vm and UI.prop(vm, k)
                    if type(v) == "string" and v ~= "" then name = v break end
                end
            end
            if name then out[#out + 1] = { name = name, button = b } end
        end
    end
    return out
end

local function travelError(dc)
    local e = UI.path(dc or hotbarDC(), "CurrentPlayer.TravelError")
    if e == nil or e == "None" then return nil end
    return tostring(e)
end

-- The selected member's position: a living ClientControl holder first. A dead member can keep ClientControl next to
-- the one just selected (launch-v1 act1_level_3 r2, 2026-11-01): a trip measured on the corpse never "moved".
local function playerPos()
    local c
    for _, e in ipairs(try(Ext.Entity.GetAllEntitiesWithComponent, "ClientControl") or {}) do
        if U.comp(e, "Death") == nil then c = e break end
        c = c or e
    end
    local t = c and U.comp(c, "Transform")
    local v = t and try(function() return t.Transform.Translate end)
    return v and { v[1], v[2], v[3] } or nil
end

-- Opens the map if needed, runs fn(mapWidget, openedByUs, state) on a later tick.
-- Live S021 (x7): every fast_travel right after a successful one failed "the map didn't
-- open" after 3 s, and the call after that found the map already up (done in 0.5 s): the
-- M toggle was a step out of phase, so one press was swallowed. So: no map MAP_PRESS_MS
-- after a press -> press again (MAP_PRESSES in all). A game menu (a stray ESCAPE) eats M,
-- so that press is an ESCAPE instead. A map that opens slowly and gets closed by the
-- 2nd press is reopened by the 3rd (normal opens + travel take 0.5 s in all).
-- pre(job, state): runs each tick before the map is touched; nil = keep waiting, true = go on,
-- or "failed", result, err (fast_travel waits out a TravelError that clears after a load).
-- 5 (was 3): live 11:4x, right after a camp leave M was ignored for >3.6 s (the leave's transition)
local MAP_PRESS_MS, MAP_PRESSES = 1200, 5
-- S055 06:46:20: fast_travel "the map didn't open (pressed M,M,M,M,M over 6.2s)" in the Lower City, and check_all's
-- "screen map after closing the map (left open?)" before S052/S055: the Act 3 map takes longer than 1.2 s to show,
-- so the next M closed it again. A JournalMap widget that appeared after our press but isn't visible yet is the map
-- opening: wait for it up to MAP_OPENING_MS before pressing again.
SV.MAP_OPENING_MS = 4000
local function withMap(kind, timeout_ms, fn, pre)
    local state = { mapPresses = 0, mapKeys = {} }
    local function press()
        local k = visibleWidget("GameMenu") and "ESCAPE" or "M"
        if state.preexist == nil then state.preexist = UI.widget("JournalMap") ~= nil end
        Ext.Input.InjectKeyPress(k)
        state.mapPresses, state.mapPressT = state.mapPresses + 1, RPC.now_ms()
        state.mapKeys[#state.mapKeys + 1] = k
    end
    local opened = false
    local function begin()
        state.begun = true
        if not mapWidget() then
            press()
            opened = true
        end
    end
    if not pre then begin() end
    return waitJob(kind, timeout_ms, function(job)
        if not state.begun then
            local go, res, err = pre(job, state)
            if go == "failed" then return "failed", res, err end
            if not go then return nil end
            begin()
            return nil
        end
        if not state.ready then
            if not mapWidget() then
                -- A question box up instead of the map (vis live: a rest prompt; M then "didn't open the map"):
                -- report it, never answer it here.
                local A = BG3AI.UIActions
                local box = A and A.confirmBox and A.confirmBox()
                if box then
                    local labels = {}
                    for _, b in ipairs(box.buttons or {}) do labels[#labels + 1] = b.label end
                    local q = box.text or box.title or "?"
                    return "failed", { question = q, buttons = labels, unanswered = true, keys = state.mapKeys },
                        "the game asks: " .. q .. " [" .. table.concat(labels, "/") .. "] (instead of the map "
                        .. "opening): not answered; decide with confirm(...), then try again"
                end
                local opening = not state.preexist and UI.widget("JournalMap") ~= nil
                if opening then state.opening = true end
                if RPC.now_ms() - (state.mapPressT or 0) >= (opening and SV.MAP_OPENING_MS or MAP_PRESS_MS) then
                    if state.mapPresses >= MAP_PRESSES then
                        return "failed", { map_presses = state.mapPresses, keys = state.mapKeys, opening = state.opening },
                            "the map didn't open (pressed " .. table.concat(state.mapKeys, ",") .. " over " .. string.format("%.1f", (RPC.now_ms() - job.started) / 1000)
                            .. "s). On screen: " .. screenNow()
                    end
                    press()
                end
                return nil
            end
            state.ready = true
        end
        return fn(mapWidget(), opened, state)
    end)
end

local function closeMap(opened)
    if opened and mapWidget() then Ext.Input.InjectKeyPress("ESCAPE") end
end

-- check_all S11 (19:11:17 before S066, 19:56:53 before S068: "screen map after closing the map (left open?)", S068
-- started on the map): done went out on the tick of the ESCAPE, and that ESCAPE was swallowed. Done only once the map
-- is gone: none after SV.MAP_CLOSE_MS -> one more ESCAPE; still up after another -> done with map_open = true (the
-- list itself is good; the caller closes it).
SV.MAP_CLOSE_MS = 1500
RPC.register("ui.waypoints", function(args)
    local err = travelError()
    local out, closeT, escapes
    return withMap("ui.waypoints", 9000, function(w, opened, state)
        if not out then
            out = {}
            for _, wp in ipairs(mapWaypoints(w)) do
                out[#out + 1] = { region = wp.region, name = wp.name, off = wp.off }
            end
            closeMap(opened)
            closeT, escapes = RPC.now_ms(), opened and 1 or 0
        end
        local res = { waypoints = out, travel_blocked = err, map_presses = state.mapPresses, map_escapes = escapes }
        if not opened or not mapWidget() then return "done", res end
        if RPC.now_ms() - closeT < SV.MAP_CLOSE_MS then return nil end
        if escapes >= 2 then
            res.map_open = true
            return "done", res
        end
        Ext.Input.InjectKeyPress("ESCAPE")
        closeT, escapes = RPC.now_ms(), escapes + 1
        return nil
    end)
end)

-- screenshot(map=true) (S055 x2, S056 x3: M, screenshot, ESCAPE by hand; S056 07:17 a 2nd M closed the map and the
-- ESCAPE after it was refused): open the map like ui.waypoints does and leave it open for the picture;
-- ui.map_close closes it again (only if we opened it: the bridge passes that back).
RPC.register("ui.map_show", function(args)
    return withMap("ui.map_show", 8000, function(w, opened, state)
        return "done", { opened = opened, map_presses = state.mapPresses }
    end)
end)

RPC.register("ui.map_close", function(args)
    local had = mapWidget() ~= nil
    closeMap(true)
    return { closed = had }
end)

-- "Waypoint: Roadside-Cliffs" -> "roadside cliffs"
local function wpNorm(s)
    s = tostring(s or ""):lower():gsub("^%s*waypoint%s*[:%-]?%s*", ""):gsub("^wayp_", "")
    return (s:gsub("[^%w]+", " "):gsub("^%s+", ""):gsub("%s+$", ""))
end

-- WAYP_CHA_Top (get_map's id) -> its display name, when the client can read it.
local function wpDisplayName(id)
    if not tostring(id):match("^WAYP_") then return nil end
    local L = Ext.Loca
    local h = L and try(L.GetTranslatedStringKey, id)
    local name = h and (U.loca and U.loca(tostring(h)) or try(L.GetTranslatedString, tostring(h)))
    return (name and name ~= "") and name or nil
end

-- Waypoint by name, forgiving like a player reading the map: case, a
-- "Waypoint:" prefix, punctuation, partial words ("roadside", "cliffs"),
-- get_map ids (WAYP_CHA_Top). Returns wp, or nil + number of matches.
function SV.pickWaypoint(list, want)
    local names = { wpNorm(want) }
    local disp = wpDisplayName(want)
    if disp then table.insert(names, 1, wpNorm(disp)) end
    local function unique(pred)
        local hit, n = nil, 0
        for _, wp in ipairs(list) do
            if pred(wpNorm(wp.name)) then hit, n = wp, n + 1 end
        end
        return hit, n
    end
    local best = 0
    for _, w in ipairs(names) do
        if w ~= "" then
            local words = {}
            for x in w:gmatch("%S+") do words[#words + 1] = x end
            local tiers = {
                function(n) return n == w end,
                function(n) return n:find(w, 1, true) ~= nil or (#n >= 4 and w:find(n, 1, true) ~= nil) end,
                function(n)  -- every word of want starts a word of the name
                    for _, x in ipairs(words) do
                        if not (" " .. n):find(" " .. x, 1, true) then return false end
                    end
                    return true
                end,
            }
            for _, t in ipairs(tiers) do
                local hit, n = unique(t)
                if n == 1 then return hit end
                if n > 1 then return nil, n end
            end
            -- most shared words (≥3 letters), if one waypoint wins outright
            local top, topN, tie = nil, 0, false
            for _, wp in ipairs(list) do
                local n = " " .. wpNorm(wp.name) .. " "
                local c = 0
                for _, x in ipairs(words) do
                    if #x >= 3 and n:find(" " .. x, 1, true) then c = c + 1 end
                end
                if c > topN then top, topN, tie = wp, c, false elseif c == topN and c > 0 then tie = true end
            end
            if top and not tie then return top end
            if tie then best = math.max(best, 2) end
        end
    end
    return nil, best
end
local pickWaypoint = SV.pickWaypoint

-- "Overgrown Ruins (Wilderness), Roadside Cliffs (Wilderness)"
local function waypointNames(list)
    local out = {}
    for _, x in ipairs(list) do
        out[#out + 1] = tostring(x.name) .. (x.region and (" (" .. tostring(x.region) .. ")") or "")
    end
    return table.concat(out, ", ")
end
SV.waypointNames = waypointNames

-- What blocks travel right now (camp leave / fast travel), read like the map's greyed-out button:
-- {error = TravelError or nil, in_camp, dialogue, screen}. Live 2026-10-10 (LIVETEST-setup, a save taken
-- right after a long rest): camp leave failed "cannot travel right now" with no reason given.
RPC.register("ui.travel_block", function(args)
    local dc = hotbarDC()
    return {
        error = travelError(dc),
        in_camp = selected(dc, "PlayerCharacterProperties.IsInCamp") == true,
        night_camp = nightCamp(dc) or nil,
        hud = visibleWidget("HotBar") ~= nil,   -- false during the morning transition after a rest (live 11:3x)
        dialogue = visibleWidget("Dialogue") ~= nil,
        screen = screenNow(),
    }
end)

-- Questions fast_travel must never answer itself: resting/camp decisions (vis live 2026-10-10: a rest prompt was
-- answered during a fast travel). Box texts may read "[ForceUpdate]" (formatter-driven): those are answered as
-- before but listed by name in the result.
function SV.travelVeto(q)
    local t = tostring(q or ""):lower()
    for _, w in ipairs({ "rest", "sleep", "end the day", "camp", "supplies" }) do
        if t:find("%f[%a]" .. w .. "%f[%A]") then return "a rest/camp decision is yours, not fast_travel's" end
    end
    return nil
end

-- Right after a load (live 2026-10-10, LIVETEST-se / -setup at the red-forest camp) TravelError reads
-- "Unavailable" for a few seconds and then clears: wait it out instead of failing the call.
SV.FT_UNAVAILABLE_WAIT_MS = 8000

RPC.register("ui.fast_travel", function(args)
    local want = args.waypoint or error("waypoint required")
    local dc = hotbarDC()
    local err = travelError(dc)
    if err and err ~= "Unavailable" then error("can't fast travel now (" .. err .. ")", 0) end
    if err and nightCamp(dc) then
        error("can't fast travel now (Unavailable): the night at camp hasn't ended (a long rest that didn't "
            .. "finish): long_rest goes to sleep and ends it", 0)
    end
    if selected(dc, "CharacterMode") == "InCombat" then error("can't fast travel in combat", 0) end
    -- Live 2026-10-10: a fast travel from camp moved only the controlled character (companions and
    -- the camera stayed at the Campsite, PARTY APART 1800 m). The bridge leaves camp first.
    if selected(dc, "PlayerCharacterProperties.IsInCamp") == true and not args.from_camp then
        error("at camp: leave camp first (camp leave), then fast travel", 0)
    end
    if args.dry then return { can = true, waiting = err } end

    local startPos
    local function pre(job, state)
        local e = travelError()
        if e == nil then
            startPos = playerPos()
            state.unavailableMs = err and (RPC.now_ms() - job.started) or nil
            return true
        end
        if e ~= "Unavailable" then return "failed", nil, "can't fast travel now (" .. e .. ")" end
        if RPC.now_ms() - job.started >= SV.FT_UNAVAILABLE_WAIT_MS then
            return "failed", { travel_blocked = e }, "can't fast travel now (Unavailable for "
                .. string.format("%.0f", SV.FT_UNAVAILABLE_WAIT_MS / 1000) .. " s: the game blocks travel here; "
                .. "walk (travel_to) or try again after moving)"
        end
        return nil
    end
    return withMap("ui.fast_travel", 58000 + SV.FT_UNAVAILABLE_WAIT_MS, function(w, opened, state)
        if not state.sent then
            local list = mapWaypoints(w)
            -- The list fills a few frames after the map opens (live S007: an empty
            -- list right after M -> "no unlocked waypoint named 'Roadside Cliffs'").
            state.listT = state.listT or RPC.now_ms()
            if #list == 0 and RPC.now_ms() - state.listT < 2000 then return nil end
            local wp, n = pickWaypoint(list, want)
            if not wp then
                closeMap(opened)
                local names = {}
                for _, x in ipairs(list) do names[#names + 1] = x.name end
                local msg
                if #list == 0 then
                    msg = "the map shows no waypoint list (UI not loaded); try again, or walk (travel_to marker/objective)"
                elseif n and n > 1 then
                    msg = "'" .. want .. "' matches " .. n .. " waypoints; be more specific. Unlocked: " .. waypointNames(list)
                else
                    msg = "no unlocked waypoint named '" .. want .. "'. Unlocked: " .. waypointNames(list)
                end
                return "failed", { waypoints = names }, msg
            end
            local cmd = wp.button and UI.prop(wp.button, "Command")
            local vm = wp.button and try(function() return wp.button.DataContext end)
            local ok, e2 = UI.execute(cmd, vm, false)
            if not ok then
                closeMap(opened)
                return "failed", nil, "couldn't travel to " .. wp.name .. ": " .. tostring(e2)
            end
            state.sent, state.target, state.t = true, wp, RPC.now_ms()
            return nil
        end
        local c, cres, cerr = handleConfirm(state, args, nil, SV.travelVeto)
        if c == "failed" then
            if not (cres and cres.needs_click) then closeMap(opened) end
            return "failed", cres, cerr
        end
        if c then state.t = RPC.now_ms() return nil end
        -- Arrived once the party moved and the game is running again.
        local gs = tostring(try(Ext.Utils.GetGameState) or "?")
        if gs ~= "Running" then state.loading = true return nil end
        local pos = playerPos()
        local moved = startPos and pos and U.dist(startPos, pos) > 3
        if moved or (state.loading and RPC.now_ms() - state.t > 2000) then
            if mapWidget() then Ext.Input.InjectKeyPress("ESCAPE") end
            return "done", { arrived = state.target.name, region = state.target.region, confirmed = state.confirms,
                waited_unavailable_ms = state.unavailableMs }
        end
        local te = travelError()
        if te and RPC.now_ms() - state.t > 1000 then
            closeMap(opened)
            return "failed", nil, "travel refused (" .. te .. ")"
        end
        return nil
    end, pre)
end)
