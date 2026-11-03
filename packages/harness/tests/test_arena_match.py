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
from bg3harness.testing.fakemod import unknown
from bg3harness.testing.scripted import FakeClock, GameModel, Member

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


def wrong_context(ctx: str, op: str) -> dict[str, Any] | None:
    """The mod's refusal of an op sent to a context that doesn't register it (Client/: ui.*, autoskip.*, arena.camera;
    Shared/:    events, job, sys, hotbar, in both; Server/: the rest), else None."""
    shared = op.startswith(("events.", "job.", "sys.", "hotbar."))
    client = op.startswith(("ui.", "autoskip.")) or op == "arena.camera"
    if shared or (ctx == "client") == client:
        return None
    return unknown(ctx, op)


class ArenaModel(GameModel):
    """GameModel plus the mod's arena module: setup starts the match (in `order`, names, when given; one shared turn
    group per round, as live), a change of the acting side or the round is a phase: one ring arena_turn {side,
    character, members (the side's run of the order from the acting one), round, phase, group, shared}
    (Ar.phase_check), every TurnStarted a turn boundary where a wiped team ends the match, the start of round
    round_cap + 1 ends it as a draw with the round left at the cap (caps=False: a mod without the cap),
    arena.status/end as Arena.lua answers them. The one game client's reaction prompt holds the game (a side's
    act.end_turn waits); read with no side, a prompt naming nobody of exactly one team is unowned (UIState.lua) and
    the harness answers it. An op sent to a context the mod doesn't register it in is refused (wrong_context).
    hold: the game takes a side's act.end_turn ("done") but never moves the turn on (live 7: a downed member's turn),
    the member's row in state.combat then ended; refuse_end: every act.end_turn fails with it; combat_error:
    state.combat fails; phases=False: no arena_turn record ever comes. A side's state.combat rows show ended, downed
    (hp 0) as StateWorld.lua does."""

    def __init__(self, clock: FakeClock, caps: bool = True, order: list[str] | None = None) -> None:
        super().__init__(clock=clock)
        self.order = order
        self.phase: tuple[int, str | None, int] = (0, None, 0)  # (round, acting side, phase index) of the last record
        self.ended: dict[str, Any] | None = None
        self.ends: list[dict[str, Any]] = []  # arena.end calls
        self.alive = True
        self.answers = True
        self.setup_error: str | None = None
        self.caps = caps
        self.setup_args: dict[str, Any] = {}
        self.round_cap: int | None = None
        self.mod_round = 0  # the mod's st.round
        self.summon_names: dict[str, str] = {}  # summon name -> side: in the match from its start, in `order`
        self.summons: set[str] = set()  # their uuids (not in arena.status's teams)
        self.hold = False
        self.phases = True
        self.refuse_end: str | None = None
        self.combat_error: str | None = None
        self.held_ends: set[str] = set()  # the uuids whose end the game took while holding
        self.cameras = 0  # arena.camera calls (no side)
        self.camera_error: str | None = None  # arena.camera fails with it
        self.camera_skips = 0  # the first arena.camera calls this many answer skipped (a side's input)

    def team_uuids(self) -> dict[str, list[str]]:
        """Each team's uuids as arena.setup's result and arena.status give them (Ar.state.teams: no summons)."""
        return {s: [m.uuid for m in team if m.uuid not in self.summons] for s, team in self.match().teams.items()}

    def hp(self, side: str) -> float:
        a = self.match()
        team = a.teams[side]
        return round(sum(m.hp for m in team) / sum(m.max_hp for m in team), 3)

    def finish(self, winner: str | None, reason: str) -> None:
        if self.ended is not None:
            return
        self.ended = {
            "winner": winner,
            "reason": reason,
            "rounds": self.mod_round,
            "hp_left_A": self.hp("A"),
            "hp_left_B": self.hp("B"),
        }
        self.ref_record("arena_end", **{k: v for k, v in self.ended.items() if v is not None})

    def push(self, kind: str, data: dict[str, Any] | None = None, ctx: str = "server", views=None) -> int:
        seq = super().push(kind, data, ctx, views)
        if kind == "round_started" and self.ended is None:  # Ar.round_started
            r = int((data or {})["round"])
            if self.round_cap is not None and r > self.round_cap:
                self.finish(None, "round_cap")
            else:
                self.mod_round = r
        return seq

    def _push_turn(self, kind: str, who: Member | None = None, rnd: int | None = None) -> None:
        super()._push_turn(kind, who, rnd)
        a = self.match()
        if kind != "turn_started":  # a round change: round_started, this turn_started, then the old round's turn_ended
            return
        now = a.acting
        self.mod_round = max(self.mod_round, 1)
        side = a.side_of(now.uuid)
        rnd, last, n = self.phase
        if (rnd, last) != (self.mod_round, side) and self.phases:
            n = n + 1 if rnd == self.mod_round else 1
            self.phase = (self.mod_round, side, n)
            members = []
            for u in a.order[a.turn :]:
                if a.side_of(u) != side:
                    break
                members.append(u)
            sides = {a.side_of(u) for u in a.order}
            self.ref_record(
                "arena_turn",
                side=side,
                character=now.uuid,
                members=members,
                names=[a.member(u).name for u in members],
                round=self.mod_round,
                phase=n,
                group="team-1",
                shared=len(sides) == 2,
            )
        wiped = [s for s in ("A", "B") if all(m.hp == 0 for m in a.teams[s])]
        if wiped:
            self.finish(None if len(wiped) == 2 else ("B" if wiped == ["A"] else "A"), "wipe")

    def combat_end(self) -> None:
        """The arena's combat ended without a wipe (Ar.combat_ended)."""
        if self.ended is None:
            self.ref_record("arena_combat_ended", rounds=self.mod_round)

    def owners(self, who: str) -> list[str]:
        return [s for s, team in self.match().teams.items() if any(m.name == who for m in team)]

    def reply_side(self, ctx: str, op: str, args: dict[str, Any], side: str) -> Any:
        if (refused := wrong_context(ctx, op)) is not None:
            return refused
        if op == "act.end_turn" and self.reaction is not None:
            return {"ok": True, "result": self.failed_job(op, "a reaction prompt is open", side)}
        if op == "act.end_turn" and self.refuse_end:
            return {"ok": True, "result": self.failed_job(op, self.refuse_end, side)}
        if op == "act.end_turn" and self.hold:
            self.held_ends.add(self.match().member(str(args["char"])).uuid)
            return {"ok": True, "result": self.settled_job(op, {"ended": True}, side)}
        if op == "state.combat" and self.combat_error:
            return {"ok": False, "error": self.combat_error}
        r = super().reply_side(ctx, op, args, side)
        if op == "state.combat" and r and r.get("ok"):
            for row in r["result"]["order"]:
                if row["uuid"] in self.held_ends:
                    row["ended"] = True
                if row["relation"] == "party" and self.match().member(row["uuid"]).hp == 0:
                    row["downed"] = True
        return r

    def reply(self, ctx: str, op: str, args: dict[str, Any]) -> Any:
        if not self.answers:
            return None
        if (refused := wrong_context(ctx, op)) is not None:
            return refused
        if op == "arena.setup":
            if self.setup_error:
                return {"ok": True, "result": self.failed_job("arena.setup", self.setup_error)}
            self.setup_args = dict(args)
            self.round_cap = args.get("round_cap") if self.caps else None
            teams = args["arena"]["teams"]
            self.ref_record("arena_start", teams={})
            extra = {s: [n for n, side in self.summon_names.items() if side == s] for s in ("A", "B")}
            names = {s: [m["name"] for m in teams[s]] + extra[s] for s in ("A", "B")}
            self.start_arena(names["A"], names["B"], self.order)
            self.summons = {self.match().member(n).uuid for n in self.summon_names}
            return {"ok": True, "result": self.settled_job("arena.setup", {"ok": True, "teams": self.team_uuids()})}
        if op == "arena.status":
            a = self.arena
            if a is None:
                return {"ok": True, "result": {"active": False}}
            return {
                "ok": True,
                "result": {
                    "active": self.ended is None,
                    "teams": self.team_uuids(),
                    "turn": {"side": a.side_of(a.acting.uuid), "uuid": a.acting.uuid, "round": self.mod_round},
                    "hp": {"A": self.hp("A"), "B": self.hp("B")},
                    "ended": self.ended,
                },
            }
        if op == "arena.end":
            self.ends.append(dict(args))
            self.finish(args.get("winner"), args["reason"])
            return {"ok": True, "result": {"ended": self.ended}}
        if op == "arena.camera":
            self.cameras += 1
            r = {"moved": False, "skipped": "side A's input"} if self.cameras <= self.camera_skips else {"moved": True}
            return {"ok": False, "error": self.camera_error} if self.camera_error else {"ok": True, "result": r}
        if op == "ui.state" and self.reaction is not None and self.arena is not None:
            ui = super().reply(ctx, op, args)
            assert ui is not None
            if len(self.owners(self.reaction["options"][0]["who"])) != 1:
                ui["result"]["reaction"]["unowned"] = True
            return ui
        if op == "ui.react" and self.reaction is not None:
            self.reacted.append((self.reaction["options"][0]["who"], bool(args.get("use"))))
            self.reaction = None
            return {"ok": True, "result": self.settled_job("ui.react", {"answered": "skip"})}
        return super().reply(ctx, op, args)


