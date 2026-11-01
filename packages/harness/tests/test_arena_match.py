# pyright: basic
"""run_arena offline: the scripted two-team GameModel with the mod's harness-only arena ops (arena.setup/status/end
and the referee ring's arena records, as Server/Arena.lua writes them) and scripted agents, one per side, that play
through side-routed ops. Each match's facts validate against the facts schema and bg3bench grades them."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from bg3harness.arena import match as am
from bg3harness.arena.spec import ArenaSpec, ArenaSpecError, Side
from bg3harness.testing.scripted import FakeClock, GameModel

REPO = Path(__file__).resolve().parents[3]
FACTS_SCHEMA = json.loads((REPO / "packages" / "harness" / "schemas" / "facts.v1.schema.json").read_text())
ARENA = {
    "schema": "arena.v1",
    "name": "test_field",
    "title": "Test field",
    "save": {"name": "Tav-1__SCN-arena_test", "sha1": "0" * 40},
    "location": {"level": "WLD_Main_A", "spawns": {"A": [0.0, 0.0, 0.0], "B": [20.0, 0.0, 0.0]}},
    "teams": {"A": [{"name": "Karlach"}, {"name": "Gale"}], "B": [{"name": "Wyll"}, {"name": "Halsin"}]},
    "seed": 7,
    "karmic": False,
    "round_cap": 4,
    "wall_cap_min": 30,
    "turn_cap_s": 60,
    "condition": "rules",
    "tools_off": "arena-v1",
}
SIDES = {"A": Side("A", "pi/model-x"), "B": Side("B", "pi/model-y")}


class ArenaModel(GameModel):
    """GameModel plus the mod's arena module: setup starts the match, every TurnStarted is a ring arena_turn (and
    a turn boundary where a wiped team ends the match), arena.status/end as Arena.lua answers them."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock=clock)
        self.ended: dict[str, Any] | None = None
        self.ends: list[dict[str, Any]] = []  # arena.end calls
        self.alive = True
        self.answers = True
        self.setup_error: str | None = None

    def hp(self, side: str) -> float:
        a = self._match()
        team = a.teams[side]
        return round(sum(m.hp for m in team) / sum(m.max_hp for m in team), 3)

    def finish(self, winner: str | None, reason: str) -> None:
        if self.ended is not None:
            return
        a = self._match()
        self.ended = {
            "winner": winner,
            "reason": reason,
            "rounds": a.round,
            "hp_left_A": self.hp("A"),
            "hp_left_B": self.hp("B"),
        }
        self.ref_record("arena_end", **{k: v for k, v in self.ended.items() if v is not None})

    def _push_turn(self, kind: str) -> None:
        super()._push_turn(kind)
        a = self._match()
        if kind != "turn_started" or self.ended is not None:
            return
        now = a.acting
        self.ref_record("arena_turn", side=a.side_of(now.uuid), character=now.uuid, round=a.round)
        wiped = [s for s in ("A", "B") if all(m.hp == 0 for m in a.teams[s])]
        if wiped:
            self.finish(None if len(wiped) == 2 else ("B" if wiped == ["A"] else "A"), "wipe")

    def reply(self, ctx: str, op: str, args: dict[str, Any]) -> Any:
        if not self.answers:
            return None
        if op == "arena.setup":
            if self.setup_error:
                return {"ok": True, "result": self._failed_job("arena.setup", self.setup_error)}
            teams = args["arena"]["teams"]
            self.ref_record("arena_start", teams={})
            self.start_arena([m["name"] for m in teams["A"]], [m["name"] for m in teams["B"]])
            return {"ok": True, "result": self._job("arena.setup", {"ok": True})}
        if op == "arena.status":
            a = self.arena
            if a is None:
                return {"ok": True, "result": {"active": False}}
            return {
                "ok": True,
                "result": {
                    "active": self.ended is None,
                    "teams": {s: [m.uuid for m in team] for s, team in a.teams.items()},
                    "turn": {"side": a.side_of(a.acting.uuid), "uuid": a.acting.uuid, "round": a.round},
                    "hp": {"A": self.hp("A"), "B": self.hp("B")},
                    "ended": self.ended,
                },
            }
        if op == "arena.end":
            self.ends.append(dict(args))
            self.finish(args.get("winner"), args["reason"])
            return {"ok": True, "result": {"ended": self.ended}}
        return super().reply(ctx, op, args)


