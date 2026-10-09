#!/usr/bin/env python3
"""Lint a playthrough memory directory.

Checks:
  * required files/dirs exist, markers.json is valid
  * size budgets (content lines/chars, HTML comments and blank lines excluded)
  * provenance tags on bullets in curated fact files ([seen] [told] [wiki] [guess])
  * HANDOFF.md freshness (at --phase end it must be written by the current session)
  * deletion guard vs git HEAD: decisions.md / npcs.md may not lose more than N lines unless an
    added line contains "ARCHIVE:"; session logs are append-only
  * warnings: stale open hypotheses, oversized session logs, compaction due, screenshot bloat

Exit status: 0 = no errors (warnings allowed), 1 = errors, 2 = usage problem.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memlib as M  # noqa: E402

SCREENSHOT_WARN_FILES = 60
SCREENSHOT_WARN_BYTES = 40 * 1024 * 1024
HYPOTHESIS_MAX_AGE = 3


@dataclass
class Report:
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)

    def error(self, kind: str, msg: str) -> None:
        self.errors.append((kind, msg))

    def warn(self, kind: str, msg: str) -> None:
        self.warnings.append((kind, msg))

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self, mem: Path) -> str:
        head = f"memory lint: {mem} — {len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        lines = [head]
        lines += [f"ERROR  {k:<11} {m}" for k, m in self.errors]
        lines += [f"WARN   {k:<11} {m}" for k, m in self.warnings]
        if self.errors:
            lines.append("Fix the errors above, then re-run. (Session wrap-up refuses to commit until lint passes.)")
        return "\n".join(lines)


# ---------------------------------------------------------------- individual checks


def check_structure(mem: Path, r: Report) -> None:
    for f in M.REQUIRED_FILES:
        if not (mem / f).is_file():
            r.error("missing", f"{f} does not exist (recreate it from memory-template/{f})")
    for d in M.REQUIRED_DIRS:
        if not (mem / d).is_dir():
            r.error("missing", f"{d}/ directory does not exist")
    mk = mem / "markers.json"
    if mk.is_file():
        try:
            data = json.loads(mk.read_text())
            # The MCP server (place_marker) writes a bare list; an object with "markers" is accepted too.
            markers = data if isinstance(data, list) else (data.get("markers") if isinstance(data, dict) else None)
            if not isinstance(markers, list):
                r.error("markers", 'markers.json must be a list of markers (or an object with a "markers" list)')
            else:
                for i, m in enumerate(markers):
                    if not isinstance(m, dict) or "id" not in m or "label" not in m:
                        r.error("markers", f"markers.json entry #{i} needs at least 'id' and 'label'")
        except json.JSONDecodeError as e:
            r.error("markers", f"markers.json is not valid JSON: {e}")


def check_budgets(mem: Path, r: Report) -> None:
    for name, (max_lines, max_chars) in M.BUDGETS.items():
        p = mem / name
        if not p.is_file():
            continue
        lines = M.content_lines(p.read_text())
        if len(lines) > max_lines:
            r.error("budget", f"{name}: {len(lines)} content lines > {max_lines}. Trim or condense it "
                              f"(move detail to the right file, archive old entries).")
        if max_chars is not None:
            chars = sum(len(ln) + 1 for ln in lines)
            if chars > max_chars:
                r.error("budget", f"{name}: {chars} content chars > {max_chars}. It is injected at "
                                  f"session start; shorten it.")
    chapters = mem / "chapters"
    if chapters.is_dir():
        for f in sorted(chapters.glob("*.md")):
            n = len(M.content_lines(f.read_text()))
            if n > M.CHAPTER_MAX_LINES:
                r.error("budget", f"chapters/{f.name}: {n} content lines > {M.CHAPTER_MAX_LINES}")
    sessions = mem / "sessions"
    if sessions.is_dir():
        for f in sorted(sessions.glob("S*.md")):
            n = len(M.content_lines(f.read_text()))
            if n > M.SESSION_LOG_MAX_LINES:
                r.warn("budget", f"sessions/{f.name}: {n} lines > {M.SESSION_LOG_MAX_LINES}; log "
                                 f"events more tersely (one line each)")


def untagged_bullets(text: str) -> list[tuple[int, str]]:
    """(line number, text) of bullets lacking a provenance tag, outside HTML comments."""
    out = []
    # Keep line numbers stable: blank out comments without removing their newlines.
    cleaned = M.COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    for i, line in enumerate(cleaned.splitlines(), 1):
        m = M.BULLET_RE.match(line)
        if not m:
            continue
        body = m.group(1).strip()
        if not body or M.TAG_RE.search(body):
            continue
        label = body.split(":", 1)[0].strip().lower() if ":" in body else ""
        if label in M.META_LABELS:
            continue
        if M.ARCHIVE_MARKER in body:
            continue
        out.append((i, body))
    return out


def check_provenance(mem: Path, r: Report) -> None:
    for name in M.TAGGED_FILES:
        p = mem / name
        if not p.is_file():
            continue
        text = p.read_text()
        missing = untagged_bullets(text)
        for lineno, body in missing[:15]:
            short = body if len(body) <= 70 else body[:67] + "..."
            r.error("provenance", f"{name}:{lineno}: no [seen]/[told]/[wiki]/[guess] tag: {short!r}")
        if len(missing) > 15:
            r.error("provenance", f"{name}: ... and {len(missing) - 15} more untagged bullets")
        for m in M.BOGUS_TAG_RE.finditer(M.strip_comments(text)):
            r.warn("provenance", f"{name}: non-standard tag {m.group(0)}; use [seen]/[told]/[wiki]/[guess]")


def handoff_session(mem: Path) -> int | None:
    p = mem / "HANDOFF.md"
    if not p.is_file():
        return None
    m = M.HANDOFF_RE.search(p.read_text())
    return int(m.group(1)) if m else None


def check_handoff(mem: Path, r: Report, phase: str, current: int | None) -> None:
    p = mem / "HANDOFF.md"
    if not p.is_file():
        return
    text = M.strip_comments(p.read_text())
    hs = handoff_session(mem)
    if hs is None:
        r.error("handoff", 'HANDOFF.md must start with a heading like "# Handoff S042 → S043"')
        return
    if current is None:
        return
    if phase == "end":
        if hs != current:
            r.error("handoff", f"HANDOFF.md was written by {M.session_id(hs)}, not by the current session "
                               f"{M.session_id(current)}. Rewrite it (heading '# Handoff "
                               f"{M.session_id(current)} → {M.session_id(current + 1)}').")
        for needle in ("Save to load:", "## Doing right now", "## Next steps"):
            if needle not in text:
                r.error("handoff", f"HANDOFF.md is missing the required '{needle}' line/section")
    elif hs < current - 1:
        r.warn("handoff", f"HANDOFF.md is from {M.session_id(hs)}; sessions since then ended without a "
                          f"handoff. Reconcile carefully against live state.")


def _diff_lines(mem: Path, rel: str) -> tuple[list[str], list[str]]:
    out = M.git(mem, "diff", "-U0", "--no-color", "--no-ext-diff", "HEAD", "--", rel, check=False).stdout
    removed, added = [], []
    for line in out.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    return removed, added


def _meaningful(lines: list[str]) -> list[str]:
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith(("<!--", "-->"))]


def lost_lines(mem: Path, rel: str) -> tuple[list[str], bool]:
    """Lines present at HEAD but gone now (moved lines don't count), and whether an ARCHIVE: line was added."""
    removed, added = _diff_lines(mem, rel)
    lost = Counter(_meaningful(removed)) - Counter(_meaningful(added))
    archived = any(M.ARCHIVE_MARKER in a for a in added)
    return list(lost.elements()), archived


def _sample(lines: list[str], n: int = 8) -> str:
    shown = [f"    | {ln[:110]}" for ln in lines[:n]]
    if len(lines) > n:
        shown.append(f"    | ... and {len(lines) - n} more")
    return "\n" + "\n".join(shown)


def tracked_in_head(mem: Path, rel: str) -> bool:
    return M.git(mem, "cat-file", "-e", f"HEAD:{rel}", check=False).returncode == 0


def check_deletions(mem: Path, r: Report, max_deleted: int) -> None:
    if not M.is_git_repo(mem):
        r.warn("git", "memory dir is not a git repository; deletion guard skipped")
        return
    if not M.has_head(mem):
        return
    for rel in M.GUARDED_FILES:
        if not tracked_in_head(mem, rel):
            continue
        lost, archived = lost_lines(mem, rel)
        if len(lost) > max_deleted and not archived:
            r.error("deletion", f"{rel}: {len(lost)} lines removed since the last commit (limit {max_deleted}). "
                                f"Don't delete facts: put them back (or condensed) under '## Archive', or add "
                                f"a line 'ARCHIVE: <why>' if condensing is intended. Removed lines:"
                                + _sample(lost))
        elif len(lost) > max_deleted:
            r.warn("deletion", f"{rel}: {len(lost)} lines removed (ARCHIVE: marker present)")
    sessions = mem / "sessions"
    if sessions.is_dir():
        for f in sorted(sessions.glob("S*.md")):
            rel = f"sessions/{f.name}"
            if tracked_in_head(mem, rel):
                lost, _ = lost_lines(mem, rel)
                if lost:
                    r.error("append-only", f"{rel}: {len(lost)} committed lines were changed or removed; "
                                           f"session logs are append-only. Restore these lines exactly:"
                                           + _sample(lost, 20))
    for rel in M.REQUIRED_FILES:
        if tracked_in_head(mem, rel) and not (mem / rel).exists():
            r.error("deletion", f"{rel} was deleted; recreate it (the operator can restore it from git)")


def check_hypotheses(mem: Path, r: Report, current: int | None) -> None:
    p = mem / "hypotheses.md"
    if current is None or not p.is_file():
        return
    for m in M.OPEN_HYPOTHESIS_RE.finditer(M.strip_comments(p.read_text())):
        age = current - int(m.group(2))
        if age > HYPOTHESIS_MAX_AGE:
            r.warn("hypothesis", f"{m.group(1)} has been open since {M.session_id(int(m.group(2)))} "
                                 f"({age} sessions): test it this session or close it as dropped")


def check_misc(mem: Path, r: Report, current: int | None) -> None:
    shots = mem / "screenshots"
    if shots.is_dir():
        files = [f for f in shots.iterdir() if f.is_file() and f.name != ".gitkeep"]
        size = sum(f.stat().st_size for f in files)
        if len(files) > SCREENSHOT_WARN_FILES or size > SCREENSHOT_WARN_BYTES:
            r.warn("screenshots", f"screenshots/ holds {len(files)} files ({size // 1024} KiB); delete ones "
                                  f"no note references")
    if current is not None and M.compaction_due(mem, current):
        r.warn("compaction", f"compaction due: sessions after S{M.last_compacted_session(mem):03d} are not "
                             f"in a chapter yet (run the compaction skill)")


# ---------------------------------------------------------------- driver


def lint(mem: Path, phase: str = "any", session: int | None = None,
         max_deleted: int = M.DEFAULT_MAX_DELETED) -> Report:
    r = Report()
    if not mem.is_dir():
        r.error("missing", f"memory dir {mem} does not exist (create one with tools/memory/new_playthrough.py)")
        return r
    if session is None:
        st = M.load_state(mem)
        session = st.get("current") if st.get("open") else None
    check_structure(mem, r)
    check_budgets(mem, r)
    check_provenance(mem, r)
    check_handoff(mem, r, phase, session)
    check_deletions(mem, r, max_deleted)
    check_hypotheses(mem, r, session)
    check_misc(mem, r, session)
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("memory_dir", nargs="?", help="memory directory (default: $BG3_MEMORY_DIR or ./memory)")
    ap.add_argument("--phase", choices=["any", "end"], default="any",
                    help="'end' = session wrap-up: HANDOFF must be from the current session")
    ap.add_argument("--session", type=int, help="current session number (default: from .session.json)")
    ap.add_argument("--max-deleted", type=int, default=M.DEFAULT_MAX_DELETED)
    args = ap.parse_args(argv)
    mem = M.resolve_memory_dir(args.memory_dir)
    r = lint(mem, args.phase, args.session, args.max_deleted)
    print(r.render(mem))
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
