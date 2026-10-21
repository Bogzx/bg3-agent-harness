import pytest

from bg3bench import conditions
from bg3bench.adapters import play_flags


def test_tools_off_follows_the_condition():
    assert play_flags.entry("rules").env == {"BG3AI_TOOLS_OFF": "wiki_search,wiki_page,load_game"}
    assert play_flags.entry("rules+wiki").env == {"BG3AI_TOOLS_OFF": "load_game"}
    assert play_flags.entry("rules+reloads:2").env == {"BG3AI_TOOLS_OFF": "wiki_search,wiki_page"}
    assert play_flags.entry("rules+wiki+reloads:2").env == {}
    for cid in ("rules", "rules+wiki", "rules+reloads:2"):
        off = play_flags.entry(cid).env.get("BG3AI_TOOLS_OFF", "")
        assert off.split(",") == conditions.parse(cid).removed_tools() or off == ""


def test_gaps_name_what_play_py_lacks():
    assert play_flags.entry("rules").gaps == (play_flags.GAP_RULES_PROMPT, play_flags.GAP_SEED)
    assert play_flags.entry("rules+coaching@1a2b3c4d+persona:evil+reloads:1").gaps == (
        play_flags.GAP_COACHING,
        play_flags.GAP_PERSONA,
        play_flags.GAP_RELOADS,
        play_flags.GAP_SEED,
    )
    assert play_flags.entry("rules").flags == ()


def test_table_refuses_gaps_unless_a_pilot():
    with pytest.raises(ValueError, match="can't play 'rules' yet: it lacks a rules-only prompt"):
        play_flags.table(["rules"])
    flags, env = play_flags.table(["rules", "rules+wiki"], allow_gaps=True)
    assert flags == {"rules": [], "rules+wiki": []}
    assert env["rules+wiki"] == {"BG3AI_TOOLS_OFF": "load_game"}
    with pytest.raises(ValueError, match="canonical"):
        play_flags.table(["rules+wiki+coaching@1a2b3c4d"], allow_gaps=True)
