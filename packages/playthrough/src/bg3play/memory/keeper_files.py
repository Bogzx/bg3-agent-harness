"""The memory keeper's ground rules on the memory dir (keeper.py runs the pass): what it may write (WRITABLE_FILES,
HANDOFF.md's last section, CHARACTER.md's development lines), the pre-pass snapshot and restore, the MEM-1 grounding
check on added bullets, and the pending-pass file that recover() uses after a runner was killed mid-pass."""

from __future__ import annotations

import base64
import contextlib
import importlib
import json
import os
import re
import signal
import time
from pathlib import Path
from typing import Any

from bg3play.memory import lint, memlib

REPO = memlib.REPO_ROOT
# bg3ai-bridge (stdlib only); Any to pyright (the bridge ships no types)
milestones: Any = importlib.import_module("bg3ai.milestones")
bg3progress: Any = importlib.import_module("bg3ai.progress")

clients: Any = importlib.import_module("bg3harness.clients")  # Any to pyright like the bridge: an untyped legacy module

SESSION_PY = REPO / "tools" / "memory" / "session.py"
KNOWLEDGE = REPO / "play" / "knowledge"

KEEPER_MODEL = "sonnet"  # run_keeper's own default; the runner passes the play model (--keeper-model same)
KEEPER_EFFORT = "low"
KEEPER_MINUTES = 5.0
FIX_MIN_SECONDS = 45  # less than this left after the pass: no lint-fix retry
MAX_LOGS = 8  # session logs named in the prompt (newest first wins)

# What the keeper may change. Everything else in the memory dir is restored after the pass.
WRITABLE_FILES = [
    "objectives.md",
    "quests.md",
    "npcs.md",
    "locations.md",
    "party-build.md",
    "strategy.md",
    "decisions.md",
    "hypotheses.md",
    "inventory-notes.md",
    "PLAYTHROUGH.md",
    "lessons.md",
]
WRITABLE_DIRS = ["chapters"]
SNAPSHOT_DIRS = ["chapters", "sessions"]  # plus every top-level file


# HANDOFF.md: the keeper owns only this last section (the player's own text above it, incl. the "Save to
# load" line the runner reads, is kept byte for byte: see keep_handoff_head). It carries what the next session
# needs from the logs, so the player never Reads old sessions/Sxxx.md mid-session (~19 s/session in S021-S026).
HANDOFF_SECTION = "## From the logs (memory keeper)"


GIT_NAMES = {".git", ".gitattributes", ".gitignore", ".gitmodules"}  # never the keeper's (FAIR-1)


def writable(rel: str) -> bool:
    if any(part.lower() in GIT_NAMES for part in rel.split("/")):
        return False
    return (
        rel in WRITABLE_FILES
        or rel in ("HANDOFF.md", lint.CHARACTER_FILE)
        or any(rel.startswith(d + "/") for d in WRITABLE_DIRS)
    )


def _handoff_split(text: str) -> tuple[str, str]:
    i = text.find("\n" + HANDOFF_SECTION)
    return (text, "") if i < 0 else (text[:i], text[i + 1 :])


def keep_handoff_head(mem: Path, before: dict[str, bytes]) -> bool:
    """Puts back the player's part of HANDOFF.md (everything above HANDOFF_SECTION) if the keeper touched it;
    keeps only the keeper's own section. True when something had to be put back."""
    p = mem / "HANDOFF.md"
    if "HANDOFF.md" not in before or not p.is_file():
        return False
    old = before["HANDOFF.md"].decode("utf-8", "replace")
    new = p.read_text(encoding="utf-8", errors="replace")
    old_head, _ = _handoff_split(old)
    new_head, section = _handoff_split(new)
    if new_head == old_head:
        return False
    merged = old_head.rstrip("\n") + ("\n\n" + section.rstrip("\n") + "\n" if section else "\n")
    p.write_text(merged if section else old, encoding="utf-8")
    return True


