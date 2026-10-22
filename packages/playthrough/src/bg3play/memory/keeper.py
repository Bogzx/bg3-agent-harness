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

MEM-1 (audit 2026-10-13): lint can't tell a hallucinated fact from a real one. A bullet the keeper adds to a fact
file that names something (a capitalised word past the bullet's first) that appears nowhere in the memory before the
pass, the read-only knowledge dir or the keeper's own prompt is dropped (ungrounded_lines): it can't have come from
the player's notes. The pre-pass snapshot is also kept on disk (keeper_pending.json next to the keeper's log) until
the pass is committed or rolled back; a runner killed mid-pass leaves it, and recover() at the next start kills a
leftover keeper process and puts the memory back as the player left it, so the next session never builds on
unvalidated keeper edits.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bg3play.memory import lint, memlib
from bg3play.memory.keeper_files import (
    FIX_MIN_SECONDS,
    HANDOFF_SECTION,
    KEEPER_EFFORT,
    KEEPER_MINUTES,
    KEEPER_MODEL,
    KNOWLEDGE,
    SESSION_PY,
    WRITABLE_DIRS,
    WRITABLE_FILES,
    changes,
    clear_pending,
    clients,
    corpus,
    drop_lines,
    keep_character_core,
    keep_handoff_head,
    note_pgid,
    restore,
    save_pending,
    snapshot,
    ungrounded_lines,
    writable,
)
from bg3play.memory.keeper_files import MAX_LOGS as MAX_LOGS  # these: the runner's and the tests' names on keeper
from bg3play.memory.keeper_files import PENDING as PENDING
from bg3play.memory.keeper_files import REPO as REPO
from bg3play.memory.keeper_files import recover as recover
from bg3play.memory.keeper_files import ungrounded_names as ungrounded_names
from bg3play.memory.keeper_prompt import DONE_MAX_LINES as DONE_MAX_LINES
from bg3play.memory.keeper_prompt import build_prompt
from bg3play.memory.keeper_prompt import done_records as done_records
from bg3play.memory.keeper_prompt import game_done_records as game_done_records
from bg3play.memory.keeper_prompt import logs_to_read as logs_to_read
from bg3play.memory.keeper_prompt import old_debrief_logs as old_debrief_logs
from bg3play.memory.keeper_prompt import refused_plans as refused_plans
from bg3play.memory.keeper_spend import KeeperSpend

# ---------------------------------------------------------------- command


def keeper_settings(mem: Path, knowledge: Path = KNOWLEDGE) -> dict[str, Any]:
    m, k = mem.resolve(), knowledge.resolve()
    deny = [f"{tool}(/{p})" for tool in ("Edit", "Write") for p in (f"{m}/sessions/**", f"{k}/**")]
    # The memory repo's git dir and git control files: never read or written (FAIR-1; git runs on them in hooks).
    deny += [
        f"{tool}(/{p})"
        for tool in ("Read", "Edit", "Write")
        for p in (f"{m}/.git/**", f"{m}/**/.git/**", f"{m}/**/.git*", f"{k}/**/.git*")
    ]
    return {
        "permissions": {
            "defaultMode": "acceptEdits",
            "allow": ["Read", "Glob", "Grep", f"Edit(/{m}/**)", f"Write(/{m}/**)"],
            "deny": deny,
        },
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {"type": "command", "command": f"{sys.executable} {SESSION_PY} memlint --memory-dir {m}"}
                    ],
                }
            ]
        },
    }


# ---------------------------------------------------------------- any client: the bg3 server in keeper mode


