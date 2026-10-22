# pyright: basic
"""Per-run results (schema/result.v1.schema.json) and the release file (schema/results.v1.schema.json).

`from_record` turns one runs.jsonl record (runner/runs.py) into a result, with the grader output (graders.grade)
and the method attribution (methods.attribute) when the run has a facts.jsonl. Without a grade, the runner's own
milestone verdict is used (`outcome == "reached"`), which is how series before facts.jsonl were scored.

Validity (docs/BENCHMARK-PLAN.md): a run is invalid, rerun and never scored when the runner itself failed
(`runner_error`), a harness failure was classified (runs.py `harness_failures`), an operator acted on it, or an
endpoint served it outside its profile's provider pin (`off_route`: the record's off_route, set by the session
runner from bg3harness.profiles.off_route).

Thinking time (`think_s`) is the model's own time from the record's `model_time.model_s` (bg3harness/model_time.py);
realtime scenarios (D7) report it next to wall time, since the world runs while the model thinks. `luck` is
luck.summary over the run's roll log (D9), null when the run has none. A choice scenario's result has `choices`
(choices.grade: the picks, the persona fit), and its release run the picks and the fit score (choices.table).

`build_results` aggregates (manifest, result) pairs per (model, condition) with stats.summarize over valid runs. A
release is of one suite version (`suite`, bench-vX.Y: its runs' bench versions; its `bench_tag` when tagged), and
each group lists the harness versions its runs played with.
"""

from __future__ import annotations

import json
import random
from collections.abc import Collection, Iterable, Sequence
from pathlib import Path
from statistics import median

from bg3harness.judge import same_model

from bg3bench import choices as choices_mod
from bg3bench import stats, suite

SCHEMA = 1
OUTCOMES = ("reached", "timeout", "over_budget", "gave_up", "runner_error")


def validity(record: dict) -> tuple[bool, str | None]:
    if record["outcome"] == "runner_error":
        return False, "runner_error"
    if record.get("harness_failures"):
        return False, "harness_failure"
    if record.get("operator"):
        return False, "operator"
    if record.get("off_route"):
        return False, "off_route"
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
) -> dict:
    """One runs.jsonl record (+ grader output, + method attribution, + luck.summary, + the choices grader's
    picks and fit for a choice scenario, + what a pilot run didn't play of its condition) -> result v1."""
    if record["outcome"] not in OUTCOMES:
        raise ValueError(f"unknown outcome {record['outcome']!r}")
    valid, reason = validity(record)
    if grade is None:
        passed, t_pass = record["outcome"] == "reached", record.get("time_to_milestone_s")
    else:
        passed, t_pass = grade["passed"], grade["t_pass_s"]
    tokens = record.get("tokens") or {}
    deaths = record.get("deaths") or {}
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
        "luck": luck,
        "reloads": int(record.get("reloads") or 0),
        "deaths": {k: int(deaths.get(k) or 0) for k in ("fights", "downs", "deaths", "wipes")},
        "fairness": {k: int(fair.get(k) or 0) for k in ("leak", "check", "blocks")},
    }
    if choices is not None:
        out["choices"] = choices
    if pilot:
        out["pilot"] = list(pilot)
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
        entirely (a published release never contains them; contamination.holdout_names lists them). A release whose
    judge (manifest `judge`) is the same model or family as a contestant is refused: the judge never scores itself.
    So is one whose runs are of more than one suite version, or of another suite than its `bench_tag`."""
    runs, groups, pairs_kept = [], {}, []
    for man, res in pairs:
        if man["run_id"] != res["run_id"]:
            raise ValueError(f"manifest {man['run_id']!r} paired with result {res['run_id']!r}")
        if man["scenario"]["name"] in holdout:
            continue
        pairs_kept.append((man, res))
        key = (man["model"]["slug"], man["condition"])
        groups.setdefault(key, []).append((man["scenario"]["name"], res))
        runs.append(
            {
                "run_id": res["run_id"],
                "versions": {**man["versions"], "suite": suite.suite_version(man["versions"]["bench"])},
                "scenario": man["scenario"]["name"],
                "model": key[0],
                "condition": key[1],
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
            }
        )
    if len({r["run_id"] for r in runs}) != len(runs):
        raise ValueError("a run_id appears twice")
    suites = sorted({r["versions"]["suite"] for r in runs})
    if len(suites) > 1:
        raise ValueError(f"runs of {len(suites)} suite versions ({', '.join(suites)}): scores don't compare")
    if bench_tag is not None and suites and suites != [bench_tag]:
        raise ValueError(f"release {bench_tag} holds runs of {suites[0]}")
    contestants = {model for model, _ in groups}
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
    for (model, condition), members in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        valid = [(s, r) for s, r in members if r["valid"]]
        by_scenario: dict[str, list[bool]] = {}
        for s, r in valid:
            by_scenario.setdefault(s, []).append(r["passed"])
        summary = stats.summarize(by_scenario, random.Random(seed), seed=seed, b=b) if valid else None
        walls = [r["wall_s"] for _, r in valid if r["wall_s"] is not None]
        thinks = [r["think_s"] for _, r in valid if r["think_s"] is not None]
        out_groups.append(
            {
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
    return {
        "schema": SCHEMA,
        "bench_tag": bench_tag,
        "suite": suites[0] if suites else None,
        "created": created,
        "seed": seed,
        "b": b,
        "runs": runs,
        "groups": out_groups,
    }


def write(data: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a v{SCHEMA} result file (schema={data.get('schema')!r})")
    return data