def keep_character_core(mem: Path, before: dict[str, bytes]) -> bool:
    """CHARACTER.md (the persona) is the player's, except that lines may be appended to its "## Character
    development" section. Anything else the keeper did to it (a rewritten core or older development lines, a new
    or deleted file) is put back, keeping only the appended lines. True when something had to be put back."""
    p = mem / lint.CHARACTER_FILE
    old = before.get(lint.CHARACTER_FILE)
    new = p.read_bytes() if p.is_file() else None
    if new == old:
        return False
    if old is None:  # written in the character-creation phase only, never by the keeper
        p.unlink()
        return True
    if new is None:
        p.write_bytes(old)
        return True
    old_t, new_t = old.decode("utf-8", "replace"), new.decode("utf-8", "replace")
    old_core, old_dev = lint.character_split(old_t)
    new_core, new_dev = lint.character_split(new_t)
    old_lines, new_lines = old_dev.splitlines(), new_dev.splitlines()
    if new_core == old_core and new_lines[: len(old_lines)] == old_lines:
        return False  # a pure append
    added = [ln for ln in new_lines[1:] if ln.strip() and ln not in old_lines]
    merged = old_t.rstrip("\n") + "\n"
    if added:
        if not old_dev:
            merged += f"\n{lint.CHARACTER_DEV_HEADING}\n"
        merged += "\n".join(added) + "\n"
    p.write_text(merged, encoding="utf-8")
    return True


# ---------------------------------------------------------------- snapshot / restore


def snapshot(mem: Path) -> dict[str, bytes]:
    """Contents of the memory files the keeper could touch: top-level files (and the .git* control files, so a
    keeper-written .gitattributes is reverted), chapters/, sessions/."""
    out: dict[str, bytes] = {}
    for p in mem.iterdir():
        if p.is_file() and (not p.name.startswith(".") or p.name.lower() in GIT_NAMES):
            out[p.name] = p.read_bytes()
    for d in SNAPSHOT_DIRS:
        if (mem / d).is_dir():
            for p in (mem / d).iterdir():
                if p.is_file():
                    out[f"{d}/{p.name}"] = p.read_bytes()
    return out


def changes(mem: Path, before: dict[str, bytes]) -> list[str]:
    """Paths changed, added or deleted since `before` (sorted)."""
    now = snapshot(mem)
    return sorted(rel for rel in set(before) | set(now) if before.get(rel) != now.get(rel))


def restore(mem: Path, before: dict[str, bytes], rels: list[str]) -> None:
    for rel in rels:
        p = mem / rel
        if rel in before:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(before[rel])
        else:
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------- grounding (MEM-1)

FACT_FILES = [*memlib.TAGGED_FILES, "lessons.md", "objectives.md", "party-build.md", "strategy.md"]
_NAME_RE = re.compile(r"[A-Z][A-Za-z'\u2019-]{2,}")
_SENT_START = re.compile(r"(?:^|[.!?:;]\s+|[(\[]|\*\*|`)$")
# words the prompt itself asks the keeper to write
_KEEPER_WORDS = {
    "when",
    "note",
    "retired",
    "archive",
    "last",
    "updated",
    "done",
    "died",
    "ally",
    "fall",
    "seen",
    "told",
    "wiki",
    "guess",
    "worked",
    "blocked",
    "lessons",
    "party",
    "main",
    "now",
}


def corpus(snap: dict[str, bytes], knowledge: Path = KNOWLEDGE, extra: str = "") -> str:
    """Lower-cased text the keeper may draw on: the memory before the pass, the knowledge dir, its prompt."""
    parts = [b.decode("utf-8", "replace") for b in snap.values()]
    try:
        for p in sorted(knowledge.rglob("*.md")):
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    parts.append(extra)
    return "\n".join(parts).lower()


def ungrounded_names(line: str, text: str) -> list[str]:
    """Capitalised words of a bullet (not starting it or a sentence) that `text` (lower-case corpus) never has."""
    body = re.sub(r"^\s*[-*+]\s+", "", line)
    out: list[str] = []
    for m in _NAME_RE.finditer(body):
        # S071: "Myrmidon's" isn't in a corpus that has "myrmidon": the possessive goes before the lookup
        w = re.sub(r"['\u2019]s$", "", m.group(0)).strip("'\u2019-")
        if m.start() == 0 or _SENT_START.search(body[: m.start()]) or w.lower() in _KEEPER_WORDS:
            continue
        if re.fullmatch(r"S\d+|L\d+|[IVX]+", w) or w.lower() in text:
            continue
        out.append(w)
    return out


