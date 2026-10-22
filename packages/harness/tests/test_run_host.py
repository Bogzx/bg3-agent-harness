"""bg3harness.runner.run_host: the bench's live RunHost with a fake runner (no game, no client process).

A save start and a new-game start become their Options and kickoff; start resets the run (saves from now, the mod's
nav files moved into the run dir), puts the game at the start in order and hands back the runner's session hooks;
record ends facts.jsonl, stops the game and returns the run's fields with the outcome the harness can tell."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from bg3harness import client_setup
from bg3harness.runner import env as renv
from bg3harness.runner import run_host as H  # noqa: N812
from bg3harness.runner import state as rstate
from bg3harness.session import SessionOutcome

SAVE_SC: dict[str, Any] = {
    "name": "grove_fight",
    "title": "The grove gate fight",
    "start": {
        "kind": "save",
        "save": "Lae'zel-12__SCN-grove_fight",
        "note": {"where": "The grove gate", "objective": "Win the fight at the gate"},
    },
    "goal": {"hostiles_dead": {"area": "gate"}},
    "caps": {"run_hours": 0.5, "max_cost": 2.0, "max_tokens": None},
    "game_settings": {"difficulty": "Balanced", "karmic_dice": False, "seed": 7},
    "timing": "turn_based",
    "profile": {"character": {"name": "Lae'zel", "brief": "A githyanki warrior.", "roleplay": "light"}},
    "reset": {"nav_state": "archive"},
}
NEW_SC: dict[str, Any] = {
    **SAVE_SC,
    "name": "sell_idol",
    "start": {"kind": "new_game"},
    "profile": {"character": {"brief": "A careful trader.", "roleplay": "strict"}},
}
JOB = {"model": "opus", "client": "claude", "effort": "high", "condition": "rules", "max_cost": None}


class State(dict[str, Any]):
    def __init__(self, d: Path):
        super().__init__(history=[], loaded_save=None)
        self.dir = d


class FakeRunner:
    """The SessionRunner calls the host makes, recorded in order."""

    def __init__(self, opt: rstate.Options, calls: list[str]):
        self.opt, self.calls = opt, calls
        self.state = State(renv.RUNTIME / "sessions" / opt.playthrough)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.cap_hit: dict[str, Any] | None = None
        self.mod_rev: str | None = None
        self.run_started: float | None = None
        self.settles_budget = True
        self.game_settings = {"difficulty": "Balanced"}
        self.facts_fail = False

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))

    def refresh_mod_snapshot(self) -> list[str]:
        self.calls.append("snapshot")
        return []

    def ensure_game(self, first: bool) -> str | None:
        self.calls.append(f"ensure_game({first})")
        self.state["loaded_save"] = self.opt.start_save
        return "SCN-grove_fight" if self.opt.start_save else None

    def hot_reload_mod(self) -> int:
        self.calls.append("reload")
        return 0

    def apply_play_settings(self) -> None:
        self.calls.append("settings")

    def check_settings(self) -> None:
        self.calls.append("check_settings")

    def end_facts(self, outcome: str) -> None:
        self.calls.append(f"end_facts({outcome})")
        if self.facts_fail:
            raise RuntimeError("the game stopped answering")

    def end_run_game(self, when: str = "", after: str = "") -> None:
        self.calls.append(f"end_run_game({when})")

    def session_hooks(self, sid: str, minutes: float) -> rstate.RunnerSessionHooks:
        return rstate.RunnerSessionHooks(self, sid, minutes)  # pyright: ignore[reportArgumentType]


@dataclass
class Rig:
    h: H.LiveRunHost
    calls: list[str]
    runners: list[FakeRunner]
    dumped: list[tuple[Path, str, int | None, str | None]]
    env: dict[str, str]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setattr(renv, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(renv, "SE_BG3AI_DIR", tmp_path / "se" / "BG3AI")
    monkeypatch.setattr(renv, "log", lambda msg: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    renv.SE_BG3AI_DIR.mkdir(parents=True)
    for name in ("nav_guard_aa.json", "nav_aa.json", "other.json"):
        (renv.SE_BG3AI_DIR / name).write_text("{}")
    rig = Rig(H.LiveRunHost(lambda opt: FakeRunner(opt, [])), [], [], [], {})

    def make(opt: rstate.Options) -> FakeRunner:
        rig.runners.append(FakeRunner(opt, rig.calls))
        return rig.runners[-1]

    def run_files(d: Path, client: str, seed: int | None, timing: str | None) -> dict[str, Any]:
        rig.dumped.append((d, client, seed, timing))
        return {"tools_expected": 90, "manifest": {"m": 1}}

    rig.h = H.LiveRunHost(make, run_files=run_files, env=rig.env, clock=lambda: 1000.0)
    return rig


def run_dir(name: str) -> Path:
    return renv.RUNTIME / "sessions" / name


def test_options_of_a_save_start_and_a_new_game():
    opt = H.options_for(JOB, SAVE_SC, "m-grove", {"tools_expected": 90})
    assert (opt.playthrough, opt.session_minutes, opt.run_hours, opt.hours) == ("m-grove", 30.0, 0.5, None)
    assert (opt.start_save, opt.save_owner, opt.scenario, opt.seed) == (
        SAVE_SC["start"]["save"],
        "Lae'zel",
        "grove_fight",
        7,
    )
    assert (opt.keeper, opt.judge, opt.max_cost, opt.tools_expected) == (False, False, 2.0, 90)
    assert opt.scenario_settings == {"difficulty": "Balanced", "karmic_dice": False, "timing": "turn_based"}
    new = H.options_for({**JOB, "max_cost": 0.5}, NEW_SC, "m-idol", {})
    assert (new.start_save, new.save_owner, new.max_cost, new.character_brief) == (
        None,
        "Tav",
        0.5,
        "A careful trader.",
    )


def test_the_kickoff_names_the_start_the_goal_and_the_character_and_no_memory_steps():
    text = H.scenario_kickoff(SAVE_SC, "S001", "SCN-grove_fight")
    assert 'loaded save "SCN-grove_fight"' in text and "Never start a new game" in text
    assert "Objective: Win the fight at the gate" in text and "Where: The grove gate" in text
    assert json.dumps(SAVE_SC["goal"], sort_keys=True) in text and "A githyanki warrior." in text
    for playthrough_only in ("session_start", "playbook", "memory_", "HANDOFF"):
        assert playthrough_only not in text
    new = H.scenario_kickoff(NEW_SC, "S001", None, pace="PACE: two actions per turn")
    assert 'Name your character exactly "Tav"' in new and new.endswith("PACE: two actions per turn")
    assert "PACE" not in H.scenario_kickoff(NEW_SC, "S001", None)


def test_start_resets_the_run_and_puts_the_game_at_the_scenario_start(rig: Rig):
    h, calls, runners, dumped, env = rig.h, rig.calls, rig.runners, rig.dumped, rig.env
    st = h.start(JOB, SAVE_SC, run_dir("m-grove"))
    (r,) = runners
    assert calls == ["snapshot", "ensure_game(True)", "reload", "settings", "check_settings"]
    assert (r.state["saves_after"], r.run_started) == (1000.0, 1000.0)
    assert not r.settles_budget  # the matrix adds the run's cost to budget.json, not the runner
    assert sorted(p.name for p in (run_dir("m-grove") / "reset").iterdir()) == ["nav_aa.json", "nav_guard_aa.json"]
    assert [p.name for p in renv.SE_BG3AI_DIR.iterdir()] == ["other.json"]
    assert r.events == [("run_reset", {"nav_files": ["nav_aa.json", "nav_guard_aa.json"]})]
    assert env["BG3AI_PROMPT_CONDITION"] == "rules" and (r.opt.tools_expected, r.opt.manifest) == (90, {"m": 1})
    assert dumped == [(run_dir("m-grove"), "claude", 7, "turn_based")]
    assert st.prompt == H.scenario_kickoff(SAVE_SC, "S001", "SCN-grove_fight")  # rules: no pace pre-flight
    assert st.prompt_sha == hashlib.sha256(st.prompt.encode()).hexdigest() and st.harness_version
    assert isinstance(st.hooks, rstate.RunnerSessionHooks) and (st.hooks.sid, st.hooks.minutes) == ("S001", 30.0)


def test_a_coaching_condition_gets_the_pace_preflight(rig: Rig):
    h = rig.h
    st = h.start({**JOB, "condition": "rules+coaching"}, NEW_SC, run_dir("m-idol"))
    pace = client_setup.kickoff_pace("claude", {"BG3AI_PROMPT_CONDITION": "rules+coaching"})
    assert pace and st.prompt.endswith(pace)


def test_a_run_dir_the_runner_doesnt_write_to_is_refused(rig: Rig, tmp_path: Path):
    h, calls = rig.h, rig.calls
    with pytest.raises(ValueError, match="runtime/sessions"):
        h.start(JOB, SAVE_SC, tmp_path / "elsewhere" / "m-grove")
    assert calls == []


def outcome(invalid: str | None = None) -> SessionOutcome:
    res = rstate.SessionResult("S001", "c-1", 0, "exited", 1000.0, 1000.0 + 25 * 60)
    return SessionOutcome(res, 1000.0, invalid=invalid)


def test_record_ends_the_facts_stops_the_game_and_returns_the_run(rig: Rig):
    h, calls = rig.h, rig.calls
    h.start(JOB, SAVE_SC, run_dir("m-grove"))
    calls.clear()
    rec = h.record(JOB, SAVE_SC, run_dir("m-grove"), outcome())
    assert calls == ["end_facts(timeout)", "end_run_game(run end)"]
    assert (rec["outcome"], rec["reason"], rec["wall_min"], rec["sessions"]) == ("timeout", "exited", 25.0, 1)
    assert (rec["cost_usd"], rec["reset"]) == (0.0, {"nav_files": ["nav_aa.json", "nav_guard_aa.json"]})
    assert (rec["game_settings"], rec["manifest"], rec["loaded_save"]) == (
        {"difficulty": "Balanced"},
        {"m": 1},
        SAVE_SC["start"]["save"],
    )


def test_record_outcomes_and_the_game_stops_even_when_the_facts_fail(rig: Rig):
    h, calls, runners = rig.h, rig.calls, rig.runners
    h.start(JOB, SAVE_SC, run_dir("a"))
    assert h.record(JOB, SAVE_SC, run_dir("a"), outcome(invalid="crash"))["outcome"] == "gave_up"
    h.start(JOB, SAVE_SC, run_dir("b"))
    runners[-1].cap_hit = {"cap": "cost"}
    assert h.record(JOB, SAVE_SC, run_dir("b"), outcome())["outcome"] == "over_budget"
    h.start(JOB, SAVE_SC, run_dir("c"))
    runners[-1].facts_fail = True
    calls.clear()
    with pytest.raises(RuntimeError):
        h.record(JOB, SAVE_SC, run_dir("c"), outcome())
    assert calls == ["end_facts(timeout)", "end_run_game(run end)"]


def test_archive_moves_only_the_nav_files(tmp_path: Path):
    se = tmp_path / "se"
    se.mkdir()
    for name in ("nav_guard.json", "nav_x.json", "keep.json"):
        (se / name).write_text("{}")
    assert H.archive_nav_state(se, tmp_path / "dest") == ["nav_guard.json", "nav_x.json"]
    assert [p.name for p in se.iterdir()] == ["keep.json"]
    assert H.archive_nav_state(tmp_path / "missing", tmp_path / "dest") == []
