"""The playing agent's memory and session harness, served as MCP tools by server.py.

Until 2026-10-13 this was Claude Code's: file tools (Read/Edit/Write/Grep/Glob) fenced by a PreToolUse hook
(play/.claude/hooks/fairness_guard.py), a SessionStart hook that opened the session and injected the memory, a
PostToolUse lint hook after edits, and Stop/SessionEnd hooks that linted and committed. Here the server does all of
it, so any MCP client gets the same harness and the server, not the client, enforces the sandbox:

* Sandbox: paths are "memory/<rel>" (the playthrough's memory repo; a bare path means memory/) or
  "knowledge/<rel>" (read-only reference notes and playbooks). Refused: absolute paths, "..", any dot-file or
  dot-dir (the memory repo's .git and .git* control files, which host-side git acts on: FAIR-1; the harness's
  .session.json), any symlink below a root, writes to knowledge/ (FAIR-2: it is shared by every playthrough),
  snapshots/ (generated), markers.json (place_marker), whole-file rewrites of a session log (append-only), files
  other than .md, and CHARACTER.md changes other than appends to its "## Character development" once it is
  committed.
* Lint: every write runs tools/memory/lint.py and the result names the errors about that file (was the memlint
  hook).
* Lifecycle (tools/memory/session.py): the runner opens the session and puts the briefing in its kickoff, and sets
  BG3AI_SESSION; the server then only attaches. Without the runner the server process's first tool call opens the
  session and its result starts with the briefing (was the SessionStart hook; a plain import of the server, as the
  dev checks do, never opens one; BG3AI_AUTO_SESSION=0 turns it off); session_start shows it again. end_session makes the
  Stop hook's check (keep playing / write the handoff / lint errors / committed); the runner makes the same check
  when the client exits. Manual sessions (no deadline) get the elapsed-time wrap-up reminder in results.
* Keeper mode (server.keeper_server, BG3AI_KEEPER_MODE=1): the runner's memory keeper pass between sessions, for
  any client: the memory tools only, over KeeperSandbox (the keeper's writable files; sessions/ read-only; HANDOFF.md
  only in its last section), and no lifecycle.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Callable

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tools" / "memory"))
import lint as L  # noqa: E402
import memlib as M  # noqa: E402
import session as S  # noqa: E402

ROOTS = ("memory", "knowledge")
WRITE_SUFFIXES = (".md",)
READ_MAX_CHARS = 30_000
READ_MAX_LINES = 400
WRITE_MAX_CHARS = 120_000
SEARCH_MAX_HITS = 40
TEXT_SUFFIXES = (".md", ".json", ".txt")


class Refused(ValueError):
    """A memory-tool call outside the sandbox; the message is the tool result."""


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class Sandbox:
    """The memory dir (read/write) and the knowledge dir (read-only). The getters are read on every call, so a
    test (or the server) can repoint them."""

    def __init__(self, memory: Callable[[], Path], knowledge: Callable[[], Path]):
        self._memory, self._knowledge = memory, knowledge

    @property
    def memory(self) -> Path:
        return Path(self._memory())

    @property
    def knowledge(self) -> Path:
        return Path(self._knowledge())

    def root_dir(self, name: str) -> Path:
        return self.memory if name == "memory" else self.knowledge

    def resolve(self, path: str, write: bool = False) -> tuple[str, Path, str]:
        """(root name, real path, path relative to the root) for an agent path, or Refused."""
        raw = (path or "").strip().replace("\\", "/")
        if not raw or "\0" in raw:
            raise Refused("REFUSED: give a path like memory/npcs.md or knowledge/tools.md")
        if raw.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", raw):
            raise Refused(f"REFUSED: {raw!r}: use a relative path under memory/ or knowledge/")
        parts = [p for p in PurePosixPath(raw).parts if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise Refused(f"REFUSED: {raw!r}: '..' is outside memory/ and knowledge/")
        root = parts[0] if parts and parts[0] in ROOTS else "memory"
        rest = parts[1:] if parts and parts[0] in ROOTS else parts
        if any(p.startswith(".") for p in rest):
            raise Refused(f"REFUSED: {raw!r}: hidden and git files (.git, .gitattributes, .session.json, ...) are "
                          f"outside the sandbox")
        base = self.root_dir(root)
        if not base.is_dir():
            raise Refused(f"REFUSED: there is no {root}/ directory (operator: tools/memory/new_playthrough.py)")
        real_root = os.path.realpath(base)   # play/memory itself is normally a symlink into playthroughs/
        cur = real_root
        for p in rest:  # no symlink below the root: nothing can point the tools outside it
            cur = os.path.join(cur, p)
            if os.path.islink(cur):
                raise Refused(f"REFUSED: {raw!r}: symlinks are outside the sandbox")
        real = os.path.realpath(cur)
        if not _within(real, real_root):
            raise Refused(f"REFUSED: {raw!r} resolves outside {root}/")
        rel = "/".join(rest)
        if write:
            self._check_write(root, rel, Path(real))
        return root, Path(real), rel

    @staticmethod
    def _check_write(root: str, rel: str, p: Path) -> None:
        if root == "knowledge":
            raise Refused(f"REFUSED: knowledge/ is read-only (reference notes shared by every playthrough). "
                          f"Write what you learned in memory/ (strategy.md, lessons via note('[lesson] ...')).")
        if not rel:
            raise Refused("REFUSED: give a file path under memory/")
        top = rel.split("/", 1)[0]
        if top == "snapshots":
            raise Refused("REFUSED: memory/snapshots/ is generated from the live game; never edit it")
        if rel == "markers.json":
            raise Refused("REFUSED: markers.json changes only through place_marker / delete_marker")
        if not rel.endswith(WRITE_SUFFIXES):
            raise Refused(f"REFUSED: {rel}: memory notes are Markdown (.md) files")
        if p.exists() and not p.is_file():
            raise Refused(f"REFUSED: {rel} is a directory")

    def check_content(self, rel: str, old: str | None, new: str) -> str | None:
        """None if writing `new` over `old` to memory/<rel> is allowed, else why not (KeeperSandbox: HANDOFF.md)."""
        return None

    def display(self, root: str, rel: str) -> str:
        return f"{root}/{rel}" if rel else f"{root}/"


class KeeperSandbox(Sandbox):
    """The memory keeper's sandbox (server.keeper_server, BG3AI_KEEPER_MODE=1): the runner's keeper pass between
    sessions (runner/memory_keeper.py) for any client. On top of the player's rules: writes only to `writable`
    (memory-relative files, and dirs given as "chapters/"); sessions/ is read-only (the player's append-only logs);
    HANDOFF.md only from `handoff_section` (its heading) on: the player's text above it stays byte for byte (the
    runner reads its "Save to load" line). The runner still restores anything out of bounds after the pass."""

    def __init__(self, memory: Callable[[], Path], knowledge: Callable[[], Path], writable: list[str] | tuple = (),
                 handoff_section: str | None = None):
        super().__init__(memory, knowledge)
        self.writable = [w.strip() for w in writable if w.strip()]
        self.handoff_section = handoff_section

    @classmethod
    def from_env(cls, memory: Callable[[], Path], knowledge: Callable[[], Path], env=None) -> "KeeperSandbox":
        """BG3AI_KEEPER_WRITABLE (comma-separated; nothing writable without it) and BG3AI_KEEPER_HANDOFF_SECTION."""
        env = os.environ if env is None else env
        return cls(memory, knowledge, (env.get("BG3AI_KEEPER_WRITABLE") or "").split(","),
                   env.get("BG3AI_KEEPER_HANDOFF_SECTION") or None)

    def may_write(self, rel: str) -> bool:
        return any(rel == w or (w.endswith("/") and rel.startswith(w)) for w in self.writable)

    def _check_write(self, root: str, rel: str, p: Path) -> None:
        super()._check_write(root, rel, p)
        if rel.split("/", 1)[0] == "sessions":
            raise Refused("REFUSED: memory/sessions/ is read-only for the memory keeper (the player's logs)")
        if not self.may_write(rel):
            raise Refused(f"REFUSED: the memory keeper may write only {', '.join(self.writable) or 'nothing'}")

    def check_content(self, rel: str, old: str | None, new: str) -> str | None:
        if rel != "HANDOFF.md" or not self.handoff_section or old is None:
            return None

        def head(text: str) -> str:
            i = text.find("\n" + self.handoff_section)
            return (text if i < 0 else text[:i]).rstrip("\n")
        if head(new) != head(old):
            return (f"REFUSED: HANDOFF.md above '{self.handoff_section}' is the player's: change only that last "
                    f"section (memory_edit inside it, or memory_append it if it is missing)")
        return None


# ---------------------------------------------------------------- reading


def _text(p: Path, shown: str) -> str:
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        raise Refused(f"FAILED: {shown} does not exist (memory_list shows what does)") from None
    except IsADirectoryError:
        raise Refused(f"FAILED: {shown} is a directory: memory_list(\"{shown}\")") from None
    if b"\0" in data[:4096]:
        raise Refused(f"FAILED: {shown} is not a text file")
    return data.decode("utf-8", "replace")


def list_files(sb: Sandbox, path: str = "memory") -> str:
    root, p, rel = sb.resolve(path)
    shown = sb.display(root, rel)
    if not p.is_dir():
        raise Refused(f"FAILED: {shown} is not a directory" + ("" if p.exists() else " (it doesn't exist)"))
    rows = []
    for e in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name)):
        if e.name.startswith(".") or e.is_symlink():
            continue
        if e.is_dir():
            n = sum(1 for x in e.iterdir() if not x.name.startswith("."))
            rows.append(f"  {e.name}/  ({n} entries)")
        else:
            try:
                lines = e.read_bytes().count(b"\n") if e.suffix in TEXT_SUFFIXES else None
            except OSError:
                lines = None
            rows.append(f"  {e.name}  ({lines} lines)" if lines is not None else f"  {e.name}")
    ro = "  (read-only)" if root == "knowledge" else ""
    return f"{shown}{ro}\n" + ("\n".join(rows) if rows else "  (empty)")


def read_file(sb: Sandbox, path: str, start_line: int = 1, max_lines: int = READ_MAX_LINES) -> str:
    root, p, rel = sb.resolve(path)
    shown = sb.display(root, rel)
    lines = _text(p, shown).splitlines()
    start = max(1, int(start_line or 1))
    n = max(1, min(int(max_lines or READ_MAX_LINES), 2000))
    body, used, end = [], 0, start - 1
    for ln in lines[start - 1:start - 1 + n]:
        if used + len(ln) + 1 > READ_MAX_CHARS and body:
            break
        body.append(ln)
        used += len(ln) + 1
        end += 1
    head = f"{shown} (lines {start}-{end} of {len(lines)})" if (start > 1 or end < len(lines)) else \
        f"{shown} ({len(lines)} lines)"
    more = f"\n(more: memory_read(\"{shown}\", start_line={end + 1}))" if end < len(lines) else ""
    return head + "\n" + "\n".join(body) + more


def search(sb: Sandbox, query: str, path: str = "memory", regex: bool = False,
           max_hits: int = SEARCH_MAX_HITS) -> str:
    """Case-insensitive search in the text files under a path (was Grep)."""
    if not (query or "").strip():
        raise Refused("FAILED: give a query (a name, a word)")
    try:
        rx = re.compile(query if regex else re.escape(query.strip()), re.IGNORECASE)
    except re.error as e:
        raise Refused(f"FAILED: bad regex: {e}") from None
    root, p, rel = sb.resolve(path)
    base = Path(os.path.realpath(sb.root_dir(root)))
    files = [p] if p.is_file() else []
    if p.is_dir():
        for d, dirs, names in os.walk(p):  # no symlinks followed, no dot-dirs (.git)
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and not os.path.islink(os.path.join(d, x)))
            files += [Path(d) / n for n in sorted(names) if not n.startswith(".") and n.endswith(TEXT_SUFFIXES)
                      and not os.path.islink(os.path.join(d, n))]
    hits, total = [], 0
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        frel = f"{root}/{f.relative_to(base).as_posix()}"
        for i, ln in enumerate(text.splitlines(), 1):
            if rx.search(ln):
                total += 1
                if len(hits) < max_hits:
                    hits.append(f"{frel}:{i}: {ln.strip()[:300]}")
    if not hits:
        return f"No match for {query!r} in {sb.display(root, rel)}."
    more = f"\n(+{total - len(hits)} more matches: narrow the query or the path)" if total > len(hits) else ""
    return "\n".join(hits) + more


# ---------------------------------------------------------------- writing


def _atomic_write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _character_ok(sb: Sandbox, old: str | None, new: str) -> str | None:
    """None if this CHARACTER.md change is allowed, else why not. Free until the file is committed (the
    character-creation session writes and corrects it); then only lines appended to its development section."""
    mem = sb.memory
    if old is None or not (M.is_git_repo(mem) and M.has_head(mem) and L.tracked_in_head(mem, L.CHARACTER_FILE)):
        return None
    old_core, old_dev = L.character_split(old)
    new_core, new_dev = L.character_split(new)
    old_lines, new_lines = old_dev.splitlines(), new_dev.splitlines()
    if new_core == old_core and new_lines[:len(old_lines)] == old_lines:
        return None
    return (f"REFUSED: {L.CHARACTER_FILE} is who you play: after character creation only lines appended to its "
            f"'{L.CHARACTER_DEV_HEADING}' section may change (memory_append(\"memory/{L.CHARACTER_FILE}\", "
            f"\"- Sxxx: ...\"))")


def lint_note(sb: Sandbox, rel: str) -> str:
    """The lint errors about one memory file, for the write's result (was the PostToolUse memlint hook)."""
    try:
        mine = S.memlint_findings(sb.memory, rel)
    except Exception as e:  # noqa: BLE001 - lint trouble never fails a write
        return f"(lint not run: {type(e).__name__})"
    if not mine:
        return "lint: ok"
    return f"LINT ({len(mine)} error(s) in {rel}; fix them before wrap-up):\n" + "\n".join(
        f"- {k}: {m}" for k, m in mine[:10])


