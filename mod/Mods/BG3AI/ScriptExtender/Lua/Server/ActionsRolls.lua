-- Part of Server/Actions.lua (split for the 1000-line cap): the rolls of the casts the harness orders, and when a
-- cast has started. Loaded before ActionsCast.lua, which calls these at its order, CastedSpell and on_tick.
--
-- Forced hits (ops/logs/arena-dice.md "H4 cast path"; live 2026-11-03: Osi.UseSpell casts hit 8/8 at AC 35 and
-- Sacred Flame never rolled a save, while the hotbar route missed 9/9): a cast an Osiris UseSpell /
-- UseSpellAtPosition starts carries IgnoreSpellRolls in its SpellCastState.CastOptions, so its SpellRoll (attack
-- roll, saving throw) was skipped and its success applied. A player's cast rolls. The order stamps the caster, and
-- when the cast entity is made (SpellCastState OnCreate, before the roll) the flag is dropped and read back. Live
-- (cast-rolls probe rerun): CastOptions reads and writes as a Lua table of flag names, and the write there gave 6/6
-- misses at AC 35 and 2/6 saves made; the cast's start request is too late.
--
-- REVIEW-5 F2: the stamp outlives the job (ROLL_STAMP_MS). Every cast state of a stamped caster that carries
-- IgnoreSpellRolls (only an Osiris order makes one: the game's own casts and the player's hotbar clicks never carry
-- it, live) is cleared, whatever its job did: a cast that starts after its job gave up, or a second cast for one
-- order. Only a state of the awaited spell marks the job's cast; a late or second one is a late_cast event, so the
-- agent hears that an earlier attack went off. F7: a cast that went through without its roll is reported done with
-- unrolled = true (and a referee record), never as failed: the agent must not order it again.

local RPC = BG3AI.RPC
local G = BG3AI.Guard
local A = BG3AI.Actions

A.ROLL_STAMP_MS = 20000
A.ROLL_STOP_MS = 2500 -- a failed clear: no CastedSpell this long after the purge = stopped
A.roll_wait = A.roll_wait or {} -- caster guid -> {job, spell, t}: the last order's stamp, kept past its job

