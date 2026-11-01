# pyright: basic
"""run_arena: one match, from the arena's setup to its end, with both sides' agents playing at once.

The game, the agents and the clock come in through ArenaGame, Agents and Clock, so a fake game (the scripted
two-team GameModel) and scripted clients drive it in tests; bg3harness.arena.live has the real ones.

1. `arena.setup {arena, round_cap}` (a job, polled), then `arena.status` until the match is active.
2. The referee writes `arena_start {arena, seed, teams (names), sides (players), uuids (arena.status teams)}` into the
   match's one facts.jsonl, and both agents start with the kickoff. The referee ring (RingFeed, no side: the
   harness's reads see both teams) carries the mod's arena records: its `arena_turn` (one per phase: a same-side
   run of the rolled order inside one turn group, {side, character (the phase's first open member), members (its
   open members, in turn order), round, phase, group (absent when the mod can't read the turn order), shared}) is
   written with the team members' names (a summon has none: it is left out of the names, and the character is then
   the phase's first team member); its `arena_end` (the mod's own end: a wipe at a turn boundary, the round cap at
   the start of round round_cap + 1) as it is; its `arena_start`, an end the runner asked for and any arena record
   after the match's end are left out, as the referee writes those itself. A phase with no member of its side's team
   (only summons) is not a team turn, and one naming the other team's characters is not the side's: not written,
   never forced. A turn below is a phase.
3. Every POLL_S: the ring, then `arena.status`. The match ends at the first of
   - the mod's end (`arena.status.ended` or the ring's `arena_end`, any reason: a wipe, or the round cap as a draw
     with `rounds` = round_cap);
   - the mod's `arena_combat_ended` (the arena's combat ended without a wipe): `fault` (invalid, rerun);
   - a turn of round round_cap + 1 (a mod that doesn't cap: a draw, `rounds` = round_cap) or the wall cap (a draw);
   - a forfeit: a side whose own turns were forced to an end FORFEIT_TURNS times in a row (`forced_turns`), or
     whose agent failed again after FAULT_RESUMES resume or exited mid-fight more than EXIT_RESUMES times
     (`client_fault`);
   - the game gone or not answering for HANG_S: `fault` (invalid, rerun); an operator's abort: `abort` (invalid).
   The runner's own ends go to the mod as `arena.end {reason, winner}` (no answer from a dead game is needed) and
   into facts as `arena_end {winner, reason, rounds, hp_left_A, hp_left_B, forfeit?, cause?}`, with the HP fractions
   from the last `arena.status.hp`. arena.end answers with the match's first end: when that is the mod's own (it
   ended the match after this tick's read), the mod's end is the one written, its `detail` naming the runner's.
4. Per-turn cap: a side's model time (Agents: from its client's stream, tool time excluded) since its team's turn
   began reaching the arena's turn_cap_s times the turn's member count (summons included) ends that turn:
   `act.end_turn` for each of its members in turn order, sent as that side, one at a time (each job polled until it
   settles, at most END_SETTLE_S; a member that already ended is refused), then `arena_turn_forced {side,
   character}` once any end went through. The cap of a side's FORFEIT_TURNS-th turn in a row is its forfeit,
   decided there: `arena_turn_forced`, then the end goes to the mod, and no `act.end_turn` is sent (the mod keeps a
   match's first end, so no wipe at the next turn boundary can come before the forfeit).
5. A reaction prompt no team owns (a summon's name on both teams, no rows read) waits for an answer neither side may
   give: the runner declines it (`ui.react {use: false}`, no side) and writes `arena_reaction_unowned {round,
   detail}`. It reads `ui.state` for one only once a turn (or the wait for the first) is UNOWNED_CHECK_S old, then
   at most every UNOWNED_CHECK_S: a turn that moves on costs no read.
6. An agent that exits while the fight is on is resumed in the same conversation with RESUME; exit 0 counts toward
   EXIT_RESUMES, anything else (a crash, an API or MCP failure, a hang) toward FAULT_RESUMES.

Both agents are stopped at the end; their exits and costs are in the outcome.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from bg3harness import facts
from bg3harness.arena.spec import SIDES, ArenaSpec, Side, other

POLL_S = 1.0
SETUP_TIMEOUT_S = 180.0  # arena.setup's job and the match becoming active
HANG_S = 60.0  # arena.status unanswered this long: the game hung
FORFEIT_TURNS = 3  # a side's own turns forced to an end in a row
EXIT_RESUMES = 3  # a side's agent exiting (exit 0) mid-fight: resumed this many times, then it forfeits
FAULT_RESUMES = 1  # a side's client failing (crash, API/MCP failure, hang): resumed once, then it forfeits
END_SETTLE_S = 10.0  # a cap's act.end_turn job: polled this long for its end before the next member's
END_POLL_S = 0.25
UNOWNED_CHECK_S = 15.0  # a turn this old: ui.state is read for a prompt no team owns, then at most this often
END_FIELDS = ("winner", "reason", "rounds", "hp_left_A", "hp_left_B")
KICKOFF = "The fight is on. You play side {side}: {names}."
RESUME = "The fight is on."


class Rpc(Protocol):
    def __call__(self, op: str, args: dict[str, Any], side: str | None = None) -> Any: ...


class ArenaGame(Protocol):
    rpc: Rpc  # one mod op; raises when the game refuses or doesn't answer

    def alive(self) -> bool: ...  # the game process is up


@dataclass(frozen=True)
class AgentExit:
    reason: str  # "exited" or the runner's fault reasons (crash, agent_hang, mcp_failed, api_auth, ...)
    exit_code: int | None
    conversation: str | None = None  # the client's conversation id, to resume
    cost_usd: float | None = None

    @property
    def fault(self) -> bool:
        return self.reason != "exited" or self.exit_code != 0


class AgentRun(Protocol):
    def done(self) -> AgentExit | None: ...  # None while the process runs
    def model_s(self) -> float: ...  # the process's model time so far
    def stop(self) -> AgentExit: ...  # end the process (if it runs) and return its exit


class Agents(Protocol):
    def start(self, side: str, prompt: str, conversation: str | None, tag: str) -> AgentRun: ...


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, s: float) -> None: ...


@dataclass
class ArenaOutcome:
    end: dict[str, Any] | None  # the arena_end fields written; None when the match never started
    invalid: str | None = None  # why the match can't be graded (a setup failure, fault, abort), else None
    exits: dict[str, list[AgentExit]] = field(default_factory=lambda: {s: [] for s in SIDES})

    def cost(self, side: str) -> float | None:
        """The side's cost over its processes (None when one didn't report it)."""
        costs = [e.cost_usd for e in self.exits[side]]
        return None if any(c is None for c in costs) else round(sum(c for c in costs if c is not None), 6)


@dataclass
class Turn:
    side: str
    uuid: str
    name: str
    round: int
    members: list[str]  # uuids, in turn order, summons included
    forced: bool = False


class Referee:
    """The match's facts: arena_start, the ring's arena records translated (RingFeed's translate), the forced turns
    and the end."""

    def __init__(self, spec: ArenaSpec, sides: Mapping[str, Side], writer: facts.FactsWriter):
        self.spec, self.sides, self.writer = spec, sides, writer
        self.names: dict[str, tuple[str, str]] = {}  # uuid -> (side, name)
        self.turns: list[Turn] = []
        self.mod_end: dict[str, Any] | None = None  # the mod's own arena_end from the ring
        self.combat_ended: dict[str, Any] | None = None  # the mod's arena_combat_ended
        self.done = False  # arena_end written: later arena records are not the match's

    @property
    def turn(self) -> Turn | None:
        return self.turns[-1] if self.turns else None

    @property
    def round(self) -> int:
        return self.turn.round if self.turn else 0

    def start(self, teams: Mapping[str, list[str]]) -> None:
        """arena_start; `teams`: arena.status's uuids, in the arena file's team order."""
        for s in SIDES:
            if len(teams.get(s) or []) != len(self.spec.teams[s]):
                raise ArenaSetupError(
                    f"arena.status team {s} has {len(teams.get(s) or [])} characters, the arena "
                    f"{len(self.spec.teams[s])}"
                )
            for uuid, name in zip(teams[s], self.spec.teams[s], strict=True):
                self.names[uuid.lower()] = (s, name)
        self.event(
            "arena_start",
            arena=self.spec.name,
            seed=self.spec.seed,
            teams=self.spec.names(),
            sides={s: self.sides[s].player for s in SIDES},
            uuids={s: list(teams[s]) for s in SIDES},
        )

    def translate(self, fact: dict) -> dict | None:
        ev = str(fact.get("ev") or "")
        if not ev.startswith("arena_"):
            return fact
        if self.done:
            return None
        if ev == "arena_turn":
            members = [str(u) for u in fact.get("members") or []]
            known = [self.names[u.lower()] for u in members if u.lower() in self.names]
            names = [name for side, name in known if side == fact.get("side")]
            if not names or len(names) != len(known):  # only summons, or the other team's characters
                return None
            first = self.names.get(str(fact.get("character") or "").lower())
            name = first[1] if first and first[1] in names else names[0]
            self.turns.append(Turn(str(fact["side"]), str(fact["character"]), name, int(fact["round"]), members))
            return {**fact, "character": name, "members": names}
        if ev == "arena_end":
            self.mod_end = {"winner": None, **fact}  # the ring leaves a nil winner (a draw) out
            self.done = True
            return self.mod_end
        if ev == "arena_combat_ended" and self.combat_ended is None:
            self.combat_ended = fact
        return None

    def forced(self) -> None:
        t = self.turn
        assert t is not None and not t.forced
        t.forced = True
        self.event("arena_turn_forced", side=t.side, character=t.name)

    def forfeits(self) -> bool:
        """Forcing the turn in progress makes FORFEIT_TURNS forced turns in a row of its side."""
        t = self.turn
        assert t is not None
        own = [x for x in self.turns if x.side == t.side][-FORFEIT_TURNS:]
        return len(own) == FORFEIT_TURNS and all(x.forced for x in own[:-1])

    def end(self, **fields: Any) -> dict[str, Any]:
        self.event("arena_end", **fields)
        self.done = True
        return fields

    def event(self, ev: str, **fields: Any) -> None:
        self.writer.timed_events([{"ev": ev, **fields}])


class ArenaSetupError(RuntimeError):
    pass


class _Ended(Exception):  # noqa: N818 - control flow inside run_arena
    def __init__(
        self,
        reason: str,
        winner: str | None = None,
        forfeit: str | None = None,
        cause: str | None = None,
        detail: str = "",
        rounds: int | None = None,
        game_up: bool = True,
    ):
        super().__init__(reason)
        self.reason, self.winner, self.forfeit, self.cause, self.detail = reason, winner, forfeit, cause, detail
        self.rounds, self.game_up = rounds, game_up  # game_up: the game answers, the mod hears the end


def _setup(spec: ArenaSpec, game: ArenaGame, clock: Clock) -> dict[str, Any]:
    """arena.setup's job to its end, then arena.status until active; the active status."""
    deadline = clock.now() + SETUP_TIMEOUT_S
    job = game.rpc("arena.setup", {"arena": spec.arena, "round_cap": spec.round_cap}) or {}
    while job.get("status") not in ("done", "failed"):
        if clock.now() > deadline:
            raise ArenaSetupError(f"arena.setup still {job.get('status')} after {SETUP_TIMEOUT_S:.0f} s")
        clock.sleep(POLL_S)
        job = game.rpc("job.poll", {"id": job.get("job", job.get("id"))}) or {}
    if job["status"] == "failed":
        raise ArenaSetupError(f"arena.setup failed: {job.get('error')}")
    while True:
        st = game.rpc("arena.status", {}) or {}
        if st.get("active"):
            return st
        if clock.now() > deadline:
            raise ArenaSetupError(f"the match was not active {SETUP_TIMEOUT_S:.0f} s after arena.setup")
        clock.sleep(POLL_S)


def _decline_unowned(spec: ArenaSpec, game: ArenaGame, ref: Referee, log: Callable[[str], None]) -> None:
    """A reaction prompt no team owns on the game client's screen (ui.state with no side: reaction.unowned): declined,
    and arena_reaction_unowned written."""
    try:
        ui = game.rpc("ui.state", {}) or {}
        rx = ui.get("reaction") if ui.get("screen") == "reaction" else None
        if not (isinstance(rx, dict) and rx.get("unowned")):
            return
        job = game.rpc("ui.react", {"use": False}) or {}
    except Exception as e:  # read again at the next check
        log(f"arena {spec.name}: the unowned reaction check failed: {e}")
        return
    if job.get("status") == "failed":
        log(f"arena {spec.name}: declining an unowned reaction prompt failed: {job.get('error')}")
        return
    detail = str(rx.get("prompt") or "")
    ref.event("arena_reaction_unowned", round=ref.round, detail=detail)
    log(f"arena {spec.name}: declined a reaction prompt no team owns ({detail})")


def _end_turn(game: ArenaGame, clock: Clock, uuid: str, side: str) -> str | None:
    """act.end_turn for `uuid` as `side`, its job polled until it settles (at most END_SETTLE_S); the refusal or
    failure, else None."""
    job = game.rpc("act.end_turn", {"char": uuid}, side) or {}
    deadline = clock.now() + END_SETTLE_S
    while job.get("status") not in ("done", "failed") and clock.now() < deadline:
        clock.sleep(END_POLL_S)
        job = game.rpc("job.poll", {"id": job.get("job", job.get("id"))}, side) or {}
    return str(job.get("error")) if job.get("status") == "failed" else None


def run_arena(
    spec: ArenaSpec,
    sides: Mapping[str, Side],
    game: ArenaGame,
    agents: Agents,
    out: Path,
    clock: Clock,
    log: Callable[[str], None] = lambda _m: None,
    aborted: Callable[[], bool] = lambda: False,
    writer: facts.FactsWriter | None = None,
) -> ArenaOutcome:
    """One match into `out` (facts.jsonl, rolls.jsonl; the agents write their own files); see the module doc.
    writer: the run's facts writer when the caller has one (its run_start stays time zero)."""
    out.mkdir(parents=True, exist_ok=True)
    writer = writer or facts.FactsWriter(out / "facts.jsonl", clock=clock.now)
    ref = Referee(spec, sides, writer)
    feed = facts.RingFeed(
        lambda op, args: game.rpc(op, args), writer, out / "rolls.jsonl", wall=clock.now, translate=ref.translate
    )
    if writer.t0 is None:
        writer.mark("run_start", {"arena": spec.name})
    outcome = ArenaOutcome(None)
    try:
        feed.poll()  # the rings' readers start here: the setup's records are the match's
        status = _setup(spec, game, clock)
        ref.start(status["teams"])
    except Exception as e:  # no match: nothing to grade, the rerun sets up again
        outcome.invalid = f"setup: {type(e).__name__}: {e}"
        log(f"arena {spec.name}: {outcome.invalid}")
        writer.mark("run_end", {"invalid": outcome.invalid})
        return outcome
    started = clock.now()
    log(f"arena {spec.name}: the match is on ({sides['A'].player} v {sides['B'].player})")
    runs: dict[str, AgentRun] = {}
    for s in SIDES:
        runs[s] = agents.start(s, KICKOFF.format(side=s, names=", ".join(spec.teams[s])), None, "")
    closed_model = dict.fromkeys(SIDES, 0.0)  # model time of a side's ended processes
    exits = dict.fromkeys(SIDES, 0)
    faults = dict.fromkeys(SIDES, 0)
    turn_mark: tuple[int, float] | None = None  # (index of the turn, the side's model time at its start)
    hp = dict.fromkeys(SIDES, 1.0)
    answered = clock.now()
    seen_turns, turn_at, ui_read = 0, started, started  # the turns so far, when the last began, the last ui.state

    def model(side: str) -> float:
        return closed_model[side] + runs[side].model_s()

    try:
        while True:
            clock.sleep(POLL_S)
            now = clock.now()
            if aborted():
                raise _Ended("abort", detail="operator stop")
            if not game.alive():
                raise _Ended("fault", detail="the game process is gone", game_up=False)
            try:
                feed.poll()
                status = game.rpc("arena.status", {}) or {}
                answered = now
            except Exception as e:  # a slow frame; a hang only after HANG_S
                if now - answered >= HANG_S:
                    raise _Ended(
                        "fault", detail=f"arena.status unanswered for {now - answered:.0f} s: {e}", game_up=False
                    ) from e
                continue
            hp = {s: float((status.get("hp") or {}).get(s, hp[s])) for s in SIDES}
            if status.get("ended") and ref.mod_end is None:
                feed.poll()  # the ring's arena_end may have come after this tick's read
            if ref.mod_end is not None or status.get("ended"):
                break
            if ref.combat_ended is not None:
                n = int(ref.combat_ended.get("rounds") or ref.round)
                raise _Ended("fault", detail=f"the arena's combat ended without a wipe (rounds {n})", rounds=n)
            if ref.round > spec.round_cap:  # a mod that doesn't cap
                raise _Ended("round_cap")
            if now - started >= spec.wall_cap_min * 60:
                raise _Ended("wall_cap")
            if len(ref.turns) != seen_turns:
                seen_turns, turn_at = len(ref.turns), now
            if now - max(turn_at, ui_read) >= UNOWNED_CHECK_S:
                ui_read = now
                _decline_unowned(spec, game, ref, log)
            for s in SIDES:
                ex = runs[s].done()
                if ex is None:
                    continue
                outcome.exits[s].append(ex)
                closed_model[s] += runs.pop(s).model_s()
                if ex.fault:
                    faults[s] += 1
                else:
                    exits[s] += 1
                log(f"arena {spec.name}: side {s}'s agent ended mid-fight ({ex.reason}, exit {ex.exit_code})")
                if faults[s] > FAULT_RESUMES or exits[s] > EXIT_RESUMES:
                    raise _Ended(
                        "forfeit",
                        winner=other(s),
                        forfeit=s,
                        cause="client_fault",
                        detail=f"side {s}'s agent: {ex.reason}",
                    )
                n = faults[s] + exits[s]
                runs[s] = agents.start(s, RESUME, ex.conversation, f"-r{n}")
            turn = ref.turn
            if turn is None or turn.forced:
                continue
            idx = len(ref.turns) - 1
            if turn_mark is None or turn_mark[0] != idx:
                turn_mark = (idx, model(turn.side))
            cap = spec.turn_cap_s * len(turn.members)
            if model(turn.side) - turn_mark[1] < cap:
                continue
            if ref.forfeits():
                ref.forced()
                log(f"arena {spec.name}: {turn.name}'s turn (side {turn.side}) hit the {cap:g} s cap")
                raise _Ended("forfeit", winner=other(turn.side), forfeit=turn.side, cause="forced_turns")
            errors = [_end_turn(game, clock, u, turn.side) for u in turn.members]
            if all(errors):
                log(f"arena {spec.name}: ending {turn.name}'s turn at the cap failed: {'; '.join(map(str, errors))}")
                continue
            ref.forced()
            log(f"arena {spec.name}: {turn.name}'s turn (side {turn.side}) ended at the {cap:g} s cap")
        if ref.mod_end is None:  # the mod ended it but its record didn't reach the ring: the status's end
            e = status["ended"]
            outcome.end = ref.end(**{k: e.get(k) for k in END_FIELDS})
        else:
            outcome.end = {k: ref.mod_end.get(k) for k in END_FIELDS}
        log(f"arena {spec.name}: the mod's end: {outcome.end.get('reason')}, winner {outcome.end.get('winner')}")
    except _Ended as e:
        first: dict[str, Any] = {}  # the mod's own end, when it came before the runner's
        if e.game_up:
            try:
                ans = game.rpc("arena.end", {"reason": e.reason, **({"winner": e.winner} if e.winner else {})}) or {}
                if (ans.get("ended") or {}).get("reason") not in (None, e.reason):
                    first = ans["ended"]
            except Exception as err:  # the match ends here either way
                log(f"arena {spec.name}: arena.end failed: {err}")
        runners = e.reason + (f", winner {e.winner}" if e.winner else "") + (f" ({e.detail})" if e.detail else "")
        if first:
            outcome.end = ref.end(
                **{k: first.get(k) for k in END_FIELDS}, detail=f"the runner's end came after it: {runners}"
            )
            log(f"arena {spec.name}: the mod's end came first: {first.get('reason')}, winner {first.get('winner')}")
        else:
            rounds = e.rounds if e.rounds is not None else spec.round_cap if e.reason == "round_cap" else ref.round
            fields: dict[str, Any] = {
                "winner": e.winner,
                "reason": e.reason,
                "rounds": rounds,
                "hp_left_A": hp["A"],
                "hp_left_B": hp["B"],
            }
            if e.forfeit:
                fields.update(forfeit=e.forfeit, cause=e.cause)
            outcome.end = ref.end(**fields)
            if e.reason in ("fault", "abort"):
                outcome.invalid = f"{e.reason}: {e.detail}"
            log(f"arena {spec.name}: {runners}")
    finally:
        for s, run in runs.items():
            outcome.exits[s].append(run.stop())
    with contextlib.suppress(Exception):  # a dead game: what was read is what there is
        feed.poll()  # the last rolls
    writer.mark("run_end", {"arena_end": outcome.end.get("reason") if outcome.end else None})
    return outcome
