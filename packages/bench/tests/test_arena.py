# pyright: basic
"""Arena files (arena.v1) and the match grader over synthetic facts."""

import copy
import json
import shutil
from pathlib import Path

import jsonschema
import pytest
from bg3harness.arena.match import END_SETTLE_S
from bg3harness.game.io import EVAL_TIMEOUT

from bg3bench import arena, facts, stats, suite

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
REPO = PKG.parents[1]
FIXTURES = HERE / "fixtures" / "arena"
ARENA_SCHEMA = json.loads((PKG / "schema" / "arena.v1.schema.json").read_text())
EVENT_SCHEMA = json.loads((PKG / "schema" / "arena-facts.v1.schema.json").read_text())
FACTS_SCHEMA = json.loads((REPO / "packages" / "harness" / "schemas" / "facts.v1.schema.json").read_text())
DRAFT = json.loads((PKG / "arenas" / "draft_open_field.json").read_text())
SAVE = {"name": "Tav-1234__SCN-arena_open_field", "sha1": "0123456789abcdef0123456789abcdef01234567"}
# the draft with a save and fair spawns (check_geometry: level, apart along z, the teams' lines along x)
FAIR = {"level": DRAFT["location"]["level"], "spawns": {"A": [0.0, 0.0, 0.0], "B": [0.0, 0.0, 20.0]}}
READY = {k: v for k, v in {**DRAFT, "save": SAVE, "location": FAIR}.items() if k != "draft"}
ARENA = arena.with_defaults(READY)
TEAMS = arena.team_names(ARENA)
SIDES = {"A": "pi/model-x", "B": "pi/model-y"}


def ev(t, name, **kw):
    return {"v": 1, "kind": "event", "t": float(t), "ev": name, **kw}


def match(winner="A", reason="wipe", hp=(0.75, 0.0), rounds=2, start=None, turns=None, ends=None, forced=(), **kw):
    """A match's facts: run_start, arena_start, the turns ((side, character, round[, members]); one member by
    default; those at the indices in `forced` forced to an end), arena_end (+ kw), run_end."""
    recs = [{"v": 1, "kind": "mark", "t": 0.0, "mark": "run_start", "detail": None}]
    recs.append(start or ev(1, "arena_start", arena=ARENA["name"], seed=ARENA["seed"], teams=TEAMS, sides=SIDES))
    for i, (side, who, rnd, *members) in enumerate(turns or [("A", "Karlach", 1), ("B", "Wyll", 1), ("A", "Gale", 2)]):
        names = members[0] if members else [who]
        phase = {"members": names, "names": names, "phase": 1, "group": "g1", "shared": True}
        recs.append(ev(10 + i, "arena_turn", side=side, character=who, round=rnd, **phase))
        if i in forced:
            recs.append(ev(10.5 + i, "arena_turn_forced", side=side, character=who))
    end = {"winner": winner, "rounds": rounds, "hp_left_A": hp[0], "hp_left_B": hp[1], "reason": reason, **kw}
    for e in ends if ends is not None else [end]:
        recs.append(ev(50, "arena_end", **e))
    recs.append({"v": 1, "kind": "mark", "t": 60.0, "mark": "run_end", "detail": None})
    return facts.check(recs)


# ---------------------------------------------------------------------------------------------------- arena files


# Played at their pins and kept as played (arena-v0: the released seat run; arena-v0-seed3: window 5's pairing): this
# code's builds check wants the race identity fields they predate, so they validate only with the code they played
PLAYED_AT_PINS = ("arena-v0", "arena-v0-seed3")


def raw(name: str) -> dict:
    return json.loads((PKG / "arenas" / f"{name}.json").read_text())


def test_every_arena_file_is_valid_and_the_placeholder_is_a_draft():
    files = sorted((PKG / "arenas").glob("*.json"))
    assert [p.stem for p in files] == [
        "arena-v0-seed3",
        "arena-v0.1",
        "arena-v0",
        "builds-l2-4v4-v1",
        "crash-companions-v1",
        "crash-mirror-v1",
        "draft_open_field",
    ]
    for p in files:
        a = json.loads(p.read_text())
        jsonschema.validate(a, ARENA_SCHEMA)
        if p.stem in PLAYED_AT_PINS:  # only the kit's race id is missing, pair by pair
            race = a["mirror"]["pairs"][0]["race"]
            assert arena.validate(a, p.stem) == [
                f"mirror.pairs[{i}].kit's race part is for race None, the pair's race is {race}" for i in range(2)
            ]
        else:
            assert arena.validate(a, p.stem) == []
    assert "draft" in arena.load_named("draft_open_field")


def test_arena_v0_1_is_arena_v0_with_the_race_identity_fields():
    # owner 2026-11-05: arena-v0.json stays as the seat run played it (its release pins its sha256); v0.1 adds the race
    # id to each kit's race part (race.race, the pair's race), and nothing a twin plays with
    a, b = raw("arena-v0"), raw("arena-v0.1")
    assert b["name"] == "arena-v0.1" and b["about"].startswith(a["about"])
    for p in b["mirror"]["pairs"]:
        assert p["kit"]["race"].pop("race") == p["race"] == "76057327-da03-4398-aaf0-c59345ef3a8b"
    assert {**b, "name": a["name"], "about": a["about"]} == a
    assert arena.load_named("arena-v0.1")["name"] == "arena-v0.1"


def test_an_arena_name_may_carry_one_numeric_version():
    assert all(arena.ARENA_NAME_RE.match(n) for n in ("arena-v0", "arena-v0.1", "crash-mirror-v1", "a.12"))
    assert not any(arena.ARENA_NAME_RE.match(n) for n in ("arena-v0.", "arena-v0.x", "arena-v0.1.2", ".1", "Arena-v0"))


def test_arena_v0_seed3_is_arena_v0_with_seed_3():
    # window 5's DeepSeek pairing (ops/logs/live.md 'DeepSeek pairing inputs'): only the name and the seed differ
    a, b = raw("arena-v0"), raw("arena-v0-seed3")
    assert (b["name"], b["seed"]) == ("arena-v0-seed3", 3)
    assert {**b, "name": a["name"], "seed": a["seed"]} == a


# The site search's fine reads at site 2 (2026-11-03, ops/logs/live.md '# arena site search'): where each member
# (Setup.line: spawn x -+ 0.75) landed; x/z as asked
SITE_2_LANDED = {"A": [(212.25, 10.9746, 288.0), (213.75, 10.9805, 288.0)],
                 "B": [(212.25, 11.0186, 296.0), (213.75, 10.9492, 296.0)]}  # fmt: skip


