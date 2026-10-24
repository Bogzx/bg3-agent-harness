"""The matrix's run_one on bg3harness.session.run_session: one job = one more run of a series, no resume.

The bench builds the run: the series (`series_name`: one per scenario, model, client, effort and condition), the run
number (one after the series' last runs.jsonl record), the run directory (runtime/sessions/<series.run_name>/), the
SessionSpec (the host's kickoff, the scenario's run_hours, Recovery(max_resumes=0)) and, at the end, the record, its
runs.jsonl line (its pins carry the harness, bench and suite versions: pins.versions, the host's harness version
first) and write_run_artifacts' manifest.json and result.json. The harness side comes in through RunHost:
`start` puts the game at the scenario's start and returns the kickoff and the session hooks (the agent process, the
game, the clock; the job it gets carries "tools_off", the tools its condition removes, and "prompt_condition", the
harness prompt condition it plays: conditions.Condition.prompt_condition, and for a goal scenario "verdict", a
callable the host polls while the run plays: live_verdict's "passed" or "failed" ends the run, so it never plays on
past its grade), `record` returns the run's
record fields once the session is over (facts.jsonl closed). The harness never imports the bench: the scenario
reaches the host as the v2 dict (scenario.load: with defaults).

A fault (game crash or hang, agent hang) ends the session at once (run_session's `invalid`): the run counts one more
harness failure, so results.validity makes it invalid and the matrix reruns the job. A host whose `start` fails ran
no agent: an invalid attempt that cost nothing, recorded in runs.jsonl as a runner_error (no artifacts), so the
rerun plays in a fresh run directory (the failed start may have left a facts.jsonl run_start there). A run whose
artifacts can't be written (a facts or roll log the bench refuses) is invalid with the record's cost, so the matrix
settles what it spent and reruns it instead of stopping on an unknown cost. A run whose condition the session can't
play exactly (conditions.session_gaps) is labelled a pilot in result.json (the matrix refuses it unless allow_gaps). series.json freezes the scenario from
the series' first run; a later run of an edited scenario is refused.

Budget: a paid client's (matrix.PAID_CLIENTS) cost is settled by the matrix (`charged` False), so the host must not
add it to runtime/budget.json itself (LiveRunHost turns its runner's settles_budget off); a subscription run isn't
billed (`charged`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from bg3harness import profiles
from bg3harness.session import Recovery, SessionHooks, SessionOutcome, SessionSpec, run_session

from bg3bench import conditions, graders, pins, run_artifacts, scenario, series
from bg3bench.facts import FactsError
from bg3bench.matrix import PAID_CLIENTS, RunResult

NO_RESUME = Recovery(max_resumes=0)
JSON = dict[str, Any]


def series_name(matrix: str, job: Mapping[str, Any]) -> str:
    parts: list[str] = [matrix, job["scenario"], job["model"], job["client"], job["effort"] or "", job["condition"]]
    return re.sub(r"[^a-z0-9_-]+", "-", "-".join(p for p in parts if p).lower()).strip("-")[:100]


def live_verdict(goal: JSON, facts_path: Path) -> str | None:
    """graders.verdict over a facts.jsonl the run is still writing: a half-written last line is left for the next
    poll, and a file the grader can't read yet decides nothing."""
    try:
        lines = facts_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    records: list[JSON] = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            break
    try:
        return graders.verdict(goal, records)
    except FactsError:
        return None


def profile_ref(path: str | None) -> JSON | None:
    """A profile job's manifest `profile`: the file's name and the sha256 of what it sets (profiles.digest)."""
    if not path:
        return None
    return {"name": Path(path).name, "sha256": profiles.digest(profiles.load(path))}


def served_providers(rec: JSON) -> list[str]:
    served: JSON = rec.get("served") or {}
    providers: list[str] = served.get("providers") or []
    return providers


@dataclass(frozen=True)
class Start:
    prompt: str  # the kickoff
    hooks: SessionHooks
    client_session: str | None = None
    prompt_sha: str | None = None  # sha256 of the assembled prompt
    prompt_parts: Sequence[JSON] = field(default_factory=tuple)  # [{"file", "sha256"}]
    harness_version: str | None = None
    tools: JSON | None = None  # {"allowlist_sha256", "count"} of the bg3 tools served
    client_version: str | None = None


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
        profile = profile_ref(job.get("profile"))  # as the run starts: what it played with
        try:
            cond = conditions.parse(job["condition"])
            host_job = {**job, "tools_off": cond.removed_tools(), "prompt_condition": cond.prompt_condition()}
            if "goal" in sc:  # an until scenario's runner stops at its milestone itself
                goal, facts_path = sc["goal"], run_dir / run_artifacts.FACTS
                host_job["verdict"] = lambda: live_verdict(goal, facts_path)
            st = self.host.start(host_job, sc, run_dir)
        except Exception as e:  # noqa: BLE001 - no agent ran: invalid, nothing spent
            why = f"start failed: {type(e).__name__}: {e}"
            failed: JSON = {"series": name, "playthrough": run_dir.name, "run": k, "client": job["client"]}
            failed.update(model=job["model"], effort=job["effort"], outcome="runner_error", reason=why, cost_usd=0.0)
            with runs.open("a", encoding="utf-8") as f:  # the run number is used: a rerun gets a fresh run_dir
                f.write(json.dumps(failed) + "\n")
            return RunResult(None, False, False, 0.0, why, True)
        spec = SessionSpec(run_dir.name, st.prompt, sc["caps"]["run_hours"] * 60, st.client_session, NO_RESUME)
        out = run_session(spec, st.hooks)
        rec: JSON = dict(self.host.record(job, sc, run_dir, out))
        rec.update(series=name, playthrough=run_dir.name, run=k, client=job["client"], model=job["model"])
        rec.setdefault("effort", job["effort"])
        if job.get("profile") and (off := profiles.off_route(profiles.load(job["profile"]), served_providers(rec))):
            rec["off_route"] = off  # served outside the profile's provider pin: invalid (results.validity)
        rec.setdefault("started", out.started)
        versions = {**pins.versions(), **({"harness": st.harness_version} if st.harness_version else {})}
        rec["pins"] = {"versions": versions, **(rec.get("pins") or {})}
        if out.invalid:
            rec["harness_failures"] = int(rec.get("harness_failures") or 0) + 1
            rec["session_invalid"] = out.invalid
        with runs.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        charged = job["client"] not in PAID_CLIENTS
        try:
            _, res = run_artifacts.write_run_artifacts(
                run_dir,
                meta,
                rec,
                st.harness_version,
                st.prompt_sha,
                prompt_parts=st.prompt_parts,
                tools=st.tools,
                client_version=st.client_version,
                scenario_spec=sc,
                condition=job["condition"],
                profile=profile,
                pilot=conditions.session_gaps(job["condition"], job["client"]) or None,
            )
        except Exception as e:  # noqa: BLE001 - the run played and spent: ungradable is invalid, its cost still known
            why = f"artifacts failed: {type(e).__name__}: {e}"
            return RunResult(None, False, False, rec.get("cost_usd"), why[:500], charged)
        why = res["invalid_reason"]
        if why and out.invalid:
            why += f": {out.invalid} (no resume)"
        return RunResult(res["run_id"], res["valid"], res["passed"], res["cost_usd"], why, charged)
