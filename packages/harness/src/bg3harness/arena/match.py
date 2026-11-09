# pyright: basic
"""run_arena: one match, from the arena's setup to its end, with both sides' agents playing at once.

The game, the agents and the clock come in through ArenaGame, Agents and Clock, so a fake game (the scripted
two-team GameModel) and scripted clients drive it in tests; bg3harness.arena.live has the real ones.

1. `arena.setup {arena, round_cap}` (a job, polled), then `arena.status` until the match is active. The teams'
   uuids are arena.status's `teams`, in the arena file's order; a mirror arena's (its twins don't exist before the
   setup makes them: the file names them only) are the setup job result's `teams` (pair i at index i), and
   arena.status must report the same. A result without them, short of a member, or disagreeing with arena.status
   starts no match (a setup error).
2. The referee writes `arena_start {arena, seed, turn_order, teams (names), sides (players), uuids (those teams)}`
   into the match's one facts.jsonl, and both agents start with the kickoff. The referee ring (RingFeed, no side:
   the harness's reads see both teams) carries the mod's arena records: its `arena_turn` (one per phase: one
   character of a turn group, the sides alternating, summons included, {side, character, members (that character;
   an older mod's: the side's run of open members), names (their display names), round, phase, group (absent when
   the mod can't read the turn order), shared, order, opening, rule}) is written with names: `members` = a team
   member's name from the arena file, a summon's from the record; `character` = the first; `names` as the mod gave
   them. Its `arena_end` (the mod's own end: a wipe at a turn boundary, the round cap at the start of round
   round_cap + 1) is written as it is; its `arena_start`, an
   end the runner asked for and any arena record after the match's end are left out, as the referee writes those
   itself. Its `arena_turn_passed {side, character, name, why (dead, downed or zero_hp), round, error?, order,
   opening, rule}` (the mod ended the turn of a member that can't act, its slot in the turn order taken;
   `death_save` for a downed one) is written with `character` named as in
   arena_turn, and its `arena_turn_readmitted {side, character, name, round, why}` (a passed member stood up with its
   turn back) the same way, the member no longer passed, and its `arena_stood_up {side, character, name, round,
   status, movement_before, movement_after, movement_max, deducted, game_took}` (a prone member stood up at its phase
   start by the mod) the same way. Its `arena_round {round, first, how}` (a round's first
   phase: the round's opening side, and whether it acted or was passed whole; once per round) is written as it is.
   A mirror arena's `arena_surfaces {when, round, tiles_total, tiles_read, radius, off, tiles, appeared,
   appeared_tiles}` (the mod's report-only re-read of the surfaces at round 2's start and at the end, a runner's end
   included: arena.end, then the last poll) is written as it is, after the end too; `arena_start` carries the setup's
   surface check (`surfaces`) and field clean (`field`). Its
   `arena_group_end_written {round, group, character, members, names, errors?, how}` (a shared group the game kept open
   after every member's end, in any round: the mod wrote the members' RequestedEndTurn; a turn group's end, not the
   match's) is written with `character` and `members`
   named as in arena_turn, `names` and `errors` (by uuid) as the mod gave them. A phase naming
   the other team's character is not the side's: not written, never forced. A turn below is a phase; a summon's
   phase is a turn of its side like any other.
3. Every POLL_S: the ring, then `arena.status`. The match ends at the first of
   - the mod's end (`arena.status.ended` or the ring's `arena_end`, any reason: a wipe, or the round cap as a draw
     with `rounds` = round_cap);
   - the mod's `arena_combat_ended` (the arena's combat ended without a wipe): `fault` (invalid, rerun);
   - a turn of round round_cap + 1 (a mod that doesn't cap: a draw, `rounds` = round_cap) or the wall cap (a draw);
   - a forfeit: a side whose own turns were forced to an end FORFEIT_TURNS times in a row (`forced_turns`), whose
     model ended its session (exit 0) mid-fight more than EXIT_RESUMES times (`client_exit`), or whose conversation
     outgrew its model's context after its first tool call (`context_overflow`, at the first one: a resume sends the
     same full conversation; owner A6, 2026-11-05);
   - a side's client failing again after FAULT_RESUMES resume (an API or provider error, a 400 that is no overflow
     included, an overflow before the side's first tool call (the harness's prompt filled the context), the bg3 MCP
     server failing, a hang, a crash or kill: the model's infrastructure, not its play; owner D459): `client_fault`
     (invalid, rerun, no winner), with `failed` = the side and `cause` = the exit's reason (api_error,
     kickoff_overflow, mcp_failed, agent_hang, killed, crash);
   - the game gone or not answering for HANG_S: `fault` (invalid, rerun); an operator's abort: `abort` (invalid);
   - max_cost (USD, when given): both sides' cost so far (each process's running cost, Agents' cost_usd: pi's
     comes after each model response) reaching it, or one side's reaching its own cap (side_caps, a matrix's per-side
     reserve): `over_budget` (invalid, no winner), both agents stopped, `capped` = the sides that reached their own
     cap, both when the match's max_cost was reached (the results' per-model over-budget count, owner R4).
   The runner's own ends go to the mod as `arena.end {reason, winner}` (no answer from a dead game is needed) and
   into facts as `arena_end {winner, reason, rounds, hp_left_A, hp_left_B, forfeit?, failed?, cause?, capped?}`, with
   the HP fractions from the last `arena.status.hp`. Every arena_end, the mod's too, carries `cost {A, B}` (each
   side's cost, USD, read once both agents are stopped: it is written then, so a response charged after the end
   counts) and `max_cost` (None without one). arena.end answers with the match's first end: when that is the
   mod's own (it ended the match after this tick's read), the mod's end is the one written, its `detail` naming the
   runner's.
4. Per-turn cap: a side's model time (Agents: from its client's stream, tool time excluded) since its team's turn
   began reaching the arena's turn_cap_s ends that turn (a phase is one character: the mod's turn order, owner D643;
   a record naming more members, from an older mod, gets the same turn_cap_s):
   `act.end_turn {char, forced}` for each of its members in turn order, sent as that side (forced: the cap as the
   mod tells the side, "the per-turn time cap (N s of model time)" or "the stall cap (...)"), one at a time (each
   job polled until it
   settles, at most END_SETTLE_S, which outlasts the mod's own wait for a group's last end; a member whose turn is
   no longer open is refused), then `arena_turn_forced {side, character}` once an end went through and every
   other member's was refused as not open (ENDED_REFUSALS). An end the game ignored, one still running at
   END_SETTLE_S or any other failure writes no arena_turn_forced: the cap's ends are sent again once (CAP_TRIES),
   then the stall decides (held: capped again; the game's: a fault). The cap of a side's FORFEIT_TURNS-th turn in
   a row is its forfeit, decided there: `arena_turn_forced`, then the end goes to the mod, and no `act.end_turn` is
   sent (the mod keeps a match's first end, so no wipe at the next turn boundary can come before the forfeit; a
   wipe already there when arena.end comes is the end: the mod checks for one first). A phase capped only because of
   an unanswered prompts' charge (arena/unanswered.py), its model time under turn_cap_s, is forced like any (told the
   time it had, `arena_turn_forced`'s `charged_cap`) but counts toward no forfeit and breaks the run: only a side's own
   full cap can lose it the match (owner, 2026-11-08). So with a phase capped while its side's stream wrote nothing
   for a whole turn_cap_s of model time, or in progress when its side's client failed (`arena_turn_forced`'s `excused`,
   stream_silent or client_fault): a stalled stream is infrastructure, not play (owner, 2026-11-09). A stall that starts
   inside what would be a side's third forced phase in a row doesn't fill a whole cap, and still forfeits.
   Stall: a phase's idle clock (wall time less the phase's own side's model time gain, tick by tick; the other
   side thinking during it is no progress of the phase; once the phase was capped, no model time counts; before the
   first phase, the larger of the two sides' gains) runs from its last progress: a new phase, a
   change of arena.status's turn, round or HP, a new referee or roll ring row. At STALL_CAPS x turn_cap_s x members
   (at least STALL_MIN_S) the runner reads `state.combat` as the phase's side: a member of the phase still current,
   not ended and not passed by the mod (its arena_turn_passed: who can't act is the mod's call, not state.combat's
   downed or dead) means the side holds the turn, and the phase is capped as at the model-time cap
   (`arena_stall {stall: "held", ...}` first; it counts toward the forfeit); none (every end went in, or nobody can
   act), the read failing or a second stall of the same phase means the game holds it: `arena_stall {stall: "game",
   ...}` and the match ends as `fault` (invalid, rerun), no act.end_turn sent. So does a match whose first phase
   never comes, idle for max(STALL_CAPS x turn_cap_s, STALL_MIN_S) (its record names no side, character or members,
   round 0). arena_stall {stall, side, character, members, round, idle_s, status {turn, round, hp} (the last
   arena.status), last {A, B: the side's last STALL_CALLS tool calls from its events file, {tool, result}},
   detail?}.
   Each tick of a match still on, `arena.camera {}` (client context, no side: Client/ArenaCamera.lua) puts the game
   camera on the fighters; its answer (on the fighters, skipped and why, failed and the error) is logged when it
   changes, and a failure never ends the match.
5. A reaction prompt no team owns (a summon's name on both teams, no rows read) waits for an answer neither side may
   give: the runner declines it (`ui.react {use: false}`, no side) and writes `arena_reaction_unowned {round,
   detail}`. Both ops are the mod's client context's, as the bridge sends them. It reads `ui.state` for one only
   once a turn (or the wait for the first) is UNOWNED_CHECK_S old, then at most every UNOWNED_CHECK_S: a turn that
   moves on costs no read; while the game holds for a reaction, every REACTION_POLL_S. A prompt its owner leaves open
   for REACTION_ANSWER_S from its first read on screen is declined the same way (`arena_reaction_declined {round, side,
   detail, waited_s, over_budget}`), and the side is told (`arena.notice`). The wait comes off that side's next phases'
   caps, at most half a cap each (`arena_turn_charged {side, character, round, charged_s, debt_left_s}`); past its
   budget (BUDGET_SHARE of the wall cap, `arena_reaction_budget_spent {side, round, waited_s, budget_s}` once) its
   prompts are declined at their first read (arena/unanswered.py). A hold
   REACTION_STUCK_S past its prompt's last read on screen (or its first read, if none showed), past a decline, or
   past the first failure of a decline that keeps failing, is one nobody can clear: the match ends
   `fault`, so it can't hang. A stall with the game held or paused is the game's too, whoever's phase it is. The same
   read finds a prompt one side owns (the
   mod's `owner`, from the game's own observer) while the other side's phase is on: until the next read or the next
   phase, that side's model time counts as the
   phase's progress on the stall clock (the game waits for its answer, not for the acting side; fairness audit
   S2-1). While the game holds, the mod refuses a side's orders and ends (Guard.side_gate), the phase's model-time cap
   doesn't run and the time a read shows held doesn't count toward it.
   A phase whose character the game runs itself (arena.status's turn.lost_control: Guard.lost_control, a player has no
   control of it either) is its side's to watch: `arena_control_lost {side, character, uuid, status, round}` once at the
   first read, no model-time cap, no forfeit count, and the stall's model-time excuse doesn't apply. No end is sent for
   it: live, the game's AI ends such a turn itself, also with nothing in reach; one that hangs is a stall "game", a
   fault.
6. An agent that exits while the fight is on is resumed in the same conversation (resume_prompt); exit 0 counts toward
   EXIT_RESUMES (then a forfeit), anything else (a crash, an API or MCP failure, a hang) toward FAULT_RESUMES (then
   client_fault: invalid).

Both agents are stopped at the end; their exits and costs are in the outcome. A match a side of which the harness's
scripted client plays (a player "scripted/...") is a live check: invalid ("scripted: ...") whatever its end, and its
arena_end carries `invalid: "scripted"`.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from bg3harness import facts
from bg3harness.arena.scripted_client import SCRIPTED
from bg3harness.arena.spec import SIDES, ArenaSpec, Side, other
from bg3harness.arena.unanswered import BUDGET_SHARE, Unanswered, cap_words, charge_phase, note_declined

POLL_S = 1.0
SETUP_TIMEOUT_S = 180.0  # arena.setup's job and the match becoming active
HANG_S = 60.0  # arena.status unanswered this long: the game hung
FORFEIT_TURNS = 3  # a side's own turns forced to an end in a row
EXIT_RESUMES = 3  # a side's agent exiting (exit 0) mid-fight: resumed this many times, then it forfeits
FAULT_RESUMES = 1  # a side's client failing (crash, API/MCP failure, hang): resumed once, then the match is invalid
# A cap's act.end_turn job: polled this long for its end before the next member's. It outlasts the mod's own job:
# in a match a group's last end waits up to Actions.lua A.END_LAST_WAIT_MS (20 s) for the game to close the group
# (the job's timeout is 1 s more) before it fails "the game ignored the end-turn order".
END_SETTLE_S = 22.0
# The refusals of a cap's end for a member whose turn is no longer open (ActionsParty.lua end_turn, Guard.lua): not
# a failed end.
ENDED_REFUSALS = (
    "this character already ended its turn",
    "it is not this character's turn",
    "not your turn",
    "character is dead",
    "character is downed",
    "not in combat",
)
CAP_TRIES = 2  # a model-time cap whose ends didn't go through is sent this many times, then the stall decides
END_POLL_S = 0.25
STALL_CAPS = 3  # a phase idle this many times its turn cap (turn_cap_s x members) ...
STALL_MIN_S = 300.0  # ... and at least this long has stalled
STALL_CALLS = 3  # the tool calls of each side an arena_stall record shows
UNOWNED_CHECK_S = 15.0  # a turn this old: ui.state is read for a prompt no team owns, then at most this often
REACTION_POLL_S = 2.0  # while the game holds for a reaction: ui.state this often
PAUSED_REFUSAL = "the game is paused for a reaction decision"  # Guard.side_gate's words
REACTION_STUCK_S = 60.0  # a hold this long with no prompt on screen, or after its decline, ends the match
END_FIELDS = ("winner", "reason", "rounds", "hp_left_A", "hp_left_B")
# the runner's ends that make the match invalid (rerun, never graded): the game failed, our client side failed past
# its resume (client_fault), an operator's abort, the money cap
INVALID_ENDS = ("fault", "client_fault", "abort", "over_budget")
CONTEXT_OVERFLOW = "context_overflow"  # an agent's exit reason and its forfeit's cause (owner A6)
KICKOFF_OVERFLOW = "kickoff_overflow"  # an overflow before the side's first tool call: a client fault's cause
KICKOFF = "The fight is on. You play side {side}: {names}."
RESUME = "The fight is on."
FAULT_RESUME = (
    " Your client stopped and was restarted; the game kept running, and an order sent before it stopped may have run."
)


class Rpc(Protocol):
    def __call__(self, op: str, args: dict[str, Any], side: str | None = None, context: str = "server") -> Any: ...


class ArenaGame(Protocol):
    rpc: Rpc  # one mod op; raises when the game refuses or doesn't answer

    def alive(self) -> bool: ...  # the game process is up


@dataclass(frozen=True)
class AgentExit:
    reason: str  # "exited", context_overflow or the runner's fault reasons (crash, agent_hang, mcp_failed, ...)
    exit_code: int | None
    conversation: str | None = None  # the client's conversation id, to resume
    cost_usd: float | None = None

    @property
    def fault(self) -> bool:
        return self.reason != "exited" or self.exit_code != 0


class AgentRun(Protocol):
    def done(self) -> AgentExit | None: ...  # None while the process runs
    def model_s(self) -> float: ...  # the process's model time so far
    def silent_model_s(self) -> float: ...  # the model time since its stream last wrote anything
    def cost_usd(self) -> float: ...  # the process's cost so far (USD)
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
    members: list[str]  # uuids, in turn order, summons included (uuid is the first)
    names: list[str] = field(default_factory=list)  # their names as written in arena_turn
    forced: bool = False
    capped: bool = False  # the runner sent its cap's ends (forced once one went through)
    stalled: bool = False  # a stall "held" capped it
    cap_tries: int = 0  # the times the runner sent its cap's ends
    passed: set[str] = field(default_factory=set)  # lowercase uuids of members the mod passed (arena_turn_passed)
    control_lost: bool = False  # arena_control_lost was written for it
    charged_s: float = 0.0  # what its side's unanswered prompts took off its cap
    charged_cap: bool = False  # capped under that charge with its model time short of the full cap: no forfeit count
    excused: str | None = None  # stream_silent or client_fault: its side's infrastructure, no forfeit count


class Referee:
    """The match's facts: arena_start, the ring's arena records translated (RingFeed's translate), the forced turns
    and the end."""

    def __init__(self, spec: ArenaSpec, sides: Mapping[str, Side], writer: facts.FactsWriter):
        self.spec, self.sides, self.writer = spec, sides, writer
        self.names: dict[str, tuple[str, str]] = {}  # uuid -> (side, name)
        self.turns: list[Turn] = []
        self.mod_end: dict[str, Any] | None = None  # the mod's own arena_end from the ring
        self.combat_ended: dict[str, Any] | None = None  # the mod's arena_combat_ended
        self.done = False  # the match ended: later arena records are not the match's
        # arena_end's fields, written by write_end once both agents are stopped, so its cost holds every response
        # charged, the last ones too (window 6: written at the end moment it missed ~$0.0006 a match)
        self.pending_end: dict[str, Any] | None = None
        # the sides the harness's scripted client plays (a player "scripted/..."): the match is a live check, invalid
        self.scripted = [s for s in SIDES if sides[s].player.split("/")[0] == SCRIPTED]
        self.money: Callable[[], dict[str, Any]] = dict  # arena_end's cost and max_cost (run_arena sets it)

    @property
    def turn(self) -> Turn | None:
        return self.turns[-1] if self.turns else None

    @property
    def round(self) -> int:
        return self.turn.round if self.turn else 0

    def start(self, teams: Mapping[str, list[str]], surfaces: Any = None, field: Any = None) -> None:
        """arena_start; `teams`: the teams' uuids (_teams), in the arena file's team order; `surfaces`: a mirror
        arena's surface check from the setup result ({points, read, radius, off, wide}), when it has one; `field`:
        its field clean's counts ({radius, items, removed, ..., left {A, B}}), when it has one."""
        for s in SIDES:
            if len(teams.get(s) or []) != len(self.spec.teams[s]):
                raise ArenaSetupError(
                    f"arena.status team {s} has {len(teams.get(s) or [])} characters, the arena "
                    f"{len(self.spec.teams[s])}"
                )
            for uuid, name, want in zip(teams[s], self.spec.teams[s], self.spec.uuids[s], strict=True):
                if want is not None and uuid.lower() != want:
                    raise ArenaSetupError(f"arena.status team {s} has {uuid} for {name}, the arena {want}")
                self.names[uuid.lower()] = (s, name)
        self.event(
            "arena_start",
            arena=self.spec.name,
            seed=self.spec.seed,
            turn_order=self.spec.turn_order,
            teams=self.spec.names(),
            sides={s: self.sides[s].player for s in SIDES},
            uuids={s: list(teams[s]) for s in SIDES},
            **({"surfaces": surfaces} if isinstance(surfaces, dict) else {}),
            **({"field": field} if isinstance(field, dict) else {}),
        )

    def translate(self, fact: dict) -> dict | None:
        ev = str(fact.get("ev") or "")
        if not ev.startswith("arena_") or ev == "arena_surfaces":  # report-only re-reads: the end's comes with it
            return fact
        if self.done:
            return None
        if ev == "arena_turn":
            members = [str(u) for u in fact.get("members") or []]
            shown = [str(n) for n in fact.get("names") or []]
            if not members or len(shown) != len(members):
                return None
            team = [self.names.get(u.lower()) for u in members]
            if any(t is not None and t[0] != fact.get("side") for t in team):  # the other team's character
                return None
            names = [t[1] if t is not None else n for t, n in zip(team, shown, strict=True)]
            self.turns.append(Turn(str(fact["side"]), members[0], names[0], int(fact["round"]), members, names))
            return {**fact, "character": names[0], "members": names}
        if ev == "arena_group_end_written":
            members = [str(u) for u in fact.get("members") or []]
            shown = [str(n) for n in fact.get("names") or []]
            shown += members[len(shown) :]
            c = str(fact.get("character") or "")
            return {
                **fact,
                "character": self._named(c, shown[members.index(c)] if c in members else c),
                "members": [self._named(u, n) for u, n in zip(members, shown, strict=False)],
            }
        if ev == "arena_turn_passed":
            u = str(fact.get("character") or "")
            t = self.names.get(u.lower())
            turn = self.turn
            if turn is not None and fact.get("side") == turn.side and fact.get("round") == turn.round:
                turn.passed.add(u.lower())
            return {**fact, "character": t[1] if t is not None else str(fact.get("name") or u)}
        if ev == "arena_stood_up":  # a prone member stood up at its phase start (the mod's): named as in arena_turn
            u = str(fact.get("character") or "")
            return {**fact, "character": self._named(u, str(fact.get("name") or u))}
        if ev == "arena_turn_readmitted":  # a passed member stood up with its turn back: it can act again
            u = str(fact.get("character") or "")
            t = self.names.get(u.lower())
            if self.turn is not None:
                self.turn.passed.discard(u.lower())
            return {**fact, "character": t[1] if t is not None else str(fact.get("name") or u)}
        if ev == "arena_round":  # a round's first phase, its opening side (seat audit): written as the mod gave it
            return fact
        if ev == "arena_end":
            # the ring leaves a nil winner (a draw) out
            self.mod_end = {"winner": None, **fact, **self.invalid()}
            self.pending_end = self.mod_end
            self.done = True
            return None
        if ev == "arena_combat_ended" and self.combat_ended is None:
            self.combat_ended = fact
        return None

    def _named(self, uuid: str, shown: str) -> str:
        """A character as arena_turn names it: a team member by the arena file's name, anyone else by `shown`."""
        t = self.names.get(uuid.lower())
        return t[1] if t is not None else shown

    def forced(self, t: Turn) -> None:
        """arena_turn_forced for `t`, the capped phase (a later one may have begun while its ends settled)."""
        assert not t.forced
        t.forced = True
        self.event(
            "arena_turn_forced",
            side=t.side,
            character=t.name,
            **({"charged_cap": True} if t.charged_cap else {}),
            **({"excused": t.excused} if t.excused else {}),
        )

    def forfeits(self) -> bool:
        """Forcing the turn in progress makes FORFEIT_TURNS forced turns in a row of its side (a charged cap or an
        excused turn breaks the run: Turn.charged_cap, Turn.excused)."""
        t = self.turn
        assert t is not None
        own = [x for x in self.turns if x.side == t.side][-FORFEIT_TURNS:]
        counted = [x.forced and not x.charged_cap and not x.excused for x in own[:-1]]
        return len(own) == FORFEIT_TURNS and not (t.charged_cap or t.excused) and all(counted)

    def end(self, **fields: Any) -> dict[str, Any]:
        self.pending_end = {"ev": "arena_end", **fields, **self.invalid()}
        self.done = True
        return fields

    def write_end(self) -> None:
        """arena_end, once: the end's fields with the cost and max_cost as they stand now (after the stops)."""
        if self.pending_end is not None:
            self.writer.timed_events([{**self.pending_end, **self.money()}])
            self.pending_end = None

    def invalid(self) -> dict[str, str]:
        """arena_end's `invalid` field: "scripted" when a side is the scripted client, else none."""
        return {"invalid": SCRIPTED} if self.scripted else {}

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
        failed: str | None = None,
        capped: list[str] | None = None,
    ):
        super().__init__(reason)
        self.reason, self.winner, self.forfeit, self.cause, self.detail = reason, winner, forfeit, cause, detail
        self.rounds, self.game_up = rounds, game_up  # game_up: the game answers, the mod hears the end
        self.failed = failed  # client_fault: the side whose client failed
        self.capped = capped  # over_budget: the sides over their cap


