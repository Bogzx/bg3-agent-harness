# pyright: basic
"""Re-grade finished runs with today's failure classifier, read-only: what each run's harness failures and validity
would be if its record were written now, and its party losses (result.json `deaths`) recounted from its facts.jsonl
(facts.party_losses: result.json files written before that counter have all zeros). Prints old -> new and the
failures that changed; never writes a file.

    uv run --project packages/bench python -m bg3bench.regrade RUN_DIR [RUN_DIR ...]

A run dir is a bench series' state dir (runtime/sessions/<series>/): its runs.jsonl holds every run's record, and
run k's play streams are in its own dir, the record's playthrough (<series>-r<k> for k > 1). bg3harness.runs.restream_failures re-reads the play streams (the record's other failures are kept);
results.validity decides with the record's own seed (its manifest's). diverse-2026-10-25 Opus x act3_gold_max was
voided by a background companion's move_to TIMEOUT echoed in a wait_for's EVENTS (now bg_timeout, not harness).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from bg3harness import runs

from bg3bench import facts, results

Json = dict[str, Any]


def _key(f: Json) -> tuple[str, str, str]:
    return f["kind"], f["class"], str(f.get("detail") or "")


def losses(record: Json, run_dir: Path) -> tuple[Json | None, Json | None]:
    """(old, new) party losses: result.json's deaths (else the record's) and facts.party_losses over the run's facts;
    new is None without a facts.jsonl."""
    res = run_dir / "result.json"
    old = json.loads(res.read_text(encoding="utf-8")).get("deaths") if res.is_file() else record.get("deaths")
    path = run_dir / "facts.jsonl"
    return old, facts.party_losses(facts.load(path)) if path.is_file() else None


def regrade(record: Json, run_dir: Path) -> Json:
    """{harness, valid, reason} old and new, the failures only in the old or the new list, the losses old and new."""
    fails: list[Json] = runs.restream_failures(record, run_dir)
    new = record | {"harness_failures": sum(int(f.get("n") or 1) for f in fails if f["class"] == runs.HARNESS)}
    seed = (record.get("manifest") or {}).get("seed")
    old_c, new_c = Counter(map(_key, record.get("failures") or [])), Counter(map(_key, fails))
    return {
        "old": {
            "harness": record.get("harness_failures"),
            **dict(zip(("valid", "reason"), results.validity(record, seed))),
        },
        "new": {"harness": new["harness_failures"], **dict(zip(("valid", "reason"), results.validity(new, seed)))},
        "dropped": sorted((old_c - new_c).elements()),
        "added": sorted((new_c - old_c).elements()),
        "deaths": dict(zip(("old", "new"), losses(record, run_dir))),
    }


def report(run_dir: Path) -> list[str]:
    recs: list[Json] = runs.read_jsonl(run_dir / "runs.jsonl")
    if not recs:
        return [f"{run_dir.name}: no runs.jsonl record yet"]
    out: list[str] = []
    for rec in recs:
        # run k's streams are in its own dir, not the series dir that holds runs.jsonl (series.run_name)
        r = regrade(rec, run_dir.parent / rec["playthrough"] if rec.get("playthrough") else run_dir)
        o, n = r["old"], r["new"]
        same = "unchanged" if o == n else "CHANGED"
        out.append(
            f"{run_dir.name} run {rec.get('run')}: {same}: harness_failures {o['harness']} -> {n['harness']}, "
            f"valid {o['valid']} ({o['reason']}) -> {n['valid']} ({n['reason']})"
        )
        d_old, d_new = r["deaths"]["old"], r["deaths"]["new"]
        if d_new is not None and d_old != d_new:
            out.append(f"  deaths {d_old} -> {d_new}")
        out += [f"  - {k} [{c}] {d}" for k, c, d in r["dropped"]]
        out += [f"  + {k} [{c}] {d}" for k, c, d in r["added"]]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bg3bench.regrade", description="Re-grade finished runs, read-only.")
    ap.add_argument("run_dirs", nargs="+", type=Path)
    for d in ap.parse_args(argv).run_dirs:
        print("\n".join(report(d)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