class Game:
    """ArenaGame on the model, as FakeMod would route a call: with a side to reply_side."""

    def __init__(self, model: ArenaModel) -> None:
        self.model = model
        self.calls: list[tuple[str, str | None]] = []

    def rpc(self, op: str, args: dict[str, Any], side: str | None = None, context: str = "server") -> Any:
        self.calls.append((op, side))
        m = self.model
        r = m.reply_side(context, op, args, side) if side else m.reply(context, op, args)
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
        self.cost = 0.01  # its client's running cost (USD); a play may add to it
        self.ended: set[str] = set()  # ender_once's ended members
        self.exit: am.AgentExit | None = None
        self.stopped = False

    def tick(self) -> None:
        if self.exit is None and not self.stopped:
            with contextlib.suppress(RuntimeError, TimeoutError):  # a tool call refused or not answered
                self.agents.plays[self.side](self.agents.game, self.side, self)

    def end(self, reason: str = "exited", code: int | None = 0) -> None:
        self.exit = am.AgentExit(reason, code, self.conversation, self.cost)

    def done(self) -> am.AgentExit | None:
        return self.exit

    def model_s(self) -> float:
        return self.model

    def cost_usd(self) -> float:
        return self.cost

    def stop(self) -> am.AgentExit:
        self.stopped = True
        return self.exit or am.AgentExit("stopped", -2, self.conversation, self.cost)


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
    a = m.match()
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


