from bg3play import parse as P


def test_log_line_kinds_and_newline_marks():
    assert P.log_line("23:27:44 [say] Dash toward ⏎ the Transponder.") == {
        "clock": "23:27:44",
        "kind": "say",
        "text": "Dash toward\nthe Transponder.",
    }
    assert P.log_line('23:27:40 [tool] save_game {"label":"S003-start"}')["kind"] == "tool"
    assert P.log_line("===== S037 2026-10-10 20:14:00 claude session x") == {
        "clock": None,
        "kind": "header",
        "session": "S037",
        "text": "2026-10-10 20:14:00 claude session x",
    }
    assert P.log_line("no clock here") is None


def test_end_line():
    assert P.end_line("success turns=324 duration=25.3min cost=$9.89") == {
        "outcome": "success",
        "turns": 324,
        "minutes": 25.3,
        "cost_usd": 9.89,
    }
    assert P.end_line("error_max_turns") == {
        "outcome": "error_max_turns",
        "turns": None,
        "minutes": None,
        "cost_usd": None,
    }


def test_action_and_tool_kind():
    a = P.action('{"t": 1.0, "tool": "get_status", "args": {}, "ms": 5, "result": "FAILED: x"}')
    assert a == {
        "t": 1.0,
        "tool": "get_status",
        "args": {},
        "ms": 5,
        "result": "FAILED: x",
        "ok": False,
        "kind": "read",
    }
    assert P.action("not json") is None and P.action("[1]") is None
    assert P.tool_kind("mcp__bg3__screenshot") == "vision" and P.tool_kind("save_game") == "save"
    assert P.tool_kind("combat_turn") == "act"
    long = P.action('{"tool": "look", "result": "' + "x" * 3000 + '"}', result_limit=10)
    assert long["result"] == "x" * 10 + "…"


STATUS = (
    "WHERE Self-Same Trial · Shadow-Cursed Lands | exploring | gold 1695\n"
    "PARTY e3 Wyll Warlock6(The Fiend) HP 90/90 [Blessing of Selûne,Darkveil Precision]\n"
    "PARTY *e6 Claude Paladin6(Oath of Devotion) HP 82/104 [Blessing of Selûne]\n"
    "PARTY: Halsin (camp, Druid 5) is tied to Shadow-Cursed Lands\n"
    "PARTY APART: e4 Karlach near"
)
SNAPSHOT = (
    "WHERE Upper City | COMBAT round 14 turn=? turn-based | gold 7166\n"
    "PARTY * The Emperor L12 HP 122/145 [Aid,Marked for Negation] in_combat\n"
    "PARTY Wyll Warlock 12 (The Fiend) HP 0/174 [Weavewalker,Downed] downed in_combat\n"
    "PARTY Gale Wizard 12 (Evocation) HP 116/177+1 [Aid] in_combat\n"
    "PARTY Shadowheart Cleric 12 (Trickery Domain) HP 0/174 [Weavewalker] dead"
)


def test_status_text_from_get_status():
    st = P.status_text(STATUS)
    assert st["where"] == "Self-Same Trial · Shadow-Cursed Lands" and st["mode"] == "exploring" and st["gold"] == 1695
    assert [(m["name"], m["classes"], m["hp"], m["max_hp"], m["active"]) for m in st["party"]] == [
        ("Wyll", "Warlock6(The Fiend)", 90, 90, False),
        ("Claude", "Paladin6(Oath of Devotion)", 82, 104, True),
    ]
    assert st["party"][0]["statuses"] == ["Blessing of Selûne", "Darkveil Precision"]


def test_status_text_from_snapshot():
    st = P.status_text(SNAPSHOT)
    assert st["mode"] == "COMBAT round 14 turn=? turn-based" and st["gold"] == 7166
    names = [m["name"] for m in st["party"]]
    assert names == ["The Emperor", "Wyll", "Gale", "Shadowheart"]
    emperor, wyll, gale, sh = st["party"]
    assert emperor["active"] and emperor["classes"] == "L12" and emperor["in_combat"]
    assert wyll["downed"] and wyll["classes"] == "Warlock 12 (The Fiend)"
    assert gale["temp_hp"] == 1 and sh["dead"]


def test_status_text_without_rows_is_none():
    assert P.status_text("e6 Claude moved 3m NW, HP 82->0/104\nACTIVE e3 Wyll") is None


def test_current_goal():
    objectives = "# Objectives\n<!-- NOW: not this -->\n## Main\n1. DONE S055: x\n10. NOW: finish the epilogue\n"
    assert P.current_goal(objectives, "") == "finish the epilogue"
    handoff = "# Handoff\n## Doing right now\n\nFinal fight try 12.\nmore\n## Next steps\n1. x"
    assert P.current_goal("# Objectives\n", handoff) == "Final fight try 12."
    assert P.current_goal("", "# Handoff\n## Doing right now\n## Next\n") is None


def test_decisions():
    text = (
        "## Decisions\n\n## D-07 Exposed Kagha (S013)\n- Choice: showed the letter\n  publicly\n- Why: protect\n"
        "- Consequence: Kagha left [seen S013]\n- Expected later: hostile [wiki]\n- Reversible: no\n"
    )
    (card,) = P.decisions(text)
    assert card["id"] == "D-07" and card["session"] == "S013" and card["choice"] == ["showed the letter publicly"]
    assert card["consequences"] == [{"text": "Kagha left", "tag": "seen S013"}]
    assert card["expected"] == [{"text": "hostile", "tag": "wiki"}] and card["reversible"] == "no"
    assert P.session_number("S013") == 13 and P.session_number(None) is None


def test_fight_and_git_log():
    row = {
        "v": 1,
        "session": "S037",
        "t": "2026-10-10T20:34:34",
        "region": "Wilderness",
        "grade": "clean",
        "rounds": 1,
        "enemies": [{"name": "Goblin", "count": 3}, {"name": "Boss"}],
        "party": [{"name": "Wyll", "downed": 1, "died": False}, {"name": "Gale", "downed": 0, "died": True}],
    }
    f = P.fight(row)
    assert f["enemies"] == ["Goblin x3", "Boss"] and f["downed"] == 1 and f["died"] == ["Gale"]
    assert P.fight({"no": "session"}) is None
    log = "abc\t1792312786\tS096 memory-keeper: HANDOFF.md\ndef\t1792311056\tinit\nbroken line\n"
    assert P.git_log(log) == [
        {"sha": "abc", "t": 1792312786, "subject": "S096 memory-keeper: HANDOFF.md", "session": "S096"},
        {"sha": "def", "t": 1792311056, "subject": "init", "session": None},
    ]


def test_scrub():
    assert P.scrub("id 306e86eb-9171-40f8-b333-db447c38843f end") == "id … end"


def test_diff_lines():
    patch = ("diff --git a/x.md b/x.md\nindex 1..2 100644\n--- a/x.md\n+++ b/x.md\n@@ -1,2 +1,2 @@\n same\n-old\n"
             "+new\n\\ No newline at end of file\n")
    assert [d["kind"] for d in P.diff_lines(patch)] == ["meta", "meta", "meta", "meta", "hunk", "ctx", "del", "add",
                                                          "meta"]
    assert P.diff_lines("") == []
