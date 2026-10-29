# pyright: basic
"""Per-run results (schema/result.v1.schema.json) and the release file (schema/results.v1.schema.json).

`from_record` turns one runs.jsonl record (bg3harness.runs) into a result, with the grader output (graders.grade)
and the method attribution (methods.attribute) when the run has a facts.jsonl. Without a grade, the runner's own
milestone verdict is used (`outcome == "reached"`), which is how series before facts.jsonl were scored.

Validity (docs/BENCHMARK-PLAN.md): a run is invalid, rerun and never scored when the runner itself failed
(`runner_error`), its game loaded a save that isn't its start save or its own (`foreign_save`: the record's, set by
bg3harness.runner.run_host.SaveWatch), a harness failure was classified (runs.py `harness_failures`), an operator acted on it, an
endpoint served it outside its profile's provider pin (`off_route`: the record's off_route, set by the session
runner from bg3harness.profiles.off_route), or its scenario fixes a dice seed the game didn't apply
(`unseeded`: the record's settings readout, game_settings.seed, has no applied seed or another one; live7 played its
nautiloid unseeded).

Thinking time (`think_s`) is the model's own time from the record's `model_time.model_s` (bg3harness/model_time.py);
realtime scenarios (D7) report it next to wall time, since the world runs while the model thinks. `luck` is
luck.summary over the run's roll log (D9), null when the run has none. A choice scenario's result has `choices`
(choices.grade: the picks, the persona fit), and its release run the picks and the fit score (choices.table).

A maximise scenario's result (scenario.py `maximise`) has no pass: `passed` and `t_pass_s` are null and `score`
is graders.measure's {fact, start, end, gain, peak}, the gain being the score. Its runs are left out of every pass
rate; a release run carries the score.

`build_results` aggregates (manifest, result) pairs per (client, model, condition) with stats.summarize over valid
runs: a leaderboard row is a model played through one client (the manifest's `client.name`; null for a human), so
one model through two clients is two rows. A Claude Code run (PILOT_CLIENTS: a subscription CLI, played as a pilot)
never enters a release. Each condition's rows are ranked (`ranking`): a model is ranked when it has valid runs in at least RANKED_SHARE of
the condition's pass/fail scenarios (no choice, no maximise: 6 of 8); the suite score is the mean pass rate over the
common set, the pass/fail scenarios every ranked model has a valid run of; its rank range and interval come from
stats.rank_ranges (seeded). An unranked model's suite score and interval (stats.bootstrap_mean) are over its own
cells of that set, with no rank. A
release is of one suite version (`suite`, bench-vX.Y: its runs' bench versions; its `bench_tag` when tagged), and
each group lists the harness versions its runs played with. The runs of one cell (scenario, model, condition) cite
the same game pins (game_pins: the game build, the bg3le build, the mod), or the release is refused: a run's
version_drift is a flag, but a cell's rate never mixes game setups.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Collection, Iterable, Sequence
from pathlib import Path
from statistics import median

from bg3harness.judge import same_model

from bg3bench import choices as choices_mod
from bg3bench import stats, suite

SCHEMA = 1
# failed: a run that its live verdict ended on a fired never or a party wipe (a maximise run's too: its score stands)
OUTCOMES = ("reached", "failed", "timeout", "over_budget", "gave_up", "runner_error")
RANKED_SHARE = 2 / 3  # a model is ranked with valid runs in at least this share of its condition's pass/fail scenarios
PILOT_CLIENTS = frozenset({"claude"})  # their runs are pilots: build_results leaves them out of a release


def applied_seed(record: dict) -> int | None:
    """The dice seed the game applied (the readout's game_settings.seed.applied.seed, from the mod's scn.seed), None
    when the run wasn't seeded or the seeding failed."""
    entry = (record.get("game_settings") or {}).get("seed") or {}
    return (entry.get("applied") or {}).get("seed") if isinstance(entry, dict) else None