def resume_prompt(fault: bool, turn: Turn | None, side: str) -> str:
    """What `side`'s restarted agent is told, facts only: the fight, that its client failed (a fault), and whose turn
    it is (not while a capped phase is ending: the next one isn't known). Only its own side's character is named: the
    other team's are listed only while seen (rules/arena.md)."""
    fell = FAULT_RESUME if fault else ""
    who = f"{turn.name}'s turn (side {side})" if turn and turn.side == side else "the other side's turn"
    now = f" Now round {turn.round}, {who}." if turn and not turn.capped else ""
    return f"{RESUME}{fell}{now}"


def excuse_fault(turn: Turn | None, side: str) -> None:
    """`side`'s client failed: the phase of its in progress counts toward no forfeit. One already forced stays counted:
    its arena_turn_forced is written, and the grader has to read the same streak."""
    if turn is not None and turn.side == side and not turn.forced:
        turn.excused = "client_fault"


def _fault_cause(ex: AgentExit) -> str:
    """A client fault's cause for arena_end: the exit's reason (api_error, mcp_failed, agent_hang, killed, ...), or
    "crash" for a process that exited non-zero with no other reason."""
    return "crash" if ex.reason == "exited" else ex.reason


def _setup(spec: ArenaSpec, game: ArenaGame, clock: Clock) -> tuple[dict[str, Any], dict[str, Any]]:
    """arena.setup's job to its end, then arena.status until active; (the job's result, the active status)."""
    deadline = clock.now() + SETUP_TIMEOUT_S
    job = game.rpc("arena.setup", {"arena": spec.arena, "round_cap": spec.round_cap}) or {}
    while job.get("status") not in ("done", "failed"):
        if clock.now() > deadline:
            raise ArenaSetupError(f"arena.setup still {job.get('status')} after {SETUP_TIMEOUT_S:.0f} s")
        clock.sleep(POLL_S)
        job = game.rpc("job.poll", {"id": job.get("job", job.get("id"))}) or {}
    if job["status"] == "failed":
        raise ArenaSetupError(f"arena.setup failed: {job.get('error')}")
    result = job.get("result")
    while True:
        st = game.rpc("arena.status", {}) or {}
        if st.get("active"):
            return (result if isinstance(result, dict) else {}), st
        if clock.now() > deadline:
            raise ArenaSetupError(f"the match was not active {SETUP_TIMEOUT_S:.0f} s after arena.setup")
        clock.sleep(POLL_S)


