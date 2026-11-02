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


def facts(job: dict, out: Path, reason: str = "wipe") -> None:
    """A whole match's facts as the referee writes them: side A wipes side B in round 1 (or another end)."""
    arena = arena_matrix.load(FX / PILOT, FX)["arenas"][job["arena"]]
    teams = {s: [m["name"] for m in arena["teams"][s]] for s in ("A", "B")}
    recs: list[dict[str, Any]] = [
        {"ev": "arena_start", "arena": arena["name"], "seed": arena["seed"], "teams": teams,
         "sides": {s: job["sides"][s]["player"] for s in ("A", "B")}},
    ]  # fmt: skip
    order = [x for pair in zip(teams["A"], teams["B"], strict=True) for x in pair]  # A, B, A, B, A, B
    for n, (s, name) in enumerate(zip("ABABAB", order, strict=True), 1):
        recs.append({"ev": "arena_turn", "side": s, "character": name, "members": [name], "round": 1, "phase": n,
                     "group": "g", "shared": True})  # fmt: skip
    winner = "A" if reason == "wipe" else None
    recs.append({"ev": "arena_end", "winner": winner, "reason": reason, "rounds": 1, "hp_left_A": 1.0,
                 "hp_left_B": 0.0 if reason == "wipe" else 0.5})  # fmt: skip
    out.mkdir(parents=True, exist_ok=True)
    rows = [{"v": 1, "kind": "event", "t": 1000.0 + i, **r} for i, r in enumerate(recs)]
    (out / "facts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


class Player:
    """The fake live.play: each call writes a match into tmp/<run name>; `script` gives the next calls' outcomes
    ("wipe", "over_budget", "invalid", "refused", "crash", "kill"), then wipes."""

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
        facts(job, out, "over_budget" if what == "over_budget" else "wipe")
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
