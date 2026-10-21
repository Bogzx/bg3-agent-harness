import pytest

pytest.importorskip("inspect_ai")

from inspect_ai.log import read_eval_log
from test_manifest_results import naut2_pairs

from bg3bench import export_inspect, results


def release_and_results():
    (m1, r1), (m2, r2), (m3, r3) = naut2_pairs()
    other = {**m3, "run_id": "x-r001", "model": {**m3["model"], "slug": "openrouter/x/y"}, "condition": "rules+c@1a2b"}
    pairs = [
        (m1, r1),
        (m2, {**r2, "valid": False, "invalid_reason": "operator"}),
        (m3, r3),
        (other, {**r3, "run_id": "x-r001", "passed": False, "t_pass_s": None}),
    ]
    release = results.build_results(pairs, seed=0, created=1792300000.0, bench_tag="bench-v0.1", b=200)
    return release, {r["run_id"]: r for _, r in pairs}


def test_eval_logs_round_trip(tmp_path):
    release, per_run = release_and_results()
    logs = export_inspect.to_evallogs(release, goals={"nautiloid": "nautiloid_escaped"}, run_results=per_run)
    paths = export_inspect.write(logs, tmp_path / "logs")
    assert [p.name for p in paths] == [
        "bg3-bench_openrouter_x_y_rules_c_1a2b.eval",
        "bg3-bench_opus_default.eval",
    ]
    back = read_eval_log(str(paths[1]))
    assert back.status == "success" and back.eval.model == "opus" and back.eval.task == "bg3-bench"
    assert back.eval.metadata == {"bench_tag": "bench-v0.1", "seed": 0, "n_runs": 3, "n_invalid": 1}
    assert back.eval.config.epochs == 2 and back.eval.dataset.samples == 1
    # the invalid run (r002) is not a sample; epochs count the valid runs per scenario
    assert [(s.id, s.epoch, s.metadata["run_id"]) for s in back.samples] == [
        ("nautiloid", 1, "naut2-r001"),
        ("nautiloid", 2, "naut2-r003"),
    ]
    s1 = back.samples[0]
    assert s1.target == "nautiloid_escaped"
    assert s1.scores["goal"].value == 1.0 and s1.scores["goal"].metadata == {"t_pass_s": 545.9}
    usage = s1.model_usage["opus"]
    assert usage.total_cost == 3.349 and usage.input_tokens_cache_read == 4291143 and usage.output_tokens == 15172
    assert s1.total_time == 600.0
    (score,) = back.results.scores
    assert score.metrics["accuracy"].value == 1.0
    assert score.metrics["ci_lo"].value <= 1.0 == score.metrics["ci_hi"].value
    other = read_eval_log(str(paths[0]))
    assert other.eval.task_args == {"condition": "rules+c@1a2b"}
    assert other.samples[0].scores["goal"].value == 0.0 and other.samples[0].scores["goal"].answer == "failed"


def test_without_per_run_results_or_any_valid_run():
    release, _ = release_and_results()
    release["runs"] = [{**r, "valid": False} for r in release["runs"]]
    for g in release["groups"]:
        g["stats"] = None
    logs = export_inspect.to_evallogs(release)
    assert all(log.samples == [] and log.results.scores[0].metrics == {} for log in logs)
    assert logs[0].eval.config.epochs == 1
    release, _ = release_and_results()
    (log, _) = export_inspect.to_evallogs(release)
    assert log.samples[0].model_usage == {} and log.samples[0].target == ""
