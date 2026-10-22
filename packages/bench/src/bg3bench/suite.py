"""The benchmark-suite version (`bench-vX.Y`): what makes scores comparable.

The suite version is bg3bench's `X.Y` (pyproject.toml; CHANGELOG.md: a release is tag `bench-vX.Y` at bg3bench
`X.Y.0`). It must change whenever a score can change: a scenario file, a grader, the conditions, the statistics or
the invalid-run rule (results.validity). `digest()` hashes those files (SUITE_FILES) and `suite.json` pins the
digests each suite version was made of; `check()` (run by tests/test_suite.py) fails when the files no longer match
a digest pinned for the current version. The fix is one of:

- the change can move a score: bump bg3bench's minor version and pin the new digest under the new suite version;
- it can't (a comment, a refactor, a typo in a scene line the agent never sees): append the digest to the current
  version's list, and bump only the patch version.

Never edit an existing entry: an entry is what a published result was graded with.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bg3bench import __version__

PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # packages/bench
PINS = PACKAGE_ROOT / "suite.json"
SCENARIO_DIR = "scenarios"  # every file under it: the v2 scenarios and their quizzes
# The code that turns a run into a score (bg3bench sources): graders and goal predicates, the choice grader, the
# conditions, the statistics and the invalid-run rule.
SUITE_FILES = (
    "src/bg3bench/graders.py",
    "src/bg3bench/predicates.py",
    "src/bg3bench/choices.py",
    "src/bg3bench/conditions.py",
    "src/bg3bench/stats.py",
    "src/bg3bench/results.py",
)


def suite_version(bench_version: str = __version__) -> str:
    """`bench-vX.Y` of a bg3bench version `X.Y.Z`."""
    major, minor, *_ = bench_version.split(".")
    return f"bench-v{major}.{minor}"


def suite_files(root: Path = PACKAGE_ROOT) -> list[str]:
    """The suite's files relative to root, sorted (scenario files found, code files listed)."""
    scenarios = sorted(p.relative_to(root).as_posix() for p in (root / SCENARIO_DIR).rglob("*") if p.is_file())
    return sorted([*scenarios, *SUITE_FILES])


def digest(root: Path = PACKAGE_ROOT) -> str:
    """sha256 over each suite file's path and sha256: any added, removed, renamed or edited file changes it."""
    h = hashlib.sha256()
    for rel in suite_files(root):
        h.update(f"{rel}\0{hashlib.sha256((root / rel).read_bytes()).hexdigest()}\n".encode())
    return h.hexdigest()


def load_pins(path: Path = PINS) -> dict[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    pins: dict[str, list[str]] = data["versions"]
    return pins


def check(root: Path = PACKAGE_ROOT, pins: dict[str, list[str]] | None = None, version: str | None = None) -> list[str]:
    """What is wrong with the pins: the current files' digest not pinned for the current suite version, or one
    digest pinned for two versions. [] = the suite version names exactly these files."""
    pins = load_pins(root / PINS.name) if pins is None else pins
    version = suite_version() if version is None else version
    errors: list[str] = []
    now = digest(root)
    if now not in pins.get(version, []):
        other = [v for v, ds in pins.items() if now in ds]
        errors.append(
            f"suite files changed without a suite version: digest {now} is not pinned for {version}"
            + (f" (it is {other[0]}'s)" if other else "")
            + f"; see {Path(__file__).name}'s docstring for the fix"
        )
    seen: dict[str, str] = {}
    for v, ds in pins.items():
        for d in ds:
            if d in seen and seen[d] != v:
                errors.append(f"digest {d} is pinned for both {seen[d]} and {v}")
            seen.setdefault(d, v)
    return errors