def test_arena_v0_is_live_15s_two_against_two():
    # live 15 (2026-11-02, ops/logs/live.md): pairs 1-2 of the live-mirror-3 spec (Fighter Ash/Birch, Rogue
    # Cedar/Dune), setup spawn_order [A, B] (an odd seed); the spawns are the site search's site 2, as landed (v0.1:
    # the same spawns, and the file this code loads)
    a = arena.load_named("arena-v0.1")
    saves = json.loads((PKG / "saves.json").read_text())
    rows = [r for suite in saves["suites"].values() for r in suite["saves"] if r["name"] == a["save"]["name"]]
    assert {r["save_sha1"] for r in rows} == {a["save"]["sha1"]} and "crash_site_grove" in a["save"]["name"]
    spawns = a["location"]["spawns"]
    assert spawns == {"A": [213.0, 10.98, 288.0], "B": [213.0, 10.98, 296.0]}
    for s, members in SITE_2_LANDED.items():  # the file's spawn = its line's centre where the members landed
        assert [round(sum(m[0] for m in members) / 2, 2), round(sum(m[1] for m in members) / 2, 2), members[0][2]] == (
            spawns[s]
        )
    ys = [m[1] for line in SITE_2_LANDED.values() for m in line]
    assert max(ys) - min(ys) < 0.2  # under setup's 0.3 m with margin, every pair and across the pairs
    assert arena.team_names(a) == {"A": ["Ash", "Cedar"], "B": ["Birch", "Dune"]}
    assert a["mirror"]["pairs"] == arena.load_named("crash-mirror-v1")["mirror"]["pairs"][:2]
    assert (a["level"], a["seed"] % 2, a["karmic"], a["round_cap"], a["wall_cap_min"], a["tools_off"]) == (
        2,
        1,
        False,
        20,
        60,  # owner D459 (2026-11-03): an arena's wall cap is 60 min
        "arena-v1",
    )
    assert "draft" not in a


def test_the_companions_arena_is_the_crash_site_save_with_every_member_named_and_identified():
    a = arena.load_named("crash-companions-v1")
    saves = json.loads((PKG / "saves.json").read_text())
    rows = [r for suite in saves["suites"].values() for r in suite["saves"] if r["name"] == a["save"]["name"]]
    sha1s = {r["save_sha1"] for r in rows}
    assert sha1s == {a["save"]["sha1"]} and "crash_site_grove" in a["save"]["name"]
    assert arena.team_names(a) == {
        "A": ["Gale", "Astarion", "Lae'zel"],
        "B": ["Karlach", "Wyll", "Minthara"],
    }
    assert all("uuid" in m for s in ("A", "B") for m in a["teams"][s])
    assert (a["level"], a["seed"], a["round_cap"], a["wall_cap_min"], a["turn_cap_s"]) == (1, 2026, 20, 30, 60)
    assert "0.50 m apart in height" in a["draft"]  # until live 15's height read gives new spawns (D349)


def test_the_mirror_arena_names_four_twin_pairs_and_waits_for_its_live_check():
    a = arena.load_named("crash-mirror-v1")
    companions = arena.load_named("crash-companions-v1")
    pairs = a["mirror"]["pairs"]
    assert [p["class"] for p in pairs] == ["Fighter", "Rogue", "Barbarian", "Ranger"]
    assert arena.team_names(a) == {s: [p["names"][s] for p in pairs] for s in ("A", "B")}
    assert all(set(m) == {"name"} for s in ("A", "B") for m in a["teams"][s])  # the setup makes the uuids
    assert a["level"] == 2 and "0.50 m apart in height" in a["draft"]
    same = ("save", "location", "seed", "round_cap", "wall_cap_min", "turn_cap_s", "condition", "tools_off")
    assert {k: a[k] for k in same} == {k: companions[k] for k in same}
    assert "NOT READY FOR RATED MATCHES" in a["about"]


def test_the_builds_arena_is_a_rated_4v4_red_blue_from_the_build_file_with_a_race_per_pair():
    a = arena.load_named("builds-l2-4v4-v1")
    pairs = a["mirror"]["pairs"]
    assert [p["build"] for p in pairs] == ["fighter", "rogue", "barbarian", "ranger"]
    assert len({p["race"] for p in pairs}) == 4 and len({p["template"] for p in pairs}) == 4  # A1: a race per build
    assert all(p["kit"]["race"]["race"] == p["race"] for p in pairs)
    assert "draft" not in a and all(p["reference"]["profile"] for p in pairs)  # S3's four references
    names = arena.team_names(a)
    assert all(n.startswith("Red ") for n in names["A"]) and all(n.startswith("Blue ") for n in names["B"])
    assert [n.split(" ", 1)[1] for n in names["A"]] == [n.split(" ", 1)[1] for n in names["B"]]
    # S1's P5 cells: a 1.5 m line of 4 doesn't fit the ground that lands exactly, a 1.25 m line does
    loc = a["location"]
    assert loc["line_gap"] == 1.25
    assert loc["spawns"] == {"A": [212.875, 10.98, 288.0], "B": [212.875, 10.98, 296.0]}
    assert arena.check_geometry(loc["spawns"], len(pairs), loc["line_gap"]) == []
    assert {k: a[k] for k in ("save", "seed", "karmic")} == {
        k: arena.load_named("crash-mirror-v1")[k] for k in ("save", "seed", "karmic")
    }


def test_a_kit_part_only_a_reference_checks_needs_one_whatever_build_says():
    a = copy.deepcopy(raw("arena-v0.1"))
    assert arena.validate(a, "arena-v0.1") == []
    a["mirror"]["pairs"][0]["kit"]["prepared"] = ["Target_Bless"]
    a["mirror"]["pairs"][0]["kit"]["subclass"] = "life"
    assert arena.validate(a, "arena-v0.1") == [
        "mirror.pairs[0]: a kit with subclass, prepared has no reference (only a draft may lack one)"
    ]
    a["mirror"]["pairs"][0]["reference"] = {"profile": {}}
    assert arena.validate(a, "arena-v0.1") == []
    del a["mirror"]["pairs"][0]["reference"]
    a["draft"] = "a kit in progress"
    assert arena.validate(a, "arena-v0.1") == []


def test_location_line_gap_is_optional_checked_and_used_by_the_geometry():
    with_gap = _with(location={**FAIR, "line_gap": 1.25})
    jsonschema.validate(with_gap, ARENA_SCHEMA)
    assert arena.validate(with_gap, READY["name"]) == []
    for bad in (0, -1, "1.25", True):
        got = arena.validate(_with(location={**FAIR, "line_gap": bad}), READY["name"])
        assert got == ["location.line_gap must be a number above 0 (metres)"]
    assert arena.validate(_with(location={**FAIR, "gap": 1.25}), READY["name"]) == [
        "location must be {level, spawns, line_gap?}"
    ]
    # the cross rule scales with the line: this offset fails on a long line and passes on a short one
    spawns = {"A": [0.0, 0.0, 0.0], "B": [0.5, 0.0, 8.0]}
    n = 4
    assert arena.check_geometry(spawns, n, 0.25) == []
    assert "must be the same within" in arena.check_geometry(spawns, n, 3.0)[0]
    far = _with(location={"level": FAIR["level"], "spawns": spawns, "line_gap": 3.0})
    assert any("must be the same within" in e for e in arena.validate(far, READY["name"]))


