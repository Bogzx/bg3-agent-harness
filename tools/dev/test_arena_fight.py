"""Offline tests for tools/dev/arena_fight.py on a small hand-made run folder (two phases, one call each side in each).
Run: uv run pytest -q tools/dev/test_arena_fight.py"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import arena_fight as af

T0 = 1_000_000.0


def _w(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def ev(t: float, kind: str, **kw) -> dict:
    return {"v": 1, "seq": 0, "t": T0 + t, "src": "client", "kind": kind, **kw}


def fact(t: float, name: str, **kw) -> dict:
    return {"v": 1, "kind": "event", "t": T0 + t, "game_t": t, "ev": name, **kw}


def run_dir(tmp: Path) -> Path:
    teams = {"A": ["Ash"], "B": ["Birch"]}
    _w(
        tmp / "facts.jsonl",
        [
            {"v": 1, "kind": "mark", "t": T0 - 10, "game_t": 0.0, "mark": "run_start"},
            fact(
                0,
                "arena_start",
                arena="x",
                seed=1,
                teams=teams,
                sides={"A": "pi/a", "B": "pi/b"},
                uuids={"A": ["ua"], "B": ["ub"]},
            ),
            fact(1, "arena_turn", side="A", character="Ash", members=["Ash"], round=1, phase=1),
            fact(40, "arena_turn_forced", side="A", character="Ash"),
            fact(41, "arena_turn", side="B", character="Birch", members=["Birch"], round=1, phase=2),
            fact(60, "arena_turn_passed", side="B", character="Birch", why="downed"),
            fact(61, "arena_end", winner="A", reason="wipe", rounds=1, hp_left_A=1.0, hp_left_B=0.0),
        ],
    )
    _w(
        tmp / "A.events.jsonl",
        [
            ev(0, "init", session_id="sess-aaaa"),
            ev(1.5, "thinking", text="I will attack Birch with my longsword right now because it is close enough."),
            ev(2, "tool_call", tool="get_combat_state", args={}, call_id="a1"),
            ev(2.1, "tool_result", call_id="a1", text="e1 Ash party hp 100% current can_act\ne2 Birch hostile hp 50%"),
            ev(2.2, "tool_call", tool="get_combat_state", args={}, call_id="a1b"),
            ev(2.3, "tool_result", call_id="a1b", text="e1 Ash party hp 100% current\ne2 Birch hostile hp 50%"),
            ev(32.3, "tool_call", tool="use_ability", args={"ability": "Attack"}, call_id="a2"),
            ev(34, "tool_result", call_id="a2", text="done: hit"),
            ev(45, "tool_call", tool="use_ability", args={"ability": "Attack"}, call_id="a3"),
            ev(45.5, "tool_result", call_id="a3", text="use_ability: FAILED — not your turn (side B is acting)"),
        ],
    )
    _w(
        tmp / "B.events.jsonl",
        [
            ev(0, "init", session_id="sess-bbbb"),
            ev(42, "tool_call", tool="get_combat_state", args={}, call_id="b1"),
            ev(
                42.2,
                "tool_result",
                call_id="b1",
                text="e7 Birch party hp 0% downed current\ne8 Ash hostile hp 100% current; note sess-aaaa",
            ),
        ],
    )
    _w(
        tmp / "rolls.jsonl",
        [
            {"roller": "ua", "natural": 15},
            {"roller": "ub", "natural": 3, "advantage": True},
            {"roller": "zz", "natural": 9},
            {
                "roller": "ua",
                "event": "attack",
                "spell": "Target_MainHandAttack",
                "natural": None,
                "cast_roll": {"natural": 12, "total": 17, "dc": 17, "advantage": True},
            },
            {
                "roller": "ub",
                "event": "attack",
                "spell": "Projectile_MainHandAttack",
                "natural": None,
                "cast_roll": {"natural": 20, "total": 25, "dc": 30, "critical": "Success"},
            },
            {
                "roller": "ub",
                "event": "attack",
                "spell": "Projectile_MainHandAttack",
                "natural": None,
                "cast_roll": {"natural": 1, "total": 6, "dc": 3},
            },
        ],
    )
    (tmp / "arena.json").write_text(
        json.dumps(
            {"arena": {"turn_cap_s": 20}, "invalid": None, "sides": {"A": {"cost_usd": 0.01}, "B": {"cost_usd": 0.02}}}
        )
    )
    return tmp


def test_calls_land_in_the_phase_open_at_their_time_with_latency_and_model_time(tmp_path):
    r = af.analyse(run_dir(tmp_path))
    p1, p2 = r["phases"]
    assert [c["tool"] for c in p1["calls"]["A"]] == ["get_combat_state", "get_combat_state", "use_ability"]
    assert p1["calls"]["A"][2]["ms"] == 1700 and p1["calls"]["A"][2]["think_s"] == 30.0
    assert p1["forced"] == ["Ash"] and p1["secs"] == 40.0
    assert p1["model_s"] == 32.1 and p1["over_cap"] is True  # 2 + 0.1 + 30 > the 20 s cap of one member
    assert p2["passed"] == ["Birch (downed)"]


def test_an_action_in_the_other_sides_phase_and_a_refusal_are_counted(tmp_path):
    r = af.analyse(run_dir(tmp_path))
    a = r["per_side"]["A"]
    assert a["off_turn_actions"] == 1 and len(a["refusals"]) == 1 and "not your turn" in a["refusals"][0]["text"]
    assert [c["tool"] for c in r["phases"][1]["off_turn_actions"]] == ["use_ability"]
    assert a["repeated_reads"] == 1  # the same get_combat_state twice with no action between
    assert a["cost"] == 0.01


def test_the_view_check_and_the_leak_scan(tmp_path):
    r = af.analyse(run_dir(tmp_path))
    assert {"round": 1, "phase": 1, "character": "Ash", "A_only": ["can_act", "current"], "B_only": []} in r["views"]
    assert {"side": "B", "what": "the other side's session id sess-aaaa"} in r["leaks"]
    assert not [x for x in r["leaks"] if x["side"] == "A"]


def test_rolls_count_each_sides_plain_d20_faces(tmp_path):
    r = af.analyse(run_dir(tmp_path))
    assert r["rolls"] == {"A": [15], "B": []} and r["rolls_other"] == 1


def test_main_writes_markdown_and_json_and_refuses_a_folder_without_facts(tmp_path, capsys):
    run = run_dir(tmp_path / "run") if (tmp_path / "run").mkdir() is None else None
    assert run is not None
    assert af.main([str(run), "--json", str(tmp_path / "r.json")]) == 0
    out = capsys.readouterr().out
    assert "r1 p1 A ['Ash']" in out and "**REFUSED**" in out and "**over cap**" in out
    assert json.loads((tmp_path / "r.json").read_text())["end"]["winner"] == "A"
    assert af.main([str(tmp_path / "missing")]) == 2


def test_refusals_follow_the_bridges_failure_rule(tmp_path):
    """bp-app's cross-check (arena-video.md): a failed ability check and another member's failure in a result's
    EVENTS block are not refusals; a batch's step that failed after 400+ characters of done steps is one."""
    run = run_dir(tmp_path)
    steps = "\n".join(f"{i}. move_to: done, walked to the spot by the crates {'x' * 60}" for i in range(1, 6))
    extra = [
        ev(50, "tool_call", tool="roll", args={}, call_id="a4"),
        ev(50.1, "tool_result", call_id="a4", text="roll: Persuasion (Charisma Check) DC 16: d20 4 + 7 = 11 → FAILED"),
        ev(51, "tool_call", tool="move_to", args={}, call_id="a5"),
        ev(
            51.1,
            "tool_result",
            call_id="a5",
            text="move_to: done\nEVENTS\n  queue e5 Lae'zel: STOPPED: step 2 loot failed\n  2. loot: FAILED — gone",
        ),
        ev(52, "tool_call", tool="do_actions", args={}, call_id="a6"),
        ev(
            52.1,
            "tool_result",
            call_id="a6",
            text=f"do_actions: done\n{steps}\n6. use_ability: FAILED — not your turn (side B is acting)",
        ),
    ]
    with (run / "A.events.jsonl").open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for r in extra))
    r = af.analyse(run)
    assert len(steps) > 400
    refused = r["per_side"]["A"]["refusals"]
    assert [x["tool"] for x in refused] == ["use_ability", "do_actions"]
    assert refused[1]["text"] == "6. use_ability: FAILED — not your turn (side B is acting)"
    assert "6. use_ability: FAILED" in af.markdown(r)


