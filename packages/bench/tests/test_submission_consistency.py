# pyright: basic
"""bg3bench.submission.consistency: each check's failure branch on small hand-made bundles (validate.Loaded), the
whole-bundle path being test_submission_validate.py's."""

from __future__ import annotations

from pathlib import Path

from bg3bench import facts, scenario
from bg3bench.submission import consistency as c
from bg3bench.submission import pack, validate


def loaded(
    facts: list[dict] | None = None,
    events: dict[str, list[dict]] | None = None,
    result: dict | None = None,
    rolls: list[dict] | None = None,
    release_scenario: dict | None = None,
    release_condition: dict | None = None,
    manifest: dict | None = None,
    sc: dict | None = None,
) -> validate.Loaded:
    return validate.Loaded(
        Path("."),
        {},
        {},
        manifest or {"condition": "rules"},
        result or {},
        facts or [],
        rolls,
        {"S001.events.jsonl": []} if events is None else events,
        sc or {},
        release_scenario or {},
        release_condition or {},
    )


def mark(t: float, name: str, **detail) -> dict:
    return {"v": 1, "kind": "mark", "t": t, "mark": name} | ({"detail": detail} if detail else {})


def ev(t: float, kind: str, **kw) -> dict:
    return {"v": 1, "t": t, "kind": kind} | kw


def test_fingerprint_needs_a_snapshot_and_x01_a_published_one():
    assert c.fingerprint([mark(0, "run_start")]) is None
    assert c.x01_start_state(loaded()) == (False, "the suite publishes no start-state fingerprint for this scenario")
    ok, detail = c.x01_start_state(loaded(release_scenario={"fingerprint": {"gold": 1}}))
    assert ok is False and "differs" in detail


def test_x01_cannot_check_a_first_snapshot_taken_after_the_agent_started():
    """launch-v1 Sonnet x act3_gold_max: the first snapshot came 15.8 s after run_start, a poll after the agent
    started, and showed 428 gold (it had sold junk) against the published 410. Such a run from the release's start
    save is not checkable (null, never a pass); a snapshot at the start, or a run from another save, is held to it."""
    sha1 = "5630b7441579e2a07cb5d5023a827b3fd0ddb344"
    fp = {"party": [], "gold": 410, "region": None, "area": None, "difficulty": None, "name": None}
    rel = {"fingerprint": fp, "save_sha1": sha1}
    man = {"scenario": {"start_save_sha1": sha1}}

    def x01(snap_t: float, gold: int, manifest: dict = man) -> c.Outcome:
        rows = [mark(1000, "run_start"), ev(1000 + snap_t, "snapshot", gold=gold, party=[])]
        return c.x01_start_state(loaded(rows, release_scenario=rel, manifest=manifest))

    assert x01(15.8, 428) == (None, "not checkable: first snapshot at 15.8 s")
    assert x01(15.8, 410) == (None, "not checkable: first snapshot at 15.8 s")  # never a pass
    assert x01(0.5, 410) == (True, "")  # a snapshot at the start (new runs)
    assert x01(0.5, 428)[0] is False
    other = {"scenario": {"start_save_sha1": "0" * 40}}
    assert x01(15.8, 428, other)[0] is False  # not the release's start save: P01 fails it, X01 still compares
    assert x01(15.8, 410, other) == (True, "")


