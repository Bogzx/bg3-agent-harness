"""Scenario authoring kit (docs/SCENARIOS-AUTHORING.md): the steps from a game save to a scenario that is ready.

1. `register_save`: copy a save folder (never touching the original) into a store, check the character name is
   neutral and the label is `SCN-<name>`, run the save inspector on the copy, record the .lsv sha1 the runner checks
   (runs.save_digest) and a sha256 per file. Writes `<store>/<name>/registration.json`.
2. `draft_scenario`: a scenario v2 from the registration and the author's goal and note, validated.
3. `attach_oracle`: the reference trajectory (D12: one successful run by a strong model, a human run, or a script
   for a trivial scenario) as `<oracle_dir>/<name>.json`, with the trajectory file's sha256.
4. `run_baselines`: do-nothing × n and random × n seeds through the matrix runner, on a budget of $0, and the
   baseline rule: each must pass in under 5% of at least 5 valid runs.

Step 1 from the shell, with tools/saves/inspect.py as the inspector:

    uv run --project packages/bench python -m bg3bench.authoring register <save folder> <store> <name>
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from bg3bench import matrix, scenario

REGISTRATION_SCHEMA = 1
ORACLE_SCHEMA = 1
ORACLE_KINDS = ("model_run", "human_run", "scripted")
NEUTRAL_RE = re.compile(r"^[A-Z][a-z]{1,15}$")
# Names that tie a save to a model, a client, a person or a playthrough: a start save is anyone's start.
NOT_NEUTRAL = (
    "claude", "anthropic", "opus", "sonnet", "haiku", "fable", "gpt", "openai", "codex", "gemini", "deepseek",
    "qwen", "llama", "grok", "mistral", "bogdan", "bogzx", "truta", "agent", "bot", "model", "test",
)  # fmt: skip
BASELINE_RULE = 0.05
BASELINE_MIN_RUNS = 5
REPO = Path(__file__).resolve().parents[4]


class AuthoringError(ValueError):
    pass


def _sha(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def neutral_name_problems(owner: str) -> list[str]:
    if not NEUTRAL_RE.match(owner):
        return [f"character name {owner!r}: use one plain capitalised word (Tav, Ana)"]
    low = owner.lower()
    return [f"character name {owner!r} names {bad!r}: pick a neutral name" for bad in NOT_NEUTRAL if bad in low]


def register_save(
    src: str | Path,
    store: str | Path,
    name: str,
    *,
    inspector: Callable[[Path], list[str]] | None = None,
    now: Callable[[], float],
) -> dict:
    """Copy save folder `src` (`<owner>-<id>__SCN-<name>`) to `<store>/<name>/<folder>` and register it."""
    src = Path(src)
    m = scenario.SAVE_FOLDER_RE.match(src.name)
    if not src.is_dir() or m is None:
        raise AuthoringError(f"{src}: not a save folder (<owner>-<id>__<label>)")
    owner, label = m.group("owner"), m.group("label")
    problems = neutral_name_problems(owner)
    if label != f"SCN-{name}":
        problems.append(f"save label {label!r}: re-save it as SCN-{name}")
    lsv = src / f"{label}.lsv"
    if not lsv.is_file():
        problems.append(f"{lsv.name} missing: not a complete save")
    if problems:
        raise AuthoringError("; ".join(problems))
    dest_root = Path(store) / name
    dest = dest_root / src.name
    if dest_root.exists():
        raise AuthoringError(f"{dest_root} exists: a start save is registered once (make a new scenario name)")
    shutil.copytree(src, dest)
    findings = inspector(dest) if inspector else []
    files = [
        {"path": p.relative_to(dest).as_posix(), "bytes": p.stat().st_size, "sha256": _sha(p, "sha256")}
        for p in sorted(dest.rglob("*"))
        if p.is_file()
    ]
    reg = {
        "schema": REGISTRATION_SCHEMA,
        "name": name,
        "folder": src.name,
        "owner": owner,
        "lsv_sha1": _sha(dest / lsv.name, "sha1"),
        "files": files,
        "inspector": None if inspector is None else findings,
        "registered": now(),
    }
    (dest_root / "registration.json").write_text(json.dumps(reg, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if findings:
        raise AuthoringError(f"the save inspector flagged the copy (kept in {dest_root}): " + "; ".join(findings))
    return reg


def draft_scenario(
    registration: dict,
    *,
    title: str,
    family: str,
    where: str,
    objective: str,
    brief: str,
    run_hours: float,
    goal: dict | None = None,
    until: str | None = None,
    max_cost: float | None = None,
    difficulty: str | None = "Balanced",
    seed: int | None = None,
    holdout: bool = False,
    requires: dict | None = None,
    conditions: Sequence[str] = ("rules",),
    party: Sequence[str] = (),
    journal: Sequence[str] = (),
) -> dict:
    """A validated scenario v2 for a registered start save."""
    note = {"where": where, "objective": objective}
    if party:
        note["party"] = list(party)
    if journal:
        note["journal"] = list(journal)
    sc = {
        "schema": scenario.SCHEMA,
        "name": registration["name"],
        "title": title,
        "family": family,
        "holdout": holdout,
        "start": {"kind": "save", "save": registration["folder"], "save_sha1": registration["lsv_sha1"], "note": note},
        **({"goal": goal} if goal is not None else {}),
        **({"until": until} if until is not None else {}),
        "runs": 3,
        "caps": {"run_hours": run_hours, "max_cost": max_cost, "max_tokens": None},
        "profile": {"character": {"name": registration["owner"], "brief": brief, "roleplay": "strict"}},
        "game_settings": {**scenario.GAME_SETTINGS, "difficulty": difficulty, "seed": seed},
        "timing": scenario.default_timing(family),
        "requires": {**scenario.REQUIRES, **(requires or {})},
        "conditions": list(conditions),
    }
    if errs := scenario.validate(sc, sc["name"]):
        raise AuthoringError("; ".join(errs))
    return sc


def write_draft(sc: dict, scenarios_dir: str | Path) -> Path:
    path = Path(scenarios_dir) / f"{sc['name']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise AuthoringError(f"{path} exists: a scenario file is never overwritten (series freeze its digest)")
    path.write_text(json.dumps(sc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def attach_oracle(
    oracle_dir: str | Path,
    sc: dict,
    trajectory: str | Path,
    *,
    kind: str,
    run_id: str | None,
    passed: bool,
    t_pass_s: float | None,
) -> dict:
    """Record the reference trajectory: proof the goal is reachable with the tools and caps a model gets."""
    if kind not in ORACLE_KINDS:
        raise AuthoringError(f"oracle kind must be one of {', '.join(ORACLE_KINDS)}")
    if not passed:
        raise AuthoringError("an oracle trajectory must reach the goal (the grader passed it)")
    traj = Path(trajectory)
    if not traj.is_file():
        raise AuthoringError(f"{traj}: no trajectory file")
    ref = {
        "schema": ORACLE_SCHEMA,
        "scenario": sc["name"],
        "scenario_digest": scenario.digest(sc),
        "kind": kind,
        "run_id": run_id,
        "trajectory": traj.name,
        "sha256": _sha(traj, "sha256"),
        "t_pass_s": t_pass_s,
    }
    out = Path(oracle_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{sc['name']}.json").write_text(json.dumps(ref, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return ref


def baseline_jobs(name: str, runs: int = BASELINE_MIN_RUNS, condition: str = "rules") -> list[dict]:
    """do-nothing × runs, then random with seeds 1..runs (one run each), as matrix jobs."""
    nothing = matrix.plan([{"slug": "baseline:donothing", "client": "baseline"}], [name], [condition], runs)
    random_ = matrix.plan(
        [{"slug": f"baseline:random:{i}", "client": "baseline"} for i in range(1, runs + 1)], [name], [condition], 1
    )
    return nothing + random_


def run_baselines(
    root: str | Path,
    name: str,
    run_one: matrix.RunOne,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    guard: Callable[[], str | None] | None = None,
    runs: int = BASELINE_MIN_RUNS,
) -> dict:
    """Both baselines through the matrix runner on a $0 budget; the verdict of the baseline rule."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    budget = root / "budget.json"
    if not budget.exists():
        budget.write_text(json.dumps({"cap": 0.0, "spent": 0.0}) + "\n", encoding="utf-8")
    m = matrix.Matrix(root / "matrix", run_one, budget, now=now, sleep=sleep, guard=guard)
    m.create(f"baselines-{name}", baseline_jobs(name, runs))
    stop = m.run()
    out: dict = {"stop": stop}
    for agent in ("donothing", "random"):
        valid = [
            a["result"]
            for st in m.state().values()
            if st["job"]["model"].startswith(f"baseline:{agent}")
            for a in st["attempts"]
            if a["result"] is not None and a["result"]["valid"]
        ]
        k = sum(1 for r in valid if r["passed"])
        out[agent] = {"n": len(valid), "k": k}
    out["ok"] = stop == matrix.FINISHED and all(
        out[a]["n"] >= BASELINE_MIN_RUNS and out[a]["k"] / out[a]["n"] < BASELINE_RULE for a in ("donothing", "random")
    )
    return out


