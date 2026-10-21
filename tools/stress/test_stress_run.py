"""stress_core's verdicts, plan and rendering, and stress_exec's Runner against fake game, saves, lock, clock and
runners; run.py's offline modes (--plan, --record, --render, --check)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import pytest
import run as cli
import stress_core as core
import stress_exec as ex
import stress_matrix as sm

pytestmark = pytest.mark.integration

HERE = Path(__file__).resolve().parent
SCHEMA = json.loads((HERE / "results.schema.json").read_text())
MATRIX = """
[defaults]
profile = "profiles/sub.toml"
max_cost = 0.5
minutes = 10
character_brief = "Plays the save's character."
min_free_gb = 4.0
max_cpu_c = 90
guard_wait_s = 60
guard_poll_s = 30

[[group]]
key = "g"
title = "Group"
rows = [
"""
ROWS = [  # one TOML line per row once joined
    (
        '{id = "A1", situation = "load", act = "2", save = "S027-start", check = [{kind = "acc", '
        'section = "se", item = "load_game game-process check"}], baseline = "PASS acc"}'
    ),
    (
        '{id = "A2", situation = "two", act = "2", save = "S027-start", check = [{kind = "acc", '
        'section = "trade", item = "trade confirm closes"}, {kind = "acc", section = "reads", '
        'item = "ALERTS line"}]}'
    ),
    (
        '{id = "B1", situation = "probe", act = "1", save = "S007-pre-grove-gate", check = [{kind = "probe", '
        'script = "runtime/probes/live1/l2.py"}]}'
    ),
    (
        '{id = "B2", situation = "not ready", act = "1", save = "S007-pre-grove-gate", '
        'check = [{kind = "probe", script = "x.py", ready = false, why = "needs args"}]}'
    ),
    (
        '{id = "C1", situation = "fight", act = "2", save = "S043-prison-cleared", check = [{kind = "agent", '
        'where = "Prison.", objective = "Win the fight.", goal = {region = "moonrise"}, minutes = 30}]}'
    ),
    (
        '{id = "C2", situation = "look", act = "P", save = "", check = [{kind = "agent", where = "Menu.", '
        'objective = "Start.", profile = "profiles/paid.toml"}, {kind = "manual", what = "press F5"}]}'
    ),
]
MATRIX += "".join(f"  {r},\n" for r in ROWS) + "]\n"
ITEMS = [  # cut from the 2026-10-21 live window's acceptance/results.json
    {"section": "se", "item": "load_game game-process check", "verdict": "PASS", "evidence": "new Lua state"},
    {"section": "trade", "item": "trade confirm closes the window", "verdict": "PASS", "evidence": "closed"},
    {"section": "reads", "item": "section reads", "verdict": "FAIL", "evidence": "script error KeyError"},
]


def facts(region: str) -> list[dict]:
    snap = {
        "v": 1,
        "kind": "snapshot",
        "t": 1010,
        "game_t": 10,
        "gold": 0,
        "party": [],
        "region": region,
        "area": None,
        "areas": [],
        "combat": False,
        "inv": [],
        "quests": {},
    }
    return [{"v": 1, "kind": "mark", "t": 1000, "game_t": 0, "mark": "run_start"}, snap]


RECORD = {
    "playthrough": "p",
    "outcome": "timeout",
    "wall_min": 30.2,
    "cost_usd": 0,
    "harness_failures": 0,
    "crashes": 0,
}


@pytest.fixture
def m() -> sm.Matrix:
    return sm.parse(MATRIX)


# ------------------------------------------------------------------------------------------------ pure core
def test_combine_fail_wins_then_error_then_all_pass():
    assert core.combine(["PASS", "FAIL", "ERROR"]) == "FAIL"
    assert core.combine(["PASS", "ERROR", "SKIP"]) == "ERROR"
    assert core.combine(["PASS", "PASS"]) == "PASS"
    assert core.combine(["PASS", "SKIP", "REVIEW"]) == "REVIEW"
    assert core.combine(["INFO", "SKIP"]) == "SKIP"
    assert core.combine([]) == ""


def test_a_row_waits_for_its_checks_unless_one_failed(m):
    row = m.by_id["A2"]
    res = core.new_results()
    assert core.row_result(row, res) == ("", [], 2)
    res["checks"]["A2#0"] = {"verdict": "PASS", "evidence": "ok", "state": "done"}
    assert core.row_result(row, res) == ("", ["A2#0 PASS: ok"], 1)
    res["checks"]["A2#0"]["verdict"] = "FAIL"
    assert core.row_result(row, res)[0] == "FAIL"
    res["checks"]["A2#1"] = {"verdict": "", "evidence": "", "state": "running"}  # interrupted: not finished
    assert core.row_result(row, res)[2] == 1


def test_todo_resumes_and_reruns(m):
    res = core.new_results()
    everything = [c.key for c in core.todo(m, res, core.Select())]
    assert everything == ["A1#0", "A2#0", "A2#1", "B1#0", "B2#0", "C1#0", "C2#0", "C2#1"]
    res["checks"] = {
        "A1#0": {"verdict": "PASS", "state": "done"},
        "A2#0": {"verdict": "FAIL", "state": "done"},
        "A2#1": {"verdict": "", "state": "running"},  # the runner died during it
    }
    assert [c.key for c in core.todo(m, res, core.Select(kinds=frozenset({"acc"})))] == ["A2#1"]
    assert [c.key for c in core.todo(m, res, core.Select(kinds=frozenset({"acc"})), "failed")] == ["A2#0", "A2#1"]
    assert len(core.todo(m, res, core.Select(kinds=frozenset({"acc"})), "all")) == 3
    assert [c.key for c in core.todo(m, res, core.Select(ids=frozenset({"C2#1", "B1"})))] == ["B1#0", "C2#1"]
    assert [c.key for c in core.todo(m, res, core.Select(acts=frozenset({"P"})))] == ["C2#0", "C2#1"]
    with pytest.raises(ValueError):
        core.todo(m, res, core.Select(), "some")


def test_plan_runs_one_live_window_first_then_matrix_order_then_manual(m):
    steps = core.plan(core.todo(m, core.new_results(), core.Select()), ["reads", "setup", "trade", "se"])
    assert [(s.kind, [c.key for c in s.checks]) for s in steps] == [
        ("acc", ["A1#0", "A2#0", "A2#1"]),
        ("probe", ["B1#0"]),
        ("skip", ["B2#0"]),
        ("agent", ["C1#0"]),
        ("agent", ["C2#0"]),
        ("manual", ["C2#1"]),
    ]
    assert steps[0].sections == ("reads", "trade", "se")


def test_acc_verdicts_match_by_prefix_and_say_why_an_item_is_missing(m):
    checks = m.by_id["A1"].checks + m.by_id["A2"].checks
    out = core.acc_verdicts(checks, ITEMS, "runtime/live/x")
    assert out["A1#0"][0] == "PASS" and "new Lua state" in out["A1#0"][1]
    assert out["A2#0"][0] == "PASS"
    assert out["A2#1"][0] == "ERROR"
    assert "no item 'ALERTS line'" in out["A2#1"][1] and "script error KeyError" in out["A2#1"][1]


@pytest.mark.parametrize(
    ("rc", "text", "want"),
    [
        (0, "50 cycles\nSTRESS PASS 50/50 closed\n", ("PASS", "50/50 closed (l.log)")),
        (1, "STRESS FAIL: stuck 3\nTraceback", ("FAIL", "stuck 3 (l.log)")),
        (0, "cycle median 0.4\n", ("REVIEW", "exit 0, no STRESS line: read l.log; last line: cycle median 0.4")),
        (1, "boom\n\n", ("FAIL", "exit 1: boom (l.log)")),
        (None, "half", ("FAIL", "timeout; last line: half (l.log)")),
    ],
)
def test_probe_verdict(rc, text, want):
    assert core.probe_verdict(rc, text, "l.log") == want


def test_session_verdict():
    goal = {"region": "moonrise"}
    assert core.session_verdict(None, [], goal, "w")[0] == "ERROR"
    assert core.session_verdict(dict(RECORD, crashes=1), [], goal, "w")[0] == "ERROR"
    assert core.session_verdict(dict(RECORD, outcome="runner_error"), [], goal, "w")[0] == "ERROR"
    v, ev = core.session_verdict(RECORD, facts("Moonrise Towers"), None, "w")
    assert v == "REVIEW" and "outcome timeout, 30.2 min" in ev
    v, ev = core.session_verdict(RECORD, facts("Moonrise Towers"), goal, "w")
    assert v == "PASS" and ev.startswith("goal met at 10 s")
    assert core.session_verdict(RECORD, facts("Wilderness"), goal, "w")[0] == "FAIL"
    assert core.session_verdict(RECORD, [], goal, "w")[0] == "ERROR"  # empty timeline


def test_scenario_and_cost_cap(m):
    c1, c2 = m.by_id["C1"].checks[0], m.by_id["C2"].checks[0]
    sc = core.resolve_scenario(m.by_id["C1"], c1, m.defaults, ("Claude-1__SCN-stress_c1", "Claude"), None)
    assert sc["start"] == {
        "kind": "save",
        "save": "Claude-1__SCN-stress_c1",
        "save_sha1": None,
        "note": {"where": "Prison.", "objective": "Win the fight."},
    }
    assert sc["caps"] == {"run_hours": 0.5, "max_cost": None, "max_tokens": None}
    assert sc["profile"]["character"]["name"] == "Claude" and sc["until"] == "game_end"
    sc2 = core.resolve_scenario(m.by_id["C2"], c2, m.defaults, None, 0.5)
    assert sc2["start"] == {"kind": "new_game"} and sc2["caps"]["run_hours"] == round(10 / 60, 4)
    assert core.cost_cap(c1, m.defaults, {"client": "claude"}, 2.0) is None
    assert core.cost_cap(c1, m.defaults, {"client": "pi"}, None) == 0.5
    assert core.cost_cap(c1, m.defaults, {"client": "pi"}, 2.0) == 2.0


def test_render_summarises_per_group_and_act(m):
    res = core.new_results()
    res["checks"] = {
        "A1#0": {"verdict": "PASS", "evidence": "acc ok", "state": "done"},
        "A2#0": {"verdict": "FAIL", "evidence": "a|b", "state": "done"},
        "C2#0": {"verdict": "REVIEW", "evidence": "read it", "state": "done"},
    }
    md = core.render(m, res, "2026-10-22 10:00")
    assert "| Group | 6 | 1 | 1 | 0 | 0 | 0 | 0 | 4 |" in md  # C2 waits for its manual check
    assert "| 2 | 3 | 1 | 1 | 0 | 0 | 0 | 0 | 1 |" in md
    assert "| A2 | two | 2 | S027-start |" in md and "A2#0 FAIL: a/b" in md
    assert "| C2 | look | P | new game | agent (10 min, review) + manual | 1/2 run: C2#0 REVIEW: read it |" in md
    assert "| B2 | not ready | 1 | S007-pre-grove-gate | probe:x.py (not ready) |" in md


# ------------------------------------------------------------------------------------------------ Runner with fakes
class FakeClock:
    def __init__(self):
        self.t, self.slept = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


class FakeHost:
    def __init__(self, mem=(16.0,), temp=50.0):
        self.mem, self.temp, self.stop = list(mem), temp, False

    def mem_available_gb(self):
        return self.mem.pop(0) if len(self.mem) > 1 else self.mem[0]

    def cpu_temp_c(self):
        return self.temp

    def stop_requested(self):
        return self.stop


class FakeSaves:
    def __init__(self, missing=()):
        self.copies, self.missing = [], set(missing)

    def copy(self, label, new_label):
        if label in self.missing:
            raise ex.SaveError(f"no save labelled {label!r}")
        self.copies.append((label, new_label))
        return f"Claude-1__{new_label}", "Claude"


class FakeLock:
    def __init__(self, busy=False):
        self.held, self.busy, self.active = 0, busy, False

    @contextmanager
    def hold(self):
        if self.busy:
            raise ex.LockBusyError("game.lock held")
        self.held += 1
        self.active = True
        try:
            yield
        finally:
            self.active = False


class FakeAcc:
    def __init__(self, items=ITEMS):
        self.calls, self.items = [], items

    def run(self, sections):
        self.calls.append(sections)
        return ex.AccOutcome(self.items, "runtime/live/w1", "" if self.items is not None else "exit 1")


class StoppingAcc(FakeAcc):
    """Someone touches the STOP file while the live window runs."""

    def __init__(self, host: FakeHost):
        super().__init__()
        self.host = host

    def run(self, sections):
        self.host.stop = True
        return super().run(sections)


class FakeProbe:
    def __init__(self, lock, outcome=None):
        self.calls, self.lock, self.outcome = [], lock, outcome

    def run(self, script, args, save_folder, out_dir, timeout_s):
        assert self.lock.active, "a probe runs under game.lock"
        self.calls.append((script, save_folder, out_dir, timeout_s))
        return self.outcome or ex.ProbeOutcome(0, "STRESS PASS 50/50", "p.log")


class FakeSession:
    def __init__(self, lock, region="Moonrise Towers", outcome=None):
        self.calls, self.lock, self.region, self.outcome = [], lock, region, outcome

    def run(self, scenario, profile, max_cost, playthrough):
        assert self.lock.active, "a session runs under game.lock"
        self.calls.append((scenario, profile, max_cost, playthrough))
        if self.outcome:
            return self.outcome
        return ex.SessionOutcome(dict(RECORD, playthrough=playthrough), facts(self.region), f"sessions/{playthrough}")


class MemStore:
    def __init__(self, results=None):
        self.results, self.saves = results or core.new_results(), 0

    def load(self):
        return json.loads(json.dumps(self.results))

    def save(self, results):
        jsonschema.validate(results, SCHEMA)
        self.results, self.saves = json.loads(json.dumps(results)), self.saves + 1


PROFILES = {"profiles/sub.toml": {"client": "claude"}, "profiles/paid.toml": {"client": "pi"}}


@dataclass
class World:
    """The fakes one Runner was built with, typed for the assertions."""

    clock: FakeClock
    host: FakeHost
    saves: FakeSaves
    lock: FakeLock
    acc: FakeAcc
    probe: FakeProbe
    session: FakeSession
    store: MemStore
    rendered: list[dict] = field(default_factory=list)
    said: list[str] = field(default_factory=list)
    profiles: dict[str, dict] = field(default_factory=lambda: dict(PROFILES))

    def read_profile(self, path: str) -> dict:
        if path not in self.profiles:
            raise OSError("no such file")
        return self.profiles[path]


def runner(
    m: sm.Matrix,
    store: MemStore | None = None,
    host: FakeHost | None = None,
    saves: FakeSaves | None = None,
    lock: FakeLock | None = None,
    acc: FakeAcc | None = None,
    opts: ex.Opts | None = None,
    region: str = "Moonrise Towers",
    probe_out: ex.ProbeOutcome | None = None,
    session_out: ex.SessionOutcome | None = None,
) -> tuple[ex.Runner, World]:
    lock = lock or FakeLock()
    w = World(
        FakeClock(),
        host or FakeHost(),
        saves or FakeSaves(),
        lock,
        acc or FakeAcc(),
        FakeProbe(lock, probe_out),
        FakeSession(lock, region, session_out),
        store or MemStore(),
    )
    deps = ex.Deps(
        clock=w.clock,
        host=w.host,
        saves=w.saves,
        lock=w.lock,
        acc=w.acc,
        probe=w.probe,
        session=w.session,
        store=w.store,
        read_profile=w.read_profile,
        render=w.rendered.append,
        say=w.said.append,
    )
    return ex.Runner(m, deps, opts or ex.Opts()), w


def steps(m, results=None, sel: core.Select | None = None, rerun="none"):
    return core.plan(core.todo(m, results or core.new_results(), sel or core.Select(), rerun), ["reads", "trade", "se"])


def test_a_full_run_records_every_check(m):
    r, d = runner(m)
    assert r.run(steps(m)) == ex.EXIT_DONE
    got = {k: v["verdict"] for k, v in d.store.results["checks"].items()}
    assert got == {
        "A1#0": "PASS",
        "A2#0": "PASS",
        "A2#1": "ERROR",
        "B1#0": "PASS",
        "B2#0": "SKIP",
        "C1#0": "PASS",
        "C2#0": "REVIEW",
    }  # C2#1 is manual: listed, not run
    assert d.acc.calls == [("reads", "trade", "se")]
    assert d.saves.copies == [("S007-pre-grove-gate", "SCN-stress_b1"), ("S043-prison-cleared", "SCN-stress_c1")]
    assert d.probe.calls == [("runtime/probes/live1/l2.py", "Claude-1__SCN-stress_b1", "stress/probe/B1-0", 1800.0)]
    (sc1, prof1, cost1, pt1), (sc2, prof2, cost2, _pt2) = d.session.calls
    assert (sc1["start"]["save"], prof1, cost1, pt1) == (
        "Claude-1__SCN-stress_c1",
        "profiles/sub.toml",
        None,
        "stress-c1-a1",
    )
    assert (sc2["start"], prof2, cost2) == ({"kind": "new_game"}, "profiles/paid.toml", 0.5)
    assert sc2["caps"]["max_cost"] == 0.5
    assert d.lock.held == 3
    assert any("manual C2#1: press F5" in s for s in d.said)
    assert d.rendered and d.rendered[-1] == d.store.results
    assert all(v["attempts"] == 1 for v in d.store.results["checks"].values())


def test_a_cli_profile_and_max_cost_apply_to_every_agent_check(m):
    r, d = runner(m, opts=ex.Opts(profile="profiles/paid.toml", max_cost=2.0))
    r.run(steps(m, sel=core.Select(kinds=frozenset({"agent"}))))
    assert [(c[1], c[2]) for c in d.session.calls] == [("profiles/paid.toml", 2.0)] * 2


def test_a_goal_not_met_is_a_fail(m):
    r, d = runner(m, region="Wilderness")
    r.run(steps(m, sel=core.Select(ids=frozenset({"C1"}))))
    assert d.store.results["checks"]["C1#0"]["verdict"] == "FAIL"


def test_resume_reruns_the_interrupted_check_only(m):
    first, d1 = runner(m)
    first.run(steps(m))
    done = d1.store.results
    done["checks"]["C1#0"].update(state="running", verdict="", evidence="")  # the runner died in C1's session
    store = MemStore(done)
    r, d = runner(m, store=store)
    r.run(steps(m, store.load()))
    assert d.acc.calls == [] and len(d.session.calls) == 1 and d.probe.calls == []
    c1 = d.store.results["checks"]["C1#0"]
    assert c1["attempts"] == 2 and c1["evidence"].endswith("[attempt 2]") and c1["state"] == "done"


def test_the_ram_guard_waits_then_runs(m):
    r, d = runner(m, host=FakeHost(mem=(2.0, 2.0, 8.0)))
    assert r.run(steps(m, sel=core.Select(ids=frozenset({"A1"})))) == ex.EXIT_DONE
    assert d.clock.slept == [30, 30]
    assert any("RAM 2.0 GB free < 4" in s for s in d.said)


def test_a_guard_that_never_clears_stops_the_run_before_starting_anything(m):
    host = FakeHost(temp=95.0)
    r, d = runner(m, host=host)
    assert r.run(steps(m)) == ex.EXIT_STOPPED
    assert d.clock.slept == [30, 30] and r.o.stopped == "guard: CPU 95 C > 90 after 60 s"
    assert d.acc.calls == [] and d.store.results["checks"] == {} and d.rendered


def test_the_stop_file_and_the_deadline_stop_between_steps(m):
    host = FakeHost()
    r, d = runner(m, host=host, acc=StoppingAcc(host))
    assert r.run(steps(m)) == ex.EXIT_STOPPED and r.o.stopped == "STOP file"
    assert "B1#0" not in d.store.results["checks"] and "A1#0" in d.store.results["checks"]
    r2, d2 = runner(m, opts=ex.Opts(hours=1))
    d2.clock.t += 3600
    assert r2.run(steps(m)) == ex.EXIT_STOPPED and r2.o.stopped == "--hours 1 used up"


def test_missing_save_busy_lock_bad_profile_and_no_acc_results_are_errors(m):
    r, d = runner(m, saves=FakeSaves(missing={"S043-prison-cleared"}), acc=FakeAcc(items=None))
    r.run(steps(m))
    res = d.store.results["checks"]
    assert res["C1#0"]["verdict"] == "ERROR" and "no save labelled" in res["C1#0"]["evidence"]
    assert res["A1#0"]["verdict"] == "ERROR" and "acceptance gave no results: exit 1" in res["A1#0"]["evidence"]
    r2, d2 = runner(m, lock=FakeLock(busy=True))
    r2.run(steps(m, sel=core.Select(kinds=frozenset({"probe", "agent"}))))
    assert {d2.store.results["checks"][k]["verdict"] for k in ("B1#0", "C1#0", "C2#0")} == {"ERROR"}
    r3, d3 = runner(m)
    d3.profiles.clear()
    r3.run(steps(m, sel=core.Select(ids=frozenset({"C1"}))))
    assert "profile profiles/sub.toml: no such file" in d3.store.results["checks"]["C1#0"]["evidence"]


def test_a_probe_or_session_that_could_not_start_is_an_error(m):
    r, d = runner(
        m,
        probe_out=ex.ProbeOutcome(None, "", "p.log", "no probe script x"),
        session_out=ex.SessionOutcome(None, [], "s.log", "play.py refused: no budget"),
    )
    r.run(steps(m, sel=core.Select(ids=frozenset({"B1", "C1"}))))
    res = d.store.results["checks"]
    assert res["B1#0"]["evidence"] == "no probe script x (p.log)"
    assert res["C1#0"]["evidence"] == "play.py refused: no budget (s.log)"


# ------------------------------------------------------------------------------------------------ run.py offline
def test_cli_plan_record_render(tmp_path, capsys):
    rt, doc = tmp_path / "rt", tmp_path / "STRESS-TEST.md"
    base = ["--runtime", str(rt), "--doc", str(doc)]
    assert cli.main(["--plan", "--only", "C5,G6", *base]) == 0
    out = capsys.readouterr().out
    assert "acc   live_window --only se: G6#0" in out
    assert "agent C5#0 40 min on copy of S043-prison-cleared as SCN-stress_c5" in out and "(subscription)" in out
    assert out.strip().endswith("3 steps")
    assert cli.main(["--plan", "--only", "C5", "--profile", "profiles/deepseek-flash.toml", *base]) == 0
    assert "--max-cost 1," in capsys.readouterr().out
    assert cli.main(["--record", "G6#1", "MAYBE", "x", *base]) == 2
    assert cli.main(["--record", "G6#1", "PASS", "F5 refused", *base]) == 0
    saved = json.loads((rt / "stress" / "results.json").read_text())
    jsonschema.validate(saved, SCHEMA)
    assert saved["checks"]["G6#1"]["verdict"] == "PASS" and saved["checks"]["G6#1"]["by"] == "hand"
    assert "G6#1 PASS: F5 refused" in doc.read_text()
    with pytest.raises(sm.MatrixError):
        cli.main(["--record", "G6#7", "PASS", "x", *base])
    capsys.readouterr()
    assert cli.main(["--plan", "--only", "G6", *base]) == 0
    assert "manual G6#1" not in capsys.readouterr().out  # recorded: done
    doc.unlink()
    assert cli.main(["--render", *base]) == 0 and "# Stress-test matrix" in doc.read_text()
    assert cli.main(["--check", *base]) == 0
