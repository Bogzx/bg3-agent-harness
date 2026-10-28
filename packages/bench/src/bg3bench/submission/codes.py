"""The submission validator's check codes, as data (schema/checks.v1.json is this table written out for the site).

Codes are stable: they are stored with every verdict and shown on a run's page. `where` says which runner performs
the check: `worker` (the site's API, on upload), `job` (the full validation job with the graders), `panel` (the
local panel, before upload). `severity` `reject` rejects the bundle; `flag` accepts it with the named flag shown.

    python -m bg3bench.submission.codes > packages/bench/schema/checks.v1.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

Where = Literal["panel", "worker", "job"]
Severity = Literal["reject", "flag"]

SCHEMA = 1


@dataclass(frozen=True)
class Check:
    code: str
    where: tuple[Where, ...]
    severity: Severity
    summary: str
    flag: str | None = None  # the flag a `flag` check sets
    weekly: bool = False  # re-run on every listed run each week


_PW: tuple[Where, ...] = ("panel", "worker")
_PJ: tuple[Where, ...] = ("panel", "job")

CHECKS: tuple[Check, ...] = (
    Check("F01", _PW, "reject", "the files are exactly those bundle.json lists, sizes match, within the size caps"),
    Check(
        "F02",
        _PW,
        "reject",
        "every file's sha256 equals bundle.json files[]; canonical bundle.json; run_hash recomputes",
    ),
    Check("F03", _PW, "reject", "bundle, manifest, result and every facts/rolls/events line match their schemas"),
    Check("F04", _PW, "reject", "not a pilot, valid, no invalid_reason, not a holdout scenario"),
    Check("P01", _PW, "reject", "released suite; harness/bench versions, scenario digest and start save match it"),
    Check("P02", _PW, "reject", "game build, bg3le sha256 and mod sha1 equal the suite's pins; repo not dirty"),
    Check("C01", _PW, "reject", "prompt sha256 and tool allowlist sha256 equal the condition's"),
    Check("C02", _PJ, "reject", "the tool list names equal the allowlist; the served texts hash to prompt.parts"),
    Check("C03", _PJ, "reject", "caps, seed (and seed applied) and difficulty equal the scenario's"),
    Check("D01", ("worker",), "reject", "no submission with this bundle hash; no accepted one by this user and run id"),
    Check(
        "D02",
        ("job",),
        "flag",
        "the events are not a near-copy of another submission's",
        flag="duplicate_suspect",
    ),
    Check("X01", _PJ, "reject", "the first facts snapshot matches the scenario's start-state fingerprint"),
    Check("X02", _PJ, "reject", "run_start and run_end marks; wall_s equals their distance; events lie between"),
    Check("X03", _PJ, "reject", "acting tool calls have facts counterparts; no load the events don't show"),
    Check("X04", _PJ, "reject", "the rolls agree with the facts; luck recomputed equals result.luck"),
    Check(
        "X05", _PJ, "flag", "tokens equal the events' usage; cost within 25 % of the recomputed", flag="cost_mismatch"
    ),
    Check("X06", _PJ, "reject", "no resumed process counted twice; first_stop_s consistent with the events"),
    Check("X07", ("job",), "flag", "the outcome is not far outside the verified distribution", flag="outlier"),
    Check("G01", _PJ, "reject", "the graders over the submitted facts give the result's verdict, time and score"),
    Check("G02", _PJ, "reject", "deaths and reloads equal the facts' party losses and load marks"),
    Check("A01", _PJ, "reject", "the fairness audit over the tool results shows no leak and agrees with the result"),
    Check("A02", _PJ, "reject", "no tool outside the allowlist; no load_game without the reloads condition"),
    Check(
        "E01",
        ("panel", "worker"),
        "reject",
        "the YouTube video resolves, its title has the run id, embedding is allowed",
        flag="evidence_missing",
        weekly=True,
    ),
    Check("E02", ("job",), "reject", "the video's duration equals render.json's span, which equals wall_s"),
    Check(
        "E03", _PJ, "reject", "render.json is for this run_hash; its (file, seq) events exist, monotone, in the video"
    ),
    Check("E04", _PJ, "reject", "frames.tar holds only frames/<n>.jpg within its cap, one decodable frame per 30 s"),
    Check("S01", _PW, "reject", "no home path, e-mail, Steam ID, key shape or .env value in any file"),
    Check("S02", _PJ, "reject", "no hidden reasoning in the events"),
)

BY_CODE: dict[str, Check] = {c.code: c for c in CHECKS}


def as_json() -> dict[str, Any]:
    """The table as schema/checks.v1.json holds it."""
    return {
        "schema": SCHEMA,
        "checks": [
            {
                "code": c.code,
                "where": list(c.where),
                "severity": c.severity,
                "flag": c.flag,
                "weekly": c.weekly,
                "summary": c.summary,
            }
            for c in CHECKS
        ],
    }


def main() -> None:
    print(json.dumps(as_json(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
