"""Shim: the run records' core lives in packages/harness (bg3harness.runs: failure tagging, the usage meter and caps,
the run metrics and their backfill), the benchmark parts in packages/bench (bg3bench: run naming and the frozen
series settings (series), the per-run resets and the knowledge lock (resets), the version pins (pins), the runs table
and series summary (records), the v1 scenario files (scenario_v1)). Both are re-exported here, so `runs.<name>`
keeps working for play.py and the runner's tests.

Benchmark comparability: every run record carries its version pins (game build, bg3le, mod, repo) and caps (time,
cost, tokens); a series freezes both in series.json from its first run (play.py refuses a drifted later run unless
--allow-version-drift / --allow-cap-change), and the persona judge's model with them (--allow-judge-change).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "bench" / "src"))
import _harness  # noqa: E402,F401  (bridge/src and packages/harness/src on sys.path)
from bg3bench.pins import (  # noqa: E402
    CODE_PATHS,
    DRIFT_KEYS,
    bg3le_pin,
    buildinfo,
    mod_version,
    pin_drift,
    repo_pin,
    tree_digest,
    version_pins,
)
from bg3bench.records import (  # noqa: E402
    COMPARE_METRICS,
    PERSONA_SHORT,
    RESULT_TOK_BASELINE,
    RESULT_TOK_BASELINE_N,
    RESULT_TOK_BASELINE_PER_RESULT,
    RESULT_TOK_BASELINE_TOTAL,
    RESULT_TOK_BETTER,
    RESULT_TOK_WORSE,
    SUMMARY_METRICS,
    best_streak,
    class_counts,
    clean_streak,
    floor_note,
    persona_cell,
    record_adjusted_model_s,
    record_api_floor,
    record_idle,
    record_persona,
    record_result_tokens,
    record_ttm,
    result_tok_cell,
    result_tok_grade,
    runs_table,
    series_compare,
    series_summary,
    stats,
    time_to_milestone,
)
from bg3bench.resets import (  # noqa: E402
    WRITE_TOOLS,
    agent_written,
    archive_nav_state,
    lock_tree,
    restore_agent_edits,
    snapshot_tree,
    tree_changes,
    unlock_tree,
)
from bg3bench.scenario_v1 import (  # noqa: E402
    AGENT_LABEL_RE,
    INVALID,
    NEEDS_SAVE,
    NOTE_FIELDS,
    NOTE_HISTORY_RE,
    NOTE_MAX_CHARS,
    READY,
    RESETS,
    SAVE_FOLDER_RE,
    SCENARIO_KEYS,
    SCENARIO_NAME_RE,
    START_KINDS,
    ScenarioError,
    load_scenario,
    note_text,
    save_digest,
    save_label_of,
    save_lsv,
    save_owner_of,
    scenario_cli_conflicts,
    scenario_digest,
    scenario_freeze,
    scenario_series_check,
    scenario_status,
    start_note,
    validate_scenario,
)
from bg3bench.series import (  # noqa: E402
    CAP_KEYS,
    next_run_index,
    run_name,
    same_record,
    sandbox_mode,
    series_caps,
    series_client,
    series_judge_model,
    series_records,
    series_sandbox,
    update_record,
)
from bg3harness.runs import *  # noqa: E402,F403
