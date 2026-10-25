"""Inspect AI export: a release (results.json v1) -> one `.eval` log per (model, condition).

Optional: needs the `inspect` extra (`uv sync --extra inspect`, inspect-ai pinned in pyproject.toml). Our JSON stays
canonical; the `.eval` files are a view of it for Inspect's log viewer (`inspect view bundle --log-dir <dir>
--output-dir <www>` builds a static site; it needs a server with HTTP range requests).

Mapping (research/arch/bench.md 2.4): task `bg3-bench`, the condition in `task_args`; one sample per valid run with
`id` = scenario and `epoch` = the run's 1-based index among that scenario's valid runs (in results order), `target` =
the scenario's goal text; score `goal` is 1.0/0.0; `model_usage` carries the billed cost and tokens when the per-run
result is given. Invalid runs are not scored, so they are not samples; their count is in the log metadata.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalLog,
    EvalMetric,
    EvalResults,
    EvalSample,
    EvalScore,
    EvalSpec,
    EvalStats,
    write_eval_log,
)
from inspect_ai.model import ModelUsage
from inspect_ai.scorer import Score

TASK = "bg3-bench"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def _usage(result: Mapping | None) -> ModelUsage | None:
    if result is None:
        return None
    tok = result["tokens"]
    return ModelUsage(
        input_tokens=tok["input"],
        output_tokens=tok["output"],
        total_tokens=sum(tok.values()),
        input_tokens_cache_read=tok["cache_read"],
        input_tokens_cache_write=tok["cache_write"],
        total_cost=result["cost_usd"],
    )


def _gain(score: Mapping) -> dict[str, Score]:
    """A maximise run's score (no pass): the fact's gain; none when the run read no value of it."""
    if score["gain"] is None:
        return {}
    return {"gain": Score(value=score["gain"], answer=score["fact"], metadata=dict(score))}


def _sample(run: Mapping, epoch: int, goals: Mapping[str, str], result: Mapping | None) -> EvalSample:
    usage = _usage(result)
    return EvalSample(
        id=run["scenario"],
        epoch=epoch,
        input=run["scenario"],
        target=goals.get(run["scenario"], ""),
        scores={
            "goal": Score(
                value=1.0 if run["passed"] else 0.0,
                answer="passed" if run["passed"] else "failed",
                metadata={"t_pass_s": run["t_pass_s"]},
            )
        }
        if run["passed"] is not None
        else _gain(run["score"]),
        metadata={"run_id": run["run_id"], "cost_usd": run["cost_usd"], "t_pass_s": run["t_pass_s"]},
        model_usage={} if usage is None else {run["model"]: usage},
        total_time=run["wall_s"],
    )


def to_evallogs(
    release: Mapping,
    *,
    goals: Mapping[str, str] | None = None,
    run_results: Mapping[str, Mapping] | None = None,
) -> list[EvalLog]:
    """results.json v1 (+ goal text per scenario, + result v1 per run_id) -> one EvalLog per group."""
    goals = goals or {}
    run_results = run_results or {}
    created = _iso(release["created"])
    logs = []
    for group in release["groups"]:
        key = (group["model"], group["condition"])
        runs = [r for r in release["runs"] if (r["model"], r["condition"]) == key and r["valid"]]
        seen: dict[str, int] = {}
        samples = []
        for run in runs:
            seen[run["scenario"]] = seen.get(run["scenario"], 0) + 1
            samples.append(_sample(run, seen[run["scenario"]], goals, run_results.get(run["run_id"])))
        metrics = {}
        if group["stats"] is not None:
            agg = group["stats"]["aggregate"]
            metrics = {
                "accuracy": EvalMetric(name="accuracy", value=agg["mean_rate"]),
                "ci_lo": EvalMetric(name="ci_lo", value=agg["ci"][0]),
                "ci_hi": EvalMetric(name="ci_hi", value=agg["ci"][1]),
            }
        logs.append(
            EvalLog(
                status="success",
                eval=EvalSpec(
                    created=created,
                    task=TASK,
                    task_args={"condition": group["condition"]},
                    dataset=EvalDataset(name=TASK, samples=len(seen)),
                    model=group["model"],
                    config=EvalConfig(epochs=max(seen.values(), default=1)),
                    metadata={
                        "bench_tag": release["bench_tag"],
                        "seed": release["seed"],
                        "n_runs": group["n_runs"],
                        "n_invalid": group["n_invalid"],
                    },
                ),
                results=EvalResults(
                    total_samples=len(samples),
                    completed_samples=len(samples),
                    scores=[EvalScore(name="goal", scorer="bg3bench.graders", metrics=metrics)],
                ),
                stats=EvalStats(started_at=created, completed_at=created),
                samples=samples,
            )
        )
    return logs


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-." else "_" for c in text)


def log_name(log: EvalLog) -> str:
    cond = log.eval.task_args.get("condition") or "default"
    return f"{TASK}_{_slug(log.eval.model)}_{_slug(cond)}.eval"


def write(logs: list[EvalLog], out_dir: str | Path) -> list[Path]:
    """Write each log as `<out_dir>/<log_name>`; returns the paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for log in logs:
        path = out / log_name(log)
        write_eval_log(log, str(path))
        paths.append(path)
    return paths
