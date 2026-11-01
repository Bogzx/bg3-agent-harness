# pyright: basic
"""Arena pairing matrices: models x models x arenas x repeats, each pairing two matches with the sides swapped.

`python -m bg3bench.arena_matrix plan --matrix <file.toml>` prints the plan and writes nothing. The file (paths
relative to the repo):

    name = "arena-pilot"                              # runtime/matrix/<name>/ holds the queue
    models = ["profiles/a.toml", "profiles/b.toml"]   # client profiles; every pair of them plays (round robin)
    pairs = [["profiles/a.toml", "profiles/a.toml"]]  # or: the pairs to play, instead of every pair of models
    arenas = ["open_field"]                           # packages/bench/arenas/<name>.json
    repeats = 1
    allow_unverified = false                          # true: profiles whose status is "offline"

    [budget]
    file = "runtime/budget-arena.json"                # this matrix's own cap: every match's cost, both sides'
    max_cost = { open_field = 3.0 }                   # USD per side per match: a paid side's reserve and cap

    [budget.models]                                   # a side's cost also goes into its model's files (bench's)
    "profiles/a.toml" = ["runtime/budget-opus.json", "runtime/budget-anthropic.json"]

Give `models` or `pairs`. A pair may name one profile twice (a self-match: graded, never rated). The jobs are
matches, repeat-major, the two matches of a pairing one after the other (match 1: the pair's first model on side A,
match 2: on side B), so a stopped matrix leaves whole pairings. A draft arena (arena.load's `draft`) gets no
matches: plan lists it as skipped. A paid side (matrix.PAID_CLIENTS) needs its arena's max_cost.

Each match charges both models: `reserves(job)` is what each budget file holds back before the match starts (the
matrix's own file both sides' reserves, each model file its side's; a file both sides charge both), `charges(job,
costs)` what each file gets from the two sides' costs as it ends. The queue (`open_queue`) is bench.matrix's format and
fold (a plan line, then start/end per attempt; an interrupted attempt reruns), so a matrix resumes from its queue;
the runner that plays the matches is the harness's.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from bg3bench import arena, matrix
from bg3bench.matrix_cli import MatrixFileError, load_profile

ARENAS = Path("packages/bench/arenas")
BUDGET = "runtime/budget.json"


def player(model: Mapping[str, Any]) -> str:
    """The name a side's model is rated under (arena_start's sides): "<client>/<model slug>"."""
    return f"{model['client']}/{model['slug']}"


def _strs(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) and x for x in v)


def _pairs(raw: dict, path: Path) -> list[tuple[str, str]]:
    if ("models" in raw) == ("pairs" in raw):
        raise MatrixFileError(f"{path}: give either models (every pair plays) or pairs")
    if "models" in raw:
        ms = raw["models"]
        if not _strs(ms) or len(ms) < 2 or len(set(ms)) != len(ms):
            raise MatrixFileError(f"{path}: models must list at least two different profiles")
        return [(a, b) for i, a in enumerate(ms) for b in ms[i + 1 :]]
    ps = raw["pairs"]
    if not isinstance(ps, list) or not ps or not all(_strs(p) and len(p) == 2 for p in ps):
        raise MatrixFileError(f"{path}: pairs must list [profile, profile] pairs")
    if len({frozenset(p) for p in ps}) != len(ps):
        raise MatrixFileError(f"{path}: pairs lists a pair twice")
    return [(a, b) for a, b in ps]


def _budget(raw: dict, path: Path, arenas: Sequence[str], profiles: Sequence[str]) -> dict[str, Any]:
    budget = raw.get("budget", {})
    max_cost = budget.get("max_cost", {})
    if not isinstance(max_cost, dict) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) and v > 0 for v in max_cost.values()
    ):
        raise MatrixFileError(f"{path}: budget.max_cost maps arenas to USD > 0")
    if unknown := sorted(set(max_cost) - set(arenas)):
        raise MatrixFileError(f"{path}: budget.max_cost names arenas not in the matrix: {unknown}")
    own = budget.get("file", BUDGET)
    models = budget.get("models", {})
    if not isinstance(models, dict) or not all(_strs(v) and len(set(v)) == len(v) for v in models.values()):
        raise MatrixFileError(f"{path}: budget.models maps profiles to lists of budget files")
    if unknown := sorted(set(models) - set(profiles)):
        raise MatrixFileError(f"{path}: budget.models names profiles not in the matrix: {unknown}")
    if any(own in v for v in models.values()):
        raise MatrixFileError(f"{path}: budget.models lists the matrix's own file {own}")
    return {"file": own, "max_cost": {k: float(v) for k, v in max_cost.items()}, "models": models}


def plan(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    arenas: Sequence[str],
    repeats: int,
    *,
    max_cost: Mapping[str, float] | None = None,
    budgets: Mapping[str, Sequence[str]] | None = None,
    own: str = BUDGET,
    condition: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The matches. A model is matrix_cli.load_profile's {"slug", "client", "effort", "profile"}; budgets: profile
    -> its model budget files; condition: arena -> its condition id (default rules)."""
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    max_cost, budgets, condition = max_cost or {}, budgets or {}, condition or {}
    jobs = []
    for r in range(1, repeats + 1):
        for x, y in pairs:
            for a in arenas:
                pid = f"{matrix._slug(player(x))}~vs~{matrix._slug(player(y))}~{a}~r{r}"
                for n, (first, second) in enumerate(((x, y), (y, x)), 1):
                    jobs.append(
                        {
                            "id": f"{pid}~m{n}",
                            "pairing": pid,
                            "match": n,
                            "arena": a,
                            "condition": condition.get(a, "rules"),
                            "repeat": r,
                            "sides": {
                                "A": {**first, "player": player(first)},
                                "B": {**second, "player": player(second)},
                            },
                            "max_cost": max_cost.get(a),
                            "budgets": {
                                "own": own,
                                "A": list(budgets.get(first["profile"], ())),
                                "B": list(budgets.get(second["profile"], ())),
                            },
                        }
                    )
    if len({j["id"] for j in jobs}) != len(jobs):
        raise ValueError("duplicate matches: a pair or an arena is listed twice")
    return jobs


