-- Referee ring (server context): what the party really did, for the benchmark's graders only.
--
-- Unlike Server/Events.lua this ring is NOT fair: it records hidden facts (who stole what, crimes nobody saw,
-- kills out of sight). It must never reach the agent: nothing here calls RPC.push, and only the harness reads
-- it, through ref.since / ref.snapshot (bridge/src/bg3ai/referee.py; no MCP tool calls them, see
-- bridge/tests/test_referee.py). Records follow the facts v1 event shapes
-- (packages/harness/schemas/facts.v1.schema.json): {seq, t (monotonic ms), gt (game s), ev, ...}.
--
-- Event names and arities are the ones seen FIRING on the live build (research/ovh/live1.md §6). A listener
-- with a wrong name or arity registers fine and never fires, so ref.snapshot reports which listeners fired at
-- least once (`fired` / `never`), not which registered.

BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local U = BG3AI.Util
local V = BG3AI.Visibility
local R = BG3AI.Referee or {}
BG3AI.Referee = R

R.CAP = 4000
R.MATCH_MS = 3000 -- a RemovedFrom pairs with the AddedTo of the same item within this time
R.ring = R.ring or {}
R.seq = R.seq or 0
R.fired = R.fired or {}
R.registered = R.registered or {}
R.removed = R.removed or {} -- item uuid -> {holder, t}: the last RemovedFrom, paired with the next AddedTo
R.traders = R.traders or {} -- npc uuid -> true: every RequestTrade partner or Trade-offering speaker this Lua state saw
R.combats = R.combats or {} -- combat guid -> {announced, members = {uuid -> true}}
R.ppItems = R.ppItems or {} -- item uuids a CharacterPickpocketSuccess already counted
R.lastHit = R.lastHit or {} -- defender uuid -> {attacker, owner, t}: the last AttackedBy, for self-kills
R.HIT_MS = 5000 -- a self-kill goes to the last hit within this time
R.pendingKills = R.pendingKills or {} -- victim uuid -> {rec, hit, t}: a self-kill waiting for the killing blow
R.PENDING_MS = 1000 -- how long a self-kill waits for the AttackedBy that follows it

local truthy = U.truthy
local osi = V.osi

local function now_ms() return Ext.Utils.MonotonicTime() end
local function game_s()
    local ok, t = pcall(function() return Ext.Utils.GameTime() end)
    return ok and t or nil
end

