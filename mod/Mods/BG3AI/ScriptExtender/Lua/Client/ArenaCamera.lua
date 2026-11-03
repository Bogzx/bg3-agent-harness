-- arena.camera: the harness keeps the game's camera on an arena's fight (the recording's view). Harness only: refused
-- with a side and without an active arena. The runner calls it once a second (bg3harness.arena.match).
-- Live window 5 (ops/logs/live.md, load 3): the game has one GameCameraBehavior entity; writing TargetDestination +
-- PlayerInControl = true moves the view there and holds; DistanceDestination clamps to 12 (17 only in the game's
-- combat-focus mode); an attack's focus sets PlayerInControl false and moves the target, a re-write takes it back.
-- Clicks project through the camera (Client/Pick.lua), so nothing is written while a side's input is in flight.
BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local C = BG3AI.ArenaCamera or {}
BG3AI.ArenaCamera = C

C.SHIFT_M = 2.0        -- re-write when the camera's target is more than this from the fighters' centroid
C.MIN_D, C.MAX_D = 8, 12 -- distance range (12: the clamp outside the combat-focus mode)
C.SPREAD_PAD_M = 6     -- distance = the fighters' spread (farthest from the centroid) + this, within the range
C.INPUT_QUIET_MS = 2000 -- no write this soon after a side's last click step (RPC.sideInputAt)

local function comp(e, name)
    local ok, v = pcall(function() return e[name] end)
    return ok and v or nil
end

-- The game camera's behaviour component (the one GameCameraBehavior entity), nil when there is none.
function C.behavior()
    local ok, list = pcall(Ext.Entity.GetAllEntitiesWithComponent, "GameCameraBehavior")
    if not ok or type(list) ~= "table" or not list[1] then return nil end
    return comp(list[1], "GameCameraBehavior")
end

-- Positions of both teams' members that aren't dead (downed ones are still in the fight).
function C.fighters()
    local out = {}
    for _, s in ipairs({ "A", "B" }) do
        for _, u in ipairs(BG3AI.Arena.state.teams[s] or {}) do
            local e = Ext.Entity.Get(u)
            if e and comp(e, "Death") == nil then
                local ok, t = pcall(function() return e.Transform.Transform.Translate end)
                if ok and type(t) == "table" and tonumber(t[1]) then out[#out + 1] = { t[1], t[2], t[3] } end
            end
        end
    end
    return out
end

-- Why the camera must not move now: a side's client job is running or one of a side's click steps through the
-- camera (RPC.CAMERA_INPUT_OPS) ran within C.INPUT_QUIET_MS (a click is several ops: project, move the cursor, check
-- the hover, click); nil when free.
function C.inputBusy()
    for _, job in pairs(RPC.jobs) do
        if job.status == "running" and job.side then return "side " .. job.side .. "'s " .. tostring(job.kind) end
    end
    for s, t in pairs(RPC.sideInputAt) do
        if RPC.now_ms() - t < C.INPUT_QUIET_MS then return "side " .. s .. "'s input" end
    end
    return nil
end

local function dist(a, b)
    return math.sqrt((a[1] - b[1]) ^ 2 + (a[2] - b[2]) ^ 2 + (a[3] - b[3]) ^ 2)
end

RPC.register("arena.camera", function()
    if RPC.Ctx.side then error("arena.camera is the harness's (no side)", 0) end
    if not BG3AI.Arena.active() then error("no active arena", 0) end
    local busy = C.inputBusy()
    if busy then return { moved = false, skipped = busy } end
    local cam = C.behavior()
    if not cam then error("no game camera (GameCameraBehavior)", 0) end
    local pts = C.fighters()
    if #pts == 0 then return { moved = false, skipped = "no fighter in view" } end
    local c = { 0, 0, 0 }
    for _, p in ipairs(pts) do
        for i = 1, 3 do c[i] = c[i] + p[i] / #pts end
    end
    local spread = 0
    for _, p in ipairs(pts) do spread = math.max(spread, dist(p, c)) end
    local d = math.min(C.MAX_D, math.max(C.MIN_D, spread + C.SPREAD_PAD_M))
    -- Held only while the view is ours and on the fighters: the camera's own target, not our last write, is
    -- compared (live window 6 run 7: with PlayerInControl true the view slid 51 m off along its axis and stayed).
    local ok, td = pcall(function() local t = cam.TargetDestination return { t[1], t[2], t[3] } end)
    local on = ok and tonumber(td[1]) and tonumber(td[2]) and tonumber(td[3]) and dist(c, td) <= C.SHIFT_M
    if cam.PlayerInControl == true and on then return { moved = false, target = c, distance = d } end
    cam.TargetDestination = c
    cam.DistanceDestination = d
    cam.PlayerInControl = true
    return { moved = true, target = c, distance = d }
end)

return C
