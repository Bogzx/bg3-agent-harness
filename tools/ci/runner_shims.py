"""Phase D's end (OVERHAUL-PLAN.md): every runner/*.py shim and every place that still reaches one, so the shims can
be removed in one pass when run_session lands.

A shim is a runner module that only stands in for code that moved into a package:
    alias      `alias(__name__, "<module>")` from runner/_harness.py: `import <name>` is bg3harness.<module> itself
    re-export  a module whose docstring calls itself a shim (runner/judge.py: bg3harness.judge plus runner wiring)

A use of shim <name> is one of:
    import     `import <name>` / `from <name> import ...` / `importlib.import_module("<name>")` /
               `__import__("<name>")` anywhere, unless the importing file's own directory has a <name>.py (a script's
               own directory is sys.path[0]): no shim name is an installed module, so the import resolves only through
               runner/ on sys.path, however it got there (tools/logs/review_session.py's `import triage` gets it from
               series_report). In a package module (bridge/src, packages/*/src) the own-directory exception doesn't
               hold: an absolute import never searches it (bg3harness's `__import__("pi_client")`). And the app's
               `legacy_paths.load("<name>")` (bg3play/adapters/legacy_paths.py puts runner/ on sys.path)
    dotted     `import runner.<name>` / `from runner.<name> import ...` / `from runner import <name>`
    path       a Python string value or path join naming runner/<name>.py (spec_from_file_location, argv, ...)
    mention    runner/<name>.py in a docstring, or in a tracked non-Python text file (docs, shell scripts, configs)

A deleted shim (DELETED) must stay gone: any use of one (an import of it would fail, a mention is stale) fails
--check and tools/ci/test_runner_shims.py, so CI catches the old name coming back.

    uv run python tools/ci/runner_shims.py          # the table: shim -> target, then its uses (file:line kind)
    uv run python tools/ci/runner_shims.py --json   # the same as JSON
    uv run python tools/ci/runner_shims.py --check  # exit 1 on any use of a deleted shim
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = (".md", ".sh", ".toml", ".yml", ".yaml", ".json", ".txt", ".cfg", ".ini")
# Shims removed once nothing used them (research/ovh/found.md round 8): name -> the package module to use instead.
DELETED = {
    "choices": "bg3harness.choices",
    "deepseek_bill": "bg3harness.deepseek_bill",
    "display_env": "bg3harness.display_env",
    "events": "bg3harness.events",
    "facts": "bg3harness.facts",
    "fairness": "bg3harness.fairness",
    "game_settings": "bg3harness.game_settings",
    "host_tuning": "bg3harness.host_tuning",
    "model_time": "bg3harness.model_time",
    "ramwatch": "bg3harness.ramwatch",
    "sandbox": "bg3harness.sandbox",
}


@dataclass(frozen=True)
class Shim:
    name: str  # the module name runner/ gives it
    kind: str  # "alias" or "re-export"
    target: str  # the package module it stands in for


@dataclass(frozen=True)
class Use:
    shim: str
    path: str
    line: int
    kind: str  # "import", "dotted", "path", "mention", or "file" (a deleted shim's own file, deleted_uses)


def find_shim(name: str, source: str) -> Shim | None:
    """The shim a runner/<name>.py source is, or None for a real module."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "alias"
            and len(node.args) == 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            return Shim(name, "alias", f"bg3harness.{node.args[1].value}")
    doc = ast.get_docstring(tree) or ""
    if re.search(r"\bShim\b", doc):
        targets = sorted(
            {
                n.module
                for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0].startswith("bg3")
            }
        )
        return Shim(name, "re-export", ", ".join(targets))
    return None


LOAD = "legacy_paths.load"
PACKAGED = re.compile(r"(bridge/src|packages/[^/]+/src)/")


