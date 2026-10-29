# pyright: basic
"""The website's catalog of a released suite (`<suite>/catalog.json` in the saves bucket, next to suite.json and
results.json): what the board shows about each scenario and each model's display name.

    {"scenarios": [{"id", "title", "family", "mode", "cap_min", "seed", "difficulty", "holdout", "task"}],
     "models": [{"client", "slug", "name"}]}

The scenarios are the release file's (every non-holdout scenario of the suite), read from their scenario files:
`mode` is "maximise" for a maximise scenario, else "pass" (a choice scene too: the board leaves the choice family out
of the suite score itself), `cap_min` the time cap in minutes, `seed` and `difficulty` the game settings'. The models
are the leaderboard's rows: one per (client, model slug) of the profiles (profiles/*.toml; an unsupported profile and
a results.PILOT_CLIENTS one, whose runs never enter a release, left out), named from packages/bench/models.json by
slug; a slug with no name there is refused. Offline: uploading it is a later step.

    python -m bg3bench.submission.catalog [--suite bench-v0.8] [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bg3harness import profiles

from bg3bench import results, suite
from bg3bench.submission import pack, release

Json = dict[str, Any]

NAMES = suite.PACKAGE_ROOT / "models.json"
PROFILES = suite.PACKAGE_ROOT.parents[1] / "profiles"


class CatalogError(ValueError):
    pass


def scenario_entry(sc: Json) -> Json:
    settings = sc.get("game_settings") or {}
    minutes = sc["caps"]["run_hours"] * 60
    return {
        "id": sc["name"],
        "title": sc["title"],
        "family": sc["family"],
        "mode": "maximise" if "maximise" in sc else "pass",
        "cap_min": int(minutes) if float(minutes).is_integer() else minutes,
        "seed": settings.get("seed"),
        "difficulty": settings.get("difficulty"),
        "holdout": bool(sc.get("holdout")),
        "task": sc["task"],
    }


def load_names(path: Path = NAMES) -> dict[str, str]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != 1:
        raise CatalogError(f"{path}: not a v1 model names file")
    return dict(doc["models"])


def model_rows(root: Path = PROFILES) -> list[tuple[str, str]]:
    """Every (client, model slug) of the profiles, an unsupported or a PILOT_CLIENTS profile left out."""
    loaded = [profiles.load(p) for p in sorted(root.glob("*.toml"))]
    keep = [p for p in loaded if p.status != "unsupported" and p.client not in results.PILOT_CLIENTS]
    return sorted({(p.client, p.model) for p in keep})


def build(rel: Json, scenarios: Mapping[str, Json], rows: Sequence[tuple[str, str]], names: Mapping[str, str]) -> Json:
    missing = [s for s in rel["scenarios"] if s not in scenarios]
    if missing:
        raise CatalogError(f"{rel['suite']}: no scenario file for {', '.join(missing)}")
    unnamed = sorted({s for _, s in rows if s not in names})
    if unnamed:
        raise CatalogError(f"no display name in {NAMES.name} for {', '.join(unnamed)}")
    return {
        "scenarios": [scenario_entry(scenarios[s]) for s in rel["scenarios"]],
        "models": [{"client": c, "slug": s, "name": names[s]} for c, s in rows],
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m bg3bench.submission.catalog", description=(__doc__ or "").splitlines()[0]
    )
    ap.add_argument("--suite", help="the suite tag (default: the installed suite)")
    ap.add_argument("--out", type=Path, help="default: stdout")
    a = ap.parse_args(argv)
    path = release.default_path(a.suite)
    try:
        rel = json.loads(path.read_text(encoding="utf-8"))
        doc = build(rel, pack.load_scenarios(), model_rows(), load_names())
    except (OSError, CatalogError, profiles.ProfileError) as e:
        print(f"catalog: {e}", file=sys.stderr)
        return 2
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
