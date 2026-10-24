-- Offline tests for Server/Referee.lua: the hidden referee ring (B-mod task 1).
-- Event names, arities and argument shapes are the live ones from research/ovh/live1.md §6
-- (runtime/probes/live1/ref_events.jsonl). Ext, Osi, RPC and Visibility are stubbed; Shared/Util.lua is real.
-- Run: tools/dev/test_referee_lua.sh  (Lua 5.3 + 5.4 in Docker)

local LUA = arg[1] or "mod/Mods/BG3AI/ScriptExtender/Lua"

local HOST = "14e68d14-c5a2-1e68-d51f-c2fe2defd545"
local SH = "aaaaaaaa-0000-0000-0000-000000000002"
local TRADER = "e1fda08b-45d4-41db-84b5-20e9931ba5af"
local GOB = "b1e2d6a7-0fad-4485-ae2e-2ec78ce4e658"
local CHEST = "cccccccc-0000-0000-0000-000000000003"
local GUARD = "825714dd-7df9-4cd1-aecb-edf577baa487"
local NULL = "NULL_00000000-0000-0000-0000-000000000000"
local P = "HalfOrcs_Female_Player_" .. HOST
local T = "S_Trader_" .. TRADER

local listeners, ops, pushed, ticks = {}, {}, {}, {}
local clock = 1000
local gold = 100
local dead, items, traders = {}, { [CHEST] = true }, {}
local names = { [HOST] = "Tav", [SH] = "Shadowheart", [TRADER] = "Arron", [GOB] = "Goblin", [CHEST] = "Chest",
    [GUARD] = "Guard" }
local stacks = {}