def test_forced_passed_and_stall_records_before_the_first_phase_are_kept(tmp_path):
    run = run_dir(tmp_path)
    rows = [json.loads(x) for x in (run / "facts.jsonl").read_text().splitlines()]
    early = [
        fact(0.5, "arena_turn_passed", side="B", character="Birch", why="zero_hp"),
        fact(0.6, "arena_turn_forced", side="A", character="Ash"),
        fact(0.7, "arena_stall", stall="game"),
    ]
    _w(run / "facts.jsonl", rows[:1] + early + rows[1:])
    r = af.analyse(run)
    assert r["before_first_phase"] == {"forced": ["Ash"], "passed": ["Birch (zero_hp)"], "stall": "game"}
    assert r["phases"][0]["forced"] == ["Ash"] and r["phases"][1]["passed"] == ["Birch (downed)"]
    assert "before the first phase: forced ['Ash'], passed ['Birch (zero_hp)'], stall game" in af.markdown(r)


def test_live_plays_manifest_names_the_arena_and_keeps_costs_apart(tmp_path):
    """live.play's arena.json: `arena` is the arena's name and the costs are under `costs` (smoke 2026-11-03)."""
    run = run_dir(tmp_path)
    (run / "arena.json").write_text(json.dumps({"arena": "arena-v0", "invalid": None, "costs": {"A": 0.5, "B": 0.25}}))
    r = af.analyse(run, 20)
    assert r["per_side"]["A"]["cost"] == 0.5 and r["phases"][0]["over_cap"] is True
    assert af.analyse(run)["phases"][0]["over_cap"] is False  # no cap known without --turn-cap


