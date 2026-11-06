#!/usr/bin/env python3
"""play.py arena-matrix offline: the pilot matrix (packages/bench/matrices/arena-pilot.toml) played through
arena_queue.ArenaQueue by a fake player that writes each match's facts, with its budget files in a temp dir: the
pairing's two matches back to back, reserves before and charges after each, a refused reserve, an interrupted match
rerun, an over_budget match not rerun, an invalid one rerun, runtime/STOP, a refusal, the disk guard, a harness
failure; live_play's caps; the plan and the table. Never launches the game.

    python3 -m pytest runner/test_arena_queue.py
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401, I001  (first: the game's profile folders are a temp dir before play)
import arena_queue as aq
import play as play_cli

from bg3bench import arena_matrix
from bg3harness.arena.match import ArenaSetupError
from test_arena_runner import level_arena

REPO = Path(__file__).resolve().parents[1]
PILOT = "packages/bench/matrices/arena-pilot.toml"
FILES = ("runtime/budget-arena.json", "runtime/budget-deepseek.json", "runtime/budget-haiku.json",
         "runtime/budget-anthropic.json")  # fmt: skip
M1, M2 = (f"pi_deepseek-flash~vs~pi_claude-haiku-5-5~crash-companions-v1~r1~m{n}" for n in (1, 2))
FX = REPO  # this repo with crash-companions-v1 as a playable fixture copy: set by fixture_repo


@pytest.fixture(scope="module", autouse=True)
def fixture_repo(tmp_path_factory):
    """The pilot's arena is a draft until live 15's height read (D349): the matrix plays a level copy of it, in a repo
    whose profiles and matrices are this one's."""
    global FX
    FX = tmp_path_factory.mktemp("repo")
    for d in ("profiles", "packages/bench/matrices"):
        (FX / d).parent.mkdir(parents=True, exist_ok=True)
        (FX / d).symlink_to(REPO / d)
    level_arena("crash-companions-v1", FX / "packages" / "bench" / "arenas")
    yield
    FX = REPO


