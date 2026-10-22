"""bg3bench.scenario_v1: validation, status, CLI conflicts and the series check of a v1 scenario (moved from
runner/test_scenarios.py; the play.py wiring and the bundled files' test stay there)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from bg3bench import scenario_v1 as R

REPO = Path(__file__).resolve().parents[3]
# The legacy v1 set (scenarios/README.md): closed until Phase D ends; packages/bench/scenarios/ (v2) is canonical.
LEGACY = {"nautiloid": "nautiloid", "crash_site_grove": None, "act1_underdark": None, "act2_last_light": None}

MILESTONES = ["nautiloid_escaped", "beach", "grove"]  # stand-ins for bg3ai.milestones' ids
ROLEPLAY_MODES = ("strict", "light")  # play.py's
DIFFICULTIES = ("Explorer", "Balanced", "Tactician")  # play.py's (new_game's)
START = "Claude-33912620918__SCN-beach"


def save_scenario(**over) -> dict:
    sc = {
        "name": "beach",
        "title": "Beach → Grove",
        "start": {
            "kind": "save",
            "save": START,
            "save_sha1": None,
            "note": {
                "where": "Ravaged Beach, out of combat.",
                "objective": "Find a Cure.",
                "party": ["Claude (Paladin)", "Shadowheart (Cleric)"],
                "journal": ["Find a Cure"],
            },
        },
        "until": "grove",
        "runs": 2,
        "caps": {"run_hours": 2, "max_cost": 40, "max_tokens": None},
        "profile": {
            "character": {"name": "Claude", "brief": "a devotion paladin", "roleplay": "strict"},
            "difficulty": "Explorer",
        },
    }
    sc.update(over)
    return sc


def problems(sc: dict, name: str = "beach") -> list[str]:
    return R.validate_scenario(sc, name, MILESTONES, ROLEPLAY_MODES, DIFFICULTIES)


class Format(unittest.TestCase):
    def test_a_valid_save_scenario(self):
        self.assertEqual(problems(save_scenario()), [])
        self.assertEqual(problems(save_scenario(start=dict(save_scenario()["start"], save=None))), [])  # placeholder

    def test_invalid_scenarios(self):
        def first(sc, name="beach"):
            errs = problems(sc, name)
            self.assertTrue(errs, sc)
            return " | ".join(errs)

        st = save_scenario()["start"]
        note = st["note"]
        self.assertIn("not a milestone", first(save_scenario(until="moon")))
        self.assertIn("must be the file name", first(save_scenario(), "other"))
        self.assertIn("unknown keys: extra", first(save_scenario(extra=1)))
        for gs in ({"seed": -1}, {"seed": 2**31}, {"seed": True}, {"seed": "1"}, {"resolution": "1920x1080"}, []):
            self.assertIn("game_settings must be", first(save_scenario(game_settings=gs)))
        self.assertEqual(problems(save_scenario(game_settings={"seed": 12345}), "beach"), [])
        # a resolved v2 scenario (bg3bench.scenario.resolve): the difficulty twice, Karmic Dice off, a timing
        resolved = {"seed": 7, "difficulty": "Explorer", "karmic_dice": False}
        self.assertEqual(problems(save_scenario(game_settings=resolved, timing="turn_based")), [])
        self.assertIn("karmic_dice must be false", first(save_scenario(game_settings=dict(resolved, karmic_dice=True))))
        self.assertIn(
            "must equal profile.difficulty", first(save_scenario(game_settings=dict(resolved, difficulty="Balanced")))
        )
        self.assertIn("timing must be", first(save_scenario(timing="paused")))
        self.assertIn("run_hours is required", first(save_scenario(caps={"max_cost": 5})))
        self.assertIn("caps must be", first(save_scenario(caps={"run_hours": -1})))
        self.assertIn("runs must be", first(save_scenario(runs=0)))
        self.assertIn("brief is required", first(save_scenario(profile={"character": {"name": "Claude"}})))
        self.assertIn(
            "roleplay",
            first(save_scenario(profile={"character": {"name": "Claude", "brief": "b", "roleplay": "method"}})),
        )
        self.assertIn(
            "difficulty",
            first(save_scenario(profile={"character": {"name": "Claude", "brief": "b"}, "difficulty": "Honour"})),
        )
        self.assertIn("start.kind", first(save_scenario(start={"kind": "load"})))
        self.assertIn("takes no save or note", first(save_scenario(start={"kind": "new_game", "note": note})))
        # the save fixes the character: the profile names it, and it is the save's owner
        self.assertIn("needs profile.character.name", first(save_scenario(profile={"character": {"brief": "b"}})))
        self.assertIn(
            "must match the start save's character",
            first(save_scenario(profile={"character": {"name": "Ilse", "brief": "b"}})),
        )
        self.assertIn("not a save folder name", first(save_scenario(start=dict(st, save="beach"))))
        self.assertIn("re-save it as SCN-beach", first(save_scenario(start=dict(st, save="Claude-3391__S003-beach"))))
        self.assertIn("needs start.note", first(save_scenario(start=dict(st, note={"where": "x"}))))
        self.assertIn("list of strings", first(save_scenario(start=dict(st, note=dict(note, journal="Find")))))
        # the note is neutral game facts: never a playthrough's own history, and short
        self.assertIn("'S012'", first(save_scenario(start=dict(st, note=dict(note, where="where S012 ended")))))
        self.assertIn("claude-run", first(save_scenario(start=dict(st, note=dict(note, objective="as claude-run-2")))))
        self.assertIn("keep it short", first(save_scenario(start=dict(st, note=dict(note, journal=["q" * 40] * 40)))))
        self.assertIn("reset must be the standard", first(save_scenario(reset={"memory": "copy"})))
        self.assertEqual(problems(save_scenario(reset=R.RESETS["save"])), [])
        self.assertEqual(problems([]), ["not a JSON object"])
        self.assertIn("use a-z, 0-9", first(save_scenario(name="Beach!"), "Beach!"))
        self.assertIn("save_sha1 must be a string", first(save_scenario(start=dict(st, save_sha1=1))))
        self.assertIn("unknown start keys: area", first(save_scenario(start=dict(st, area="x"))))
        self.assertIn("unknown start.note keys: mood", first(save_scenario(start=dict(st, note=dict(note, mood="x")))))

    def test_load_fills_the_defaults_or_refuses(self):
        tmp = Path(tempfile.mkdtemp(prefix="bg3ai-scen-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        load = lambda p: R.load_scenario(p, MILESTONES, ROLEPLAY_MODES, DIFFICULTIES)
        sc = save_scenario(caps={"run_hours": 2}, profile={"character": {"name": "Claude", "brief": "b"}})
        del sc["runs"]
        (tmp / "beach.json").write_text(json.dumps(sc))
        got = load(tmp / "beach.json")
        self.assertEqual(got["runs"], 3)
        self.assertEqual(got["caps"], {"run_hours": 2, "max_cost": None, "max_tokens": None})
        self.assertEqual((got["reset"], got["profile"]["character"]["roleplay"]), (R.RESETS["save"], "strict"))
        (tmp / "bad.json").write_text("{")
        (tmp / "other.json").write_text(json.dumps(save_scenario()))
        for path, msg in ((tmp / "none.json", "No such file"), (tmp / "bad.json", "Expecting"),
                          (tmp / "other.json", "must be the file name")):  # fmt: skip
            with self.assertRaisesRegex(R.ScenarioError, msg):
                load(path)

    def test_status(self):
        base = save_scenario()
        self.assertEqual(R.scenario_status(base, {START})[0], R.READY)
        self.assertEqual(
            R.scenario_status(base, set()), (R.NEEDS_SAVE, f"start save {START} is not in the save folder")
        )
        empty = save_scenario(start=dict(base["start"], save=None))
        self.assertEqual(R.scenario_status(empty, {START})[0], R.NEEDS_SAVE)
        pinned = save_scenario(start=dict(base["start"], save_sha1="abc"))
        self.assertEqual(R.scenario_status(pinned, {START}, lambda f: "abc")[0], R.READY)
        st, why = R.scenario_status(pinned, {START}, lambda f: "def")
        self.assertEqual(st, R.NEEDS_SAVE)
        self.assertIn("changed", why)
        self.assertEqual(R.scenario_status({"start": {"kind": "new_game"}}, set()), (R.READY, "new game"))

    def test_start_note(self):
        note = R.start_note(save_scenario())
        self.assertTrue(note.startswith("# Handoff S000 → S001\n"))
        for line in (
            'Save to load: "SCN-beach"',
            "scenarios/beach.json",
            "Scenario start: Beach → Grove.",
            "Where: Ravaged Beach, out of combat.",
            "Party: Claude (Paladin), Shadowheart (Cleric)",
            "1. Find a Cure.",
            "## Journal at the start save\n- Find a Cure\n",
        ):
            self.assertIn(line, note)
        st = save_scenario()["start"]
        bare = save_scenario(title=None, start=dict(st, save=None, note={"where": "Beach.", "objective": "Go."}))
        note = R.start_note(bare)
        self.assertIn("(the scenario's start save)", note)
        self.assertIn("Scenario start: beach.", note)
        self.assertNotIn("Party:", note)
        self.assertIn("## Journal at the start save\n- (empty)\n", note)


class Checks(unittest.TestCase):
    def test_cli_conflicts(self):
        sc = save_scenario()
        none = {"until": None, "character_brief": None, "roleplay": None, "difficulty": None, "save_owner": None}
        self.assertEqual(R.scenario_cli_conflicts(sc, none), [])
        same = {
            "until": "grove",
            "character_brief": " a devotion paladin ",
            "roleplay": "strict",
            "difficulty": "Explorer",
            "save_owner": "Claude",
        }
        self.assertEqual(R.scenario_cli_conflicts(sc, same), [])
        bad = R.scenario_cli_conflicts(
            sc,
            {
                "until": "beach",
                "character_brief": "a dwarf",
                "roleplay": "light",
                "difficulty": "Tactician",
                "save_owner": "Ilse",
            },
        )
        self.assertEqual(
            [b.split()[0] for b in bad], ["--until", "--character-brief", "--roleplay", "--difficulty", "--save-owner"]
        )
        # a new game whose agent names the character: --save-owner would name it, which the scenario doesn't
        naut = dict(sc, start={"kind": "new_game"}, profile={"character": {"brief": "b", "roleplay": "strict"}})
        self.assertEqual(len(R.scenario_cli_conflicts(naut, dict(none, save_owner="Tav"))), 1)
        seeded = dict(sc, game_settings={"seed": 7})
        self.assertEqual(R.scenario_cli_conflicts(seeded, dict(none, seed=7)), [])
        self.assertEqual([b.split()[0] for b in R.scenario_cli_conflicts(seeded, dict(none, seed=8))], ["--seed"])

    def test_series_check(self):
        sc = save_scenario()
        self.assertIsNone(R.scenario_series_check({}, sc, "beach", "s1"))
        frozen = {"series": "beach", "scenario": R.scenario_freeze(sc, "s1")}
        self.assertEqual(frozen["scenario"]["spec"], sc)
        self.assertIsNone(R.scenario_series_check(frozen, sc, "beach", "s1"))
        self.assertIn("started without a scenario", R.scenario_series_check({"series": "beach"}, sc, "beach", "s1"))
        self.assertIn(
            "plays scenario other",
            R.scenario_series_check({"scenario": dict(frozen["scenario"], name="other")}, sc, "beach", "s1"),
        )
        self.assertIn(
            "changed since series beach started",
            R.scenario_series_check(frozen, dict(sc, caps={"run_hours": 9}), "beach", "s1"),
        )
        self.assertIn("the start save", R.scenario_series_check(frozen, sc, "beach", "s2"))

    def test_save_digest_reads_the_lsv(self):
        tmp = Path(tempfile.mkdtemp(prefix="bg3ai-scen-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        label = START.split("__", 1)[1]
        (tmp / START).mkdir()
        (tmp / START / f"{label}.lsv").write_bytes(b"abc")  # <owner>-<id>__<label>/<label>.lsv, as the game writes
        (tmp / START / f"{label}.WebP").write_bytes(b"webp")
        self.assertEqual(R.save_digest(tmp, START), "a9993e364706816aba3e25717850c26c9cd0d89d")
        self.assertIsNone(R.save_digest(tmp, "Claude-1__nope"))
        # a label the game rewrote (a default-name save): the folder's only .lsv
        odd = "Claude-2__SCN-x"
        (tmp / odd).mkdir()
        (tmp / odd / "Some Area - 1h 2m.lsv").write_bytes(b"abc")
        self.assertEqual(R.save_digest(tmp, odd), "a9993e364706816aba3e25717850c26c9cd0d89d")
        (tmp / odd / "another.lsv").write_bytes(b"x")
        self.assertIsNone(R.save_digest(tmp, odd))  # ambiguous: two .lsv and none named after the label
        self.assertIsNone(R.save_lsv(tmp, "Claude-3__missing"))  # no such folder
        if os.geteuid():  # root reads a mode-000 file
            lsv = tmp / START / f"{label}.lsv"
            lsv.chmod(0)
            self.assertIsNone(R.save_digest(tmp, START))


class Legacy(unittest.TestCase):
    def test_the_legacy_set_is_closed_and_loads(self):
        """No new v1 file: a new scenario is a v2 file in packages/bench/scenarios. The ones left still load."""
        names = {p.stem for p in (REPO / "scenarios").glob("*.json")}
        self.assertEqual(names, set(LEGACY), "new scenarios go to packages/bench/scenarios/ (v2), not scenarios/")
        for name, twin in LEGACY.items():
            sc = R.load_scenario(
                REPO / "scenarios" / f"{name}.json",
                MILESTONES + ["underdark", "last_light"],
                ROLEPLAY_MODES,
                DIFFICULTIES,
            )
            self.assertEqual(sc["name"], name)
            if twin:
                self.assertTrue((REPO / "packages" / "bench" / "scenarios" / f"{twin}.json").is_file())
