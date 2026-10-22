# pyright: basic
"""Code and game health between sessions (moved from runner/play.py, Phase D.3): the mod snapshot and
its hashes, CodeWatch (the runner restarts itself when its own code changed), the execv handover, HealthTracker (a
hang is HEALTH_FAILURES failed liveness evals), the save list and tools/dev/check_all.py's between-session check.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from bg3ai.luadebug import LuaDebugError

from bg3harness import bg3le_buildinfo, steam_paths
from bg3harness.game import controller as gamectl
from bg3harness.game import io as game
from bg3harness.game.io import Save
from bg3harness.game.saves import save_lsv
from bg3harness.runner import env as renv

MOD_DIR = renv.REPO / "mod"
# The runner's game loads the mod from this copy of mod/ (launch_game.sh --mod-path), refreshed only at a session
# boundary: S090 loaded the Lua of a merge into main that landed 19 s after the session's bridge started.
MOD_LIVE = renv.RUNTIME / "mod-live"
RELOAD_TIMEOUT = 60


def mod_tree_hashes(root: Path | None = None) -> dict[str, str]:
    """{relative path: sha1} for every file under root (default MOD_LIVE: the Lua tree the runner's game loads)."""
    root = MOD_LIVE if root is None else root
    out = {}
    for p in sorted(root.rglob("*")) if root.is_dir() else []:
        if p.is_file() and "__pycache__" not in p.parts:
            out[str(p.relative_to(root))] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def changed_files(old: dict | None, new: dict) -> list[str]:
    """Files added, removed or edited between two mod_tree_hashes; everything when old is unknown."""
    if old is None:
        return sorted(new)
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def sync_tree(src: Path, dst: Path) -> list[str]:
    """Make dst hold exactly src's files (no __pycache__), in place: changed files are rewritten through a temp file
    and a rename, extra ones removed. Never a swapped directory: launch_game.sh shares the mod path into the game's
    container by itself, so a new dst directory would leave the running game on the old one. -> the paths changed."""
    want = mod_tree_hashes(src)
    changed = changed_files(mod_tree_hashes(dst), want)
    for rel in changed:
        p = dst / rel
        if rel not in want:
            p.unlink(missing_ok=True)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.sync")
        shutil.copyfile(src / rel, tmp)
        tmp.replace(p)
    for d in sorted((p for p in dst.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    return changed


def repo_commit(repo: Path = renv.REPO, paths: tuple[str, ...] = ("mod", "bridge/src")) -> str:
    """HEAD's short sha, "+dirty" when paths have uncommitted changes (the working tree is what the bridge runs and
    the mod snapshot copies), "unknown" without git."""
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=9", "HEAD"], capture_output=True,
                              text=True, timeout=10)
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", *paths], capture_output=True,
                               text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if head.returncode != 0 or not head.stdout.strip():
        return "unknown"
    return head.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")


def game_mod_path(proc: Path = Path("/proc")) -> str | None:
    """BG3LE_MOD_PATH of the running harness game (its environ): where it loads the mod from. None: no game, or
    none it reads (no bg3le, or an unreadable environ)."""
    for pid in game.pids_of_game():
        try:
            env = (proc / str(pid) / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        for kv in env:
            if kv.startswith(b"BG3LE_MOD_PATH="):
                return kv.split(b"=", 1)[1].decode(errors="replace")
    return None


BRIDGE_SRC = renv.REPO / "bridge" / "src"
HANDOVER = renv.RUNTIME / "runner-handover.json"   # state the runner hands to its own execv'd successor
HANDOVER_MAX_AGE = 300


def _sha1(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def loaded_code_files(repo: Path = renv.REPO, modules=None) -> list[Path]:
    """Source files of the repo modules loaded in this process: runner/*.py plus what runs in-process from
    tools/ and bridge/src (metrics, progress, skipper, ...). Never a venv or site-packages."""
    out = set()
    for m in list((modules if modules is not None else sys.modules).values()):
        f = getattr(m, "__file__", None)
        if not f or not f.endswith(".py"):
            continue
        p = Path(f).resolve()
        if p.is_relative_to(repo) and not any(x in p.parts for x in (".venv", "site-packages", "__pycache__")):
            out.add(p)
    return sorted(out)


class CodeWatch:
    """Remembers each loaded repo module's hash when first seen (at start, or on a later lazy import) and
    reports the ones edited on disk since: that code is stale in this process."""

    def __init__(self, files=None):
        self.hashes: dict[Path, str | None] = {}
        self.track(files)

    def track(self, files=None) -> None:
        for p in loaded_code_files() if files is None else files:
            self.hashes.setdefault(p, _sha1(p))

    def changed(self, files=None) -> list[Path]:
        self.track(files)
        return [p for p, h in self.hashes.items() if _sha1(p) != h]


def tree_hashes(root: Path, pattern: str = "*.py") -> dict[str, str]:
    """{relative path: sha1} for the files matching pattern under root (the bridge's source tree)."""
    out = {}
    for p in sorted(root.rglob(pattern)) if root.is_dir() else []:
        if p.is_file() and "__pycache__" not in p.parts:
            out[str(p.relative_to(root))] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def code_check_cmd(script: Path) -> list[str]:
    """Imports the new runner the way a restart would (argparse --help exits 0 after every top-level import)."""
    return [sys.executable, str(script), "run", "--help"]


def restart_argv() -> list[str]:
    """Same interpreter, flags and arguments (cwd is unchanged by execv, so relative paths still hold)."""
    return [sys.executable, *getattr(sys, "orig_argv", [sys.executable, *sys.argv])[1:]]


def write_handover(data: dict, path: Path | None = None) -> None:
    path = path or HANDOVER
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**data, "pid": os.getpid(), "t": time.time()}))
    tmp.replace(path)


def read_handover(path: Path | None = None, now: float | None = None) -> dict | None:
    """The handover the previous incarnation of this process wrote (same pid, fresh); always consumed."""
    path = path or HANDOVER
    try:
        return peek_handover(path, now)
    finally:
        path.unlink(missing_ok=True)


def peek_handover(path: Path | None = None, now: float | None = None) -> dict | None:
    """read_handover without consuming it: is this process the execv'd successor of a runner mid-run?"""
    try:
        data = json.loads((path or HANDOVER).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("pid") != os.getpid():
        return None
    if (now or time.time()) - data.get("t", 0) > HANDOVER_MAX_AGE:
        return None
    return data


class RestartRunner(Exception):
    """Raised between sessions when the runner's own code changed on disk: cmd_run execs the new code."""


class HealthTracker:
    """Counts consecutive failed liveness checks; `hung` once the limit is reached."""

    def __init__(self, limit: int = renv.HEALTH_FAILURES):
        self.limit = limit
        self.failures = 0

    def record(self, ok: bool) -> None:
        self.failures = 0 if ok else self.failures + 1

    @property
    def hung(self) -> bool:
        return self.failures >= self.limit


# What a session's setup or recovery may raise when the game or the mod misbehaves: retried, never fatal.
SETUP_ERRORS = (RuntimeError, OSError, TimeoutError, LuaDebugError)


def steam_ready() -> tuple[bool, str]:
    return gamectl.steam_ready(steam_paths)


def bg3le_problems() -> list[str]:
    return gamectl.bg3le_problems(bg3le_buildinfo.problems, bg3le_buildinfo.DIST)


def list_saves() -> list[Save]:
    return gamectl.list_saves(renv.SAVE_DIR, save_lsv)


def memory_lint_errors(report: str, skip: tuple[str, ...] = ()) -> list[str]:
    """The ERROR lines of a tools/memory/lint.py report ("ERROR  <check>  <message>"), minus the checks in `skip`."""
    out = []
    for line in report.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] == "ERROR" and parts[1] not in skip:
            out.append(line.strip())
    return out


def until_stop_handoff_only(reason: str | None, until: bool, report: str) -> bool:
    """The session ended on the --until milestone and its end lint failed only on the handoff check (no wrap-up
    ran, by design): no fix-notes session. Any other ERROR (a real notes problem), or a report without ERROR lines
    (session.py itself failed), still gets one."""
    return (reason == "milestone" and until and bool(memory_lint_errors(report))
            and not memory_lint_errors(report, skip=("handoff",)))


def check_all_cmd(json_path: Path) -> list[str]:
    """check_all imports the MCP server (bridge deps): through uv's bridge env when uv is there."""
    uv = shutil.which("uv")
    py = [uv, "run", "--quiet", "--project", str(renv.REPO / "bridge"), "python"] if uv else [sys.executable]
    return py + [str(renv.CHECK_ALL), "--json", str(json_path)]


def check_plans_cmd(json_path: Path, mem: Path) -> list[str]:
    uv = shutil.which("uv")
    py = [uv, "run", "--quiet", "--project", str(renv.REPO / "bridge"), "python"] if uv else [sys.executable]
    return py + [str(renv.CHECK_PLANS), "--json", str(json_path), "--memory", str(mem)]


def plan_check_result(rows: list[dict]) -> tuple[str, list[dict]]:
    """check_plans rows → (log text, bad lines in the keeper's refused-line format {name, level, problems})."""
    bad = [r for r in rows if r.get("problems")]
    out = [{"name": r["name"], "level": r["level"],
            "problems": (f"check_plans between sessions (the game's offers): {'; '.join(r['problems'])} | stored line: "
                         f"{json.dumps(r.get('plan')) if r.get('plan') is not None else '(none)'} | offers: {r.get('offers')}")[:1500]}
           for r in bad]
    if not bad:
        return f"plan lines: {len(rows)} checked, all valid against the game's offers", out
    return (f"plan lines: {len(bad)} of {len(rows)} INVALID: "
            + "; ".join(f"{r['name']} L{r['level']}: {', '.join(r['problems'])}" for r in bad))[:600], out


def plan_note(bad: list[dict]) -> str:
    return ("RUNNER NOTE: these level-up plan lines in memory/party-build.md don't match what the game will offer; "
            "fix each line (picks only from the offers listed) before that level-up:\n"
            + "\n".join(f"- {b['name']} L{b['level']}: {b['problems']}" for b in bad))


def check_all_due(last: float | None, now: float, interval: float = renv.CHECK_ALL_INTERVAL) -> bool:
    return last is None or now - last >= interval


def check_all_result(rc: int | None, out: str, report: dict | None) -> dict:
    """{ok, counts, line, rows, why} from check_all's exit code, stdout and --json report. ok=False: no verdict
    (refused, game unreachable, timed out or crashed), which doesn't count as a run for the 30-min spacing."""
    if report and isinstance(report.get("counts"), dict):
        c = {k: int(report["counts"].get(k, 0)) for k in ("PASS", "WARN", "FAIL", "SKIP")}
        line = f"check_all: {c['PASS']} pass, {c['WARN']} warn, {c['FAIL']} fail" + (
            f", {c['SKIP']} skip" if c["SKIP"] else "")
        return {"ok": True, "counts": c, "line": line, "rows": report.get("rows") or [], "why": None}
    why = {None: f"timed out after {renv.CHECK_ALL_TIMEOUT} s", 2: "game/mod not reachable",
           3: "refused: a session is playing"}.get(rc)
    if why is None:
        tail = (out.strip().splitlines() or ["no output"])[-1][:160]
        why = f"exit {rc}: {tail}"
    return {"ok": False, "counts": None, "line": f"check_all: no verdict ({why})", "rows": [], "why": why}


def _cell(text, limit: int = 300) -> str:
    return str(text)[:limit].replace("|", "\\|").replace("\n", " ")


def check_all_markdown(res: dict, when: float, secs: float, label: str = "") -> str:
    """The section appended to the finished session's Sxxx.triage.md."""
    verdict = res["line"].removeprefix("check_all: ")
    head = f"\n## check_all {label}({datetime.fromtimestamp(when):%H:%M}, {secs:.0f} s): {verdict}\n"
    if not res["ok"]:
        return head
    out = [head, "| check | verdict | secs | detail |", "|---|---|---|---|"]
    for r in res["rows"]:
        mark = {"FAIL": "**FAIL**"}.get(r.get("verdict"), r.get("verdict"))
        out.append(f"| {r.get('name')} | {mark} | {r.get('secs', '')} | {_cell(r.get('detail', ''))} |")
        out += [f"| | | | {_cell(line, 150)} |" for line in (r.get("lines") or [])[1:4]]
    return "\n".join(out) + "\n"