def setup(plays: dict[str, Play], arena: dict | None = None, caps: bool = True, order: list[str] | None = None):
    mc = FakeClock()
    model = ArenaModel(mc, caps, order)
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
    assert start["uuids"]["A"] == [m.uuid for m in model.match().teams["A"]]
    turns = [(e["side"], e["character"], e["round"]) for e in evs if e["ev"] == "arena_turn"]
    assert turns == [("A", "Karlach", 1), ("B", "Wyll", 1), ("A", "Gale", 1), ("B", "Halsin", 1)]
    assert [e["ev"] for e in evs].count("arena_start") == 1 and evs[-1]["ev"] == "arena_end"
    assert "invalid" not in evs[-1]  # two models' match
    assert agents.started == [
        ("A", "The fight is on. You play side A: Karlach, Gale.", None, ""),
        ("B", "The fight is on. You play side B: Wyll, Halsin.", None, ""),
    ]
    assert model.ends == []  # the mod ended it
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["reason"], g["turns"]) == (True, "A", "wipe", {"A": 2, "B": 2})
    assert out.cost("A") == 0.01 and out.cost("B") == 0.01  # each side's one process, stopped at the end


def chip_once(model: ArenaModel) -> Callable[[], None]:
    hit = []

    def chip() -> None:  # one hit on Wyll before the cap
        if model.arena is not None and not hit:
            model.arena_damage("Wyll", "Karlach", 6)
            hit.append(1)

    return chip


def test_the_mods_round_cap_ends_the_match_as_a_draw(tmp_path):
    """The mod ends the match at the start of round cap + 1 and leaves its round at the cap: the runner stops on
    that end (it never sees a turn past the cap) and writes it once."""
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    clock.tick_hooks.insert(0, chip_once(model))
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert model.setup_args["round_cap"] == 4
    assert out.invalid is None
    assert out.end == {"winner": None, "reason": "round_cap", "rounds": 4, "hp_left_A": 1.0, "hp_left_B": 0.75}
    assert model.ends == []  # the mod ended it
    evs = arena_events(tmp_path)
    assert [e["ev"] for e in evs].count("arena_end") == 1 and evs[-1]["ev"] == "arena_end"
    assert max(e["round"] for e in evs if e["ev"] == "arena_turn") == 4
    assert clock.t - 1_800_000_000 < 60  # stopped at the cap, not played on to the wall cap
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["reason"], g["rounds"]) == (True, "draw", "round_cap", 4)


def test_a_mod_without_the_round_cap_is_capped_by_the_runner(tmp_path):
    model, game, clock, agents, spec = setup({"A": passer, "B": passer}, caps=False)
    clock.tick_hooks.insert(0, chip_once(model))
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is None
    assert out.end == {"winner": None, "reason": "round_cap", "rounds": 4, "hp_left_A": 1.0, "hp_left_B": 0.75}
    assert model.ends == [{"reason": "round_cap"}]
    evs = arena_events(tmp_path)
    turns = [e for e in evs if e["ev"] == "arena_turn"]
    assert max(e["round"] for e in turns) == 5  # the turn of round 5 that ended it, before any action of it
    assert [e["ev"] for e in evs].count("arena_end") == 1  # the mod's record of the runner's end is left out
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["reason"], g["rounds"]) == (True, "draw", "round_cap", 4)


def test_the_arenas_combat_ending_without_a_wipe_is_an_invalid_match(tmp_path):
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})

    def end_combat() -> None:
        if model.arena is not None and model.mod_round == 2 and not model.ends:
            model.combat_end()

    clock.tick_hooks.insert(0, end_combat)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid == "fault: the arena's combat ended without a wipe (rounds 2)"
    assert out.end is not None and (out.end["reason"], out.end["winner"], out.end["rounds"]) == ("fault", None, 2)
    assert model.ends == [{"reason": "fault"}]  # the game is up: the mod hears the end
    assert [e["ev"] for e in arena_events(tmp_path)].count("arena_end") == 1
    g = grade(tmp_path)
    assert (g["valid"], g["invalid_reason"]) == (False, "fault")


def patient(game: Game, side: str, run: Run) -> None:
    """Ends its turn; while a reaction prompt holds the game, its end_turn waits (no model time)."""
    if (char := my_turn(game, side)) and game.rpc("act.end_turn", {"char": char}, side)["status"] == "done":
        run.model += 5


def open_at_round_2(model: ArenaModel, who: str) -> Callable[[], None]:
    def hook() -> None:
        if model.arena is not None and model.mod_round == 2 and not model.reacted and model.reaction is None:
            model.open_reaction(who)

    return hook