def test_a_draft_may_leave_both_spawns_todo_but_not_one():
    todo = _with(location={"level": "WLD_Main_A", "spawns": {"A": None, "B": None}}, draft="spawns: a live read")
    jsonschema.validate(todo, ARENA_SCHEMA)
    assert arena.validate(todo, READY["name"]) == []
    half = _with(location={"level": "WLD_Main_A", "spawns": {"A": [0, 0, 0], "B": None}}, draft="spawns: a live read")
    assert arena.validate(half, READY["name"]) == ["location.spawns.B must be [x, y, z] in metres"]


def test_load_fills_the_defaults(tmp_path):
    bare = {k: v for k, v in READY.items() if k not in ("round_cap", "wall_cap_min", "condition", "level")}
    (tmp_path / f"{bare['name']}.json").write_text(json.dumps(bare))
    a = arena.load(tmp_path / f"{bare['name']}.json")
    assert (a["round_cap"], a["wall_cap_min"], a["condition"], a["level"]) == (20, 30, "rules", 2)
    assert a["turn_order"] == "alternate"


def test_turn_order_alternate_or_snake():
    assert arena.validate(_with(turn_order="snake"), READY["name"]) == []
    assert arena.validate(_with(turn_order="alternate"), READY["name"]) == []
    for bad in ("team", 1, "Snake"):
        assert "turn_order must be one of alternate, snake" in arena.validate(_with(turn_order=bad), READY["name"])


def _with(**kw):
    a = copy.deepcopy(READY)
    for k, v in kw.items():
        if v is None:
            a.pop(k)
        else:
            a[k] = v
    return a


def pair(cls: str, a: str, b: str) -> dict:
    return {
        "class": cls,
        "template": "t",
        "race": "r",
        "stats": "s",
        "names": {"A": a, "B": b},
        "kit": {"race": {"race": "r"}},
    }


PAIRS = [pair("Fighter", "Ash", "Birch"), pair("Rogue", "Cedar", "Dune")]
NAME_ONLY = {"A": [{"name": "Ash"}, {"name": "Cedar"}], "B": [{"name": "Birch"}, {"name": "Dune"}]}
MIRROR = _with(teams=NAME_ONLY, mirror={"pairs": PAIRS})

BAD = {
    "no save outside a draft": _with(save={"name": None, "sha1": None}),
    "short sha1": _with(save={**SAVE, "sha1": "abc"}),
    "not a save folder": _with(save={**SAVE, "name": "no_owner_label"}),
    "one spawn": _with(location={"level": "WLD_Main_A", "spawns": {"A": [0, 0, 0]}}),
    "a 2d spawn": _with(location={"level": "WLD_Main_A", "spawns": {"A": [0, 0], "B": [1, 0, 0]}}),
    "five a side": _with(teams={"A": [{"name": f"a{i}"} for i in range(5)], "B": [{"name": "b"}]}),
    "an empty side": _with(teams={"A": [], "B": [{"name": "b"}]}),
    "an unknown member key": _with(teams={"A": [{"name": "a", "hp": 9}], "B": [{"name": "b"}]}),
    "a member uuid that isn't one": _with(teams={"A": [{"name": "a", "uuid": "Shadowheart"}], "B": [{"name": "b"}]}),
    "level 0": _with(level=0),
    "level 13": _with(level=13),
    "a level that isn't a number": _with(level="2"),
    "no seed": _with(seed=None),
    "a negative seed": _with(seed=-1),
    "round cap 0": _with(round_cap=0),
    "wall cap 0": _with(wall_cap_min=0),
    "an unknown tools list": _with(tools_off="everything"),
    "an unknown key": _with(effort="high"),
    "an empty draft reason": _with(draft=""),
    "karmic dice on": _with(karmic=True),
    "no karmic": _with(karmic=None),
    "no turn cap": _with(turn_cap_s=None),
    "a turn cap of 0": _with(turn_cap_s=0),
    "a mirror with no pairs": _with(teams=NAME_ONLY, mirror={"pairs": []}),
    "a mirror of five pairs": _with(teams=NAME_ONLY, mirror={"pairs": [*PAIRS] * 2 + [PAIRS[0]]}),
    "a mirror key that isn't pairs": _with(teams=NAME_ONLY, mirror={"pairs": PAIRS, "seed": 1}),
    "a mirror pair without its kit": _with(teams=NAME_ONLY, mirror={"pairs": [{**PAIRS[0], "kit": None}, PAIRS[1]]}),
    "a mirror pair with one name": _with(
        teams=NAME_ONLY, mirror={"pairs": [{**PAIRS[0], "names": {"A": "Ash"}}, PAIRS[1]]}
    ),
    "a mirror pair with an unknown key": _with(teams=NAME_ONLY, mirror={"pairs": [{**PAIRS[0], "hp": 9}, PAIRS[1]]}),
    "a mirror pair with an empty build": _with(
        teams=NAME_ONLY, mirror={"pairs": [{**PAIRS[0], "build": ""}, PAIRS[1]]}
    ),
    "a mirror twin with a uuid": _with(
        teams={**NAME_ONLY, "A": [{"name": "Ash", "uuid": "3ed74f06-3c60-42dc-83f6-f034cb47c679"}, {"name": "Cedar"}]},
        mirror={"pairs": PAIRS},
    ),
}


@pytest.mark.parametrize("why", sorted(BAD))
def test_a_bad_arena_file_is_refused_by_the_validator_and_the_schema(why):
    a = BAD[why]
    assert arena.validate(a, a["name"]) != []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(a, ARENA_SCHEMA)


def test_validator_only_checks_a_pairs_kit_race_and_todo_spawns():
    """The mod's arena.setup refuses a kit whose race part is for another race than its pair's; spawns are TODO only
    in a draft. Cross-field rules: the validator, not the schema."""
    other = _with(teams=NAME_ONLY, mirror={"pairs": [{**PAIRS[0], "kit": {"race": {"race": "other"}}}, PAIRS[1]]})
    none = _with(teams=NAME_ONLY, mirror={"pairs": [PAIRS[0], {**PAIRS[1], "kit": {}}]})
    todo = _with(location={"level": "WLD_Main_A", "spawns": {"A": None, "B": None}})
    assert arena.validate(other, READY["name"]) == [
        "mirror.pairs[0].kit's race part is for race other, the pair's race is r"
    ]
    assert arena.validate(none, READY["name"]) == [
        "mirror.pairs[1].kit's race part is for race None, the pair's race is r"
    ]
    assert arena.validate(todo, READY["name"]) == [
        "location.spawns.A must be [x, y, z] in metres",
        "location.spawns.B must be [x, y, z] in metres",
    ]
    for a in (other, none, todo):
        jsonschema.validate(a, ARENA_SCHEMA)