def python_uses(path: str, source: str, shims: Iterable[str], local: Iterable[str] = ()) -> list[Use]:
    """The uses of the shims in one Python file; `local` are the module names of the file's own directory."""
    names = set(shims)
    mine: set[str] = set() if PACKAGED.match(path) else set(local)  # absolute imports never search a package's dir
    tree = ast.parse(source)
    docs = {  # docstrings and other bare string statements name a path, they don't load it
        id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    }
    out: list[Use] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top, _, rest = a.name.partition(".")
                if top in names and top not in mine:
                    out.append(Use(top, path, node.lineno, "import"))
                if top == "runner" and rest.split(".")[0] in names:
                    out.append(Use(rest.split(".")[0], path, node.lineno, "dotted"))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top, _, rest = node.module.partition(".")
            if top in names and top not in mine:
                out.append(Use(top, path, node.lineno, "import"))
            elif top == "runner" and rest.split(".")[0] in names:
                out.append(Use(rest.split(".")[0], path, node.lineno, "dotted"))
            elif node.module == "runner":
                out.extend(Use(a.name, path, node.lineno, "dotted") for a in node.names if a.name in names)
        elif (
            isinstance(node, ast.Call)
            and ast.unparse(node.func) in ("importlib.import_module", "import_module", "__import__", LOAD)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            top, _, rest = node.args[0].value.partition(".")
            if top in names and top not in mine:
                out.append(Use(top, path, node.lineno, "import"))
            elif top == "runner" and rest.split(".")[0] in names:
                out.append(Use(rest.split(".")[0], path, node.lineno, "dotted"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            kind = "mention" if id(node) in docs else "path"
            out.extend(Use(n, path, node.lineno, kind) for n in names if f"runner/{n}.py" in node.value)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            seg = ast.unparse(node)
            for n in names:
                if re.search(rf"""['"]runner['"]\s*/\s*['"]{n}\.py['"]""", seg):
                    out.append(Use(n, path, node.lineno, "path"))
    return sorted(set(out), key=lambda u: (u.path, u.line, u.shim, u.kind))


def text_uses(path: str, text: str, shims: Iterable[str]) -> list[Use]:
    """runner/<name>.py mentions in a non-Python file."""
    pats = {n: re.compile(rf"(?<![\w/])runner/{n}\.py\b") for n in shims}
    return [
        Use(n, path, i, "mention")
        for i, line in enumerate(text.splitlines(), 1)
        for n, pat in pats.items()
        if pat.search(line)
    ]


def scan(files: Mapping[str, str]) -> tuple[list[Shim], list[Use]]:
    """Shims and uses over {repo path: content} (the shims' own files are not uses of themselves)."""
    shims = [
        s
        for p, src in sorted(files.items())
        if re.fullmatch(r"runner/[a-z_][a-z0-9_]*\.py", p)
        and not p.startswith("runner/test_")
        and (s := find_shim(p[len("runner/") : -3], src))
    ]
    names = [s.name for s in shims]
    own = {f"runner/{n}.py" for n in names}
    uses: list[Use] = []
    for p, src in sorted(files.items()):
        if p in own:
            continue
        if p.endswith(".py"):
            here = p.rpartition("/")[0]
            local = [q.rpartition("/")[2][:-3] for q in files if q.endswith(".py") and q.rpartition("/")[0] == here]
            uses += python_uses(p, src, names, [] if here == "runner" else local)
        elif p.endswith(TEXT_SUFFIXES):
            uses += text_uses(p, src, names)
    return shims, uses


def deleted_uses(files: Mapping[str, str], deleted: Iterable[str] = DELETED) -> list[Use]:
    """Uses of deleted shims over {repo path: content}, scanned as if each shim were still there; a deleted shim's
    file back in runner/ is a use too (kind "file")."""
    names = list(deleted)
    own = {f"runner/{n}.py" for n in names}
    uses = [Use(n, f"runner/{n}.py", 1, "file") for n in names if f"runner/{n}.py" in files]
    for p, src in sorted(files.items()):
        if p in own:
            continue
        if p.endswith(".py"):
            here = p.rpartition("/")[0]
            local = [q.rpartition("/")[2][:-3] for q in files if q.endswith(".py") and q.rpartition("/")[0] == here]
            uses += python_uses(p, src, names, [] if here == "runner" else local)
        elif p.endswith(TEXT_SUFFIXES):
            uses += text_uses(p, src, names)
    return uses


def tracked(root: Path = ROOT) -> dict[str, str]:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout.split()
    files: dict[str, str] = {}
    for p in out:
        if p.startswith("third_party/") or not (p.endswith(".py") or p.endswith(TEXT_SUFFIXES)):
            continue
        try:
            files[p] = (root / p).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="every runner/*.py shim and every use of one")
    ap.add_argument("--json", action="store_true", help="print {shims, uses} as JSON")
    ap.add_argument("--check", action="store_true", help="exit 1 on any use of a deleted shim (DELETED)")
    a = ap.parse_args(argv)
    files = tracked()
    if a.check:
        gone = deleted_uses(files)
        for u in gone:
            print(f"{u.path}:{u.line}: {u.kind} of deleted shim runner/{u.shim}.py: use {DELETED[u.shim]}")
        print(f"deleted shims: {len(gone)} uses" if gone else f"deleted shims: OK ({len(DELETED)} gone, no uses)")
        return 1 if gone else 0
    shims, uses = scan(files)
    if a.json:
        print(json.dumps({"shims": [asdict(s) for s in shims], "uses": [asdict(u) for u in uses]}, indent=1))
        return 0
    for s in shims:
        mine = [u for u in uses if u.shim == s.name]
        print(f"runner/{s.name}.py  {s.kind} -> {s.target}  ({len(mine)} uses)")
        for u in mine:
            print(f"    {u.path}:{u.line}  {u.kind}")
    print(f"{len(shims)} shims, {len(uses)} uses")
    return 0


if __name__ == "__main__":
    sys.exit(main())
