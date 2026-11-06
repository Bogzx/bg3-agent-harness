-- Part of test_arena_lua.lua (split for the 1000-line cap): the tier's XP step of a mirror setup, on the helpers
-- test_arena_lua_mirror.lua shares as MIRROR.

local M = MIRROR
local stub, guarded, named, mirrorArena, mirrorSetup = M.stub, M.guarded, M.named, M.mirrorArena, M.mirrorSetup
local HOST, twin = M.HOST, M.twin

-- The tier's XP (G6): XP_FAKE is the host's total; a grant lands after `lag` ms (0: in its call).
local function xpGame(have, lag)
    twin(HOST, {})
    world[HOST].comps.Experience = { TotalExperience = have }
    stub("AddExplorationExperience", function(u, n)
        local calls = M.calls()
        calls[#calls + 1] = { "AddExplorationExperience", u, n }
        local land = function() world[HOST].comps.Experience.TotalExperience = have + n end
        if lag == nil then return end -- never lands
        tickers[#tickers + 1] = function() if clock >= lag then land() end end
    end)
end

test("tier XP: under the tier the host gets the difference before any twin exists, read back", guarded(function()
    local n = #tickers
    xpGame(428, 0)
    local j = mirrorSetup(mirrorArena({ level = 5, mirror = { party_xp = { total = 6500, below = 13000 },
        pairs = mirrorArena().mirror.pairs } }))
    for i = #tickers, n + 1, -1 do table.remove(tickers, i) end
    eq(j.step, 1, "at the party step: " .. tostring(j.error))
    eq(enc(j.xp), enc({ added = 6072, after = 6500, before = 428, below = 13000, total = 6500 }), "recorded")
    local first = {}
    for i, c in ipairs(M.calls()) do first[c[1]] = first[c[1]] or i end
    assert(first.AddExplorationExperience < first.CreateAt, "the XP before the first spawn")
    eq(#named("AddExplorationExperience"), 1, "one grant")
end))

test("tier XP: inside the window no call (L2 on a 428 XP save); above it, or a grant that never lands, fails setup",
    guarded(function()
        local pairs_ = mirrorArena().mirror.pairs
        xpGame(428, 0)
        local j = mirrorSetup(mirrorArena({ mirror = { party_xp = { total = 300, below = 900 }, pairs = pairs_ } }))
        eq(j.step, 1, "at the party step: " .. tostring(j.error))
        eq(#named("AddExplorationExperience"), 0, "no grant")
        eq(j.xp.added, 0, "recorded")
        local made = #named("CreateAt")
        xpGame(900, 0)
        j = mirrorSetup(mirrorArena({ mirror = { party_xp = { total = 300, below = 900 }, pairs = pairs_ } }))
        eq(j.status, "failed", "above the window")
        assert(j.error:find("no call takes XP away", 1, true) and #named("CreateAt") == made, j.error)
        xpGame(428, nil)
        j = mirrorSetup(mirrorArena({ level = 5, mirror = { party_xp = { total = 6500 }, pairs = pairs_ } }))
        eq(j.status, "failed", "never landed")
        assert(j.error:find("428 XP", 1, true) and #named("CreateAt") == made, j.error)
        for _, bad in ipairs({ { total = -1 }, { total = 900, below = 900 }, "x" }) do
            local ok = pcall(Ar.Setup.check_party_xp, bad)
            eq(ok, false, "party_xp refused")
        end
    end))
