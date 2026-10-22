#!/usr/bin/env python3
"""run --scenario-file <resolved.json> (bg3harness.scenario_file): the file runs like run --scenario NAME, its seed
reaches the run, its timing and expected settings reach the run record, and the settings check stops a run whose
game contradicts them (karmic dice, difficulty). Never launches claude or the game; the save folder is a temp dir.

    python3 -m unittest runner/test_scenario_file_run.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401, I001  (first: the game's profile folders are a temp dir before play)
import test_scenarios as T  # noqa: N812  (first: it sets the test runtime before play is imported)
from bg3harness.runner import agent_side as ragent
from bg3harness.runner import checks_side as rchecks
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
import legacy_cli as lcli
import legacy_playthrough as lpt
import legacy_series as lser

RESOLVED: dict[str, Any] = dict(
    T.save_scenario(), game_settings={"seed": 1002, "difficulty": "Balanced", "karmic_dice": False}, timing="realtime"
)


class Cli(T.TmpDir):
    def setUp(self):
        super().setUp()
        self.calls = []
        for p in (
            mock.patch.object(lcli, "acquire_lock", lambda: None),
            mock.patch.object(lser, "current_pins", dict),
            mock.patch.object(lser, "run_series", lambda *a: self.calls.append(a) or 0),
            mock.patch("bg3ai.skipper.start_background", return_value=None),
            mock.patch.object(rpolicy.ramwatch, "start_background", return_value=None),
            mock.patch.object(ragent.signal, "signal"),
        ):
            p.start()
            self.addCleanup(p.stop)

    run_cli = T.Cli.run_cli

    def resolved(self, sc: dict, name: str = "resolved.json") -> str:
        path = self.tmp / name  # any file name: the scenario's name comes from its content
        path.write_text(json.dumps(sc))
        return str(path)

    def test_a_resolved_scenario_runs_like_a_named_one(self):
        self.make_save()
        rcode.list_saves = lambda: [rpolicy.Save(T.START, "Claude", "SCN-beach", 1.0)]
        self.assertEqual(self.run_cli("--scenario-file", self.resolved(RESOLVED), "--hours", "5"), (0, ""))
        opt, n, _current, _drift, got, _sha1 = self.calls[-1]
        self.assertEqual(
            (opt.series, opt.until, n, opt.scenario, opt.start_save, opt.seed),
            ("beach", "grove", 2, "beach", T.START, 1002),
        )
        self.assertEqual(opt.scenario_settings, {"difficulty": "Balanced", "karmic_dice": False, "timing": "realtime"})
        self.assertEqual(got["game_settings"], {"seed": 1002})  # the v1 part: what series.json freezes

    def test_a_scenario_with_autoskip_off_turns_it_off_for_the_run(self):
        self.make_save()
        rcode.list_saves = lambda: [rpolicy.Save(T.START, "Claude", "SCN-beach", 1.0)]
        off = dict(RESOLVED, game_settings={**RESOLVED["game_settings"], "autoskip": False})
        with mock.patch.dict(os.environ, {"BG3AI_AUTOSKIP": "1"}):
            self.assertEqual(self.run_cli("--scenario-file", self.resolved(off)), (0, ""))
            self.assertEqual(os.environ["BG3AI_AUTOSKIP"], "0")
        self.assertEqual(self.calls[-1][4]["game_settings"], {"seed": 1002, "autoskip": False})
        with mock.patch.dict(os.environ, {"BG3AI_AUTOSKIP": "1"}):
            self.assertEqual(self.run_cli("--scenario-file", self.resolved(RESOLVED)), (0, ""))
            self.assertEqual(os.environ["BG3AI_AUTOSKIP"], "1")  # on: nothing changed
        rc, err = self.run_cli("--scenario-file", self.resolved(RESOLVED), "--no-autoskip")
        self.assertEqual(rc, 2)
        self.assertIn("--no-autoskip", err)

    def test_refusals(self):
        self.make_save()
        rcode.list_saves = lambda: [rpolicy.Save(T.START, "Claude", "SCN-beach", 1.0)]
        bad = dict(RESOLVED, game_settings={"karmic_dice": "off"})
        cases = [
            (("--scenario-file", self.resolved(bad, "bad.json")), "karmic_dice must be true, false or null"),
            (("--scenario-file", self.resolved(dict(RESOLVED, until="nowhere"), "until.json")), "is not a milestone"),
            (("--scenario-file", str(self.tmp / "missing.json")), "missing.json"),
            (("--scenario-file", self.resolved(RESOLVED), "--scenario", "beach"), "not both"),
            (("--scenario-file", self.resolved(RESOLVED), "--seed", "5"), "--seed"),
        ]
        for args, want in cases:
            rc, err = self.run_cli(*args)
            self.assertEqual(rc, 2, args)
            self.assertIn(want, err, args)
        self.assertEqual(self.calls, [])


class SettingsCheck(T.TmpDir):
    def runner(self, want: dict | None, got: dict) -> lpt.Runner:
        r = lpt.Runner.__new__(lpt.Runner)
        r.opt = rstate.Options("beach", None, 60, "opus", scenario_settings=want)
        r.game_settings = got  # game_setup's readout, done
        self.events: list[tuple[str, dict]] = []
        r.event = lambda kind, **f: self.events.append((kind, f))
        return r

    def test_a_contradiction_stops_the_run(self):
        r = self.runner(
            {"difficulty": "Balanced", "karmic_dice": False, "timing": "realtime"},
            {"difficulty": None, "karmic_dice": True},
        )
        with (
            mock.patch.object(rstate, "write_status") as status,
            self.assertRaisesRegex(rchecks.SettingsMismatch, "karmic_dice is True, the scenario needs False"),
        ):
            r.check_settings()
        self.assertEqual([k for k, _ in self.events], ["settings_unverified", "settings_mismatch"])
        self.assertEqual(status.call_args.kwargs["alert"]["kind"], "settings_mismatch")

    def test_a_matching_or_unknown_game_plays(self):
        r = self.runner(
            {"difficulty": "Balanced", "karmic_dice": False, "timing": None}, {"difficulty": None, "karmic_dice": False}
        )
        r.check_settings()  # difficulty unknown to the readout: logged, not refused
        self.assertEqual(self.events, [("settings_unverified", {"keys": ["difficulty"]})])
        self.runner(None, {"karmic_dice": True}).check_settings()  # no scenario file: no check


class RunRecord(T.TmpDir):
    def test_the_run_record_carries_timing_and_settings(self):
        r = T.Wiring.runner(self, T.save_scenario())  # pyright: ignore[reportArgumentType]  (Wiring's helper, not its tests)
        r.opt.scenario_settings = {"difficulty": None, "karmic_dice": False, "timing": "turn_based"}
        t0 = time.time() - 60
        r.begin_run(t0)
        rec = r.write_run_record(t0, "timeout", "time cap used up")
        self.assertEqual((rec["timing"], rec["scenario_settings"]["karmic_dice"]), ("turn_based", False))


if __name__ == "__main__":
    unittest.main()
