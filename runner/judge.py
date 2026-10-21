"""Persona judge for the benchmark: scores a run's in-character decisions against the playthrough's CHARACTER.md.

    python3 runner/judge.py <session_dir> [--run K] [--model M | --profile P --max-cost USD] [--dry-run]

Shim: the judge lives in packages/harness (bg3harness.judge, decision D4). This module wires it to the runner: the
events loader (runner/events.py), the playthroughs dir and the default judge model, which tests patch here.

judge_run(state_dir, record, ...) is what play.py calls to add `persona` (persona_field(result)) to a run record.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "harness" / "src"))
import events
from bg3harness import judge as _core
from bg3harness.judge import (
    ATTEMPTS,
    BATCH_TOOLS,
    CHOOSE,
    CHUNK,
    FAILED,
    FAMILIES,
    MAX_CHARACTER_CHARS,
    MAX_DECISIONS,
    NO_CHARACTER,
    NO_DECISIONS,
    OK,
    PLAY_STREAM_RE,
    PROMPT,
    RUN_DIR_RE,
    SYSTEM,
    TIMEOUT_S,
    WINDOW_SLACK_S,
    ChatCompletions,
    ClaudeCLI,
    CostCapError,
    JudgeError,
    UnusableProfileError,
    _call_decisions,
    _choice_steps,
    _read_jsonl,
    build_prompt,
    call_client,
    cap,
    client_for,
    clipped,
    dedupe,
    dialogue_blocks,
    find_record,
    judge_chunk,
    judge_decisions,
    parse_verdict,
    persona_field,
    run_streams,
    same_model,
    streams_dir,
)

import profiles

REPO = Path(__file__).resolve().parent.parent
PLAYTHROUGHS = Path(os.environ.get("BG3AI_PLAYTHROUGHS", REPO / "playthroughs"))   # as play.py
DEFAULT_MODEL = os.environ.get("BG3AI_JUDGE_MODEL", "claude-sonnet-5-5")   # pinned: change it and old scores no
                                                                          # longer compare (judge_model is recorded)


def stream_decisions(stream: Path, since: float | None = None, until: float | None = None) -> list[dict]:
    return _core.stream_decisions(stream, since, until, events.load_events)


def extract(state_dir: Path, since: float | None = None, until: float | None = None,
            limit: int = MAX_DECISIONS) -> tuple[list[dict], int]:
    return _core.extract(state_dir, since, until, events.load_events, limit)


def load_character(record: dict | None, session_dir: Path, path: Path | None = None) -> tuple[str | None, str]:
    return _core.load_character(record, session_dir, path, PLAYTHROUGHS)


def judge_run(state_dir: Path, record: dict | None = None, *, client=None, **kw) -> dict:
    """bg3harness.judge.judge_run with the runner's playthroughs, events loader and default judge (claude -p)."""
    return _core.judge_run(state_dir, record, client=client or ClaudeCLI(DEFAULT_MODEL), playthroughs=PLAYTHROUGHS,
                           load=events.load_events, **kw)


# ======================================================================== CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score a run's in-character decisions against CHARACTER.md.")
    ap.add_argument("session_dir", type=Path, help="runtime/sessions/<playthrough> (the series or the run's dir)")
    ap.add_argument("--run", type=int, default=None, help="run number in runs.jsonl (default: the dir's / last)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"judge model via claude -p (default {DEFAULT_MODEL}; "
                                                           "keep it fixed)")
    ap.add_argument("--profile", type=Path, default=None, help="judge with this client profile (profiles/*.toml) "
                                                               "instead of --model")
    ap.add_argument("--max-cost", type=float, default=None, help="USD cap over all judge calls (needed for a paid "
                                                                 "profile)")
    ap.add_argument("--character", type=Path, default=None, help="CHARACTER.md to judge against (default: the "
                                                                 "run's playthrough memory)")
    ap.add_argument("--limit", type=int, default=MAX_DECISIONS)
    ap.add_argument("--chunk", type=int, default=CHUNK)
    ap.add_argument("--allow-same-model", action="store_true", help="judge with the model under test anyway")
    ap.add_argument("--dry-run", action="store_true", help="print the decisions and the prompt; no model call")
    a = ap.parse_args(argv)
    record = find_record(a.session_dir, a.run)
    if record is None and a.run is not None:
        print(f"no run {a.run} in {a.session_dir}/runs.jsonl", file=sys.stderr)
        return 2
    try:
        client = client_for(profiles.load(a.profile), max_cost_usd=a.max_cost) if a.profile else ClaudeCLI(a.model)
    except (profiles.ProfileError, UnusableProfileError) as e:
        print(f"judge: {e}", file=sys.stderr)
        return 2
    if a.dry_run:
        sdir = streams_dir(a.session_dir, record)
        end = (record or {}).get("t")
        decisions, dropped = extract(sdir, (record or {}).get("started"),
                                     end + WINDOW_SLACK_S if end else None, a.limit)
        character, source = load_character(record, sdir, a.character)
        print(f"judge {client.model} run {(record or {}).get('run')} streams {sdir} character {source or 'NONE'}: "
              f"{len(decisions)} decisions ({dropped} dropped)\n")
        print(build_prompt(character or "(none)", decisions, dropped))
        return 0
    res = judge_run(a.session_dir, record, client=client, character_path=a.character, limit=a.limit,
                    chunk=a.chunk, allow_same_model=a.allow_same_model)
    print(json.dumps(res, indent=1, ensure_ascii=False))
    return 0 if res["status"] in (OK, NO_DECISIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