class Game:
    """ArenaGame on the model, as FakeMod would route a call: with a side to reply_side."""

    def __init__(self, model: ArenaModel) -> None:
        self.model = model
        self.calls: list[tuple[str, str | None]] = []

    def rpc(self, op: str, args: dict[str, Any], side: str | None = None) -> Any:
        self.calls.append((op, side))
        m = self.model
        r = m.reply_side("server", op, args, side) if side else m.reply("server", op, args)
        if r is None:
            raise TimeoutError(f"{op}: no answer")
        if not r["ok"]:
            raise RuntimeError(r["error"])
        return r["result"]

    def alive(self) -> bool:
        return self.model.alive


class Clock:
    def __init__(self, model_clock: FakeClock) -> None:
        self.t = 1_800_000_000.0
        self.mc = model_clock
        self.tick_hooks: list[Callable[[], None]] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        self.mc.advance(int(s * 1000))
        for hook in self.tick_hooks:
            hook()


# A side's play on one tick: (game, side, run) -> None; it acts through side-routed ops like a bridge does.
Play = Callable[[Game, str, "Run"], None]


class Run:
    def __init__(self, agents: Agents, side: str, prompt: str, conversation: str | None) -> None:
        self.agents, self.side, self.prompt = agents, side, prompt
        self.conversation = conversation or f"conv-{side}"
        self.model = 0.0
        self.exit: am.AgentExit | None = None
        self.stopped = False

    def tick(self) -> None:
        if self.exit is None and not self.stopped:
            with contextlib.suppress(RuntimeError, TimeoutError):  # a tool call refused or not answered
                self.agents.plays[self.side](self.agents.game, self.side, self)

    def end(self, reason: str = "exited", code: int | None = 0) -> None:
        self.exit = am.AgentExit(reason, code, self.conversation, 0.01)

    def done(self) -> am.AgentExit | None:
        return self.exit

    def model_s(self) -> float:
        return self.model

    def stop(self) -> am.AgentExit:
        self.stopped = True
        return self.exit or am.AgentExit("stopped", -2, self.conversation, 0.01)


class Agents:
    def __init__(self, game: Game, clock: Clock, plays: dict[str, Play]) -> None:
        self.game, self.plays = game, plays
        self.started: list[tuple[str, str, str | None, str]] = []
        self.runs: dict[str, Run] = {}
        clock.tick_hooks.append(self.tick)

    def tick(self) -> None:
        for r in list(self.runs.values()):
            r.tick()

    def start(self, side: str, prompt: str, conversation: str | None, tag: str) -> Run:
        self.started.append((side, prompt, conversation, tag))
        self.runs[side] = Run(self, side, prompt, conversation)
        return self.runs[side]


def my_turn(game: Game, side: str) -> str | None:
    """The side's acting character (state.combat from its own view), None when the other side acts."""
    st = game.rpc("state.combat", {}, side)
    mine = any(r.get("is_current") and r["relation"] == "party" for r in st["order"])
    return st["current"] if mine else None


def attacker(game: Game, side: str, run: Run) -> None:
    """On its turn: a hit that drops the first standing enemy, then end_turn (both through its side's ops)."""
    if not (char := my_turn(game, side)):
        return
    m = game.model
    a = m._match()
    foe = next((x for x in a.teams["B" if side == "A" else "A"] if x.hp > 0), None)
    if foe is not None:
        m.arena_damage(foe.name, a.acting.name, foe.hp)
    run.model += 5
    job = game.rpc("act.end_turn", {"char": char}, side)
    assert job["status"] == "done", job


def passer(game: Game, side: str, run: Run) -> None:
    if char := my_turn(game, side):
        run.model += 5
        assert game.rpc("act.end_turn", {"char": char}, side)["status"] == "done"


def staller(game: Game, side: str, run: Run) -> None:
    """Thinks forever on its turn: 20 s of model time a tick, never ends it."""
    if my_turn(game, side):
        run.model += 20


def setup(plays: dict[str, Play], arena: dict | None = None):
    mc = FakeClock()
    model = ArenaModel(mc)
    game = Game(model)
    clock = Clock(mc)
    agents = Agents(game, clock, plays)
    return model, game, clock, agents, ArenaSpec.from_dict(arena or ARENA)


def records(out: Path, name: str = "facts.jsonl") -> list[dict]:
    return [json.loads(x) for x in (out / name).read_text().splitlines()]


def grade(out: Path, arena: dict | None = None) -> dict:
    from bg3bench import arena as barena

    recs = records(out)
    for r in recs:
        jsonschema.validate(r, FACTS_SCHEMA)
    return barena.grade(recs, barena.with_defaults(arena or ARENA))