def test_an_unowned_reaction_prompt_is_declined_once_and_recorded(tmp_path):
    """A prompt for someone on neither team holds the game: neither side may answer it, so the runner declines it
    (with no side) and records it. Turns that move on cost no ui.state read."""
    model, game, clock, agents, spec = setup({"A": patient, "B": patient})
    clock.tick_hooks.insert(0, open_at_round_2(model, "Wolf"))
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert model.reacted == [("Wolf", False)]
    assert game.calls.count(("ui.react", None)) == 1
    assert game.calls.count(("ui.state", None)) == 1  # read only once the held turn was UNOWNED_CHECK_S old
    unowned = [e for e in arena_events(tmp_path) if e["ev"] == "arena_reaction_unowned"]
    assert [(e["round"], e["detail"]) for e in unowned] == [(2, "Wolf | Shield | Choose how to react.")]
    assert out.end is not None and out.end["reason"] == "round_cap" and out.invalid is None
    assert grade(tmp_path)["valid"]


def test_a_reaction_prompt_a_team_owns_is_left_to_that_side(tmp_path):
    model, game, clock, agents, spec = setup({"A": patient, "B": patient})
    clock.tick_hooks.insert(0, open_at_round_2(model, "Wyll"))

    def answer() -> None:  # side B answers its prompt once the runner has looked at it
        if model.reaction is not None and ("ui.state", None) in game.calls:
            game.rpc("ui.react", {"use": True}, "B", context="client")

    clock.tick_hooks.append(answer)
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert model.reacted == [("Wyll", True)]
    assert ("ui.react", None) not in game.calls
    assert not [e for e in arena_events(tmp_path) if e["ev"] == "arena_reaction_unowned"]


def test_an_end_of_the_mods_own_before_the_runners_is_the_one_written(tmp_path):
    """The mod wipes team B after the tick's status read, before the runner's wall-cap arena.end: arena.end answers
    with the mod's first end, and that is the match's (one arena_end), naming the runner's end in detail."""
    arena = dict(ARENA, wall_cap_min=1, round_cap=100)
    model, game, clock, agents, spec = setup({"A": passer, "B": passer}, arena)
    rpc = game.rpc

    def wipe_first(op: str, args: dict[str, Any], side: str | None = None) -> Any:
        if op == "arena.end":
            for m in model.match().teams["B"]:
                m.hp = 0
            model.finish("A", "wipe")
        return rpc(op, args, side)

    game.rpc = wipe_first  # type: ignore[method-assign]
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is None and model.ends == [{"reason": "wall_cap"}]
    assert out.end is not None
    assert {k: out.end[k] for k in ("winner", "reason", "hp_left_A", "hp_left_B")} == {
        "winner": "A",
        "reason": "wipe",
        "hp_left_A": 1.0,
        "hp_left_B": 0.0,
    }
    assert model.ended is not None and out.end["rounds"] == model.ended["rounds"]
    assert out.end["detail"] == "the runner's end came after it: wall_cap"
    ends = [e for e in arena_events(tmp_path) if e["ev"] == "arena_end"]
    assert len(ends) == 1 and ends[0]["reason"] == "wipe"
    g = grade(tmp_path, arena)
    assert (g["valid"], g["result"], g["reason"]) == (True, "A", "wipe")


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


def test_the_third_forced_turn_forfeits_before_any_end_turn_so_no_wipe_comes_first(tmp_path):
    """The game wipes team A at the turn boundary after a forced end_turn of B's once B has two forced turns: the
    forfeit is decided at the third cap, so the runner sends no end_turn there and the mod's first end is the
    forfeit (one arena_end, a valid match)."""
    model, game, clock, agents, spec = setup({"A": passer, "B": staller})
    rpc = game.rpc

    def wipe_after_end_turn(op: str, args: dict[str, Any], side: str | None = None) -> Any:
        r = rpc(op, args, side)
        if op == "act.end_turn" and side == "B" and game.calls.count(("act.end_turn", "B")) >= 3:
            for m in model.match().teams["A"]:
                m.hp = 0
            model.finish("B", "wipe")
        return r

    game.rpc = wipe_after_end_turn  # type: ignore[method-assign]
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert game.calls.count(("act.end_turn", "B")) == 2  # the two forced turns before the forfeit
    assert model.ends == [{"reason": "forfeit", "winner": "A"}] and model.ended is not None
    assert model.ended["reason"] == "forfeit"
    assert out.end is not None and (out.end["reason"], out.end["winner"], out.end["forfeit"]) == ("forfeit", "A", "B")
    evs = arena_events(tmp_path)
    assert [e["ev"] for e in evs].count("arena_end") == 1
    forced = [(e["side"], e["character"]) for e in evs if e["ev"] == "arena_turn_forced"]
    assert forced == [("B", "Wyll"), ("B", "Halsin"), ("B", "Wyll")]
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["forfeit"]) == (True, "A", {"side": "B", "cause": "forced_turns"})


def test_a_turn_under_the_cap_is_never_forced(tmp_path):
    _model, game, clock, agents, spec = setup({"A": attacker, "B": passer})
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert not [e for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]


ARENA4 = dict(
    ARENA,
    teams={
        "A": [{"name": "Karlach"}, {"name": "Gale"}, {"name": "Astarion"}, {"name": "Shadowheart"}],
        "B": [{"name": "Wyll"}, {"name": "Halsin"}, {"name": "Minthara"}, {"name": "Jaheira"}],
    },
)
ORDER4 = ["Wyll", "Karlach", "Gale", "Astarion", "Shadowheart", "Halsin", "Minthara", "Jaheira"]  # B|AAAA|BBB