def _teams(spec: ArenaSpec, result: Mapping[str, Any], status: Mapping[str, Any]) -> dict[str, list[str]]:
    """Each side's uuids, in the arena file's order: arena.status's teams; a mirror arena's from the setup result,
    which arena.status must repeat (fail closed: never a uuid from the file, which names the twins only)."""
    teams = status.get("teams")
    if not spec.mirror:
        return teams if isinstance(teams, dict) else {}
    made = result.get("teams")
    got: dict[str, list[str]] = {}
    for s in SIDES:
        row = made.get(s) if isinstance(made, dict) else None
        if not (
            isinstance(row, list) and len(row) == len(spec.teams[s]) and all(isinstance(u, str) and u for u in row)
        ):
            raise ArenaSetupError(
                f"arena.setup's result gives team {s} of the mirror arena {row!r}, not {len(spec.teams[s])} uuids"
            )
        shown = teams.get(s) if isinstance(teams, dict) else None
        if not isinstance(shown, list) or [str(u).lower() for u in shown] != [u.lower() for u in row]:
            raise ArenaSetupError(f"arena.status team {s} is {shown!r}, arena.setup made {row}")
        got[s] = list(row)
    return got


def _prompt_owner(rx: Mapping[str, Any], ref: Referee) -> str | None:
    """The side whose characters an open reaction prompt's rows all name (the arena file's names, and the names its
    phases were written with: summons), None when a row names nobody of one side or the rows name both."""
    sides = {name: side for side, name in ref.names.values()}
    for t in ref.turns:
        sides.update(dict.fromkeys(t.names, t.side))
    owners = {sides.get(str(r.get("who"))) for r in rx.get("options") or [] if isinstance(r, dict)}
    return owners.pop() if len(owners) == 1 and None not in owners else None


