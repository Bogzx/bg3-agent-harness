"""The matrix's run_one on bg3harness.session.run_session: one job = one more run of a series, no resume.

The bench builds the run: the series (`series_name`: one per scenario, model, client, effort and condition), the run
number (one after the series' last runs.jsonl record), the run directory (runtime/sessions/<series.run_name>/), the
SessionSpec (the host's kickoff, the scenario's run_hours, Recovery(max_resumes=0)) and, at the end, the record, its
runs.jsonl line (its pins carry the harness, bench and suite versions: pins.versions, the host's harness version
first) and write_run_artifacts' manifest.json and result.json. The harness side comes in through RunHost:
`start` puts the game at the scenario's start and returns the kickoff and the session hooks (the agent process, the
game, the clock), `record` returns the run's record fields once the session is over (facts.jsonl closed). The
harness never imports the bench: the scenario reaches the host as the v2 dict (scenario.load: with defaults).

A fault (game crash or hang, agent hang) ends the session at once (run_session's `invalid`): the run counts one more
harness failure, so results.validity makes it invalid and the matrix reruns the job. A host whose `start` fails ran
no agent: an invalid attempt that cost nothing. series.json freezes the scenario from the series' first run; a later
run of an edited scenario is refused.

Budget: a paid client's (matrix.PAID_CLIENTS) cost is settled by the matrix (`charged` False), so the host must not
add it to runtime/budget.json itself; a subscription run isn't billed (`charged`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from bg3harness.session import Recovery, SessionHooks, SessionOutcome, SessionSpec, run_session

from bg3bench import pins, run_artifacts, scenario, series
from bg3bench.matrix import PAID_CLIENTS, RunResult

NO_RESUME = Recovery(max_resumes=0)
JSON = dict[str, Any]


def series_name(matrix: str, job: Mapping[str, Any]) -> str:
    parts: list[str] = [matrix, job["scenario"], job["model"], job["client"], job["effort"] or "", job["condition"]]
    return re.sub(r"[^a-z0-9_-]+", "-", "-".join(p for p in parts if p).lower()).strip("-")[:100]


@dataclass(frozen=True)
class Start:
    prompt: str  # the kickoff
    hooks: SessionHooks
    client_session: str | None = None
    prompt_sha: str | None = None  # sha256 of the assembled prompt
    prompt_parts: Sequence[JSON] = field(default_factory=tuple)  # [{"file", "sha256"}]
    harness_version: str | None = None


class RunHost(Protocol):
    def start(self, job: JSON, sc: JSON, run_dir: Path) -> Start: ...  # the scenario's start state, the kickoff
    def record(self, job: JSON, sc: JSON, run_dir: Path, outcome: SessionOutcome) -> JSON: ...  # runs.jsonl fields


class SessionRun:
    """run_one for matrix.Matrix over run_session. scenarios maps each scenario name to its v2 dict."""

    def __init__(self, matrix: str, host: RunHost, *, scenarios: Mapping[str, JSON], sessions: str | Path):
        self.matrix, self.host, self.scenarios, self.sessions = matrix, host, dict(scenarios), Path(sessions)

    def _series(self, name: str, job: JSON, sc: JSON) -> JSON:
        path = self.sessions / name / "series.json"
        want: JSON = {"name": sc["name"], "digest": scenario.digest(sc), "start_save_sha1": None, "spec": sc}
        if path.exists():
            meta: JSON = json.loads(path.read_text(encoding="utf-8"))
            frozen: JSON = meta.get("scenario") or {}
            if frozen.get("digest") != want["digest"]:
                raise ValueError(f"series {name}: scenario {sc['name']} changed since its first run")
            return meta
        meta = {"series": name, "client": job["client"], "scenario": want, "caps": dict(sc["caps"])}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        return meta

    def __call__(self, job: JSON) -> RunResult:
        if job["scenario"] not in self.scenarios:
            raise ValueError(f"no scenario {job['scenario']!r}")
        sc = self.scenarios[job["scenario"]]
        name = series_name(self.matrix, job)
        meta = self._series(name, job, sc)
        runs = self.sessions / name / "runs.jsonl"
        k = series.next_run_index(series.series_records(runs, name))
        run_dir = self.sessions / series.run_name(name, k)
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            st = self.host.start(job, sc, run_dir)
        except Exception as e:  # noqa: BLE001 - no agent ran: invalid, nothing spent
            return RunResult(None, False, False, 0.0, f"start failed: {type(e).__name__}: {e}", True)
        spec = SessionSpec(run_dir.name, st.prompt, sc["caps"]["run_hours"] * 60, st.client_session, NO_RESUME)
        out = run_session(spec, st.hooks)
        rec: JSON = dict(self.host.record(job, sc, run_dir, out))
        rec.update(series=name, playthrough=run_dir.name, run=k, client=job["client"], model=job["model"])
        rec.setdefault("effort", job["effort"])
        rec.setdefault("started", out.started)
        versions = {**pins.versions(), **({"harness": st.harness_version} if st.harness_version else {})}
        rec["pins"] = {"versions": versions, **(rec.get("pins") or {})}
        if out.invalid:
            rec["harness_failures"] = int(rec.get("harness_failures") or 0) + 1
            rec["session_invalid"] = out.invalid
        with runs.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        _, res = run_artifacts.write_run_artifacts(
            run_dir,
            meta,
            rec,
            st.harness_version,
            st.prompt_sha,
            prompt_parts=st.prompt_parts,
            scenario_spec=sc,
            condition=job["condition"],
        )
        why = res["invalid_reason"]
        if why and out.invalid:
            why += f": {out.invalid} (no resume)"
        charged = job["client"] not in PAID_CLIENTS
        return RunResult(res["run_id"], res["valid"], res["passed"], res["cost_usd"], why, charged)