def save_inspector(repo: Path = REPO) -> Callable[[Path], list[str]]:
    """tools/saves/inspect.py (the harness's, not packaged) as register_save's inspector: one line per finding."""
    spec = importlib.util.spec_from_file_location("bg3_save_inspect", repo / "tools" / "saves" / "inspect.py")
    if spec is None or spec.loader is None:
        raise AuthoringError(f"no save inspector at {repo / 'tools' / 'saves' / 'inspect.py'}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def inspect(folder: Path) -> list[str]:
        return [
            f"{h['kind']} {h['value']} in {', '.join(h['members'])}"
            for lsv in sorted(folder.glob("*.lsv"))
            for h in mod.inspect_save(lsv)["findings"]
        ]

    return inspect


def main(argv: Sequence[str] | None = None, *, inspector: Callable[[Path], list[str]] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bg3bench.authoring", description="Register a start save (step 1).")
    sub = ap.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register", help="copy and register a SCN-<name> save folder")
    reg.add_argument("save", type=Path, help="the save folder <owner>-<id>__SCN-<name> (never modified)")
    reg.add_argument("store", type=Path, help="the start-save store (outside the repo)")
    reg.add_argument("name", help="the scenario name")
    args = ap.parse_args(argv)
    try:
        got = register_save(args.save, args.store, args.name, inspector=inspector or save_inspector(), now=time.time)
    except AuthoringError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"registered {got['name']}: {args.store / got['name'] / got['folder']} (lsv sha1 {got['lsv_sha1']})")
    return 0


if __name__ == "__main__":  # pragma: no cover - python -m bg3bench.authoring
    raise SystemExit(main())
