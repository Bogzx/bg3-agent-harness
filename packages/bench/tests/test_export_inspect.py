import pytest

pytest.importorskip("inspect_ai")

from inspect_ai.log import read_eval_log
from test_manifest_results import naut2_pairs

from bg3bench import export_inspect, results, suite

TAG = suite.suite_version()  # the installed bench's: a release holds runs of its own suite


def release_and_results():
    (m1, r1), (m2, r2), (m3, r3) = naut2_pairs()
    other = {**m3, "run_id": "x-r001", "model": {**m3["model"], "slug": "openrouter/x/y"}, "condition": "rules+c@1a2b"}
    pairs = [
        (m1, r1),
        (m2, {**r2, "valid": False, "invalid_reason": "operator"}),
        (m3, r3),
        (other, {**r3, "run_id": "x-r001", "passed": False, "t_pass_s": None}),
    ]
    release = results.build_results(pairs, seed=0, created=1792300000.0, bench_tag=TAG, b=200)
    return release, {r["run_id"]: r for _, r in pairs}


def test_eval_logs_round_trip(tmp_path):
    release, per_run = release_and_results()
    logs = export_inspect.to_evallogs(release, goals={"nautiloid": "nautiloid_escaped"}, run_results=per_run)
    paths = export_inspect.write(logs, tmp_path / "logs")
    assert [p.name for p in paths] == [
        "bg3-bench_openrouter_x_y_pi_rules_c_1a2b.eval",
        "bg3-bench_opus_pi_default.eval",
    ]
    back = read_eval_log(str(paths[1]))
    assert back.status == "success" and back.eval.model == "opus" and back.eval.task == "bg3-bench"
    assert back.eval.metadata == {"bench_tag": TAG, "seed": 0, "n_runs": 3, "n_invalid": 1}
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
    assert other.eval.task_args == {"condition": "rules+c@1a2b", "client": "pi"}
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


def test_a_maximise_run_exports_its_gain_not_a_failed_goal():
    (m1, r1), _, _ = naut2_pairs()
    score = {"fact": "gold", "start": 133, "end": 650, "gain": 517, "peak": 900}
    unread = {**score, "start": None, "end": None, "gain": None, "peak": None}
    m2 = {**m1, "run_id": "g2"}
    pairs = [(m1, {**r1, "passed": None, "t_pass_s": None, "score": score}),
             (m2, {**r1, "run_id": "g2", "passed": None, "t_pass_s": None, "score": unread})]  # fmt: skip
    release = results.build_results(pairs, seed=0, created=1792300000.0, b=10)
    (log,) = export_inspect.to_evallogs(release)
    gain = log.samples[0].scores["gain"]
    assert (gain.value, gain.answer, gain.metadata) == (517, "gold", score) and "goal" not in log.samples[0].scores
    assert log.samples[1].scores == {} and log.results.scores[0].metrics == {}  # no pass: no accuracy


def test_one_model_through_two_clients_is_two_logs(tmp_path):
    release, _ = release_and_results()
    twin = [{**g, "client": "codex"} for g in release["groups"] if g["model"] == "opus"]
    release["groups"] += twin
    release["runs"] += [
        {**r, "client": "codex", "run_id": r["run_id"] + "-c"} for r in release["runs"] if r["model"] == "opus"
    ]
    paths = export_inspect.write(export_inspect.to_evallogs(release), tmp_path)
    assert sorted(p.name for p in paths if "_opus_" in p.name) == [
        "bg3-bench_opus_codex_default.eval",
        "bg3-bench_opus_pi_default.eval",
    ]
