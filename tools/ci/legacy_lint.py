"""The legacy lint lists only shrink (OVERHAUL-PLAN.md D10).

Legacy files are exempt from parts of the strict tooling through three lists:

- ruff.toml `[lint.per-file-ignores]`: per legacy file, the rule codes it still breaks;
- ruff.toml `[format] exclude`: legacy files not yet in ruff format;
- pyrightconfig.json `ignore`: legacy files with pyright (basic) errors.

This check reruns each tool with its list emptied and fails when a list holds more than reality needs: a file that
no longer breaks a listed rule, is formatted now, or has no pyright errors must leave the list. Growing a list is
caught by the tools themselves (ruff / pyright fail on the new violation), and reviewers see it in the diff.

    uv run python tools/ci/legacy_lint.py            # check all three lists
    uv run python tools/ci/legacy_lint.py --needed   # print what the lists must hold today (for shrinking them)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def stale_ignores(recorded: Mapping[str, Sequence[str]], needed: Mapping[str, set[str]]) -> list[str]:
    """Problems with the per-file ignore list: listed codes a file no longer breaks, listed files that are clean."""
    problems: list[str] = []
    for path, codes in sorted(recorded.items()):
        extra = sorted(set(codes) - needed.get(path, set()))
        if extra and set(extra) == set(codes):
            problems.append(f"ruff per-file-ignores: {path} is clean now: remove its entry")
        elif extra:
            problems.append(f"ruff per-file-ignores: {path} no longer breaks {', '.join(extra)}: remove them")
    return problems


def stale_paths(kind: str, recorded: Sequence[str], needed: set[str]) -> list[str]:
    """Problems with a plain list of exempt files: the listed ones that no longer need the exemption."""
    return [f"{kind}: {path} no longer needs the exemption: remove it" for path in sorted(set(recorded) - needed)]


def _rel(path: str) -> str:
    return Path(path).resolve().relative_to(ROOT).as_posix()


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)


def _ruff_check(ignores: Mapping[str, Sequence[str]]) -> list[dict[str, str]]:
    table = ", ".join(f"{json.dumps(k)} = {json.dumps(list(v))}" for k, v in ignores.items())
    out = _run(
        [
            "ruff",
            "check",
            "--config",
            f"lint.per-file-ignores = {{{table}}}",
            "--output-format",
            "json",
            "--exit-zero",
            ".",
        ]
    )
    if out.returncode:
        raise RuntimeError(f"ruff check failed: {out.stderr}")
    return json.loads(out.stdout)


def ruff_needed(recorded: Mapping[str, Sequence[str]]) -> dict[str, set[str]]:
    """Rule codes each file breaks with no per-file ignores. RUF100 (unused noqa) is the exception: a noqa for a
    rule the file ignores is unused only because of the ignore, so it is needed when the recorded ignores minus
    RUF100 produce it."""
    needed: dict[str, set[str]] = {}
    for d in _ruff_check({}):
        needed.setdefault(_rel(d["filename"]), set()).add(d["code"] or "syntax")
    for codes in needed.values():
        codes.discard("RUF100")
    for d in _ruff_check({k: [c for c in v if c != "RUF100"] for k, v in recorded.items()}):
        if d["code"] == "RUF100":
            needed.setdefault(_rel(d["filename"]), set()).add("RUF100")
    return needed


def format_needed() -> set[str]:
    """Files ruff format would change with no format exclude list."""
    out = _run(["ruff", "format", "--check", "--config", "format.exclude = []", "--output-format", "json", "."])
    if out.returncode > 1:
        raise RuntimeError(f"ruff format failed: {out.stderr}")
    return {_rel(d["filename"]) for d in json.loads(out.stdout or "[]")}


def pyright_needed(config: Mapping[str, object]) -> set[str]:
    """Files with pyright errors under the repo's config with an empty ignore list. The temporary config sits in
    the repo root, so every relative path in it means what it means in pyrightconfig.json."""
    cfg = {k: v for k, v in config.items() if k != "ignore"}
    with tempfile.NamedTemporaryFile("w", dir=ROOT, prefix=".pyright-legacy-", suffix=".json") as f:
        json.dump(cfg, f)
        f.flush()
        out = _run(["pyright", "-p", f.name, "--outputjson"])
    if out.returncode > 1:
        raise RuntimeError(f"pyright failed: {out.stdout[-2000:]}{out.stderr}")
    report = json.loads(out.stdout)
    return {_rel(d["file"]) for d in report["generalDiagnostics"] if d["severity"] == "error"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--needed", action="store_true", help="print what each list must hold today")
    args = ap.parse_args(argv)
    ruff_cfg = tomllib.loads((ROOT / "ruff.toml").read_text())
    pyright_cfg = json.loads((ROOT / "pyrightconfig.json").read_text())
    recorded = ruff_cfg.get("lint", {}).get("per-file-ignores", {})
    ruff, fmt, pyr = ruff_needed(recorded), format_needed(), pyright_needed(pyright_cfg)
    if args.needed:
        print("[lint.per-file-ignores]")
        for path, codes in sorted(ruff.items()):
            print(f'"{path}" = {json.dumps(sorted(codes))}')
        print("\n[format] exclude =", json.dumps(sorted(fmt), indent=4))
        print("\npyright ignore =", json.dumps(sorted(pyr), indent=4))
        return 0
    problems = stale_ignores(recorded, ruff)
    problems += stale_paths("ruff format exclude", ruff_cfg.get("format", {}).get("exclude", []), fmt)
    problems += stale_paths("pyright ignore", pyright_cfg.get("ignore", []), pyr)
    for p in problems:
        print(p)
    print(f"legacy lists: {len(problems)} stale entries" if problems else "legacy lists: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