def test_x01_cannot_check_a_field_the_first_snapshot_read_as_null():
    """launch-v1 Opus x fight_act2_balthazar r2: the first snapshot, 1.26 s after run_start, had area null (read
    before the game had it on load; 61.9 s later it was "Nightsong's Prison") and the rest matched. From the release's
    start save that field is not checkable; a value that differs still rejects, and with no field read X01 is null."""
    sha1 = "da79bf42f2f672b815ae64c2008e80483f2c33f3"
    party = [["Karlach", 6], ["Shadowheart", 6], ["Tav", 6], ["Wyll", 6]]
    fp = {"party": party, "gold": 60, "region": "Shadow-Cursed Lands", "area": "Nightsong's Prison"}
    fp |= {"difficulty": "Balanced", "name": "Tav"}
    rel = {"fingerprint": fp, "save_sha1": sha1}
    man = {"scenario": {"start_save_sha1": sha1}}
    snap = {"party": [{"name": n, "level": lv} for n, lv in party], "gold": 60, "region": "Shadow-Cursed Lands"}

    def x01(manifest: dict = man, **fields) -> c.Outcome:
        rows = [
            mark(1793507314.33, "run_start"),
            mark(1793507314.52, "settings", difficulty="Balanced", avatar_name="Tav"),
            ev(1793507315.59, "snapshot", **(snap | fields)),
        ]
        return c.x01_start_state(loaded(rows, release_scenario=rel, manifest=manifest))

    assert x01(area=None) == (True, "not checkable (null in the first snapshot): area")
    assert x01(area="Nightsong's Prison") == (True, "")  # r1, r3: read at 1.1 s
    assert x01(area="Moonrise Towers") == (False, "first snapshot differs from the published start state in: area")
    assert x01(area=None, gold=61)[1] == "first snapshot differs from the published start state in: gold"
    nothing = {"party": None, "gold": None, "region": None, "area": None}
    assert x01(**nothing) == (None, "not checkable: the first snapshot has no party, gold, region, area")
    other = {"scenario": {"start_save_sha1": "0" * 40}}
    assert x01(other, area=None)[0] is False  # not the release's start save: held to every field


def test_g01_regrades_a_maximise_score_from_the_published_start_state():
    sc = scenario.with_defaults(pack.load_scenarios()["act3_gold_max"])
    state = {"party": [], "inv": [], "combat": False, "region": "Lower City", "areas": [], "quests": {}}
    run = [
        mark(0, "run_start") | {"game_t": 0},
        ev(15.8, "snapshot", game_t=15.8, gold=428, **state),
        ev(2700, "snapshot", game_t=2700, gold=7988, **state),
        mark(2700, "run_end") | {"game_t": 2700},
    ]
    rel = {"fingerprint": {"gold": 410}}
    old = {"fact": "gold", "start": 428, "end": 7988, "gain": 7560, "peak": 7988}  # the first-snapshot grader's
    ok, detail = c.g01_regrade(loaded(run, sc=sc, release_scenario=rel, result={"passed": None, "score": old}))
    assert ok is False and "re-grade score" in detail
    new = {**old, "start": 410, "gain": 7578, "start_from": "start_state"}
    assert c.g01_regrade(loaded(run, sc=sc, release_scenario=rel, result={"passed": None, "score": new})) == (True, "")


def test_x02_time_marks():
    assert c.x02_time_marks(loaded([mark(0, "run_start")]))[1] == "1 run_start and 0 run_end marks (want 1 each)"
    marks = [mark(0, "run_start"), mark(100, "run_end")]
    assert "wall_s 50" in c.x02_time_marks(loaded(marks, result={"wall_s": 50}))[1]
    late = {"S001.events.jsonl": [ev(500, "text")]}
    assert c.x02_time_marks(loaded(marks, late, {"wall_s": 100})) == (False, "1 events outside the run's marks")
    assert c.x02_time_marks(loaded(marks, {"S001.events.jsonl": [ev(50, "text")]}, {"wall_s": 100})) == (True, "")