def ungrounded_lines(mem: Path, before: dict[str, bytes], rels: list[str], text: str) -> dict[str, list[str]]:
    """{file: [added bullet lines naming something the corpus never has]} over the keeper's FACT_FILES edits."""
    out: dict[str, list[str]] = {}
    for rel in rels:
        if rel not in FACT_FILES or not (mem / rel).is_file():
            continue
        old = before.get(rel, b"").decode("utf-8", "replace").splitlines()
        pool: dict[str, int] = {}
        for ln in old:
            pool[ln] = pool.get(ln, 0) + 1
        bad: list[str] = []
        for ln in (mem / rel).read_text(encoding="utf-8", errors="replace").splitlines():
            if pool.get(ln):
                pool[ln] -= 1
                continue
            if re.match(r"\s*[-*+]\s", ln) and ungrounded_names(ln, text):
                bad.append(ln)
        if bad:
            out[rel] = bad
    return out


def drop_lines(mem: Path, found: dict[str, list[str]]) -> None:
    for rel, bad in found.items():
        p = mem / rel
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines(True)
        left = list(bad)
        keep: list[str] = []
        for ln in lines:
            if ln.rstrip("\n") in left:
                left.remove(ln.rstrip("\n"))
                continue
            keep.append(ln)
        p.write_text("".join(keep), encoding="utf-8")


# ---------------------------------------------------------------- crash recovery (MEM-1)

PENDING = "keeper_pending.json"


def save_pending(state_dir: Path, mem: Path, sid: str, snap: dict[str, bytes]) -> Path:
    p = Path(state_dir) / PENDING
    p.write_text(
        json.dumps(
            {
                "sid": sid,
                "mem": str(mem.resolve()),
                "t": round(time.time(), 1),
                "files": {k: base64.b64encode(v).decode() for k, v in snap.items()},
            }
        )
    )
    return p


def note_pgid(pending: Path | None, pgid: int) -> None:
    if pending is not None:
        with contextlib.suppress(OSError):
            Path(f"{pending}.pgid").write_text(str(pgid))


def clear_pending(pending: Path | None) -> None:
    if pending is not None:
        for p in (pending, Path(f"{pending}.pgid")):
            Path(p).unlink(missing_ok=True)


def recover(mem: Path, state_dir: Path) -> str | None:
    """At the runner's start: a keeper pass that never finished (the runner was killed mid-pass) left its pre-pass
    snapshot. Kills a leftover keeper process group, then puts the memory back as the player left it unless the
    pass was committed. Returns what was done, or None when nothing was pending. Never raises."""
    p = Path(state_dir) / PENDING
    if not p.is_file():
        return None
    try:
        try:
            pgid = int(Path(f"{p}.pgid").read_text().strip())
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
        d = json.loads(p.read_text())
        sid = d.get("sid") or "?"
        if Path(d.get("mem") or "") != mem.resolve():
            clear_pending(p)
            return f"{sid} keeper snapshot is for another memory dir: dropped"
        head = (
            memlib.git(mem, "log", "-1", "--format=%s", check=False).stdout.strip() if memlib.is_git_repo(mem) else ""
        )
        if head.startswith(f"{sid} memory-keeper:"):
            clear_pending(p)
            return f"{sid} keeper pass was committed: nothing to undo"
        files: dict[str, str] = d.get("files") or {}
        snap = {k: base64.b64decode(v) for k, v in files.items()}
        rels = changes(mem, snap)
        restore(mem, snap, rels)
        clear_pending(p)
        return f"{sid} keeper pass never finished: put back {', '.join(rels) or 'nothing (no edits)'}"
    except Exception as e:
        return f"keeper recovery failed: {type(e).__name__}: {e}"[:300]
