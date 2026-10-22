"""The persona judge wired to the runner (moved from runner/judge.py): bg3harness.judge with the runner's events
loader (bg3harness.events), its playthroughs dir (renv.PLAYTHROUGHS, read at call time) and the pinned default judge
model. judge_run(state_dir, record, ...) is what the runner calls to add `persona` (judge.persona_field) to a record.

    uv run python -m bg3harness.runner.judging <session_dir> [--run K] [--model M | --profile P --max-cost USD]
                                                [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from bg3harness import events, profiles
from bg3harness import judge as core
from bg3harness.runner import env as renv

# pinned: change it and old scores no longer compare (judge_model is recorded)
DEFAULT_MODEL = os.environ.get("BG3AI_JUDGE_MODEL", "claude-sonnet-5-5")


def stream_decisions(stream: Path, since: float | None = None, until: float | None = None) -> list[core.Decision]:
    return core.stream_decisions(stream, since, until, events.load_events)


def extract(
    state_dir: Path, since: float | None = None, until: float | None = None, limit: int = core.MAX_DECISIONS
) -> tuple[list[core.Decision], int]:
    return core.extract(state_dir, since, until, events.load_events, limit)


def load_character(
    record: dict[str, Any] | None, session_dir: Path, path: Path | None = None
) -> tuple[str | None, str]:
    return core.load_character(record, session_dir, path, renv.PLAYTHROUGHS)


def judge_run(
    state_dir: Path, record: dict[str, Any] | None = None, *, client: Any = None, **kw: Any
) -> dict[str, Any]:
    """bg3harness.judge.judge_run with the runner's playthroughs, events loader and default judge (claude -p)."""
    return core.judge_run(
        state_dir,
        record,
        client=client or core.ClaudeCLI(DEFAULT_MODEL),
        playthroughs=renv.PLAYTHROUGHS,
        load=events.load_events,
        **kw,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score a run's in-character decisions against CHARACTER.md.")
    ap.add_argument("session_dir", type=Path, help="runtime/sessions/<playthrough> (the series or the run's dir)")
    ap.add_argument("--run", type=int, default=None, help="run number in runs.jsonl (default: the dir's / last)")
    ap.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"judge model via claude -p (default {DEFAULT_MODEL}; keep it fixed)"
    )
    ap.add_argument(
        "--profile", type=Path, default=None, help="judge with this client profile (profiles/*.toml) instead of --model"
    )
    ap.add_argument(
        "--max-cost", type=float, default=None, help="USD cap over all judge calls (needed for a paid profile)"
    )
    ap.add_argument(
        "--character",
        type=Path,
        default=None,
        help="CHARACTER.md to judge against (default: the run's playthrough memory)",
    )
    ap.add_argument("--limit", type=int, default=core.MAX_DECISIONS)
    ap.add_argument("--chunk", type=int, default=core.CHUNK)
    ap.add_argument("--allow-same-model", action="store_true", help="judge with the model under test anyway")
    ap.add_argument("--dry-run", action="store_true", help="print the decisions and the prompt; no model call")
    a = ap.parse_args(argv)
    record = core.find_record(a.session_dir, a.run)
    if record is None and a.run is not None:
        print(f"no run {a.run} in {a.session_dir}/runs.jsonl", file=sys.stderr)
        return 2
    try:
        client = (
            core.client_for(profiles.load(a.profile), max_cost_usd=a.max_cost) if a.profile else core.ClaudeCLI(a.model)
        )
    except (profiles.ProfileError, core.UnusableProfileError) as e:
        print(f"judge: {e}", file=sys.stderr)
        return 2
    if a.dry_run:
        rec = record or {}
        sdir = core.streams_dir(a.session_dir, record)
        end = rec.get("t")
        decisions, dropped = extract(sdir, rec.get("started"), end + core.WINDOW_SLACK_S if end else None, a.limit)
        character, source = load_character(record, sdir, a.character)
        print(
            f"judge {client.model} run {rec.get('run')} streams {sdir} character {source or 'NONE'}: "
            f"{len(decisions)} decisions ({dropped} dropped)\n"
        )
        print(core.build_prompt(character or "(none)", decisions, dropped))
        return 0
    res = judge_run(
        a.session_dir,
        record,
        client=client,
        character_path=a.character,
        limit=a.limit,
        chunk=a.chunk,
        allow_same_model=a.allow_same_model,
    )
    print(json.dumps(res, indent=1, ensure_ascii=False))
    return 0 if res["status"] in (core.OK, core.NO_DECISIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