def facts(job: dict, out: Path, reason: str = "wipe", **end: Any) -> None:
    """A whole match's facts as the referee writes them: side A wipes side B in round 1 (or another end)."""
    arena = arena_matrix.load(FX / PILOT, FX)["arenas"][job["arena"]]
    teams = {s: [m["name"] for m in arena["teams"][s]] for s in ("A", "B")}
    recs: list[dict[str, Any]] = [
        {"ev": "arena_start", "arena": arena["name"], "seed": job["seed"], "teams": teams,
         "sides": {s: job["sides"][s]["player"] for s in ("A", "B")}},
    ]  # fmt: skip
    order = [x for pair in zip(teams["A"], teams["B"], strict=True) for x in pair]  # A, B, A, B, A, B
    for n, (s, name) in enumerate(zip("ABABAB", order, strict=True), 1):
        recs.append({"ev": "arena_turn", "side": s, "character": name, "members": [name], "round": 1, "phase": n,
                     "group": "g", "shared": True})  # fmt: skip
    winner = "A" if reason == "wipe" else None
    recs.append({"ev": "arena_end", "winner": winner, "reason": reason, "rounds": 1, "hp_left_A": 1.0,
                 "hp_left_B": 0.0 if reason == "wipe" else 0.5, **end})  # fmt: skip
    out.mkdir(parents=True, exist_ok=True)
    rows = [{"v": 1, "kind": "event", "t": 1000.0 + i, **r} for i, r in enumerate(recs)]
    (out / "facts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


class Player:
    """The fake live.play: each call writes a match into tmp/<run name>; `script` gives the next calls' outcomes
    ("wipe", "over_budget", "invalid", "client_fault", "refused", "crash", "kill"), then wipes."""

    def __init__(self, tmp: Path, *script: str, costs: dict[str, float] | None = None) -> None:
        self.tmp, self.script, self.calls = tmp, list(script), []
        self.costs = costs or {"A": 0.2, "B": 0.3}

    def __call__(self, job: dict, name: str) -> aq.Played:
        self.calls.append(name)
        what = self.script.pop(0) if self.script else "wipe"
        if what == "refused":
            raise aq.RefusedError("side A: DEEPSEEK_API_KEY is set neither in the env nor in .env")
        if what == "crash":
            raise RuntimeError("the harness failed")
        if what == "kill":
            raise KeyboardInterrupt  # the matrix runner dies mid-match: its start has no end
        out = self.tmp / "sessions" / name
        if what == "client_fault":  # side A's client failed again after its resume: invalid, rerun
            facts(job, out, "client_fault", failed="A", cause="api_error")
            return aq.Played(out, "client_fault: side A's agent: api_error", dict(self.costs))
        if what == "over_budget":
            facts(job, out, "over_budget", capped=["A"])
        else:
            facts(job, out, "wipe")
        invalid = "fault: the game process is gone" if what == "invalid" else None
        return aq.Played(out, invalid, dict(self.costs))


@pytest.fixture
def pilot():
    return arena_matrix.load(FX / PILOT, FX)


def budgets(tmp: Path, **caps: float) -> None:
    for f in FILES:
        key = Path(f).stem.removeprefix("budget-")
        (tmp / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp / f).write_text(json.dumps({"cap": caps.get(key, 10.0), "spent": 0}))


def spent(tmp: Path) -> dict[str, float]:
    return {Path(f).stem.removeprefix("budget-"): json.loads((tmp / f).read_text())["spent"] for f in FILES}


def queue(pilot, tmp: Path, player, **kw) -> aq.ArenaQueue:
    return aq.ArenaQueue(pilot, tmp / "matrix" / pilot["name"], tmp, player, now=lambda: 1.0, **kw)


def progress(tmp: Path) -> tuple[str, str, int]:
    p = json.loads((tmp / "matrix" / "arena-pilot" / "progress.json").read_text())
    return p["state"], p["stop_reason"], p["pending"]


def lines(tmp: Path) -> list[dict]:
    return [json.loads(x) for x in (tmp / "matrix" / "arena-pilot" / "queue.jsonl").read_text().splitlines()]


def test_the_pairing_plays_back_to_back_and_charges_each_file_its_sides(tmp_path, pilot):
    budgets(tmp_path)
    player = Player(tmp_path)
    q = queue(pilot, tmp_path, player)
    assert q.run() == "finished"
    assert player.calls == [f"arena-pilot~{M1}~a1", f"arena-pilot~{M2}~a1"]
    # m1: A deepseek $0.2, B haiku $0.3; m2: A haiku $0.2, B deepseek $0.3
    assert spent(tmp_path) == pytest.approx({"arena": 1.0, "deepseek": 0.5, "haiku": 0.5, "anthropic": 0.5})
    assert {j: st["status"] for j, st in q.state().items()} == {M1: "done", M2: "done"}
    progress = json.loads((tmp_path / "matrix" / "arena-pilot" / "progress.json").read_text())
    assert (progress["state"], progress["done"], progress["runs"]) == ("finished", 2, 2)
    table = aq.barena.results(q.matches(), 2026, b=50)
    assert table["counts"] == {"matches": 2, "invalid": 0, "self": 0, "incomplete": 0, "pairings": 1}
    assert {r["player"]: r["points"] for r in table["models"]} == {"pi/deepseek-flash": 1.0, "pi/claude-haiku-5-5": 1.0}


class PinnedPlayer(Player):
    """Player whose matches write arena.json with the next pins of `pins` (live.play: run_host.machine_pins)."""

    def __init__(self, tmp: Path, *pins: Any, script: tuple[str, ...] = ()) -> None:
        super().__init__(tmp, *script)
        self.pins = list(pins)

    def __call__(self, job: dict, name: str) -> aq.Played:
        played = super().__call__(job, name)
        (played.run_dir / "arena.json").write_text(json.dumps({"pins": self.pins.pop(0)}))
        return played


def pins(commit: str = "a", mod: str = "m1") -> dict[str, Any]:
    return {"game_build": "25605617", "bg3le": {"sha256": "b7ec", "source": f"x-g{commit}"}, "mod": {"sha1": mod},
            "repo": {"commit": commit, "dirty": False, "diff": None}}  # fmt: skip


def test_a_match_that_played_with_other_pins_than_the_first_is_recorded_and_stops_the_matrix(tmp_path, pilot):
    """REVIEW-16 A2: a mod hot-fix between a pairing's two matches is a different experiment: m2's result records
    version_drift and the matrix stops (drift) unless the matrix file allows it."""
    budgets(tmp_path)
    player = PinnedPlayer(tmp_path, pins(), pins(mod="m2"))
    q = queue(pilot, tmp_path, player)
    stop = q.run()
    assert stop.startswith("drift: ") and "mod.sha1: m1 → m2" in stop
    ends = [r["result"] for r in lines(tmp_path) if r["type"] == "end"]
    assert [e["pins"]["mod"]["sha1"] for e in ends] == ["m1", "m2"]
    assert "version_drift" not in ends[0] and ends[1]["version_drift"] == ["mod.sha1: m1 → m2"]
    assert (ends[1]["valid"], ends[1]["invalid_reason"]) == (False, "version_drift: mod.sha1: m1 → m2")
    assert progress(tmp_path)[:2] == ("stopped", stop)


def test_a_drifted_match_plays_again_once_the_pins_are_restored_and_the_table_has_one_per_job(tmp_path, pilot):
    """REVIEW-18 5a: the drifted match was recorded valid, so after the restore it was never replayed and the table
    graded it with the rest (two code versions in one table)."""
    budgets(tmp_path)
    player = PinnedPlayer(tmp_path, pins(), pins(mod="m2"), pins())
    assert queue(pilot, tmp_path, player).run().startswith("drift: ")
    q = queue(pilot, tmp_path, player)  # the operator restored the pins and restarted
    assert q.run() == "finished"
    assert player.calls == [f"arena-pilot~{M1}~a1", f"arena-pilot~{M2}~a1", f"arena-pilot~{M2}~a2"]
    assert len(q.matches()) == 2
    picked = [next(a["result"] for a in st["attempts"] if a["result"]["valid"]) for st in q.state().values()]
    assert [r["pins"]["mod"]["sha1"] for r in picked] == ["m1", "m1"]
    assert all("version_drift" not in r for r in picked)


def test_a_drift_after_two_invalid_matches_is_no_rerun_and_never_reaches_the_table(tmp_path, pilot):
    """REVIEW-18g: status() counted the drifted run toward MAX_RERUNS, so a job with 2 earlier invalid matches that
    drifted gave up after the restore and matches() graded the drifted attempt; a matrix never restarted graded it."""
    budgets(tmp_path)
    player = PinnedPlayer(tmp_path, pins(), pins(), pins(mod="m2"), pins(), pins(), script=("invalid", "invalid"))
    assert queue(pilot, tmp_path, player).run().startswith("drift: ")
    q = queue(pilot, tmp_path, player)
    assert q.state()[M1]["status"] == "pending"
    assert len(q.attempts()) == 2  # never restarted: the drifted attempt counts nowhere
    assert q.run() == "finished"  # the operator restored the pins and restarted
    assert player.calls == [f"arena-pilot~{M1}~a{n}" for n in (1, 2, 3, 4)] + [f"arena-pilot~{M2}~a1"]
    assert len(q.matches()) == 2 and len(q.attempts()) == 4
    picked = [next(a["result"] for a in st["attempts"] if a["result"]["valid"]) for st in q.state().values()]
    assert [r["pins"]["mod"]["sha1"] for r in picked] == ["m1", "m1"]


def test_allow_drift_records_the_drift_and_plays_on(tmp_path, pilot):
    budgets(tmp_path)
    player = PinnedPlayer(tmp_path, pins(), pins(commit="b"))
    assert queue({**pilot, "allow_drift": True}, tmp_path, player).run() == "finished"
    ends = [r["result"] for r in lines(tmp_path) if r["type"] == "end"]
    assert ends[1]["version_drift"] == ["bg3le.source: x-ga → x-gb", "repo.commit: a → b"]
    assert ends[1]["valid"] is True  # allowed: valid, flagged


def test_flipping_allow_drift_mid_matrix_keeps_each_attempt_as_it_was_recorded(tmp_path, pilot):
    """REVIEW-18j: the table read the matrix's allow_drift now: a drifted match recorded valid under allow_drift left
    the table once the flag was removed, and a drift-invalid one came back once it was set."""
    budgets(tmp_path)
    player = PinnedPlayer(tmp_path, pins(), pins(commit="b"))
    assert queue({**pilot, "allow_drift": True}, tmp_path, player).run() == "finished"
    q = queue(pilot, tmp_path, player)  # the flag removed afterwards
    assert len(q.matches()) == 2 and len(q.attempts()) == 2  # the allowed drift stays the job's match
    other = tmp_path / "other"
    budgets(other)
    player = PinnedPlayer(other, pins(), pins(commit="b"))
    assert queue(pilot, other, player).run().startswith("drift: ")
    q = queue({**pilot, "allow_drift": True}, other, player)  # the flag set afterwards
    assert len(q.attempts()) == 1  # the drift-invalid attempt stays out


def test_the_same_pins_play_on_and_unread_pins_after_the_first_count_as_drift(tmp_path, pilot):
    budgets(tmp_path)
    assert queue(pilot, tmp_path, PinnedPlayer(tmp_path, pins(), pins())).run() == "finished"
    assert all("version_drift" not in r["result"] for r in lines(tmp_path) if r["type"] == "end")
    other = tmp_path / "other"
    budgets(other)
    stop = queue(pilot, other, PinnedPlayer(other, pins(), {"error": "OSError: no appmanifest"})).run()
    assert stop.startswith("drift: ") and "pins: not read (arena.json)" in stop


def test_a_queue_the_operator_closed_is_refused_with_nothing_charged_or_played(tmp_path, pilot):
    budgets(tmp_path)
    player = Player(tmp_path, "kill")
    with pytest.raises(KeyboardInterrupt):
        queue(pilot, tmp_path, player).run()  # the runner dies mid-match: a start with no end
    log = tmp_path / "matrix" / pilot["name"] / "queue.jsonl"
    rec = {"type": aq.OPERATOR_STOP, "job": M1, "attempt": 1, "t": 2.0, "by": "op", "why": "spend written by hand"}
    with log.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    before = spent(tmp_path)
    q = queue(pilot, tmp_path, player)
    with pytest.raises(ValueError, match="closed by the operator: spend written by hand"):
        q.run()
    assert spent(tmp_path) == before and len(player.calls) == 1
    assert [r["type"] for r in lines(tmp_path)][-2:] == ["start", aq.OPERATOR_STOP]
    assert q.state()[M1]["attempts"][-1]["result"] is None  # no end record was made up


def test_a_model_file_in_a_sibling_checkout_is_the_ledger_the_match_reads_and_charges(tmp_path, pilot):
    tree = tmp_path / "tree"
    budgets(tree)
    sibling = "../ovh-e2e/runtime/budget-deepseek.json"
    ledger = tmp_path / "ovh-e2e" / "runtime" / "budget-deepseek.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"cap": 0.1, "spent": 0}))
    jobs = [{**j, "budgets": {"own": j["budgets"]["own"], "A": [sibling], "B": [sibling]}} for j in pilot["jobs"]]
    m = {**pilot, "jobs": jobs}
    player = Player(tree)
    assert queue(m, tree, player).run() == "budget"  # its reserve doesn't fit the sibling's cap
    assert player.calls == []
    ledger.write_text(json.dumps({"cap": 10.0, "spent": 1.0}))
    assert queue(m, tree, player).run() == "finished"
    assert json.loads(ledger.read_text())["spent"] == 2.0  # both matches' A + B
    assert not (tree / "ovh-e2e").exists() and not (tree / "runtime" / sibling).exists()


def test_a_reserve_a_budget_file_cant_hold_stops_the_matrix_and_a_later_run_resumes(tmp_path, pilot):
    budgets(tmp_path, haiku=0.5)  # the haiku side reserves $1.00
    player = Player(tmp_path)
    assert queue(pilot, tmp_path, player).run() == "budget"
    assert player.calls == [] and [r["type"] for r in lines(tmp_path)] == ["plan"]
    budgets(tmp_path)
    assert queue(pilot, tmp_path, player).run() == "finished"
    assert len(player.calls) == 2


def test_a_match_the_matrix_runner_died_in_is_charged_its_reserves_and_played_again(tmp_path, pilot):
    budgets(tmp_path)
    player = Player(tmp_path, "kill")
    with pytest.raises(KeyboardInterrupt):
        queue(pilot, tmp_path, player).run()
    assert [r["type"] for r in lines(tmp_path)] == ["plan", "start"]
    assert progress(tmp_path)[:2] == ("stopped", "aborted: KeyboardInterrupt: ")  # never "running" after it died
    assert queue(pilot, tmp_path, player).run() == "finished"
    assert player.calls == [f"arena-pilot~{M1}~a1", f"arena-pilot~{M1}~a2", f"arena-pilot~{M2}~a1"]
    # the interrupted match: each paid side's whole reserve ($1); then the two played matches
    assert spent(tmp_path) == pytest.approx({"arena": 3.0, "deepseek": 1.5, "haiku": 1.5, "anthropic": 1.5})
    ends = [r["result"]["invalid_reason"] for r in lines(tmp_path) if r["type"] == "end"]
    assert ends == ["interrupted", None, None]


def usage(path: Path, *rows: tuple[str, float]) -> None:
    """A side's events: a usage event per (process, its running cost)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines_ = [{"v": 1, "seq": i, "t": 1.0 + i, "src": "client", "kind": "usage", "session_id": p, "cost_usd": c}
              for i, (p, c) in enumerate(rows)]  # fmt: skip
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in lines_))  # as EventLog writes


# D192 (seat run r3 a1): the matrix runner was killed while the match's game loaded, before either side's first model
# call; its reserve stayed charged to budget-arena-v0.json though nothing was spent.
def test_a_match_killed_before_any_model_call_is_charged_nothing(tmp_path, pilot):
    budgets(tmp_path)
    player = Player(tmp_path, "kill")
    with pytest.raises(KeyboardInterrupt):
        queue(pilot, tmp_path, player, sessions=tmp_path / "sessions").run()
    (tmp_path / "sessions" / f"arena-pilot~{M1}~a1" / "A").mkdir(parents=True)  # run files written, no process yet
    assert queue(pilot, tmp_path, player, sessions=tmp_path / "sessions").run() == "finished"
    ends = [r["result"] for r in lines(tmp_path) if r["type"] == "end"]
    assert (ends[0]["invalid_reason"], ends[0]["costs"]) == ("interrupted", {"A": 0.0, "B": 0.0})
    assert spent(tmp_path) == pytest.approx({"arena": 1.0, "deepseek": 0.5, "haiku": 0.5, "anthropic": 0.5})


def test_a_match_killed_mid_fight_is_charged_what_its_sides_billed(tmp_path, pilot):
    budgets(tmp_path)
    player = Player(tmp_path, "kill")
    with pytest.raises(KeyboardInterrupt):
        queue(pilot, tmp_path, player, sessions=tmp_path / "sessions").run()
    run = tmp_path / "sessions" / f"arena-pilot~{M1}~a1"
    usage(run / "A.events.jsonl", ("p1", 0.01), ("p1", 0.04), ("p2", 0.02))  # p1 resumed as p2: 0.04 + 0.02
    usage(run / "B.events.jsonl", ("q1", 0.05))
    assert queue(pilot, tmp_path, player, sessions=tmp_path / "sessions").run() == "finished"
    end = next(r["result"] for r in lines(tmp_path) if r["type"] == "end")
    assert end["costs"] == {"A": 0.06, "B": 0.05}
    assert spent(tmp_path)["arena"] == pytest.approx(1.0 + 0.11)


# D822: runtime/budget.json is charged by live.play as a match ends, so a killed match's spend never reached it.
def test_a_killed_match_is_charged_to_the_global_budget_at_its_matrix_figure(tmp_path, pilot):
    budgets(tmp_path)
    glob = tmp_path / "runtime" / "budget.json"
    glob.write_text(json.dumps({"cap": 10.0, "spent": 1.0}))
    player = Player(tmp_path, "kill")
    with pytest.raises(KeyboardInterrupt):
        queue(pilot, tmp_path, player, sessions=tmp_path / "sessions", global_budget=glob).run()
    run = tmp_path / "sessions" / f"arena-pilot~{M1}~a1"
    usage(run / "A.events.jsonl", ("p1", 0.04))
    usage(run / "B.events.jsonl", ("q1", 0.05))
    assert queue(pilot, tmp_path, player, sessions=tmp_path / "sessions", global_budget=glob).run() == "finished"
    b = json.loads(glob.read_text())  # the fake player charges nothing: only the interrupted match is here
    assert b["spent"] == pytest.approx(1.0 + 0.09) and b["last"]["playthrough"] == f"arena-pilot~{M1}~a1"
    assert spent(tmp_path)["arena"] == pytest.approx(1.0 + 0.09)  # the same figure as the matrix's own file


def test_a_match_live_play_finished_is_not_charged_to_the_global_budget_again(tmp_path, pilot):
    budgets(tmp_path)
    glob = tmp_path / "runtime" / "budget.json"
    glob.write_text(json.dumps({"cap": 10.0, "spent": 1.0}))
    player = Player(tmp_path, "kill")
    with pytest.raises(KeyboardInterrupt):
        queue(pilot, tmp_path, player, sessions=tmp_path / "sessions", global_budget=glob).run()
    run = tmp_path / "sessions" / f"arena-pilot~{M1}~a1"
    usage(run / "A.events.jsonl", ("p1", 0.04))
    (run / "arena.json").write_text("{}")  # live.play charged runtime/budget.json, then the runner died
    assert queue(pilot, tmp_path, player, sessions=tmp_path / "sessions", global_budget=glob).run() == "finished"
    assert json.loads(glob.read_text())["spent"] == 1.0
    assert spent(tmp_path)["arena"] == pytest.approx(1.0 + 0.04)


def test_an_over_budget_match_is_not_rerun_and_an_invalid_one_is(tmp_path, pilot):
    budgets(tmp_path)
    player = Player(tmp_path, "over_budget", "invalid")
    q = queue(pilot, tmp_path, player)
    assert q.run() == "finished"
    assert player.calls == [f"arena-pilot~{M1}~a1", f"arena-pilot~{M2}~a1", f"arena-pilot~{M2}~a2"]
    st = q.state()
    assert (st[M1]["status"], st[M2]["status"]) == ("over_budget", "done")
    assert st[M2]["attempts"][0]["result"]["invalid_reason"] == "fault: the game process is gone"
    assert aq.barena.results(q.matches(), 2026, b=10)["counts"]["incomplete"] == 1  # m1 has no valid match
    failures = aq.barena.results(q.matches(), 2026, b=10, attempts=q.attempts())["failures"]
    by = {r["player"]: r["over_budget"] for r in failures}  # owner R4: the table publishes it, per model
    assert by == {st[M1]["job"]["sides"]["A"]["player"]: 1, st[M1]["job"]["sides"]["B"]["player"]: 0}


def test_a_client_fault_is_rerun_and_counts_in_its_models_failure_rate(tmp_path, pilot):
    """Owner D459 (fairness audit S1-1): side A's client failing past its resume makes the match invalid (rerun, not
    a forfeit), and the table's failures count it for side A's model over every attempt, the rerun one too."""
    budgets(tmp_path)
    player = Player(tmp_path, "client_fault")
    q = queue(pilot, tmp_path, player)
    assert q.run() == "finished"
    assert player.calls == [f"arena-pilot~{M1}~a1", f"arena-pilot~{M1}~a2", f"arena-pilot~{M2}~a1"]
    st = q.state()
    assert st[M1]["attempts"][0]["result"]["invalid_reason"] == "client_fault"
    table = aq.barena.results(q.matches(), 2026, b=10, attempts=q.attempts())
    assert table["counts"]["invalid"] == 0  # the rerun is the job's match
    a = st[M1]["job"]["sides"]["A"]["player"]
    by = {r["player"]: r for r in table["failures"]}
    assert (by[a]["n_matches"], by[a]["client_faults"], by[a]["causes"]) == (3, 1, {"api_error": 1})
    assert sum(r["client_faults"] for r in table["failures"]) == 1


def test_the_stop_file_stops_between_matches_and_the_matrix_resumes(tmp_path, pilot):
    budgets(tmp_path)
    player = Player(tmp_path)
    assert queue(pilot, tmp_path, player, stopped=lambda: bool(player.calls)).run() == "stopped"
    assert player.calls == [f"arena-pilot~{M1}~a1"]
    assert queue(pilot, tmp_path, player).run() == "finished"
    assert player.calls[-1] == f"arena-pilot~{M2}~a1"


def test_a_refused_match_stops_the_matrix_without_an_attempt(tmp_path, pilot):
    budgets(tmp_path)
    player = Player(tmp_path, "refused")
    assert queue(pilot, tmp_path, player).run().startswith("refused: side A: DEEPSEEK_API_KEY is set neither")
    state, stop, pending = progress(tmp_path)
    assert (state, pending) == ("stopped", 2) and stop.startswith("refused: side A: DEEPSEEK_API_KEY")
    assert queue(pilot, tmp_path, player).run() == "finished"
    st = queue(pilot, tmp_path, player).state()[M1]
    assert [bool(a["result"].get("refused")) for a in st["attempts"]] == [True, False]
    assert spent(tmp_path)["arena"] == pytest.approx(1.0)  # the refusal spent nothing


def test_runtime_budget_json_short_of_the_matchs_cap_stops_the_matrix_before_it_starts(tmp_path, pilot):
    """live.play charges runtime/budget.json and refuses a match whose cap ($1 + $1) it can't hold; the matrix
    checks it with the reserves, so it stops on budget (resumable) before starting a match live.play would refuse."""
    budgets(tmp_path)
    glob = tmp_path / "runtime" / "budget.json"
    glob.write_text(json.dumps({"cap": 4.5, "spent": 2.6}))
    player = Player(tmp_path)
    assert queue(pilot, tmp_path, player, global_budget=glob).run() == "budget"
    assert player.calls == [] and [r["type"] for r in lines(tmp_path)] == ["plan"]
    glob.write_text(json.dumps({"cap": 4.5, "spent": 2.5}))  # exactly $2 left
    assert queue(pilot, tmp_path, player, global_budget=glob).run() == "finished"
    assert json.loads(glob.read_text())["spent"] == 2.5  # live.play charges it, not the matrix


def test_the_disk_guard_stops_the_matrix_before_a_match(tmp_path, pilot):
    budgets(tmp_path)
    player = Player(tmp_path)
    assert queue(pilot, tmp_path, player, disk=lambda job: "100 MB free").run() == "disk: 100 MB free"
    assert player.calls == []


def test_a_harness_failure_mid_match_stops_the_matrix_as_its_cost_is_unknown(tmp_path, pilot):
    budgets(tmp_path)
    player = Player(tmp_path, "crash")
    assert queue(pilot, tmp_path, player).run() == "cost unknown"
    assert spent(tmp_path)["arena"] == 0


def test_a_queue_of_another_plan_is_refused(tmp_path, pilot):
    budgets(tmp_path)
    q = queue(pilot, tmp_path, Player(tmp_path))
    q.q.create("arena-pilot", pilot["jobs"][:1])
    with pytest.raises(ValueError, match="holds another plan"):
        q.run()


def test_live_play_caps_each_paid_side_at_its_reserve_and_the_match_at_their_sum(tmp_path, pilot, monkeypatch):
    from bg3harness.arena import live

    seen: dict[str, Any] = {}

    class Outcome:
        invalid = None

        def cost(self, side: str) -> float:
            return {"A": 0.1, "B": 0.4}[side]

    def fake_play(spec, sides, make_runner, name, env, max_cost=None, side_caps=None):
        seen.update(spec=spec.name, sides=sides, name=name, max_cost=max_cost, side_caps=side_caps)
        return Outcome(), tmp_path / name

    monkeypatch.setattr(live, "play", fake_play)
    played = aq.live_play(pilot, REPO, {})(pilot["jobs"][1], "n1")
    assert (seen["spec"], seen["max_cost"], seen["side_caps"]) == ("crash-companions-v1", 2.0, {"A": 1.0, "B": 1.0})
    assert seen["sides"]["A"].player == "pi/claude-haiku-5-5"
    assert seen["sides"]["A"].profile == str(REPO / "profiles" / "claude-haiku-5-5.toml")
    assert played == aq.Played(tmp_path / "n1", None, {"A": 0.1, "B": 0.4})

    def refused(*a, **k):
        raise ArenaSetupError("the global budget has $0.20 left, less than the match's max_cost $2")

    monkeypatch.setattr(live, "play", refused)
    with pytest.raises(aq.RefusedError, match=r"less than the match's max_cost \$2"):
        aq.live_play(pilot, REPO, {})(pilot["jobs"][0], "n2")


def test_a_repeats_match_plays_and_is_graded_on_its_jobs_seed(tmp_path, pilot, monkeypatch):
    """Fairness audit S1-4: repeat 2 plays the arena's seed + 1 (live.play's spec, so arena.json and arena_start
    record it), and its grade checks that seed: the arena's own seed there is an unseeded match."""
    from bg3harness.arena import live

    job = {**pilot["jobs"][0], "repeat": 2, "seed": pilot["jobs"][0]["seed"] + 1}
    seen: dict[str, Any] = {}

    class Outcome:
        invalid = None

        def cost(self, side: str) -> float:
            return 0.0

    def fake_play(spec, sides, make_runner, name, env, max_cost=None, side_caps=None):
        seen["seed"] = spec.seed
        return Outcome(), tmp_path / name

    monkeypatch.setattr(live, "play", fake_play)
    aq.live_play(pilot, REPO, {})(job, "n1")
    arena = pilot["arenas"][job["arena"]]
    assert seen["seed"] == arena["seed"] + 1
    facts(job, tmp_path / "m")
    assert aq.graded(job, arena, tmp_path / "m")["valid"] is True
    facts({**job, "seed": arena["seed"]}, tmp_path / "old")
    assert aq.graded(job, arena, tmp_path / "old")["invalid_reason"] == "unseeded"


