"""Memory keeper: a short `claude -p` pass between play sessions that brings the curated memory up to date.

The playing agent logs every session (sessions/Sxxx.md) but rarely updates the curated files (claude-run-2:
quests/npcs/locations unchanged for 8 sessions, objectives for 3, compaction due since S010). So after each
session the runner gives that job to a cheap model (sonnet, low effort, ≤5 min) that sees ONLY the memory
dir and the read-only knowledge dir: no game, no bg3 MCP tools, no shell, no web. It reads the session logs
and HANDOFF.md and updates objectives/quests/npcs/locations/party-build/strategy, compacts old sessions into
a chapter when due, and the runner commits its edits as "<sid> memory-keeper: ..." so lint's staleness
counters reset.

When it runs (and why no write conflicts): after the session's memory is closed and BEFORE the next player
starts. It overlaps only the runner's between-session game prep (ensure_game, mod hot reload, settings,
check_all), which never writes memory; the runner joins it before the next `claude` starts. It never runs
during play: the player edits the same curated files mid-session and its Stop hook commits `git add -A`,
so a concurrent keeper would race both. HANDOFF.md (the runner reads its "Save to load" line during the
prep) is the player's except its last section (HANDOFF_SECTION, the keeper's: what the next session needs
from the logs); the runner restores the player's part byte for byte if touched. sessions/ (append-only player
logs) is read-only for the keeper: denied by its permissions and restored by the runner if touched anyway.

Fairness: it only reorganizes what the memory already says (the player's own notes); no new information.

Result guard: the runner lints after the pass. New lint errors get one short resumed fix attempt; if they
remain, every keeper edit is rolled back from the pre-pass snapshot (the memory is left as the player left it).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools" / "memory"))
import lint as L  # noqa: E402
import memlib as M  # noqa: E402

SESSION_PY = REPO / "tools" / "memory" / "session.py"
KNOWLEDGE = REPO / "play" / "knowledge"

KEEPER_MODEL = "sonnet"
KEEPER_EFFORT = "low"
KEEPER_MINUTES = 5.0
FIX_MIN_SECONDS = 45           # less than this left after the pass: no lint-fix retry
MAX_LOGS = 8                   # session logs named in the prompt (newest first wins)

# What the keeper may change. Everything else in the memory dir is restored after the pass.
WRITABLE_FILES = ["objectives.md", "quests.md", "npcs.md", "locations.md", "party-build.md", "strategy.md",
                  "decisions.md", "hypotheses.md", "inventory-notes.md", "PLAYTHROUGH.md", "lessons.md"]
WRITABLE_DIRS = ["chapters"]
SNAPSHOT_DIRS = ["chapters", "sessions"]   # plus every top-level file


# HANDOFF.md: the keeper owns only this last section (the player's own text above it, incl. the "Save to
# load" line the runner reads, is kept byte for byte: see keep_handoff_head). It carries what the next session
# needs from the logs, so the player never Reads old sessions/Sxxx.md mid-session (~19 s/session in S021-S026).
HANDOFF_SECTION = "## From the logs (memory keeper)"


def writable(rel: str) -> bool:
    return rel in WRITABLE_FILES or rel == "HANDOFF.md" or any(rel.startswith(d + "/") for d in WRITABLE_DIRS)


def _handoff_split(text: str) -> tuple[str, str]:
    i = text.find("\n" + HANDOFF_SECTION)
    return (text, "") if i < 0 else (text[:i], text[i + 1:])


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


# ---------------------------------------------------------------- snapshot / restore

def snapshot(mem: Path) -> dict[str, bytes]:
    """Contents of the memory files the keeper could touch: top-level files, chapters/, sessions/."""
    out: dict[str, bytes] = {}
    for p in mem.iterdir():
        if p.is_file() and not p.name.startswith("."):
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


# ---------------------------------------------------------------- prompt / command

def logs_to_read(mem: Path, sid_n: int) -> list[int]:
    """Session logs since the stalest curated file was last committed (at most MAX_LOGS, oldest first)."""
    touched = [M.last_touched_session(mem, rel) for rel in M.STALE_AFTER]
    known = [t for t in touched if t is not None and t >= 0]
    since = min(known) if known else 0
    nums = [n for n in M.session_numbers(mem) if since <= n <= sid_n]
    return nums[-MAX_LOGS:]


def harness_news(knowledge_dir: Path = KNOWLEDGE, last: int = 12) -> list[str]:
    """The newest harness-news.md entries ('- YYYY-MM-DD HH:MM …'), at most `last`."""
    try:
        text = (knowledge_dir / "harness-news.md").read_text(encoding="utf-8")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.startswith("- 20")][-last:]


_PLAN_NOTE = re.compile(r"\(plan: (?P<name>[^()]+?) L(?P<lvl>\d{1,2}) from memory/party-build\.md\)")
_REFUSED = re.compile(r"not offered|already known|needs -?\d+ more|FAILED", re.I)
# The harness failing to click, not the game refusing the plan (S032: Wyll L6 "spell picks pending (Hunger of
# Hadar)" was a valid pick the tool lost; the keeper was told to rewrite the line).
_HARNESS_FAIL = re.compile(r"spell picks pending|no spell button|stuck in its intro|TIMEOUT|screen fade", re.I)
_PLAN_FAIL = re.compile(r"not offered|already known|needs -?\d+ more", re.I)
REFUSED_MAX_CHARS = 1500


def refused_plans(actions: list[dict]) -> list[dict]:
    """[{name, level, problems}] for stored party-build.md plan lines the game refused this session (level_up
    results, runtime/actions.jsonl) and that no later call completed. level_up's result already carries what the
    game offered ("can pick: …", "options={…}"): the keeper rewrites the line from it, so the next level_up
    completes instead of failing once per refused line (live 12:2x: Wyll L5 "Thirsting Blade: not offered")."""
    last: dict[tuple[str, int], dict] = {}
    for rec in actions:
        if rec.get("tool") != "level_up" or not isinstance(rec.get("result"), str):
            continue
        text = rec["result"]
        start = 0
        for m in _PLAN_NOTE.finditer(text):   # a camp member's result holds one chunk per level: "L4: … (plan: …)"
            chunk = text[start:m.start()]
            start = m.end()
            key = (m.group("name").strip(), int(m.group("lvl")))
            done = "level_up: done" in chunk and not re.search(r"not offered|already known", chunk)
            if done:
                last.pop(key, None)
            elif _HARNESS_FAIL.search(chunk) and not _PLAN_FAIL.search(chunk):
                continue
            elif _REFUSED.search(chunk):
                last[key] = {"name": key[0], "level": key[1], "problems": chunk.strip()[-REFUSED_MAX_CHARS:]}
    return list(last.values())


def build_prompt(mem: Path, sid: str, sid_n: int, lint_report: str, minutes: float,
                 refused: list[dict] | None = None) -> str:
    logs = ", ".join(f"sessions/{M.session_id(n)}.md" for n in logs_to_read(mem, sid_n)) or "(none)"
    stale = L.stale_files(mem, sid_n + 1)
    stale_txt = ", ".join(f"{rel} ({age} sessions)" for rel, age in stale) or "none"
    nxt = M.session_id(sid_n + 1)
    lines = [
        f"You are the MEMORY KEEPER of an AI's Baldur's Gate 3 playthrough. Session {sid} just ended; {nxt} "
        f"starts after you. You have {minutes:.0f} minutes, Read/Edit/Write/Glob/Grep only, and no game access. "
        "Work fast: few, targeted reads and edits. No questions: nobody answers.",
        "",
        "JOB: bring the curated memory files up to date from the player's own notes. Your working directory "
        "is the memory dir. Read HANDOFF.md, objectives.md, PLAYTHROUGH.md, then these session logs (newest "
        f"matter most): {logs}. Then edit:",
        "- objectives.md: the current Main goal, its why and its next step, matching HANDOFF.md and the latest "
        "log. Mark finished goals done.",
        "- quests.md, npcs.md, locations.md: add/update what the logs record (new quests and their state, NPCs met "
        "and what they said or did, places visited and what's there). Keep the existing format.",
        "- party-build.md, strategy.md, decisions.md, hypotheses.md: only where the logs give something new "
        "(level-ups, builds, lessons, decisions, confirmed/refuted hypotheses).",
        "- lessons.md (create it from the format below if missing): the bridge shows its lines at the next fight "
        "with a matching enemy/race/place, so the next try goes better. From every `[fight]` debrief graded "
        "costly, near_wipe, wipe or reloaded and every `[lesson]` note in the logs, write ONE line per lesson: "
        "`- WHEN <trigger>, <trigger>: <what failed> → <what to do instead> (Sxxx) [seen]`. Triggers = words "
        "the game shows (enemy name or name stem like Justiciar, race like Undead, region/place) or ambush, "
        "escort, ranged, surrounded, boss. MERGE: a lesson for the same trigger and failure gets the new session "
        "appended (S023, S033) and its advice sharpened, never a second line; a retry that WON with the advice "
        "keeps it (add 'worked S034'); drop a lesson the logs show is wrong. ≤ 60 lessons, under `## Lessons`, "
        "newest last. A clean fight gives no lesson; a `[lesson]` about a tool or the harness (how to call "
        "combat_turn, a click quirk) is not a fight lesson: it belongs in strategy.md. Only facts from the logs "
        "(fairness).",
        "- PLAYTHROUGH.md (it is shown to the player at every session start): keep the `Main thread:` line and "
        "`## Party` current (who is in the active party, in camp, gone). Leave the `Now:` line as the player "
        "wrote it.",
        "- PRUNE what no longer helps the next session: a goal, lead or 'Blocked' item for a region or act the "
        "party has left, or one the logs show is done or superseded, is removed from objectives.md (the "
        "session logs keep the history) and becomes one line under '## Done' in quests.md with the session "
        "it ended; never keep two entries for the same goal. "
        "Stale advice costs the player turns (S025: objectives still listed an Act 1 door as Blocked).",
        f"- HANDOFF.md, LAST: the player reads it at the start and must not need to Read old session logs "
        f"mid-play. Write (or replace) ONE section at the very end, heading exactly `{HANDOFF_SECTION}`, "
        "≤ 12 lines: what the logs record that HANDOFF's text above doesn't say and the next session needs. "
        "If the last log has a later state than HANDOFF (e.g. 'session ended WITHOUT wrap-up'): where the party "
        "was, what it was doing, the next concrete step. Also open leads with their exact names (doors, keys, "
        "NPCs, passwords) and lessons the player looked up in old logs. Write 'nothing new' if nothing. "
        "Don't change one character above that heading (the runner reads its Save to load line).",
        f"Files no session changed for a while (update them first): {stale_txt}. If such a file truly has "
        f"nothing new, add or refresh one line `- Last updated: {sid} (no change)`.",
        "",
        "RULES:",
        "- Fairness: write only what the memory files already say. No facts from your own knowledge of BG3, "
        "no guesses presented as facts.",
        "- Every bullet in npcs/locations/quests/decisions/hypotheses needs a provenance tag: [seen] [told] "
        "[wiki] [guess] (copy the tag the log implies; [seen] for what the player observed).",
        "- NEVER edit anything in sessions/, nor HANDOFF.md above its last section (read-only; edits are "
        "reverted). Never delete facts "
        "from decisions.md or npcs.md: condense them under '## Archive' and add a line 'ARCHIVE: <why>'.",
        "- Keep files within budget (objectives ≤80 lines, quests ≤300, npcs/locations ≤400, party-build ≤150).",
        "- A hook reports lint errors after each edit (silence = clean): fix them right away. The runner lints "
        "after you and rolls back ALL your edits if you leave new errors.",
    ]
    news = harness_news(knowledge_dir=KNOWLEDGE)
    if news:
        lines += [
            "",
            "HARNESS FIXES (operator's harness-news.md, newest last). Memory that prescribes a hand workaround "
            "for something below is now WRONG and costs the player turns (S026: skipped giving an amulet "
            "because 'give is bugged'; clicked rest/trade buttons by coordinates). In strategy.md '## Harness and "
            "tool quirks' and in objectives.md: move each quirk or workaround a fix below retires to strategy.md "
            "'## Retired quirks (fixed in the harness; don't use the old workaround)' as ONE line "
            "'- RETIRED: <what works now> (<date>) [harness-news]'; drop click coordinates and screenshot+click "
            "steps for things a tool now does. Keep quirks no entry addresses.",
            *news,
        ]
    if refused:
        lines += [
            "",
            "REFUSED LEVEL-UP PLAN LINES (do this FIRST, in party-build.md): the game refused these stored lines this "
            "session, or the between-sessions check found them invalid or missing against the game's offers. Write or "
            "rewrite each `- <Name> L<n>: {json}` line so level_up completes next time: remove picks the game "
            "says are 'not offered' or 'already known', and fill every choice it says 'needs N more' with picks from "
            "ITS list ('can pick: …' / 'options={…}'), keeping the build's intent from the rest of party-build.md. "
            "Valid JSON, same keys (subclass, feat, abilities, spells, cantrips, skills, passives). Then add one line "
            f"`- NOTE {sid}: <Name> L<n> line rewritten from the game's offer list` under it.",
        ]
        for r in refused:
            lines.append(f"- {r['name']} L{r['level']}: {r['problems']}")
    if M.compaction_due(mem, sid_n + 1):
        last = M.last_compacted_session(mem)
        lines += [
            "",
            f"COMPACTION DUE (sessions after {M.session_id(last)} are in no chapter): AFTER the curated files, "
            f"follow {KNOWLEDGE / 'playbooks' / 'compaction.md'} (step 1 and the pruning in step 2, skip its "
            f"logging step). Cover {M.session_id(last + 1)}-{M.session_id(min(last + 10, sid_n))} in "
            f"chapters/C<nn>_S{last + 1:03d}-S{min(last + 10, sid_n):03d}_<slug>.md; write the chapter in ONE "
            "Write call once you have read those logs (a half-written chapter would be counted as done). "
            "If a chapter file for these sessions already exists, update it instead of adding a second one.",
        ]
    lines += ["", "Current lint report:", lint_report[-3000:], "",
              "End with one line: what you updated (files) and anything you could not do."]
    return "\n".join(lines)


def keeper_settings(mem: Path, knowledge: Path = KNOWLEDGE) -> dict:
    m, k = mem.resolve(), knowledge.resolve()
    deny = [f"{tool}(/{p})" for tool in ("Edit", "Write") for p in
            (f"{m}/sessions/**", f"{m}/.git/**", f"{k}/**")]
    return {
        "permissions": {
            "defaultMode": "acceptEdits",
            "allow": ["Read", "Glob", "Grep", f"Edit(/{m}/**)", f"Write(/{m}/**)"],
            "deny": deny,
        },
        "hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [{
            "type": "command",
            "command": f"{sys.executable} {SESSION_PY} memlint --memory-dir {m}"}]}]},
    }


def keeper_cmd(prompt: str, mem: Path, model: str, effort: str | None, session_uuid: str, resume: bool,
               claude: str = "claude", knowledge: Path = KNOWLEDGE) -> list[str]:
    # --restricted: no Bash/web tools, user/project/local settings ignored (so none of play/'s hooks: its
    # SessionStart hook would open a memory session), file tools confined to cwd (the memory dir) + --add-dir.
    cmd = [claude, "-p", prompt, "--model", model, "--output-format", "stream-json", "--verbose",
           "--restricted", "--tools", "Read,Edit,Write,Glob,Grep", "--strict-mcp-config",
           "--disable-slash-commands", "--add-dir", str(knowledge.resolve()),
           "--settings", json.dumps(keeper_settings(mem, knowledge))]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--resume", session_uuid] if resume else ["--session-id", session_uuid]
    return cmd


# ---------------------------------------------------------------- run

@dataclass
class KeeperResult:
    sid: str
    status: str = "error"          # ok | no_changes | lint_failed | error
    seconds: float = 0.0
    files: list[str] = field(default_factory=list)
    reverted: list[str] = field(default_factory=list)   # touched outside the writable set
    commit: str | None = None
    timed_out: bool = False
    exit_code: int | None = None
    lint_errors: list[str] = field(default_factory=list)
    detail: str = ""

    def summary(self) -> str:
        s = f"{self.sid} memory keeper: {self.status} in {self.seconds:.0f} s"
        if self.files:
            s += f"; {', '.join(self.files)}" + (f" (commit {self.commit})" if self.commit else "")
        if self.reverted:
            s += f"; reverted out-of-bounds edits to {', '.join(self.reverted)}"
        if self.timed_out:
            s += "; timed out"
        if self.lint_errors:
            s += f"; {len(self.lint_errors)} lint error(s): {self.lint_errors[0][:200]}"
        if self.detail:
            s += f"; {self.detail}"
        return s


def _errors(mem: Path, session: int) -> list[str]:
    return [f"{k}: {m}" for k, m in L.lint(mem, "any", session).errors]


def _run_claude(cmd: list[str], cwd: Path, log_path: Path, timeout: float) -> tuple[int | None, bool]:
    """Runs one claude call with its stream-json into log_path; kills its process group at the timeout."""
    env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    for k in ("BG3AI_SESSION_DEADLINE", "BG3AI_SESSION_WRAPUP", "BG3AI_METRICS_PATH"):
        env.pop(k, None)
    with open(log_path, "a") as out:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, text=True)
        try:
            return proc.wait(timeout=max(1.0, timeout)), False
        except subprocess.TimeoutExpired:
            for sig, wait in ((signal.SIGINT, 10), (signal.SIGKILL, 10)):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
            return proc.returncode, True


def commit(mem: Path, rels: list[str], message: str) -> str | None:
    """Commits only `rels` (the keeper's files), never the open session's other changes."""
    if not rels or not M.is_git_repo(mem):
        return None
    # A path that is gone from disk and from the index (someone else deleted and committed it meanwhile, S026)
    # makes `git add -- path` fail with "pathspec did not match": there's nothing of it left to commit.
    tracked = set(M.git(mem, "ls-files", "--", *rels, check=False).stdout.splitlines())
    rels = [r for r in rels if (mem / r).exists() or r in tracked]
    if not rels:
        return None
    M.git(mem, "add", "-A", "--", *rels)
    if M.git(mem, "diff", "--cached", "--quiet", "--", *rels, check=False).returncode == 0:
        return None
    subprocess.run(["git", *M.git_identity_args(mem), "-C", str(mem), "commit", "-q", "-m", message, "--", *rels],
                   check=True, capture_output=True, text=True)
    return M.git(mem, "rev-parse", "--short", "HEAD").stdout.strip()


def run_keeper(mem: Path, sid_n: int, log_base: Path, model: str = KEEPER_MODEL, effort: str | None = KEEPER_EFFORT,
               minutes: float = KEEPER_MINUTES, claude: str = "claude", knowledge: Path = KNOWLEDGE,
               refused: list[dict] | None = None) -> KeeperResult:
    """One keeper pass for the session that just ended (number sid_n). Never raises."""
    sid = M.session_id(sid_n)
    res = KeeperResult(sid)
    t0 = time.time()
    end = t0 + minutes * 60
    try:
        before_errors = set(_errors(mem, sid_n + 1))
        report = L.lint(mem, "any", sid_n + 1).render(mem)
        snap = snapshot(mem)
        log_path = Path(f"{log_base}.jsonl")
        sess = str(uuid.uuid4())
        prompt = build_prompt(mem, sid, sid_n, report, minutes, refused)
        rc, res.timed_out = _run_claude(keeper_cmd(prompt, mem, model, effort, sess, False, claude, knowledge),
                                        mem, log_path, end - time.time())
        res.exit_code = rc
        for attempt in range(2):
            changed = changes(mem, snap)
            res.reverted = sorted(set(res.reverted) | {r for r in changed if not writable(r)})
            restore(mem, snap, [r for r in changed if not writable(r)])
            if keep_handoff_head(mem, snap):
                res.reverted = sorted(set(res.reverted) | {"HANDOFF.md (player part)"})
            new = [e for e in _errors(mem, sid_n + 1) if e not in before_errors]
            left = end - time.time()
            if not new or attempt == 1 or left < FIX_MIN_SECONDS or res.timed_out:
                break
            fix = ("Memory lint now reports these NEW errors; fix them with minimal edits, then stop:\n"
                   + "\n".join(new)[-3000:])
            rc, res.timed_out = _run_claude(keeper_cmd(fix, mem, model, effort, sess, True, claude, knowledge),
                                            mem, log_path, left)
            res.exit_code = rc
        files = [r for r in changes(mem, snap) if writable(r)]
        if new:
            restore(mem, snap, files)
            res.status, res.lint_errors = "lint_failed", new
            res.detail = "all keeper edits rolled back"
        elif not files:
            res.status = "no_changes" if rc == 0 or res.timed_out else "error"
            if res.status == "error":
                res.detail = f"claude exit {rc}"
        else:
            res.files = files
            res.commit = commit(mem, files, f"{sid} memory-keeper: {', '.join(files)}")
            res.status = "ok"
    except Exception as e:  # noqa: BLE001 - a keeper failure never stops play
        res.status, res.detail = "error", f"{type(e).__name__}: {e}"[:300]
    res.seconds = round(time.time() - t0, 1)
    return res
