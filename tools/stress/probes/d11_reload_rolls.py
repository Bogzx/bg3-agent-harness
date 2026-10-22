"""D11 reloads reroll (live1 §4, L3): the same 24 passive checks after each load of the save copy.

Every load reseeds the dice from the wall clock (BaseSeed = the load's unix second), so with Karmic Dice off or on
two loads of one save roll different patterns unless a scenario seeds them. PASS: every load gave 24 rolls and no two
patterns are equal. The Karmic setting (roll.snapshot mode) is in the evidence.

    d11_reload_rolls.py [<loads>=3]
"""

from __future__ import annotations

import os

from probelib import Probe, Verdict, arg, main

N_ROLLS = 24


def judge(patterns: list[str], mode: str | None) -> Verdict:
    short = [i for i, pat in enumerate(patterns) if len(pat) != N_ROLLS]
    shown = " ".join(patterns)
    if short:
        return "FAIL", f"load(s) {short} rolled {[len(patterns[i]) for i in short]} of {N_ROLLS}: {shown}"
    if len(set(patterns)) < len(patterns):
        return "FAIL", f"two loads rolled the same pattern (mode {mode}): {shown}"
    return "PASS", f"{len(patterns)} loads, {len(patterns)} different patterns (mode {mode}): {shown}"


def run(p: Probe) -> Verdict:
    loads = int(arg(p, 0, "3"))
    save = os.environ.get("BG3AI_STRESS_SAVE", "")
    if loads > 1 and not save:
        return "FAIL", "BG3AI_STRESS_SAVE is unset: no save copy to reload"
    mode = (p.game.rpc("roll.snapshot") or {}).get("mode")
    patterns = [p.roll_batch(N_ROLLS)]
    for _ in range(loads - 1):
        p.log(p.S.load_game(save=save)[:300])
        if not p.wait_ready():
            return "FAIL", f"the game didn't come back after load {len(patterns) + 1}"
        patterns.append(p.roll_batch(N_ROLLS))
        p.log(patterns[-1])
    return judge(patterns, mode)


if __name__ == "__main__":
    raise SystemExit(main(run))