def test_status_path_and_route_reads_are_not_actions():
    """smoke 2026-11-03: get_status in the other side's phase was counted as an action sent out of turn."""
    assert {"get_status", "path_preview", "route", "look"} <= af.READS  # look: seed-5 m1


def test_attacks_read_the_cast_roll_hit_on_total_vs_ac_crit_and_nat1(tmp_path):
    r = af.analyse(run_dir(tmp_path))
    assert [(x["natural"], x["adv"], x["hit"]) for x in r["attacks"]["A"]] == [(12, True, True)]
    assert [(x["natural"], x["hit"]) for x in r["attacks"]["B"]] == [(20, True), (1, False)]
    md = af.markdown(r)
    assert "- A: 1 hits in 1: Target_MainHandAttack 12a/17 v 17 hit" in md and "- B: 1 hits in 2" in md


def test_phases_and_calls_share_the_run_start_clock(tmp_path):
    # W5-1 (seed-3 m1): a phase time read from the first turn was set against call times from the run start
    run = run_dir(tmp_path)
    r = af.analyse(run)
    assert [p["at"] for p in r["phases"]] == [11.0, 51.0]
    refused = next(c for p in r["phases"] for c in p["calls"]["A"] if c["error"])
    assert refused["at"] == 55.0
    md = af.markdown(r)
    assert "@11.0 s" in md and "@55.0 s" in md


def opener_run(tmp: Path) -> Path:
    """Two members a side; A's calls name Ash by name and Cedar by handle (e2, seat run r12); B's Dune never gets a
    phase (downed first)."""
    teams = {"A": ["Ash", "Cedar"], "B": ["Birch", "Dune"]}
    _w(
        tmp / "facts.jsonl",
        [
            {"v": 1, "kind": "mark", "t": T0 - 10, "game_t": 0.0, "mark": "run_start"},
            fact(0, "arena_start", arena="x", seed=1, teams=teams, sides={"A": "pi/a", "B": "pi/b"}, uuids={}),
            fact(1, "arena_turn", side="A", members=["Ash"], round=1, phase=1),
            fact(20, "arena_turn", side="B", members=["Birch"], round=1, phase=2),
            fact(30, "arena_turn", side="A", members=["Cedar"], round=1, phase=3),
            fact(40, "arena_turn", side="A", members=["Ash"], round=2, phase=1),
            fact(50, "arena_end", winner="A", reason="wipe", rounds=2),
        ],
    )

    def use(t: float, cid: str, who: str | None, text: str, side_text: str = "") -> list[dict]:
        args = {"ability": "x"} | ({"who": who} if who else {})
        return [
            ev(t, "tool_call", tool="use_ability", args=args, call_id=cid),
            ev(t + 0.1, "tool_result", call_id=cid, text=text + side_text),
        ]

    _w(
        tmp / "A.events.jsonl",
        [
            ev(0, "init", session_id="sa"),
            ev(1.5, "tool_call", tool="get_party", args={}, call_id="p"),
            ev(1.6, "tool_result", call_id="p", text="e1 Ash hp 20/20\ne2 Cedar hp 17/17"),
            *use(2, "a1", "Ash", "use_ability: done — cast=Rush_SpringAttack damage=[e15 Birch 4]"),
            *use(
                3,
                "a2",
                "Ash",
                "use_ability: done — cast=Target_PommelStrike",
                "\nEVENTS\n  e2 Cedar cast=Target_OpeningAttack",
            ),
            *use(4, "a3", "Ash", "use_ability: FAILED — cast=Target_PommelStrike on cooldown"),
            *use(31, "a4", "e2", "use_ability: done — cast=Shout_Hide_BonusAction"),
            *use(32, "a5", None, "use_ability: done — cast=Projectile_SneakAttack"),
            *use(41, "a6", "e1", "use_ability: done — cast=Target_PommelStrike"),
        ],
    )
    _w(tmp / "B.events.jsonl", [ev(0, "init", session_id="sb")])
    return tmp