Ext = {
    Osiris = { RegisterListener = function(name, arity, _, fn) listeners[name .. "/" .. arity] = fn end },
    Utils = { MonotonicTime = function() return clock end, GameTime = function() return clock / 1000 end,
        PrintWarning = function(m) print("WARN " .. m) end, Print = function() end },
    Events = { Tick = { Subscribe = function(_, fn) ticks[#ticks + 1] = fn end } },
}
Osi = {
    PartyGetGold = function() return gold end,
    GetStackAmount = function(i) return stacks[i] or 1 end,
    IsCharacter = function(u) return (names[u] and not items[u]) and 1 or 0 end,
    IsItem = function(u) return items[u] and 1 or 0 end,
    IsDead = function(u) return dead[u] and 1 or 0 end,
    CanTrade = function(u) return traders[u] and 1 or 0 end,
    CombatGetGuidFor = function() return nil end,
    GetTemplate = function() return "TPL_Ring_" .. "dddddddd-0000-0000-0000-000000000001" end,
}
local party = { HOST, SH }
BG3AI = {
    RPC = { push = function(kind, data) pushed[#pushed + 1] = { kind = kind, data = data } end,
        register = function(name, fn) ops[name] = fn end, QUIET_OPS = {}, BOOT = "boot-1" },
    Visibility = {
        osi = function(fn, ...) local ok, r = pcall(fn, ...) if ok then return r end return nil end,
        uuid = function(x) return type(x) == "string" and x:match("%x+%-%x+%-%x+%-%x+%-%x+$") or nil end,
        party = function()
            local seen = {}
            for _, u in ipairs(party) do seen[u] = true end
            return party, seen
        end,
        name = function(u) return names[u] or ("item " .. u:sub(1, 4)) end,
    },
}
dofile(LUA .. "/Shared/Util.lua")
dofile(LUA .. "/Server/Referee.lua")
local R = BG3AI.Referee

local failures, passed = 0, 0
local function check(name, cond, extra)
    if cond then passed = passed + 1 else
        failures = failures + 1
        print("FAIL " .. name .. (extra and (": " .. tostring(extra)) or ""))
    end
end
local function fire(key, ...)
    local fn = listeners[key]
    assert(fn, "no listener " .. key)
    clock = clock + 1
    fn(...)
end
local function since(seq) return ops["ref.since"]({ seq = seq or 0 }).events end
local function recs(ev)
    local out = {}
    for _, r in ipairs(R.ring) do if r.ev == ev then out[#out + 1] = r end end
    return out
end
local function lastOf(ev) local l = recs(ev) return l[#l] end
local function item(tag, n) return string.format("%s_%08x-0000-0000-0000-000000000000", tag, n) end

-- Listeners are registered at the live arities, and the quiet op list knows the poll.
for _, key in ipairs({ "RequestTrade/4", "TradeEnds/2", "GoldChanged/2", "AddedTo/3", "TemplateAddedTo/4",
    "RemovedFrom/2", "RequestPickpocket/2", "CharacterPickpocketFailed/2", "CrimeIsRegistered/8", "KilledBy/4",
    "AttackedBy/7", "Died/1", "CombatStarted/1", "CombatEnded/1", "CombatRoundStarted/2", "EnteredCombat/2", "LeftCombat/2" }) do
    check("listens " .. key, listeners[key] ~= nil)
end
check("ref.since quiet", BG3AI.RPC.QUIET_OPS["ref.since"] == true)

-- Self-check before anything fired: every listener is in `never`.
do
    local s = ops["ref.snapshot"]({})
    check("snapshot boot", s.boot == "boot-1")
    check("all never fired", #s.never >= 16, #s.never)
end

-- Buy (live1 'buy-shov'): RemovedFrom(item, trader) -> AddedTo(item, player); gold -1 player / +1 trader.
do
    fire("RequestTrade/4", P, T, 3, "")
    local o = lastOf("trade_open")
    check("trade_open", o and o.npc == "Arron" and o.npc_uuid == TRADER and o.char == "Tav")
    local shovel = item("TOOL_GEN_Shovel_A", 1)
    fire("RemovedFrom/2", shovel, T)
    fire("AddedTo/3", shovel, P, "Regular")
    fire("TemplateAddedTo/4", "TOOL_GEN_Shovel_A_73efd1f7-5616-4ca8-84e6-6128aa6efaea", shovel, P, "Regular")
    gold = 99
    fire("GoldChanged/2", P, -1)
    fire("GoldChanged/2", T, 1)
    local a = lastOf("item_added")
    check("bought item_added", a and a.bought == true and a.from_uuid == TRADER and a.in_trade, a and a.from)
    check("tpl patched", a and a.tpl == "73efd1f7-5616-4ca8-84e6-6128aa6efaea", a and a.tpl)
    local g = lastOf("gold")
    check("gold -1", g and g.gold == 99 and g.delta == -1, g and g.delta)
    check("trader gold not a party gold record", #recs("gold") == 1)
    fire("TradeEnds/2", P, T)
    local e = lastOf("trade_end")
    check("trade_end paired", e and #e.bought == 1 and #e.sold == 0 and e.gold_out == 1 and e.gold_in == 0)
end

-- Sell (live1 'sell'): RemovedFrom(item, player) -> AddedTo(item, trader); gold +1 player.
do
    fire("RequestTrade/4", P, T, 3, "")
    local tablet = item("BOOK_Tablet", 2)
    fire("RemovedFrom/2", tablet, P)
    fire("AddedTo/3", tablet, T, "Regular")
    gold = 100
    fire("GoldChanged/2", T, -1)
    fire("GoldChanged/2", P, 1)
    fire("TradeEnds/2", P, T)
    local e = lastOf("trade_end")
    check("sold paired", e and #e.sold == 1 and e.sold[1].name == "item " .. tablet:match("%x+%-"):sub(1, 4)
        and e.gold_in == 1, e and #e.sold)
    check("sold item is no item_added", #recs("item_added") == 1)
    check("gold +1", lastOf("gold").delta == 1)
    check("no trade open after end", R.trade == nil)
end

-- Gold moved between party members nets out; a split (delta 0) is ignored.
do
    local n = #recs("gold")
    fire("GoldChanged/2", P, -10) -- party total unchanged (100)
    fire("GoldChanged/2", "S_Player_ShadowHeart_" .. SH, 10)
    fire("GoldChanged/2", P, 0)
    check("party transfer: no gold record", #recs("gold") == n, #recs("gold") - n)
    local mv = item("LOOT_Gold_A", 3)
    fire("RemovedFrom/2", mv, P)
    fire("AddedTo/3", mv, "S_Player_ShadowHeart_" .. SH, "Regular")
    check("party move: no item_added", #recs("item_added") == 1)
end

-- Pickpocket: failure (live1 pp4), then success through the item pairing (no Success event seen live).
do
    fire("RequestPickpocket/2", HOST, TRADER)
    fire("CharacterPickpocketFailed/2", P, T)
    fire("CrimeIsRegistered/8", T, "PickPocketFailed", 126, NULL, P, NULL, NULL, NULL)
    fire("CrimeIsRegistered/8", "Elf_" .. GUARD, "Guard_NPCInterrogate_Fallback", 127, NULL, T, NULL, NULL, NULL)
    local f = lastOf("pickpocket")
    check("pickpocket failed", f and f.ok == false and f.npc == "Arron")
    local c = recs("crime")
    check("party crime only", #c == 1 and c[1].type == "PickPocketFailed" and c[1].victim == "Arron"
        and c[1].criminal == "Tav" and c[1].id == 126, #c)
    local ring = item("RING", 4)
    stacks[ring] = 1
    fire("RemovedFrom/2", ring, T)
    fire("AddedTo/3", ring, P, "Regular")
    local s = lastOf("pickpocket")
    check("pickpocket ok", s and s.ok == true and s.npc_uuid == TRADER and s.count == 1, s and tostring(s.ok))
    -- a Success event for an item counts once (the pairing skips it)
    local ring2 = item("RING", 5)
    fire("CharacterPickpocketSuccess/6", P, T, ring2, NULL, 1, 0)
    fire("RemovedFrom/2", ring2, T)
    fire("AddedTo/3", ring2, P, "Regular")
    local n = 0
    for _, r in ipairs(recs("pickpocket")) do if r.ok then n = n + 1 end end
    check("success counted once", n == 2, n)
end

-- Kill, loot generated into the corpse (no record), then looted from it; a container loot.
do
    traders[TRADER] = true
    fire("KilledBy/4", "S_GOB_" .. GOB, P, P, 0)
    local k = lastOf("kill")
    check("kill", k and k.victim == "Goblin" and k.killer == "Tav" and k.by_party and k.trader == false)
    dead[GOB] = true
    fire("Died/1", "S_GOB_" .. GOB)
    local d = lastOf("died")
    check("died", d and d.name == "Goblin" and d.party == false)
    local coins = item("LOOT_Gold_A", 6)
    stacks[coins:match("%x+%-.*$")] = 5
    fire("AddedTo/3", coins, "S_GOB_" .. GOB, "Treasure")
    local nAdded = #recs("item_added")
    check("treasure into corpse: nothing", #recs("loot") == 0)
    fire("RemovedFrom/2", coins, "S_GOB_" .. GOB)
    fire("AddedTo/3", coins, P, "Regular")
    local l = lastOf("loot")
    check("loot corpse", l and l.from == "corpse" and l.src == "Goblin")
    local a = lastOf("item_added")
    check("gold stack item_added", #recs("item_added") == nAdded + 1 and a.gold == true and a.count == 5)
    local gem = item("GEM", 7)
    fire("RemovedFrom/2", gem, "CONT_Chest_" .. CHEST)
    fire("AddedTo/3", gem, P, "Regular")
    check("loot container", lastOf("loot").from == "container")
    fire("KilledBy/4", T, NULL, P, 0)
    check("trader kill", lastOf("kill").trader == true)
    -- live4: a self-kill (a mephit's death burst) goes to the last hit; the engine's names are kept.
    local MEPHIT, MASTER = "dddddddd-0000-0000-0000-00000000000a", "dddddddd-0000-0000-0000-00000000000b"
    names[MEPHIT], names[MASTER] = "Young Grease Mephit", "Grease Mephit"
    local M, G = "Mephit_Grease_Young_" .. MEPHIT, "S_LOW_GreaseMephit_005_" .. MASTER
    local function tick() for _, fn in ipairs(ticks) do fn() end end
    check("one tick listener", #ticks == 1, #ticks)
    -- Window B (S049-end): KilledBy fires ~47 ms BEFORE the killing blow's AttackedBy (no earlier hit)
    local nk = #recs("kill")
    fire("KilledBy/4", M, G, M, 0)
    check("self-kill waits for its hit", #recs("kill") == nk)
    clock = clock + 47
    tick()
    fire("AttackedBy/7", M, P, P, "Piercing", 4, "Attack", 14319)
    local sk = lastOf("kill")
    check("self-kill to the blow after it", #recs("kill") == nk + 1 and sk.killer == "Tav" and sk.owner == "Tav"
        and sk.by_party and sk.self_kill and sk.victim == "Young Grease Mephit"
        and sk.engine_killer == "Young Grease Mephit" and sk.engine_owner == "Grease Mephit", sk.killer)
    check("blow after the kill not kept as a hit", R.lastHit[MEPHIT] == nil and R.pendingKills[MEPHIT] == nil)
    -- the death burst hits others afterwards: no second kill
    fire("AttackedBy/7", "S_LOW_GreaseOoze_004_" .. GUARD, G, M, "Force", 9, "Attack", 0)
    check("death burst no kill", #recs("kill") == nk + 1)
    -- an earlier hit by Shadowheart, the killing blow by Tav after the kill: Tav
    fire("AttackedBy/7", M, P, SH, "Piercing", 8, "Attack", 0)
    clock = clock + 100
    fire("KilledBy/4", M, G, M, 0)
    fire("AttackedBy/7", M, P, HOST, "Piercing", 4, "Attack", 0)
    check("blow after beats the hit before", lastOf("kill").killer == "Tav", lastOf("kill").killer)
    -- no blow after the kill: the last hit before it, once the wait is over (the tick records it)
    fire("AttackedBy/7", M, P, SH, "Piercing", 8, "Attack", 0)
    clock = clock + 100
    fire("KilledBy/4", M, G, M, 0)
    clock = clock + R.PENDING_MS
    tick()
    check("still waiting at PENDING_MS", #recs("kill") == nk + 2)
    clock = clock + 1
    tick()
    sk = lastOf("kill")
    check("self-kill to the last hit", #recs("kill") == nk + 3 and sk.killer == "Shadowheart" and sk.owner == "Tav"
        and sk.by_party and sk.self_kill and sk.engine_killer == "Young Grease Mephit", sk.killer)
    check("hit forgotten after the kill", R.lastHit[MEPHIT] == nil)
    -- an old hit and no blow after: the engine's names stand, flagged; a hit after the wait is no killer
    fire("AttackedBy/7", M, P, HOST, "Piercing", 8, "Attack", 0)
    clock = clock + R.HIT_MS + 1
    fire("KilledBy/4", M, G, M, 0)
    clock = clock + R.PENDING_MS + 1
    fire("AttackedBy/7", M, P, HOST, "Piercing", 8, "Attack", 0)
    sk = lastOf("kill")
    check("stale hit: engine names", #recs("kill") == nk + 4 and sk.killer == "Young Grease Mephit"
        and sk.owner == "Grease Mephit" and sk.self_kill and not sk.by_party, sk.killer)
    R.lastHit[MEPHIT] = nil
    -- a reader never waits on the tick: ref.since records an expired self-kill first
    fire("KilledBy/4", M, G, M, 0)
    clock = clock + R.PENDING_MS + 1
    local got = since(lastOf("kill").seq)
    check("ref.since flushes", #got == 1 and got[1].ev == "kill" and got[1].self_kill, #got)
    -- a self-hit is no hit; a normal kill has no self_kill
    fire("AttackedBy/7", "S_M_" .. MEPHIT, "S_M_" .. MEPHIT, "S_M_" .. MEPHIT, "Fire", 3, "Attack", 0)
    check("self-hit ignored", R.lastHit[MEPHIT] == nil)
    fire("KilledBy/4", "S_M_" .. MEPHIT, P, P, 0)
    check("normal kill unflagged", lastOf("kill").self_kill == nil and lastOf("kill").killer == "Tav")
    -- Died forgets a hit
    fire("AttackedBy/7", "S_M_" .. MEPHIT, P, HOST, "Piercing", 8, "Attack", 0)
    fire("Died/1", "S_M_" .. MEPHIT)
    check("died forgets the hit", R.lastHit[MEPHIT] == nil)
    -- a pick-up from the ground: item_added only
    local nl = #recs("loot")
    fire("AddedTo/3", item("WPN_Bow", 8), P, "Regular")
    check("pickup: no loot", #recs("loot") == nl)
    -- a RemovedFrom too old to pair is not a source
    local old = item("OLD", 9)
    fire("RemovedFrom/2", old, "S_GOB_" .. GOB)
    clock = clock + R.MATCH_MS + 10
    fire("AddedTo/3", old, P, "Regular")
    check("stale removal unpaired", lastOf("item_added").from == nil)
end

-- Combat: items entering combat are dropped; a fight without the party is not announced.
do
    local G1, G2 = "d24eec60-2fe1-4cb5-fc36-4e65f15eb589", "e24eec60-2fe1-4cb5-fc36-4e65f15eb589"
    fire("CombatStarted/1", G2)
    fire("EnteredCombat/2", "S_GOB_" .. GOB, G2)
    fire("CombatEnded/1", G2)
    check("npc fight silent", #recs("combat_started") == 0 and #recs("combat_ended") == 0)
    fire("CombatStarted/1", G1)
    check("not announced before members", #recs("combat_started") == 0)
    fire("EnteredCombat/2", "CONT_Chest_" .. CHEST, G1)
    fire("EnteredCombat/2", "S_GOB_" .. GOB, G1)
    fire("EnteredCombat/2", P, G1)
    check("announced on party entry", #recs("combat_started") == 1)
    fire("CombatRoundStarted/2", G1, 1)
    check("announced once", #recs("combat_started") == 1)
    local s = ops["ref.snapshot"]({})
    local m = s.combats[G1] and s.combats[G1].members or {}
    check("members: characters only", #m == 2, #m)
    fire("CombatEnded/1", G1)
    check("combat_ended", lastOf("combat_ended") and lastOf("combat_ended").combat == G1)
end

-- Self-check: fired listeners leave `never`; ones that didn't fire stay.
do
    local s = ops["ref.snapshot"]({})
    local never = {}
    for _, k in ipairs(s.never) do never[k] = true end
    check("fired not in never", not never["RequestTrade/4"] and not never["KilledBy/4"])
    check("unfired in never", never["CharacterStoleItem/10"] and never["CharacterLootedCharacter/2"])
    check("fired counts", s.fired["AddedTo/3"] >= 8, s.fired["AddedTo/3"])
end

-- ref.since: cursor, limit, first/latest; the cap drops the oldest.
do
    local all = since(0)
    check("since 0 = all", #all == R.seq, #all)
    check("since cursor", #since(R.seq - 2) == 2)
    local r = ops["ref.since"]({ seq = 0, limit = 3 })
    check("limit", #r.events == 3 and r.latest == R.seq and r.first == 1 and r.boot == "boot-1")
    check("records carry t and gt", all[1].t and all[1].gt)
    R.CAP = 5
    fire("Died/1", "S_GOB_" .. GOB)
    r = ops["ref.since"]({ seq = 0 })
    check("cap", #r.events == 5 and r.first == R.seq - 4, r.first)
end

-- A throwing handler never escapes into the game.
do
    local saved = Osi.PartyGetGold
    Osi.PartyGetGold = nil
    local ok = pcall(listeners["GoldChanged/2"], P, 5)
    check("handler error contained", ok)
    Osi.PartyGetGold = saved
end

-- Fairness: nothing went to the agent-facing event stream.
check("no RPC.push", #pushed == 0, #pushed)

print(string.format("referee: %d passed, %d failed", passed, failures))
if failures > 0 then os.exit(1) end
