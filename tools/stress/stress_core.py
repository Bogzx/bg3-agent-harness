"""The stress run's pure logic: which checks to run, in which steps, their verdicts, the scenario an agent check
plays, and docs/STRESS-TEST.md. No I/O: stress_exec.py drives it, stress_io.py holds the real adapters.

results.json (v1, results.schema.json): {"v": 1, "checks": {"G6#1": {"kind", "verdict", "evidence", "state",
"attempts", "started", "ended"}}}. state "running" = started and never recorded (the runner died): run again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bg3bench.facts import FactsError
from bg3bench.graders import grade
from stress_matrix import Check, Matrix, Row

RESULTS_V = 1
VERDICTS = ("PASS", "FAIL", "ERROR", "REVIEW", "SKIP", "INFO")
PAID_CLIENTS = ("pi",)  # runner/spend.py PAID_CLIENTS: these need --max-cost and runtime/budget.json
STRESS_LINE_RE = re.compile(r"^STRESS (PASS|FAIL|SKIP|INFO|REVIEW)\b[ :]*(.*)$")
EVIDENCE_MAX = 300


def new_results() -> dict:
    return {"v": RESULTS_V, "checks": {}}


def combine(verdicts: list[str]) -> str:
    """A row's verdict from its checks': any FAIL, else any ERROR, else PASS when all pass, else the first of
    REVIEW, SKIP, INFO present. Empty when there's nothing."""
    for v in ("FAIL", "ERROR"):
        if v in verdicts:
            return v
    if verdicts and all(v == "PASS" for v in verdicts):
        return "PASS"
    return next((v for v in ("REVIEW", "SKIP", "INFO") if v in verdicts), "")


def done(results: dict, key: str) -> dict | None:
    """The recorded result of a check, None when it never finished."""
    r = results["checks"].get(key)
    return r if r and r.get("state") == "done" else None


def row_result(row: Row, results: dict) -> tuple[str, list[str], int]:
    """(verdict, evidence per finished check, checks not finished). A FAIL or ERROR decides the row at once;
    otherwise it has no verdict until every check finished."""
    finished = [(c, done(results, c.key)) for c in row.checks]
    have = [(c, r) for c, r in finished if r]
    pending = len(row.checks) - len(have)
    verdict = combine([r["verdict"] for _, r in have])
    if pending and verdict not in ("FAIL", "ERROR"):
        verdict = ""
    return verdict, [f"{c.key} {r['verdict']}: {r['evidence']}" for c, r in have], pending


# ------------------------------------------------------------------------------------------------ selection, plan
@dataclass(frozen=True)
class Select:
    ids: frozenset[str] = frozenset()
    groups: frozenset[str] = frozenset()
    kinds: frozenset[str] = frozenset()
    acts: frozenset[str] = frozenset()

    def wants(self, row: Row, check: Check) -> bool:
        return (
            (not self.ids or row.id in self.ids or check.key in self.ids)
            and (not self.groups or row.group in self.groups)
            and (not self.kinds or check.kind in self.kinds)
            and (not self.acts or row.act in self.acts)
        )


def todo(m: Matrix, results: dict, sel: Select, rerun: str = "none") -> list[Check]:
    """The selected checks to run: never finished (or interrupted), plus with rerun="failed" the FAIL/ERROR ones,
    with rerun="all" every one. Matrix order."""
    if rerun not in ("none", "failed", "all"):
        raise ValueError(f"rerun is none, failed or all, got {rerun!r}")
    out = []
    for row in m.rows:
        for c in row.checks:
            if not sel.wants(row, c):
                continue
            r = done(results, c.key)
            if r is None or rerun == "all" or (rerun == "failed" and r["verdict"] in ("FAIL", "ERROR")):
                out.append(c)
    return out


@dataclass(frozen=True)
class Step:
    """One unit of the run. kind: acc (every acc check in one live window), probe, agent, skip (a probe that
    isn't ready: recorded SKIP without the game), manual (listed, never run: --record)."""

    kind: str
    checks: tuple[Check, ...]
    sections: tuple[str, ...] = ()


def plan(checks: list[Check], section_order: list[str]) -> list[Step]:
    """Steps in run order: the acceptance first (its sections in acceptance.py's order, one live window), then
    probes and agent sessions in matrix order, then the manual checks."""
    acc = [c for c in checks if c.kind == "acc"]
    steps = []
    if acc:
        want = {c.spec["section"] for c in acc}
        steps.append(Step("acc", tuple(acc), tuple(s for s in section_order if s in want)))
    for c in checks:
        if c.kind == "probe":
            steps.append(Step("probe" if c.spec.get("ready", True) else "skip", (c,)))
        elif c.kind == "agent":
            steps.append(Step("agent", (c,)))
    steps += [Step("manual", (c,)) for c in checks if c.kind == "manual"]
    return steps


