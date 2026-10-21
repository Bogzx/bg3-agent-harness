"""Run the import-linter contracts of pyproject.toml ([tool.importlinter]) on the packages that exist so far.

The contracts name bg3harness, bg3bench and bg3play before those packages exist (OVERHAUL-PLAN.md phases B-D).
import-linter refuses a root package it can't import, so this drops the missing ones from the config, and with them
every contract that has nothing left to compare, then runs lint-imports on what remains.

Report mode (the default until Phase D): broken contracts are printed and the exit code is 0. Only a run that
didn't get to check the contracts (bad config, import-linter crash) fails. The one switch to enforce is
`enforce-boundaries = true` under [tool.bg3ci] in pyproject.toml: this check (in CI and locally) then fails on a
broken contract, and tools/ci/test_boundaries.py fails on a boundary violation. --enforce forces it for one run.
When all packages exist, the CI step can call `uv run lint-imports` directly and this wrapper can go.

    uv run python tools/ci/import_contracts.py [--enforce]
"""

from __future__ import annotations

import argparse
import configparser
import importlib.util
import io
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
MODULE_LISTS = ("source_modules", "forbidden_modules", "modules", "containers", "layers")


def _present(module: str, is_present: Callable[[str], bool]) -> bool:
    return is_present(module.split(".")[0])


def filter_config(cfg: Mapping[str, Any], is_present: Callable[[str], bool]) -> tuple[dict[str, Any], list[str]]:
    """The config with missing root packages removed, and the names of the contracts dropped with them."""
    roots = [p for p in cfg["root_packages"] if is_present(p)]
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for contract in cfg.get("contracts", []):
        c = dict(contract)
        for key in MODULE_LISTS:
            if key in c:
                c[key] = [m for m in c[key] if _present(m, is_present)]
        if c["type"] == "independence":
            useful = len(c["modules"]) >= 2
        else:
            useful = all(c[key] for key in MODULE_LISTS if key in c)
        if useful:
            kept.append(c)
        else:
            dropped.append(c["name"])
    return {**cfg, "root_packages": roots, "contracts": kept}, dropped


def to_ini(cfg: Mapping[str, Any]) -> str:
    """import-linter's INI form of a [tool.importlinter] table."""
    ini = configparser.ConfigParser()

    def value(v: Any) -> str:
        if isinstance(v, list):
            return "\n" + "\n".join(map(str, cast(list[object], v)))
        return str(v)

    ini["importlinter"] = {k: value(v) for k, v in cfg.items() if k != "contracts"}
    for i, contract in enumerate(cfg["contracts"]):
        ini[f"importlinter:contract:{i}"] = {k: value(v) for k, v in contract.items()}
    out = io.StringIO()
    ini.write(out)
    return out.getvalue()


def _importable(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def enforced(pyproject: Mapping[str, Any]) -> bool:
    """The Phase D switch: [tool.bg3ci] enforce-boundaries (default false); anything but a bool is an error."""
    flag = pyproject.get("tool", {}).get("bg3ci", {}).get("enforce-boundaries", False)
    if not isinstance(flag, bool):
        raise ValueError(f"[tool.bg3ci] enforce-boundaries must be true or false, got {flag!r}")
    return flag


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--enforce", action="store_true", help="exit 1 on a broken contract, whatever pyproject says")
    args = ap.parse_args(argv)
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    enforce = args.enforce or enforced(pyproject)
    cfg = pyproject["tool"]["importlinter"]
    filtered, dropped = filter_config(cfg, _importable)
    missing = sorted(set(cfg["root_packages"]) - set(filtered["root_packages"]))
    if missing:
        print(f"not yet importable, skipped: {', '.join(missing)}")
    for name in dropped:
        print(f"contract skipped (its packages don't exist yet): {name}")
    with tempfile.NamedTemporaryFile("w", suffix=".ini") as f:
        f.write(to_ini(filtered))
        f.flush()
        out = subprocess.run(
            ["lint-imports", "--config", f.name, "--no-cache"], cwd=ROOT, capture_output=True, text=True, check=False
        )
    print(out.stdout + out.stderr)
    if "Contracts: " not in out.stdout:
        print("import-linter did not check the contracts")
        return 2
    if out.returncode and not enforce:
        print(
            "REPORT MODE: broken contracts above don't fail the build until Phase D ([tool.bg3ci] enforce-boundaries)"
        )
        return 0
    return out.returncode


if __name__ == "__main__":
    sys.exit(main())