def phases(out: Path) -> list[tuple]:
    return [
        (e["round"], e["phase"], e["side"], e["character"], e["members"], e["shared"])
        for e in arena_events(out)
        if e["ev"] == "arena_turn"
    ]


def test_a_shared_group_is_one_turn_per_phase(tmp_path):
    """One turn group B|AAAA|BBB is three turns (phases), each written once with its members."""
    _model, game, clock, agents, spec = setup({"A": passer, "B": passer}, ARENA4, order=ORDER4)
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert phases(tmp_path)[:4] == [
        (1, 1, "B", "Wyll", ["Wyll"], True),
        (1, 2, "A", "Karlach", ["Karlach", "Gale", "Astarion", "Shadowheart"], True),
        (1, 3, "B", "Halsin", ["Halsin", "Minthara", "Jaheira"], True),
        (2, 1, "B", "Wyll", ["Wyll"], True),
    ]
    assert len(phases(tmp_path)) == 3 * spec.round_cap
    g = grade(tmp_path, ARENA4)
    assert (g["valid"], g["reason"], g["turns"]) == (True, "round_cap", {"A": 4, "B": 8})


def test_a_capped_phase_has_turn_cap_times_its_members_and_ends_each_open_member(tmp_path):
    """Side A ends Karlach and Gale itself, then thinks on: its 4-member phase is capped at 4 x turn_cap_s of model
    time, where the runner ends each member in turn order as side A (the two already ended are refused) and writes
    one arena_turn_forced naming the phase."""
    ends: list[tuple[str, str | None, float]] = []

    def half(game: Game, side: str, run: Run) -> None:
        if not (char := my_turn(game, side)):
            return
        if game.model.match().acting.name in ("Karlach", "Gale"):
            run.model += 5
            game.rpc("act.end_turn", {"char": char}, side)
        else:
            run.model += 20

    model, game, clock, agents, spec = setup({"A": half, "B": passer}, ARENA4, order=ORDER4)
    rpc = game.rpc

    def watch(op: str, args: dict[str, Any], side: str | None = None) -> Any:
        if op == "act.end_turn" and side == "A":
            ends.append((model.match().member(args["char"]).name, side, agents.runs["A"].model))
        return rpc(op, args, side)

    game.rpc = watch  # type: ignore[method-assign]
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    a = model.match()
    runner = [(n, m) for n, _s, m in ends[2:6]]  # round 1: Karlach and Gale by the agent, then the runner's four
    assert [n for n, _m in runner] == ["Karlach", "Gale", "Astarion", "Shadowheart"]
    assert runner[0][1] >= 10 + 4 * spec.turn_cap_s  # the phase's cap: 4 x turn_cap_s past its start
    assert all(m == runner[0][1] for _n, m in runner)  # all four at that cap
    forced = [(e["side"], e["character"]) for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert forced[0] == ("A", "Karlach")
    assert phases(tmp_path)[2][:4] == (1, 3, "B", "Halsin")  # B's phase came next
    assert a.side_of(a.member("Halsin").uuid) == "B"
    g = grade(tmp_path, ARENA4)
    assert g["valid"] and g["forced"]["A"] >= 1


def test_the_caps_ends_go_one_at_a_time(tmp_path):
    """A cap's act.end_turn that is still running is polled to its end before the next member's is sent."""
    model, game, clock, agents, spec = setup({"A": staller, "B": passer}, ARENA4, order=ORDER4)
    rpc = game.rpc
    log: list[str] = []
    running: dict[int, int] = {}  # job -> polls left

    def slow(op: str, args: dict[str, Any], side: str | None = None) -> Any:
        if op == "act.end_turn" and side == "A":
            log.append("end " + model.match().member(args["char"]).name)
            r = rpc(op, args, side)
            running[r["job"]] = 2
            return {"job": r["job"], "status": "running"}
        if op == "job.poll" and side == "A" and running.get(args["id"]):
            running[args["id"]] -= 1
            log.append("poll")
            return {"id": args["id"], "status": "running"}
        if op == "job.poll" and side == "A":
            log.append("settled")
        return rpc(op, args, side)

    game.rpc = slow  # type: ignore[method-assign]
    am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    one = ["poll", "poll", "settled"]
    assert log[:16] == [
        "end Karlach",
        *one,
        "end Gale",
        *one,
        "end Astarion",
        *one,
        "end Shadowheart",
        *one,
    ]


def test_forfeit_counts_phases(tmp_path):
    """Side B's three phases in a row capped (Wyll, Halsin-Minthara-Jaheira, Wyll: five members) forfeit; the third
    cap sends no end_turn."""
    model, game, clock, agents, spec = setup({"A": passer, "B": staller}, ARENA4, order=ORDER4)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    forced = [(e["side"], e["character"]) for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert forced == [("B", "Wyll"), ("B", "Halsin"), ("B", "Wyll")]
    assert game.calls.count(("act.end_turn", "B")) == 1 + 3  # Wyll's phase, then Halsin's three members
    assert out.end is not None and (out.end["reason"], out.end["forfeit"], out.end["cause"]) == (
        "forfeit",
        "B",
        "forced_turns",
    )
    assert model.ends == [{"reason": "forfeit", "winner": "A"}]
    g = grade(tmp_path, ARENA4)
    assert (g["valid"], g["result"], g["forced"]) == (True, "A", {"A": 0, "B": 3})


def test_a_summons_phase_is_a_turn_of_its_side_capped_and_counted_toward_forfeit(tmp_path):
    """Side B's wolf (a summon) acts alone between Karlach and Gale: its phase is written with the wolf's name,
    capped at turn_cap_s, ended at the cap and counted toward B's forfeit like any phase (Wolf, Wyll-Halsin,
    Wolf)."""
    model, game, clock, agents, spec = setup(
        {"A": passer, "B": staller}, order=["Karlach", "Wolf", "Gale", "Wyll", "Halsin"]
    )
    model.summon_names = {"Wolf": "B"}
    rpc = game.rpc
    ended: list[str] = []

    def watch(op: str, args: dict[str, Any], side: str | None = None) -> Any:
        if op == "act.end_turn" and side == "B":
            ended.append(model.match().member(args["char"]).name)
        return rpc(op, args, side)

    game.rpc = watch  # type: ignore[method-assign]
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert [p[:5] for p in phases(tmp_path)[:3]] == [
        (1, 1, "A", "Karlach", ["Karlach"]),
        (1, 2, "B", "Wolf", ["Wolf"]),
        (1, 3, "A", "Gale", ["Gale"]),
    ]
    forced = [(e["side"], e["character"]) for e in arena_events(tmp_path) if e["ev"] == "arena_turn_forced"]
    assert forced == [("B", "Wolf"), ("B", "Wyll"), ("B", "Wolf")]
    assert ended == ["Wolf", "Wyll", "Halsin"]  # the third cap forfeits with no end_turn
    assert out.end is not None and (out.end["reason"], out.end["forfeit"]) == ("forfeit", "B")
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["forced"]) == (True, "A", {"A": 0, "B": 3})


@pytest.mark.parametrize(
    ("reason", "code", "cause"),
    [("api_error", 1, "api_error"), ("mcp_failed", 1, "mcp_failed"), ("agent_hang", -15, "agent_hang"),
     ("exited", 1, "crash")],
)  # fmt: skip
def test_a_client_fault_is_resumed_once_then_the_match_is_invalid(tmp_path, reason, code, cause):
    """Owner D459 (fairness audit S1-1): a side's provider ("overloaded" twice), our bridge/MCP server or a hang
    failing again after its resume is not the model's play: the match is invalid (client_fault, rerun), no winner,
    arena_end naming the side and the cause. It was a forfeit loss."""
    faults = []

    def flaky(game: Game, side: str, run: Run) -> None:
        if len(faults) < 2:
            faults.append(run.conversation)
            run.end(reason, code)
            return
        passer(game, side, run)

    _model, game, clock, agents, spec = setup({"A": passer, "B": flaky})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None
    assert (out.end["winner"], out.end["reason"], out.end.get("forfeit")) == (None, "client_fault", None)
    [end] = [e for e in arena_events(tmp_path) if e["ev"] == "arena_end"]
    assert (end["failed"], end["cause"]) == ("B", cause)
    assert (out.end["failed"], out.end["cause"]) == ("B", cause)  # arena.json's `end` (live.play writes out.end)
    assert out.invalid is not None and out.invalid.startswith("client_fault: side B's agent")
    assert agents.started[2:] == [("B", "The fight is on.", "conv-B", "-r1")]  # one resume, same conversation
    assert [e.reason for e in out.exits["B"]] == [reason, reason]
    g = grade(tmp_path)
    assert (g["valid"], g["invalid_reason"], g["result"], g["fault"]) == (
        False,
        "client_fault",
        None,
        {"side": "B", "cause": cause},
    )


def test_an_agent_that_exits_mid_fight_is_resumed_up_to_three_times(tmp_path):
    def quitter(game: Game, side: str, run: Run) -> None:
        run.end()  # exit 0 on its first tick, every time

    _model, game, clock, agents, spec = setup({"A": quitter, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert [t for (s, _p, _c, t) in agents.started if s == "A"] == ["", "-r1", "-r2", "-r3"]
    assert out.end is not None and (out.end["forfeit"], out.end["cause"]) == ("A", "client_exit")
    assert len(out.exits["A"]) == 4
    g = grade(tmp_path)
    assert (g["valid"], g["result"], g["forfeit"], g["fault"]) == (
        True,
        "B",
        {"side": "A", "cause": "client_exit"},
        None,
    )


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


def test_a_team_member_whose_uuid_disagrees_with_the_arena_file_starts_no_match(tmp_path):
    """The arena file gives Karlach's uuid; arena.status reports another character there: the setup fails closed."""
    karlach = "000001f4-0000-4000-8000-0000000001f4"  # the fake game's first character
    teams = {"A": [{"name": "Karlach", "uuid": karlach}, {"name": "Gale"}], "B": ARENA["teams"]["B"]}
    model, game, clock, agents, spec = setup({"A": passer, "B": passer}, dict(ARENA, teams=teams))
    assert model.arena is None
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.invalid is None and agents.started  # the right uuid plays

    wrong = {"A": [{"name": "Karlach", "uuid": "0" * 8 + "-0000-4000-8000-" + "0" * 12}, {"name": "Gale"}]}
    model, game, clock, agents, spec = setup({"A": passer, "B": passer}, dict(ARENA, teams={**teams, **wrong}))
    out = am.run_arena(spec, SIDES, game, agents, tmp_path / "wrong", clock)
    assert out.end is None and out.invalid == (
        f"setup: ArenaSetupError: arena.status team A has {karlach} for Karlach, the arena "
        "00000000-0000-4000-8000-000000000000"
    )
    assert agents.started == []


def test_the_wall_cap_is_a_draw(tmp_path):
    arena = dict(ARENA, wall_cap_min=1, round_cap=100)
    _model, game, clock, agents, spec = setup({"A": passer, "B": passer}, arena)
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock)
    assert out.end is not None and (out.end["reason"], out.end["winner"]) == ("wall_cap", None)
    assert grade(tmp_path, arena)["result"] == "draw"


BENCH_FACTS_SCHEMA = json.loads((REPO / "packages" / "bench" / "schema" / "arena-facts.v1.schema.json").read_text())


@pytest.mark.parametrize("scripted", ["A", "B"])
def test_a_match_the_scripted_client_plays_is_invalid_whatever_its_end(tmp_path, scripted):
    """A side the harness's scripted client plays (player "scripted/none"): the match is a live check, invalid in the
    harness's own outcome, its arena_end marked, both for the mod's end (a wipe) and the runner's (the wall cap)."""
    sides = {**SIDES, scripted: Side(scripted, "scripted/none", "scripted", "scripted", "none")}
    _model, game, clock, agents, spec = setup({"A": attacker, "B": passer})
    out = am.run_arena(spec, sides, game, agents, tmp_path / "wipe", clock)
    assert out.end is not None and out.end["reason"] == "wipe"
    assert out.invalid == f"scripted: side {scripted} played the scripted client (a live check, never graded)"
    arena = dict(ARENA, wall_cap_min=1, round_cap=100)
    _model, game, clock, agents, spec = setup({"A": passer, "B": passer}, arena)
    capped = am.run_arena(spec, sides, game, agents, tmp_path / "cap", clock)
    assert capped.end is not None and capped.end["reason"] == "wall_cap"
    assert capped.invalid is not None and capped.invalid.startswith("scripted: ")
    for run, a in (("wipe", ARENA), ("cap", arena)):
        end = [e for e in arena_events(tmp_path / run) if e["ev"] == "arena_end"]
        assert len(end) == 1 and end[0]["invalid"] == "scripted"
        jsonschema.validate(end[0], BENCH_FACTS_SCHEMA)
        assert grade(tmp_path / run, a)["invalid_reason"] == "scripted"


def test_an_operator_abort_is_invalid(tmp_path):
    model, game, clock, agents, spec = setup({"A": passer, "B": passer})
    out = am.run_arena(spec, SIDES, game, agents, tmp_path, clock, aborted=lambda: clock.t > 1_800_000_003)
    assert out.end is not None and out.end["reason"] == "abort" and model.ends == [{"reason": "abort"}]
    assert grade(tmp_path)["invalid_reason"] == "abort"


def test_ring_records_of_the_mod_are_translated_or_left_out(tmp_path):
    """The mod's own arena_start and a runner end's arena_end never reach facts; a phase names its characters; a
    phase's members are named (a summon by the record's names) and a phase naming the other team's character or
    without members and names is not written; the mod's own end of any reason is the match's, and nothing of the arena's
    after an end is written."""
    _model, _game, clock, _agents, spec = setup({"A": passer, "B": passer})
    from bg3harness import facts as hfacts

    ref = am.Referee(spec, SIDES, hfacts.FactsWriter(tmp_path / "f.jsonl", clock=clock.now))
    ref.start({"A": ["UA1", "UA2"], "B": ["UB1", "UB2"]})
    assert ref.translate({"ev": "arena_start", "teams": {}}) is None
    phase = {"ev": "arena_turn", "side": "A", "round": 1, "phase": 2, "group": "g", "shared": True}
    two = {**phase, "character": "ua2", "members": ["ua2", "UA1"], "names": ["Gale", "Karlach"]}
    assert ref.translate(two) == {**two, "character": "Gale", "members": ["Gale", "Karlach"]}
    assert ref.turn is not None and ref.turn.members == ["ua2", "UA1"]
    # a team member's name is the arena file's (the game may name it otherwise); a summon's is the record's
    with_summon = {**phase, "character": "summon-1", "members": ["summon-1", "ua1"], "names": ["Wolf", "Karla"]}
    assert ref.translate(with_summon) == {**with_summon, "character": "Wolf", "members": ["Wolf", "Karlach"]}
    assert ref.turn.members == ["summon-1", "ua1"] and (ref.turn.uuid, ref.turn.name) == ("summon-1", "Wolf")
    alone = {**phase, "character": "summon-1", "members": ["summon-1"], "names": ["Wolf"]}
    assert ref.translate(alone) == {**alone, "members": ["Wolf"], "character": "Wolf"}
    assert ref.translate({**phase, "character": "ua1", "members": ["ua1", "ub1"], "names": ["Karlach", "Wyll"]}) is None
    assert ref.translate({**phase, "character": "ua1", "members": ["ua1"]}) is None  # no names
    assert ref.translate({"ev": "arena_turn", "side": "A", "character": "ua1", "round": 1}) is None
    assert len(ref.turns) == 3
    assert ref.translate({"ev": "arena_combat_ended", "rounds": 1}) is None and ref.combat_ended == {
        "ev": "arena_combat_ended",
        "rounds": 1,
    }
    assert ref.translate({"ev": "kill", "name": "x"}) == {"ev": "kill", "name": "x"}
    passed = {"ev": "arena_turn_passed", "side": "A", "character": "ua2", "name": "Gale (downed)", "why": "downed"}
    assert ref.translate({**passed, "round": 1}) == {**passed, "round": 1, "character": "Gale"}  # the arena's name
    summon = {**passed, "character": "summon-1", "name": "Wolf", "why": "dead", "round": 1}
    assert ref.translate(summon) == {**summon, "character": "Wolf"}  # not a team member: the record's name
    cap = ref.translate({"ev": "arena_end", "reason": "round_cap", "rounds": 4, "hp_left_A": 1, "hp_left_B": 1})
    assert cap is not None and cap["winner"] is None and ref.mod_end == cap
    assert ref.translate({**phase, "character": "ua1", "members": ["ua1"], "names": ["Karlach"], "round": 4}) is None
    assert ref.translate({"ev": "arena_end", "reason": "wipe", "rounds": 4}) is None
    assert ref.translate({**passed, "round": 4}) is None  # after the end

    ref2 = am.Referee(spec, SIDES, hfacts.FactsWriter(tmp_path / "f2.jsonl", clock=clock.now))
    ref2.start({"A": ["UA1", "UA2"], "B": ["UB1", "UB2"]})
    ref2.end(winner="A", reason="forfeit", rounds=1, hp_left_A=1, hp_left_B=1, forfeit="B", cause="forced_turns")
    assert ref2.translate({"ev": "arena_end", "reason": "forfeit", "rounds": 1}) is None  # the runner's own end
    assert ref2.mod_end is None


def test_the_spec_refuses_a_draft_or_incomplete_arena():
    with pytest.raises(ArenaSpecError, match="draft"):
        ArenaSpec.from_dict(dict(ARENA, draft="placeholder"))
    with pytest.raises(ArenaSpecError, match="turn_cap_s"):
        ArenaSpec.from_dict({k: v for k, v in ARENA.items() if k != "turn_cap_s"})
    with pytest.raises(ArenaSpecError, match="karmic"):
        ArenaSpec.from_dict(dict(ARENA, karmic=True))
    spec = ArenaSpec.from_dict(ARENA)
    assert spec.teams == {"A": ("Karlach", "Gale"), "B": ("Wyll", "Halsin")} and spec.arena == ARENA


def test_both_schemas_take_a_passed_turn_and_a_stall():
    base = {"v": 1, "kind": "event", "t": 1.0}
    passed = {**base, "ev": "arena_turn_passed", "side": "A", "character": "Gale", "name": "Gale", "why": "downed"}
    stall = {**base, "ev": "arena_stall", "stall": "held", "side": "A", "character": "Gale", "members": ["Gale"]}
    stall.update(round=2, idle_s=300.0, status={"turn": None, "round": 2, "hp": {}}, last={"A": [], "B": []})
    zero_hp = {**passed, "round": 2, "why": "zero_hp"}  # Arena.lua cantAct: 0 HP without DOWNED
    for good in ({**passed, "round": 2}, {**passed, "round": 2, "error": "x"}, zero_hp, stall):
        jsonschema.validate(good, FACTS_SCHEMA)
        jsonschema.validate(good, BENCH_FACTS_SCHEMA)
    no_phase = {k: v for k, v in stall.items() if k not in ("side", "character", "members")}
    jsonschema.validate({**no_phase, "stall": "game", "round": 0}, FACTS_SCHEMA)  # before the first phase
    jsonschema.validate({**no_phase, "stall": "game", "round": 0}, BENCH_FACTS_SCHEMA)
    bad_stalls = ({**stall, "stall": "slow"}, {**stall, "members": []}, no_phase)  # a held stall names its phase
    for bad in (passed, {**passed, "round": 2, "why": "stunned"}, *bad_stalls):
        for schema in (FACTS_SCHEMA, BENCH_FACTS_SCHEMA):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(bad, schema)


def test_the_facts_schema_takes_a_phase_and_refuses_a_turn_without_its_fields():
    rec = {"v": 1, "kind": "event", "t": 1.0, "ev": "arena_turn", "side": "A", "character": "Karlach", "round": 1}
    phase = {"members": ["Karlach", "Gale"], "names": ["Karlach", "Gale"], "phase": 2, "group": "g", "shared": True}
    jsonschema.validate({**rec, **phase}, FACTS_SCHEMA)
    jsonschema.validate({**rec, **{k: v for k, v in phase.items() if k != "group"}}, FACTS_SCHEMA)  # no turn order
    no_names = {k: v for k, v in phase.items() if k != "names"}
    for bad in (rec, {**rec, **phase, "members": []}, {**rec, **phase, "phase": 0}, {**rec, **no_names}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, FACTS_SCHEMA)