def _save(sb: Sandbox, root: str, p: Path, rel: str, old: str | None, new: str, verb: str) -> str:
    if len(new) > WRITE_MAX_CHARS:
        raise Refused(f"REFUSED: {rel} would be {len(new)} chars (limit {WRITE_MAX_CHARS}); condense it")
    if rel == L.CHARACTER_FILE and (why := _character_ok(sb, old, new)):
        raise Refused(why)
    if why := sb.check_content(rel, old, new):
        raise Refused(why)
    _atomic_write(p, new)
    return f"{verb} {sb.display(root, rel)} ({len(new.splitlines())} lines).\n{lint_note(sb, rel)}"


def write_file(sb: Sandbox, path: str, content: str) -> str:
    root, p, rel = sb.resolve(path, write=True)
    old = _text(p, rel) if p.exists() else None
    if old is not None and rel.startswith("sessions/"):
        raise Refused("REFUSED: session logs are append-only: note(\"[tag] ...\") or memory_append, and "
                      "memory_edit for the Goal line")
    text = content if content.endswith("\n") or not content else content + "\n"
    return _save(sb, root, p, rel, old, text, "Wrote" if old is None else "Rewrote")


def edit_file(sb: Sandbox, path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    root, p, rel = sb.resolve(path, write=True)
    if not p.exists():
        raise Refused(f"FAILED: memory/{rel} does not exist (memory_write creates it)")
    old = _text(p, rel)
    if not old_text:
        raise Refused("FAILED: old_text is empty: give the exact text to replace (memory_append adds at the end)")
    n = old.count(old_text)
    if n == 0:
        raise Refused(f"FAILED: old_text not found in memory/{rel} (it must match exactly, whitespace included; "
                      f"memory_read it first)")
    if n > 1 and not replace_all:
        raise Refused(f"FAILED: old_text occurs {n} times in memory/{rel}: add surrounding text to make it unique, "
                      f"or replace_all=true")
    new = old.replace(old_text, new_text) if replace_all else old.replace(old_text, new_text, 1)
    return _save(sb, root, p, rel, old, new, f"Edited ({n if replace_all else 1} replacement(s))")


def append_file(sb: Sandbox, path: str, text: str) -> str:
    root, p, rel = sb.resolve(path, write=True)
    if not (text or "").strip():
        raise Refused("FAILED: nothing to append")
    old = _text(p, rel) if p.exists() else ""
    sep = "" if not old or old.endswith("\n") else "\n"
    return _save(sb, root, p, rel, old if p.exists() else None, old + sep + text.rstrip("\n") + "\n", "Appended to")


# ---------------------------------------------------------------- session lifecycle


class Lifecycle:
    """Who opens, shows and ends the memory session (session.py) for this server process.

    Runner mode ($BG3AI_SESSION or $BG3AI_FIX_NOTES set): the runner opened the session and put the briefing in
    its kickoff; the server never opens one. Manual mode: the first tool call opens a session when none was opened
    by this process, and its result starts with the briefing. After end_session commits, nothing reopens a
    session except an explicit session_start (a long-lived HTTP server serving the next session)."""

    def __init__(self, memory: Callable[[], Path], auto: bool = False):
        self._memory = memory
        # Opening on the first call is only for the MCP server process (server.main sets it). The dev checks
        # (tools/dev/check_*.py, run by the runner between sessions) import the server and call its tools on the
        # live play/memory: they must never open a session there.
        self.auto = auto
        self.opened: int | None = None   # the session this process opened (manual mode)
        self.ended = False
        self.pending = ""                # the briefing for the next text result

    @property
    def memory(self) -> Path:
        return Path(self._memory())

    @staticmethod
    def runner_mode() -> bool:
        return bool(os.environ.get("BG3AI_SESSION") or os.environ.get("BG3AI_FIX_NOTES"))

    def _is_memory(self) -> bool:
        return (self.memory / "PLAYTHROUGH.md").is_file()   # a real playthrough memory, not a scratch dir

    def ensure(self) -> None:
        """Before every tool call: in manual mode open the session once (the briefing goes to `pending`)."""
        if not self.auto or self.opened is not None or self.ended or self.runner_mode():
            return
        if not self._is_memory():
            return
        self._open()

    def _open(self) -> str:
        ok, text = S.start_session(self.memory)
        if ok:
            self.opened = M.load_state(self.memory).get("current")
            self.ended = False
            self.pending = text
        return text

    def take_pending(self) -> str:
        text, self.pending = self.pending, ""
        return text

    def briefing(self) -> str:
        """The session_start tool: the memory briefing (and the session opened, if nobody opened one)."""
        mem = self.memory
        if not mem.is_dir():
            return S.no_memory_message(mem)
        if self.pending:
            return self.take_pending()
        st = M.load_state(mem)
        if not self.runner_mode() and (self.opened is None or not st.get("open")) and self._is_memory():
            self._open()
            return self.take_pending()
        return S.build_context(mem, "repeat" if st.get("open") else "startup")

    def end(self, in_combat: Callable[[], bool] | None = None) -> str:
        """The end_session tool: the Stop hook's check (session.py stop_decision), as text."""
        mem = self.memory
        if not mem.is_dir():
            return S.no_memory_message(mem)
        st = M.load_state(mem)
        if not st.get("open"):
            return "No open session (it was already committed). You can end your turn."
        sid = M.session_id(st["current"])
        out = S.stop_decision(mem, in_combat=in_combat)
        if out is None:
            return (f"{sid} stays open: wrap-up isn't due and HANDOFF.md isn't written for {sid}. Keep playing, or "
                    f"run the session-wrapup playbook first if the operator asked you to stop.")
        if out.get("decision") == "block":
            return "NOT ENDED. " + out["reason"].replace("then stop again", "then call end_session again")
        if not M.load_state(mem).get("open"):
            self.ended = True
            self.opened = None
            return out.get("systemMessage", f"{sid} closed.") + " Session over: end your turn now."
        return out.get("systemMessage", "") + " You can end your turn."

    def clock(self, now: float | None = None) -> str:
        """Manual sessions (no runner deadline): the elapsed-time wrap-up reminder (was the PostToolUse clock hook)."""
        if self.opened is None or os.environ.get("BG3AI_SESSION_DEADLINE"):
            return ""
        mem = self.memory
        try:
            st = M.load_state(mem)
            msg = S.clock_message(st, now or time.time())
            if msg:
                M.save_state(mem, st)
            return msg or ""
        except OSError:
            return ""
