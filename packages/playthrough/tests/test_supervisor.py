import json
import subprocess
import threading
from pathlib import Path

import pytest

from bg3play import config as C
from bg3play import supervisor as S
from bg3play.adapters import legacy_cli as L
from bg3play.supervisor import SessionRequest, SessionResult, Supervisor, SupervisorError


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeRunner:
    """Plays scripted sessions: each takes the config's minutes on the fake clock and returns the next scripted
    (outcome, progress); `during` runs inside a session (e.g. a user stop)."""

    def __init__(self, clock: Clock, script: list[tuple[str, bool | None]], log: list):
        self.clock, self.script, self.log = clock, list(script), log
        self.requests: list[SessionRequest] = []
        self.during: dict[int, object] = {}

    def run_session(self, req: SessionRequest, stop: threading.Event) -> SessionResult:
        self.requests.append(req)
        self.log.append(("run", req.session))
        t0 = self.clock.t
        if req.session in self.during:
            self.during[req.session]()
        self.clock.t += req.config.session.minutes * 60
        outcome, progress = self.script.pop(0) if self.script else ("ok", True)
        if stop.is_set() and outcome == "ok":
            outcome = "stopped"
        return SessionResult(f"S{req.session:03d}", outcome, progress, t0, self.clock.t)


class FakeKeeper:
    def __init__(self, log: list):
        self.log = log

    def run_keeper(self, pt: str, result: SessionResult) -> None:
        self.log.append(("keeper", result.session))


def make(tmp_path: Path, script, hours=3.0, minutes=60.0, keeper=True):
    clock, log, events = Clock(), [], []
    pts = tmp_path / "playthroughs"
    (pts / "ilse").mkdir(parents=True)
    cfg = C.validate({"v": 1, "name": "ilse", "session": {"minutes": minutes, "hours": hours},
                      "keeper": {"enabled": keeper}})
    C.save(pts / "ilse", cfg)
    runner = FakeRunner(clock, script, log)
    sup = Supervisor(runner, FakeKeeper(log), tmp_path / "runtime", pts, clock, events.append)
    return sup, runner, log, events


def test_runs_sessions_to_the_budget_with_the_keeper_between(tmp_path: Path):
    sup, runner, log, events = make(tmp_path, [], hours=3.0)
    sup.start("ilse", wait=True)
    assert log == [("run", 1), ("keeper", "S001"), ("run", 2), ("keeper", "S002"), ("run", 3), ("keeper", "S003")]
    assert [r.resume for r in runner.requests] == [False, True, True]
    hist = sup.history("ilse")
    assert [h["session"] for h in hist] == ["S001", "S002", "S003"] and hist[0]["started"] == 1000.0
    assert sup.view() == {"playthrough": "ilse", "state": "idle", "detail": "hours budget spent", "session": "S003"}
    assert events[0]["state"] == "running" and events[-1]["state"] == "idle"


def test_stuck_nudge_after_two_sessions_without_progress(tmp_path: Path):
    sup, runner, _, _ = make(tmp_path, [("ok", False), ("ok", None), ("ok", False), ("ok", False), ("ok", True)],
                             hours=6.0)
    sup.start("ilse", wait=True)
    stuck = [any("CHANGE APPROACH" in n for n in r.notes) for r in runner.requests]
    # None (unknown) doesn't count; S005 follows S003 + S004 without progress; S006 follows S004 + a progress one
    assert stuck == [False, False, False, False, True, False]


def test_crash_resume_note_and_error_stops(tmp_path: Path):
    sup, runner, log, _ = make(tmp_path, [("crash", None), ("ok", True), ("error", None)], hours=0)
    sup.start("ilse", wait=True)
    assert runner.requests[1].notes and "ended early (crash)" in runner.requests[1].notes[0]
    assert runner.requests[2].notes == ()
    assert ("keeper", "S003") not in log and sup.state == "idle" and sup.detail.startswith("session error")


def test_finished_story_ends_the_playthrough(tmp_path: Path):
    sup, runner, log, _ = make(tmp_path, [("ok", True), ("finished", True)], hours=0)
    sup.start("ilse", wait=True)
    assert len(runner.requests) == 2 and log[-1] == ("keeper", "S002") and sup.detail == "the story is finished"


