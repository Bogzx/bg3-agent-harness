"""BG3AI_RPC_RECORD: the bridge records every Lua eval and the game's answer as fixture JSONL (bg3ai/rpc_record.py)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bg3ai import game as game_mod
from bg3ai import rpc_record
from bg3ai.game import Game, GameError
from fake_bg3le import FakeBg3le

SINGLE = 'return Mods.BG3AI.BG3AI.rpc([=[{"op":"state.status","args":{}}]=])'
MULTI = (
    'local R=Mods.BG3AI.BG3AI.rpc return \'[\'..R([=[{"op":"a.x","args":{"n":1}}]=])..\',\'..'
    'R([==[{"op":"b.y","args":{"s":"]=]"}}]==])..\']\''
)


class DecodeTest(unittest.TestCase):
    def test_single(self):
        self.assertEqual(rpc_record.decode(SINGLE), ([{"op": "state.status", "args": {}}], False))

    def test_multi_with_nested_brackets(self):
        self.assertEqual(
            rpc_record.decode(MULTI), ([{"op": "a.x", "args": {"n": 1}}, {"op": "b.y", "args": {"s": "]=]"}}], True)
        )

    def test_other_lua(self):
        self.assertIsNone(rpc_record.decode("return tostring(Ext.Utils.GetGameState())"))


class ScrubTest(unittest.TestCase):
    def test_paths(self):
        cases = {
            "/home/u/.local/share/Larian Studios/Baldur's Gate 3/PlayerProfiles/Public/Savegames/A.lsv": "<path>/A.lsv",
            "saved to /home/u/saves/Quick.lsv ok": "saved to <path>/Quick.lsv ok",
            "~/play/memory/notes.md": "<path>/notes.md",
            "/data/Repos/bg3-ai/runtime/x.json": "<path>/x.json",
            "C:\\Users\\steamuser\\AppData\\Local\\save.lsv": "<path>/save.lsv",
            "/tmp/abc/ and more": "<path>/abc/ and more",
            "'/home/u/x', /run/user/1000/y": "'<path>/x', <path>/y",
            # two paths with only spaces/brackets between them may merge into one: more is scrubbed, never less
            "'/home/u/x' (and /run/user/1000/y)": "'<path>/y)",
        }
        for raw, want in cases.items():
            self.assertEqual(rpc_record.scrub(raw), want, raw)

    def test_not_paths(self):
        for s in (
            "HP 12/12",
            "a/b",
            "1/2 left",
            "Act 1 · Blighted Village",
            "e1fda08b-45d4-41db-84b5-20e9931ba5af",
            "/homework/x",
            "data/x/y",
            "~ 5 m",
        ):
            self.assertEqual(rpc_record.scrub(s), s)

    def test_nested(self):
        got = rpc_record.scrub({"/home/x/k": ["/home/u/a.txt", {"p": "/data/q/b.lsv"}, 3, None, True]})
        self.assertEqual(got, {"<path>/k": ["<path>/a.txt", {"p": "<path>/b.lsv"}, 3, None, True]})


class EntryTest(unittest.TestCase):
    def test_single_reply(self):
        line = rpc_record.entry(SINGLE, "server", 61.23, 1.5, '{"ok":true,"result":{"gold":5}}', None)
        self.assertEqual(
            line,
            {
                "v": 1,
                "t": 1.5,
                "ms": 61.2,
                "ctx": "server",
                "calls": [{"op": "state.status", "args": {}}],
                "replies": [{"ok": True, "result": {"gold": 5}}],
            },
        )

    def test_multi_reply(self):
        line = rpc_record.entry(MULTI, "client", 1, 0, '[{"ok":true,"result":1},{"ok":false,"error":"no"}]', None)
        self.assertTrue(line["multi"])
        self.assertEqual(line["replies"], [{"ok": True, "result": 1}, {"ok": False, "error": "no"}])

    def test_lua_error(self):
        line = rpc_record.entry(SINGLE, "server", 1, 0, None, "attempt to index a nil value (field 'BG3AI')")
        self.assertEqual(line["error"], "attempt to index a nil value (field 'BG3AI')")
        self.assertNotIn("replies", line)

    def test_raw_and_nil(self):
        line = rpc_record.entry("return tostring(Ext.Utils.GetGameState())", "client", 1, 0, "Running", None)
        self.assertEqual((line["code"], line["reply"]), ("return tostring(Ext.Utils.GetGameState())", "Running"))
        self.assertEqual(rpc_record.entry(SINGLE, "server", 1, 0, "nil", None)["reply"], "nil")


class RecorderTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertIsNone(rpc_record.from_env({}))
        self.assertIsNone(rpc_record.from_env({rpc_record.ENV: "  "}))
        if rpc_record.ENV not in os.environ:
            self.assertIsNone(game_mod.RECORD)

    def test_writes_scrubbed_lines_with_fake_clock(self):
        clock = iter([10.0, 10.5, 10.5625]).__next__
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rec.jsonl"
            rec = rpc_record.from_env({rpc_record.ENV: str(path)})
            rec._clock = clock
            rec._t0 = clock()
            t0 = rec.now()
            rec.record(SINGLE, "server", t0, rec.now(), result='{"ok":true,"result":{"save":"/home/u/s/A.lsv"}}')
            line = json.loads(path.read_text())
        self.assertEqual(line["t"], 0.5)
        self.assertEqual(line["ms"], 62.5)
        self.assertEqual(line["replies"][0]["result"]["save"], "<path>/A.lsv")


class GameHookTest(unittest.TestCase):
    """Game.eval records every eval (rpc, multi, a Lua error) when RECORD is set."""

    def setUp(self):
        self.fake = FakeBg3le(server_tick_s=0.002, client_tick_s=0.002).start()
        self.fake.op("state.gold", lambda a, c: {"gold": 7, "where": "/home/u/x.lsv"})
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "rec.jsonl"
        self.game = Game(port=self.fake.port, pool_max=1)
        self.game._ev_synced = True
        self.game.job_push = False

    def tearDown(self):
        self.game.close()
        self.fake.stop()
        self.tmp.cleanup()

    def lines(self):
        return [json.loads(t) for t in self.path.read_text().splitlines()]

    def test_records_rpc_multi_and_error(self):
        with mock.patch.object(game_mod, "RECORD", rpc_record.Recorder(self.path)):
            self.assertEqual(self.game.rpc("state.gold")["gold"], 7)
            self.assertEqual(
                self.game.multi([("state.gold", {"a": 1}), ("nope", {})]), [{"gold": 7, "where": "/home/u/x.lsv"}, None]
            )
            with self.assertRaises(GameError):
                self.game.eval("return 1+", "server")
        single, multi, err = self.lines()
        self.assertEqual(single["calls"], [{"op": "state.gold", "args": {}}])
        self.assertEqual(single["replies"], [{"ok": True, "result": {"gold": 7, "where": "<path>/x.lsv"}}])
        self.assertEqual([c["op"] for c in multi["calls"]], ["state.gold", "nope"])
        self.assertFalse(multi["replies"][1]["ok"])
        self.assertEqual((err["code"], err["ctx"]), ("return 1+", "server"))
        self.assertIn("can't run", err["error"])
        self.assertTrue(all(line["ms"] >= 0 for line in (single, multi, err)))

    def test_no_file_without_recorder(self):
        with mock.patch.object(game_mod, "RECORD", None):
            self.game.rpc("state.gold")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
