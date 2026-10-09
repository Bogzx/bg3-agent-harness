"""Shared helpers for the playthrough memory tools (stdlib only).

The memory directory layout is created from ``memory-template/`` by ``new_playthrough.py``.
It is its own git repository; ``session.py end`` commits it once per session.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "memory-template"

STATE_FILE = ".session.json"

# Files that must exist in every memory dir.
REQUIRED_FILES = [
    "PLAYTHROUGH.md",
    "HANDOFF.md",
    "objectives.md",
    "hypotheses.md",
    "strategy.md",
    "decisions.md",
    "npcs.md",
    "locations.md",
    "quests.md",
    "party-build.md",
    "inventory-notes.md",
    "markers.json",
]
REQUIRED_DIRS = ["snapshots", "sessions", "chapters", "screenshots"]

# (max content lines, max content chars or None). Content = HTML comments and blank lines removed.
BUDGETS: dict[str, tuple[int, int | None]] = {
    "PLAYTHROUGH.md": (120, 5000),
    "HANDOFF.md": (60, 3500),
    "objectives.md": (80, None),
    "hypotheses.md": (60, None),
    "strategy.md": (150, None),
    "decisions.md": (400, None),
    "npcs.md": (400, None),
    "locations.md": (400, None),
    "quests.md": (300, None),
    "party-build.md": (150, None),
    "inventory-notes.md": (100, None),
}
CHAPTER_MAX_LINES = 150
SESSION_LOG_MAX_LINES = 300

# Curated fact files in which every bullet needs a provenance tag.
TAGGED_FILES = ["npcs.md", "locations.md", "quests.md", "decisions.md", "hypotheses.md"]
# Bullets starting with one of these labels are meta/plan lines and need no tag.
META_LABELS = {
    "thread", "threads", "last updated", "updated", "see", "status", "test", "plan", "choice",
    "why", "reversible", "next", "kill criterion", "role", "todo",
}
# Files guarded against bulk deletion (lines lost vs git HEAD).
GUARDED_FILES = ["decisions.md", "npcs.md"]
ARCHIVE_MARKER = "ARCHIVE:"
DEFAULT_MAX_DELETED = 10

TAG_RE = re.compile(r"\[(seen|told|wiki|guess)\b[^\]]*\]", re.IGNORECASE)
BOGUS_TAG_RE = re.compile(
    r"\[(verified|confirmed|fact|known|assumed|rumou?r|unverified|likely|maybe)\]", re.IGNORECASE
)
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
SESSION_FILE_RE = re.compile(r"^S(\d{3,})\.md$")
CHAPTER_RANGE_RE = re.compile(r"S(\d{3,})-S(\d{3,})")
HANDOFF_RE = re.compile(r"^#\s*Handoff\s+S(\d+)", re.MULTILINE)
OPEN_HYPOTHESIS_RE = re.compile(r"^##\s*(H-\d+)\b.*\(open,\s*S(\d+)\)", re.MULTILINE)

UUID_LIKE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

COMPACTION_EVERY = 10

# Curated files that go stale when only the session logs get written (claude-run-2: objectives.md,
# npcs.md, quests.md and locations.md were untouched from S000-S003 to S011 while the logs grew):
# file -> sessions without a change before lint warns. objectives.md is the goal stack the session-start
# playbook sends the agent to, so at wrap-up a stale one is an error (OBJECTIVES_STALE_ERROR).
STALE_AFTER = {
    "objectives.md": 3,
    "quests.md": 5,
    "npcs.md": 5,
    "locations.md": 5,
    "strategy.md": 6,
    "party-build.md": 6,
}
OBJECTIVES_STALE_ERROR = 3
COMMIT_SESSION_RE = re.compile(r"^S(\d{3,})\b")


def session_id(n: int) -> str:
    return f"S{n:03d}"


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def strip_comments(text: str) -> str:
    """Remove HTML comments (template guidance) from markdown."""
    return COMMENT_RE.sub("", text)


def content_lines(text: str) -> list[str]:
    """Non-blank lines once HTML comments are removed."""
    return [ln for ln in strip_comments(text).splitlines() if ln.strip()]


def resolve_memory_dir(arg: str | None) -> Path:
    """--memory-dir, else $BG3_MEMORY_DIR, else $CLAUDE_PROJECT_DIR/memory, else ./memory."""
    if arg:
        return Path(arg)
    if os.environ.get("BG3_MEMORY_DIR"):
        return Path(os.environ["BG3_MEMORY_DIR"])
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return Path(os.environ["CLAUDE_PROJECT_DIR"]) / "memory"
    return Path("memory")


# ---------------------------------------------------------------- session state


def load_state(mem: Path) -> dict:
    p = mem / STATE_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(mem: Path, state: dict) -> None:
    p = mem / STATE_FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(p)


def session_numbers(mem: Path) -> list[int]:
    d = mem / "sessions"
    if not d.is_dir():
        return []
    return sorted(int(m.group(1)) for f in d.iterdir() if (m := SESSION_FILE_RE.match(f.name)))


def last_compacted_session(mem: Path) -> int:
    """Highest session covered by a chapter file named like 'C01_S001-S010_<slug>.md'."""
    d = mem / "chapters"
    best = 0
    if d.is_dir():
        for f in d.iterdir():
            for m in CHAPTER_RANGE_RE.finditer(f.name):
                best = max(best, int(m.group(2)))
    return best


def compaction_due(mem: Path, current: int) -> bool:
    return (current - 1) - last_compacted_session(mem) >= COMPACTION_EVERY


# ---------------------------------------------------------------- git

GIT_FALLBACK_IDENTITY = ["-c", "user.name=bg3-agent", "-c", "user.email=bg3-agent@localhost"]


def git(mem: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(mem), *args], check=check, capture_output=True, text=True
    )


def git_identity_args(mem: Path) -> list[str]:
    """Use the configured identity if any; otherwise a fixed bot identity.

    Commit signing is disabled for the memory repo: commits are made unattended by hooks.
    """
    args = ["-c", "commit.gpgsign=false"]
    name = git(mem, "config", "user.name", check=False).stdout.strip()
    email = git(mem, "config", "user.email", check=False).stdout.strip()
    if not (name and email):
        args += GIT_FALLBACK_IDENTITY
    return args


def is_git_repo(mem: Path) -> bool:
    return (mem / ".git").exists()


def has_head(mem: Path) -> bool:
    return git(mem, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0


def last_touched_session(mem: Path, rel: str) -> int | None:
    """Session whose memory commit last changed `rel` (commit subjects start "Sxxx"), or None if unknown.
    A file with uncommitted changes counts as touched by the open session: returns -1 (caller decides)."""
    if not is_git_repo(mem) or not has_head(mem):
        return None
    if git(mem, "status", "--porcelain", "--", rel, check=False).stdout.strip():
        return -1
    log = git(mem, "log", "--format=%s", "--", rel, check=False).stdout.splitlines()
    for subject in log:
        if m := COMMIT_SESSION_RE.match(subject):
            return int(m.group(1))
    return None


def git_commit_all(mem: Path, message: str) -> str | None:
    """`git add -A && git commit -m message`. Returns the new short hash, or None if nothing changed."""
    git(mem, "add", "-A")
    extra = []
    if git(mem, "diff", "--cached", "--quiet", check=False).returncode == 0:
        if has_head(mem):
            return None
        extra = ["--allow-empty"]
    subprocess.run(
        ["git", *git_identity_args(mem), "-C", str(mem), "commit", "-q", *extra, "-m", message],
        check=True, capture_output=True, text=True,
    )
    return git(mem, "rev-parse", "--short", "HEAD").stdout.strip()
