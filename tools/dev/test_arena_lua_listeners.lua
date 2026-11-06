-- Part of test_arena_lua.lua (split for the 1000-line cap): listener-time code, whose side is never the op's an event
-- fires inside of. test_arena_lua.lua runs it after its stubs and helpers, shared as globals.

local LADDER = "acacacac-0000-0000-0000-000000000008"

-- B1 re-climbs a ladder: A.LOOP_USES UseFinished with its Osiris tasks still queued.
local function loop(side)
    world[LADDER] = { pos = { 10, 0, 1 }, comps = {} }
    world[B1].comps.ServerCharacter = { OsirisController = { Tasks = { {} } } }
    BG3AI.Actions.use_streak = {}
    local function uses()
        for _ = 1, BG3AI.Actions.LOOP_USES do fire("UseFinished", B1, LADDER, 1) end
    end
    if side then as(side, uses) else uses() end
end

test("a loop stopped at listener time is the stuck character's side's event", function()
    begin()
    local s0 = RPC.eventSeq
    loop(nil)
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}", "B"), "loop_stopped"), 1, "B's")
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}", "A"), "loop_stopped"), 0, "A's")
end)

test("an event fired inside the other side's op: the loop is still found and the stuck character's side's", function()
    begin()
    local s0 = RPC.eventSeq
    loop("A")
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}", "B"), "loop_stopped"), 1, "B's")
    eq(#kinds(rpc("events.since", "{seq=" .. s0 .. "}", "A"), "loop_stopped"), 0, "A's")
end)

test("a referee record made inside a side's op is the harness's (both teams are the party)", function()
    begin()
    as("A", fire, "Died", B1)
    local d = refs("died")
    eq(#d, 1, "died records")
    eq(d[1].party, true, "B1 is party for the harness")
end)

test("a roll logged inside a side's op is the harness's (both teams are the party)", function()
    begin()
    local L = BG3AI.RollLog
    local n = #L.ring
    as("A", fire, "RollResult", "ev", B1, GOB, 1, 1, 0)
    eq(#L.ring, n + 1, "logged")
    eq(L.ring[#L.ring].party, true, "B1's roll")
end)

test("turn_started / turn_ended carry the round the turn started in, in the live round-change order", function()
    begin()
    fire("CombatRoundStarted", "combat1", "1")
    for _, u in ipairs({ A1, B1 }) do fire("TurnStarted", "S_Player_" .. u) end
    local s0 = RPC.eventSeq
    fire("CombatRoundStarted", "combat1", "2")
    for _, u in ipairs({ A1, B1 }) do fire("TurnStarted", "S_Player_" .. u) end
    for _, u in ipairs({ A1, B1 }) do fire("TurnEnded", "S_Player_" .. u) end
    for _, x in ipairs({ { "A", A1 }, { "B", B1 } }) do -- each side's own member (the other is out of sight here)
        local got = {}
        for _, ev in ipairs(rpc("events.since", "{seq=" .. s0 .. "}", x[1]).result.events) do
            if ev.kind == "turn_started" or ev.kind == "turn_ended" then
                got[#got + 1] = ev.kind .. " " .. ev.data.uuid .. " " .. tostring(ev.data.round)
            end
        end
        eq(table.concat(got, "; "), "turn_started " .. x[2] .. " 2; turn_ended " .. x[2] .. " 1", x[1] .. "'s events")
    end
end)