@dataclass
class _Hold:
    """The hold the last read found: the game's key for it, when it was first read, when its prompt was first and
    last on screen, when it was declined."""

    key: str | None = None
    since: float = 0.0
    opened: float | None = None
    shown: float | None = None
    declined: float | None = None
    decline_failed: float | None = None
    ledger: Unanswered = field(default_factory=Unanswered)


def _reaction_check(
    spec: ArenaSpec, game: ArenaGame, ref: Referee, log: Callable[[str], None], hold: _Hold, now: float
) -> str | None:
    """Reads the game client's reaction state (ui.state with no side). Declines a prompt no team owns at once and one
    its owner left open for REACTION_ANSWER_S from its first read (at that read once the side's budget is spent); ends
    the match on a hold nobody can clear (REACTION_STUCK_S). Returns the side that owns an open prompt (None: no
    prompt, or none owned)."""
    try:
        ui = game.rpc("ui.state", {}, context="client") or {}
    except Exception as e:  # read again at the next check
        log(f"arena {spec.name}: the reaction check failed: {e}")
        return None
    rx = ui.get("reaction") if ui.get("screen") == "reaction" else None
    rx = rx if isinstance(rx, dict) else None
    held = ui.get("held_reaction") if isinstance(ui.get("held_reaction"), dict) else None
    if rx is None and held is None:
        hold.key = None
        return None
    key = str((held or {}).get("key") or (rx or {}).get("prompt") or "")
    if key != hold.key:
        hold.key, hold.since, hold.opened, hold.shown, hold.declined = key, now, None, None, None
        hold.decline_failed = None
    if rx is not None:
        hold.opened, hold.shown = hold.opened or now, now
    owner = (held or {}).get("side") if rx is None else None
    if rx is None or hold.declined is not None:
        last = hold.declined or hold.shown or hold.since  # time with a prompt on screen doesn't count
        if now - last >= REACTION_STUCK_S:
            age = now - hold.since
            raise _Ended("fault", detail=f"a reaction held the game {age:.0f} s with no answer left to give ({key})")
        return owner
    unowned = bool(rx.get("unowned"))
    owner = None if unowned else rx.get("owner") or _prompt_owner(rx, ref)
    if not unowned and now - (hold.opened or now) < hold.ledger.bound(owner):
        return owner
    if hold.decline_failed is not None and now - hold.decline_failed >= REACTION_STUCK_S:
        age = now - hold.since
        raise _Ended("fault", detail=f"a reaction held the game {age:.0f} s and its decline keeps failing ({key})")
    try:
        job = game.rpc("ui.react", {"use": False}, context="client") or {}
    except Exception as e:  # read again at the next check
        log(f"arena {spec.name}: declining a reaction prompt failed: {e}")
        hold.decline_failed = hold.decline_failed or now
        return owner
    if job.get("status") == "failed":
        log(f"arena {spec.name}: declining a reaction prompt failed: {job.get('error')}")
        hold.decline_failed = hold.decline_failed or now
        return owner
    hold.declined = now
    detail = str(rx.get("prompt") or "")
    if unowned:
        ref.event("arena_reaction_unowned", round=ref.round, detail=detail)
        log(f"arena {spec.name}: declined a reaction prompt no team owns ({detail})")
    else:
        if owner is not None:
            note_declined(game, ref, log, hold.ledger, owner, rx, now - (hold.opened or now))
        log(f"arena {spec.name}: declined side {owner}'s unanswered reaction prompt ({detail})")
    return None