function R.record(ev, data)
    R.seq = R.seq + 1
    local rec = data or {}
    rec.seq, rec.t, rec.gt, rec.ev = R.seq, now_ms(), game_s(), ev
    R.ring[#R.ring + 1] = rec
    while #R.ring > R.CAP do table.remove(R.ring, 1) end
    return rec
end

-- Party membership, cached for 250 ms (a trade fires ~8 events in one frame).
local partyCache, partyTime = nil, -1e9
local function isParty(u)
    local t = now_ms()
    if not partyCache or t - partyTime > 250 then
        local _, seen = V.party()
        partyCache, partyTime = seen or {}, t
    end
    return u ~= nil and partyCache[u] == true
end
function R.resetPartyCache() partyCache = nil end

local function host()
    local p = V.party()
    return p and p[1]
end

-- "LOOT_Gold_A_<uuid>" -> uuid; bare uuids (RequestPickpocket) pass through.
local uuid = V.uuid
local function name(u) return u and V.name(u) or nil end
local function isGold(item) return type(item) == "string" and item:find("^LOOT_Gold") ~= nil end
local function tplId(tpl) return uuid(tpl) or tpl end

local function count(item)
    return math.max(1, math.floor(tonumber(osi(Osi.GetStackAmount, item)) or 1))
end

------------------------------------------------------------------------
-- Listeners: each counts its firings (the self-check) and never throws into the game.
------------------------------------------------------------------------

local function listen(ev, arity, fn)
    local key = ev .. "/" .. arity
    R.fired[key] = R.fired[key] or 0
    local ok, err = pcall(Ext.Osiris.RegisterListener, ev, arity, "after", function(...)
        R.fired[key] = R.fired[key] + 1
        local ok2, err2 = pcall(fn, ...)
        if not ok2 then Ext.Utils.PrintWarning("[BG3AI] referee " .. key .. ": " .. tostring(err2)) end
    end)
    R.registered[key] = ok and true or tostring(err)
end

-- Trade: RequestTrade ... AddedTo/RemovedFrom + GoldChanged pairs ... TradeEnds (live1 §6: no sold/bought event).
listen("RequestTrade", 4, function(player, trader)
    local npc = uuid(trader)
    if not npc then return end
    R.traders[npc] = true
    R.trade = { npc = npc, char = uuid(player), sold = {}, bought = {}, gold_in = 0, gold_out = 0 }
    R.record("trade_open", { npc = name(npc), npc_uuid = npc, char = name(uuid(player)) })
end)

listen("TradeEnds", 2, function(player, trader)
    local npc = uuid(trader)
    local tr = R.trade
    local rec = { npc = name(npc), npc_uuid = npc, char = name(uuid(player)) }
    if tr and tr.npc == npc then
        rec.sold, rec.bought, rec.gold_in, rec.gold_out = tr.sold, tr.bought, tr.gold_in, tr.gold_out
    end
    R.trade = nil
    R.record("trade_end", rec)
end)

-- A dialogue that offers Trade (Client/UIState.lua S.tradeOffered: the game's Trade button) makes its NPC speakers
-- traders. Live N3/N5: no static read marks a merchant (the Trader tag misses Mohan; Osi.CanTrade, Flags2 Trader and
-- the template's TradeTreasures hold for goblins, kids, druids ... too), so this and RequestTrade fill R.traders.
-- Known gap: an untagged merchant nobody talked to (Mohan before any dialogue) isn't a trader yet.
function R.tradeOffered()
    for _, npcs in pairs(BG3AI.Events and BG3AI.Events.dialogs or {}) do
        for _, u in ipairs(npcs) do R.traders[u] = true end
    end
end
R.fired["net/TradeOffered"] = R.fired["net/TradeOffered"] or 0
local okNet, errNet = pcall(function()
    Ext.Net.CreateChannel("65734c81-edd1-4b6f-b10e-663fec0e768e", "TradeOffered", function()
        R.fired["net/TradeOffered"] = R.fired["net/TradeOffered"] + 1
        R.tradeOffered()
    end)
end)
R.registered["net/TradeOffered"] = okNet and true or tostring(errNet)

-- A trader: one the party traded with or that offered Trade in a dialogue (R.traders), or a merchant with the
-- Trader tag. Not Osi.CanTrade: it held for every windmill goblin, Barth, Arka ... (diverse-2026-10-25 facts).
function R.isTrader(u, e) return u ~= nil and (R.traders[u] or U.comp(e, "Trader") ~= nil) end

-- Gold: one record per change of the party's total (a move between party members nets out).
listen("GoldChanged", 2, function(holder, delta)
    local u = uuid(holder)
    local d = math.floor(tonumber(delta) or 0)
    if d == 0 or not isParty(u) then return end
    local tr = R.trade
    if tr then
        if d > 0 then tr.gold_in = tr.gold_in + d else tr.gold_out = tr.gold_out - d end
    end
    local total = tonumber(osi(Osi.PartyGetGold, host()))
    if not total then return end
    total = math.floor(total)
    local before = R.lastGold or (total - d)
    R.lastGold = total
    if total ~= before then
        R.record("gold", { gold = total, delta = total - before, holder = name(u) })
    end
end)

listen("RemovedFrom", 2, function(item, holder)
    local i, h = uuid(item), uuid(holder)
    if not (i and h) then return end
    local t = now_ms()
    for k, v in pairs(R.removed) do
        if t - v.t > R.MATCH_MS then R.removed[k] = nil end
    end
    R.removed[i] = { holder = h, t = t }
end)

-- Where the item came from: the holder of its RemovedFrom a moment ago, if any.
local function source(i)
    local r = R.removed[i]
    R.removed[i] = nil
    if r and now_ms() - r.t <= R.MATCH_MS then return r.holder end
    return nil
end

local function pickpocketFrom(src)
    local pp = R.pickpocket
    return pp and src == pp.npc
end

listen("AddedTo", 3, function(item, holder, addType)
    local i, h = uuid(item), uuid(holder)
    if not (i and h) then return end
    local src = source(i)
    local tr = R.trade
    local entry = { name = name(i), count = count(i), gold = isGold(item) or nil }
    if not isParty(h) then
        -- the party's item into the trader's inventory: sold (gold leaving the payer has no RemovedFrom)
        if tr and h == tr.npc and isParty(src) then tr.sold[#tr.sold + 1] = entry end
        return
    end
    if isParty(src) then return end -- moved between party members
    local rec = R.record("item_added", {
        name = entry.name, count = entry.count, gold = entry.gold, uuid = i, add_type = addType,
        from = name(src), from_uuid = src, in_trade = tr and true or nil,
    })
    if tr and src == tr.npc then
        rec.bought = true
        tr.bought[#tr.bought + 1] = entry
    elseif src and pickpocketFrom(src) then
        if not R.ppItems[i] then
            R.record("pickpocket", { ok = true, npc = name(src), npc_uuid = src, item = entry.name,
                count = entry.count, char = name(h) })
        end
    elseif src then
        local from
        if truthy(osi(Osi.IsCharacter, src)) then
            if truthy(osi(Osi.IsDead, src)) then from = "corpse" end
        elseif truthy(osi(Osi.IsItem, src)) then
            from = "container"
        end
        if from then R.record("loot", { from = from, item = entry.name, src = name(src), char = name(h) }) end
    end
end)

-- The root template of the item just recorded (it fires right after its AddedTo).
listen("TemplateAddedTo", 4, function(tpl, item)
    local i = uuid(item)
    for k = #R.ring, math.max(1, #R.ring - 8), -1 do
        local rec = R.ring[k]
        if rec.uuid == i and rec.tpl == nil then rec.tpl = tplId(tpl) end
    end
end)

-- Pickpocket: the window opens (bare uuids), then a failure, or the item arrives (paired in AddedTo).
listen("RequestPickpocket", 2, function(player, npc)
    R.pickpocket = { npc = uuid(npc), char = uuid(player), t = now_ms() }
end)

listen("CharacterPickpocketFailed", 2, function(player, npc)
    local n = uuid(npc)
    R.record("pickpocket", { ok = false, npc = name(n), npc_uuid = n, char = name(uuid(player)) })
end)

-- Not seen firing on the live build yet (live1 §6); arity from the osi reference.
listen("CharacterPickpocketSuccess", 6, function(player, npc, item, _, amount)
    local i, n = uuid(item), uuid(npc)
    if i then R.ppItems[i] = true end
    R.record("pickpocket", { ok = true, npc = name(n), npc_uuid = n, item = name(i),
        count = math.max(1, math.floor(tonumber(amount) or 1)), char = name(uuid(player)) })
end)

listen("CharacterStoleItem", 10, function(char, item, _, _, _, _, owner)
    local i = uuid(item)
    R.record("stole", { char = name(uuid(char)), item = name(i), tpl = tplId(osi(Osi.GetTemplate, item)),
        owner = name(uuid(owner)) })
end)

-- Crimes the party committed (CrimeIsRegistered(victim, type, id, _, criminal, ...)). Follow-up registrations
-- (KeepAnEyeOnMe, WaitingForCalledGuard, ...) are kept with their crime id; graders dedupe.
listen("CrimeIsRegistered", 8, function(victim, crimeType, crimeId, _, criminal)
    local c = uuid(criminal)
    if not isParty(c) then return end
    local v = uuid(victim)
    R.record("crime", { type = tostring(crimeType), id = tonumber(crimeId), victim = v and name(v) or nil,
        criminal = name(c) })
end)

-- Kills: KilledBy(victim, killerOwner, killer, _): the owner tells a summon's kill from its master's.
-- live4: Karlach's arrow killed a Young Grease Mephit and KilledBy named the mephit itself as the killer (its death
-- burst; owner its summoner). Window B (S049-end): KilledBy fires ~47 ms BEFORE the killing blow's AttackedBy, so a
-- self-kill waits up to PENDING_MS for the victim's next AttackedBy and goes to that attacker; none: to the last hit
-- before the kill within HIT_MS; none either: the engine's names stand. The engine's names are kept as
-- engine_killer / engine_owner with self_kill = true.
-- trader: R.isTrader. Conjured victims (diverse-2026-10-25 act3_gold_max: 78 street magicians' Cats killed by
-- their conjurers): summon when the engine marks one (the IsSummon component, as State.lua's party summons;
-- Osi.IsSummon; the ServerCharacter Summon flag), with its summoner when known; temporary when spawned at run time.
local function conjured(e, v, rec)
    local flags = {}
    pcall(function() for _, f in pairs(e.ServerCharacter.Flags) do flags[tostring(f)] = true end end)
    local sc = U.comp(e, "IsSummon")
    if sc or flags.Summon or truthy(osi(Osi.IsSummon, v)) then
        rec.summon = true
        local s
        pcall(function() s = V.entityUuid(sc.Owner) or V.entityUuid(sc.Summoner) end)
        if s then rec.summoner, rec.summoner_uuid = name(s), s end
    end
    if flags.Temporary then rec.temporary = true end
end

local function recordKill(v, rec, k, o)
    rec.victim, rec.victim_uuid, rec.killer = name(v) or "Unknown", v, k and name(k) or nil
    rec.owner, rec.by_party = o and name(o) or nil, isParty(o) or isParty(k)
    local e = V.entity(v)
    rec.trader = R.isTrader(v, e) and true or false
    conjured(e, v, rec)
    R.record("kill", rec)
end

-- Self-kills whose wait is over: recorded with the hit before the kill, or as the engine said.
local function flushKills()
    local t = now_ms()
    for v, p in pairs(R.pendingKills) do
        if t - p.t > R.PENDING_MS then
            R.pendingKills[v] = nil
            local h = p.hit
            if h then recordKill(v, p.rec, h.attacker, h.owner) else recordKill(v, p.rec, p.k, p.o) end
        end
    end
end

if not R.tickSubscribed then
    R.tickSubscribed = true
    Ext.Events.Tick:Subscribe(function()
        local ok, err = pcall(flushKills)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] referee kills: " .. tostring(err)) end
    end)
end

-- Hits: AttackedBy(defender, attackerOwner, attacker, damageType, amount, cause, _) (live1 §6), kept per defender.
listen("AttackedBy", 7, function(defender, owner, attacker)
    local d, a = uuid(defender), uuid(attacker)
    if not d or not a or a == d then return end
    flushKills()
    local p = R.pendingKills[d]
    if p then
        R.pendingKills[d] = nil
        return recordKill(d, p.rec, a, uuid(owner))
    end
    R.lastHit[d] = { attacker = a, owner = uuid(owner), t = now_ms() }
end)

listen("KilledBy", 4, function(victim, owner, killer)
    local v, o, k = uuid(victim), uuid(owner), uuid(killer)
    if not v then return end
    local h = R.lastHit[v]
    R.lastHit[v] = nil
    if k ~= v then return recordKill(v, {}, k, o) end
    local rec = { self_kill = true, engine_killer = name(k), engine_owner = o and name(o) or nil }
    R.pendingKills[v] = { rec = rec, k = k, o = o, t = now_ms(),
        hit = h and now_ms() - h.t <= R.HIT_MS and h or nil }
end)

listen("Died", 1, function(char)
    local u = uuid(char)
    if not u then return end
    R.lastHit[u] = nil
    R.record("died", { name = name(u) or "Unknown", party = isParty(u), uuid = u })
end)

-- Combat: only fights the party is in; EnteredCombat/LeftCombat fire for every item in range too (live1 §6), so
-- membership keeps characters only.
local function partyIn(g)
    local c = R.combats[g]
    if c then
        for m in pairs(c.members) do
            if isParty(m) then return true end
        end
    end
    for _, u in ipairs((V.party())) do
        if osi(Osi.CombatGetGuidFor, u) == g then return true end
    end
    return false
end

local function combat(g)
    local c = R.combats[g]
    if not c then
        c = { announced = false, members = {} }
        R.combats[g] = c
    end
    return c
end

local function announce(g)
    local c = combat(g)
    if not c.announced and partyIn(g) then
        c.announced = true
        R.record("combat_started", { combat = g })
    end
end

listen("CombatStarted", 1, function(g) announce(g) end)
listen("CombatRoundStarted", 2, function(g) announce(g) end)

listen("EnteredCombat", 2, function(entity, g)
    local u = uuid(entity)
    if not u or not truthy(osi(Osi.IsCharacter, u)) then return end
    combat(g).members[u] = true
    if isParty(u) then announce(g) end
end)

listen("LeftCombat", 2, function(entity, g)
    local c = R.combats[g]
    local u = uuid(entity)
    if c and u then c.members[u] = nil end
end)

listen("CombatEnded", 1, function(g)
    local c = R.combats[g]
    R.combats[g] = nil
    if c and c.announced then R.record("combat_ended", { combat = g }) end
end)

-- Not seen firing yet (live1 §6): listened to so the self-check shows whether they exist.
listen("CharacterLootedCharacter", 2, function() end)

------------------------------------------------------------------------
-- Ops (harness only; never an MCP tool)
------------------------------------------------------------------------

-- The ring after `seq`: {boot, now, gt, latest, first, events}. `first` is the oldest seq kept: a reader whose
-- cursor is below first - 1 lost records to the cap. A new `boot` (a load reset the Lua state) restarts seq at 1.
RPC.register("ref.since", function(args)
    flushKills()
    local since = tonumber(args.seq) or 0
    local limit = tonumber(args.limit) or 500
    local out = {}
    for _, rec in ipairs(R.ring) do
        if rec.seq > since then
            out[#out + 1] = rec
            if #out >= limit then break end
        end
    end
    return { boot = RPC.BOOT, now = now_ms(), gt = game_s(), latest = R.seq,
        first = R.ring[1] and R.ring[1].seq or R.seq + 1, events = out }
end)

-- The self-check and live state: which listeners fired at least once, open trade/pickpocket, combats.
RPC.register("ref.snapshot", function()
    local never, combats = {}, {}
    for key, n in pairs(R.fired) do
        if n == 0 then never[#never + 1] = key end
    end
    table.sort(never)
    for g, c in pairs(R.combats) do
        local members = {}
        for m in pairs(c.members) do members[#members + 1] = m end
        table.sort(members)
        combats[g] = { announced = c.announced, members = members }
    end
    return { boot = RPC.BOOT, now = now_ms(), gt = game_s(), latest = R.seq, fired = R.fired, never = never,
        registered = R.registered, trade = R.trade and { npc = R.trade.npc } or nil,
        pickpocket = R.pickpocket and { npc = R.pickpocket.npc } or nil, combats = combats }
end)

RPC.QUIET_OPS["ref.since"] = true

return R
