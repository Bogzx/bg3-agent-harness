"""SessionRunner's agent side (moved from runner/play.py, Phase D.3): the play client's command and
env, one agent process with its stream, health and stop checks (run_claude), the sandbox container's cleanup, and
the per-process latency and triage files.
"""

from __future__ import annotations

import collections
import json
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from bg3ai import metrics as latency

from bg3harness import (
    client_setup,
    clients,
    events,
    pi_client,
    runs,
    sandbox,
    session_summary,
    spend,
    triage,  # noqa: F401  (re-exported: test_play_between)
)
from bg3harness.game import io as game
from bg3harness.game.io import DEBUG_PORT
from bg3harness.game.saves import foreign_saves  # noqa: F401  (re-exported: test_play_saves)
from bg3harness.runner import agent_group
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate


class AgentSide:
    # a paid process's cost goes into runtime/budget.json as it ends (play.py run); a bench run's host turns this
    # off: the matrix settles the run's cost (bg3bench.adapters.session_run), so it is added once
    settles_budget = True
    # the agent loop's clock and its pause between checks (a test's fake clock advances on each pause)
    clock = staticmethod(time.time)
    pause = staticmethod(time.sleep)

    def model_for(self, tag: str = "") -> tuple[str, str | None]:
        """(model, effort) for a session: CLI options < session-config.json; the fix pass has its own."""
        if tag == "-fix":
            return self.opt.fix_model or self.opt.model, self.opt.fix_effort
        state = getattr(self, "state", None)
        o = rstate.session_overrides(state.dir / "session-config.json") if state else {}
        model = self.opt.model if getattr(self.opt, "profile", None) else o.get("model", self.opt.model)
        return model, o.get("effort", self.opt.effort)

    @property
    def client(self) -> clients.Client:
        """The play CLI (--client), built once in __init__ (a bare Runner in a test builds it on first use)."""
        if self._client is None:
            self._client = rstate.make_client(self.opt)
        return self._client

    def play_request(self, prompt: str, session: str | None, resume: bool, tag: str = "", env: dict | None = None,
                     sid: str = "") -> clients.Request:
        """One play process for the client: model and effort (model_for), the fallback model, the autocompact
        window (play sessions only, not the fix pass), the session env and the file stem (sid + tag) that names a
        client's per-session config file."""
        model, effort = self.model_for(tag)
        state = getattr(self, "state", None)
        ac = None
        if tag != "-fix":
            ac = (rstate.session_overrides(state.dir / "session-config.json") if state else {}).get(
                "autocompact", self.opt.autocompact)
        sdir = state.dir if state else renv.RUNTIME / "sessions" / self.opt.playthrough
        return clients.Request(prompt, model, sdir, session=session, resume=resume, effort=effort,
                               fallback_model=self.opt.fallback_model, autocompact=ac, env=dict(env or {}),
                               tag=sid + tag)

    def agent_cmd(self, req: clients.Request) -> list[str]:
        """The play process's command line (the client's; tests swap the binary here)."""
        return self.client.command(req)

    def save_env(self) -> dict:
        """The MCP server's save identity and gates: BG3AI_SAVE_OWNER/PREFIX (save_identity) and BG3AI_SAVE_GATES;
        a --runs run's BG3AI_SAVES_AFTER, a scenario's BG3AI_START_SAVE (older than the run, still its save), and
        BG3AI_SAVES_EXCLUDE: the owner's saves another playthrough or a worker wrote (foreign_saves, newline-separated)."""
        state = getattr(self, "state", None)
        owner = self.opt.save_owner or (state["save_owner"] if state else None)
        after = state["saves_after"] if state else None   # a --runs run: the server's saves start at the run
        listed = state and owner and not after
        return rpolicy.save_env(owner, rcode.list_saves() if listed else None, self.ours() if listed else set(),
                                self.play_windows() if listed else [], after, getattr(self.opt, "start_save", None),
                                getattr(self, "gates", None))

    def run_claude(self, sid: str, prompt: str, minutes: float, claude_session: str | None,
                   resume: bool = False, tag: str = "", needs_game: bool = True) -> rstate.SessionResult:
        """needs_game=False (the fix-notes pass edits memory only): a game crash meanwhile doesn't stop it (S027-fix
        was killed 30 s in by the OOM kill of the game); the next session's setup relaunches the game."""
        start = self.clock()
        deadline, hard_stop = rpolicy.deadlines(start, minutes)
        client = self.client
        session_env = dict(BG3AI_SESSION_DEADLINE=str(int(deadline)),
                           BG3AI_PLAYTHROUGH=self.opt.playthrough,
                           BG3_SESSION_MINUTES=str(minutes),
                           BG3_WRAP_MINUTES=str(rpolicy.wrap_minutes(minutes)),
                           # when the MCP clock line switches to WRAP UP (epoch seconds); play until then
                           BG3AI_SESSION_WRAPUP=str(int(start + rpolicy.wrap_minutes(minutes) * 60)))
        base_env = dict(self.environ)
        base_env.pop("BG3AI_SAVE_GATES", None)
        session_env.update(self.save_env())
        base = self.state.dir / f"{sid}{tag}"
        session_env["BG3AI_METRICS_PATH"] = f"{base}.metrics.jsonl"  # the bridge's per-tool latency records
        # the runner opened this memory session: the MCP server attaches, never opens one
        session_env["BG3AI_SESSION"] = sid
        session_env["BG3AI_FIGHT_HOLD_PATH"] = str(rpolicy.fight_hold_path(self.state.dir, sid))  # per session, not per -rN
        # the memory keeper compacts old logs between sessions: session.py then doesn't ask the player to
        session_env["BG3AI_KEEPER"] = "1" if self.opt.keeper and not self.opt.dry_run else "0"
        if not needs_game:
            session_env["BG3AI_FIX_NOTES"] = "1"   # the stop check lints + commits instead of "keep playing"; no game I/O
        # The runner's own BG3AI_*/BG3_* go to the MCP server too (claude passes its whole env on; codex and gemini
        # forward Request.env by name).
        session_env = {**{k: base_env[k] for k in clients.session_env_names(base_env)}, **session_env}
        # A client that names the conversation itself (codex, gemini) starts without an id: it comes in the
        # stream's init event. One that never got it can't resume: that process starts a new conversation.
        if not client.assigns_session and not resume:
            claude_session = None
        if resume and not claude_session:
            renv.log(f"{sid}{tag}: no {client.name} session id to resume; starting a new conversation")
            resume = False
        req = self.play_request(prompt, claude_session, resume, tag, session_env, sid)
        written = client.write_config(req)
        # The playthrough has its own memory system; Claude Code's auto-memory would add a second one (and a
        # per-machine path in the prompt): CLAUDE_CODE_DISABLE_AUTO_MEMORY is ClaudeClient.env's.
        env = clients.process_env(client, req, base_env)
        cmd = self.agent_cmd(req)
        box, cname = self.box, None
        if box is not None:   # --sandbox container: the client in the container, the bg3 server over http here
            cname, rw = box.container_name(sid + tag), client_setup.container_writable(client, req)
            cmd = box.run_argv(cname, cmd, client.env(req), written, writable=rw)
        model, effort = self.model_for(tag)
        rstate.write_status(playthrough=self.opt.playthrough, session=sid + tag, claude_session=claude_session,
                     model=model, effort=effort or "default",
                     started=start, deadline=deadline, hard_stop=hard_stop, log=str(base) + ".log",
                     metrics=f"{base}.metrics.jsonl", turns=f"{base}.turns.jsonl",
                     progress=f"{base}.progress.jsonl", session_ended=None)
        self.write_status_line(f"playing {sid}{tag} (until {datetime.fromtimestamp(deadline):%H:%M})")
        renv.log(f"{sid}{tag}: {client.name} starting ({model}, effort {effort or 'default'}; deadline "
            f"{datetime.fromtimestamp(deadline):%H:%M}, hard stop {datetime.fromtimestamp(hard_stop):%H:%M}; "
            f"code {self.mod_rev or 'unknown'})")
        self.code_moved(sid + tag)
        if self.opt.dry_run:
            print(json.dumps({"cmd": cmd, "cwd": str(renv.PLAY_DIR), "deadline": deadline}, indent=1))
            return rstate.SessionResult(sid, claude_session, 0, "dry_run", start, start, model=model, effort=effort)
        agent_group.reap_leftover(renv.RUNTIME / agent_group.FILE, renv.log)  # never two agents on one game

        jsonl = open(f"{base}.jsonl", "a")
        evlog = self.events_log = events.EventLog(Path(f"{base}.events.jsonl"))
        evlog.harness("session_start", session=sid + tag, client=client.name, model=model, effort=effort,
                      conversation=claude_session, resume=resume, deadline=deadline, hard_stop=hard_stop)
        human = open(f"{base}.log", "a", buffering=1)
        human.write(f"===== {sid}{tag} {datetime.now():%Y-%m-%d %H:%M:%S} {client.name} session "
                    f"{claude_session or '(new)'}\n")
        human.write("[prompt] " + rpolicy._short(prompt, 4000) + "\n")
        if box is not None:
            try:
                self._sandbox_live = (box.start_server(clients.bg3_server(), rstate.sandbox_server_env(base_env, req),
                                                       renv.PLAY_DIR, Path(f"{base}.mcp-http.log")), cname)
            except sandbox.SandboxError as e:   # RUN-8's path: a setup failure, the agent would have no tools
                detail = f"sandbox: {e}"
                human.write(f"{datetime.now():%H:%M:%S} [runner] {detail}\n")
                human.close()
                jsonl.close()
                renv.log(f"{sid}{tag}: {detail} (a setup failure)")
                self.event("mcp_failed", session=sid + tag, detail=detail[:300])
                self.events_log = evlog.end("mcp_failed", None, start)
                return rstate.SessionResult(sid, claude_session, None, "mcp_failed", start, self.clock(), model=model,
                                     effort=effort, detail=detail)
        try:
            proc = subprocess.Popen(cmd, cwd=renv.PLAY_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True, text=True, bufsize=1)
        except OSError as e:
            self.sandbox_cleanup()
            human.close()
            jsonl.close()
            self.events_log = evlog.end("setup_failed", None, start, detail=str(e)[:300])
            raise
        self.agent_proc = proc  # an operator's hard exit kills it (kill_agent): it runs in its own session
        agent_group.record(renv.RUNTIME / agent_group.FILE, proc)

        timing = latency.StreamTiming(Path(f"{base}.turns.jsonl"))
        last_output = [self.clock()]  # the agent-hang watchdog: any stream line counts as alive
        meter = None   # --max-cost/--max-tokens: the run's cost and tokens as this stream goes (runs.UsageMeter)
        since = self.run_started if self.run_started is not None else getattr(self, "run_t0", None)
        if self.has_usage_caps() and since is not None:
            done = runs.claude_totals(self.state.dir, since)
            meter = runs.UsageMeter(done["cost_usd"], runs.token_total(done["tokens"]),
                                    done["running"] if client.name in clients.RUNNING_COST else None)
        seen_session = [claude_session]   # the conversation id: assigned up front, or the stream's init event's
        api = {"texts": [], "rejected": False, "resets_at": None, "mcp": None,   # RUN-7/RUN-8, from the stream
               "overflow": None, "raw": collections.deque(maxlen=40),   # pi: its overflow result, stderr tail
               "tool_calls": 0}
        cost = spend.ProcessCost()

        def pump():
            for line in proc.stdout:
                last_output[0] = self.clock()
                jsonl.write(line)
                jsonl.flush()
                stamp = datetime.now().strftime("%H:%M:%S")
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    if line.strip():
                        human.write(f"{stamp} [raw] {line.rstrip()[:500]}\n")
                        api["raw"].append(line.rstrip())
                    continue
                try:
                    events = client.parse(line)
                except Exception as e:
                    human.write(f"{stamp} [raw] {type(e).__name__}: {line.rstrip()[:400]}\n")
                    continue
                for e in events:
                    evlog.write(e)
                    for text in clients.format_event(e):
                        human.write(f"{stamp} {text}\n")
                    if e.kind == "init" and e.session_id and not seen_session[0]:
                        seen_session[0] = e.session_id
                        rstate.write_status(claude_session=e.session_id)
                    if e.kind == "init" and api["mcp"] is None:
                        api["mcp"] = (renv.mcp_problem(e.mcp, client.name == "claude") or client_setup.tool_count_problem(
                            e.tools, self.opt.tools_expected, self.environ) or "")
                    if e.kind == "tool_call":
                        api["tool_calls"] += 1
                    if e.kind == "result" and e.subtype in ("error_context_overflow", "error_overflow_loop"):
                        api["overflow"] = e.subtype
                    cost.on_event(e)
                    if e.api_error and e.kind in ("text", "error", "result") and e.text:
                        api["texts"].append(e.text[:300])
                    if e.kind == "rate_limit":
                        api["resets_at"] = e.resets_at or api["resets_at"]
                        api["rejected"] = api["rejected"] or e.api_error
                try:
                    # bg3ai.metrics reads claude's stream-json; other clients' events are fed in that shape
                    for tev in ([ev] if client.name == "claude" else [rpolicy.timing_event(e) for e in events]):
                        if tev is not None:
                            timing.on_event(tev)
                    if meter is not None:
                        for e in events:
                            meter.on_event(e)
                except Exception:
                    pass

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        poll_stop = threading.Event()
        poller = threading.Thread(target=self.progress_loop, args=(sid + tag, base, start, poll_stop, human),
                                  name="progress", daemon=True)
        poller.start()

        health = rcode.HealthTracker()
        next_health = self.clock() + renv.HEALTH_INTERVAL
        mod_gone_since = None   # RUN-8: the game answers but Mods.BG3AI is gone (a Lua reset, a mod load error)
        reason = "exited"
        detail, fault_at = "", None
        signals_sent = []
        hold, held_until, planned_stop = rpolicy.fight_hold_path(self.state.dir, sid), deadline, hard_stop
        while proc.poll() is None:
            self.pause(2)
            now = self.clock()
            if reason == "exited" and not signals_sent and needs_game and rpolicy.held_deadline(hold, deadline) != held_until:
                # the cap while the fight lasts, then the fight's end + the wrap-up; never before the planned stop
                held_until = rpolicy.held_deadline(hold, deadline)
                hard_stop = max(planned_stop, held_until + renv.HARD_STOP_GRACE)
                human.write(f"{datetime.now():%H:%M:%S} [runner] fight hold (a fight at WRAP UP): session end "
                            f"{datetime.fromtimestamp(held_until):%H:%M:%S}, hard stop "
                            f"{datetime.fromtimestamp(hard_stop):%H:%M}\n")
                rstate.write_status(deadline=held_until, hard_stop=hard_stop)
            if self.abort and reason == "exited":
                reason = "aborted"
                hard_stop = min(hard_stop, now)
            if reason == "exited" and self.stop_ends_session and (self.stop_requested or (renv.RUNTIME / "STOP").exists()):
                reason = "stopped"   # a bench run is one session: an operator stop ends it now, and it is recorded
                human.write(f"{datetime.now():%H:%M:%S} [runner] operator stop (a signal or runtime/STOP); stopping "
                            f"{client.name}\n")
                renv.log(f"{sid}{tag}: operator stop; stopping {client.name}")
                self.event("operator_stop", session=sid + tag)
                hard_stop = min(hard_stop, now)
            if needs_game and now >= next_health and reason == "exited":
                alive = self.game_alive()
                ok = alive and game.game_state(renv.HEALTH_EVAL_TIMEOUT) is not None
                health.record(ok)
                next_health = self.clock() + (renv.HEALTH_INTERVAL if ok else renv.HEALTH_RETRY_INTERVAL)
                rstate.write_status(heartbeat=time.time())
                if ok and game.mod_loaded(renv.HEALTH_EVAL_TIMEOUT) is False:
                    mod_gone_since = mod_gone_since or now
                else:
                    mod_gone_since = None
                if not alive or health.hung:
                    reason = "crash" if not alive else "hang"
                    detail, fault_at = self.fault_detail(health), now
                elif mod_gone_since is not None and now - mod_gone_since >= renv.MOD_GONE_S:
                    reason, fault_at = "hang", now   # GAME_FAULTS: recover (relaunch + load) and resume claude
                    detail = (f"the game answers but the BG3AI mod is gone (Mods.BG3AI) for {now - mod_gone_since:.0f} s: "
                              "a Lua reset or a mod load error; the agent's tools can't work")
            if needs_game and reason == "exited" and self.until_hit:
                reason = "milestone"
                human.write(f"{datetime.now():%H:%M:%S} [runner] milestone {self.until_hit['id']} reached; "
                            f"stopping claude ({'the game is finished' if self.game_ended() else '--until'})\n")
                hard_stop = min(hard_stop, now)
            if reason == "exited" and meter is not None and self.check_usage_caps(meter.cost, meter.tokens):
                reason = "cap"
                human.write(f"{datetime.now():%H:%M:%S} [runner] {self.cap_hit['why']}; stopping claude\n")
                hard_stop = min(hard_stop, now)
            if reason == "exited" and api["overflow"] == "error_overflow_loop":   # pi would go on: stop it
                reason, detail = "context_overflow", "pi's overflow recovery looped (pi #11161)"
                human.write(f"{datetime.now():%H:%M:%S} [runner] {detail}; stopping {client.name}\n")
                hard_stop = min(hard_stop, now)
            if reason == "exited" and api["mcp"]:
                # RUN-8: a toolless agent would be pushed to its hard stop by the stop check: end it at once
                reason, detail = "mcp_failed", api["mcp"]
                human.write(f"{datetime.now():%H:%M:%S} [runner] {detail}; stopping {client.name}\n")
                renv.log(f"{sid}{tag}: {detail}; stopping {client.name} (a setup failure)")
                self.event("mcp_failed", session=sid + tag, detail=detail)
                hard_stop = min(hard_stop, now)
            silent = now - last_output[0]
            if reason == "exited" and silent >= renv.AGENT_SILENT_MINUTES * 60:
                reason, fault_at = "agent_hang", now
                detail = f"no claude output for {silent / 60:.1f} min (limit {renv.AGENT_SILENT_MINUTES:g})"
            if fault_at == now:
                human.write(f"{datetime.now():%H:%M:%S} [runner] FAULT {reason}: {detail}; stopping claude\n")
                renv.log(f"{sid}{tag}: FAULT {reason}: {detail}; stopping claude")
                self.event("fault", fault=reason, session=sid + tag, detail=detail)
                hard_stop = min(hard_stop, now)
            if now >= hard_stop and "int" not in signals_sent:
                if reason == "exited":
                    reason = "hard_stop"
                human.write(f"{datetime.now():%H:%M:%S} [runner] {reason}: SIGINT\n")
                self._signal(proc, signal.SIGINT)
                signals_sent.append("int")
                term_at = now + renv.SIGINT_GRACE
            if "int" in signals_sent and "term" not in signals_sent and now >= term_at:
                self._signal(proc, signal.SIGTERM)
                signals_sent.append("term")
                kill_at = now + renv.SIGTERM_GRACE
            if "term" in signals_sent and "kill" not in signals_sent and now >= kill_at:
                self._signal(proc, signal.SIGKILL)
                signals_sent.append("kill")
        self.agent_proc = None
        if not agent_group.end_group(renv.RUNTIME / agent_group.FILE, proc):  # its MCP server too: nothing of it plays on
            renv.log(f"{sid}{tag}: a process of {client.name}'s group survived SIGKILL (pgid {proc.pid})")
        reader.join(timeout=10)
        self.sandbox_cleanup()
        reason = renv.exit_reason(reason, proc.returncode)
        if api["mcp"] is None and client.name == "pi":   # pi ended before any event: bg3.ts failed to load?
            api["mcp"] = pi_client.exit_problem(proc.returncode, "\n".join(api["raw"]))
        if reason in ("exited", "killed") and api["mcp"]:
            reason, detail = "mcp_failed", api["mcp"]   # it exited before the 2 s loop saw the init event
        if reason == "exited" and api["overflow"]:   # never a backoff-retry or a resume of a full conversation
            reason, detail = "context_overflow", f"the context is full ({api['overflow']})"
        kind = renv.classify_api_failure(api["texts"], api["rejected"]) if reason == "exited" else None
        if kind == "limit":   # RUN-7: wait it out in the run loop (never a quick failure)
            reason, detail = "api_limit", (api["texts"] or ["rate limit rejected"])[-1]
            renv.log(f"{sid}{tag}: {client.name} hit an API limit: {detail[:200]}")
        elif kind == "auth":
            detail = api["texts"][-1]
            renv.log(f"{sid}{tag}: {client.name} API/config error (counts toward the quick-failure stop): {detail[:200]}")
        if reason == "killed":
            detail = f"claude ended by a signal from outside the runner (exit {proc.returncode})"
            renv.log(f"{sid}{tag}: {detail}")
        poll_stop.set()
        poller.join(timeout=20)
        end = self.clock()
        rstate.write_status(session_ended=end)
        human.write(f"===== {sid}{tag} ended: {reason}, exit {proc.returncode}, "
                    f"{(end - start) / 60:.1f} min\n")
        human.close()
        jsonl.close()
        self.events_log = evlog.end(reason, proc.returncode, start, detail=detail, fault_at=fault_at,
                                    conversation=seen_session[0])
        if not self.opt.dry_run:
            self.write_choices(sid + tag, base)
        renv.log(f"{sid}{tag}: claude finished ({reason}, exit {proc.returncode}, {(end - start) / 60:.1f} min)")
        if spend.is_paid(client.name):
            self.settle_cost(sid + tag, cost.usd)
        self.code_moved(sid + tag)
        self.write_latency(f"{sid}{tag}", base, start, end)
        self.write_progress(f"{sid}{tag}", base, start, end)
        self.write_triage(f"{sid}{tag}", base)
        return rstate.SessionResult(sid, seen_session[0], proc.returncode, reason, start, end, model=model, effort=effort,
                             detail=detail, fault_at=fault_at, resets_at=api["resets_at"],
                             tool_calls=api["tool_calls"])

    def kill_agent(self) -> None:
        """An operator's hard exit: SIGKILL the play process a session left running and every process of its group
        (its MCP server), and wait until they are gone (it has its own session, so the terminal's Ctrl-C never
        reaches it)."""
        proc, self.agent_proc = self.agent_proc, None
        if proc is not None and not agent_group.end_group(renv.RUNTIME / agent_group.FILE, proc):
            renv.log(f"a process of the play agent's group survived SIGKILL (pgid {proc.pid})")
        self.sandbox_cleanup()

    def settle_cost(self, stem: str, usd: float) -> None:
        """One paid process's cost into runtime/budget.json, unless the run's host settles it (settles_budget)."""
        if not self.settles_budget:
            renv.log(f"{stem}: cost ${usd:.4f} (the bench settles the budget)")
            return
        try:
            b = spend.add_spend(renv.RUNTIME / "budget.json", usd, playthrough=self.opt.playthrough, session=stem)
            renv.log(f"{stem}: cost ${usd:.4f}" + (f"; budget ${b['spent']:.2f} of ${b['cap']:g}" if b else ""))
        except (OSError, spend.BudgetError) as e:
            renv.log(f"{stem}: budget update failed ({e}); cost ${usd:.4f}")

    def sandbox_cleanup(self) -> None:
        """--sandbox container: the play process's container (a SIGKILL to the docker CLI doesn't reach it) and
        its http server. Also at the runner's end, for a session an exception left."""
        live, self._sandbox_live = getattr(self, "_sandbox_live", None), None
        if live:
            server, cname = live
            if cname:
                sandbox.Sandbox.remove(cname)
            server.stop()

    def fault_detail(self, health: rcode.HealthTracker) -> str:
        if self.game_proc is not None and self.game_proc.poll() is not None:
            return f"game process exited (status {self.game_proc.returncode})"
        if not game.port_open():
            return f"debug port {DEBUG_PORT} stopped answering" + (" (game process still alive)" if self.game_proc else "")
        return f"port {DEBUG_PORT} open but {health.limit} liveness evals in a row failed or timed out"

    def write_latency(self, session: str, base: Path, start: float, end: float) -> dict | None:
        """Sxxx.latency.json + a line in latency_history.jsonl (session_summary, the app's too)."""
        return session_summary.write_latency(session, base, start, end, self.state.dir, renv.log)

    def write_triage(self, session: str, base: Path) -> Path | None:
        """Sxxx.triage.md (session_summary); remembered for the check_all after the session."""
        path = session_summary.write_triage(session, base, renv.log)
        if path is not None:
            self.last_triage = (session, path)
        return path