def _paused(game: ArenaGame, side: str) -> bool:
    """The mod's own gate says the game is paused for a reaction: no side can end a turn then, so a stall isn't its."""
    try:
        game.rpc("guard.side_gate", {}, side)
    except Exception as e:
        return PAUSED_REFUSAL in str(e)
    return False


def _lost_control(status: Mapping[str, Any], turn: Turn | None) -> dict[str, Any] | None:
    """arena.status's turn.lost_control when that read is `turn`'s phase ({name?: its status}); None when not lost."""
    read = status.get("turn") or {}
    if turn is None or str(read.get("uuid") or "").lower() != turn.uuid.lower():
        return None
    lost = read.get("lost_control")
    if lost is None:
        return None
    return lost if isinstance(lost, dict) else {}  # a status with no name shown reads as an empty table


def _held(game: ArenaGame, turn: Turn) -> list[str]:
    """The phase's members its side still holds: in the side's own state.combat, current, not ended (an end of its
    own pending counts as ended), and not passed by the mod. Who can't act is the mod's call (Arena.lua cantAct:
    dead, DOWNED or 0 HP), told by its arena_turn_passed, never state.combat's downed/dead: live 10 m1 a2 r3 read
    Lae'zel at 0 HP without DOWNED (the mod then let her act) as downed, a fault instead of the cap. A member
    missing from the read isn't held."""
    st = game.rpc("state.combat", {}, turn.side) or {}
    rows = {str(r.get("uuid") or "").lower(): r for r in st.get("order") or []}
    held = []
    for u in turn.members:
        r = rows.get(u.lower())
        if r and r.get("is_current") and not r.get("ended") and u.lower() not in turn.passed:
            held.append(u)
    return held


