"""The stress-test round (docs/OVERHAUL-PLAN.md Phase F), unattended: tools/stress/matrix.toml's checks against the
live game, one game at a time, results in runtime/stress/results.json, docs/STRESS-TEST.md rendered from both.

    uv run python tools/stress/run.py --plan                       # what would run (nothing starts)
    uv run python tools/stress/run.py                              # every check not finished yet
    uv run python tools/stress/run.py --group combat,party --kind acc,probe --hours 3
    uv run python tools/stress/run.py --only C5,C23 --profile profiles/deepseek-flash.toml --max-cost 1
    uv run python tools/stress/run.py --rerun failed               # FAIL and ERROR checks again
    uv run python tools/stress/run.py --record G6#1 PASS "F5/F8 refused by press_key"   # a manual check
    uv run python tools/stress/run.py --render                     # docs/STRESS-TEST.md only
    uv run python tools/stress/run.py --check                      # offline: the matrix, sections, milestones

Order: every acceptance check in one tools/dev/live_window.py window (it takes runtime/game.lock, launches,
stops), then probes and agent sessions in matrix order, each under runtime/game.lock and started on a COPY of the
row's save (`<owner>-<id>__SCN-stress_<row>`; the original is only read). Before each step the guards wait for
MemAvailable >= min_free_gb and the CPU <= max_cpu_c (matrix [defaults]), then stop; `touch runtime/stress/STOP`
stops before the next step; --hours stops starting steps. Exit 3 = stopped early: the same command resumes (a
check that was running when the runner died runs again). Agent sessions on a paid profile (pi) need --max-cost
or the matrix's max_cost and runtime/budget.json (play.py refuses without it); Claude Code is the subscription.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import stress_core as core
import stress_exec as ex
import stress_matrix as sm

REPO = Path(__file__).resolve().parents[2]
MATRIX = Path(__file__).resolve().parent / "matrix.toml"
DOC = REPO / "docs" / "STRESS-TEST.md"
# the machine's runtime: game.lock (live_window, acceptance, every worker), ramwatch.lock, runtime/live windows,
# play.py's sessions. --runtime never moves them: a run elsewhere must still exclude the others (live6)
MACHINE = REPO / "runtime"


def csv(s: str) -> frozenset[str]:
    return frozenset(x.strip() for x in s.split(",") if x.strip())


def parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", type=csv, default=frozenset(), help="row ids or check ids (G6, G6#1), comma-separated")
    ap.add_argument("--group", type=csv, default=frozenset(), help="group keys (matrix.toml [[group]] key)")
    ap.add_argument("--kind", type=csv, default=frozenset(), help="check kinds: acc, probe, agent, manual")
    ap.add_argument("--act", type=csv, default=frozenset(), help="acts: P, 1, 2, 3, any")
    ap.add_argument(
        "--rerun",
        choices=("none", "failed", "all"),
        default="none",
        help="failed: FAIL and ERROR checks again; all: every selected check",
    )
    ap.add_argument("--profile", help="every agent session on this profile (profiles/*.toml)")
    ap.add_argument("--max-cost", type=float, help="USD per agent session on a paid profile")
    ap.add_argument("--hours", type=float, help="start no step after this many hours")
    ap.add_argument("--plan", action="store_true", help="print the steps and stop")
    ap.add_argument("--render", action="store_true", help="write docs/STRESS-TEST.md and stop")
    ap.add_argument("--check", action="store_true", help="validate the matrix offline and stop")
    ap.add_argument("--record", nargs=3, metavar=("CHECK", "VERDICT", "EVIDENCE"), help="record a check's result")
    ap.add_argument("--matrix", type=Path, default=MATRIX)
    ap.add_argument(
        "--runtime",
        type=Path,
        default=MACHINE,
        help="the run's own files (stress/results.json, probe output, scenarios); game.lock, the live windows, "
        "ramwatch and play.py's sessions stay in the machine's runtime/ (live6)",
    )
    ap.add_argument("--doc", type=Path, default=DOC)
    a = ap.parse_args(argv)
    if a.max_cost is not None and a.max_cost <= 0:
        ap.error("--max-cost must be positive")
    return a


def describe(step: core.Step, m: sm.Matrix, a: argparse.Namespace, paid: dict[str, bool]) -> str:
    if step.kind == "acc":
        return f"acc   live_window --only {','.join(step.sections)}: " + ", ".join(c.key for c in step.checks)
    c = step.checks[0]
    row = m.by_id[c.row]
    save = f"copy of {row.save} as {core.copy_label(row)}" if row.save else "new game"
    if step.kind == "probe":
        return f"probe {c.key} {c.spec['script']} {' '.join(c.spec.get('args') or [])} on {save}"
    if step.kind == "skip":
        return f"skip  {c.key} probe not ready: {c.spec['why']}"
    if step.kind == "manual":
        return f"manual {c.key}: {c.spec['what']}"
    profile = a.profile or c.spec.get("profile") or m.defaults["profile"]
    cap = (a.max_cost or c.spec.get("max_cost") or m.defaults["max_cost"]) if paid.get(profile) else None
    goal = "goal predicate" if c.spec.get("goal") else "no goal (REVIEW)"
    cost = f", --max-cost {cap:g}" if cap else " (subscription)"
    return (
        f"agent {c.key} {c.spec.get('minutes') or m.defaults['minutes']:g} min on {save}, {profile}{cost}, "
        f"{goal}: {c.spec['objective']}"
    )


def main(argv: list[str] | None = None) -> int:
    a = parse(argv)
    m = sm.parse(a.matrix.read_text())
    import stress_io as io  # the real adapters (bg3bench, the save folder): not needed to parse the CLI

    store = io.JsonStore(a.runtime / "stress" / "results.json", core.new_results())

    def render(results: dict) -> None:
        a.doc.write_text(core.render(m, results, time.strftime("%Y-%m-%d %H:%M")))

    if a.check:
        from bg3ai.milestones import MILESTONES

        errs = sm.problems(m, io.acceptance_sections(REPO), [x[0] for x in MILESTONES])
        print("\n".join(errs) or f"matrix OK: {len(m.rows)} rows, {sum(len(r.checks) for r in m.rows)} checks")
        return 1 if errs else 0
    if a.render:
        render(store.load())
        print(f"wrote {a.doc}")
        return 0
    if a.record:
        key, verdict, evidence = a.record
        if verdict not in core.VERDICTS:
            print(f"verdict must be one of {', '.join(core.VERDICTS)}", file=sys.stderr)
            return 2
        m.check(key)  # MatrixError on an unknown id
        results = store.load()
        prev = results["checks"].get(key) or {}
        results["checks"][key] = {
            "kind": m.check(key).kind,
            "verdict": verdict,
            "evidence": core.clip(evidence),
            "state": "done",
            "attempts": prev.get("attempts", 0) + 1,
            "ended": round(time.time(), 1),
            "by": "hand",
        }
        store.save(results)
        render(results)
        print(f"recorded {key} {verdict}; wrote {a.doc}")
        return 0
    sel = core.Select(a.only, a.group, a.kind, a.act)
    results = store.load()
    steps = core.plan(core.todo(m, results, sel, a.rerun), io.acceptance_sections(REPO))
    if a.plan:
        profiles = (
            {a.profile}
            if a.profile
            else {m.defaults["profile"]}
            | {c.spec["profile"] for r in m.rows for c in r.checks if c.spec.get("profile")}
        )
        paid = {p: core.is_paid(io.read_profile(p, REPO)) for p in profiles}
        for s in steps:
            print(describe(s, m, a, paid))
        print(f"{len(steps)} steps")
        return 0
    return ex.Runner(m, adapters(a, store, render), ex.Opts(profile=a.profile, max_cost=a.max_cost, hours=a.hours)).run(
        steps
    )


def adapters(a: argparse.Namespace, store: Any, render: Callable[[dict], None]) -> ex.Deps:
    import stress_io as io
    from bg3harness import steam_paths

    return ex.Deps(
        clock=io.SystemClock(),
        host=io.ProcHost(a.runtime / "stress" / "STOP"),
        saves=io.DirSaves(steam_paths.saves_dir()),  # (the tools/steam_paths.py shim, loaded as a file, has none)
        lock=io.FlockLock(MACHINE / "game.lock"),
        acc=io.LiveWindowAcc(REPO, a.runtime, MACHINE),
        probe=io.WindowProbe(REPO, a.runtime, MACHINE),
        session=io.PlaySession(REPO, a.runtime, MACHINE),
        store=store,
        read_profile=lambda p: io.read_profile(p, REPO),
        render=render,
        say=lambda s: print(s, flush=True),
    )


if __name__ == "__main__":
    raise SystemExit(main())