def arena_events(out: Path) -> list[dict]:
    return [r for r in records(out) if str(r.get("ev", "")).startswith("arena_")]


def test_a_full_match_to_a_wipe(tmp_path):
    model, game, clock, agents, spec = setup({"A": attacker, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is None
    assert out.end == {"winner": "A", "reason": "wipe", "rounds": 1, "hp_left_A": 1.0, "hp_left_B": 0.0}
    evs = arena_events(tmp_path)
    start = evs[0]
    assert start["ev"] == "arena_start" and start["teams"] == {"A": ["Karlach", "Gale"], "B": ["Wyll", "Halsin"]}
    assert start["sides"] == {"A": "pi/model-x", "B": "pi/model-y"} and start["seed"] == 7
    assert start["uuids"]["A"] == [m.uuid for m in model._match().teams["A"]]
    turns = [(e["side"], e["character"], e["round"]) for e in evs if e["ev"] == "arena_turn"]
    assert turns == [("A", "Karlach", 1), ("B", "Wyll", 1), ("A", "Gale", 1), ("B", "Halsin", 1)]
    assert [e["ev"] for e in evs].count("arena_start") == 1 and evs[-1]["ev"] == "arena_end"
    assert agents.started == [
        ("A", "The fight is on. You play side A: Karlach, Gale.", None, ""),
        ("B", "The fight is on. You play side B: Wyll, Halsin.", None, ""),
    ]
    assert model.ends == []  # the mod ended it
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["reason"], g["turns"]) == (True, "A", "wipe", {"A": 2, "B": 2})
    assert out.cost("A") == 0.01 and out.cost("B") == 0.01  # each side's one process, stopped at the end


def test_the_round_cap_is_a_draw_with_the_hp_left(tmp_path):
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    model_hit = []

    def chip() -> None:  # one hit on Wyll before the cap
        if model.arena is not None and not model_hit:
            model.arena_damage("Wyll", "Karlach", 6)
            model_hit.append(1)

    clock.tick_hooks.insert(0, chip)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is None
    assert out.end == {"winner": None, "reason": "round_cap", "rounds": 4, "hp_left_A": 1.0, "hp_left_B": 0.75}
    assert model.ends == [{"reason": "round_cap"}]
    turns = [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn"]
    assert max(e["round"] for e in turns) == 5  # the turn of round 5 that ended it, before any action of it
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["reason"], g["rounds"]) == (True, "draw", "round_cap", 4)


def test_three_forced_turns_in_a_row_forfeit(tmp_path):
    model, game, clock, agents, spec = setup({"A": passer, "B": staller})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.invalid is None
    assert (out.end["winner"], out.end["reason"], out.end["forfeit"], out.end["cause"]) == (
        "A",
        "forfeit",
        "B",
        "forced_turns",
    )
    forced = [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert [(e["side"], e["character"]) for e in forced] == [("B", "Wyll"), ("B", "Halsin"), ("B", "Wyll")]
    # the harness's end_turn goes as the acting side, like that side's own bridge
    assert [c for c in game.calls if c[0] == "act.end_turn" and c[1] == "B"][:1] == [("act.end_turn", "B")]
    assert model.ends == [{"reason": "forfeit", "winner": "A"}]
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["forfeit"], g["forced"]) == (
        True,
        "A",
        {"side": "B", "cause": "forced_turns"},
        {"A": 0, "B": 3},
    )


def test_a_turn_under_the_cap_is_never_forced(tmp_path):
    _model, game, clock, agents, spec = setup({"A": attacker, "B": passer})
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert not [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]


def test_a_client_fault_is_resumed_once_then_forfeits(tmp_path):
    faults = []

    def flaky(game: Game, side: str, run: Run) -> None:
        if len(faults) < 2:
            faults.append(run.conversation)
            run.end("mcp_failed", 1)
            return
        passer(game, side, run)

    _model, game, clock, agents, spec = setup({"A": passer, "B": flaky})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None
    assert (out.end["winner"], out.end["reason"], out.end["forfeit"], out.end["cause"]) == (
        "A",
        "forfeit",
        "B",
        "client_fault",
    )
    assert agents.started[2:] == [("B", "The fight is on.", "conv-B", "-r1")]  # one resume, same conversation
    assert [e.reason for e in out.exits["B"]] == ["mcp_failed", "mcp_failed"]
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["forfeit"]) == (True, "A", {"side": "B", "cause": "client_fault"})


