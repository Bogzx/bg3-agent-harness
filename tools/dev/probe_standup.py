"""Live probe (act6 1b, for N10): right after a Scroll of Revivify, end_standup's own walk vs probe_revive_stuck's raw
walk on the revived member, sampled every 100 ms.

N9 (research/fix/live.md "Night N9" step 1): end_standup's reply "Claude stood up (stepped 0.0 m, walk ordered 3
times)", then no order of Claude's started (layer-1 ActionState, TransactionId 2). N8 5B: probe_revive_stuck `move
Claude --dx 3` 3.4 s after the reply walked 2.98 m and took the ResurrectComponent and the ActionState away. In the code
both send the same Osiris call (CharacterMoveToPosition, "Run", an event, an id); what differed was when (end_standup
ordered at once, while the member still had RegisteredForAutomatedDialog), the purges (end_standup purged and
re-ordered every 1.5 s and purged the queued walk at its end; the raw walk never purges) and what counted as done (the
ResurrectComponent gone). This probe records which of those the game cares about.

It runs the C1 replay (probe_revive_replay: the job's own calls, the two-click revive) and stops after the revive
(--to 16:47:30). The bridge's world_tools._end_standup is wrapped:
  1. a server tick sampler on the revived member (>= 100 ms apart): position, ResurrectComponent, layer-1 action state
     (type, finished, TransactionId), Osiris task count, RegisteredForAutomatedDialog, IsResurrected, dead; and every
     G.osi call the mod makes for that member (end_standup's walk orders and purges) with its time;
  2. the real _end_standup (act.end_standup, the exact path the agent gets) and its reply;
  3. if the member moved < 0.3 m: probe_revive_stuck's raw `move <member> --dx 3` (Osi.CharacterMoveToPosition, no
     guard) and its result RAW_SECS later;
  4. the samples and calls, as one {"step": "standup_probe"} line in --standup-out.
Read-only apart from the walk orders: the sampler and the G.osi wrapper are removed at the end.

Run like probe_revive_replay (night-live/lib, a fresh load of the job's start save or its PROBE- copy):
    cd /data/Repos/ovh-e2e/runtime/probes/night-live/lib && uv run -q --project /data/Repos/ovh-e2e/bridge python \\
        /data/Repos/ovh-e2e/tools/dev/probe_standup.py --two-click-revive --to 16:47:30 \\
        --out ../revive/C1-N10.jsonl --standup-out ../revive/C1-N10-standup.jsonl
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import probe_revive_replay as replay  # noqa: E402  (tools/dev on the path first)
import probe_revive_stuck as probe  # noqa: E402

RAW_DX = 3.0
RAW_SECS = 6.0
MOVED_M = 0.3  # end_standup's own "it stepped" (A.STANDUP_MOVED_M)

SAMPLER = r"""
local B = Mods.BG3AI.BG3AI
local P = B.ReviveProbe
local c = P.who(%s)
local S = { t0 = Ext.Utils.MonotonicTime(), recs = {}, calls = {}, last = -1000, c = c }
P.su = S
local function layer1()
    local ok, st = pcall(function() return Ext.Entity.Get(c).ServerCharacter.AiActionMachine.Layers[1].State end)
    if not ok or st == nil then return "-" end
    local okt, ty = pcall(function() return st.Type end)
    local okf, fin = pcall(function() return st.Finished end)
    local oki, tid = pcall(function() return st.TransactionId end)
    return (okt and tostring(ty) or "?") .. ((okf and fin) and "(finished)" or "") .. " tid="
        .. (oki and tostring(tid) or "?")
