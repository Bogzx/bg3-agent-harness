-- BG3AI memory watch, loaded in both the server and the client Lua context.
--
-- 2026-10-10 20:48 (S038, Shadowfell): the game's RSS went 5.0 -> 13.3 GB in ~30 s, the kernel OOM-killed it and
-- systemd took every agent down with it. The server thread froze inside one call (nav.landing from the bridge, or
-- the ways warm-up 0.3 s after the walk ended: both run the engine's FindPath from the same spot); the client
-- thread kept ticking. Nothing in the bg3le log said which call or whose memory. Now:
--   * every minute each context logs its Lua heap ("[BG3AI] mem: server lua 41.2 MB (peak 55.0)"): the bg3le log is
--     written live and survives a crash, so a runaway reads as Lua (heap climbing) or engine (heap flat);
--   * a heap jump of SPIKE_MB between two 1 s checks is logged at once, with the op that ran;
--   * past CAP_MB (after a full collect) M.high is set and logged loudly: non-essential work (warm-ups, index
--     rebuilds) checks M.paused() and skips; it resumes below RESUME_MB;
--   * M.crumb(label) prints a breadcrumb before risky work (bridge ops that pathfind, the warm-up), deduped, so a
--     hang leaves the last thing the server started as the log's last [BG3AI] line;
--   * sys.mem returns the numbers (the bridge's sampler writes them into Sxxx.metrics.jsonl each minute).

BG3AI = BG3AI or {}
local M = BG3AI.Mem or {}
BG3AI.Mem = M

local CONTEXT = Ext.IsServer() and "server" or "client"
M.CAP_MB = 512
M.RESUME_MB = 384
M.SPIKE_MB = 64
M.CHECK_MS = 1000
M.LOG_EVERY_MS = 60000
M.CRUMB_REPEAT_MS = 10000

M.state = M.state or { t = 0, logged = 0, mb = 0, peak = 0, high = false, trips = 0 }

local function now_ms() return Ext.Utils.MonotonicTime() end
local function fmt(x) return string.format("%.1f", x) end

function M.heap_mb() return collectgarbage("count") / 1024 end

-- true while the heap is over the cap: callers skip work nobody asked for.
function M.paused() return M.state.high == true end

M.lastCrumb = M.lastCrumb or { label = nil, t = -1e9 }
M.crumbSeen = M.crumbSeen or {}
M.crumbN = M.crumbN or 0
-- Prints `label` unless the same label printed within CRUMB_REPEAT_MS; returns true when printed.
function M.crumb(label)
    local t = now_ms()
    M.lastCrumb.label, M.lastCrumb.t = label, t
    local seen = M.crumbSeen
    if seen[label] and t - seen[label] < M.CRUMB_REPEAT_MS then return false end
    if M.crumbN >= 256 then seen = {}; M.crumbSeen = seen; M.crumbN = 0 end
    if not seen[label] then M.crumbN = M.crumbN + 1 end
    seen[label] = t
    Ext.Utils.Print("[BG3AI] " .. CONTEXT .. "> " .. label)
    return true
end

-- One check (the Tick calls it every CHECK_MS; tests call it with a heap value).
function M.check(t, mb)
    local st = M.state
    mb = mb or M.heap_mb()
    local prev = st.mb
    st.mb = mb
    if mb > st.peak then st.peak = mb end
    if prev > 0 and mb - prev >= M.SPIKE_MB then
        Ext.Utils.PrintWarning(string.format("[BG3AI] mem: %s lua heap +%s MB in 1 s (%s -> %s MB), last: %s",
            CONTEXT, fmt(mb - prev), fmt(prev), fmt(mb), tostring(M.lastCrumb.label)))
    end
    if not st.high and mb > M.CAP_MB then
        collectgarbage("collect")
        mb = M.heap_mb()
        st.mb = mb
        if mb > M.CAP_MB then
            st.high, st.trips = true, st.trips + 1
            Ext.Utils.PrintError(string.format(
                "[BG3AI] MEMGUARD: %s Lua heap %s MB > cap %d MB after a full collect: warm-ups and index rebuilds"
                .. " paused (last: %s)", CONTEXT, fmt(mb), M.CAP_MB, tostring(M.lastCrumb.label)))
        end
    elseif st.high and mb < M.RESUME_MB then
        st.high = false
        Ext.Utils.Print(string.format("[BG3AI] MEMGUARD: %s Lua heap back to %s MB: resumed", CONTEXT, fmt(mb)))
    end
    if t - st.logged >= M.LOG_EVERY_MS then
        st.logged = t
        Ext.Utils.Print(string.format("[BG3AI] mem: %s lua %s MB (peak %s)%s", CONTEXT, fmt(mb), fmt(st.peak),
            st.high and " PAUSED" or ""))
    end
    return st
end

if not M.tickSubscribed then
    M.tickSubscribed = true
    Ext.Events.Tick:Subscribe(function()
        local t = now_ms()
        if t - M.state.t < M.CHECK_MS then return end
        M.state.t = t
        local ok, err = pcall(M.check, t)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] mem check: " .. tostring(err)) end
    end)
end

if BG3AI.RPC then
    BG3AI.RPC.register("sys.mem", function()
        local st = M.state
        return { context = CONTEXT, lua_mb = math.floor(M.heap_mb() * 10 + 0.5) / 10,
            peak_mb = math.floor(st.peak * 10 + 0.5) / 10, high = st.high or nil, trips = st.trips,
            cap_mb = M.CAP_MB }
    end)
end

return M