def keeper_server(mem: Path, knowledge: Path = KNOWLEDGE, task: Path | None = None) -> dict[str, Any]:
    """The bg3 stdio server in keeper mode (bridge server.keeper_server): only the memory tools, the keeper's
    write rules enforced server-side (memtools.KeeperSandbox from WRITABLE_FILES/WRITABLE_DIRS + HANDOFF.md +
    CHARACTER.md, sessions/ and knowledge/ read-only, HANDOFF.md only below HANDOFF_SECTION) and `task` as its
    instructions. play/.mcp.json's command, with its --project made absolute (the client runs in the memory dir)."""
    s = clients.bg3_server()
    args = list(s.get("args") or [])
    for i, a in enumerate(args[:-1]):
        if a == "--project":
            args[i + 1] = str((clients.PLAY_DIR / args[i + 1]).resolve())
    env: dict[str, Any] = {
        **(s.get("env") or {}),
        "BG3AI_KEEPER_MODE": "1",
        "BG3AI_MEMORY_DIR": str(mem.resolve()),
        "BG3_KNOWLEDGE_DIR": str(knowledge.resolve()),
        "BG3AI_KEEPER_WRITABLE": ",".join(
            [*WRITABLE_FILES, "HANDOFF.md", lint.CHARACTER_FILE, *(d + "/" for d in WRITABLE_DIRS)]
        ),
        "BG3AI_KEEPER_HANDOFF_SECTION": HANDOFF_SECTION,
        "BG3AI_AUTO_SESSION": "0",
        "BG3AI_MOVIE_SKIP": "0",
        "BG3AI_METRICS_SAMPLE_S": "0",
    }
    if task is not None:
        env["BG3AI_KEEPER_TASK"] = str(Path(task).resolve())
    return {"type": "stdio", "command": s["command"], "args": args, "env": env, "timeout": s.get("timeout") or 110_000}


def keeper_client(
    name: str,
    mem: Path,
    log_base: Path,
    knowledge: Path = KNOWLEDGE,
    binary: str | None = None,
    profile: Any = None,
) -> Any:
    """A clients.py Client whose only tools are the keeper-mode bg3 server's (its config files next to log_base).
    pi plays from its client profile (`profile`, a parsed profiles.Profile) with the task as its system text."""
    task = Path(f"{log_base}.task.md")
    server = keeper_server(mem, knowledge, task)
    kw: dict[str, Any] = {"binary": binary} if binary else {}
    if name == "claude":
        cfg = Path(f"{log_base}.mcp.json")
        cfg.write_text(json.dumps({"mcpServers": {"bg3": server}}, indent=1) + "\n")
        return clients.ClaudeClient(mcp_config=cfg, settings={"permissions": {"allow": ["mcp__bg3__*"]}}, **kw)
    if name == "pi":
        if profile is None:
            raise ValueError("a pi keeper needs its client profile")
        kw |= {"profile": profile, "system_file": task}
    return clients.make(name, server=server, cwd=mem, **kw)


def _session_from_log(client: Any, log_path: Path) -> str | None:
    """The conversation id a client that doesn't take one up front (codex, gemini) printed in its init event."""
    sid: str | None = None
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for ln in lines:
        for ev in client.parse(ln):
            if ev.kind == "init" and ev.session_id:
                sid = ev.session_id
    return sid


def client_cmd(
    client: Any,
    prompt: str,
    mem: Path,
    model: str,
    effort: str | None,
    session: str | None,
    resume: bool,
    log_base: Path,
) -> tuple[list[str], dict[str, Any]]:
    """(argv, extra env) of one keeper call through `client` (keeper_client)."""
    req = clients.Request(
        prompt=prompt,
        model=model,
        session_dir=Path(log_base).parent,
        session=session,
        resume=resume,
        effort=effort,
        tag=Path(log_base).name,
    )
    client.write_config(req)
    return client.command(req), client.env(req)


def keeper_cmd(
    prompt: str,
    mem: Path,
    model: str,
    effort: str | None,
    session_uuid: str,
    resume: bool,
    claude: str = "claude",
    knowledge: Path = KNOWLEDGE,
) -> list[str]:
    # --restricted: no Bash/web tools, user/project/local settings ignored (none of play/'s settings),
    # file tools confined to cwd (the memory dir) + --add-dir. The keeper still uses Claude Code's file tools.
    cmd = [
        claude,
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--restricted",
        "--tools",
        "Read,Edit,Write,Glob,Grep",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--add-dir",
        str(knowledge.resolve()),
        "--settings",
        json.dumps(keeper_settings(mem, knowledge)),
    ]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--resume", session_uuid] if resume else ["--session-id", session_uuid]
    return cmd


