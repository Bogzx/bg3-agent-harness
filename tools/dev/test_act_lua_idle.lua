-- Part of test_act_lua.lua (split for the 1000-line cap): the after-a-job teleport watch (ActionsMove A.idle_tick).
-- test_act_lua.lua runs it after its stubs and helpers, which it shares as globals.

local function last_event(kind, since)
    for i = #RPC.events, 1, -1 do
        local ev = RPC.events[i]
        if ev.seq <= (since or 0) then return nil end
        if ev.kind == kind then return ev end
    end
end

-- BACKLOG:1743, live N8 step 3 (N8-3-samples.json): the move_to ended at +0.87 s at (-772.08,-28.26,-726.07), y never
-- moved, and at +4.52 s the game put Claude at (-758.86,-25.93,-727.08) in one 77 ms tick, with no job running.
local function count_events(kind, since)
    local n = 0
    for _, ev in pairs(RPC.events) do if ev.kind == kind and ev.seq > since then n = n + 1 end end
    return n
end
local function n8_put_back(after_ms)
    world[P1].pos = { -773.71, -28.26, -727.41 }
    local j = job(op("move_to", { char = P1, pos = { -772.08, -28.26, -726.07 } }))
    arrive(j, { -772.08, -28.26, -726.07 })
    eq(j.status, "done", "the move ended " .. tostring(j.error))
    local t_end = clock
    while clock - t_end < after_ms do tick() end
    world[P1].pos = { -758.86, -25.93, -727.08 }
    tick()
end

test("N8 step 3: put 13.5 m off 3.65 s after its move_to ended, nothing of ours running = the game's", function()
    local seq = RPC.eventSeq
    n8_put_back(3650)
    local ev = last_event("teleported_by_game", seq)
    assert(ev, "no teleported_by_game event")
    eq(ev.data.uuid, P1, "uuid"); near(ev.data.m, 13.5, "m")
    assert(ev.data.why:find("^3%.[67] s after its move_to ended, with no order of ours running"), ev.data.why)
    tick(5)
    eq(count_events("teleported_by_game", seq), 1, "said once")
end)

test("idle watch: a jump IDLE_WATCH_MS after the job ended, or two members jumping at once, isn't reported", function()
    local seq = RPC.eventSeq
    n8_put_back(A.IDLE_WATCH_MS + 500)
    eq(last_event("teleported_by_game", seq), nil, "past the watch")
    reset()
    party_rows = { P1, P2 }
    world[P2] = { pos = { -770, -28.26, -726 }, comps = {} }
    seq = RPC.eventSeq
    world[P1].pos = { -773.71, -28.26, -727.41 }
    local j = job(op("move_to", { char = P1, pos = { -772.08, -28.26, -726.07 } }))
    arrive(j, { -772.08, -28.26, -726.07 })
    tick(10)
    world[P1].pos, world[P2].pos = { 100, 0, 0 }, { 102, 0, 0 } -- a fast travel / a load: the whole party at once
    tick()
    eq(last_event("teleported_by_game", seq), nil, "a party-wide move")
end)

test("idle watch: a jump while the member's next job runs is that job's (one event, not two)", function()
    local seq = RPC.eventSeq
    world[P1].pos = { 0, 0, 0 }
    local j1 = job(op("move_to", { char = P1, pos = { 2, 0, 0 } }))
    arrive(j1, { 2, 0, 0 })
    tick(3)
    local j2 = job(op("move_to", { char = P1, pos = { 3, 0, 0 } }))
    tick()
    world[P1].pos = { 2.5, -22, 0 } -- S063's ritual drop mid-walk: the game's at once (teleport_check)
    tick()
    eq(j2.status, "interrupted", "status"); eq(j2.result.by_game, true, "by_game")
    tick(5)
    eq(count_events("teleported_by_game", seq), 1, "one event")
end)