def test_stop_finishes_the_session_then_resume_continues(tmp_path: Path):
    sup, runner, log, events = make(tmp_path, [], hours=0)
    runner.during[2] = sup.stop  # the user presses Stop during S002
    sup.start("ilse", wait=True)
    assert log == [("run", 1), ("keeper", "S001"), ("run", 2), ("keeper", "S002")]
    assert sup.history("ilse")[-1]["outcome"] == "stopped" and sup.view()["detail"] == "stopped"
    assert any(e["state"] == "stopping" for e in events)
    with pytest.raises(SupervisorError, match="resume it"):
        sup.start("ilse")
    runner.during[3] = sup.stop
    sup.resume("ilse", wait=True)
    assert runner.requests[2].session == 3 and runner.requests[2].resume


def test_refusals(tmp_path: Path):
    sup, runner, _, _ = make(tmp_path, [])
    with pytest.raises(SupervisorError, match="nothing is running"):
        sup.stop()
    with pytest.raises(SupervisorError, match="no sessions to resume"):
        sup.resume("ilse")
    gate, entered = threading.Event(), threading.Event()

    def hold():
        entered.set()
        gate.wait(10)

    runner.during[1] = hold
    sup.start("ilse")  # on a thread
    assert entered.wait(10)
    with pytest.raises(SupervisorError, match="ilse is running"):
        sup.start("other")
    sup.stop()
    gate.set()
    sup.thread.join(10)
    assert sup.state == "idle" and len(runner.requests) == 1


def test_a_runner_exception_is_reported(tmp_path: Path):
    sup, runner, _, _ = make(tmp_path, [])

    def boom():
        raise OSError("play.py missing")

    runner.during[1] = boom
    sup.start("ilse", wait=True)
    assert sup.state == "idle" and sup.detail == "OSError: play.py missing"


def test_launch_config_and_saved_on_start(tmp_path: Path):
    sup, runner, _, _ = make(tmp_path, [("error", None)])
    cfg = S.launch_config(C.load(tmp_path / "playthroughs" / "ilse"),
                          {"profile": "codex", "brief": "a dwarf cleric", "difficulty": "Tactician",
                           "session_minutes": 45, "recording": False})
    assert (cfg.agent.profile, cfg.character.brief, cfg.difficulty, cfg.session.minutes, cfg.recording.enabled) == (
        "codex", "a dwarf cleric", "Tactician", 45.0, False)
    with pytest.raises(C.ConfigError, match="unknown launch field"):
        S.launch_config(cfg, {"api_key": "x"})
    with pytest.raises(C.ConfigError, match="difficulty"):
        S.launch_config(cfg, {"difficulty": "Nightmare"})
    sup.start("ilse", wait=True, cfg=cfg)
    assert C.load(tmp_path / "playthroughs" / "ilse") == cfg and runner.requests[0].record is False


# ------------------------------------------------------------ the legacy CLI adapter


def req(**cfg) -> SessionRequest:
    return SessionRequest("ilse", 1, C.validate({"v": 1, "name": "ilse", **cfg}), resume=False)


def test_play_args():
    a = L.play_args("python3", Path("/repo"), req(session={"minutes": 45}), C.Agent(client="claude", model="opus"))
    assert a[:4] == ["python3", "/repo/runner/play.py", "run", "--playthrough"]
    s = " ".join(a)
    assert "--hours 0.75 --session-minutes 45 --client claude --model opus" in s and "--effort" not in a
    assert "--keeper-effort low --keeper-minutes 5" in s
    a = L.play_args("py", Path("/r"), req(difficulty="Balanced", keeper={"enabled": False},
                                          character={"brief": "a bard", "roleplay": "light"}),
                    C.Agent(client="codex", model="gpt", effort="high"))
    s = " ".join(a)
    assert "--effort high" in s and "--no-keeper" in a and "--difficulty Balanced" in s
    assert a[a.index("--character-brief") + 1] == "a bard" and "--roleplay light" in s


def test_profile_agent(tmp_path: Path):
    (tmp_path / "codex.toml").write_text('client = "codex"\nmodel = "gpt-5.5"\neffort = "high"\n')
    (tmp_path / "pi.toml").write_text('client = "pi"\nmodel = "x"\n[endpoint]\nprovider = "openrouter"\n')
    assert L.profile_agent(tmp_path, "codex") == C.Agent("codex", "codex", "gpt-5.5", "high")
    with pytest.raises(SupervisorError, match="pi.toml"):   # runner/profiles.py: no base_url, no model_info
        L.profile_agent(tmp_path, "pi")
    with pytest.raises(SupervisorError, match="missing"):
        L.profile_agent(tmp_path, "missing")


