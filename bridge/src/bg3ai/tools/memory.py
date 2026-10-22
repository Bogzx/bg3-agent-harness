"""Memory and session tools: note, memory_*, session_start, rules, end_session.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .. import memtools, rules
from . import combat, core, reporting


def _note_lines(text: str, tag: str = "note") -> list[str]:
    """Session-log lines '- HH:MM [tag] text' from note text: one per non-empty line; a leading '- ', an
    'HH:MM ' (or 'HH:MM-HH:MM ') and a '[tag]' the agent wrote are kept, the rest filled in."""
    tag = re.sub(r"[^\w -]", "", tag or "note").strip() or "note"
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        if not line:
            continue
        m = combat._NOTE_TIME.match(line)
        hhmm = m.group(1) if m else time.strftime("%H:%M")
        if m:
            line = line[m.end():]
        if not combat._NOTE_TAG.match(line):
            line = f"[{tag}] {line}"
        out.append(f"- {hhmm} {line}")
    return out


def _append_note(text: str, tag: str = "note") -> tuple[int, Path | None]:
    """Append note lines to the session log (append-only, as the memory rules want). -> (lines, path)."""
    lines = _note_lines(text, tag)
    path = session._session_log()
    if not lines or path is None or not path.parent.is_dir():
        return 0, path
    with open(path, "a+") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() > 0:
            f.seek(f.tell() - 1)
            if f.read(1) != "\n":
                f.write("\n")
        f.write("\n".join(lines) + "\n")
    return len(lines), path


def _note_quiet(text: str) -> None:
    """note= on an action tool: written before the action starts; a failure never blocks the action."""
    if text and text.strip():
        try:
            _append_note(text)
        except OSError as e:
            core._log("note", {}, f"note not saved: {e}", 0)


@core.mcp.tool()
@reporting._guard
def note(text: str, tag: str = "note") -> str:
    """Append line(s) to your session log (memory/sessions/Sxxx.md) at once: no memory_read/memory_edit round trip, so a note never holds up play. One log line per line of text, '- HH:MM [tag] text'; start a line with its own [tag] (quest, decision, npc, combat, loot, puzzle, lesson, stuck, ...) to override tag. Curated files (objectives, npcs, quests, ...) are updated from the log between sessions; edit them yourself (memory_edit) only for plan changes you need this session. travel_to, queue_actions and do_actions take note= too (written with the action, no extra turn)."""
    t0 = time.monotonic()
    n, path = _append_note(text, tag)
    if not n:
        body = "Nothing noted (empty text)." if path is not None else "NOT SAVED: no session log."
    else:
        body = f"Noted {n} line{'s' if n != 1 else ''} in {path.name}."
    return reporting._finish("note", {}, body, t0)


# Memory files and the session lifecycle (memtools.py): the sandbox is enforced here, not by the client.

def _mem_finish(tool: str, args: dict, body: str, t0: float) -> str:
    """A memory tool's result: like any tool's (events, RUNNING, the clock) while a game runs; in the runner's
    fix-notes pass (no game) only the text."""
    if os.environ.get("BG3AI_FIX_NOTES"):
        text = core.H.scrub(body.rstrip())
        core._log(tool, args, text, (time.monotonic() - t0) * 1000)
        return text
    return reporting._finish(tool, args, body, t0)


@core.mcp.tool()
@reporting._guard
def memory_list(path: str = "memory") -> str:
    """List a folder of your notes: memory/ (yours, read-write), memory/sessions, memory/chapters, knowledge/ (read-only reference notes), knowledge/playbooks. Shows each file's line count."""
    t0 = time.monotonic()
    return _mem_finish("memory_list", {"path": path}, memtools.list_files(core.SANDBOX, path), t0)


@core.mcp.tool()
@reporting._guard
def memory_read(path: str, start_line: int = 1, max_lines: int = 400) -> str:
    """Read a note file: "memory/<file>" (e.g. memory/npcs.md, memory/sessions/S012.md) or "knowledge/<file>" (e.g. knowledge/playbooks/combat.md, knowledge/tools.md). A bare name means memory/. Long files come in pages: start_line for the next one."""
    t0 = time.monotonic()
    return _mem_finish("memory_read", {"path": path, "start_line": start_line},
                       memtools.read_file(core.SANDBOX, path, start_line, max_lines), t0)


