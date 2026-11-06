"""Fairness commits on one long-lived branch with no twin on the other (website repo
ops/logs/REVIEW-16-architecture.md A3). Read-only git; nothing is checked out, rebased or written.

    uv run python tools/dev/twins.py <bench-ref> <arena-ref> [--repo PATH]

Both sides are the non-merge commits since the two refs' merge-base. A commit counts when it touches a fairness file
(FAIRNESS: the mod's Visibility*, Guard*, Events, Referee, RollLog and State* Lua; the bridge's render.py, memtools.py,
fairness.py and capabilities.py). Its twin on the other side is one of:
- a commit with the same patch-id (`git patch-id --stable`: a cherry-pick, the same change);
- a commit whose message cites its sha (7+ hex characters, a hand port: "Arena twin of bp/spell-info-2 (effd173a)");
- a commit its own message cites (it is the port of that commit);
- a commit with the same subject line, at least SUBJECT_MIN characters (a hand port that kept the message; its
  patch-id differs by context lines: arena 9135cea1 of bench d288f4be).
A cited sha counts only if it is a commit on the other side. Printed: per side, the orphans (sha, date, subject, the
fairness files it touches), then the counts. Exit 0 either way; 2 on a git error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

FAIRNESS = (
    "**/Visibility*.lua",
    "**/Guard*.lua",
    "**/Events.lua",
    "**/Referee.lua",
    "**/RollLog.lua",
    "**/State*.lua",
    "**/render.py",
    "**/memtools.py",
    "**/fairness.py",
    "**/capabilities.py",
)
SUBJECT_MIN = 40  # shorter subjects ("readme", "review fixes") recur without being the same change
SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


class GitError(Exception):
    pass


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


@dataclass
class Commit:
    sha: str
    date: str
    subject: str
    message: str
    files: list[str] = field(default_factory=list)  # the fairness files it touches
    patch_id: str | None = None


def side(repo: Path, base: str, ref: str) -> dict[str, Commit]:
    """sha -> Commit for every non-merge commit in base..ref (oldest first), with its fairness files and patch-id."""
    out: dict[str, Commit] = {}
    log = git(repo, "log", "--no-merges", "--reverse", "--format=%H%x1f%cs%x1f%s%x1f%B%x1e", f"{base}..{ref}")
    for rec in filter(str.strip, log.split("\x1e")):
        sha, date, subject, message = rec.strip("\n").split("\x1f", 3)
        out[sha] = Commit(sha, date, subject, message)
    touched = git(
        repo, "log", "--no-merges", "--format=%x1e%H", "--name-only", f"{base}..{ref}", "--",
        *(f":(glob){g}" for g in FAIRNESS),
    )  # fmt: skip
    for rec in filter(str.strip, touched.split("\x1e")):
        sha, *files = [x for x in rec.splitlines() if x.strip()]
        out[sha].files = files
    patches = git(repo, "log", "--no-merges", "-p", "--format=commit %H", f"{base}..{ref}")
    for line in git(repo, "patch-id", "--stable", stdin=patches).splitlines():
        pid, sha = line.split()
        out[sha].patch_id = pid
    return out


def cites(message: str, commits: dict[str, Commit]) -> set[str]:
    """The commits of `commits` whose sha the message cites (a 7+ character prefix)."""
    out = set()
    for tok in SHA_RE.findall(message):
        out |= {sha for sha in commits if sha.startswith(tok)}
    return out


def twin(c: Commit, other: dict[str, Commit], cited_by: dict[str, set[str]]) -> str | None:
    """How `c` has a twin on the other side ("patch-id <sha>", "cited by <sha>", "cites <sha>", "subject <sha>"), or
    None."""
    if c.patch_id:
        same = next((o.sha for o in other.values() if o.patch_id == c.patch_id), None)
        if same:
            return f"patch-id {same[:8]}"
    if cited_by.get(c.sha):
        return f"cited by {min(cited_by[c.sha])[:8]}"
    if mine := cites(c.message, other):
        return f"cites {min(mine)[:8]}"
    if len(c.subject) >= SUBJECT_MIN:
        same = next((o.sha for o in other.values() if o.subject == c.subject), None)
        if same:
            return f"subject {same[:8]}"
    return None


@dataclass
class Result:
    base: str
    orphans: dict[str, list[Commit]]  # "bench" / "arena" -> its fairness commits with no twin
    fairness: dict[str, int]  # side -> its fairness commits
    commits: dict[str, int]  # side -> its non-merge commits


def orphans(repo: Path, bench: str, arena: str) -> Result:
    base = git(repo, "merge-base", bench, arena).strip()
    sides = {"bench": side(repo, base, bench), "arena": side(repo, base, arena)}
    out: dict[str, list[Commit]] = {}
    for name, mine in sides.items():
        other = sides["arena" if name == "bench" else "bench"]
        cited_by: dict[str, set[str]] = {}
        for o in other.values():
            for sha in cites(o.message, mine):
                cited_by.setdefault(sha, set()).add(o.sha)
        out[name] = [c for c in mine.values() if c.files and twin(c, other, cited_by) is None]
    return Result(
        base,
        out,
        {n: sum(1 for c in s.values() if c.files) for n, s in sides.items()},
        {n: len(s) for n, s in sides.items()},
    )


def report(r: Result, bench: str, arena: str) -> str:
    lines = [f"# Fairness twins: {bench} vs {arena} (merge-base {r.base[:8]})", ""]
    for name, ref in (("bench", bench), ("arena", arena)):
        lines.append(
            f"## {ref}: {len(r.orphans[name])} orphan(s) of {r.fairness[name]} fairness commits "
            f"({r.commits[name]} non-merge commits)"
        )
        for c in r.orphans[name]:
            lines.append(f"- {c.sha[:8]} {c.date} {c.subject[:150]}")
            lines.append(f"  - files: {', '.join(c.files)}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fairness commits on one branch with no twin on the other.")
    ap.add_argument("bench")
    ap.add_argument("arena")
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    a = ap.parse_args(argv)
    try:
        r = orphans(a.repo, a.bench, a.arena)
    except GitError as e:
        print(f"twins: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(report(r, a.bench, a.arena))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