def test_validator_only_checks():
    same = _with(location={"level": "WLD_Main_A", "spawns": {"A": [1, 0, 0], "B": [1, 0, 0]}})
    assert any("same point" in e for e in arena.validate(same, READY["name"]))
    twice = _with(teams={"A": [{"name": "Karlach"}], "B": [{"name": "Karlach"}]})
    assert any("listed twice" in e for e in arena.validate(twice, READY["name"]))
    u = "3ed74f06-3c60-42dc-83f6-f034cb47c679"
    same_uuid = _with(teams={"A": [{"name": "Shadowheart", "uuid": u}], "B": [{"name": "Wyll", "uuid": u}]})
    assert any(f"uuid {u} is listed twice" in e for e in arena.validate(same_uuid, READY["name"]))
    assert any("file's name" in e for e in arena.validate(READY, "other_name"))
    assert any("reloads" in e for e in arena.validate(_with(condition="rules+reloads:2"), READY["name"]))
    assert any("condition" in e for e in arena.validate(_with(condition="coaching"), READY["name"]))
    assert arena.validate(_with(condition="rules+wiki"), READY["name"]) == []
    assert arena.validate([], "x") == ["an arena file is a JSON object"]


def test_a_mirror_arena_with_name_only_teams_in_pair_order_is_valid_and_the_validator_checks_the_order():
    assert arena.validate(MIRROR, READY["name"]) == []
    jsonschema.validate(MIRROR, ARENA_SCHEMA)
    order = _with(teams={**NAME_ONLY, "B": [{"name": "Dune"}, {"name": "Birch"}]}, mirror={"pairs": PAIRS})
    other = _with(teams={**NAME_ONLY, "A": [{"name": "Ash"}, {"name": "Elm"}]}, mirror={"pairs": PAIRS})
    short = _with(teams={**NAME_ONLY, "A": [{"name": "Ash"}]}, mirror={"pairs": PAIRS})
    for bad in (order, other, short):  # the schema can't say it: the validator does
        jsonschema.validate(bad, ARENA_SCHEMA)
        assert any("must name the mirror's pairs in order" in e for e in arena.validate(bad, READY["name"]))
    template = _with(
        teams={**NAME_ONLY, "A": [{"name": "Ash", "template": "t"}, {"name": "Cedar"}]}, mirror={"pairs": PAIRS}
    )
    assert any("names only" in e for e in arena.validate(template, READY["name"]))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(template, ARENA_SCHEMA)


def test_the_spawns_must_stand_both_teams_on_the_same_terms_unless_the_arena_is_a_draft():
    """The mod's F2 rule (arena.setup refuses the same): under 0.3 m apart in height, and every distance across the
    two teams' lines (along x) the same both ways within 0.1 m. The schema can't say it: the validator does."""

    def at(a, b, **kw):
        return _with(location={"level": "WLD_Main_A", "spawns": {"A": a, "B": b}}, **kw)

    assert arena.validate(at([0, 0, 0], [0, 0.29, 20]), READY["name"]) == []
    assert arena.validate(at([0, 0, 0], [0.05, 0, 20]), READY["name"]) == []  # within 0.1 m both ways
    high = at([229.8, 5.1, 268.2], [229.8, 5.6, 278.7])  # crash-companions-v1's
    assert arena.validate(high, READY["name"]) == [
        "location.spawns A and B are 0.50 m apart in height (y); they must be under 0.3 m"
    ]
    assert any("0.30 m apart in height" in e for e in arena.validate(at([0, 0, 0], [0, 0.3, 20]), READY["name"]))
    along = arena.validate(at([0, 0, 0], [20, 0, 0]), READY["name"])  # apart along x: A's 1st nearer B's 2nd
    assert len(along) == 1 and "they must be the same within 0.1 m" in along[0]
    assert arena.validate(at([0, 0, 0], [0, 0, 20]), READY["name"]) == []
    for bad in (high, at([0, 0, 0], [20, 0, 0])):
        jsonschema.validate(bad, ARENA_SCHEMA)
        assert arena.validate({**bad, "draft": "spots to read live"}, READY["name"]) == []  # a draft's placeholders
    one = at([0, 0, 0], [0.4, 0, 20], teams={"A": [{"name": "a"}], "B": [{"name": "b"}]})
    assert arena.validate(one, READY["name"]) == []  # one a side: one distance, the same both ways
    assert arena.validate(at([0, 0, 0], [0.4, 0, 20]), READY["name"]) != []  # two a side: it differs
    mirror = {**MIRROR, "location": {"level": "WLD_Main_A", "spawns": {"A": [0, 0, 0], "B": [0.4, 0, 20]}}}
    mirror["teams"] = {s: NAME_ONLY[s][:1] for s in ("A", "B")}
    mirror["mirror"] = {"pairs": PAIRS[:1]}
    assert arena.validate(mirror, READY["name"]) == []  # a mirror's line is its pairs


def test_load_names_the_file_and_every_problem(tmp_path):
    p = tmp_path / "draft_open_field.json"
    p.write_text(json.dumps({**READY, "seed": -1, "round_cap": 0}))
    with pytest.raises(arena.ArenaError, match=r"draft_open_field\.json: .*seed.*round_cap"):
        arena.load(p)
    p.write_text("{")
    with pytest.raises(arena.ArenaError, match="draft_open_field.json"):
        arena.load(p)


def test_arena_files_and_modules_are_outside_the_suite_digest(tmp_path):
    assert suite.check() == []
    assert not [f for f in suite.suite_files() if "arena" in f]
    root = tmp_path / "bench"
    for rel in suite.suite_files():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PKG / rel, root / rel)
    before = suite.digest(root)
    shutil.copytree(PKG / "arenas", root / "arenas")
    shutil.copy(PKG / "src" / "bg3bench" / "arena.py", root / "src" / "bg3bench" / "arena.py")
    assert suite.digest(root) == before


# ---------------------------------------------------------------------------------------------------- the event schema


def test_the_fixture_match_fits_the_facts_schemas_and_grades():
    recs = facts.load(FIXTURES / "wipe_a.facts.jsonl")
    for r in recs:
        if str(r.get("ev", "")).startswith("arena_"):
            jsonschema.validate(r, EVENT_SCHEMA)
        else:
            jsonschema.validate(r, FACTS_SCHEMA)
    g = arena.grade(recs, arena.with_defaults(DRAFT))
    assert g == {
        "arena": "draft_open_field",
        "seed": arena.with_defaults(DRAFT)["seed"],  # the seed it was played and checked on (pairings compare it)
        "valid": True,
        "invalid_reason": None,
        "sides": {"A": "pi/model-x", "B": "pi/model-y"},
        "result": "A",
        "reason": "wipe",
        "forfeit": None,
        "fault": None,
        "over_budget": None,
        "rounds": 2,
        "hp_left": {"A": 0.75, "B": 0},
        "turns": {"A": 2, "B": 1},
        "forced": {"A": 0, "B": 0},
        "r1_pattern": "A1|B1",
        "first_actor": "A",
        "favoured": None,  # no pair in round 1
        "round_first": [{"round": 1, "first": "A", "how": None}, {"round": 2, "first": "A", "how": None}],
        "luck": None,
    }