end
local function sample()
    local now = Ext.Utils.MonotonicTime()
    if now - S.last < 100 or #S.recs >= 600 then return end
    S.last = now
    local r = { t = now - S.t0, resurrect = false, layer1 = layer1(), dead = Osi.IsDead(c) }
    pcall(function() r.pos = { Osi.GetPosition(c) } end)
    for k in pairs(P.comp_names(c)) do
        if k:find("ResurrectComponent", 1, true) then r.resurrect = true end
    end
    pcall(function()
        local sc = Ext.Entity.Get(c).ServerCharacter
        r.osi = #sc.OsirisController.Tasks
        r.auto_dialog = P.js(sc.Flags):find("RegisteredForAutomatedDialog", 1, true) ~= nil
        r.resurrected = P.js(sc.Flags2):find("IsResurrected", 1, true) ~= nil
    end)
    S.recs[#S.recs + 1] = r
end
S.sub = Ext.Events.Tick:Subscribe(function() pcall(sample) end)
local G = B.Guard
S.osi0 = G.osi
G.osi = function(name, ...)
    local n, a = select("#", ...), { ... }
    if tostring(a[1] or ""):sub(-36) == c then
        local args = {}
        for i = 1, n do args[i] = tostring(a[i]) end
        S.calls[#S.calls + 1] = { t = Ext.Utils.MonotonicTime() - S.t0, via = "G.osi (mod)", name = name, args = args }
    end
    return S.osi0(name, ...)
end
return "sampling " .. c
"""

NOW = r"""
local S = Mods.BG3AI.BG3AI.ReviveProbe.su
local last = S.recs[#S.recs]
return Ext.Json.Stringify({ t = Ext.Utils.MonotonicTime() - S.t0, first = S.recs[1] and S.recs[1].pos,
    last = last and last.pos }, { Beautify = false })
"""

STOP = r"""
local B = Mods.BG3AI.BG3AI
local S = B.ReviveProbe.su
pcall(function() Ext.Events.Tick:Unsubscribe(S.sub) end)
if S.osi0 then B.Guard.osi = S.osi0 end
B.ReviveProbe.su = nil
return Ext.Json.Stringify({ samples = S.recs, calls = S.calls }, { Beautify = false })
"""


def moved(first: Any, last: Any) -> float:
    if not (isinstance(first, list) and isinstance(last, list) and len(first) >= 3 and len(last) >= 3):
        return 0.0
    return probe.moved_m(first, last)


def wrap(orig: Callable[[str, str], str], ev: Callable[[str], str], out: Path,
         raw_secs: float = RAW_SECS, sleep: Callable[[float], None] = time.sleep) -> Callable[[str, str], str]:
    """world_tools._end_standup, sampled, then the raw walk when it didn't step; one record line in `out`."""

    def wrapped(reader: str, body: str) -> str:
        rec: dict[str, Any] = {"step": "standup_probe", "reader": reader, "body": body}
        try:
            rec["sampler"] = ev(SAMPLER % probe.lua_str(body))
        except Exception as e:  # the reply still matters without samples
            rec["sampler_error"] = str(e)
        t0 = time.monotonic()
        reply = orig(reader, body)
        rec["end_standup"] = {"reply": reply, "took_s": round(time.monotonic() - t0, 2)}
        try:
            now = json.loads(ev(NOW))
            rec["end_standup"]["moved_m"] = m = moved(now.get("first"), now.get("last"))
            if m < MOVED_M:
                start = json.loads(ev(probe.move_code(body, None, RAW_DX)))
                sleep(raw_secs)
                end = json.loads(ev(probe.AFTER % probe.lua_str(start["uuid"])))
                rec["raw_move"] = {"at_ms": now.get("t"), "call": start, "after_s": raw_secs, "end": end,
                                   "via": "Osi.CharacterMoveToPosition (probe_revive_stuck move)",
                                   "moved_m": probe.moved_m(start["start"], end["pos"])}
            rec.update(json.loads(ev(STOP)))
        except Exception as e:  # keep what was read
            rec["probe_error"] = str(e)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        print(json.dumps({k: v for k, v in rec.items() if k not in ("samples", "calls")}, default=str)[:3000],
              flush=True)
        return reply

    return wrapped


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out = Path("standup-probe.jsonl")
    if "--standup-out" in args:
        i = args.index("--standup-out")
        out = Path(args[i + 1])
        del args[i:i + 2]
    if "--dry-run" not in args:
        from bg3ai import luadebug
        from bg3ai.tools import world_tools

        made: list[Any] = []
        base = luadebug.LuaDebugClient

        class Shared(base):  # the replay's own debugger connection, shared (requests are multiplexed by id)
            def __init__(self, *a: Any, **k: Any) -> None:
                super().__init__(*a, **k)
                made.append(self)

        luadebug.LuaDebugClient = Shared

        def ev(code: str) -> str:
            return made[0].evaluate(probe.INSTALL + "\n" + code, context="server", timeout=30.0)

        world_tools._end_standup = wrap(world_tools._end_standup, ev, out)
    return replay.main(args)


if __name__ == "__main__":
    sys.exit(main())