def _side_reserve(job: Mapping[str, Any], side: str) -> float:
    return (job["max_cost"] or 0.0) if job["sides"][side]["client"] in matrix.PAID_CLIENTS else 0.0


def _per_file(job: Mapping[str, Any], amount: Mapping[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for side in arena.SIDES:
        for f in (job["budgets"]["own"], *job["budgets"][side]):
            out[f] = out.get(f, 0.0) + amount[side]
    return out


def reserves(job: Mapping[str, Any]) -> dict[str, float]:
    """{budget file: USD it holds back before the match}: each paid side's max_cost (0 for a subscription side) into
    the matrix's own file and that side's model files."""
    return _per_file(job, {s: _side_reserve(job, s) for s in arena.SIDES})


def charges(job: Mapping[str, Any], costs: Mapping[str, float | None]) -> dict[str, float]:
    """{budget file: USD} the match's two side costs ({"A", "B"}) add as it ends; a subscription side is never
    charged, and a paid side with no cost raises (the budget can't be enforced past it)."""
    amount = {}
    for side in arena.SIDES:
        paid = job["sides"][side]["client"] in matrix.PAID_CLIENTS
        if paid and costs.get(side) is None:
            raise ValueError(f"match {job['id']}: side {side}'s cost is unknown")
        amount[side] = float(costs[side] or 0.0) if paid else 0.0
    return _per_file(job, amount)


def over_budget(job: Mapping[str, Any], budgets: Mapping[str, Mapping[str, float]]) -> list[str]:
    """The budget files (read as {"cap", "spent"}) the match's reserves don't fit in; [] = it may start."""
    return sorted(f for f, r in reserves(job).items() if budgets[f]["spent"] + r > budgets[f]["cap"])


def open_queue(root: str | Path, budget: str | Path, now: Callable[[], float]) -> matrix.Matrix:
    """The matrix's queue (bench.matrix's file and fold): .create(name, jobs) writes the plan or keeps the same one,
    .state() folds the attempts. Its run() is bench's single-cost loop, not the arena's: the harness plays matches."""

    def no_runner(job: dict[str, Any]) -> matrix.RunResult:
        raise NotImplementedError("arena matches are played by the harness's arena runner")

    return matrix.Matrix(root, no_runner, budget, now=now, sleep=lambda s: None)


def load(path: str | Path, repo: Path) -> dict[str, Any]:
    """The matrix file -> {"name", "path", "jobs", "budget", "arenas" (name -> arena), "skipped" (name -> why)};
    checks the profiles, the arenas and the budget."""
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise MatrixFileError(f"{path}: {e}") from None
    if unknown := sorted(set(raw) - {"name", "models", "pairs", "arenas", "repeats", "allow_unverified", "budget"}):
        raise MatrixFileError(f"{path}: unknown keys: {', '.join(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not name or name != matrix._slug(name):
        raise MatrixFileError(f"{path}: name must be a non-empty slug")
    repeats = raw.get("repeats", 1)
    if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1:
        raise MatrixFileError(f"{path}: repeats must be an integer >= 1")
    names: list[str] = raw.get("arenas") or []
    if not _strs(names) or len(set(names)) != len(names):
        raise MatrixFileError(f"{path}: arenas must list arena names once each")
    try:
        arenas = {a: arena.load(repo / ARENAS / f"{a}.json") for a in names}
    except arena.ArenaError as e:
        raise MatrixFileError(f"{path}: {e}") from None
    skipped = {a: ar["draft"] for a, ar in arenas.items() if "draft" in ar}
    pairs = _pairs(raw, path)
    profiles = sorted({p for pair in pairs for p in pair})
    budget = _budget(raw, path, names, profiles)
    models = {p: {**load_profile(repo / p, raw.get("allow_unverified") is True), "profile": p} for p in profiles}
    if len({player(m) for m in models.values()}) != len(models):
        raise MatrixFileError(f"{path}: two profiles play the same client and model")
    playable = [a for a in names if a not in skipped]
    jobs = plan(
        [(models[a], models[b]) for a, b in pairs],
        playable,
        repeats,
        max_cost=budget["max_cost"],
        budgets=budget["models"],
        own=budget["file"],
        condition={a: arenas[a]["condition"] for a in playable},
    )
    for j in jobs:
        for side in arena.SIDES:
            if j["sides"][side]["client"] in matrix.PAID_CLIENTS and j["max_cost"] is None:
                raise MatrixFileError(f"{path}: arena {j['arena']} has no budget.max_cost for a paid side")
    return {"name": name, "path": str(path), "jobs": jobs, "budget": budget, "arenas": arenas, "skipped": skipped}


def print_plan(m: Mapping[str, Any], out: TextIO) -> None:
    out.write(f"arena matrix {m['name']}: {len(m['jobs'])} matches, {len(m['jobs']) // 2} pairings\n")
    out.writelines(f"  skipped {a}: {why}\n" for a, why in sorted(m["skipped"].items()))
    for j in m["jobs"]:
        res = ", ".join(f"{f} ${r:.2f}" for f, r in sorted(reserves(j).items()))
        out.write(f"  {j['id']}  A={j['sides']['A']['player']}  B={j['sides']['B']['player']}  reserve {res}\n")


def main(argv: Sequence[str] | None = None, repo: Path | None = None, out: TextIO = sys.stdout) -> int:
    ap = argparse.ArgumentParser(prog="python -m bg3bench.arena_matrix", description="Arena pairing matrices.")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="print the matches and their reserves; writes nothing").add_argument(
        "--matrix", required=True
    )
    args = ap.parse_args(argv)
    repo = repo or Path.cwd()
    try:
        m = load(repo / args.matrix, repo)
    except MatrixFileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print_plan(m, out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
