"""bg3harness.video.timeline: the evidence video's pure core (recording clips, side-panel items and changes, strip
states, timeline lanes, tool call summaries, render.json)."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from bg3harness.video import timeline as T  # noqa: N812

T0 = 1000.0


def seg(file: str, start: float, end: float) -> dict[str, Any]:
    return {"file": file, "run": "rec-1", "start": start, "end": end, "bytes": 1}


def ev(seq: int, t: float, kind: str, src: str = "client", **kw: Any) -> dict[str, Any]:
    return {"v": 1, "seq": seq, "t": T0 + t, "src": src, "kind": kind, **kw}


def test_clips_cut_the_segments_to_the_run_window():
    segs = [seg("b.mp4", 1600.0, 2200.0), seg("a.mp4", 900.0, 1600.0), seg("c.mp4", 2200.0, 2500.0)]
    assert T.clips(segs, T0, 2300.0) == [
        T.Clip("a.mp4", 100.0, 700.0),
        T.Clip("b.mp4", 0.0, 600.0),
        T.Clip("c.mp4", 0.0, 100.0),
    ]
    assert T.clips(segs, 1700.0, 1800.0) == [T.Clip("b.mp4", 100.0, 200.0)]


@pytest.mark.parametrize(
    ("segs", "where"),
    [
        ([seg("a", 900, 1500), seg("b", 1506, 2100)], "at run second 500.0"),  # a 6 s hole between segments
        ([seg("a", 1006, 2100)], "at run second 0.0"),  # started late
        ([seg("a", 900, 1994)], "at run second 994.0"),  # ended early
        ([], "at run second 0.0"),
    ],
)
def test_a_recording_with_a_gap_over_5_s_is_refused(segs: list[dict[str, Any]], where: str):
    with pytest.raises(T.RenderError, match=where):
        T.clips(segs, T0, 2000.0)


def test_a_segment_still_being_written_covers_nothing():
    segs = [seg("a", 900, 1500), {**seg("b", 1500, 0), "end": None}]
    assert T.clips(segs, T0, 1500.0) == [T.Clip("a", 100.0, 600.0)]
    with pytest.raises(T.RenderError, match=r"misses 500\.0 s at run second 500\.0"):
        T.clips(segs, T0, 2000.0)


def test_a_gap_up_to_5_s_is_bridged():
    cut = T.clips([seg("a", 900, 1500), seg("b", 1505, 2100)], T0, 2000.0)
    assert [c.file for c in cut] == ["a", "b"]
    assert sum(c.duration for c in cut) == 995.0


def test_the_panel_shows_visible_text_tool_calls_and_result_first_lines_only():
    logs = {
        "S001": [
            ev(0, 0.0, "session_start", src="harness", detail={"session": "S001"}),
            ev(1, 1.0, "thinking", think_ms=600.0, text="hidden plan: cheat"),  # never shown, even with text
            ev(2, 2.0, "tool_call", tool="combat_turn", args={"action": "attack", "target": "e12"}, call_id="c1"),
            ev(3, 3.0, "tool_result", text="\nhit e12 for 7\nmore lines", call_id="c1"),
            ev(4, 4.0, "tool_result", text="out of range", is_error=True, call_id="c2"),
            ev(5, 5.0, "usage", tokens={"input": 1, "output": 2}),
            ev(6, 6.0, "text", text="  Goblin down.  "),
            ev(7, 7.0, "result", text="Goblin down.", cost_usd=0.5),
            ev(8, 8.0, "text", text="   "),
        ],
        "S001-c1": [ev(0, 2.0, "tool_call", tool="get_status", args={})],
    }
    items = T.panel_items(logs, T0)
    assert [(i.at_s, i.file, i.seq, i.kind, i.text, i.tool, i.ref) for i in items] == [
        (2.0, "S001", 2, "call", 'action="attack" target="e12"', "combat_turn", "c1"),
        (2.0, "S001-c1", 0, "call", "", "get_status", ""),
        (3.0, "S001", 3, "result", "hit e12 for 7", "", "c1"),
        (4.0, "S001", 4, "error", "out of range", "", "c2"),
        (6.0, "S001", 6, "text", "Goblin down.", "", ""),
    ]
    assert all("hidden" not in i.text for i in items)


def test_image_file_paths_never_reach_the_panel():
    path = "[Image: source: /home/someone/.claude/projects/x/tool-results/blob.jpg]"
    logs = {"S001": [ev(0, 1.0, "tool_result", text=f"<image> {path}"), ev(1, 2.0, "text", text=f"look {path} here")]}
    assert [i.text for i in T.panel_items(logs, T0)] == ["<image>", "look here"]


def items_at(*times: float) -> list[T.Item]:
    return [T.Item(t, "S001", k, "call", f"t{k}") for k, t in enumerate(times)]


def test_changes_come_at_most_twice_a_second_and_never_before_the_item():
    assert T.changes(items_at(1.0, 1.1, 1.2, 1.3, 1.9, 5.0)) == [(0.0, 0), (1.0, 1), (1.5, 4), (2.0, 5), (5.0, 6)]
    assert T.changes(items_at(0.0, 0.0, 0.2)) == [(0.0, 2), (0.5, 3)]  # the same instant is one change
    assert T.changes([]) == [(0.0, 0)]
    burst = T.changes(items_at(*[k * 0.05 for k in range(100)]))
    assert all(b[0] - a[0] >= 0.5 for a, b in itertools.pairwise(burst))
    assert burst[-1][1] == 100
    shown = items_at(*[k * 0.05 for k in range(100)])
    for at, n in burst:  # every item shown by a change came at or before it
        assert all(it.at_s <= at for it in shown[:n])


def test_strip_states_carry_the_clock_cost_gold_and_party():
    facts = [
        {"v": 1, "kind": "mark", "t": T0, "mark": "run_start"},
        {"v": 1, "kind": "snapshot", "t": T0 + 0.5, "gold": 133, "party": [{"name": "Tav", "hp": 18, "max_hp": 18}]},
        {
            "v": 1,
            "kind": "snapshot",
            "t": T0 + 2.0,
            "gold": 410,
            "party": [{"name": "Tav", "hp": 0, "max_hp": 18, "downed": True}],
        },
    ]
    logs = {
        "S001": [ev(9, 1.5, "result", cost_usd=0.25)],
        "S002": [ev(4, 2.5, "result", cost_usd=0.5), ev(5, 2.6, "result")],
    }
    states = T.strip_states(3.2, facts, logs, T0, t_pass_s=2.0)
    assert [s.clock_s for s in states] == [0, 1, 2, 3]
    assert [s.gold for s in states] == [None, 133, 410, 410]
    assert {s.gold_start for s in states} == {133}
    assert [s.cost_usd for s in states] == [None, None, 0.25, 0.75]
    assert states[2].party == (T.Member("Tav", 0, 18, True),)
    assert states[0].party == ()
    assert {s.passed_s for s in states} == {2.0}
    assert [s.party_at_s for s in states] == [None, 0.5, 2.0, 2.0]


def test_a_down_and_a_revive_show_at_their_own_time_until_a_later_snapshot():
    def snap(t: float, hp: int) -> dict[str, Any]:
        party = [{"name": "Vessa Thornquill", "hp": hp, "max_hp": 24}, {"name": "Gale", "hp": 20, "max_hp": 20}]
        return {"v": 1, "kind": "snapshot", "t": T0 + t, "gold": 0, "party": party}

    def fact(t: float, kind: str) -> dict[str, Any]:
        return {"v": 1, "kind": "event", "t": T0 + t, "ev": kind, "name": "Vessa Thornquill", "party": True}

    facts = [snap(0.0, 24), fact(2.0, "downed"), fact(4.0, "revived"), fact(5.5, "downed"), snap(6.0, 9)]
    downs = [tuple(m.down for m in s.party) for s in T.strip_states(7.0, facts, {}, T0)]
    assert downs == [(False, False)] * 2 + [(True, False)] * 2 + [(False, False)] * 2 + [(False, False)]
    assert [s.party[0].hp for s in T.strip_states(7.0, facts, {}, T0)] == [24, 24, 0, 0, 24, 24, 9]


def test_lanes_mark_fights_kills_downs_fails_and_text():
    def fact(t: float, kind: str, **kw: Any) -> dict[str, Any]:
        return {"v": 1, "kind": "event", "t": T0 + t, "ev": kind, **kw}

    facts = [
        fact(-5.0, "combat_started", combat="a"),  # before the run: the band starts at 0
        fact(3.0, "combat_ended", combat="a"),
        fact(4.0, "kill", victim="Imp"),
        fact(6.0, "combat_started", combat="b"),  # never ends: the band runs to the end
        fact(7.0, "kill", victim="Gut"),
        fact(7.5, "downed", name="Vessa"),
        fact(12.0, "kill", victim="late"),  # after the run
    ]
    items = [T.Item(1.0, "S001", 0, "error", "FAILED"), T.Item(2.0, "S001", 1, "text", "hi")]
    lanes = T.lanes(facts, items, T0, 10.0, t_pass_s=7.4)
    assert lanes == T.Lanes(((0.0, 3.0), (6.0, 10.0)), ((4.0, False), (7.0, True)), (7.5,), (1.0,), (2.0,))
    assert T.lanes(facts, items, T0, 10.0).kills == ((4.0, False), (7.0, False))


def test_a_call_summary_reads_as_a_line_not_json():
    plan = [
        {
            "who": "Vessa Thornquill",
            "plan": {
                "move": {"target": "e299", "within": 10},
                "action": [{"ability": "Sneak Attack", "target": "e299"}],
            },
        },
        {
            "who": "Shadowheart",
            "plan": {
                "action": [{"ability": "Bless", "targets": ["e4", "e1"]}],
                "bonus": {"ability": "Spiritual Weapon", "target": "e252"},
            },
        },
    ]
    assert T.summary("combat_turn", {"plan": plan}) == (
        "Vessa: Sneak Attack → e299 · move ≤ 10 m; Shadowheart: Bless → e4,e1, bonus Spiritual Weapon → e252"
    )
    one = {"who": "Lae'zel", "plan": {"move": "e252", "bonus": [{"ability": "Dash"}]}}
    assert T.summary("combat_turn", {"plan": one}) == "Lae'zel: bonus Dash · move → e252"
    assert T.summary("combat_turn", {"who": "Gale", "plan": {"action": []}}) == "Gale: -"
    assert T.summary("walk_toward", {"objective": "o1", "max_m": 40}) == "→ objective o1, ≤ 40 m"
    assert T.summary("screenshot", {"scale": 0.4}) == "(0.4\u00d7)"
    assert T.summary("wait_for", {"target": "my_turn"}) == 'target="my_turn"'


def test_render_json_lists_every_panel_item_in_the_video_and_refuses_an_early_or_late_one():
    items = items_at(1.0, 2.5)
    assert T.sync_table(items, T0, T0 + 10.0, "ab" * 32) == {
        "schema": 1,
        "run_hash": "ab" * 32,
        "video_start": T0,
        "video_end": T0 + 10.0,
        "fps": 15,
        "events": [{"file": "S001", "seq": 0, "at_s": 1.0}, {"file": "S001", "seq": 1, "at_s": 2.5}],
    }
    with pytest.raises(T.RenderError, match="after the video's end"):
        T.sync_table(items_at(1.0, 11.0), T0, T0 + 10.0, "ab" * 32)
    # S2 nautiloid: events 12.3 s before a late run_start went out as at_s -12.287 (the bench's F03 rejects it)
    with pytest.raises(T.RenderError, match="before the run's start"):
        T.sync_table(items_at(-12.287, 1.0), T0, T0 + 10.0, "ab" * 32)


def test_a_resumed_claude_conversation_costs_its_highest_running_total_not_the_sum():
    def process(client: str, sid: str, t: float, cost: float) -> list[dict[str, Any]]:
        start = ev(0, t - 1.0, "session_start", src="harness", detail={"client": client})
        return [start, ev(1, t, "result", cost_usd=cost, session_id=sid)]

    logs = {
        "S001": process("claude", "conv-a", 10.0, 0.4),  # claude's cost_usd: the conversation's total so far
        "S001-c1": process("claude", "conv-a", 20.0, 0.9),  # resumed: 0.9 in all, 0.5 of it this process's
        "S002": process("pi", "p-1", 30.0, 0.3),  # pi's: the process's own
        "S002-c1": process("pi", "p-1", 40.0, 0.3),
    }
    assert T.result_costs(logs, T0) == [(10.0, 0.4), (20.0, 0.5), (30.0, 0.3), (40.0, 0.3)]
    assert T.strip_states(41.0, [], logs, T0)[-1].cost_usd == 1.5