-- The flags the write drops. CAST_DROP: the roll flag; a cast that keeps it is stopped or marked unrolled.
-- CAST_DROP_EXTRA: check flags the cast loses as well, so the engine checks the spell's own rules as for a player
-- (Requirements, RequirementConditions, TargetConditions). Default CAST_DROP_CHECKS (window 7 per-flag probe: with
-- either one dropped the control cast still went, and without IgnoreCastChecks the engine refused Rage out of
-- combat itself). IgnoreHasSpell stays: dropping it refused every cast (window 6/7). The guard's own rule check of
-- the caster (GuardRules.lua: Requirements, RequirementConditions) still runs first; TargetConditions are the
-- engine's alone (window 9 R3). A check flag that won't clear only warns and is recorded for the referee (with the
-- guard's reading of the target conditions when IgnoreTargetChecks stayed). Probe switch
-- (tools/dev/probe_cast_checks.py): false = none, a list = those flags, true = all of CAST_DROP_PROBE.
A.CAST_DROP = { "IgnoreSpellRolls" }
A.CAST_DROP_CHECKS = { "IgnoreCastChecks", "IgnoreTargetChecks" }
A.CAST_DROP_PROBE = { "IgnoreCastChecks", "IgnoreTargetChecks", "IgnoreHasSpell" }
if A.CAST_DROP_EXTRA == nil then A.CAST_DROP_EXTRA = A.CAST_DROP_CHECKS end

-- At the order (right before the Osiris call): the cast it becomes must lose IgnoreSpellRolls.
function A.cast_rolls(job, char, spell, target)
    job.roll = { spell = spell, state = "pending", target = target }
    A.roll_wait[G.guid(char)] = { job = job, spell = spell, t = RPC.now_ms() }
end

-- The referee ring (hidden from the agent): a cast that went through without its roll.
local function referee(ev, data)
    local R = BG3AI.Referee
    if R and R.record then pcall(R.record, ev, data) end
end

-- A clear that fails at the cast state's creation purges the queue after the cast entity exists. Live (window 5) a
-- purge cancelled a started, paused cast, but that it always stops one is not shown: the job reports once it is
-- known (A.roll_stop_tick). resolved: called at the cast's CastedSpell (no cast state was seen): it went through.
local function roll_failed(job, why, resolved)
    job.roll.state, job.roll.why = "failed", why
    if resolved then
        job.roll.unrolled = true
        referee("cast_unrolled", { uuid = job.char, spell = job.roll.spell, why = why })
        return
    end
    job.stopping = true -- the purge is ours; on_finish purges no more
    pcall(G.osi, "PurgeOsirisQueue", job.char)
    job.roll.failed_ms = RPC.now_ms()
end

-- A job's on_tick, first: while its clear has failed and nothing came of it yet, nothing else of it runs.
-- went: the job's own sign that the cast resolved (sneak: SNEAKING came); a cast's sign is its CastedSpell.
function A.roll_stop_tick(job, went)
    local r = job.roll
    if not r or r.state ~= "failed" or r.unrolled or job.status ~= "running" then return false end
    if went then
        r.unrolled = true
        referee("cast_unrolled", { uuid = job.char, spell = r.spell, why = r.why })
        return false
    end
    if RPC.now_ms() - r.failed_ms >= A.ROLL_STOP_MS then
        RPC.finish_job(job, "failed", nil, "the game would have cast it without its roll (" .. r.why .. "): stopped,"
            .. string.format(" nothing cast (the queue purged, no cast in %.1f s)", A.ROLL_STOP_MS / 1000))
    end
    return true
end

-- What a done result says of a cast that went through without its roll.
function A.unrolled_result(job, res)
    if job.roll and job.roll.unrolled then
        res.unrolled = "the game cast it without its roll (" .. tostring(job.roll.why) .. ")"
    end
    if job.roll and job.roll.unchecked then
        res.unchecked = "the game cast it without checking its own rules (" .. table.concat(job.roll.unchecked, ",")
            .. " stayed set)"
    end
    return res
end

-- CastOptions as a list of flag names, or nil.
local function flag_names(v)
    if type(v) ~= "table" then return nil end
    local out = {}
    for _, n in ipairs(v) do out[#out + 1] = tostring(n) end
    return out
end

local function has(names, flag)
    for _, n in ipairs(names) do if n == flag then return true end end
    return false
end

local function state_spell(st)
    local ok, sid = pcall(function() return st.SpellId end)
    if not ok or sid == nil then return nil, nil end
    local okp, p = pcall(function() return sid.Prototype end)
    local oko, o = pcall(function() return sid.OriginatorPrototype end)
    return okp and p or nil, oko and o or nil
end

-- Drops `drop` from the state's CastOptions; returns the names after (nil = unread) and the flags that stayed.
local function strip(st, names, drop)
    local keep, any = {}, false
    for _, n in ipairs(names) do
        if has(drop, n) then any = true else keep[#keep + 1] = n end
    end
    if any then st.CastOptions = keep end
    local after = flag_names(st.CastOptions)
    local stuck, kept = {}, {}
    for _, f in ipairs(drop) do
        if not after or has(after, f) then
            if has(A.CAST_DROP, f) then stuck[#stuck + 1] = f else kept[#kept + 1] = f end
        end
    end
    if #kept > 0 then
        Ext.Utils.PrintWarning("[BG3AI] cast checks: " .. table.concat(kept, ",") .. " stayed set (the engine won't"
            .. " check those rules for this cast; the guard checked only the caster's before the order)")
    end
    return after, stuck, kept
end

-- Audit S2-1: a check flag that stayed set = a cast the engine didn't check. Not refused (a stuck write was never
-- seen live, and a purge after the cast state exists doesn't surely stop it, audit S2 b): recorded for the referee
-- with the caster's terms the guard couldn't read for it (nothing checked those), and the done result says it. With
-- IgnoreTargetChecks kept, nothing checked the target conditions either (the guard leaves them to the engine, window
-- 9 R3): target_rules is the guard's reading of them at that target (G.explain_target), for the referee.
local function unchecked(caster, spell, kept, target)
    local last = G.rules_last and G.rules_last[caster]
    local tc
    if target and has(kept, "IgnoreTargetChecks") then
        local ok, w = pcall(G.explain_target, caster, spell, target)
        tc = ok and w or nil
    end
    referee("cast_unchecked", { uuid = caster, spell = spell, flags = table.concat(kept, ","),
        rules_unread = last and last.spell == spell and #last.terms > 0 and table.concat(last.terms, "; ") or nil,
        target_rules = tc })
end

-- The roll flag plus the check flags of CAST_DROP_EXTRA.
local function drop_list()
    local extra = A.CAST_DROP_EXTRA == true and A.CAST_DROP_PROBE or type(A.CAST_DROP_EXTRA) == "table"
        and A.CAST_DROP_EXTRA or {}
    local drop = {}
    for _, f in ipairs(A.CAST_DROP) do drop[#drop + 1] = f end
    for _, f in ipairs(extra) do drop[#drop + 1] = f end
    return drop
end

-- The engine's own reason for refusing the job's cast (SpellCastOutcome.Result on the cast entity, bg3se
-- SpellCastFailReason), in plain words, and the game's code; nil when it doesn't read.
A.FAIL_REASONS = {
    Canceled = "the cast was cancelled", InvalidSpell = "the action isn't valid for this character",
    MissingTarget = "it has no target", CantCast = "it can't be cast now (its requirements aren't met)",
    InvalidTarget = "the target isn't a valid target for it", CantUseSource = "the caster can't use its source",
    CantSpendUseCosts = "its cost can't be paid", MissingAnimations = "the game has no animation for it",
    MissingTargetPosition = "it has no target position", TooManyTargets = "too many targets",
    TooFewTargets = "too few targets", MissingCaster = "no caster", DeadTarget = "the target is dead",
    EqualTargets = "the same target was picked twice", CantFindCastPosition = "no spot to cast it from",
    BlockedRequiredMove = "the move it needs is blocked", InvalidCastPosition = "the cast position isn't valid",
    CantMove = "the caster can't move", MovementFailed = "the move to cast it failed",
    ServerTimeout = "the game timed out",
}
A.SPOT_REASONS = { MissingTarget = true, TooFewTargets = true, InvalidTarget = true }
function A.engine_refusal(job)
    local e = job and job.cast_entity
    if not e then return nil end
    local ok, r = pcall(function() return e.SpellCastOutcome.Result end)
    if not ok or r == nil then return nil end
    r = tostring(r)
    if r == "None" or r == "0" then return nil end
    -- audit S3-2: at a ground point these depend on which creatures the engine finds there, hidden ones too; a
    -- player's cursor at that spot shows none of it
    if job.cast_at_pos and A.SPOT_REASONS[r] then return "it can't be cast at that spot", r end
    return (A.FAIL_REASONS[r] or "reason " .. r) .. " (the game's " .. r .. ")", r
end

-- SpellCastState OnCreate of a stamped caster.
function A.roll_cast_state(entity)
    if not next(A.roll_wait) then return end
    local st = entity.SpellCastState
    local ok, u = pcall(function() return st.Caster.Uuid.EntityUuid end)
    local caster = ok and G.guid(u) or nil
    local w = caster and A.roll_wait[caster]
    if not w then return end
    if RPC.now_ms() - w.t > A.ROLL_STAMP_MS then
        A.roll_wait[caster] = nil
        return
    end
    local job, names = w.job, flag_names(st.CastOptions)
    local p, o = state_spell(st)
    local same = (p == nil and o == nil) or p == w.spell or o == w.spell
    if same and job.status == "running" and job.roll.state == "pending" then -- the awaited cast
        if not names then return roll_failed(job, "its CastOptions don't read as flag names") end
        job.cast_entity = entity -- its SpellCastOutcome says why the engine refused it (A.engine_refusal)
        local after, stuck, kept = strip(st, names, drop_list())
        if #kept > 0 then
            job.roll.unchecked = kept
            unchecked(caster, w.spell, kept, job.roll.target)
        end
        if #stuck > 0 then
            return roll_failed(job, table.concat(stuck, ",") .. " stayed set: "
                .. (after and table.concat(after, ",") or "unread"))
        end
        job.roll.state, job.roll.options = "cleared", after
        job.state_ms = RPC.now_ms() -- the cast exists: it started (on_tick's no-start check counts it)
        return
    end
    -- Not the awaited cast: only an Osiris order's cast carries the flag; it rolls and is checked too.
    if not names or not has(names, "IgnoreSpellRolls") then return end
    local _, stuck, kept = strip(st, names, drop_list())
    if #kept > 0 then unchecked(caster, p or o or w.spell, kept) end
    if same then
        RPC.push("late_cast", { uuid = caster, spell = w.spell,
            note = (job.status == "running" and "a second cast for one order" or "an earlier order went off after its"
                .. " result") .. (#stuck > 0 and ", without its roll" or "") })
    end
    if #stuck > 0 then referee("cast_unrolled", { uuid = caster, spell = p or o or w.spell, why = "late cast" }) end
end

-- Live window 5 (ops/logs/live.md, melee-swap A/B): 3 of 10 Main Hand Attacks "never started" although their cast
-- state was made at 70-270 ms: Karlach's own reaction prompt (Reckless Attack, "needs 15 to hit") paused the cast
-- (ls::anubis::PausedComponent on the caster from ~0.8-1.4 s) and the 3 s no-start check purged it. A started cast
-- the game holds paused this long is reported as paused, its tasks kept: it goes on once the prompt is answered.
A.CAST_PAUSED_COMP = "ls::anubis::PausedComponent"
A.CAST_PAUSED_MS = 1000
A.CAST_PAUSED = "the cast started and the game paused it for a reaction decision (a reaction prompt is open):"
    .. " answer it (react); the cast goes on after it and its outcome shows in the events"

function A.cast_paused(char)
    local ok, names = pcall(function() return Ext.Entity.Get(char):GetAllComponentNames() end)
    if not ok or type(names) ~= "table" then return false end
    for _, n in ipairs(names) do if n == A.CAST_PAUSED_COMP then return true end end
    return false
end

-- CastedSpell of the job's cast: true = report it as cast. A cast whose state was never seen, or whose clear failed
-- and still resolved, went through without its roll: done, marked unrolled.
function A.roll_checked(job)
    local r = job.roll
    if r and r.state == "pending" then
        roll_failed(job, "the mod saw no cast state for it", true)
    elseif r and r.state == "failed" and not r.unrolled then
        r.unrolled = true
        referee("cast_unrolled", { uuid = job.char, spell = r.spell, why = r.why })
    end
    return true
end

-- Startup self-check (REVIEW-5): without the hook no cast of the harness rolls; every CastedSpell would report
-- "unrolled". Say so loudly at load.
if A.roll_hook == nil then
    A.roll_hook = pcall(Ext.Entity.OnCreate, "SpellCastState", function(e)
        local ok, err = pcall(A.roll_cast_state, e)
        if not ok then Ext.Utils.PrintWarning("[BG3AI] cast roll: " .. tostring(err)) end
    end)
    if not A.roll_hook then
        local say = Ext.Utils.PrintError or Ext.Utils.PrintWarning
        say("[BG3AI] SELF-CHECK FAILED: the SpellCastState OnCreate hook did not register: every cast the harness"
            .. " orders goes WITHOUT its attack roll or saving throw (forced hits). Do not run a bench or arena match.")
    end
end