# ------------------------------------------------------------------------------------------------ verdicts
def clip(s: str, n: int = EVIDENCE_MAX) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def acc_verdicts(checks: tuple[Check, ...], items: list[dict], where: str) -> dict[str, tuple[str, str]]:
    """Each acc check's (verdict, evidence) from acceptance results.json rows ({section, item, verdict,
    evidence}): the items of its section whose name starts with the check's item, combined. None found: ERROR
    (the section died before it, or the item was renamed: the section's own FAIL line is quoted)."""
    out = {}
    for c in checks:
        sec, name = c.spec["section"], c.spec["item"]
        hits = [r for r in items if r.get("section") == sec and str(r.get("item", "")).startswith(name)]
        if hits:
            verdict = combine([r.get("verdict", "") for r in hits])
            ev = "; ".join(f"{r['item']}={r.get('verdict')}: {r.get('evidence', '')}" for r in hits)
            out[c.key] = (verdict or "INFO", clip(f"acc {where}: {ev}"))
            continue
        why = [r for r in items if r.get("section") == sec and r.get("verdict") == "FAIL"]
        tail = f" (section FAILs: {'; '.join(r['item'] + ': ' + r.get('evidence', '') for r in why)})" if why else ""
        out[c.key] = ("ERROR", clip(f"acc {where}: no item {name!r} in section {sec}'s results{tail}"))
    return out


def probe_verdict(rc: int | None, output: str, log: str) -> tuple[str, str]:
    """A probe's verdict: its last `STRESS <VERDICT> <evidence>` line; else exit 0 = REVIEW (ran; the log says
    what it found), another exit = FAIL, no exit (timeout) = FAIL."""
    for line in reversed(output.splitlines()):
        m = STRESS_LINE_RE.match(line.strip())
        if m:
            return m.group(1), clip(f"{m.group(2)} ({log})")
    last = next((ln.strip() for ln in reversed(output.splitlines()) if ln.strip()), "")
    if rc is None:
        return "FAIL", clip(f"timeout; last line: {last} ({log})")
    if rc == 0:
        return "REVIEW", clip(f"exit 0, no STRESS line: read {log}; last line: {last}")
    return "FAIL", clip(f"exit {rc}: {last} ({log})")


def session_verdict(record: dict | None, facts: list[dict], goal: dict | None, where: str) -> tuple[str, str]:
    """An agent session's verdict from its runs.jsonl record and facts.jsonl: no record or a crash = ERROR (an
    invalid run, run it again); no goal = REVIEW (a person reads the session); else the goal graded on the wall
    clock: PASS when it was met, FAIL when not."""
    if record is None:
        return "ERROR", clip(f"no runs.jsonl record: the runner did not finish the run ({where})")
    summary = (
        f"outcome {record.get('outcome')}, {record.get('wall_min')} min, ${record.get('cost_usd')}, "
        f"{record.get('harness_failures', 0)} harness failures"
    )
    if record.get("outcome") == "runner_error" or record.get("crashes"):
        return "ERROR", clip(f"invalid run: {summary}, {record.get('crashes', 0)} crashes ({where})")
    if goal is None:
        return "REVIEW", clip(f"no goal predicate: read the session; {summary} ({where})")
    try:
        g = grade(goal, facts, clock="wall")
    except (FactsError, ValueError) as e:
        return "ERROR", clip(f"facts.jsonl can't be graded: {e} ({where})")
    if g["passed"]:
        return "PASS", clip(f"goal met at {g['t_pass_s']:.0f} s; {summary} ({where})")
    return "FAIL", clip(f"goal not met; {summary} ({where})")


# ------------------------------------------------------------------------------------------------ agent scenario
def scenario_name(row: Row) -> str:
    return f"stress_{row.id.lower()}"


def copy_label(row: Row) -> str:
    """The label of the save copy an agent or probe check starts from: a scenario's start save can't carry an
    agent `Sxxx-` label (bg3bench.scenario_v1), and the original is never loaded for world-changing work."""
    return f"SCN-{scenario_name(row)}"


def is_paid(profile: dict) -> bool:
    return profile.get("client", "claude") in PAID_CLIENTS


def cost_cap(check: Check, defaults: dict, profile: dict, cli_max_cost: float | None) -> float | None:
    """--max-cost for an agent session: on a paid profile the CLI's, else the check's, else the default; None on a
    subscription client (Claude Code)."""
    if not is_paid(profile):
        return None
    return cli_max_cost or check.spec.get("max_cost") or defaults["max_cost"]


