#!/usr/bin/env python3
"""Unit tests for benchmark scenarios (scenarios/<name>.json): the format and its validation (runs.py), the status,
the harness's start note, the CLI-conflict and series checks, and the play.py wiring (run --scenario, the start
save in the save choice, the run's memory and profile, play.py scenarios). Never launches claude or the game; the
save folder is a temp dir.

    python3 -m unittest runner/test_scenarios.py
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import subprocess
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
# RUN-9: play.RUNTIME is read at import; a test must never append to the real runtime/runner.log (the phantom
# "0.01 s after SIGTERM" kill bursts the orchestrator chased). Tests that need a runtime set their own.
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")   # never chmod the real play/knowledge (Knowledge tests patch it)
import play as P  # noqa: E402
import runs as R  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MILESTONES = [m[0] for m in P.milestones.MILESTONES]
START = "Claude-33912620918__SCN-beach"


def save_scenario(**over) -> dict:
    sc = {"name": "beach", "title": "Beach → Grove",
          "start": {"kind": "save", "save": START, "save_sha1": None,
                    "note": {"where": "Ravaged Beach, out of combat.", "objective": "Find a Cure.",
                             "party": ["Claude (Paladin)", "Shadowheart (Cleric)"], "journal": ["Find a Cure"]}},
          "until": "grove", "runs": 2, "caps": {"run_hours": 2, "max_cost": 40, "max_tokens": None},
          "profile": {"character": {"name": "Claude", "brief": "a devotion paladin", "roleplay": "strict"},
                      "difficulty": "Explorer"}}
    sc.update(over)
    return sc


def problems(sc: dict, name: str = "beach") -> list[str]:
    return R.validate_scenario(sc, name, MILESTONES, P.ROLEPLAY_MODES, P.DIFFICULTIES)


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-scen-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        names = ("RUNTIME", "PLAYTHROUGHS", "SCENARIOS", "SAVE_DIR", "SE_BG3AI_DIR", "KNOWLEDGE_DIR", "PLAY_DIR",
                 "list_saves", "read_actions_since", "log")
        self.saved = {n: getattr(P, n) for n in names}
        self.addCleanup(lambda: [setattr(P, n, v) for n, v in self.saved.items()])
        P.RUNTIME, P.PLAYTHROUGHS = self.tmp / "runtime", self.tmp / "playthroughs"
        P.SCENARIOS, P.SAVE_DIR = self.tmp / "scenarios", self.tmp / "saves"
        P.SE_BG3AI_DIR, P.PLAY_DIR = self.tmp / "se" / "BG3AI", self.tmp / "play"
        P.KNOWLEDGE_DIR = P.PLAY_DIR / "knowledge"
        P.read_actions_since = lambda t, path=None: []
        P.log = lambda m: None
        P.SCENARIOS.mkdir()

    def write(self, sc: dict) -> dict:
        (P.SCENARIOS / f"{sc['name']}.json").write_text(json.dumps(sc))
        return P.load_scenario_file(sc["name"])

    def make_save(self, folder: str = START, data: bytes = b"lsv", mtime: float | None = None) -> None:
        # The game's layout (a real folder: Claude-33912620918__S003-beach/{S003-beach.lsv, S003-beach.WebP}).
        d = P.SAVE_DIR / folder
        d.mkdir(parents=True, exist_ok=True)
        label = folder.split("__", 1)[1]
        (d / f"{label}.lsv").write_bytes(data)
        (d / f"{label}.WebP").write_bytes(b"webp")
        if mtime:
            import os
            os.utime(d / f"{label}.lsv", (mtime, mtime))


# ======================================================================== format

class Format(unittest.TestCase):
    def test_the_bundled_scenarios_load(self):
        names = sorted(p.stem for p in (REPO / "scenarios").glob("*.json"))
        self.assertIn("nautiloid", names)
        self.assertGreaterEqual(len(names), 3)
        for n in names:
            sc = R.load_scenario(REPO / "scenarios" / f"{n}.json", MILESTONES, P.ROLEPLAY_MODES, P.DIFFICULTIES)
            self.assertEqual(set(sc["caps"]), set(R.CAP_KEYS))
            self.assertEqual(sc["reset"], R.RESETS[sc["start"]["kind"]])
            if sc["start"]["kind"] == "save":
                self.assertTrue(sc["profile"]["character"]["name"])

    def test_nautiloid_is_the_launch_bar_series(self):
        sc = R.load_scenario(REPO / "scenarios" / "nautiloid.json", MILESTONES, P.ROLEPLAY_MODES, P.DIFFICULTIES)
        self.assertEqual((sc["start"], sc["until"], sc["runs"], sc["caps"]["run_hours"]),
                         ({"kind": "new_game"}, "nautiloid_escaped", 3, 4))
        self.assertNotIn("name", sc["profile"]["character"])          # phase 0: the agent names the character
        self.assertEqual(R.scenario_status(sc, set()), (R.READY, "new game"))


# ======================================================================== start note, conflicts, series

class StartNote(TmpDir):
    def test_the_note_is_the_scenarios_text_only(self):
        sc = save_scenario()
        text = R.start_note(sc)
        self.assertTrue(text.startswith("# Handoff S000 → S001\n"))
        self.assertIn('Save to load: "SCN-beach"', text)
        self.assertEqual(P.parse_handoff_save(text), "SCN-beach")
        for part in ("Where: Ravaged Beach, out of combat.", "1. Find a Cure.",
                     "Party: Claude (Paladin), Shadowheart (Cleric)", "## Journal at the start save\n- Find a Cure"):
            self.assertIn(part, text)
        self.assertEqual(text, R.start_note(copy.deepcopy(sc)))           # pure: same scenario, same note
        self.assertIn("- (empty)", R.start_note(save_scenario(start=dict(sc["start"], note={
            "where": "w", "objective": "o"}))))

    def test_a_memory_with_the_note_passes_the_lint(self):
        from new_playthrough import create_memory
        mem = self.tmp / "mem"
        create_memory("t", mem, P.MEMORY_TEMPLATE)
        (mem / "HANDOFF.md").write_text(R.start_note(save_scenario()))
        out = subprocess.run([sys.executable, str(REPO / "tools" / "memory" / "lint.py"), str(mem)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("0 error(s)", out.stdout)

    def test_the_kickoff_note(self):
        note = P.scenario_start_note("beach", save_scenario()["profile"], "SCN-beach")
        self.assertIn("SCENARIO START (beach)", note)
        self.assertIn('loaded "SCN-beach"', note)
        self.assertIn("The character (Claude) is the save's", note)
        self.assertIn("> a devotion paladin", note)
        self.assertIn("Never start a new game", note)
        self.assertIn("Roleplay: strict", note)



# ======================================================================== play.py wiring

class Wiring(TmpDir):
    def runner(self, sc: dict, pt: str = "beach", k: int = 1) -> P.Runner:
        return P.Runner(P.Options(pt, None, 20, "opus", save_owner="Claude", until=sc["until"], series="beach",
                                  run_index=k, scenario=sc["name"], start_save=sc["start"].get("save")))

    def test_run_memory_and_profile_come_from_the_scenario(self):
        sc = self.write(save_scenario())
        for k, pt in ((1, "beach"), (2, "beach-r2")):
            P.prepare_series_run("beach", pt, k, sc)
            mem = P.PLAYTHROUGHS / pt / "memory"
            self.assertEqual((mem / "HANDOFF.md").read_text(), R.start_note(sc))
            log = subprocess.run(["git", "-C", str(mem), "log", "--format=%s"], capture_output=True, text=True).stdout
            self.assertIn("S000 scenario beach: start note", log)
            self.assertEqual(subprocess.run(["git", "-C", str(mem), "status", "--porcelain"], capture_output=True,
                                            text=True).stdout, "")
            self.assertEqual(json.loads((P.PLAYTHROUGHS / pt / "profile.json").read_text()), sc["profile"])
        # a new game scenario keeps the template's HANDOFF (phase 0 starts from the main menu)
        naut = self.write(dict(save_scenario(name="naut2"), start={"kind": "new_game"},
                               profile={"character": {"brief": "a fighter"}}))
        P.prepare_series_run("naut2", "naut2", 1, naut)
        self.assertIn("new game", (P.PLAYTHROUGHS / "naut2" / "memory" / "HANDOFF.md").read_text())
        self.assertEqual(P.load_profile(P.PLAYTHROUGHS / "naut2" / "profile.json")["character"]["brief"], "a fighter")

    def test_the_start_save_is_loaded_until_the_agent_saves(self):
        sc = save_scenario()
        r = self.runner(sc)
        t0 = time.time()
        r.begin_run(t0)
        self.assertEqual(r.state["saves_after"], t0)
        old = [P.Save(START, "Claude", "SCN-beach", t0 - 86400),
               P.Save("Claude-9__S040-end", "Claude", "S040-end", t0 - 3600)]     # another playthrough's
        P.list_saves = lambda: old
        self.assertEqual(r.choose_save().folder, START)
        P.list_saves = lambda: old + [P.Save("Claude-7__S001-start", "Claude", "S001-start", t0 + 60)]
        self.assertEqual(r.choose_save().folder, "Claude-7__S001-start")
        self.assertEqual(r.save_env()["BG3AI_START_SAVE"], START)
        # without a scenario the old save is never a candidate
        plain = P.Runner(P.Options("x", None, 20, "opus", save_owner="Claude", until="grove", series="x",
                                   run_index=1))
        plain.begin_run(t0)
        P.list_saves = lambda: old
        self.assertIsNone(plain.choose_save())
        self.assertNotIn("BG3AI_START_SAVE", plain.save_env())

    def test_series_json_freezes_the_scenario(self):
        sc = save_scenario()
        P.freeze_series("beach", 2, "grove", sc["profile"], {"p": 1}, sc["caps"], R.scenario_freeze(sc, "s1"))
        P.freeze_series("beach", 2, "grove", {}, {"p": 2}, {}, R.scenario_freeze(dict(sc, until="x"), "s2"))
        meta = P.series_meta("beach")
        self.assertEqual((meta["scenario"]["name"], meta["scenario"]["start_save_sha1"], meta["profile"]),
                         ("beach", "s1", sc["profile"]))
        self.assertEqual(meta["scenario"]["spec"], sc)

    def test_the_run_record_names_the_scenario(self):
        sc = save_scenario()
        r = self.runner(sc)
        t0 = time.time() - 60
        r.begin_run(t0)
        rec = r.write_run_record(t0, "timeout", "time cap used up")
        self.assertEqual((rec["scenario"], rec["series"]), ("beach", "beach"))


class Cli(TmpDir):
    def setUp(self):
        super().setUp()
        self.calls = []
        for target, value in (("acquire_lock", lambda: None), ("current_pins", lambda: {}),
                              ("run_series", lambda *a: self.calls.append(a) or 0)):
            p = mock.patch.object(P, target, value)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch("bg3ai.skipper.start_background", return_value=None),
                  mock.patch.object(P.client_setup, "run_files", lambda *a: {}),   # no tool dump (uv, the bridge)
                  mock.patch.dict(os.environ),   # session_env sets BG3AI_PROMPT_CONDITION
                  mock.patch.object(P.ramwatch, "start_background", return_value=None),
                  mock.patch.object(P.signal, "signal")):
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *args) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = P.main(["run", *args])
        return rc, err.getvalue()

    def test_a_ready_scenario_runs_as_a_frozen_series(self):
        sc = self.write(save_scenario())
        self.make_save()
        P.list_saves = lambda: [P.Save(START, "Claude", "SCN-beach", 1.0)]
        self.assertEqual(self.run_cli("--scenario", "beach", "--hours", "5"), (0, ""))
        opt, n, _current, _drift, got, sha1 = self.calls[-1]
        self.assertEqual((opt.series, opt.until, n, opt.scenario, opt.start_save, opt.save_owner),
                         ("beach", "grove", 2, "beach", START, "Claude"))
        self.assertEqual((opt.run_hours, opt.max_cost, opt.max_tokens), (2, 40, None))
        self.assertIsNone(opt.character_brief)                        # profile.json comes from the scenario
        self.assertEqual((got, sha1), (sc, R.save_digest(P.SAVE_DIR, START)))
        # --runs and --playthrough override N and the series' name; flags equal to the scenario's are fine
        self.assertEqual(self.run_cli("--scenario", "beach", "--runs", "5", "--playthrough", "b2", "--until", "grove",
                                      "--max-cost", "40", "--save-owner", "Claude")[0], 0)
        self.assertEqual((self.calls[-1][0].series, self.calls[-1][1]), ("b2", 5))

    def test_coaching_only_in_playthroughs_and_for_claude(self):  # PACE:/NOTES WINDOW nudges, the kickoff's pace
        self.write(save_scenario())
        self.make_save()
        P.list_saves = lambda: [P.Save(START, "Claude", "SCN-beach", 1.0)]
        os.environ.pop("BG3AI_PROMPT_CONDITION", None)
        self.assertEqual(self.run_cli("--scenario", "beach", "--client", "codex")[0], 0)
        self.assertEqual((os.environ["BG3AI_PROMPT_CONDITION"], self.calls[-1][0].pace), ("rules", ""))
        os.environ.pop("BG3AI_PROMPT_CONDITION")
        self.assertEqual(self.run_cli("--scenario", "beach")[0], 0)  # claude: play/CLAUDE.md is rules+coaching
        self.assertEqual(os.environ["BG3AI_PROMPT_CONDITION"], "rules+coaching")
        self.assertEqual(self.calls[-1][0].pace, P.PACE_PREFLIGHT)

    def test_refusals(self):
        self.write(save_scenario())
        self.write(save_scenario(name="todo", start=dict(save_scenario()["start"], save=None)))
        P.list_saves = lambda: [P.Save(START, "Claude", "SCN-beach", 1.0)]
        self.make_save()
        cases = [
            (("--scenario", "nope"), "no scenario 'nope'"),
            (("--scenario", "todo"), "needs a start save"),
            (("--scenario", "beach", "--until", "beach"), "--until 'beach' contradicts scenario beach"),
            (("--scenario", "beach", "--character-brief", "a dwarf"), "--character-brief"),
            (("--scenario", "beach", "--save-owner", "Ilse"), "--save-owner"),
            (("--scenario", "beach", "--max-cost", "60"), "max_cost: 40 → 60.0"),
            ((), "needs --playthrough"),
        ]
        for args, want in cases:
            rc, err = self.run_cli(*args)
            self.assertEqual(rc, 2, args)
            self.assertIn(want, err, args)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_cli("--scenario", "beach", "--max-cost", "60", "--allow-cap-change")[0], 0)
        self.assertEqual(self.calls[-1][0].cap_changes, ["max_cost: 40 → 60.0"])
        # a series that belongs to another scenario, or whose scenario file changed, is refused
        P.freeze_series("beach", 2, "grove", {}, None, None, {"name": "other"})
        rc, err = self.run_cli("--scenario", "beach")
        self.assertEqual((rc, "plays scenario other" in err), (2, True))

    def test_the_scenarios_command(self):
        self.write(save_scenario())
        self.write(save_scenario(name="todo", start=dict(save_scenario()["start"], save=None)))
        (P.SCENARIOS / "broken.json").write_text('{"name": "broken"}')
        P.list_saves = lambda: [P.Save(START, "Claude", "SCN-beach", 1.0)]
        self.make_save()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(P.main(["scenarios"]), 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(any(ln.startswith("beach") and "ready" in ln and "--until grove" in ln for ln in lines))
        self.assertTrue(any(ln.startswith("todo") and "needs a start save" in ln for ln in lines))
        self.assertTrue(any(ln.startswith("broken") and "invalid" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main(verbosity=1)
