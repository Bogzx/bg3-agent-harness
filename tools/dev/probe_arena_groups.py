"""The two-team probe's q10 step (tools/dev/probe_arena.py): the combat's turn groups read, a split tried, the next
round judged. A mixin of probe_arena.Arena (its ev / raw / call / facts / jobs / waits), tested offline in
tools/dev/test_probe_arena.py."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from probe_arena_judges import SETTLE_S, SIDES, judge_groups
from probe_arena_lua import GROUPS, MOD, SERVER_TRY, lua


class GroupSteps:
    if TYPE_CHECKING:  # probe_arena.Arena's
        sleep: Callable[[float], None]

        def ev(self, code: str, timeout: float = 30.0) -> Any: ...
        def call(self, fn: str, *forms: list[Any]) -> dict[str, Any]: ...
        def facts(self, us: list[str]) -> dict[str, dict[str, Any]]: ...
        def mark(self) -> int: ...
        def since(self, i: int) -> list[dict[str, Any]]: ...
        def raw(self, code: str, context: str) -> Any: ...
        def teams(self) -> dict[str, Any]: ...
        def active(self, teams: dict[str, Any]) -> list[dict[str, Any]]: ...
        def next_turn(self, teams: dict[str, Any], mark: int) -> list[dict[str, Any]]: ...
        def end_turns(self, us: list[str]) -> list[dict[str, Any]]: ...
        def write(self, name: str, rec: dict[str, Any]) -> Path: ...

    def mod_open(self, teams: dict[str, Any]) -> list[dict[str, Any]]:
        """The 8's facts the mod's guard lets act now (Guard.is_my_turn: open, not ended, not held for the other
        side)."""
        allu = teams["A"] + teams["B"]
        us = self.ev(
            f"local G = {MOD}.Guard\nlocal o = {{}}\n"
            f"for _, u in ipairs({lua(allu)}) do if G.is_my_turn(u) then o[#o + 1] = u end end\nreturn P.js(o)"
        )
        fs = self.facts(allu)
        return [fs[u] for u in us if u in fs] if isinstance(us, list) else []

    def turn_groups(self, u: str) -> dict[str, Any]:
        r = self.ev(GROUPS + f"return P.js(P.turn_groups({lua(u)}))")
        return r if isinstance(r, dict) else {"error": str(r)}

    def groups(self, split: str, which: str, user_b: int | None, max_turns: int = 20) -> dict[str, Any]:
        """q10: the turn groups read (each group's Team, initiative, members with their CombatTeam, CombatGroupId,
        roll and user), a split tried, then the round in progress ended (Osi EndTurn on every open turn, past the
        mod's guard) and the next round watched whole: which turns open, for whom. --split write: rewrite the
        `which` list's cross-team groups into per-side runs (P.split_groups); user: AssignToUser(user_b, each team-B
        member); none: the baseline; phases: no engine change, the mod's arena begun and its view judged (a turn =
        the members the mod's guard lets act, ended through act.end_turn), the arena reset after."""
        teams = self.teams()
        a1 = teams["A"][0]
        sides = {u: s for s in SIDES for u in teams[s]}
        m = self.mark()
        r: dict[str, Any] = {"split": split, "list": which, "before": self.turn_groups(a1)}
        phases = split == "phases"
        if phases:
            t = "{A = " + lua(teams["A"]) + ", B = " + lua(teams["B"]) + "}"
            r["write"] = self.raw(SERVER_TRY.format(call=f"{MOD}.Arena.begin({t})"), "server")
        elif split == "write":
            r["write"] = self.ev(GROUPS + f"return P.js(P.split_groups({lua(a1)}, {lua(sides)}, {lua(which)}))")
        elif split == "user":
            r["write"] = [self.call("AssignToUser", [user_b, u]) for u in teams["B"]]
        r["after_write"] = self.turn_groups(a1)
        r0 = r["before"].get("round")
        r["judged_round"] = None if r0 is None else r0 + 1  # the first round that starts after the split
        turns: list[dict[str, Any]] = []
        cur = self.mod_open(teams) if phases else self.active(teams)
        for _ in range(max_turns):
            rnd = self.turn_groups(a1).get("round")
            if rnd is None or r["judged_round"] is None or rnd > r["judged_round"]:
                break
            if not cur:
                cur = self.next_turn(teams, self.mark())
                cur = self.mod_open(teams) if phases else cur
                if not cur:
                    break
                continue
            turns.append({"round": rnd, "open": [f["uuid"] for f in cur], "names": [f.get("name") for f in cur]})
            mark = self.mark()
            if phases:  # inside a shared group the next side opens without a TurnStarted
                r.setdefault("ends", []).extend(self.end_turns([f["uuid"] for f in cur]))
                self.sleep(SETTLE_S)
                cur = self.mod_open(teams)
                continue
            for f in cur:
                self.call("EndTurn", [f["uuid"]])
            cur = self.next_turn(teams, mark)
        if phases:
            r["reset"] = self.raw(SERVER_TRY.format(call=f"{MOD}.Arena.reset()"), "server")
        r.update(turns=turns, after_round=self.turn_groups(a1), events=self.since(m))
        verdict, why = judge_groups(teams, r)
        self.write("q10-groups", {"question": "q10-groups", "verdict": verdict, "why": why, **r})
        return r
