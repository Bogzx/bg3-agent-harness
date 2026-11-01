# pyright: basic
"""bg3bench.accepted: one run per matrix job, the attempt its queue accepted. launch-v1-deepseek-rest act3_gold_max
r1's attempt 3 (-r003) ended invalid (harness_failure) and the matrix ran the job again; a re-classification that
said valid on disk must not let it count next to its rerun."""

from __future__ import annotations

import json
from pathlib import Path

from test_submission_pack import run_dir

from bg3bench import accepted
from bg3bench.submission import pack

SERIES = "launch-v1-act3_gold_max-deepseek-flash-pi-medium-rules"
JOB = "deepseek-flash~act3_gold_max~rules~r1"


def queue(matrices: Path, *attempts: tuple[str | None, bool]) -> None:
    """Matrix m's queue: JOB's attempts in order, each (run_id, valid); run_id None: closed with no run."""
    d = matrices / "m"
    d.mkdir(parents=True)
    lines = [{"type": "plan", "schema": 1, "name": "m", "jobs": [{"id": JOB}]}]
    for n, (run_id, valid) in enumerate(attempts, 1):
        lines.append({"type": "start", "job": JOB, "attempt": n, "t": n})
        lines.append({"type": "end", "job": JOB, "attempt": n, "t": n, "result": {"run_id": run_id, "valid": valid}})
    (d / "queue.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))


def a_run(tmp_path: Path, name: str, run_id: str, series: str = SERIES) -> Path:
    """The fixture run as this run of the series, its result.json valid (as a re-classification would say)."""
    (tmp_path / name).mkdir()
    d = run_dir(tmp_path / name)
    man = json.loads((d / "manifest.json").read_text())
    (d / "manifest.json").write_text(json.dumps(man | {"run_id": run_id, "series": series}))
    res = json.loads((d / "result.json").read_text())
    (d / "result.json").write_text(json.dumps(res | {"run_id": run_id, "valid": True}))
    return d


def cli(d: Path, out: Path, matrices: Path) -> int:
    return pack.main([str(d), str(out), "--attempt", "1", "--matrices", str(matrices)])


def test_a_superseded_attempt_valid_on_disk_is_refused_and_the_accepted_one_packs(tmp_path, capsys):
    m = tmp_path / "matrix"
    queue(m, (f"{SERIES}-r001", False), (f"{SERIES}-r002", True))
    old, rerun = a_run(tmp_path, "a", f"{SERIES}-r001"), a_run(tmp_path, "b", f"{SERIES}-r002")
    assert accepted.accepted_run_ids(m / "m") == [f"{SERIES}-r002"]
    why = accepted.refusal(old, m)
    assert why == (
        f"{SERIES}-r001 is attempt 1 of matrix m job {JOB}, not the run its queue accepted ({SERIES}-r002): "
        "a superseded attempt never counts"
    )
    assert cli(old, tmp_path / "out-a", m) == 1 and why in capsys.readouterr().err
    assert not (tmp_path / "out-a").exists()
    assert accepted.refusal(rerun, m) is None and cli(rerun, tmp_path / "out-b", m) == 0


def test_a_rerun_supersedes_even_a_valid_attempt_and_an_unended_job_has_none(tmp_path):
    m = tmp_path / "matrix"
    queue(m, (f"{SERIES}-r001", True), (f"{SERIES}-r002", False))
    assert accepted.accepted_run_ids(m / "m") == []
    assert "(none)" in (accepted.refusal(a_run(tmp_path, "a", f"{SERIES}-r001"), m) or "")


def test_an_interrupted_attempt_the_queue_never_named_is_refused_by_its_series(tmp_path):
    m = tmp_path / "matrix"
    queue(m, (f"{SERIES}-r001", False), (None, False), (f"{SERIES}-r003", True))
    why = accepted.refusal(a_run(tmp_path, "a", f"{SERIES}-r002"), m)
    assert why == f"{SERIES}-r002 is a run of matrix m's series {SERIES} that its queue never named (interrupted)"


def test_a_run_outside_every_matrix_packs(tmp_path):
    m = tmp_path / "matrix"
    queue(m, (f"{SERIES}-r001", True))
    alone = a_run(tmp_path, "a", "play-act3_gold_max-r001", series="play-act3_gold_max")
    assert accepted.refusal(alone, m) is None
    assert accepted.refusal(alone, tmp_path / "no-matrices") is None
    assert cli(alone, tmp_path / "out", m) == 0