def test_x02_on_a_new_game_timed_from_its_run_start():
    """bench live S2: a new game's run_start came at its first level's load, ~15 s after the session start that wall_s
    counted from, so X02 failed every new-game run (pi nautiloid: wall_s 2695.2 vs the marks' 2682.9). The harness
    now writes run_start before the kickoff and records wall_min as run_end - run_start (to 0.01 min); replayed on a
    real nautiloid timeline whose agent called a tool before the level loaded, X02 holds."""
    rows = facts.load(Path(__file__).parent / "fixtures" / "timelines" / "nautiloid_sonnet_r2.facts.jsonl")
    level = rows[0]["t"]  # where S2's run_start was: the first level's load
    kickoff, end = level - 15.6, next(r["t"] for r in rows if r.get("mark") == "run_end")
    agent = {"S001.events.jsonl": [ev(kickoff + 2, "tool_call", tool="get_status"), ev(end - 1, "text")]}

    def wall_s(start: float) -> float:
        return round(round((end - start) / 60, 2) * 60, 1)  # the record's wall_min -> result.wall_s (results.py)

    timed = [{**rows[0], "t": kickoff}, *rows[1:]]  # run_start before the kickoff
    assert c.x02_time_marks(loaded(timed, agent, {"wall_s": wall_s(kickoff)})) == (True, "")
    s2 = c.x02_time_marks(loaded(rows, agent, {"wall_s": wall_s(kickoff)}))  # S2: the marks from the level load
    assert s2[0] is False and "wall_s" in s2[1]


def test_x04_rolls():
    assert c.x04_rolls(loaded(result={"luck": None})) == (True, "")
    assert c.x04_rolls(loaded(result={"luck": {"z": 1}})) == (False, "result.luck without rolls.jsonl")
    ok, detail = c.x04_rolls(loaded(rolls=[], result={"luck": {"z": 1}}))
    assert ok is False and "luck recomputed" in detail


def test_x05_tokens_and_cost():
    usage = {"S001.events.jsonl": [ev(1, "usage", tokens={"input": 5})]}
    assert "events' usage" in c.x05_tokens_cost(loaded(events=usage, result={"tokens": {}}))[1]
    assert c.x05_tokens_cost(loaded(result={"tokens": {}, "cost_usd": None}))[0] is True
    assert c.x05_tokens_cost(loaded(result={"tokens": {}, "cost_usd": 1.0}))[0] is False  # claimed, no usage costs
    priced = {"S001.events.jsonl": [ev(1, "usage", cost_usd=1.0), ev(2, "usage")]}
    assert c.process_costs(loaded(events=priced)) == {"S001.events.jsonl": 1.0}
    assert c.x05_tokens_cost(loaded(events=priced, result={"tokens": {}, "cost_usd": 1.0})) == (True, "")


def test_x06_resumes():
    start = ev(0, "session_start", detail={"session": "S001", "conversation": "a"})
    two = {"S001.events.jsonl": [start], "S001-c1.events.jsonl": [start]}
    assert "2 events logs, 2 session starts, 1 distinct" in c.x06_resumes(loaded(events=two))[1]
    one = {"S001.events.jsonl": [start]}
    ok, detail = c.x06_resumes(loaded(events=one, result={"first_stop_s": 50, "wall_s": 10}))
    assert ok is False and "first_stop_s 50" in detail
    assert c.x06_resumes(loaded(events=one, result={"first_stop_s": 5, "wall_s": 10})) == (True, "")


def test_x07_outlier():
    assert c.x07_outlier(loaded(), None)[0] is None
    fast = loaded(result={"passed": True, "t_pass_s": 10})
    assert "under half" in c.x07_outlier(fast, {"t_pass_min": 100})[1]
    rich = loaded(result={"score": {"gain": 500}})
    assert "over twice" in c.x07_outlier(rich, {"gain_max": 100})[1]
    assert c.x07_outlier(rich, {"gain_max": 300, "t_pass_min": None}) == (True, "")


def test_g01_and_g02_on_a_goal_scenario():
    sc = scenario.with_defaults(pack.load_scenarios()["fight_act2_balthazar"])
    run = [mark(0, "run_start") | {"game_t": 0}, mark(10, "run_end") | {"game_t": 10}]
    b = loaded(run, sc=sc, result={"passed": True, "t_pass_s": 1.0, "grade": {"violations": []}})
    ok, detail = c.g01_regrade(b)
    assert ok is False and "re-grade (passed, t_pass_s, violations)" in detail
    zero = {"fights": 0, "downs": 0, "deaths": 0, "wipes": 0}
    reload = loaded([mark(0, "load"), mark(5, "load")], result={"deaths": zero, "reloads": 0})
    assert "reloads 0 != the facts' 1" in c.g02_losses(reload)[1]


