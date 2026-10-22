"""bg3play.runner's pure parts (moved from runner/legacy_playthrough.py and bg3harness.runner.policy): the kickoff
and the playthrough notes, the memory session's clock env and session.py answers, the keeper's refused lines. The
mixins (memory_side, keeper_side, session_side, loop) run through runner/play.py's Runner in runner/test_*.py."""

import json

from bg3harness.runner import env as renv
from bg3harness.runner import profile
from bg3play import config
from bg3play.runner import keeper_side, memory_session, prompts, session_side
from bg3play.runner.playthrough import PlaythroughSide
from bg3play.supervisor import notes_for


def test_build_kickoff_leads_with_the_briefing_and_names_no_deadline():
    k = prompts.build_kickoff("S004", "pt1", "S003-end", ["NOTE A"], "BRIEFING", "the newest own save", pace="")
    parts = k.split("\n\n")
    assert parts[0] == "BRIEFING" and parts[1].startswith("This is session S004 of the BG3 playthrough 'pt1'")
    assert 'already loaded save "S003-end" (the newest own save)' in k and parts[-1] == "NOTE A"
    assert "minutes" not in k and "PACE PRE-FLIGHT" not in k
    assert "First call session_start" in prompts.build_kickoff("S001", "pt1", None, [])


def test_scenario_start_note_and_fix_notes():
    prof = {"character": {"brief": "a gnome\nbard", "name": "Ilse", "roleplay": "light", "constraints": "no theft"}}
    note = prompts.scenario_start_note("beach", prof, None)
    assert note.startswith("RUNNER NOTE: SCENARIO START (beach)") and '"the start save"' in note
    assert "(Ilse)" in note and "> a gnome\n> bard" in note and "Constraints: no theft" in note
    assert note.endswith(profile.ROLEPLAY_TEXT["light"])
    fix = prompts.fix_notes_prompt("x" * 7000)
    assert f"You have {renv.FIX_SESSION_MINUTES} minutes" in fix and fix.endswith("x" * 6000) and "x" * 6001 not in fix


def test_stuck_directive_names_the_region_not_the_level():
    sig = {"level": "WLD_Main_A", "region": "Wilderness", "subregion": "Roadside Cliffs"}
    d = prompts.stuck_directive([{"end_sig": sig}, {"progress": False}], n=2)
    assert "last 2 sessions" in d and "region=Wilderness, subregion=Roadside Cliffs" in d and "WLD_Main_A" not in d
    assert "no far move: n/a" in prompts.stuck_directive([])


def test_the_supervisor_nudges_with_the_runners_stuck_directive():
    hist = [
        {"session": "S001", "outcome": "ok", "progress": False},
        {"session": "S002", "outcome": "ok", "progress": False},
    ]
    assert notes_for(hist) == (prompts.stuck_directive(hist),)
    assert notes_for(hist[:1]) == ()


def test_clock_env():
    env = memory_session.clock_env(10_000.0, 30.0, keeper=True)
    assert env == {
        "BG3AI_SESSION_DEADLINE": "10000",
        "BG3AI_SESSION_WRAPUP": str(10_000 - 3 * 60),
        "BG3_SESSION_MINUTES": "30.0",
        "BG3_WRAP_MINUTES": "27.0",
        "BG3AI_KEEPER": "1",
    }
    assert memory_session.clock_env(10_000.0, 6.0, keeper=False)["BG3AI_SESSION_WRAPUP"] == str(10_000 - 2 * 60)


def test_session_py_answers():
    block = json.dumps({"decision": "block", "reason": "Write HANDOFF.md first.", "systemMessage": "lint: 1"})
    assert memory_session.stop_reason(block) == ("Write HANDOFF.md first.", "lint: 1")
    assert memory_session.stop_reason("") == (None, None)
    assert memory_session.stop_reason("[1]") == (None, None)
    assert memory_session.stop_reason(json.dumps({"reason": "not a block"})) == (None, None)
    assert memory_session.opened_sid(0, {"open": True, "current": 7}) == "S007"
    assert memory_session.opened_sid(1, {"open": True, "current": 7}) is None
    assert memory_session.opened_sid(0, {"open": False, "current": 7}) is None
    assert memory_session.parse_handoff_session("<!-- # Handoff S001 → S002 -->\n# Handoff S012 → S013\n") == 12
    assert memory_session.parse_handoff_session("no heading") is None


def test_the_keeper_gets_each_refused_line_once():
    refused = [{"name": "Shadowheart", "level": 3, "why": "game"}]
    bad = [{"name": "shadowheart", "level": 3, "why": "check"}, {"name": "Lae'zel", "level": 4, "why": "check"}]
    assert keeper_side.merge_refused(refused, bad) == [refused[0], bad[1]]


def test_relaunch_note_and_the_parts():
    assert '"the newest save"' in session_side.relaunch_note("game 3.1 h old", None)
    assert "(game 3.1 h old)" in session_side.relaunch_note("game 3.1 h old", "S004-end")
    for name in ("handoff_session", "join_keeper", "stop_check", "play_session", "run", "start_keeper"):
        assert callable(getattr(PlaythroughSide, name))


def test_the_apps_profile_migration_is_the_runners_parse():
    text = json.dumps({"character": {"brief": "a dwarf cleric", "roleplay": "wild"}, "difficulty": "Nightmare"})
    assert config.parse_profile_json(text) == profile.parse_profile(json.loads(text))
    assert config.parse_profile_json(text) == {"character": {"brief": "a dwarf cleric", "roleplay": "strict"}}
    assert profile.parse_profile({"character": "x"}) == {} and profile.parse_profile([1]) == {}
