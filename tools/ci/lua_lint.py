"""luacheck on the mod's Lua and its offline Lua tests, with a baseline that only shrinks (like legacy_lint.py).

The config is the repo's .luacheckrc; luacheck runs pinned in Docker (tools/ci/luacheck.Dockerfile, built once and
tagged by the Dockerfile's hash). The mod and the tests are two luacheck runs: the tests define their stubs as
top-level globals, which must never count as defined for the mod.

tools/ci/luacheck_baseline.json holds, per file and warning code, how many warnings the file had when the lint came
in. The check fails when a file has more of a code than the baseline (a new warning: fix it) and when it has fewer
(the baseline must shrink to match). So the baseline only gets shorter, and nothing joins it except by an edit a
reviewer sees.

    python3 tools/ci/lua_lint.py            # check (tools/ci/lua_suites.sh runs it)
    python3 tools/ci/lua_lint.py --needed   # print the baseline today's warnings need (for shrinking it)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = Path(__file__).with_name("luacheck.Dockerfile")
BASELINE = Path(__file__).with_name("luacheck_baseline.json")
VERSION = 1
MOD = "mod/Mods/BG3AI/ScriptExtender/Lua"
TESTS = "tools/dev"
LINE = re.compile(r"^(?P<path>.+?):\d+:\d+: \((?P<code>[EW]\d+)\) ")

Counts = dict[str, dict[str, int]]
Diff = tuple[str, str, int, int]
# argv -> (exit code, stdout, stderr)
Runner = Callable[[Sequence[str]], tuple[int, str, str]]


def parse(output: str) -> tuple[Counts, dict[tuple[str, str], list[str]]]:
    """luacheck's plain output (with --codes) -> warning counts per file and code, and the lines behind each."""
    counts: Counts = {}
    lines: dict[tuple[str, str], list[str]] = {}
    for line in output.splitlines():
        m = LINE.match(line)
        if m:
            path, code = m["path"], m["code"]
            counts.setdefault(path, {})[code] = counts.get(path, {}).get(code, 0) + 1
            lines.setdefault((path, code), []).append(line)
    return counts, lines


def compare(baseline: Mapping[str, Mapping[str, int]], current: Mapping[str, Mapping[str, int]]) -> list[Diff]:
    """(path, code, baseline count, current count) wherever the two differ: more is a new warning, fewer a stale
    baseline entry."""
    diffs: list[Diff] = []
    for path in sorted(set(baseline) | set(current)):
        was, now = baseline.get(path, {}), current.get(path, {})
        diffs += [
            (path, c, was.get(c, 0), now.get(c, 0))
            for c in sorted(set(was) | set(now))
            if was.get(c, 0) != now.get(c, 0)
        ]
    return diffs


def describe(diff: Diff) -> str:
    path, code, b, n = diff
    if n > b:
        return f"{path}: {n} {code} warnings, the baseline allows {b}: fix the new ones"
    return f"{path}: {code} down to {n} from {b}: lower the baseline (--needed)"


def load_baseline(text: str) -> Counts:
    data = json.loads(text)
    if data.get("version") != VERSION:
        raise ValueError(f"luacheck baseline: version {data.get('version')!r}, expected {VERSION}")
    return data["warnings"]


def dump_baseline(counts: Mapping[str, Mapping[str, int]]) -> str:
    warnings = {p: dict(sorted(c.items())) for p, c in sorted(counts.items()) if c}
    return json.dumps({"version": VERSION, "warnings": warnings}, indent=1) + "\n"


def image_tag(dockerfile: str) -> str:
    return "bg3ai-luacheck:" + hashlib.sha256(dockerfile.encode()).hexdigest()[:12]


def _run(argv: Sequence[str]) -> tuple[int, str, str]:
    out = subprocess.run(list(argv), cwd=ROOT, capture_output=True, text=True, check=False)
    return out.returncode, out.stdout, out.stderr


def ensure_image(run: Runner, dockerfile: str) -> str:
    """The luacheck image's tag, built from the Dockerfile (no context) when it isn't there yet."""
    tag = image_tag(dockerfile)
    if run(["docker", "image", "inspect", tag])[0] != 0:
        rc, out, err = run(["sh", "-c", f"docker build -q -t {tag} - < {DOCKERFILE.relative_to(ROOT)}"])
        if rc:
            raise RuntimeError(f"building the luacheck image failed:\n{out}{err}")
    return tag


def luacheck(run: Runner, tag: str, paths: Iterable[str]) -> str:
    """luacheck's plain output for these repo-relative paths, run in the image on a read-only mount of the repo."""
    argv = ["docker", "run", "--rm", "-v", f"{ROOT}:/repo:ro", "-w", "/repo", tag]
    rc, out, err = run([*argv, "luacheck", "--formatter", "plain", "--codes", "--no-color", *paths])
    if rc > 2:  # 0 clean, 1 warnings, 2 errors (a syntax error is a reported E-code); more is luacheck failing
        raise RuntimeError(f"luacheck failed (exit {rc}):\n{err}{out}")
    return out


def lua_tests(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in (root / TESTS).glob("test_*.lua"))


def current(run: Runner, dockerfile: str, root: Path = ROOT) -> tuple[Counts, dict[tuple[str, str], list[str]]]:
    tag = ensure_image(run, dockerfile)
    output = luacheck(run, tag, [MOD]) + luacheck(run, tag, lua_tests(root))
    return parse(output)


def main(argv: list[str] | None = None, run: Runner = _run, root: Path = ROOT) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--needed", action="store_true", help="print the baseline today's warnings need")
    args = ap.parse_args(argv)
    counts, lines = current(run, DOCKERFILE.read_text(), root)
    if args.needed:
        print(dump_baseline(counts), end="")
        return 0
    problems = compare(load_baseline(BASELINE.read_text()), counts)
    for path, code, b, n in problems:
        print(describe((path, code, b, n)))
        for line in lines.get((path, code), []) if n > b else []:
            print("    " + line)
    total = sum(sum(c.values()) for c in counts.values())
    print(f"luacheck: {len(problems)} problems" if problems else f"luacheck: OK ({total} baselined warnings)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