def validity(record: dict, seed: int | None = None) -> tuple[bool, str | None]:
    """(valid, reason) of a record; `seed`: the scenario's dice seed, None when it fixes none."""
    if record["outcome"] == "runner_error":
        return False, "runner_error"
    if record.get("foreign_save"):  # the game left the run's saves: it may have scored on another run's (bench B3)
        return False, "foreign_save"
    if record.get("harness_failures"):
        return False, "harness_failure"
    if record.get("operator"):
        return False, "operator"
    if record.get("off_route"):
        return False, "off_route"
    if seed is not None and applied_seed(record) != seed:
        return False, "unseeded"
    return True, None


def _minutes_to_s(m: float | None) -> float | None:
    return None if m is None else round(m * 60, 1)


def from_record(
    record: dict,
    run_id: str,
    grade: dict | None = None,
    methods: dict | None = None,
    luck: dict | None = None,
    choices: dict | None = None,
    pilot: Sequence[str] | None = None,
    seed: int | None = None,
    score: dict | None = None,
    losses: dict | None = None,
) -> dict:
    """One runs.jsonl record (+ grader output, + method attribution, + luck.summary, + the choices grader's
    picks and fit for a choice scenario, + what a pilot run didn't play of its condition, + a maximise scenario's
    score) -> result v1. `seed`: the scenario's dice seed (validity). `losses`: the run's facts.party_losses, its
    `deaths` (else the record's: a bench record has none, diverse-2026-10-25 wrote 0 for 11 wiped runs)."""
    if record["outcome"] not in OUTCOMES:
        raise ValueError(f"unknown outcome {record['outcome']!r}")
    valid, reason = validity(record, seed)
    if grade is None:
        passed, t_pass = record["outcome"] == "reached", record.get("time_to_milestone_s")
    else:
        passed, t_pass = grade["passed"], grade["t_pass_s"]
    tokens = record.get("tokens") or {}
    deaths = (record.get("deaths") if losses is None else losses) or {}
    fair = record.get("fairness_flags") or {}
    out = {
        "schema": SCHEMA,
        "run_id": run_id,
        "outcome": record["outcome"],
        "valid": valid,
        "invalid_reason": reason,
        "passed": passed,
        "t_pass_s": t_pass if passed else None,
        "grade": None if grade is None else {k: grade[k] for k in ("violations", "curve", "progress")},
        "methods": methods,
        "cost_usd": record.get("cost_usd"),
        "tokens": {k: int(tokens.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write")},
        "wall_s": _minutes_to_s(record.get("wall_min")),
        "play_s": _minutes_to_s(record.get("play_min")),
        "think_s": (record.get("model_time") or {}).get("model_s"),
        "first_stop_s": record.get("first_stop_s"),  # the agent's first end of turn (runs.jsonl), None: never or older
        "luck": luck,
        "reloads": int(record.get("reloads") or 0),
        "deaths": {k: int(deaths.get(k) or 0) for k in ("fights", "downs", "deaths", "wipes")},
        "fairness": {k: int(fair.get(k) or 0) for k in ("leak", "check", "blocks")},
    }
    if choices is not None:
        out["choices"] = choices
    if score is not None:
        out["score"] = score
    if pilot:
        out["pilot"] = list(pilot)
    return out


def game_pins(manifest: dict) -> dict:
    """The game setup a run's manifest pins: the game build, the bg3le build's sha256 and the mod's sha1."""
    p = manifest.get("pins") or {}
    return {
        "game_build": p.get("game_build"),
        "bg3le_sha256": (p.get("bg3le") or {}).get("sha256"),
        "mod_sha1": (p.get("mod") or {}).get("sha1"),
    }


def client_of(manifest: dict) -> str | None:
    """The client a run played through (manifest `client.name`); None for a human's run."""
    return (manifest.get("client") or {}).get("name")


def _row(client: str | None, model: str) -> str:
    """A leaderboard row's key inside the build: the model through one client."""
    return f"{client}/{model}" if client else model


def _mixed_pins(pairs: Iterable[tuple[dict, dict]]) -> list[str]:
    """The cells (scenario/client model/condition) whose runs cite different game pins, with the pins that differ."""
    cells: dict[str, list[dict]] = {}
    for man, _ in pairs:
        row = _row(client_of(man), man["model"]["slug"])
        cells.setdefault(f"{man['scenario']['name']}/{row}/{man['condition']}", []).append(game_pins(man))
    out = []
    for cell, pins in sorted(cells.items()):
        if differ := sorted(k for k in pins[0] if len({p[k] for p in pins}) > 1):
            out.append(f"{cell} ({', '.join(differ)})")
    return out


def _costs(results: list[dict]) -> dict:
    costs = [r["cost_usd"] for r in results]
    if not costs or any(c is None for c in costs):
        return {"median_per_run": None, "per_success": None}
    wins = sum(1 for r in results if r["passed"])
    return {"median_per_run": median(costs), "per_success": sum(costs) / wins if wins else None}


def build_results(
    pairs: Iterable[tuple[dict, dict]],
    *,
    seed: int,
    created: float,
    bench_tag: str | None = None,
    b: int = 10000,
    holdout: Collection[str] = (),
) -> dict:
    """(manifest, result) pairs -> results v1. Each group's bootstrap gets a fresh random.Random(seed), so its
        numbers do not depend on which other groups are in the release. Runs of a `holdout` scenario are left out
        entirely (a published release never contains them; contamination.holdout_names lists them), and so are the
    runs of a PILOT_CLIENTS client. A release whose
    judge (manifest `judge`) is the same model or family as a contestant is refused: the judge never scores itself.
    So is one whose runs are of more than one suite version, or of another suite than its `bench_tag`, or one with a
    cell whose runs cite different game pins."""
    runs, groups, pairs_kept = [], {}, []
    for man, res in pairs:
        if man["run_id"] != res["run_id"]:
            raise ValueError(f"manifest {man['run_id']!r} paired with result {res['run_id']!r}")
        if man["scenario"]["name"] in holdout or client_of(man) in PILOT_CLIENTS:
            continue
        pairs_kept.append((man, res))
        key = (client_of(man), man["model"]["slug"], man["condition"])
        groups.setdefault(key, []).append((man["scenario"]["name"], res))
        runs.append(
            {
                "run_id": res["run_id"],
                "versions": {**man["versions"], "suite": suite.suite_version(man["versions"]["bench"])},
                "scenario": man["scenario"]["name"],
                "client": key[0],
                "model": key[1],
                "condition": key[2],
                "valid": res["valid"],
                "passed": res["passed"],
                "t_pass_s": res["t_pass_s"],
                "cost_usd": res["cost_usd"],
                "wall_s": res["wall_s"],
                "think_s": res["think_s"],
                "timing": man["scenario"]["timing"],
                "luck_z": None if res["luck"] is None else res["luck"]["z"],
                "tokens": res["tokens"],
                **({"choices": choices_mod.release_entry(res["choices"])} if res.get("choices") else {}),
                **({"score": res["score"]} if res.get("score") else {}),
            }
        )
    if len({r["run_id"] for r in runs}) != len(runs):
        raise ValueError("a run_id appears twice")
    suites = sorted({r["versions"]["suite"] for r in runs})
    if len(suites) > 1:
        raise ValueError(f"runs of {len(suites)} suite versions ({', '.join(suites)}): scores don't compare")
    if bench_tag is not None and suites and suites != [bench_tag]:
        raise ValueError(f"release {bench_tag} holds runs of {suites[0]}")
    if mixed := _mixed_pins(pairs_kept):
        raise ValueError(f"runs of one cell with different game pins: {'; '.join(mixed)}")
    contestants = {model for _, model, _ in groups}
    for man, _ in pairs_kept:
        judge = (man.get("judge") or {}).get("model")
        clash = sorted(m for m in contestants if same_model(judge, m))
        if clash:
            raise ValueError(
                f"run {man['run_id']}: its judge {judge} is a contestant ({', '.join(clash)}): "
                "pick a judge outside the comparison"
            )
    harness_of = {r["run_id"]: r["versions"]["harness"] for r in runs}
    out_groups = []
    for (client, model, condition), members in sorted(
        groups.items(), key=lambda kv: (kv[0][1], kv[0][0] or "", kv[0][2] or "")
    ):
        valid = [(s, r) for s, r in members if r["valid"]]
        by_scenario: dict[str, list[bool]] = {}
        for s, r in valid:
            if r["passed"] is not None:  # a maximise run has no pass
                by_scenario.setdefault(s, []).append(r["passed"])
        summary = stats.summarize(by_scenario, random.Random(seed), seed=seed, b=b) if by_scenario else None
        walls = [r["wall_s"] for _, r in valid if r["wall_s"] is not None]
        thinks = [r["think_s"] for _, r in valid if r["think_s"] is not None]
        out_groups.append(
            {
                "client": client,
                "model": model,
                "condition": condition,
                "n_runs": len(members),
                "n_invalid": len(members) - len(valid),
                "invalid_rate": (len(members) - len(valid)) / len(members),
                "stats": summary,
                "cost_usd": _costs([r for _, r in valid]),
                "wall_s": {"median": median(walls) if walls else None},
                "think_s": {"median": median(thinks) if thinks else None},
                "harness": sorted({v for _, r in members if (v := harness_of[r["run_id"]]) is not None}),
            }
        )
    conditions = sorted({c for _, _, c in groups}, key=lambda c: c or "")
    ranking = [_rank(c, out_groups, groups, seed, b) for c in conditions]
    return {
        "schema": SCHEMA,
        "bench_tag": bench_tag,
        "suite": suites[0] if suites else None,
        "created": created,
        "seed": seed,
        "b": b,
        "runs": runs,
        "groups": out_groups,
        "ranking": ranking,
    }


def _rank(
    condition: str | None, out_groups: list[dict], groups: dict[tuple, list[tuple[str, dict]]], seed: int, b: int
) -> dict:
    """One condition's ranking; sets each of its groups' suite_score, suite_score_ci, suite_n, rank_range, ranked."""
    members = {_row(cl, m): rs for (cl, m, c), rs in groups.items() if c == condition}
    passfail = {s for rs in members.values() for s, r in rs if r["passed"] is not None and not r.get("choices")}
    need = math.ceil(RANKED_SHARE * len(passfail) - 1e-9)
    scored: dict[str, dict[str, list[float]]] = {}
    for m, rs in members.items():
        scored[m] = {}
        for s, r in rs:
            if r["valid"] and s in passfail:
                scored[m].setdefault(s, []).append(1.0 if r["passed"] else 0.0)
    ranked = sorted(m for m in members if len(scored[m]) >= need)
    common = set.intersection(*(set(scored[m]) for m in ranked)) if ranked else set()
    ranked = ranked if common else []
    ranges = stats.rank_ranges({m: {s: scored[m][s] for s in common} for m in ranked}, random.Random(seed), b)
    for g in out_groups:
        if g["condition"] != condition:
            continue
        row = _row(g["client"], g["model"])
        cells = {s: scored[row][s] for s in sorted(common & set(scored[row]))}
        rr = ranges.get(row)
        if rr is None and cells:  # unranked: the same two-level bootstrap over its own cells of the common set
            _, lo, hi = stats.bootstrap_mean(cells, random.Random(seed), b)
            rr = {"rank": None, "ci": (lo, hi)}
        g.update(
            suite_score=sum(sum(v) / len(v) for v in cells.values()) / len(cells) if cells else None,
            suite_score_ci=list(rr["ci"]) if rr else None,
            suite_n=len(cells),
            rank_range=list(rr["rank"]) if rr and rr["rank"] else None,
            ranked=row in ranges,
        )
    mine_groups = [g for g in out_groups if g["condition"] == condition]
    order = sorted(
        mine_groups, key=lambda g: (not g["ranked"], -(g["suite_score"] or 0.0), g["model"], g["client"] or "")
    )
    return {
        "condition": condition,
        "scenarios": sorted(common),
        "of": len(passfail),
        "min_cells": need,
        "models": [{"client": g["client"], "model": g["model"]} for g in order],
    }


def write(data: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a v{SCHEMA} result file (schema={data.get('schema')!r})")
    return data
