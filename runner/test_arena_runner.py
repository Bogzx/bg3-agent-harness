#!/usr/bin/env python3
"""play.py arena on the real runner up to the game's launch: arena_cli.main -> live.play -> the playthrough Runner
(legacy_playthrough.Runner) built from live.options, both sides' clients, the mod snapshot, then the Runner's
ensure_game, where the test stops it. A match's runner plays no client of its own (the sides' are the match's), so
nothing of the runner's assumes one. Never launches the game.

    python3 -m unittest runner/test_arena_runner.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: I001  (first: the game's profile folders are a temp dir before play)
import arena_cli
import legacy_playthrough as lpt

from bg3harness.arena import live
from bg3harness.game import io as gio
from bg3harness.runner import env as renv

REPO = Path(__file__).resolve().parents[1]
PI = str(REPO / "profiles" / "deepseek-v4.1-flash-pi.toml")
KEY_ENV = "OPENROUTER_API_KEY"  # the profile's api_key_env
ENCODER_ENV = "BG3AI_BENCH_RECORD_ENCODER"


class LaunchError(Exception):
    pass


def level_arena(name: str, dest: Path) -> Path:
    """packages/bench/arenas/<name>.json as a test fixture, dest/<name>.json: its draft dropped (the crash site's
    spawns are 0.50 m apart in height until live 15's height read, D349) and B's spawn at A's height."""
    a = json.loads((REPO / "packages" / "bench" / "arenas" / f"{name}.json").read_text())
    a.pop("draft", None)
    a["location"]["spawns"]["B"][1] = a["location"]["spawns"]["A"][1]
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{name}.json").write_text(json.dumps(a))
    return dest / f"{name}.json"


class ArenaOnTheRealRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-arena-runner-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        names = ("RUNTIME", "PLAYTHROUGHS", "SE_BG3AI_DIR", "KNOWLEDGE_DIR", "PLAY_DIR")
        saved = {n: getattr(testenv.home(n), n) for n in names}
        self.addCleanup(lambda: [setattr(testenv.home(n), n, v) for n, v in saved.items()])
        renv.RUNTIME, renv.PLAYTHROUGHS = self.tmp / "runtime", self.tmp / "playthroughs"
        renv.SE_BG3AI_DIR = self.tmp / "se" / "BG3AI"
        renv.PLAY_DIR = self.tmp / "play"
        renv.KNOWLEDGE_DIR = renv.PLAY_DIR / "knowledge"
        self.runners: list[lpt.Runner] = []
        self.arena = str(level_arena("crash-companions-v1", self.tmp / "arenas"))

    def play(
        self, a: str, b: str, key: str | None = "test-key", *more: str, encoder: str | None = "libx264"
    ) -> tuple[int, str]:
        """play.py arena on crash-companions-v1 until the Runner's ensure_game (the launch), which raises; `key`: the pi
        profile's API key in the env (a stand-in: nothing is called); `encoder`: the recording's, never this machine's
        record-encoder file (CI has none)."""
        runners = self.runners
        env = {"BG3AI_CONFIG_DIR": str(self.tmp / "config"), **({KEY_ENV: key} if key else {})}

        def launch(r, first: bool = False):
            runners.append(r)
            raise LaunchError("the test stops the match at the game's launch")

        err = io.StringIO()
        with (
            mock.patch.object(lpt.Runner, "ensure_game", launch),
            mock.patch.object(lpt.Runner, "refresh_mod_snapshot", lambda r: None),  # it copies into the game's mod dir
            mock.patch.dict("os.environ", env | ({ENCODER_ENV: encoder} if encoder else {})),
            mock.patch.object(live.client_setup, "read_dotenv", lambda _p: {}),  # the key only from the env
            contextlib.redirect_stderr(err),
        ):
            if not encoder:
                os.environ.pop(ENCODER_ENV, None)  # a shell's own value; patch.dict puts it back
            try:
                code = arena_cli.main(["--arena", self.arena, "--a", a, "--b", b, "--out", "t1",
                                       "--allow-unverified", *more])  # fmt: skip
            except LaunchError:
                code = -1
        return code, err.getvalue()

    def test_a_start_with_no_encoder_set_is_refused_before_the_launch(self):
        code, err = self.play("scripted", "scripted", encoder=None)
        self.assertEqual(code, 2)
        self.assertIn("no bench recording encoder set", err)
        self.assertEqual(self.runners, [])

    def test_scripted_sides_reach_the_launch_on_a_runner_with_no_client_of_its_own(self):
        code, err = self.play("scripted", "scripted")
        self.assertEqual(code, -1, err)  # the runner was built and went on to the launch
        r = self.runners[0]
        self.assertIsNone(r._client)
        self.assertTrue(r.opt.arena)
        with self.assertRaisesRegex(ValueError, "a match's clients are its sides'"):
            _ = r.client  # nothing asks the runner for a play client in a match; if something did, it says why
        self.assertEqual(r.state.dir, renv.RUNTIME / "sessions" / "t1")

    def test_a_pi_side_reaches_the_launch_too(self):
        renv.RUNTIME.mkdir(parents=True, exist_ok=True)
        (renv.RUNTIME / "budget.json").write_text(json.dumps({"cap": 5, "spent": 1}))
        code, err = self.play(PI, "scripted", "test-key", "--max-cost", "0.5")
        self.assertEqual(code, -1, err)
        self.assertIsNone(self.runners[0]._client)

    def test_a_pi_side_needs_a_max_cost_the_budget_has_left(self):
        renv.RUNTIME.mkdir(parents=True, exist_ok=True)
        (renv.RUNTIME / "budget.json").write_text(json.dumps({"cap": 5, "spent": 4.8}))
        code, err = self.play(PI, "scripted")
        self.assertEqual(code, 2)
        self.assertIn("side A spends money: a match needs a money cap (play.py arena --max-cost USD)", err)
        code, err = self.play(PI, "scripted", "test-key", "--max-cost", "0.5")
        self.assertEqual(code, 2)
        self.assertIn("the global budget has $0.20 left, less than the match's max_cost $0.5", err)
        code, err = self.play(PI, "scripted", "test-key", "--max-cost", "0")
        self.assertEqual(code, 2)
        self.assertIn("--max-cost must be > 0 USD (got 0)", err)
        self.assertEqual(self.runners, [])
        code, err = self.play(PI, "scripted", "test-key", "--max-cost", "0.2")  # exactly what is left
        self.assertEqual(code, -1, err)

    def test_a_pi_side_whose_api_key_isnt_set_is_refused(self):
        renv.RUNTIME.mkdir(parents=True, exist_ok=True)
        (renv.RUNTIME / "budget.json").write_text(json.dumps({"cap": 5, "spent": 1}))
        code, err = self.play(PI, "scripted", None, "--max-cost", "0.5")
        self.assertEqual(code, 2)
        self.assertIn(f"side A: {KEY_ENV} (the profile's api_key_env) is set neither", err)

    def test_a_pi_side_without_budget_left_is_refused_before_the_runner_is_built(self):
        code, err = self.play(PI, "scripted", "test-key", "--max-cost", "0.5")
        self.assertEqual(code, 2)
        self.assertIn("side A spends money: a match needs a global budget, runtime/budget.json", err)
        renv.RUNTIME.mkdir(parents=True, exist_ok=True)
        (renv.RUNTIME / "budget.json").write_text(json.dumps({"cap": 5, "spent": 5}))
        code, err = self.play(PI, "scripted", "test-key", "--max-cost", "0.5")
        self.assertEqual(code, 2)
        self.assertIn("the global budget is used up ($5.00 of $5)", err)
        self.assertEqual(self.runners, [])

    def whole_match(self, *more: str) -> tuple[int, list[str], str]:
        """play.py arena with scripted sides: the game's launch, hot reload, settings check, scenario_undo and
        end_run_game stand in; every mod op is refused (no game in a test), so the match is a setup failure."""
        calls: list[str] = []

        class Rec:
            def __init__(self, out: Path) -> None:
                calls.append(f"made {out.name}")

            def start(self) -> None:
                calls.append("start")

            def check(self) -> None:
                calls.append("check")

            def stop(self) -> None:
                run_dir = renv.RUNTIME / "sessions" / "t1"
                ended = any('"run_end"' in x for x in (run_dir / "facts.jsonl").read_text().splitlines())
                calls.append(f"stop after run_end: {ended}")

        def up(r, first: bool = False):
            calls.append("game up")
            r.state["loaded_save"] = "Tav-34221263934__SCN-crash_site_grove"
            return r.state["loaded_save"]

        def no_game(_self, op, args, side=None):
            raise ConnectionRefusedError(f"{op}: no game in this test")

        err = io.StringIO()
        with (
            mock.patch.object(lpt.Runner, "ensure_game", up),
            mock.patch.object(lpt.Runner, "refresh_mod_snapshot", lambda r: None),  # it copies into the game's mod dir
            mock.patch.object(lpt.Runner, "hot_reload_mod", lambda r: 0),
            mock.patch.object(lpt.Runner, "check_settings", lambda r: None),
            mock.patch.object(lpt.Runner, "scenario_undo", lambda r: calls.append("scenario undo")),
            mock.patch.object(lpt.Runner, "end_run_game", lambda r, when, after: calls.append(f"game stopped: {when}")),
            mock.patch.object(live.LiveGame, "rpc", no_game),
            mock.patch.object(live.run_host, "bench_recording", lambda env: Rec),
            contextlib.redirect_stderr(err),
        ):
            argv = ["--arena", self.arena, "--a", "scripted", "--b", "scripted", "--out", "t1"]
            code = arena_cli.main([*argv, "--max-cost", "1", *more])
        return code, calls, err.getvalue()

    def test_a_whole_match_on_the_runner_records_from_the_games_start_to_after_run_end(self):
        """Invalid (the setup failed), no agent started, recorded all the same; then the game is stopped, after the
        recording and scenario_undo (live 9: a matrix's next attempt found the last match's game up)."""
        code, calls, err = self.whole_match()
        self.assertEqual(code, 1, err)  # invalid: the setup failed
        self.assertEqual(calls, ["game up", "made recording", "start", "check", "stop after run_end: True",
                                 "scenario undo", "game stopped: match end"])  # fmt: skip
        run_dir = renv.RUNTIME / "sessions" / "t1"
        man = json.loads((run_dir / "arena.json").read_text())
        self.assertTrue(man["invalid"].startswith("setup: ConnectionRefusedError: arena.setup: no game"), man)
        self.assertEqual(man["sides"]["A"], {"player": "scripted/none", "profile": "scripted", "client": "scripted",
                                             "model": "none"})  # fmt: skip
        self.assertEqual(man["exits"], {"A": [], "B": []})
        self.assertEqual(man["max_cost"], 1.0)  # the cap reaches the manifest (scripted sides spend nothing)
        marks = [json.loads(x).get("mark") for x in (run_dir / "facts.jsonl").read_text().splitlines()]
        self.assertEqual((marks[0], marks[-1]), ("run_start", "run_end"))

    def test_keep_game_leaves_the_game_up_after_the_match(self):
        code, calls, err = self.whole_match("--keep-game")
        self.assertEqual(code, 1, err)
        self.assertEqual(calls[-2:], ["stop after run_end: True", "scenario undo"])

    def test_a_game_up_the_runner_didnt_launch_refuses_the_match_and_is_left_alone(self):
        """live 9: a matrix's next attempt found the last match's game still up; ensure_game's NotOurGame (a
        SystemExit) is a refusal (exit 2, nothing played), and end_run_game (which leaves a game it doesn't own
        alone) still runs."""
        calls: list[str] = []

        def foreign(r, first: bool = False):
            raise gio.NotOurGame("the game is running with a save loaded that the runner didn't load; --take-over")

        err = io.StringIO()
        with (
            mock.patch.object(lpt.Runner, "ensure_game", foreign),
            mock.patch.object(lpt.Runner, "refresh_mod_snapshot", lambda r: None),
            mock.patch.object(lpt.Runner, "scenario_undo", lambda r: calls.append("scenario undo")),
            mock.patch.object(lpt.Runner, "end_run_game", lambda r, when, after: calls.append(f"game stopped: {when}")),
            mock.patch.object(live.run_host, "bench_recording", lambda env: None),
            contextlib.redirect_stderr(err),
        ):
            argv = ["--arena", self.arena, "--a", "scripted", "--b", "scripted", "--out", "t1"]
            code = arena_cli.main(argv)
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn("refused: the game is running with a save loaded that the runner didn't load", err.getvalue())
        self.assertEqual(calls, ["scenario undo", "game stopped: match end"])
        self.assertFalse((renv.RUNTIME / "sessions" / "t1" / "arena.json").exists())

    def test_the_runs_facts_start_names_no_runner_client(self):
        self.play("scripted", "scripted")
        r = self.runners[0]
        r.facts_writer()
        first = json.loads((r.state.dir / "facts.jsonl").read_text().splitlines()[0])
        self.assertEqual(first["mark"], "run_start")
        self.assertEqual({k: first["detail"].get(k) for k in ("client", "model")}, {"client": None, "model": None})


if __name__ == "__main__":
    unittest.main()
