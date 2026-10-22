"""The playthrough memory's session (moved from runner/legacy_playthrough.py, Phase D.2): session.py start (was the
SessionStart hook), stop (the Stop hook: may the session end?), session-end (the SessionEnd hook) and end (the
commit), the clock env it reads, the next session's id and the HANDOFF's session.

The pure parts are memory_session's (the app's HarnessRunner uses them too); MemorySide is the legacy Runner's mixin
over SessionRunner's state.
"""

from __future__ import annotations

import os
import subprocess
import sys

from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
from bg3harness.session import GAME_FAULTS

from ..memory import memlib
from .memory_session import clock_env, opened_sid, parse_handoff_session, stop_reason

SESSION_PY = renv.REPO / "tools" / "memory" / "session.py"  # the path the runner and the hooks call


class MemorySide:
    def check_memory(self) -> None:
        from ..memory.new_playthrough import SetupError, link_play_memory

        if not self.mem.is_dir():
            create = f"python3 tools/memory/new_playthrough.py {self.opt.playthrough}"
            raise SystemExit(f"no memory at {self.mem}; create it with: {create}")
        link = renv.PLAY_DIR / "memory"
        if not (link.is_symlink() and link.resolve() == self.mem.resolve()) and not self.opt.dry_run:
            try:
                prev = link_play_memory(renv.PLAY_DIR, self.mem)
            except SetupError as e:
                raise SystemExit(str(e)) from e
            renv.log(f"play/memory → {self.mem} (was {prev})")

    def memory_state(self) -> dict:
        return memlib.load_state(self.mem)

    def next_sid(self) -> str:
        reuse = memlib.reusable_session(self.mem)  # an empty killed session's id is reused by session.py start
        if reuse:
            return rpolicy.session_id(reuse)
        current = self.memory_state().get("current")
        return rpolicy.session_id(rpolicy.predict_session_number(memlib.session_numbers(self.mem), current))

    def session_py(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SESSION_PY), *args, "--memory-dir", str(self.mem)],
            capture_output=True,
            text=True,
            cwd=renv.PLAY_DIR,
            stdin=subprocess.DEVNULL,
        )

    def session_env(self, deadline: float, minutes: float, sid: str | None = None) -> dict:
        """The clock env session.py sees for a session (what run_claude gives claude and the MCP server)."""
        env = dict(os.environ, **clock_env(deadline, minutes, self.opt.keeper and not self.opt.dry_run))
        if sid and getattr(self, "state", None) is not None:  # the playthrough state comes with run()
            env["BG3AI_FIGHT_HOLD_PATH"] = str(rpolicy.fight_hold_path(self.state.dir, sid))
        return env

    def session_cmd(self, args: list[str], env: dict) -> subprocess.CompletedProcess:
        """session.py <args> with the session's clock env; never reads the runner's stdin."""
        return subprocess.run(
            [sys.executable, str(SESSION_PY), *args, "--memory-dir", str(self.mem)],
            capture_output=True,
            text=True,
            cwd=renv.PLAY_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
        )

    def open_memory_session(self, deadline: float, minutes: float) -> tuple[str | None, str]:
        """session.py start (was the SessionStart hook): opens sessions/Sxxx.md. Returns (sid, the memory briefing for
        the kickoff), or (None, "") when it couldn't (the agent then calls session_start)."""
        try:
            r = self.session_cmd(["start"], self.session_env(deadline, minutes))
            st = self.memory_state()
        except (OSError, ValueError) as e:
            renv.log(f"memory session not opened: {e}")
            return None, ""
        sid = opened_sid(r.returncode, st)
        if sid is None:
            renv.log(
                f"memory session not opened (session.py start exit {r.returncode}): {(r.stdout or r.stderr)[-300:]}"
            )
            return None, ""
        return sid, r.stdout.strip()

    def stop_check(self, sid: str, deadline: float, minutes: float) -> str | None:
        """session.py stop after claude ended its turn (was the Stop hook): the reason the session can't end yet
        (keep playing, write the handoff, fix lint), or None (it ended; on success the memory is committed)."""
        try:
            r = self.session_cmd(["stop"], self.session_env(deadline, minutes, sid))
            reason, message = stop_reason(r.stdout)
        except (OSError, ValueError) as e:
            renv.log(f"{sid}: stop check failed: {e}")
            return None
        if message:
            renv.log(f"{sid}: stop check: {message}")
        return reason

    def close_memory_session(self, res: rstate.SessionResult) -> None:
        st = self.memory_state()
        if not st.get("open"):
            renv.log(f"{res.sid}: memory already committed (end_session or the stop check)")
            return
        r = self.session_py("end")
        if r.returncode == 0:
            renv.log(f"{res.sid}: memory committed by session.py end")
            return
        if rcode.until_stop_handoff_only(res.reason, bool(self.opt.until or self.until_hit), r.stdout):
            # The --until stop ends the session at the milestone, before any wrap-up, so HANDOFF.md is still the
            # previous session's: that alone is no notes problem (naut/naut2: a 14 s fix pass + the wait every run).
            # The keeper rewrites HANDOFF; a later session start reconciles the open session.
            renv.log(
                f"{res.sid}: memory lint: only the handoff check failed (the --until stop came before the wrap-up): "
                "no fix-notes session; memory left open (the keeper writes the handoff)"
            )
            self.event("fix_notes_skipped", session=res.sid, why="until_milestone_handoff_only")
            return
        if (
            res.reason in ("exited", "hard_stop", "milestone")
            and res.claude_session
            and res.ended - res.started >= 60
            and not (self.stop_requested or self.abort)
        ):  # RUN-7: <1 min: no
            from .prompts import fix_notes_prompt

            renv.log(f"{res.sid}: memory lint failed; running a {renv.FIX_SESSION_MINUTES}-minute fix-notes session")
            self.run_claude(
                res.sid,
                fix_notes_prompt(r.stdout),
                renv.FIX_SESSION_MINUTES,
                res.claude_session,
                resume=True,
                tag="-fix",
                needs_game=False,
            )
        if not self.memory_state().get("open"):
            return
        r = self.session_py("end")
        if r.returncode == 0:
            renv.log(f"{res.sid}: memory committed after the fix-notes session")
        elif res.reason in (*GAME_FAULTS, "killed", "aborted"):
            # The game died before the agent's wrap-up, so the end lint's HANDOFF check ("written by the current
            # session") can't pass and no fix pass runs. session.py start reconciles the open session (S014: the
            # note below told the next session to rewrite HANDOFF as the crashed one). Pass on only other errors.
            other = rcode.memory_lint_errors(r.stdout, skip=("handoff",))
            why = f"a game {res.reason}" if res.reason in GAME_FAULTS else f"claude {res.reason}"
            renv.log(
                f"{res.sid}: ended by {why} before wrap-up; memory left open for the next session"
                + (f" ({len(other)} other lint error(s))" if other else "")
            )
            if other:
                self.pending_notes.append(
                    f"RUNNER NOTE: {res.sid} ended by {why} and its memory is uncommitted. Besides "
                    "the missing handoff (the session ended before wrap-up), fix these lint errors early:\n"
                    + "\n".join(other)[-2500:]
                )
        else:
            renv.log(f"{res.sid}: memory still fails lint; left open for the next session to reconcile")
            self.pending_notes.append(
                f"RUNNER NOTE: memory lint still failed after {res.sid}; the session was left uncommitted. "
                "Fix these lint errors early in this session:\n" + r.stdout[-2500:]
            )

    def handoff_session(self, text: str) -> int | None:
        return parse_handoff_session(text)