def test_openers_count_each_characters_first_turn_by_name_handle_or_lone_member(tmp_path):
    o = af.analyse(opener_run(tmp_path))["openers"]
    ft = {f["character"]: f for f in o["first_turns"]}
    assert list(ft) == ["Ash", "Birch", "Cedar"]
    # the EVENTS block's Opening Attack is Cedar's background line, not Ash's cast; the refused Pommel cast nothing
    assert ft["Ash"]["casts"] == ["Rush_SpringAttack", "Target_PommelStrike"]
    assert ft["Ash"]["use"] == ["Rush", "Pommel Strike"] and ft["Ash"]["round"] == 1
    assert ft["Birch"]["use"] == ["none"]
    assert ft["Cedar"]["casts"] == ["Shout_Hide_BonusAction", "Projectile_SneakAttack"]  # e2, then no `who`
    assert ft["Cedar"]["use"] == ["other"]
    assert o["all"] == {"Rush": [[1, "Ash"]], "Opening Attack": [], "Pommel Strike": [[1, "Ash"], [2, "Ash"]]}
    assert o["no_turn"] == [["B", "Dune"]]
    assert o["unknown"] == []


def test_openers_put_a_call_naming_no_member_of_its_phase_under_unknown():
    # REVIEW-18 §6a: a call with no `who` in a phase of two members (group turns) mapped to "None" and its casts
    # were dropped, so the Pommel Strike below read as nobody's
    ps = [{"side": "A", "members": ["Ash", "Cedar"], "round": 1, "phase": 1, "passed": []}]

    def call(who: str | None, text: str) -> dict:
        return {"phase": 0, "error": False, "args": {"who": who} if who else {}, "text": text}

    cs = {
        "A": [
            call("Ash", "use_ability: done — cast=Rush_SpringAttack"),
            call(None, "use_ability: done — cast=Target_PommelStrike"),
            call("e7", "use_ability: done — cast=Shout_Dash"),  # a handle no result paired
            call(None, "get_combat_state: ok"),  # no cast: nothing to count
        ]
    }
    o = af.openers(ps, cs, {"A": ["Ash", "Cedar"], "B": []})
    assert [f["use"] for f in o["first_turns"]] == [["Rush"], ["none"]]
    assert o["unknown"] == [
        {"side": "A", "round": 1, "phase": 1, "casts": ["Target_PommelStrike"]},
        {"side": "A", "round": 1, "phase": 1, "casts": ["Shout_Dash"]},
    ]
    assert o["all"]["Pommel Strike"] == [[1, "unknown"]] and o["all"]["Rush"] == [[1, "Ash"]]


def test_own_casts_skip_the_events_block_and_handles_take_the_most_seen_pair():
    assert af.own_casts("x: done — cast=A\nEVENTS\n  e2 cast=B\nNOW cast=C") == ["A", "C"]
    cs = [{"text": "e1 Ash\ne1 Ash\ne1 Cedar\ne2 Cedar\ne9 Birch"}]
    assert af.handles(cs, ["Ash", "Cedar"]) == {"e1": "Ash", "e2": "Cedar"}


def test_main_tallies_openers_over_several_runs_and_needs_the_flag_for_them(tmp_path, capsys):
    runs = [opener_run(tmp_path / n) for n in ("r1", "r2") if (tmp_path / n).mkdir() is None]
    assert af.main([*map(str, runs), "--openers", "--json", str(tmp_path / "o.json")]) == 0
    out = capsys.readouterr().out
    assert "| r1 | 1 | 1 | A | Ash | Rush, Pommel Strike | Rush_SpringAttack Target_PommelStrike |" in out
    assert "First turns: 6 in 2 runs; 2 characters downed before their first turn." in out
    assert "- Rush: 2/6" in out and "- Pommel Strike: 2/6" in out and "- none: 2/6" in out and "- other: 2/6" in out
    assert "- Pommel Strike: 4 ({1: 2, 2: 2})" in out
    assert "- calls with casts and no known caster (not in the first turns above): 0" in out
    assert len(json.loads((tmp_path / "o.json").read_text())) == 2
    with pytest.raises(SystemExit):
        af.main([*map(str, runs)])
    assert "Openers (each character's first turn" in af.markdown(af.analyse(runs[0]))