@core.mcp.tool()
@reporting._guard
def memory_search(query: str, path: str = "memory", regex: bool = False) -> str:
    """Find a name or word in your notes (case-insensitive): every matching line as file:line: text. path narrows it (memory/npcs.md, memory/sessions, knowledge). regex=true for a regular expression."""
    t0 = time.monotonic()
    return _mem_finish("memory_search", {"query": query, "path": path},
                       memtools.search(core.SANDBOX, query, path, regex), t0)


@core.mcp.tool()
@reporting._guard
def memory_write(path: str, content: str) -> str:
    """Create or rewrite a whole memory file (memory/HANDOFF.md at wrap-up, memory/objectives.md, ...). Markdown only. Not for session logs (append-only: note) or snapshots/ (generated); knowledge/ is read-only. The result shows memory lint errors about the file: fix them."""
    t0 = time.monotonic()
    return _mem_finish("memory_write", {"path": path, "chars": len(content or "")},
                       memtools.write_file(core.SANDBOX, path, content or ""), t0)


@core.mcp.tool()
@reporting._guard
def memory_edit(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    """Replace exact text in a memory file (old_text must match once, whitespace included; replace_all=true for every match). For one changed line or section instead of a rewrite. The result shows memory lint errors about the file: fix them."""
    t0 = time.monotonic()
    return _mem_finish("memory_edit", {"path": path, "chars": len(new_text or "")},
                       memtools.edit_file(core.SANDBOX, path, old_text or "", new_text or "", replace_all), t0)


@core.mcp.tool()
@reporting._guard
def memory_append(path: str, text: str) -> str:
    """Append lines at the end of a memory file (decisions.md entries, a CHARACTER.md development line, a new npcs.md bullet). For the session log use note (it adds the time and tag)."""
    t0 = time.monotonic()
    return _mem_finish("memory_append", {"path": path, "chars": len(text or "")},
                       memtools.append_file(core.SANDBOX, path, text or ""), t0)


@core.mcp.tool()
@reporting._guard
def session_start(rules: bool = False) -> str:
    """Your memory briefing for this session: the session log it opened, CHARACTER.md, PLAYTHROUGH.md, HANDOFF.md, the last game state and harness notes. Call it first if your first message doesn't already show it, and again after your context was compacted. The first call of a session also starts with the rules of play; rules=true shows them again."""
    t0 = time.monotonic()
    briefing = core.LIFE.briefing()  # opens the session in manual mode: the key after it
    return _mem_finish("session_start", {"rules": rules}, core.RULES.wrap(briefing, _session_key(), force=rules), t0)


def _session_key() -> str:
    """The memory session the rules were last served for (rules.Once)."""
    try:
        return os.environ.get("BG3AI_SESSION") or str(memtools.backend().load_state(core.MEMORY).get("current") or "")
    except (OSError, ValueError):
        return ""


@core.mcp.prompt(name="rules", title="Rules of play", description="How to play through this server: the rules of "
            "this session (also in the instructions and the first session_start).")
def rules_prompt() -> str:
    return core.RULES.rules or "No rules file found (packages/harness/prompts, $BG3AI_RULES_PATH)."


@core.mcp.resource(rules.URI, name="rules", title="Rules of play", mime_type="text/markdown",
              description="The rules of this session, as served to MCP clients.")
def rules_resource() -> str:
    return rules_prompt()


@core.mcp.tool()
@reporting._guard
def end_session() -> str:
    """The last step of the wrap-up: checks the session can end (wrap-up time reached, HANDOFF.md written for this session, memory lint clean) and commits the memory. If it says NOT ENDED, do what it says (keep playing, or fix and call it again). Stop (no more tool calls) only when it reports the commit."""
    t0 = time.monotonic()
    if core.DEADLINE:
        core._times()  # the fight hold up to date for session.py's check ($BG3AI_FIGHT_HOLD_PATH)
    return _mem_finish("end_session", {}, core.LIFE.end(in_combat=_fight_on), t0)


def _fight_on() -> bool:
    """For an early end_session refusal: a fight is running (a fresh read; no game: no)."""
    core._hold["fight"] = None
    return core._fight_now()


# The next module (and so the rest, in file order) loads after this one; later ones used here.
# isort: off
from . import (  # noqa: E402, F401
    inventory, session,
)