def test_pi_profile_runs_from_its_file_with_a_cost_cap():
    repo_profiles = Path(__file__).resolve().parents[3] / "profiles"
    agent = L.profile_agent(repo_profiles, "deepseek-flash")
    assert (agent.client, agent.model, agent.effort) == ("pi", "deepseek-flash", "high")
    with pytest.raises(SupervisorError, match="cost cap"):
        L.play_args("py", Path("/r"), req(), agent, repo_profiles)
    a = L.play_args("py", Path("/r"), req(session={"max_cost_usd": 2.5}, keeper={"enabled": True}), agent,
                    repo_profiles)
    assert a[a.index("--profile") + 1] == str(repo_profiles / "deepseek-flash.toml")
    assert a[a.index("--max-cost") + 1] == "2.5" and "--no-keeper" in a
    assert "--client" not in a and "--keeper-effort" not in a
    with pytest.raises(SupervisorError, match="pick a pi client profile"):
        L.play_args("py", Path("/r"), req(session={"max_cost_usd": 1.0}), C.Agent(client="pi", model="x"))
    claude = L.play_args("py", Path("/r"), req(session={"max_cost_usd": 3.0}), C.Agent())
    assert claude[claude.index("--max-cost") + 1] == "3" and "--client" in claude
    assert "--max-cost" not in L.play_args("py", Path("/r"), req(), C.Agent())   # no cap: today's command line


def test_result_from():
    e = {"session": "S004", "reason": "exited", "progress": True, "started": 1.0, "ended": 2.0}
    assert L.result_from(e, None, 0, False) == SessionResult("S004", "ok", True, 1.0, 2.0, detail="reason=exited exit=0")
    assert L.result_from(e, None, 0, True).outcome == "stopped"
    assert L.result_from(dict(e, reason="crash"), None, 0, False).outcome == "crash"
    assert L.result_from(e, None, 3, False).outcome == "crash"
    assert L.result_from(e, e, 0, False).outcome == "error"  # no new session
    assert L.result_from(None, None, 2, False).detail == "play.py exited 2 without a session"


class FakeProc:
    def __init__(self, on_exit, timeouts: int):
        self.on_exit, self.timeouts = on_exit, timeouts

    def wait(self, timeout=None):
        if self.timeouts:
            self.timeouts -= 1
            raise subprocess.TimeoutExpired("play.py", timeout)
        self.on_exit()
        return 0

    def poll(self):
        return None


def test_legacy_run_session_soft_stop(tmp_path: Path):
    runtime = tmp_path / "runtime"
    rj = runtime / "sessions" / "ilse" / "runner.json"
    rj.parent.mkdir(parents=True)
    rj.write_text(json.dumps({"history": [{"session": "S001", "reason": "exited"}]}))
    spawned = []

    def finish():
        assert (runtime / "STOP").exists()  # the soft stop reached play.py before it exited
        h = json.loads(rj.read_text())["history"]
        rj.write_text(json.dumps({"history": h + [{"kind": "play", "session": "S002", "reason": "exited",
                                                   "progress": False}]}))

    def spawn(args, cwd, log):
        spawned.append((args, cwd))
        return FakeProc(finish, timeouts=1)

    cli = L.LegacyCli(tmp_path / "repo", runtime, spawn_fn=spawn, poll_s=0)
    stop = threading.Event()
    stop.set()
    res = cli.run_session(req(), stop)
    assert res.session == "S002" and res.outcome == "stopped" and res.progress is False
    assert spawned[0][1] == tmp_path / "repo" and spawned[0][0][1].endswith("runner/play.py")


def test_sessions_the_runner_played_before_count(tmp_path: Path):
    sup, runner, _, _ = make(tmp_path, [("error", None)])
    sup.played = lambda pt: 96  # a playthrough the legacy runner played 96 sessions of
    with pytest.raises(SupervisorError, match="already has 96 sessions"):
        sup.start("ilse")
    sup.resume("ilse", wait=True)
    assert runner.requests[0].session == 97 and runner.requests[0].resume


def test_a_stop_from_outside_ends_the_loop(tmp_path: Path):
    sup, runner, _, _ = make(tmp_path, [("ok", True), ("stopped", None)], hours=4)  # e.g. `play.py stop`
    sup.start("ilse", wait=True)
    assert len(runner.requests) == 2 and sup.state == "idle" and sup.detail == "stopped"


def test_crashes_in_a_row_end_the_loop(tmp_path: Path):
    sup, runner, _, _ = make(tmp_path, [("crash", None), ("ok", True)] + [("crash", None)] * 5, hours=0)
    sup.start("ilse", wait=True)
    assert len(runner.requests) == 5 and sup.detail == "3 sessions crashed in a row"
