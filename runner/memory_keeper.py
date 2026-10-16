"""Memory keeper: a short `claude -p` pass between play sessions that brings the curated memory up to date.

The playing agent logs every session (sessions/Sxxx.md) but rarely updates the curated files (claude-run-2:
quests/npcs/locations unchanged for 8 sessions, objectives for 3, compaction due since S010). So after each
session the runner gives that job to a model (the play model by default, low effort, ≤5 min) that sees ONLY the memory
dir and the read-only knowledge dir: no game tools, no shell, no web. It reads the session logs
and HANDOFF.md and updates objectives/quests/npcs/locations/party-build/strategy, compacts old sessions into
a chapter when due, and the runner commits its edits as "<sid> memory-keeper: ..." so lint's staleness
counters reset.

When it runs (and why no write conflicts): after the session's memory is closed and BEFORE the next player
starts. It overlaps only the runner's between-session game prep (ensure_game, mod hot reload, settings,
check_all), which never writes memory; the runner joins it before the next `claude` starts. It never runs
during play: the player edits the same curated files mid-session and its session end commits `git add -A`,
so a concurrent keeper would race both. HANDOFF.md (the runner reads its "Save to load" line during the
prep) is the player's except its last section (HANDOFF_SECTION, the keeper's: what the next session needs
from the logs); the runner restores the player's part byte for byte if touched. sessions/ (append-only player
logs) is read-only for the keeper: denied by its permissions and restored by the runner if touched anyway.

Which client (run_keeper's client=, mcp=): Claude's default is Claude Code's own Read/Edit/Write/Glob/Grep in the
memory dir, fenced by a --settings deny list and a PostToolUse memlint hook (keeper_cmd), as tuned live. Any other
client (and Claude with mcp=True, for a benchmark that wants every model on the same path) keeps the memory through
the bg3 MCP server in keeper mode (keeper_server): the memory_* tools only, the same write rules enforced by the
server (bridge memtools.KeeperSandbox), lint in each write's result, the task as the server's instructions. The
runner's guards below (restore out-of-bounds edits, HANDOFF head byte for byte, CHARACTER core, lint rollback) run
after either path.

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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clients as C  # noqa: E402

SESSION_PY = REPO / "tools" / "memory" / "session.py"
KNOWLEDGE = REPO / "play" / "knowledge"

KEEPER_MODEL = "sonnet"         # run_keeper's own default; the runner passes the play model (--keeper-model same)
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


GIT_NAMES = {".git", ".gitattributes", ".gitignore", ".gitmodules"}   # never the keeper's (FAIR-1)


def writable(rel: str) -> bool:
    if any(part.lower() in GIT_NAMES for part in rel.split("/")):
        return False
    return (rel in WRITABLE_FILES or rel in ("HANDOFF.md", L.CHARACTER_FILE)
            or any(rel.startswith(d + "/") for d in WRITABLE_DIRS))


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


def keep_character_core(mem: Path, before: dict[str, bytes]) -> bool:
    """CHARACTER.md (the persona) is the player's, except that lines may be appended to its "## Character
    development" section. Anything else the keeper did to it (a rewritten core or older development lines, a new
    or deleted file) is put back, keeping only the appended lines. True when something had to be put back."""
    p = mem / L.CHARACTER_FILE
    old = before.get(L.CHARACTER_FILE)
    new = p.read_bytes() if p.is_file() else None
    if new == old:
        return False
    if old is None:            # written in the character-creation phase only, never by the keeper
        p.unlink()
        return True
    if new is None:
        p.write_bytes(old)
        return True
    old_t, new_t = old.decode("utf-8", "replace"), new.decode("utf-8", "replace")
    old_core, old_dev = L.character_split(old_t)
    new_core, new_dev = L.character_split(new_t)
    old_lines, new_lines = old_dev.splitlines(), new_dev.splitlines()
    if new_core == old_core and new_lines[:len(old_lines)] == old_lines:
        return False           # a pure append
    added = [ln for ln in new_lines[1:] if ln.strip() and ln not in old_lines]
    merged = old_t.rstrip("\n") + "\n"
    if added:
        if not old_dev:
            merged += f"\n{L.CHARACTER_DEV_HEADING}\n"
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
                 refused: list[dict] | None = None, tools: str = "files") -> str:
    """The keeper's task. tools: "files" (Claude Code's Read/Edit/Write/Glob/Grep in the memory dir, today's Claude
    pass) or "mcp" (the bg3 server's memory_* tools in keeper mode, any client): only the tool wording differs."""
    mcp = tools == "mcp"
    logs = ", ".join(f"sessions/{M.session_id(n)}.md" for n in logs_to_read(mem, sid_n)) or "(none)"
    stale = L.stale_files(mem, sid_n + 1)
    stale_txt = ", ".join(f"{rel} ({age} sessions)" for rel, age in stale) or "none"
    nxt = M.session_id(sid_n + 1)
    lines = [
        f"You are the MEMORY KEEPER of an AI's Baldur's Gate 3 playthrough. Session {sid} just ended; {nxt} "
        f"starts after you. You have {minutes:.0f} minutes, "
        + ("the memory_list/memory_read/memory_search/memory_write/memory_edit/memory_append tools only"
           if mcp else "Read/Edit/Write/Glob/Grep only") + ", and no game access. "
        "Work fast: few, targeted reads and edits. No questions: nobody answers.",
        "",
        "JOB: bring the curated memory files up to date from the player's own notes. "
        + ("Paths are memory/<file> (the memory dir; a bare name means memory/) and knowledge/<file> (read-only). "
           if mcp else "Your working directory is the memory dir. ") + "Read HANDOFF.md, objectives.md, PLAYTHROUGH.md, then these session logs (newest "
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
        "keeps it (add 'worked S034'); drop a lesson the logs show is wrong. A death always gets a lesson: a "
        "`[fight]` line with `DIED:` (party member) or `ally DIED:` (an allied NPC such as Jaheira or Aylin: a "
        "story ally may be gone for good) → `- WHEN <enemy>, <place>: <who> died (how) → protect them (heal/shield "
        "first, keep them back) and revive at once (Scroll of Revivify on the body; Withers only for party "
        "members) (Sxxx) [seen]`; in PLAYTHROUGH.md `## Party` and npcs.md note a dead ally that wasn't revived. "
        "A death or near-death from the GROUND, not a fight (a fall, a jump into a chasm, a walk tool's `movement "
        "was cancelled … hp=N->0`, no enemy involved) is a route lesson, not a tactic: triggers = the place names "
        "plus the keyword `fall` (`- WHEN <place>, fall: <what killed them> → <the safe way> (Sxxx) [seen]`); the "
        "bridge shows it on entering the place, never at a fight start. "
        "≤ 60 lessons, under `## Lessons`, "
        "newest last. A clean fight gives no lesson; a `[lesson]` about a tool or the harness (how to call "
        "combat_turn, a click quirk) is not a fight lesson: it belongs in strategy.md. Only facts from the logs "
        "(fairness).",
        f"- {L.CHARACTER_FILE} (if it exists: the character the player roleplays): never change it above "
        f"`{L.CHARACTER_DEV_HEADING}` (reverted). When the logs show the character changed (a vow kept or broken, a "
        "bond formed, a belief shaken), APPEND one line at the end: `- Sxxx: <what changed and why> [seen]`.",
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
        ("- Each write's result ends with its lint check ('lint: ok' or the errors): fix errors right away. "
         if mcp else "- A hook reports lint errors after each edit (silence = clean): fix them right away. ")
        + "The runner lints after you and rolls back ALL your edits if you leave new errors.",
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
            f"follow {'knowledge/playbooks/compaction.md' if mcp else KNOWLEDGE / 'playbooks' / 'compaction.md'} (step 1 and the pruning in step 2, skip its "
            f"logging step). Cover {M.session_id(last + 1)}-{M.session_id(min(last + 10, sid_n))} in "
            f"chapters/C<nn>_S{last + 1:03d}-S{min(last + 10, sid_n):03d}_<slug>.md; write the chapter in ONE "
            f"{'memory_write' if mcp else 'Write'} call once you have read those logs (a half-written chapter would be counted as done). "
            "If a chapter file for these sessions already exists, update it instead of adding a second one.",
        ]
    lines += ["", "Current lint report:", lint_report[-3000:], "",
              "End with one line: what you updated (files) and anything you could not do."]
    return "\n".join(lines)


def keeper_settings(mem: Path, knowledge: Path = KNOWLEDGE) -> dict:
    m, k = mem.resolve(), knowledge.resolve()
    deny = [f"{tool}(/{p})" for tool in ("Edit", "Write") for p in (f"{m}/sessions/**", f"{k}/**")]
    # The memory repo's git dir and git control files: never read or written (FAIR-1; git runs on them in hooks).
    deny += [f"{tool}(/{p})" for tool in ("Read", "Edit", "Write") for p in
             (f"{m}/.git/**", f"{m}/**/.git/**", f"{m}/**/.git*", f"{k}/**/.git*")]
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


# ---------------------------------------------------------------- any client: the bg3 server in keeper mode

def keeper_server(mem: Path, knowledge: Path = KNOWLEDGE, task: Path | None = None) -> dict:
    """The bg3 stdio server in keeper mode (bridge server.keeper_server): only the memory tools, the keeper's
    write rules enforced server-side (memtools.KeeperSandbox from WRITABLE_FILES/WRITABLE_DIRS + HANDOFF.md +
    CHARACTER.md, sessions/ and knowledge/ read-only, HANDOFF.md only below HANDOFF_SECTION) and `task` as its
    instructions. play/.mcp.json's command, with its --project made absolute (the client runs in the memory dir)."""
    s = C.bg3_server()
    args = list(s.get("args") or [])
    for i, a in enumerate(args[:-1]):
        if a == "--project":
            args[i + 1] = str((C.PLAY_DIR / args[i + 1]).resolve())
    env = {**(s.get("env") or {}), "BG3AI_KEEPER_MODE": "1", "BG3AI_MEMORY_DIR": str(mem.resolve()),
           "BG3_KNOWLEDGE_DIR": str(knowledge.resolve()),
           "BG3AI_KEEPER_WRITABLE": ",".join([*WRITABLE_FILES, "HANDOFF.md", L.CHARACTER_FILE,
                                              *(d + "/" for d in WRITABLE_DIRS)]),
           "BG3AI_KEEPER_HANDOFF_SECTION": HANDOFF_SECTION, "BG3AI_AUTO_SESSION": "0", "BG3AI_MOVIE_SKIP": "0",
           "BG3AI_METRICS_SAMPLE_S": "0"}
    if task is not None:
        env["BG3AI_KEEPER_TASK"] = str(Path(task).resolve())
    return {"type": "stdio", "command": s["command"], "args": args, "env": env, "timeout": s.get("timeout") or 110_000}


def keeper_client(name: str, mem: Path, log_base: Path, knowledge: Path = KNOWLEDGE, binary: str | None = None):
    """A clients.py Client whose only tools are the keeper-mode bg3 server's (its config files next to log_base)."""
    server = keeper_server(mem, knowledge, Path(f"{log_base}.task.md"))
    kw = {"binary": binary} if binary else {}
    if name == "claude":
        cfg = Path(f"{log_base}.mcp.json")
        cfg.write_text(json.dumps({"mcpServers": {"bg3": server}}, indent=1) + "\n")
        return C.ClaudeClient(mcp_config=cfg, settings={"permissions": {"allow": ["mcp__bg3__*"]}}, **kw)
    return C.make(name, server=server, cwd=mem, **kw)


def _session_from_log(client, log_path: Path) -> str | None:
    """The conversation id a client that doesn't take one up front (codex, gemini) printed in its init event."""
    sid = None
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for ln in lines:
        for ev in client.parse(ln):
            if ev.kind == "init" and ev.session_id:
                sid = ev.session_id
    return sid


def client_cmd(client, prompt: str, mem: Path, model: str, effort: str | None, session: str | None, resume: bool,
               log_base: Path) -> tuple[list[str], dict]:
    """(argv, extra env) of one keeper call through `client` (keeper_client)."""
    req = C.Request(prompt=prompt, model=model, session_dir=Path(log_base).parent, session=session, resume=resume,
                    effort=effort, tag=Path(log_base).name)
    client.write_config(req)
    return client.command(req), client.env(req)


def keeper_cmd(prompt: str, mem: Path, model: str, effort: str | None, session_uuid: str, resume: bool,
               claude: str = "claude", knowledge: Path = KNOWLEDGE) -> list[str]:
    # --restricted: no Bash/web tools, user/project/local settings ignored (none of play/'s settings),
    # file tools confined to cwd (the memory dir) + --add-dir. The keeper still uses Claude Code's file tools.
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


def _run_claude(cmd: list[str], cwd: Path, log_path: Path, timeout: float,
                extra_env: dict | None = None) -> tuple[int | None, bool]:
    """Runs one claude (or other client) call with its stream into log_path; kills its process group at the
    timeout (the keeper-mode MCP server is in that group too)."""
    env = {**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", **(extra_env or {})}
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
    M.git(mem, *M.git_identity_args(mem), "commit", "-q", "--no-verify", "-m", message, "--", *rels)
    return M.git(mem, "rev-parse", "--short", "HEAD").stdout.strip()


def run_keeper(mem: Path, sid_n: int, log_base: Path, model: str = KEEPER_MODEL, effort: str | None = KEEPER_EFFORT,
               minutes: float = KEEPER_MINUTES, claude: str = "claude", knowledge: Path = KNOWLEDGE,
               refused: list[dict] | None = None, client: str = "claude", mcp: bool | None = None,
               binary: str | None = None) -> KeeperResult:
    """One keeper pass for the session that just ended (number sid_n). Never raises.

    client: the clients.py client that keeps the memory (the play client, so a benchmark compares one model end to
    end). mcp: through the bg3 server in keeper mode (keeper_server); None = True for every client but claude,
    whose default stays today's pass with Claude Code's file tools (keeper_cmd). binary: the client's executable
    (default: `claude` for claude, else the client's own)."""
    sid = M.session_id(sid_n)
    via_mcp = client != "claude" if mcp is None else mcp
    who = client
    res = KeeperResult(sid)
    t0 = time.time()
    end = t0 + minutes * 60
    try:
        before_errors = set(_errors(mem, sid_n + 1))
        report = L.lint(mem, "any", sid_n + 1).render(mem)
        snap = snapshot(mem)
        log_path = Path(f"{log_base}.jsonl")
        sess = str(uuid.uuid4())
        prompt = build_prompt(mem, sid, sid_n, report, minutes, refused, tools="mcp" if via_mcp else "files")
        cli = None
        if via_mcp:
            Path(f"{log_base}.task.md").write_text(prompt + "\n", encoding="utf-8")   # the server's instructions
            cli = keeper_client(client, mem, log_base, knowledge, binary or (claude if client == "claude" else None))
            sess = sess if cli.assigns_session else None

        def call(text: str, resume: bool, left: float) -> tuple[int | None, bool]:
            if cli is None:
                return _run_claude(keeper_cmd(text, mem, model, effort, sess, resume, claude, knowledge),
                                   mem, log_path, left)
            cmd, env = client_cmd(cli, text, mem, model, effort, sess, resume, log_base)
            return _run_claude(cmd, mem, log_path, left, env)

        rc, res.timed_out = call(prompt, False, end - time.time())
        if cli is not None and not cli.assigns_session:
            sess = _session_from_log(cli, log_path)
        res.exit_code = rc
        for attempt in range(2):
            changed = changes(mem, snap)
            res.reverted = sorted(set(res.reverted) | {r for r in changed if not writable(r)})
            restore(mem, snap, [r for r in changed if not writable(r)])
            if keep_handoff_head(mem, snap):
                res.reverted = sorted(set(res.reverted) | {"HANDOFF.md (player part)"})
            if keep_character_core(mem, snap):
                res.reverted = sorted(set(res.reverted) | {f"{L.CHARACTER_FILE} (core)"})
            new = [e for e in _errors(mem, sid_n + 1) if e not in before_errors]
            left = end - time.time()
            if not new or attempt == 1 or left < FIX_MIN_SECONDS or res.timed_out:
                break
            fix = ("Memory lint now reports these NEW errors; fix them with minimal edits, then stop:\n"
                   + "\n".join(new)[-3000:])
            if cli is not None and not (cli.supports_resume and sess):
                break   # no conversation to resume (the client printed no session id)
            rc, res.timed_out = call(fix, True, left)
            res.exit_code = rc
        files = [r for r in changes(mem, snap) if writable(r)]
        if new:
            restore(mem, snap, files)
            res.status, res.lint_errors = "lint_failed", new
            res.detail = "all keeper edits rolled back"
        elif not files:
            res.status = "no_changes" if rc == 0 or res.timed_out else "error"
            if res.status == "error":
                res.detail = f"{who} exit {rc}"
        else:
            res.files = files
            res.commit = commit(mem, files, f"{sid} memory-keeper: {', '.join(files)}")
            res.status = "ok"
    except Exception as e:  # noqa: BLE001 - a keeper failure never stops play
        res.status, res.detail = "error", f"{type(e).__name__}: {e}"[:300]
    res.seconds = round(time.time() - t0, 1)
    return res