def _last_calls(path: Path, n: int = STALL_CALLS) -> list[dict[str, Any]]:
    """A side's last n tool calls from its events file: {tool, result (its text, cut to 200 chars; None before it
    came)}; [] without the file."""
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    with contextlib.suppress(OSError):
        for line in path.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("kind") == "tool_call":
                c = {"tool": e.get("tool"), "result": None}
                calls.append(c)
                by_id[str(e.get("call_id"))] = c
            elif e.get("kind") == "tool_result" and (c := by_id.get(str(e.get("call_id")))) is not None:
                c["result"] = str(e.get("text") or "")[:200]
    return calls[-n:]


def _end_turn(
    game: ArenaGame, clock: Clock, uuid: str, side: str, forced: str, tick: Callable[[], None] = lambda: None
) -> str | None:
    """act.end_turn for `uuid` as `side` at a cap (`forced`: which one, as the mod tells the side: its turn_forced
    event and its out-of-phase refusals), its job polled while it runs (at most END_SETTLE_S), `tick` after each
    wait (the runner reads the ring: a phase that began meanwhile is marked then); None when it is done, else the
    refusal, the failure, its final status or "still running" (not an end)."""
    try:
        job = game.rpc("act.end_turn", {"char": uuid, "forced": forced}, side) or {}
        deadline = clock.now() + END_SETTLE_S
        while job.get("status", "running") == "running" and clock.now() < deadline:  # A.reply: no status while it runs
            clock.sleep(END_POLL_S)
            tick()
            job = game.rpc("job.poll", {"id": job.get("job", job.get("id"))}, side) or {}
    except Exception as e:  # a refusal (the op's error) or no answer
        return str(e)
    status = job.get("status", "running")
    if status == "done":
        return None
    if status == "failed":
        return str(job.get("error"))
    if status == "running":
        return f"act.end_turn still running after {END_SETTLE_S:g} s"
    return f"act.end_turn {status}: {job.get('error')}"  # interrupted or timeout: final, as RPC.lua's jobs are