@pytest.mark.parametrize(
    ("pattern", "want"),
    [
        ("A1|B2|A1", {"side": "B", "why": "pair"}),  # seed 1: B's twins back to back
        ("B1|A2|B1", {"side": "A", "why": "pair"}),  # seed 3
        ("B1|A1|B1|A1", None),  # seed 5: no pair (an alternation rule's every round 1)
        ("A2|B2", None),  # both have a pair
        (None, None),
        ("", None),
    ],
)
def test_the_favoured_side_is_the_pair_side_read_from_round_1s_order_alone(pattern, want):
    # REVIEW-7 (a) / seat fix: the pair side when only one side has a pair, else none; fixed before any result
    assert arena.favoured(pattern) == want


@pytest.mark.parametrize(
    ("pattern", "want"), [("A1|B2|A1", "A"), ("B1|A1|B1|A1", "B"), ("B2|A2", "B"), (None, None), ("", None)]
)
def test_the_first_actor_is_round_1s_first_phase_side(pattern, want):
    assert arena.first_actor(pattern) == want


def test_round_first_reads_the_mods_arena_round_records_else_each_rounds_first_arena_turn():
    # seat fix O3/O3a audit: seed-7 m1 r2 began with A's phase passed whole (Cedar down), B's arena_turn first
    def turn(side, rnd):
        return ev(1, "arena_turn", side=side, character="x", members=["x"], names=["x"], round=rnd)

    marked = [
        ev(1, "arena_round", round=1, first="A", how="turn"),
        turn("A", 1),
        turn("B", 1),
        ev(2, "arena_round", round=2, first="A", how="passed"),
        turn("B", 2),
    ]
    assert arena.round_first(marked) == [
        {"round": 1, "first": "A", "how": "turn"},
        {"round": 2, "first": "A", "how": "passed"},
    ]
    old = [r for r in marked if r["ev"] != "arena_round"]  # recorded before the mod wrote arena_round
    assert arena.round_first(old) == [
        {"round": 1, "first": "A", "how": None},
        {"round": 2, "first": "B", "how": None},
    ]
    assert arena.round_first([]) == []


def test_round_1s_phase_pattern_counts_each_phases_characters_in_order():
    # fairness audit 2026-11-03 'Why B won both': seed 1 gave Cedar (A) | Dune + Birch (B, one phase) | Ash (A)
    def turn(side, members, rnd):
        return ev(1, "arena_turn", side=side, character=members[0], members=members, names=members, round=rnd)

    recs = [turn("A", ["Cedar"], 1), turn("B", ["Dune", "Birch"], 1), turn("A", ["Ash"], 1), turn("B", ["Dune"], 2)]
    assert arena.r1_pattern(recs) == "A1|B2|A1"
    assert arena.r1_pattern([turn("A", ["Ash"], 2)]) == "A1"  # the lowest round there is
    assert arena.r1_pattern([]) is None


TURN = ev(
    1,
    "arena_turn",
    side="A",
    character="Karlach",
    members=["Karlach"],
    names=["Karlach"],
    round=1,
    phase=1,
    group="g",
    shared=True,
)


@pytest.mark.parametrize(
    "rec",
    [
        ev(1, "arena_turn", side="C", character="Karlach", round=1),
        ev(1, "arena_turn", side="A", character="Karlach", round=0),
        ev(1, "arena_turn", side="A", character="Karlach", round=1),  # no phase fields
        {k: v for k, v in TURN.items() if k != "names"},
        {
            **TURN,
            "members": [],
        },
        {
            **TURN,
            "phase": 0,
        },
        {
            **TURN,
            "shared": "yes",
        },
        ev(1, "arena_end", winner="A", rounds=2, hp_left_A=1.5, hp_left_B=0, reason="wipe"),
        ev(1, "arena_end", winner="A", rounds=2, hp_left_A=1, hp_left_B=0, reason="timeout"),
        ev(1, "arena_start", arena="x", seed=1, teams={"A": []}, sides={"A": "m", "B": "n"}),
        ev(1, "arena_turn_forced", side="A"),
        ev(1, "arena_end", winner="B", rounds=2, hp_left_A=1, hp_left_B=1, reason="forfeit"),  # no forfeit, cause
        ev(1, "arena_end", winner="B", rounds=2, hp_left_A=1, hp_left_B=1, reason="forfeit", forfeit="A", cause="x"),
        ev(1, "arena_end", winner="A", rounds=20, hp_left_A=1, hp_left_B=0.5, reason="round_cap"),  # a cap's winner
        ev(1, "arena_end", winner="A", rounds=2, hp_left_A=1, hp_left_B=0, reason="wipe", detail=3),
        ev(1, "arena_reaction_unowned", round=2),
        ev(1, "arena_reaction_unowned", round=-1, detail="Wolf | Shield"),
    ],
)
def test_the_event_schema_refuses_malformed_referee_events(rec):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(rec, EVENT_SCHEMA)


