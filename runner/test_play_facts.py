#!/usr/bin/env python3
"""runner/play.py's facts.jsonl wiring: the run_start/settings/run_end marks, the last read before run_end, the
seed setup and the dialogue choices. Never launches claude or the game.

    python3 -m unittest runner/test_play_facts.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # first: the game's profile folders are a temp dir before play

os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")
import legacy_playthrough as lpt
import legacy_series as lser
from bg3ai.luadebug import LuaDebugClient
from bg3harness import clients, events
from bg3harness.game import io as G  # noqa: N812
from bg3harness.runner import checks_side as rchecks
from bg3harness.runner import env as renv
from bg3harness.runner import state as rstate
from bg3harness.testing.fakemod import FakeMod

RECORD = {
    "clean": True,
    "milestone": "m",
    "wall_min": 1,
    "sessions": 1,
    "cost_usd": 0.0,
    "crashes": 0,
    "recoveries": 0,
    "reloads": 0,
    "harness_failures": 0,
    "agent_failures": 0,
    "unknown_failures": 0,
}


def game(region: str, area: str | None) -> dict:
    """A progress.read_game result with the fields facts.snapshot reads."""
    return {
        "status": {"region": region, "gold": 5, "party": [{"name": "Tav", "level": 1}]},
        "ui": {"screen": "game", "region": area},
        "journal": {"quests": []},
    }


class MenuMod:
    """The fake mod's responder: the server context errors until a level is loaded (bg3le's main-menu answer)."""

    MENU = "the server context runs once a save is loaded; at the main menu, use the client context"

    def __init__(self):
        self.loaded = False

    def reply(self, ctx, op, args):
        if ctx == "server" and not self.loaded:
            return {"lua_error": self.MENU}
        result = {"roll.snapshot": {"mode": "Random"}, "scn.seed": {"seed": args.get("seed")}}.get(op, {})
        return {"ok": True, "result": result}

    def eval_raw(self, ctx, code):
        return None


class FactsWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-facts-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        names = ("RUNTIME", "PLAYTHROUGHS", "SE_BG3AI_DIR", "KNOWLEDGE_DIR", "PLAY_DIR")
        saved = {n: getattr(testenv.home(n), n) for n in names}
        self.addCleanup(lambda: [setattr(testenv.home(n), n, v) for n, v in saved.items()])
        renv.RUNTIME, renv.PLAYTHROUGHS = self.tmp / "runtime", self.tmp / "playthroughs"
        renv.SE_BG3AI_DIR = self.tmp / "se" / "BG3AI"
        renv.PLAY_DIR = self.tmp / "play"
        renv.KNOWLEDGE_DIR = renv.PLAY_DIR / "knowledge"

    def record(self, r, **patches):
        """write_run_record with the record, model time and the game's reads stubbed."""
        with (
            mock.patch.object(lser, "run_record", lambda *a, **k: dict(RECORD, t=time.time())),
            mock.patch.object(lser.model_time, "model_time", lambda *a, **k: None),
            mock.patch.object(lser.runs, "model_time_field", lambda *a, **k: None),
            mock.patch.multiple(G, **patches),
        ):
            r.opt.judge = False
            return r.write_run_record(time.time() - 5)

    def facts(self, r) -> list[dict]:
        return [json.loads(x) for x in (r.state.dir / "facts.jsonl").read_text().splitlines()]

    def test_the_last_read_before_run_end_catches_the_lagging_area(self):  # live3: no `area` on the beach
        r = lpt.Runner(rstate.Options("naut-a", None, 20, "opus", until="nautiloid_escaped"))
        r.game_settings = {}  # (no settings readout here)
        r.facts_writer().observe(game("Wilderness", None))  # the region changed; the minimap has no area yet
        reads = []

        def read_game(_eval):
            reads.append(1)
            return game("Wilderness", "Ravaged Beach")

        ring = mock.Mock()
        with (
            mock.patch.object(r, "ring_feed", lambda: ring),
            mock.patch.object(rchecks.story, "read_game", read_game),
            mock.patch.object(rchecks.facts, "read_inventory", lambda *a, **k: None),
        ):
            self.record(r, port_open=lambda *a, **k: True)
        recs = self.facts(r)
        self.assertEqual(len(reads), 1)
        kinds = [(x["kind"], x.get("ev") or x.get("mark")) for x in recs]
        self.assertEqual(kinds[-3:], [("event", "area"), ("snapshot", None), ("mark", "run_end")])
        self.assertEqual(recs[-3]["name"], "Ravaged Beach")
        self.assertEqual(recs[-2]["area"], "Ravaged Beach")  # the end state, whatever the snapshot cadence
        self.assertEqual(recs[-1]["detail"], {"outcome": "reached"})

    def test_the_last_reads_wait_through_a_dialogue_without_status(self):  # live4: naut stopped 13-28 s after
        r = lpt.Runner(rstate.Options("naut-w", None, 20, "opus", until="nautiloid_escaped"))
        r.game_settings = {}
        r.facts_writer().observe(game("Nautiloid", "Helm"))
        r.facts_writer().observe(game("Wilderness", None))  # Nautiloid -> Wilderness: the area lags
        seq = iter([{"status": None, "ui": {"screen": "dialog"}}, game("Wilderness", "Ravaged Beach")])
        sleeps = []
        with (
            mock.patch.object(r, "ring_feed", lambda: mock.Mock()),
            mock.patch.object(r, "sleep", sleeps.append),
            mock.patch.object(rchecks.story, "read_game", lambda _eval: next(seq)),
            mock.patch.object(rchecks.facts, "read_inventory", lambda *a, **k: None),
        ):
            self.record(r, port_open=lambda *a, **k: True)
        self.assertEqual(sleeps, [5.0])  # facts.END_EVERY_S
        recs = self.facts(r)
        self.assertEqual([x["name"] for x in recs if x.get("ev") == "area"], ["Ravaged Beach"])
        self.assertEqual(recs[-1]["mark"], "run_end")

    def test_the_run_record_carries_the_manifest(self):  # client_setup.run_files' fields (pi, claude)
        manifest = {"client": {"name": "claude", "version": "9.9"}, "tools": {"count": 95}}
        r = lpt.Runner(rstate.Options("naut-m", None, 20, "opus", manifest=manifest))
        self.assertEqual(self.record(r, port_open=lambda *a, **k: False)["manifest"], manifest)

    def test_no_last_read_when_the_game_is_gone_or_was_never_read(self):
        for read_before, port in ((True, False), (False, True)):
            r = lpt.Runner(rstate.Options(f"naut-{read_before}-{port}", None, 20, "opus", until="nautiloid_escaped"))
            if read_before:
                r.facts_writer().observe(game("Wilderness", None))
            with mock.patch.object(rchecks.story, "read_game", mock.Mock(side_effect=AssertionError("no read"))):
                self.record(r, port_open=lambda *a, port=port, **k: port)
            self.assertEqual([x.get("mark") for x in self.facts(r) if x["kind"] == "mark"], ["run_start", "run_end"])

    def test_a_failed_last_read_still_writes_run_end(self):
        r = lpt.Runner(rstate.Options("naut-f", None, 20, "opus", until="nautiloid_escaped"))
        r.facts_writer().observe(game("Wilderness", None))
        with mock.patch.object(rchecks.story, "read_game", mock.Mock(side_effect=OSError("loading screen"))):
            self.record(r, port_open=lambda *a, **k: True)
        self.assertEqual(self.facts(r)[-1]["mark"], "run_end")

    def test_seeded_run_sets_up_records_and_undoes(self):  # D9: scn.seed once, scn.undo at the run's end
        r = lpt.Runner(rstate.Options("naut-s", None, 20, "opus", until="nautiloid_escaped", seed=12345))
        self.assertEqual(r.setup_env(), {"BG3AI_SCENARIO_SETUP": "1"})
        self.assertEqual(lpt.Runner(rstate.Options("naut-u", None, 20, "opus")).setup_env(), {})
        calls = []

        def rpc(op, args=None, **kw):
            calls.append(op)
            return {"mode": "Random"} if op == "roll.snapshot" else {}

        t0 = time.time() - 5
        with (
            mock.patch.object(G, "rpc", rpc),
            mock.patch.object(
                lser,
                "run_record",
                lambda *a, **k: {
                    "clean": True,
                    "milestone": "m",
                    "wall_min": 1,
                    "sessions": 1,
                    "cost_usd": 0.0,
                    "crashes": 0,
                    "recoveries": 0,
                    "reloads": 0,
                    "harness_failures": 0,
                    "agent_failures": 0,
                    "unknown_failures": 0,
                    "t": time.time(),
                },
            ),
            mock.patch.object(lser.model_time, "model_time", lambda *a, **k: None),
            mock.patch.object(lser.runs, "model_time_field", lambda *a, **k: None),
        ):
            r.game_setup()
            r.game_setup()  # once per runner
            r.opt.judge = False
            rec = r.write_run_record(t0)
        self.assertEqual(calls, ["sys.ping", "roll.snapshot", "scn.seed", "scn.undo"])
        self.assertEqual(rec["seed"], 12345)
        self.assertEqual(rec["game_settings"]["roll_mode"], "Random")
        self.assertIs(rec["game_settings"]["karmic_dice"], False)
        self.assertEqual(rec["game_settings"]["seed"], {"seed": 12345, "applied": {}})
        marks = [json.loads(x) for x in (r.state.dir / "facts.jsonl").read_text().splitlines()]
        self.assertEqual([m.get("mark") for m in marks], ["run_start", "settings", "run_end"])

    def menu_runner(self, karmic: bool) -> tuple[lpt.Runner, list]:
        """A seeded scenario run on a new game, at the main menu of a fake mod (FakeMod: the bridge's wire) whose
        server context refuses every eval until `menu.loaded`, as the real game's does (live7)."""
        r = lpt.Runner(
            rstate.Options(
                "new-s",
                None,
                20,
                "opus",
                seed=1001,
                scenario_settings={"difficulty": "Balanced", "karmic_dice": karmic},
            )
        )
        events_: list = []
        r.event = lambda kind, **f: events_.append((kind, f))
        r.ring_feed = lambda: mock.Mock()
        self.menu = MenuMod()
        fake = FakeMod(self.menu).start()
        self.addCleanup(fake.stop)
        self.fake = fake

        def lua_eval(code, context="client", timeout=G.EVAL_TIMEOUT):
            with LuaDebugClient(port=fake.port, connect_timeout=5.0) as lua:
                return lua.evaluate(code, context=context, timeout=timeout)

        for patch in (
            mock.patch.object(G, "lua_eval", lua_eval),
            mock.patch.object(rstate, "write_status"),
            mock.patch.object(rchecks.facts, "read_inventory", lambda *a, **k: None),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        return r, events_

    def test_a_new_game_seeds_and_checks_after_its_first_load(self):  # live7: not at the main menu
        r, events_ = self.menu_runner(karmic=False)
        r.check_settings()  # LiveRunHost's start, at the main menu
        r.check_settings()
        self.assertEqual(events_, [("settings_deferred", {})])
        self.assertIsNone(getattr(r, "game_settings", None))
        self.assertNotIn("scn.seed", self.fake.ops())
        r.facts_poll(game("Nautiloid", None))  # still at the menu: deferred again, nothing seeded
        self.assertNotIn("scn.seed", self.fake.ops())
        self.menu.loaded = True  # the new game's first level
        r.facts_poll(game("Nautiloid", None))
        r.facts_poll(game("Nautiloid", None))
        self.assertEqual(self.fake.ops().count("scn.seed"), 1)
        self.assertEqual(r.game_settings["seed"], {"seed": 1001, "applied": {"seed": 1001}})
        self.assertIs(r.game_settings["karmic_dice"], False)
        self.assertEqual(events_[1:], [("settings_unverified", {"keys": ["difficulty"]})])
        self.assertFalse(r.abort)
        marks = [m.get("mark") for m in self.facts(r)]
        self.assertEqual(marks.count("settings"), 1)

    def test_a_contradiction_found_after_the_load_stops_the_session(self):
        r, events_ = self.menu_runner(karmic=True)
        r.check_settings()
        self.menu.loaded = True
        r.facts_poll(game("Nautiloid", None))
        self.assertTrue(r.abort)
        self.assertEqual([k for k, _ in events_], ["settings_deferred", "settings_unverified", "settings_mismatch"])

    def test_choices_go_into_facts(self):
        r = lpt.Runner(rstate.Options("naut-c", None, 20, "opus"))
        base = r.state.dir / "S001"
        log = events.EventLog(Path(f"{base}.events.jsonl"))
        log.write(clients.Event("tool_result", text="DIALOGUE Mattis: Buy?\n  1. Yes.\n  2. No.", t=time.time()))
        log.write(clients.Event("text", text="Too pricey.", t=time.time()))
        log.write(clients.Event("tool_call", tool="choose_dialogue", args={"option": 2}, t=time.time()))
        log.close()
        r.write_choices("S001", base)
        recs = [json.loads(x) for x in (r.state.dir / "facts.jsonl").read_text().splitlines()]
        self.assertEqual(
            [(x.get("ev"), x.get("picked_text"), x.get("reason")) for x in recs if x["kind"] == "event"],
            [("choice", "No.", "Too pricey.")],
        )


if __name__ == "__main__":
    unittest.main()