def test_a01_fairness_audit():
    b = loaded(result={"fairness": {"leak": 0, "check": 0, "blocks": 0}})
    assert c.a01_fairness(b, lambda paths: {"leak": 2, "check": 0, "blocks": 0}) == (
        False,
        "2 leaks in the tool results",
    )
    assert "audit" in c.a01_fairness(b, lambda paths: {"leak": 0, "check": 1, "blocks": 0})[1]
    assert c.a01_fairness(b, lambda paths: {"leak": 0, "check": 0, "blocks": 0}) == (True, "")


def test_a02_allowlist():
    assert c.a02_allowlist(loaded()) == (False, "the suite publishes no tool list for this condition")
    calls = {"S001.events.jsonl": [ev(1, "tool_call", tool="load_game")]}
    tools = {"tools": {"names": ["load_game"]}}
    assert c.a02_allowlist(loaded(events=calls, release_condition=tools)) == (
        False,
        "load_game called without a reloads condition",
    )
    reloads = loaded(events=calls, release_condition=tools, manifest={"condition": "rules+reloads:2"})
    assert c.a02_allowlist(reloads) == (True, "")


def test_a02_a_call_the_client_refused_as_unknown_used_no_tool():
    """bench live S2 pi nautiloid: the model called two tools that don't exist and pi answered "Tool ... not found";
    A02 rejected the run for tools outside the allowlist. Claude Code's answer (live9) and a tool the condition
    turned off count the same; a served tool outside the list, or an unknown-tool text from a tool, still rejects."""
    tools = {"tools": {"names": ["get_status", "load_game"]}}

    def run(*evs: dict) -> c.Outcome:
        return c.a02_allowlist(loaded(events={"S001.events.jsonl": list(evs)}, release_condition=tools))

    def call(cid: str, tool: str) -> dict:
        return ev(1, "tool_call", tool=tool, call_id=cid)

    def result(cid: str, text: str, is_error: bool = True) -> dict:
        return ev(1, "tool_result", call_id=cid, text=text, is_error=is_error)

    assert run(call("a", "mcp__screenshot"), result("a", "Tool mcp__screenshot not found")) == (True, "")
    pi_bg3 = (call("b", "spring_tool_placeholder"), result("b", "Tool mcp__bg3__spring_tool_placeholder not found"))
    assert run(*pi_bg3) == (True, "")
    no_such = "<tool_use_error>Error: No such tool available: mcp__bg3__load_game</tool_use_error>"
    assert run(call("c", "load_game"), result("c", no_such)) == (True, "")  # turned off by the rules: not served
    assert run(call("d", "teleport"), result("d", "teleported")) == (False, "tools outside the allowlist: teleport")
    assert run(call("e", "teleport"), result("e", "Tool teleport not found", is_error=False))[0] is False
    assert run(call("f", "teleport"), result("f", "Tool mcp__bg3__other not found"))[0] is False  # another name
    assert run(call("g", "teleport"), result("h", "Tool teleport not found"))[0] is False  # another call's result
    assert run(call("i", "load_game"), result("i", "loaded"))[1] == "load_game called without a reloads condition"
    # launch-v1 Haiku x choice_act1_minthara r2: a garbled name with a space, refused by pi
    garbled = "mcp__bg3] choose_dialogue"
    real = (
        call("toolu_01PTDjNpaiHVwyUNYrWoPC1z", garbled),
        result("toolu_01PTDjNpaiHVwyUNYrWoPC1z", f"Tool {garbled} not found"),
    )
    assert run(*real) == (True, "")
    assert run(
        call("j", garbled), result("j", f"<tool_use_error>Error: No such tool available: {garbled}</tool_use_error>")
    ) == (True, "")
    assert run(call("k", garbled), result("k", "Tool mcp__bg3] other not found"))[0] is False  # another name