def test_an_agent_that_exits_mid_fight_is_resumed_up_to_three_times(tmp_path):
    def quitter(game: Game, side: str, run: Run) -> None:
        run.end()  # exit 0 on its first tick, every time

    _model, game, clock, agents, spec = setup({"A": quitter, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert [t for (s, _p, _c, t) in agents.started if s == "A"] == ["", "-r1", "-r2", "-r3"]
    assert out.end is not None and (out.end["forfeit"], out.end["cause"]) == ("A", "client_fault")
    assert len(out.exits["A"]) == 4


def test_a_game_crash_is_an_invalid_match(tmp_path):
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})

    def crash() -> None:
        if model.arena is not None and model.arena.round == 2:
            model.alive = False

    clock.tick_hooks.insert(0, crash)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is not None and out.invalid.startswith("fault")
    assert out.end is not None and out.end["reason"] == "fault" and model.ends == []
    assert all(r.stopped for r in agents.runs.values())
    g = grade(tmp_path)
    assert (g["valid"], g["invalid_reason"]) == (False, "fault")


def test_a_game_that_stops_answering_is_a_fault_after_the_hang_limit(tmp_path):
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    t0: list[float] = []

    def hang() -> None:
        if model.arena is not None and model.arena.round == 2 and model.answers:
            model.answers = False
            t0.append(clock.t)

    clock.tick_hooks.insert(0, hang)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and out.end["reason"] == "fault"
    assert clock.t - t0[0] == pytest.approx(am.HANG_S, abs=2 * am.POLL_S)


def test_a_failed_setup_starts_no_agent(tmp_path):
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    model.setup_error = "team B: Wyll is not in the party"
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert (
        out.end is None
        and out.invalid == "setup: ArenaSetupError: arena.setup failed: team B: Wyll is not in the party"
    )
    assert agents.started == []
    assert not arena_events(tmp_path)


def test_the_wall_cap_is_a_draw(tmp_path):
    arena = dict(ARENA, wall_cap_min=1, round_cap=100)
    _model, game, clock, agents, spec = setup({"A": passer, "B": passer}, arena)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and (out.end["reason"], out.end["winner"]) == ("wall_cap", None)
    assert grade(tmp_path, arena)["result"] == "draw"


def test_an_operator_abort_is_invalid(tmp_path):
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock, aborted=lambda: clock.t > 1_800_000_003)
    assert out.end is not None and out.end["reason"] == "abort" and model.ends == [{"reason": "abort"}]
    assert grade(tmp_path)["invalid_reason"] == "abort"


def test_ring_records_of_the_mod_are_translated_or_left_out(tmp_path):
    """The mod's own arena_start and a runner end's arena_end never reach facts; a turn names the character; a
    turn of someone outside the teams (a summon) is not written."""
    _model, _game, clock, _agents, spec = setup({"A": passer, "B": passer})
    from bg3harness import facts as hfacts

    ref = am.Referee(spec, SIDES, hfacts.FactsWriter(tmp_path / "f.jsonl", clock=clock.now))
    ref.start({"A": ["UA1", "UA2"], "B": ["UB1", "UB2"]})
    assert ref.translate({"ev": "arena_start", "teams": {}}) is None
    assert ref.translate({"ev": "arena_turn", "side": "A", "character": "ua2", "round": 1}) == {
        "ev": "arena_turn",
        "side": "A",
        "character": "Gale",
        "round": 1,
    }
    assert ref.translate({"ev": "arena_turn", "side": "A", "character": "summon-1", "round": 1}) is None
    assert ref.translate({"ev": "arena_end", "reason": "forfeit", "rounds": 1}) is None
    assert ref.translate({"ev": "kill", "name": "x"}) == {"ev": "kill", "name": "x"}
    wipe = ref.translate({"ev": "arena_end", "reason": "wipe", "rounds": 1, "hp_left_A": 0, "hp_left_B": 0})
    assert wipe is not None and wipe["winner"] is None


def test_the_spec_refuses_a_draft_or_incomplete_arena():
    with pytest.raises(ArenaSpecError, match="draft"):
        ArenaSpec.from_dict(dict(ARENA, draft="placeholder"))
    with pytest.raises(ArenaSpecError, match="turn_cap_s"):
        ArenaSpec.from_dict({k: v for k, v in ARENA.items() if k != "turn_cap_s"})
    with pytest.raises(ArenaSpecError, match="karmic"):
        ArenaSpec.from_dict(dict(ARENA, karmic=True))
    spec = ArenaSpec.from_dict(ARENA)
    assert spec.teams == {"A": ("Karlach", "Gale"), "B": ("Wyll", "Halsin")} and spec.arena == ARENA