# ---------------------------------------------------------------- run


@dataclass
class KeeperResult:
    sid: str
    status: str = "error"  # ok | no_changes | lint_failed | error | refused (a paid client without cap or budget)
    seconds: float = 0.0
    files: list[str] = field(default_factory=list[str])
    reverted: list[str] = field(default_factory=list[str])  # touched outside the writable set
    commit: str | None = None
    timed_out: bool = False
    exit_code: int | None = None
    lint_errors: list[str] = field(default_factory=list[str])
    ungrounded: list[str] = field(default_factory=list[str])  # MEM-1: added lines dropped (names not in the memory)
    cost_usd: float | None = None  # a paid client's pass (keeper_spend.py)
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
        if self.ungrounded:
            s += f"; dropped {len(self.ungrounded)} ungrounded line(s): {self.ungrounded[0][:160]}"
        if self.detail:
            s += f"; {self.detail}"
        return s


def _errors(mem: Path, session: int) -> list[str]:
    return [f"{k}: {m}" for k, m in lint.lint(mem, "any", session).errors]


def _run_claude(
    cmd: list[str],
    cwd: Path,
    log_path: Path,
    timeout: float,
    extra_env: dict[str, Any] | None = None,
    pending: Path | None = None,
    watch: Callable[[str], bool] | None = None,
) -> tuple[int | None, bool]:
    """Runs one claude (or other client) call with its stream into log_path; kills its process group at the
    timeout (the keeper-mode MCP server is in that group too), or when `watch` (each stream line; the cost cap,
    keeper_spend.py) returns True."""
    env = {**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", **(extra_env or {})}
    for k in ("BG3AI_SESSION_DEADLINE", "BG3AI_SESSION_WRAPUP", "BG3AI_METRICS_PATH"):
        env.pop(k, None)
    with open(log_path, "a") as out:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=out if watch is None else subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
        note_pgid(pending, proc.pid)  # its own process group: recover() kills it if the runner dies meanwhile
        reader = None
        if watch is not None:
            reader = threading.Thread(target=_pump, args=(proc, out, watch), daemon=True, name="keeper-stream")
            reader.start()
        try:
            return proc.wait(timeout=max(1.0, timeout)), False
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            return proc.returncode, True
        finally:
            if reader is not None:
                reader.join(10)


def _kill_group(proc: subprocess.Popen[str]) -> None:
    for sig, wait in ((signal.SIGINT, 10), (signal.SIGKILL, 10)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _pump(proc: subprocess.Popen[str], out: Any, watch: Callable[[str], bool]) -> None:
    """The stream into the log, each line through `watch`; the process group is killed when it trips."""
    assert proc.stdout is not None
    for line in proc.stdout:
        out.write(line)
        out.flush()
        if watch(line):
            _kill_group(proc)
            return


def keeper_server_env(mem: Path, knowledge: Path, task: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """(server spec, env) of the keeper-mode bg3 server run over http on the host: the env a stdio server started
    by the client would get (_run_claude's, which drops the play session's deadline and metrics) + its own."""
    server = keeper_server(mem, knowledge, task)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("BG3AI_SESSION_DEADLINE", "BG3AI_SESSION_WRAPUP", "BG3AI_METRICS_PATH")
    }
    return server, {**env, **server["env"]}


def _run_sandboxed(
    box: Any,
    cli: Any,
    text: str,
    mem: Path,
    model: str,
    effort: str | None,
    sess: str | None,
    resume: bool,
    log_base: Path,
    knowledge: Path,
    log_path: Path,
    timeout: float,
    pending: Path | None,
    watch: Callable[[str], bool] | None = None,
) -> tuple[int | None, bool]:
    """One keeper call in the container sandbox: the keeper-mode server over http on the host for this call, the
    client in a `keeper` container; both gone afterwards, whatever happened."""
    server, env = keeper_server_env(mem, knowledge, Path(f"{log_base}.task.md"))
    req = clients.Request(
        prompt=text,
        model=model,
        session_dir=Path(log_base).parent,
        session=sess,
        resume=resume,
        effort=effort,
        tag=Path(log_base).name,
    )
    files = cli.write_config(req)
    name = box.container_name(Path(log_base).name)
    cmd = box.run_argv(name, cli.command(req), cli.env(req), files, role="keeper")
    t0 = time.time()
    srv = box.start_server(server, env, mem, Path(f"{log_base}.mcp-http.log"))  # SandboxError: run_keeper's error
    try:
        return _run_claude(cmd, mem, log_path, timeout - (time.time() - t0), pending=pending, watch=watch)
    finally:
        box.remove(name)
        srv.stop()


def commit(mem: Path, rels: list[str], message: str) -> str | None:
    """Commits only `rels` (the keeper's files), never the open session's other changes."""
    if not rels or not memlib.is_git_repo(mem):
        return None
    # A path that is gone from disk and from the index (someone else deleted and committed it meanwhile, S026)
    # makes `git add -- path` fail with "pathspec did not match": there's nothing of it left to commit.
    tracked = set(memlib.git(mem, "ls-files", "--", *rels, check=False).stdout.splitlines())
    rels = [r for r in rels if (mem / r).exists() or r in tracked]
    if not rels:
        return None
    memlib.git(mem, "add", "-A", "--", *rels)
    if memlib.git(mem, "diff", "--cached", "--quiet", "--", *rels, check=False).returncode == 0:
        return None
    memlib.git(mem, *memlib.git_identity_args(mem), "commit", "-q", "--no-verify", "-m", message, "--", *rels)
    return memlib.git(mem, "rev-parse", "--short", "HEAD").stdout.strip()


def run_keeper(
    mem: Path,
    sid_n: int,
    log_base: Path,
    model: str = KEEPER_MODEL,
    effort: str | None = KEEPER_EFFORT,
    minutes: float = KEEPER_MINUTES,
    claude: str = "claude",
    knowledge: Path = KNOWLEDGE,
    refused: list[dict[str, Any]] | None = None,
    client: str = "claude",
    mcp: bool | None = None,
    binary: str | None = None,
    sandbox: Any = None,
    profile: Any = None,
    max_cost: float | None = None,
    budget: Path | None = None,
) -> KeeperResult:
    """One keeper pass for the session that just ended (number sid_n). Never raises.

    client: the clients.py client that keeps the memory (the play client, so a benchmark compares one model end to
    end). mcp: through the bg3 server in keeper mode (keeper_server); None = True for every client but claude,
    whose default stays today's pass with Claude Code's file tools (keeper_cmd). binary: the client's executable
    (default: `claude` for claude, else the client's own). sandbox: the runner's bg3harness.sandbox.Sandbox (run
    --sandbox container): the client runs in the container against the keeper-mode server over http (always MCP).
    profile: pi's client profile (profiles.Profile; its model is the keeper's). max_cost, budget: a paid client's
    cap for this pass and runtime/budget.json (keeper_spend.py): refused without them, killed at the cap."""
    sid = memlib.session_id(sid_n)
    via_mcp = client != "claude" if mcp is None else mcp
    if sandbox is not None:
        via_mcp = True  # no file tools in the container: the keeper-mode server is the only way to the memory
    who = client
    if profile is not None:
        model = profile.model  # pi runs the profile's model only
    res = KeeperResult(sid)
    spend = KeeperSpend(client, max_cost, budget)
    if spend.refusal:
        res.status, res.detail = "refused", spend.refusal
        return res
    snap = pending = None
    t0 = time.time()
    end = t0 + minutes * 60
    try:
        before_errors = set(_errors(mem, sid_n + 1))
        report = lint.lint(mem, "any", sid_n + 1).render(mem)
        snap = snapshot(mem)
        pending = save_pending(Path(log_base).parent, mem, sid, snap)
        log_path = Path(f"{log_base}.jsonl")
        sess = str(uuid.uuid4())
        prompt = build_prompt(
            mem,
            sid,
            sid_n,
            report,
            minutes,
            refused,
            tools="mcp" if via_mcp else "files",
            sessions_dir=Path(log_base).parent,
        )
        grounds = corpus(snap, knowledge, prompt)
        cli: Any = None
        if via_mcp:
            Path(f"{log_base}.task.md").write_text(prompt + "\n", encoding="utf-8")  # the server's instructions
            if sandbox is not None:
                cli = clients.make_http(client, sandbox.url, **({"binary": binary} if binary else {}))
            else:
                cli = keeper_client(
                    client, mem, log_base, knowledge, binary or (claude if client == "claude" else None), profile
                )
            sess = sess if cli.assigns_session else None

        def call(text: str, resume: bool, left: float) -> tuple[int | None, bool]:
            if cli is None:
                assert sess is not None  # only an MCP client's session can be None
                return _run_claude(
                    keeper_cmd(text, mem, model, effort, sess, resume, claude, knowledge),
                    mem,
                    log_path,
                    left,
                    pending=pending,
                )
            if sandbox is not None:
                return _run_sandboxed(
                    sandbox,
                    cli,
                    text,
                    mem,
                    model,
                    effort,
                    sess,
                    resume,
                    log_base,
                    knowledge,
                    log_path,
                    left,
                    pending,
                    spend.watcher(cli),
                )
            cmd, env = client_cmd(cli, text, mem, model, effort, sess, resume, log_base)
            return _run_claude(cmd, mem, log_path, left, env, pending=pending, watch=spend.watcher(cli))

        rc, res.timed_out = call(prompt, False, end - time.time())
        if cli is not None and not cli.assigns_session:
            sess = _session_from_log(cli, log_path)
        res.exit_code = rc
        new: list[str] = []
        for attempt in range(2):
            changed = changes(mem, snap)

            res.reverted = sorted(set(res.reverted) | {r for r in changed if not writable(r)})
            restore(mem, snap, [r for r in changed if not writable(r)])
            if keep_handoff_head(mem, snap):
                res.reverted = sorted(set(res.reverted) | {"HANDOFF.md (player part)"})
            if keep_character_core(mem, snap):
                res.reverted = sorted(set(res.reverted) | {f"{lint.CHARACTER_FILE} (core)"})
            bad = ungrounded_lines(mem, snap, [r for r in changes(mem, snap) if writable(r)], grounds)
            if bad:
                drop_lines(mem, bad)
                res.ungrounded += [f"{rel}: {ln.strip()}" for rel, lns in bad.items() for ln in lns]
            new = [e for e in _errors(mem, sid_n + 1) if e not in before_errors]
            left = end - time.time()
            if not new or attempt == 1 or left < FIX_MIN_SECONDS or res.timed_out or spend.cap_hit:
                break
            fix = (
                "Memory lint now reports these NEW errors; fix them with minimal edits, then stop:\n"
                + "\n".join(new)[-3000:]
            )
            if cli is not None and not (cli.supports_resume and sess):
                break  # no conversation to resume (the client printed no session id)
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
        clear_pending(pending)  # committed, rolled back or nothing to do: no undo left for recover()
    except Exception as e:
        res.status, res.detail = "error", f"{type(e).__name__}: {e}"[:300]
        if snap is not None and not res.commit:  # unvalidated edits never reach the next session
            try:
                restore(mem, snap, changes(mem, snap))
                clear_pending(pending)
                res.detail += "; keeper edits rolled back"
            except Exception as e2:
                res.detail += f"; rollback failed ({type(e2).__name__}), recover() at the next start"
    if spend.paid:
        res.cost_usd = round(spend.cost, 6)
        notes = [f"cost cap ${spend.cap:g} hit" if spend.cap_hit else "", spend.record(mem.parent.name, sid)]
        res.detail = "; ".join(x for x in (res.detail, *notes) if x)
    res.seconds = round(time.time() - t0, 1)
    return res