def resolve_scenario(row: Row, check: Check, defaults: dict, start: tuple[str, str] | None, cap: float | None) -> dict:
    """The scenario v1 (scenarios/README.md) an agent check plays: start = the save copy's (folder, owner), or a
    new game when None; one run; capped at the check's minutes (and cost on a paid profile); stops at `until`
    (default game_end, so the cap ends it)."""
    minutes = check.spec.get("minutes") or defaults["minutes"]
    character: dict[str, Any] = {"brief": defaults["character_brief"]}
    if start is None:
        st: dict[str, Any] = {"kind": "new_game"}
    else:
        folder, owner = start
        character["name"] = owner
        note = {"where": check.spec["where"], "objective": check.spec["objective"]}
        st = {"kind": "save", "save": folder, "save_sha1": None, "note": note}
    return {
        "name": scenario_name(row),
        "title": clip(f"stress {row.id}: {row.situation}", 120),
        "about": f"Stress-test row {row.id} ({row.group}, act {row.act}): {row.situation}",
        "start": st,
        "until": check.spec.get("until", "game_end"),
        "runs": 1,
        "caps": {"run_hours": round(minutes / 60, 4), "max_cost": cap, "max_tokens": None},
        "profile": {"character": character},
    }


# ------------------------------------------------------------------------------------------------ docs/STRESS-TEST.md
def _check_text(c: Check, defaults: dict) -> str:
    s = c.spec
    if c.kind == "acc":
        return f"acc:{s['section']}/{s['item']}"
    if c.kind == "probe":
        return f"probe:{s['script']}" + ("" if s.get("ready", True) else " (not ready)")
    if c.kind == "agent":
        goal = "goal" if s.get("goal") else "review"
        return f"agent ({s.get('minutes') or defaults['minutes']:g} min, {goal})"
    return "manual"


def _cell(s: str) -> str:
    return s.replace("|", "/").replace("\n", " ")


def render(m: Matrix, results: dict, stamp: str) -> str:
    """docs/STRESS-TEST.md: how to run it, a summary per group and per act, then one table per group."""
    per_row = {r.id: row_result(r, results) for r in m.rows}
    cols = ["PASS", "FAIL", "ERROR", "REVIEW", "SKIP", "INFO", "not run"]

    def counts(rows: list[Row]) -> list[int]:
        vs = [per_row[r.id][0] or "not run" for r in rows]
        return [vs.count(c) for c in cols]

    out = [
        "# Stress-test matrix",
        "",
        "Generated by `tools/stress/run.py --render` from `tools/stress/matrix.toml` and",
        f"`runtime/stress/results.json` ({stamp}); edit the matrix, not this file. Phase F of docs/OVERHAUL-PLAN.md:",
        "every BG3 situation group x act, the start save that reaches it, the checks that prove the harness works",
        "there, and the result.",
        "",
        "```bash",
        "uv run python tools/stress/run.py --plan                 # what would run, nothing started",
        "uv run python tools/stress/run.py --group combat --kind acc,probe",
        "uv run python tools/stress/run.py --only C5 --profile profiles/deepseek-flash.toml --max-cost 1",
        "uv run python tools/stress/run.py --record G6#1 PASS 'F5/F8 refused by press_key'",
        "```",
        "",
        "A run is resumable: re-running it skips every finished check and re-runs one that was interrupted. Verdicts:",
        "PASS, FAIL, ERROR (the check could not run: rerun with --rerun failed), REVIEW (ran, needs a person: an agent",
        "session without a goal predicate, a probe without a STRESS line), SKIP, INFO. A row passes when every check",
        "does. The baseline column is the live worker's 2026-10-21 run.",
        "",
        "## Summary",
        "",
        "| group | rows | " + " | ".join(cols) + " |",
        "|---|---|" + "---|" * len(cols),
    ]
    for key, title in m.groups:
        rows = [r for r in m.rows if r.group == key]
        out.append(f"| {title} | {len(rows)} | " + " | ".join(str(n) for n in counts(rows)) + " |")
    out.append(f"| **all** | {len(m.rows)} | " + " | ".join(str(n) for n in counts(list(m.rows))) + " |")
    out += ["", "| act | rows | " + " | ".join(cols) + " |", "|---|---|" + "---|" * len(cols)]
    for act in ("P", "1", "2", "3", "any"):
        rows = [r for r in m.rows if r.act == act]
        if rows:
            out.append(f"| {act} | {len(rows)} | " + " | ".join(str(n) for n in counts(rows)) + " |")
    for key, title in m.groups:
        out += ["", f"## {title}", "", "| id | situation | act | save | checks | result | baseline 2026-10-21 |"]
        out.append("|---|---|---|---|---|---|---|")
        for r in (r for r in m.rows if r.group == key):
            verdict, evidence, pending = per_row[r.id]
            save = r.save or "new game"
            if r.alt_saves:
                save += " / " + " / ".join(r.alt_saves)
            if r.save_note:
                save += f" ({r.save_note})"
            result = verdict or ("" if pending == len(r.checks) else f"{len(r.checks) - pending}/{len(r.checks)} run")
            if evidence:
                result = (result + ": " if result else "") + "; ".join(evidence)
            checks = " + ".join(_check_text(c, m.defaults) for c in r.checks)
            out.append(
                f"| {r.id} | {_cell(r.situation)} | {r.act} | {_cell(save)} | {_cell(checks)} | {_cell(result)} "
                f"| {_cell(r.baseline)} |"
            )
    return "\n".join(out) + "\n"