@pytest.mark.parametrize("name", ["arena_group_end_written", "arena_end_written"])
def test_a_group_end_the_mod_wrote_fits_both_schemas_under_its_name_and_its_old_one(name):
    # bp-live 2026-11-03: arena_end_written after every round read as a match end; it is a turn group's end (the mod's
    # RequestedEndTurn write, each round the game keeps a shared group open), renamed; old records stay valid
    rec = ev(1, name, round=2, group="g1", character="Ash", members=["Ash", "Birch"], how="requested_end_write")
    jsonschema.validate(rec, EVENT_SCHEMA)
    jsonschema.validate(rec, FACTS_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({k: v for k, v in rec.items() if k != "how"}, EVENT_SCHEMA)


def test_a_pass_with_its_death_save_and_a_readmission_fit_both_schemas():
    # arena/downed-save 135075ef: a downed member is passed after its turn's death save (death_save), and a passed
    # member that stands up with its turn back is readmitted
    passed = ev(1, "arena_turn_passed", side="A", character="Ash", why="downed", round=3, death_save="rolled")
    back = {**ev(2, "arena_turn_readmitted", side="A", character="Ash", round=3, why="stood up"), "name": "Ash"}
    for rec in (passed, back, {**passed, "death_save": "none in time"}):
        jsonschema.validate(rec, EVENT_SCHEMA)
        jsonschema.validate(rec, FACTS_SCHEMA)
    for bad in ({**passed, "death_save": "maybe"}, {k: v for k, v in back.items() if k != "round"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, EVENT_SCHEMA)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, FACTS_SCHEMA)


def test_a_stand_up_fits_both_schemas():
    # arena/prone-rule: a prone member stood up at its phase start by the mod (REVIEW-12 (a))
    up = ev(
        1,
        "arena_stood_up",
        side="B",
        character="Birch",
        round=2,
        status="Prone",
        movement_before=9,
        movement_after=4.5,
        movement_max=9,
        deducted=4.5,
        game_took=False,
    )
    for rec in (up, {**up, "deducted": 0, "game_took": True}):
        jsonschema.validate(rec, EVENT_SCHEMA)
        jsonschema.validate(rec, FACTS_SCHEMA)
    for bad in ({**up, "game_took": "no"}, {k: v for k, v in up.items() if k != "movement_before"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, EVENT_SCHEMA)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, FACTS_SCHEMA)


def test_the_event_schema_takes_a_declined_unowned_prompt_and_an_end_the_runner_came_after():
    phase = {"members": ["Karlach", "Gale"], "names": ["Karlach", "Gale"], "round": 1, "phase": 2, "shared": True}
    jsonschema.validate(ev(1, "arena_turn", side="A", character="Karlach", **phase), EVENT_SCHEMA)
    jsonschema.validate(
        ev(1, "arena_reaction_unowned", round=2, detail="Wolf | Shield | Choose how to react."), EVENT_SCHEMA
    )
    late = "the runner's end came after it: wall_cap"
    end = ev(1, "arena_end", winner="A", rounds=2, hp_left_A=1, hp_left_B=0, reason="wipe", detail=late)
    jsonschema.validate(end, EVENT_SCHEMA)


# ---------------------------------------------------------------------------------------------------- the grader


@pytest.mark.parametrize(
    ("winner", "reason", "hp", "result"),
    [
        ("A", "wipe", (0.4, 0.0), "A"),
        ("B", "wipe", (0.0, 0.1), "B"),
        (None, "round_cap", (0.3, 0.9), "draw"),  # the HP is recorded, never a tiebreak
        (None, "wall_cap", (0.5, 0.2), "draw"),
        (None, "round_cap", (0.5, 0.5), "draw"),
    ],
)
def test_grade_reads_the_result(winner, reason, hp, result):
    g = arena.grade(match(winner, reason, hp), ARENA)
    assert (g["valid"], g["result"], g["reason"], g["hp_left"]) == (True, result, reason, {"A": hp[0], "B": hp[1]})
    assert g["sides"] == SIDES


def _start(**kw):
    return ev(1, "arena_start", **{"arena": ARENA["name"], "seed": ARENA["seed"], "teams": TEAMS, "sides": SIDES, **kw})


END = {"winner": "A", "rounds": 2, "hp_left_A": 0.5, "hp_left_B": 0.0, "reason": "wipe"}
INVALID = {
    "no_start": {"start": {"v": 1, "kind": "mark", "t": 1.0, "mark": "settings", "detail": {}}},
    "wrong_arena": {"start": _start(arena="other")},
    "unseeded": {"start": _start(seed=7)},
    "scripted": {"start": _start(sides={"A": "scripted/none", "B": "pi/model-y"})},
    "off_side_turn": {"turns": [("A", "Wyll", 1)]},
    "no_end": {"ends": []},
    "past_round_cap": {"rounds": 21},
    "abort": {"winner": None, "reason": "abort", "hp": (0.5, 0.5)},
    "fault": {"winner": None, "reason": "fault", "hp": (0.5, 0.5)},
    "client_fault": {"winner": None, "reason": "client_fault", "failed": "B", "cause": "api_error", "hp": (0.5, 0.5)},
}
INCONSISTENT = {
    "wipe with the loser standing": {"hp": (0.5, 0.1)},
    "wipe without a winner": {"winner": None},
    "a round cap with a winner": {"winner": "A", "reason": "round_cap", "hp": (0.5, 0.2)},
    "a wall cap with a winner": {"winner": "B", "reason": "wall_cap", "hp": (0.2, 0.5)},
    "a forfeit without a side": {"winner": "B", "reason": "forfeit", "cause": "client_exit"},
    "a forfeit without a cause": {"winner": "B", "reason": "forfeit", "forfeit": "A"},
    "a forfeit won by the forfeiter": {"winner": "A", "reason": "forfeit", "forfeit": "A", "cause": "client_exit"},
    "a forced-turns forfeit with no forced turns": {
        "winner": "B",
        "reason": "forfeit",
        "forfeit": "A",
        "cause": "forced_turns",
    },
    "two ends": {"ends": [END, END]},
    "an unknown reason": {"reason": "timeout"},
    "an hp over 1": {"hp": (1.5, 0.0)},
    "no rounds": {"ends": [{k: v for k, v in END.items() if k != "rounds"}]},
    "a bad winner": {"winner": "C"},
}


@pytest.mark.parametrize("why", sorted(INVALID))
def test_grade_invalid_matches(why):
    g = arena.grade(match(**INVALID[why]), ARENA)
    assert (g["valid"], g["invalid_reason"], g["result"]) == (False, why, None)


@pytest.mark.parametrize("members", [["Karlach", "Wyll"], None])
def test_grade_a_phase_with_a_member_of_the_other_team_is_an_off_side_turn(members):
    g = arena.grade(match(turns=[("A", "Karlach", 1, members), ("B", "Wyll", 1)]), ARENA)
    assert (g["valid"], g["invalid_reason"]) == (False, "off_side_turn")


def test_grade_a_summon_on_neither_team_counts_on_its_phases_side():
    turns = [("A", "Karlach", 1, ["Karlach", "Wolf"]), ("B", "Owl", 1, ["Owl"]), ("B", "Wyll", 1)]
    g = arena.grade(match(turns=turns), ARENA)
    assert (g["valid"], g["turns"]) == (True, {"A": 1, "B": 2})


def test_grade_counts_a_phase_of_several_members_as_one_turn():
    g = arena.grade(
        match(turns=[("A", "Karlach", 1, ["Karlach", "Gale"]), ("B", "Wyll", 1, ["Wyll", "Halsin"])]), ARENA
    )
    assert (g["valid"], g["turns"]) == (True, {"A": 1, "B": 1})


@pytest.mark.parametrize("why", sorted(INCONSISTENT))
def test_grade_refuses_an_end_that_contradicts_itself(why):
    g = arena.grade(match(**INCONSISTENT[why]), ARENA)
    assert (g["valid"], g["invalid_reason"]) == (False, "inconsistent_end")


def test_grade_wrong_teams_or_two_starts_is_the_wrong_arena():
    swapped = {"A": TEAMS["B"], "B": TEAMS["A"]}
    assert arena.grade(match(start=_start(teams=swapped)), ARENA)["invalid_reason"] == "wrong_arena"
    assert arena.grade(match(start=_start(sides={"A": "pi/x"})), ARENA)["invalid_reason"] == "wrong_arena"
    twice = match()
    twice.insert(2, _start())
    assert arena.grade(twice, ARENA)["invalid_reason"] == "wrong_arena"


def test_grade_counts_turns_up_to_the_end_only():
    recs = match()
    recs.insert(-1, ev(60, "arena_turn", side="B", character="Not On Any Team", round=3))
    g = arena.grade(recs, ARENA)
    assert (g["valid"], g["turns"]) == (True, {"A": 2, "B": 1})


@pytest.mark.parametrize(
    ("record", "why"),
    [
        ({"outcome": "runner_error"}, "runner_error"),
        ({"outcome": "reached", "harness_failures": 1}, "harness_failure"),
        ({"outcome": "reached", "operator": "stop"}, "operator"),
        ({"outcome": "reached", "off_route": "B: openrouter"}, "off_route"),
        ({"outcome": "reached", "game_settings": {"seed": {"applied": {"seed": 5}}}}, "unseeded"),
    ],
)
def test_grade_takes_the_run_records_invalid_rules(record, why):
    g = arena.grade(match(), ARENA, record)
    assert (g["valid"], g["invalid_reason"], g["result"]) == (False, why, None)


def test_grade_a_valid_run_record_leaves_the_facts_verdict():
    rec = {"outcome": "reached", "game_settings": {"seed": {"applied": {"seed": ARENA["seed"]}}}}
    assert arena.grade(match(), ARENA, rec)["valid"] is True
    assert arena.grade(match(rounds=21), ARENA, rec)["invalid_reason"] == "past_round_cap"


# ---------------------------------------------------------------------------------------------------- forfeits

SIX = [
    ("A", "Karlach", 1),
    ("B", "Wyll", 1),
    ("A", "Gale", 2),
    ("B", "Halsin", 2),
    ("A", "Astarion", 3),
    ("B", "Wyll", 3),
]
FORFEIT_A = {"winner": "B", "reason": "forfeit", "forfeit": "A", "hp": (1.0, 1.0)}


def test_three_forced_turns_in_a_row_forfeit():
    g = arena.grade(match(turns=SIX, forced=(0, 2, 4), cause="forced_turns", **FORFEIT_A), ARENA)
    assert (g["valid"], g["result"], g["reason"]) == (True, "B", "forfeit")
    assert (g["forfeit"], g["forced"]) == ({"side": "A", "cause": "forced_turns"}, {"A": 3, "B": 0})


def test_the_other_sides_turns_do_not_break_a_streak_but_a_played_turn_does():
    # A's turns 0, 2, 4 forced with B's turns between: three in a row. A's 0, 2 forced and 4 played: no forfeit.
    assert arena.grade(match(turns=SIX, forced=(0, 1, 2, 4), cause="forced_turns", **FORFEIT_A), ARENA)["valid"]
    g = arena.grade(match(turns=[*SIX, ("A", "Gale", 4)], forced=(0, 2, 6), cause="forced_turns", **FORFEIT_A), ARENA)
    assert g["invalid_reason"] == "inconsistent_end"


def test_a_match_that_goes_on_past_a_forfeit_contradicts_itself():
    g = arena.grade(match(turns=SIX, forced=(0, 2, 4), hp=(0.0, 0.5), winner="B"), ARENA)
    assert g["invalid_reason"] == "inconsistent_end"
    other = {"winner": "A", "reason": "forfeit", "forfeit": "B", "cause": "client_exit", "hp": (1.0, 1.0)}
    assert arena.grade(match(turns=SIX, forced=(0, 2, 4), **other), ARENA)["invalid_reason"] == "inconsistent_end"


def test_a_client_exit_forfeit_needs_no_forced_turns():
    g = arena.grade(match(winner="A", reason="forfeit", forfeit="B", cause="client_exit", hp=(0.6, 0.9)), ARENA)
    assert (g["valid"], g["result"], g["forfeit"]) == (True, "A", {"side": "B", "cause": "client_exit"})


def test_a_client_fault_is_invalid_no_forfeit_and_names_the_failed_side():
    """Owner D459 (fairness audit S1-1): a side's client failing past its resume (an API error, our MCP server, a
    hang) is no result: invalid, not a forfeit loss. A forfeit with the old cause client_fault contradicts itself."""
    fault = {"winner": None, "reason": "client_fault", "failed": "B", "cause": "mcp_failed", "hp": (0.6, 0.9)}
    g = arena.grade(match(**fault), ARENA)
    assert (g["valid"], g["invalid_reason"], g["result"], g["forfeit"]) == (False, "client_fault", None, None)
    assert g["fault"] == {"side": "B", "cause": "mcp_failed"}
    old = arena.grade(match(winner="A", reason="forfeit", forfeit="B", cause="client_fault", hp=(0.6, 0.9)), ARENA)
    assert old["invalid_reason"] == "inconsistent_end"


NOT_OVER = {"over_budget": 0, "over_budget_rate": 0.0, "over_budget_flag": False}


def test_failures_count_each_models_own_client_faults_over_all_its_matches():
    ok = {"valid": True, "fault": None}
    ms = [
        {**ok, "sides": {"A": "x", "B": "y"}},
        {"valid": False, "fault": {"side": "B", "cause": "api_error"}, "sides": {"A": "x", "B": "y"}},
        {"valid": False, "fault": {"side": "A", "cause": "api_error"}, "sides": {"A": "y", "B": "x"}},
        {"valid": False, "fault": {"side": "B", "cause": "agent_hang"}, "sides": {"A": "x", "B": "y"}},
        {"valid": False, "fault": None, "sides": {"A": "y", "B": "y"}},  # the game's fault, y against itself
    ]
    assert arena.failures(ms) == [
        {"player": "x", "n_matches": 4, "client_faults": 0, "causes": {}, "rate": 0.0, **NOT_OVER},
        {
            "player": "y",
            "n_matches": 5,
            "client_faults": 3,
            "causes": {"api_error": 2, "agent_hang": 1},
            "rate": 0.6,
            **NOT_OVER,
        },
    ]


def test_failures_count_each_models_over_budget_matches_and_flag_a_rate_above_a_few_percent():
    ok = {"valid": True, "fault": None, "over_budget": None, "sides": {"A": "x", "B": "y"}}
    over = {**ok, "valid": False, "over_budget": ["B"]}
    by = {r["player"]: r for r in arena.failures([over, *[ok] * 9])}
    assert (by["y"]["over_budget"], by["y"]["over_budget_rate"], by["y"]["over_budget_flag"]) == (1, 0.1, True)
    assert (by["x"]["over_budget"], by["x"]["over_budget_rate"], by["x"]["over_budget_flag"]) == (0, 0.0, False)
    by = {r["player"]: r for r in arena.failures([over, *[ok] * 19])}
    assert (by["y"]["over_budget_rate"], by["y"]["over_budget_flag"]) == (0.05, False)  # at the line: not above it


def test_a_forced_turn_must_name_a_turn_it_can_have_ended():
    recs = match(turns=SIX[:2])
    recs.insert(4, ev(11.5, "arena_turn_forced", side="B", character="Halsin"))
    assert arena.grade(recs, ARENA)["invalid_reason"] == "inconsistent_turns"
    twice = match(turns=SIX[:2], forced=(1,))
    twice.insert(5, ev(11.6, "arena_turn_forced", side="B", character="Wyll"))
    assert arena.grade(twice, ARENA)["invalid_reason"] == "inconsistent_turns"
    before = match()
    before.insert(2, ev(1, "arena_turn_forced", side="A", character="Karlach"))
    assert arena.grade(before, ARENA)["invalid_reason"] == "inconsistent_turns"


def smoke(name):
    """A live match's facts (arena-v0.1, 2026-11-05): Cedar's 90 s cap, Dune and Ash downed and passed, so Birch's
    phase opened at once and arena_turn_forced came 0.2 s after it. Rows cut: arena_surfaces, died."""
    return facts.load(FIXTURES / f"{name}.facts.jsonl")


def with_forced(recs, t, side, character):
    """`recs` with one more arena_turn_forced, in time order."""
    return sorted([*recs, ev(t, "arena_turn_forced", side=side, character=character)], key=lambda r: r["t"])


def opened(recs, character, rnd):
    return next(
        r["t"] for r in recs if r.get("ev") == "arena_turn" and (r["character"], r["round"]) == (character, rnd)
    )


@pytest.mark.parametrize(("name", "winner"), [("forced_late_b", "B"), ("forced_late_a", "A")])
def test_a_forced_turn_written_after_the_next_phase_opened_counts_for_its_own_turn(name, winner):
    g = arena.grade(smoke(name), arena.load_named("arena-v0.1"))
    assert (g["valid"], g["invalid_reason"], g["result"]) == (True, None, winner)
    assert g["forced"] == {"A": 1, "B": 0}


def test_a_forced_turn_after_its_end_could_settle_is_inconsistent():
    recs = [r for r in smoke("forced_late_b") if r.get("ev") != "arena_turn_forced"]
    birch = opened(recs, "Birch", 1)  # the phase after Ash's round 1 turn
    spec = arena.load_named("arena-v0.1")
    assert arena.grade(with_forced(recs, birch + arena.CAP_END_S, "A", "Ash"), spec)["valid"]
    late = arena.grade(with_forced(recs, birch + arena.CAP_END_S + 1, "A", "Ash"), spec)
    assert late["invalid_reason"] == "inconsistent_turns"


def test_a_forced_turn_on_a_member_with_no_open_turn_is_inconsistent():
    recs = [r for r in smoke("forced_late_b") if r.get("ev") != "arena_turn_forced"]
    spec = arena.load_named("arena-v0.1")
    closed = with_forced(recs, opened(recs, "Cedar", 2) + 1, "B", "Dune")  # Dune's turn ended three phases ago
    assert arena.grade(closed, spec)["invalid_reason"] == "inconsistent_turns"
    wrong_side = with_forced(recs, opened(recs, "Birch", 2) + 0.2, "B", "Cedar")
    assert arena.grade(wrong_side, spec)["invalid_reason"] == "inconsistent_turns"


def test_a_second_forced_event_for_one_turn_is_inconsistent():
    recs = smoke("forced_late_b")
    twice = with_forced(recs, opened(recs, "Birch", 2) + 0.3, "A", "Cedar")
    assert arena.grade(twice, arena.load_named("arena-v0.1"))["invalid_reason"] == "inconsistent_turns"


def test_a_forced_turn_from_a_phase_that_never_ran_is_inconsistent():
    recs = smoke("forced_late_b")
    spec = arena.load_named("arena-v0.1")
    passed = with_forced(recs, opened(recs, "Birch", 2) + 0.2, "A", "Ash")  # the mod passed Ash in round 2
    assert arena.grade(passed, spec)["invalid_reason"] == "inconsistent_turns"
    unopened = with_forced(recs, opened(recs, "Cedar", 1) + 1, "B", "Birch")  # before Birch's first phase
    assert arena.grade(unopened, spec)["invalid_reason"] == "inconsistent_turns"


def test_the_grader_allows_a_cap_its_ends_longest_time():
    assert arena.CAP_END_S >= END_SETTLE_S + 2 * EVAL_TIMEOUT


def test_a_forced_turn_written_late_keeps_its_place_in_the_forfeit_count():
    recs = match(turns=SIX, forced=(0, 2, 4), cause="forced_turns", **FORFEIT_A)
    first = next(r for r in recs if r.get("ev") == "arena_turn_forced")
    first["t"] += 0.7  # the first of the three, written once B's phase had opened
    g = arena.grade(sorted(recs, key=lambda r: r["t"]), ARENA)
    assert (g["valid"], g["forfeit"]) == (True, {"side": "A", "cause": "forced_turns"})


def test_forced_and_forfeit_events_fit_the_event_schema():
    recs = match(turns=SIX, forced=(0, 2, 4), cause="forced_turns", **FORFEIT_A)
    for r in recs:
        if str(r.get("ev", "")).startswith("arena_"):
            jsonschema.validate(r, EVENT_SCHEMA)


# ---------------------------------------------------------------------------------------------------- luck

UUIDS = {"A": ["a-1", "a-2"], "B": ["b-1"]}


def roll(roller, natural, **kw):
    return {"roller": roller, "natural": natural, **kw}


def test_luck_is_each_sides_z_over_its_own_rolls():
    rolls = [
        roll("a-1", 20),
        roll("a-2", 20),
        roll("b-1", 1),
        roll("a-1", 1, advantage=True),  # not uniform: left out
        roll("b-1", None),  # unseen: left out
        roll("summon-9", 7),  # neither side
    ]
    out = arena.luck(rolls, UUIDS)
    assert out == {
        "A": {"n": 2, "z": round(stats.luck_z([20, 20]), 4)},
        "B": {"n": 1, "z": round(stats.luck_z([1]), 4)},
        "other": 1,
    }
    assert out["A"]["z"] > 0 > out["B"]["z"]


def test_grade_reports_luck_with_rolls_and_uuids():
    start = _start(uuids=UUIDS)
    jsonschema.validate(start, EVENT_SCHEMA)
    g = arena.grade(match(start=start), ARENA, rolls=[roll("b-1", 15)])
    assert g["luck"] == {"A": {"n": 0, "z": 0.0}, "B": {"n": 1, "z": round(stats.luck_z([15]), 4)}, "other": 0}
    assert arena.grade(match(), ARENA, rolls=[roll("b-1", 15)])["luck"] is None  # no uuids: can't attribute
    assert arena.grade(match(start=start), ARENA)["luck"] is None