def test_play_py_prints_the_plan_and_its_estimate():
    out = io.StringIO()
    assert aq.main(["--matrix", PILOT, "--plan"], repo=FX, out=out) == 0
    text = out.getvalue()
    assert "arena matrix arena-pilot: 2 matches, 1 pairings" in text
    assert "runtime/budget-arena.json: up to $4.00" in text and "runtime/budget-haiku.json: up to $2.00" in text
    err = io.StringIO()
    sys_err, sys.stderr = sys.stderr, err
    try:
        assert play_cli.main(["arena-matrix", "--matrix", "packages/bench/matrices/no-such.toml", "--plan"]) == 2
    finally:
        sys.stderr = sys_err
    assert "play.py arena-matrix refused" in err.getvalue()


def test_a_paid_sides_cap_is_its_models_max_cost_else_its_arenas():
    # arena v0 pilot: [budget.max_cost_models] gives Opus's side its own cap; a subscription side has none
    job = {"max_cost": 0.5, "sides": {"A": {"client": "pi", "max_cost": 2.5}, "B": {"client": "pi"}}}
    assert aq.side_caps(job) == {"A": 2.5, "B": 0.5}
    job["sides"]["B"] = {"client": "claude", "max_cost": 9.0}
    assert aq.side_caps(job) == {"A": 2.5}
