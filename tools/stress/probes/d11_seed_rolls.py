"""D11 the dice can be seeded (live1 §5, L4): the ServerRollStreams component's seed decides the rolls.

Writes BaseSeed and every stream's Seed = S + i, NumRolls = 0 on the single ServerRollStreams entity (the system
copy is overwritten from it within a tick), then 24 passive checks; seeds A, A, B, A. PASS: the three A batches
roll one pattern and B another.
"""

from __future__ import annotations

from probelib import Probe, Verdict, main

SET = """local r=Ext.Entity.GetAllEntitiesWithComponent("ServerRollStreams")[1].ServerRollStreams
r.BaseSeed = %d
for i,s in ipairs(r.Streams) do s.State.Seed = %d + i; s.State.NumRolls = 0 end
return tostring(r.BaseSeed)"""
SEEDS = (("A1", 12345), ("A2", 12345), ("B", 54321), ("A3", 12345))


def judge(got: dict[str, str]) -> Verdict:
    a = {got["A1"], got["A2"], got["A3"]}
    shown = ", ".join(f"{k} {v}" for k, v in got.items())
    if any(len(v) != 24 for v in got.values()):
        return "FAIL", f"a batch didn't roll 24: {shown}"
    if len(a) != 1:
        return "FAIL", f"seed 12345 rolled different patterns: {shown}"
    if got["B"] in a:
        return "FAIL", f"seeds 12345 and 54321 rolled the same pattern: {shown}"
    return "PASS", f"seed 12345 repeats x3, 54321 differs: {shown}"


def run(p: Probe) -> Verdict:
    got = {}
    for tag, seed in SEEDS:
        p.log(f"{tag} base_seed {p.lua(SET % (seed, seed))}")
        got[tag] = p.roll_batch()
    return judge(got)


if __name__ == "__main__":
    raise SystemExit(main(run))