def _ended_refusal(err: str) -> bool:
    """A cap end's refusal for a member whose turn is no longer open (ENDED_REFUSALS)."""
    return err.startswith(ENDED_REFUSALS)


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
    max_cost: float | None = None,
    side_caps: Mapping[str, float] | None = None,
) -> ArenaOutcome:
    """One match into `out` (facts.jsonl, rolls.jsonl; the agents write their own files); see the module doc.
    writer: the run's facts writer when the caller has one (its run_start stays time zero); max_cost: the match's
    money cap (USD, both sides together); side_caps: a side's own cap (USD) where it has one."""
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
        result, status = _setup(spec, game, clock)
        ref.start(_teams(spec, result, status), result.get("surfaces"), result.get("field"))
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
    closed_cost = dict.fromkeys(SIDES, 0.0)  # and their cost

    def spent() -> dict[str, float]:
        return {s: round(closed_cost[s] + (runs[s].cost_usd() if s in runs else 0.0), 6) for s in SIDES}

    ref.money = lambda: {"cost": spent(), "max_cost": max_cost}
    exits = dict.fromkeys(SIDES, 0)
    faults = dict.fromkeys(SIDES, 0)
    turn_mark: tuple[int, float] | None = None  # (index of the turn, the side's model time at its start)
    hp = dict.fromkeys(SIDES, 1.0)
    answered = clock.now()
    seen_turns, turn_at, ui_read = 0, started, started  # the turns so far, when the last began, the last ui.state
    idle, tick_at, moved_sig = 0.0, started, None  # the stall's idle clock, its last tick, the last progress read
    prompt_side: str | None = None  # the side whose reaction prompt the last ui.state read found open (S2-1)
    hold = _Hold(ledger=Unanswered(budget_s=spec.wall_cap_min * 60 * BUDGET_SHARE))
    charged_turn = -1  # the phase the ledger's debt was last taken from
    camera_seen: str | None = None  # the last arena.camera answer (on, skipped: why, failed: error), logged on change
    seen_model = dict.fromkeys(SIDES, 0.0)

    def model(side: str) -> float:
        return closed_model[side] + runs[side].model_s()

    settle_rows = 0  # ring rows read while a cap's end settled: the next tick's progress

    def mark_turn() -> None:
        """turn_mark for the latest phase, once: its side's model time when the runner first reads the phase."""
        nonlocal turn_mark
        if (turn := ref.turn) is not None and (turn_mark is None or turn_mark[0] != len(ref.turns) - 1):
            turn_mark = (len(ref.turns) - 1, model(turn.side))

    def settle_tick() -> None:
        """While a cap's end settles (up to END_SETTLE_S a member): the game may start the next phase before the
        old one's end settles (live 2026-11-02, S087: TurnEnded up to 18 s late), and that phase's side thinks from
        its start, so its mark is taken then, as at any tick, not once the ends are through (audit S3-7)."""
        nonlocal settle_rows
        settle_rows += sum(feed.poll())
        mark_turn()

    def cap_phase(turn: Turn, why: str, told: str, charged_cap: bool = False) -> None:
        """End the phase at a cap (`why`; `told`: the cap as its side is told): its side's forfeit when it is the
        FORFEIT_TURNS-th in a row, else act.end_turn for each member and arena_turn_forced once they went through.
        `charged_cap`: reached only because of a charge, so it can't count toward the forfeit; neither does a cap its
        side's stream was silent through (checked here: the client's fault isn't known until its silence limit)."""
        turn.capped, turn.charged_cap = True, charged_cap
        if runs[turn.side].silent_model_s() >= spec.turn_cap_s:
            turn.excused = "stream_silent"
        if ref.forfeits():
            ref.forced(turn)
            log(f"arena {spec.name}: {turn.name}'s turn (side {turn.side}) hit the {why}")
            raise _Ended("forfeit", winner=other(turn.side), forfeit=turn.side, cause="forced_turns")
        turn.cap_tries += 1
        errors = [_end_turn(game, clock, u, turn.side, told, settle_tick) for u in turn.members]
        failed = [e for e in errors if e is not None and not _ended_refusal(e)]
        if failed or all(errors):
            log(f"arena {spec.name}: ending {turn.name}'s turn at the {why} failed: {'; '.join(map(str, errors))}")
            return
        ref.forced(turn)
        log(f"arena {spec.name}: {turn.name}'s turn (side {turn.side}) ended at the {why}")

    try:
        while True:
            clock.sleep(POLL_S)
            now = clock.now()
            if aborted():
                raise _Ended("abort", detail="operator stop")
            if not game.alive():
                raise _Ended("fault", detail="the game process is gone", game_up=False)
            try:
                rows, settle_rows = sum(feed.poll()) + settle_rows, 0
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
            try:  # the view on the fight (the recording's); the camera never ends a match
                cam = game.rpc("arena.camera", {}, context="client") or {}
                cam_now = f"skipped: {cam['skipped']}" if cam.get("skipped") else "on the fighters"
            except Exception as e:
                cam_now = f"failed: {e}"
            if cam_now != camera_seen:
                camera_seen = cam_now
                log(f"arena {spec.name}: arena.camera {cam_now}")
            if ref.combat_ended is not None:
                n = int(ref.combat_ended.get("rounds") or ref.round)
                raise _Ended("fault", detail=f"the arena's combat ended without a wipe (rounds {n})", rounds=n)
            if ref.round > spec.round_cap:  # a mod that doesn't cap
                raise _Ended("round_cap")
            if now - started >= spec.wall_cap_min * 60:
                raise _Ended("wall_cap")
            if len(ref.turns) != seen_turns:
                seen_turns, turn_at, prompt_side = len(ref.turns), now, None
            if now - max(turn_at, ui_read) >= (REACTION_POLL_S if hold.key else UNOWNED_CHECK_S):
                ui_read = now
                prompt_side = _reaction_check(spec, game, ref, log, hold, now)
            for s in SIDES:
                ex = runs[s].done()
                if ex is None:
                    continue
                outcome.exits[s].append(ex)
                closed_cost[s] += ex.cost_usd if ex.cost_usd is not None else runs[s].cost_usd()
                closed_model[s] += runs.pop(s).model_s()
                if ex.fault:
                    faults[s] += 1
                    excuse_fault(ref.turn, s)
                else:
                    exits[s] += 1
                log(f"arena {spec.name}: side {s}'s agent ended mid-fight ({ex.reason}, exit {ex.exit_code})")
                if ex.reason == CONTEXT_OVERFLOW:
                    raise _Ended(
                        "forfeit",
                        winner=other(s),
                        forfeit=s,
                        cause=CONTEXT_OVERFLOW,
                        detail=f"side {s}'s agent: its conversation outgrew the model's context",
                    )
                if faults[s] > FAULT_RESUMES:  # its infrastructure, not its play: no result (owner D459)
                    raise _Ended(
                        "client_fault", failed=s, cause=_fault_cause(ex), detail=f"side {s}'s agent: {ex.reason}"
                    )
                if exits[s] > EXIT_RESUMES:
                    raise _Ended(
                        "forfeit",
                        winner=other(s),
                        forfeit=s,
                        cause="client_exit",
                        detail=f"side {s}'s agent: {ex.reason}",
                    )
                n = faults[s] + exits[s]
                told = resume_prompt(ex.fault, ref.turn, s)
                log(f"arena {spec.name}: side {s} resumed: {told}")
                runs[s] = agents.start(s, told, ex.conversation, f"-r{n}")
            cost, caps = spent(), side_caps or {}
            capped = [s for s in SIDES if s in caps and cost[s] >= caps[s]]
            if capped or (max_cost is not None and sum(cost.values()) >= max_cost):
                what = f"side {capped[0]}'s cap ${caps[capped[0]]:g}" if capped else f"max_cost ${max_cost:g}"
                raise _Ended(
                    "over_budget",
                    capped=capped or list(SIDES),
                    detail=f"A ${cost['A']:.4f} + B ${cost['B']:.4f} reached {what}",
                )
            turn = ref.turn
            lost = _lost_control(status, turn)
            if lost is not None and turn is not None and not turn.control_lost:
                turn.control_lost = True
                ref.event(
                    "arena_control_lost",
                    side=turn.side,
                    character=turn.name,
                    uuid=turn.uuid,
                    status=lost.get("name"),
                    round=turn.round,
                )
            gained = {s: model(s) - seen_model[s] for s in SIDES}
            seen_model = {s: model(s) for s in SIDES}
            # the phase's own side thinking is its progress; the other side's (planning in the other phase) is not
            if turn is None:
                excused = list(gained.values())
            else:
                excused = [] if turn.capped or lost is not None else [gained[turn.side]]
            if turn is not None and prompt_side not in (None, turn.side):  # the game waits on that side's answer
                excused.append(gained[prompt_side])
            idle += max(0.0, now - tick_at - max(excused, default=0.0))
            tick_at = now
            sig = (len(ref.turns), json.dumps(status.get("turn"), sort_keys=True), status.get("round"), hp)
            if rows or sig != moved_sig:
                idle, moved_sig = 0.0, sig
            members = len(turn.members) if turn is not None else 1
            if idle >= max(STALL_CAPS * spec.turn_cap_s * members, STALL_MIN_S):
                held, why = [], ""
                if turn is not None and not (turn.stalled or turn.forced):
                    try:
                        # during a pause nobody can end a turn: the game's stall (a fault, a rerun), not a forfeit
                        idle_game = hold.key or lost is not None or _paused(game, turn.side)
                        held = [] if idle_game else _held(game, turn)
                    except Exception as e:  # the game can't say: it holds the turn
                        why = f"state.combat failed: {e}"
                phase = {"side": turn.side, "character": turn.name, "members": turn.names} if turn else {}
                record = {
                    **phase,
                    "round": turn.round if turn is not None else ref.round,
                    "idle_s": round(idle, 1),
                    "status": {k: status.get(k) for k in ("turn", "round", "hp")},
                    "last": {s: _last_calls(out / f"{s}.events.jsonl") for s in SIDES},
                    **({"detail": why} if why else {}),
                }
                where = (
                    f"{turn.name}'s phase (side {turn.side}, round {turn.round})"
                    if turn is not None
                    else "the match before its first phase"
                )
                stall = f"stall: {where} idle {idle:.0f} s"
                idle = 0.0
                if turn is None or not held:
                    ref.event("arena_stall", stall="game", **record)
                    raise _Ended("fault", detail=f"{stall}, the game holds the turn" + (f" ({why})" if why else ""))
                ref.event("arena_stall", stall="held", **record)
                turn.stalled = True
                cap_phase(
                    turn,
                    f"stall ({stall}, side {turn.side} holds it)",
                    f"the stall cap (no progress for {record['idle_s']:.0f} s)",
                )
                continue
            if turn is None or turn.forced or turn.cap_tries >= CAP_TRIES:
                continue
            mark_turn()
            assert turn_mark is not None
            cap = spec.turn_cap_s  # one character a phase (the mod's turn order, D643): turn_cap_s x 1
            if lost is not None:  # the game's turn: what its side thinks meanwhile isn't this phase's
                turn_mark = (turn_mark[0], turn_mark[1] + gained[turn.side])
                continue
            if charged_turn != turn_mark[0]:  # a phase's first read: what its side's unanswered prompts cost it
                charged_turn = turn_mark[0]
                turn.charged_s = charge_phase(game, ref, log, hold.ledger, turn, cap)
                turn_mark = (turn_mark[0], turn_mark[1] - turn.charged_s)
            if not hold.key and model(turn.side) - turn_mark[1] >= cap:
                ui_read = now  # the mod refuses an end during a pause: read for one before capping
                prompt_side = _reaction_check(spec, game, ref, log, hold, now)
            if hold.key:  # waiting on the game's pause isn't the phase's thinking, slow as the other side may be
                turn_mark = (turn_mark[0], turn_mark[1] + gained[turn.side])
                continue
            if model(turn.side) - turn_mark[1] < cap:
                continue
            used = model(turn.side) - turn_mark[1] - turn.charged_s  # the charge moved the mark back: its real time
            cap_phase(turn, f"{cap - turn.charged_s:g} s cap", cap_words(cap, turn.charged_s), charged_cap=used < cap)
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
            if e.failed:
                fields.update(failed=e.failed, cause=e.cause)
            if e.capped:
                fields.update(capped=e.capped)
            outcome.end = ref.end(**fields)
            if e.reason in INVALID_ENDS:
                outcome.invalid = f"{e.reason}: {e.detail}"
            log(f"arena {spec.name}: {runners}")
    finally:
        for s, run in runs.items():
            outcome.exits[s].append(run.stop())
        ref.write_end()
    if outcome.invalid is None and ref.scripted:
        sides_ = " and ".join(ref.scripted)
        outcome.invalid = f"{SCRIPTED}: side {sides_} played the scripted client (a live check, never graded)"
        log(f"arena {spec.name}: invalid, {outcome.invalid}")
    with contextlib.suppress(Exception):  # a dead game: what was read is what there is
        feed.poll()  # the last rolls
    writer.mark("run_end", {"arena_end": outcome.end.get("reason") if outcome.end else None})
    return outcome
