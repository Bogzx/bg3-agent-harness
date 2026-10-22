"""The memory keeper's prompt: what the pass is told about the session that just ended (memory/keeper.py).

Which session logs it reads (since the stalest curated file, never before the last keeper pass or chapter), the
plans the game refused, what is already DONE (memory and fight records) and the harness news it may pass on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bg3play.jsontypes import is_list, is_obj
from bg3play.memory import lint, memlib
from bg3play.memory.keeper_files import (
    HANDOFF_SECTION,
    KNOWLEDGE,
    MAX_LOGS,
    bg3progress,
    milestones,
)


def last_keeper_session(mem: Path) -> int | None:
    """The session whose keeper pass was last committed ("Sxxx memory-keeper: ..."), or None."""
    if not memlib.is_git_repo(mem) or not memlib.has_head(mem):
        return None
    out = memlib.git(mem, "log", "-1", "--format=%s", "--grep=^S[0-9]* memory-keeper:", check=False).stdout.strip()
    m = re.match(r"S(\d+) memory-keeper:", out)
    return int(m.group(1)) if m else None


def logs_to_read(mem: Path, sid_n: int) -> list[int]:
    """Session logs since the stalest curated file was last committed (at most MAX_LOGS, oldest first). MEM-1: never
    from before the last committed keeper pass or the last chapter (those logs are already in the curated files or a
    chapter; one never-touched file used to pull `since` back to S000), and always the session that just ended."""
    touched = [memlib.last_touched_session(mem, rel) for rel in memlib.STALE_AFTER]
    known = [t for t in touched if t is not None and t >= 0]
    since = min(known) if known else 0
    kept = last_keeper_session(mem)
    if kept is not None:
        since = max(since, kept + 1)
    since = min(max(since, memlib.last_compacted_session(mem) + 1), sid_n)
    nums = [n for n in memlib.session_numbers(mem) if since <= n <= sid_n]
    return nums[-MAX_LOGS:]


# vis V6 (2026-10-17): a fight record before v2 counted a hit's rider (1-3 Radiant beside 12 Slashing) or a per-hit
# shield (Orin's Unstoppable) as a resisted damage type, so its [fight] line's "resisted:" field and "<Type> did
# little to <enemy>" facts are false (~80 in claude-run-2 S035-S065, e.g. lessons.md "EB/Radiant do ~0 vs Avatar").
# vis V18b (2026-10-18): a v2 record (S066-S077) judged "did little" from 2 hits, from killing blows on 1-HP enemies
# and against other attacks' damage (S076: "Radiant did little to Predator Werewolf", 11.7/hit beside Fireball's 33).
DEBRIEF_V = 3


def old_debrief_logs(mem: Path, sessions_dir: Path | None, nums: list[int]) -> list[int]:
    """The sessions among nums whose log has [fight] lines written before the v3 debrief: one of the session's
    fight records (sessions_dir/Sxxx[-rN].fights.jsonl, written with each line) lacks v >= DEBRIEF_V, or it has none
    (the log alone can't show its lines are new)."""
    out: list[int] = []
    for n in nums:
        sid = memlib.session_id(n)
        try:
            if "[fight]" not in (mem / "sessions" / f"{sid}.md").read_text(encoding="utf-8", errors="replace"):
                continue
        except OSError:
            continue
        vs: list[int] = []
        paths = (
            [sessions_dir / f"{sid}.fights.jsonl", *sorted(sessions_dir.glob(f"{sid}-*.fights.jsonl"))]
            if sessions_dir is not None
            else []
        )
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for ln in lines:
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if is_obj(r):
                    vs.append(int(r.get("v") or 1))
        if not vs or min(vs) < DEBRIEF_V:
            out.append(n)
    return out


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


def refused_plans(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{name, level, problems}] for stored party-build.md plan lines the game refused this session (level_up
    results, runtime/actions.jsonl) and that no later call completed. level_up's result already carries what the
    game offered ("can pick: …", "options={…}"): the keeper rewrites the line from it, so the next level_up
    completes instead of failing once per refused line (live 12:2x: Wyll L5 "Thirsting Blade: not offered")."""
    last: dict[tuple[str, int], dict[str, Any]] = {}
    for rec in actions:
        if rec.get("tool") != "level_up" or not isinstance(rec.get("result"), str):
            continue
        text = rec["result"]
        start = 0
        for m in _PLAN_NOTE.finditer(text):  # a camp member's result holds one chunk per level: "L4: … (plan: …)"
            chunk = text[start : m.start()]
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


DONE_MAX_LINES = 40
DONE_MAX_CHARS = 160
_DONE_IN_OBJECTIVES = re.compile(r"\bDONE S\d{3}\b")
_DONE_HEADING = re.compile(r"^## .*\(DONE S\d{3}\)")


def done_records(mem: Path) -> list[str]:
    """What the memory files already record as finished: objectives.md's `DONE Sxxx` lines, quests.md's
    `## <quest> (DONE Sxxx)` headings, then its `## Done` bullets in file order (newest first there), DONE_MAX_LINES in
    all. The keeper checks each
    next step and side-list item against them (S078: the side list still sent the party to the Sorcerous Sundries
    vault, "DONE S069: Annals of Karsus taken" three lines above it in objectives.md)."""
    out: list[str] = []
    bullets: list[str] = []

    def read(rel: str) -> list[str]:
        try:
            return (mem / rel).read_text(errors="replace").splitlines()
        except OSError:
            return []

    for ln in read("objectives.md"):
        if _DONE_IN_OBJECTIVES.search(ln):
            out.append("objectives.md: " + ln.strip().lstrip("-0123456789. ").strip())
    in_done = False
    for ln in read("quests.md"):
        if ln.startswith("## "):
            in_done = ln.strip() == "## Done"
            if _DONE_HEADING.match(ln):
                out.append("quests.md: " + ln[3:].strip())
        elif in_done and ln.startswith("- ") and not ln.startswith("- Last updated"):
            bullets.append("quests.md Done: " + ln[2:].strip())
    return [r[:DONE_MAX_CHARS] for r in (out + bullets)[:DONE_MAX_LINES]]


GAME_DONE_MAX_LINES = 30
_LEG = re.compile(r"(S\d{3})(?:-r(\d+))?\.(?:progress|fights)\.jsonl")


def game_done_records(sessions_dir: Path | None) -> list[str]:
    """What the runner's own records (sessions_dir = runtime/sessions/<playthrough>) show is over, beside the memory:
    the story milestones of kind quest_done reached (bg3ai.milestones: progress_state.json + Sxxx.progress.jsonl),
    and the bosses killed in a fight that wasn't reloaded (a fight record's kill whose name has a story_kill
    progress event: someone the party had talked to; the GAME_DONE_MAX_LINES newest). S085: PLAYTHROUGH.md's
    Main thread said "next Cazador" from S076 to S084, the player copied it into HANDOFF as "Cazador still pending
    (7 tries lost)"; the milestone "Cazador defeated (S077)" says otherwise. Cazador's own fight record can't (he
    left at 34% into his sarcophagus), and the journal kept a Call of the Blood objective open."""
    if sessions_dir is None or not sessions_dir.is_dir():
        return []

    def legs(kind: str) -> list[tuple[str, Path]]:  # (Sxxx, path) per session and crash-resume leg, in play order
        found = [(m, p) for p in sessions_dir.glob(f"S*.{kind}.jsonl") if (m := _LEG.fullmatch(p.name))]
        return [(m.group(1), p) for m, p in sorted(found, key=lambda x: (x[0].group(1), int(x[0].group(2) or 0)))]

    progress: list[tuple[str, list[dict[str, Any]]]] = [(sid, bg3progress.load(p)) for sid, p in legs("progress")]
    try:
        seen = json.loads((sessions_dir / "progress_state.json").read_text())
    except (OSError, ValueError):
        seen = None
    out: list[str] = []
    if seen or any(recs for _s, recs in progress):
        kinds = {mid: kind for mid, _a, _l, kind, _p in milestones.MILESTONES}
        for i in milestones.status(seen, progress)["items"]:
            if i["reached"] and kinds.get(i["id"]) == "quest_done":
                out.append(f"milestone: {i['label']}" + (f" ({i['session']})" if i["session"] else ""))
    talked_dead = {
        str(r.get("what"))
        for _s, recs in progress
        for r in recs
        if r.get("type") == "progress" and r.get("kind") == "story_kill"
    }
    killed: dict[str, str] = {}
    for sid, path in legs("fights"):
        recs: list[dict[str, Any]] = bg3progress.load(path)
        for r in recs:
            kills: Any = r.get("kills") or []
            if r.get("reloaded") or not is_list(kills):
                continue
            for k in kills:
                name = str(k.get("name") or "") if is_obj(k) else ""
                if name in talked_dead and name not in killed:
                    killed[name] = sid
    lines = [f"fight record: {name} killed in a fight not reloaded ({sid})" for name, sid in killed.items()]
    return [r[:DONE_MAX_CHARS] for r in out + lines[-GAME_DONE_MAX_LINES:]]


def build_prompt(
    mem: Path,
    sid: str,
    sid_n: int,
    lint_report: str,
    minutes: float,
    refused: list[dict[str, Any]] | None = None,
    tools: str = "files",
    sessions_dir: Path | None = None,
) -> str:
    """The keeper's task. tools: "files" (Claude Code's Read/Edit/Write/Glob/Grep in the memory dir, today's Claude
    pass) or "mcp" (the bg3 server's memory_* tools in keeper mode, any client): only the tool wording differs.
    sessions_dir: the runner's runtime/sessions/<playthrough> (the fight records, see old_debrief_logs)."""
    mcp = tools == "mcp"
    nums = logs_to_read(mem, sid_n)
    logs = ", ".join(f"sessions/{memlib.session_id(n)}.md" for n in nums) or "(none)"
    stale = lint.stale_files(mem, sid_n + 1)
    stale_txt = ", ".join(f"{rel} ({age} sessions)" for rel, age in stale) or "none"
    nxt = memlib.session_id(sid_n + 1)
    lines = [
        f"You are the MEMORY KEEPER of an AI's Baldur's Gate 3 playthrough. Session {sid} just ended; {nxt} "
        f"starts after you. You have {minutes:.0f} minutes, "
        + (
            "the memory_list/memory_read/memory_search/memory_write/memory_edit/memory_append tools only"
            if mcp
            else "Read/Edit/Write/Glob/Grep only"
        )
        + ", and no game access. "
        "Work fast: few, targeted reads and edits. No questions: nobody answers.",
        "",
        "JOB: bring the curated memory files up to date from the player's own notes. "
        + (
            "Paths are memory/<file> (the memory dir; a bare name means memory/) and knowledge/<file> (read-only). "
            if mcp
            else "Your working directory is the memory dir. "
        )
        + "Read HANDOFF.md, objectives.md, PLAYTHROUGH.md, then these session logs (newest "
        f"matter most): {logs}. Then edit:",
        "- objectives.md: the current Main goal, its why and its next step, matching HANDOFF.md and the latest "
        "log. Mark finished goals done. Check EVERY next step and side-list item (also ones you copy from HANDOFF.md) "
        "against DONE IN MEMORY below and the logs' `[quest]` lines: one they show finished is dropped, even when the "
        "game's journal or map still shows its marker (S078: the side list kept 'Sorcerous Sundries vault' after "
        "'DONE S069: Annals of Karsus taken'; the map's Tolna's Office marker stayed, and the party walked to it).",
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
        f"- {lint.CHARACTER_FILE} (if it exists: the character the player roleplays): never change it above "
        f"`{lint.CHARACTER_DEV_HEADING}` (reverted). When the logs show the character changed (a vow kept or broken, a "
        "bond formed, a belief shaken), APPEND one line at the end: `- Sxxx: <what changed and why> [seen]`.",
        "- PLAYTHROUGH.md (it is shown to the player at every session start): keep the `Main thread:` line and "
        "`## Party` current (who is in the active party, in camp, gone). The Main thread's `next …` never names "
        "a boss or goal that DONE IN MEMORY or a log shows finished (S077-S084 it still said 'next Cazador' though "
        "quests.md had Cazador DONE S077; the S085 player copied it into HANDOFF as 'Cazador still pending'). "
        "Leave the `Now:` line as the player wrote it.",
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
        "NPCs, passwords) and lessons the player looked up in old logs. A next step, side-list item or "
        "'still pending / not yet / try again' claim (a boss, a quest) in HANDOFF's text above that DONE IN MEMORY "
        "or a log (`[combat]`, `[decision]`, `[quest]`, `[save]` lines) shows finished gets one line here: `- <item> "
        "is done (Sxxx: <what the record says>): skip it.` Check such claims against those records, not the "
        "game's journal: it can keep an objective open after the boss is dead (Call of the Blood still read "
        "'trying to recover in his sarcophagus' after Cazador was killed in S077). Write 'nothing new' if nothing. "
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
        (
            "- Each write's result ends with its lint check ('lint: ok' or the errors): fix errors right away. "
            if mcp
            else "- A hook reports lint errors after each edit (silence = clean): fix them right away. "
        )
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
    due = memlib.compaction_due(mem, sid_n + 1)
    last = 0  # set when due, and only read then
    if due:
        last = memlib.last_compacted_session(mem)
        nums = sorted(set(nums) | set(range(last + 1, min(last + 10, sid_n) + 1)))
    old = old_debrief_logs(mem, sessions_dir, nums)
    if old:
        lines += [
            "",
            "OLD FIGHT DEBRIEFS (a harness bug, fixed 2026-10-17 and 2026-10-18): in "
            + ", ".join(f"sessions/{memlib.session_id(n)}.md" for n in old)
            + " the `[fight]` lines' `resisted: …` field and every `<Type> did little to <enemy>` fact are WRONG "
            "(before S066: a damage rider or an enemy's per-hit shield was read as a resisted damage type; S066-S077: "
            "'did little' was judged per hit from 2 hits, from killing blows capped by a 1-HP enemy's HP, and "
            "against other attacks' damage, e.g. Sacred Flame beside Fireball). Take no resistance, "
            "immunity or 'did little / does ~0' fact from them, nor from a `[lesson]` that rests only on one, into "
            "any file or chapter; the rest of those lines (grade, downs, deaths, damage totals, unused items) stands.",
        ]
    if due:
        compaction = "knowledge/playbooks/compaction.md" if mcp else KNOWLEDGE / "playbooks" / "compaction.md"
        lines += [
            "",
            f"COMPACTION DUE (sessions after {memlib.session_id(last)} are in no chapter): AFTER the curated files, "
            f"follow {compaction} (step 1 and the pruning in step 2, skip its logging step). "
            f"Cover {memlib.session_id(last + 1)}-{memlib.session_id(min(last + 10, sid_n))} in "
            f"chapters/C<nn>_S{last + 1:03d}-S{min(last + 10, sid_n):03d}_<slug>.md; write the chapter in ONE "
            f"{'memory_write' if mcp else 'Write'} call once you have read those logs "
            "(a half-written chapter would be counted as done). "
            "If a chapter file for these sessions already exists, update it instead of adding a second one.",
        ]
    done = done_records(mem) + game_done_records(sessions_dir)
    if done:
        lines += [
            "",
            (
                "DONE IN MEMORY (finished per the memory files and the runner's milestones and fight records; "
                "no next step, side-list item or 'still pending' claim may name one):"
            ),
            *(f"- {r}" for r in done),
        ]
    lines += [
        "",
        "Current lint report:",
        lint_report[-3000:],
        "",
        "End with one line: what you updated (files) and anything you could not do.",
    ]
    return "\n".join(lines)
