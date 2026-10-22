"""Between a playthrough's sessions (moved from runner/legacy_playthrough.py, Phase D.2): the plan-line check
(tools/dev/check_plans.py), the memory-keeper pass (bg3play.memory.keeper) in a thread joined before the next player
starts, its recovery after a runner death, and the session review (tools/logs/review_session.py)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from bg3harness.game import io as game
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate

from ..memory import keeper

REVIEW_PY = renv.REPO / "tools" / "logs" / "review_session.py"


def merge_refused(refused: list[dict], plan_bad: list[dict]) -> list[dict]:
    """The plan lines the game refused this session, plus the check's invalid ones it doesn't already name."""
    seen = {(x["name"].lower(), x["level"]) for x in refused}
    return refused + [b for b in plan_bad if (b["name"].lower(), b["level"]) not in seen]


class KeeperSide:
    def check_plan_lines(self, when: str, dry: bool = False) -> list[dict]:
        """tools/dev/check_plans.py (reads only, ~3 s): the stored plan lines for each member's next 2 levels vs the
        game's Progression offers. Logs one line; returns the invalid lines (keeper refused-line format). Needs the
        game Running with the party loaded and no bridge in use (between sessions). Never raises."""
        if (self.opt.dry_run and not dry) or not game.port_open():
            return []
        p = None
        try:
            if game.game_state() != "Running":
                return []
            json_path = renv.RUNTIME / "check_plans.json"
            json_path.unlink(missing_ok=True)
            env = dict(os.environ, BG3AI_PLAYTHROUGH=self.opt.playthrough)
            p = subprocess.run(
                rcode.check_plans_cmd(json_path, self.mem),
                capture_output=True,
                text=True,
                cwd=renv.REPO,
                env=env,
                timeout=renv.CHECK_PLANS_TIMEOUT,
            )
            rows = json.loads(json_path.read_text())
        except Exception as e:
            tail = (getattr(p, "stderr", "") or "")[-300:]
            renv.log(f"plan check ({when}) skipped: {type(e).__name__}: {e} {tail}".rstrip())
            return []
        text, bad = rcode.plan_check_result(rows)
        renv.log(f"{when}: {text}")
        self.event("plan_check", when=when, checked=len(rows), invalid=[f"{b['name']} L{b['level']}" for b in bad])
        return bad

    def start_keeper(self, sid: str, res: rstate.SessionResult, plan_bad: list[dict] | None = None) -> None:
        """Starts the memory-keeper pass for the session that just ended (bg3play.memory.keeper). It runs in a
        thread while the runner prepares the next session (game check, mod reload, settings, check_all: none of
        them write memory) and is joined before the next player starts, so it never races the player's edits."""
        if not self.opt.keeper or self.opt.dry_run or res.ended - res.started < 120 or getattr(self, "cap_hit", None):
            if plan_bad:  # no keeper pass to fix them: the next player gets the note
                self.pending_notes.append(rcode.plan_note(plan_bad))
            if not self.opt.dry_run:
                self.write_review(sid)
            return
        n = int(sid[1:])
        base = self.state.dir / f"{sid}-keeper"
        model = rpolicy.keeper_model(self.opt.keeper_model, res.model or self.opt.model)
        effort, minutes = self.opt.keeper_effort, self.opt.keeper_minutes
        client = self.opt.client

        def keep():
            renv.log(
                f"{sid}: memory keeper starting ({client} {model}, effort {effort or 'default'}, ≤{minutes:g} min)"
            )
            try:  # stored level-up plan lines the game refused this session: the keeper rewrites them
                refused = keeper.refused_plans(rpolicy.read_actions_since(res.started))
            except Exception as e:
                refused = []
                renv.log(f"{sid}: refused plan lines not read: {e!r}")
            refused = merge_refused(refused, plan_bad or [])
            if refused:
                names = ", ".join(x["name"] + " L" + str(x["level"]) for x in refused)
                renv.log(f"{sid}: refused plan lines for the keeper: {names}")
            # the play client keeps the memory too (a benchmark compares one CLI end to end); claude stays on its
            # default pass with Claude Code's file tools, the others go through the bg3 server's keeper mode
            box = getattr(self, "box", None)
            r = keeper.run_keeper(
                self.mem,
                n,
                base,
                model,
                effort,
                minutes,
                refused=refused,
                client=client,
                **({"sandbox": box} if box else {}),
            )
            renv.log(r.summary())
            self.event(
                "memory_keeper",
                session=sid,
                status=r.status,
                seconds=r.seconds,
                files=r.files,
                commit=r.commit,
                timed_out=r.timed_out,
                reverted=r.reverted,
                lint_errors=r.lint_errors[:5],
                model=model,
                client=client,
            )
            try:
                with open(self.state.dir / "keeper_history.jsonl", "a") as f:
                    row = {"t": round(time.time(), 1), "client": client, "model": model, "effort": effort, **r.__dict__}
                    f.write(json.dumps(row) + "\n")
            except OSError as e:
                renv.log(f"keeper history write failed: {e}")

        def work():
            try:
                keep()
            finally:  # after the keeper: the review reads its memory_keeper event
                self.write_review(sid)

        self.keeper_thread = threading.Thread(target=work, name="memory-keeper", daemon=True)
        self.keeper_thread.start()

    def write_review(self, sid: str) -> Path | None:
        """Sxxx.review.md (tools/logs/review_session.py) for the logs worker, once the keeper pass is done (its
        memory/keeper section and the session's runner events are in by then; tools/logs/wait_new_session.sh wakes
        on it). Covers the session and its resumes. Niced, with a timeout; nothing here raises."""
        cmd = [
            "nice",
            "-n",
            "15",
            sys.executable,
            str(REVIEW_PY),
            self.state.dir.name,
            sid,
            "--runtime",
            str(renv.RUNTIME),
            "--sessions",
            str(self.state.dir.parent),
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=renv.REPO, timeout=renv.REVIEW_TIMEOUT)
        except subprocess.TimeoutExpired:
            renv.log(f"{sid}: review killed after {renv.REVIEW_TIMEOUT} s")
            return None
        except OSError as e:
            renv.log(f"{sid}: review failed: {e}")
            return None
        if p.returncode != 0:
            renv.log(f"{sid}: review failed (exit {p.returncode}): {(p.stderr or p.stdout).strip()[-300:]}")
            return None
        path = self.state.dir / f"{sid}.review.md"
        renv.log(f"{sid}: review {path}")
        return path

    def recover_keeper(self) -> None:
        """MEM-1: a keeper pass the runner died in (killed, crashed, restarted mid-pass) left its pre-pass snapshot:
        its process is killed and its unvalidated edits put back before any session builds on them."""
        if self.opt.dry_run:
            return
        msg = keeper.recover(self.mem, self.state.dir)
        if msg:
            renv.log(f"memory keeper: {msg}")
            self.event("keeper_recovered", detail=msg)

    def join_keeper(self) -> None:
        t, self.keeper_thread = getattr(self, "keeper_thread", None), None
        if t is None:
            return
        if t.is_alive():
            t0 = time.time()
            t.join(self.opt.keeper_minutes * 60 + 60)  # run_keeper kills claude at its own deadline
            renv.log(f"waited {time.time() - t0:.0f} s for the memory keeper before the next session")
        # A session left open by failed lint (e.g. stale objectives) may pass now that the keeper updated the files.
        if self.memory_state().get("open"):
            r = self.session_py("end")
            if r.returncode == 0:
                renv.log("memory session committed after the keeper pass")
