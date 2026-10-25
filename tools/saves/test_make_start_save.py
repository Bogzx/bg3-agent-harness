"""Offline tests for tools/saves/make_start_save.py against the fake mod (packages/harness bg3harness.testing).

make() runs in-process with the bridge's Game on the fake mod's scripted model; one end-to-end run starts the tool
itself (uv run --project bridge) against the fake mod, whose ui.menu save writes a small save folder, and the real
bg3bench.authoring register copies it into a tmp store.

Run: python3 -m unittest discover -s tools/saves -p 'test_*.py'
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(REPO / "bridge" / "src"), str(REPO / "packages" / "harness" / "src")]
sys.path.append(str(HERE))

import make_start_save as mss  # noqa: E402
from bg3ai import difficulty, lspk  # noqa: E402
from bg3ai.game import Game, GameError  # noqa: E402
from bg3harness.testing.fakemod import FakeMod, Ops  # noqa: E402
from bg3harness.testing.scripted import FakeClock, GameModel  # noqa: E402


def lsv_bytes() -> bytes:
    return lspk.build([("SaveInfo.json", json.dumps({"Save Name": "x"}).encode(), "zlib")])


class Make(unittest.TestCase):
    def setUp(self) -> None:
        self.model = GameModel(clock=FakeClock())
        self.model.roll_mode = "WeightedRandom"  # the profile has Karmic Dice on
        self.fake = FakeMod(self.model).start()
        self.game = Game(port=self.fake.port, pool_max=2)
        self.game.job_push = False
        self.tmp = tempfile.TemporaryDirectory()
        self.saves = Path(self.tmp.name)
        self.registered: list[Path] = []
        self.logged: list[str] = []
        self.karmic_at_save: list[str] = []

    def tearDown(self) -> None:
        self.game.close()
        self.fake.stop()
        self.tmp.cleanup()

    def rpc(self, op: str, args: dict[str, Any] | None = None, context: str = "server") -> Any:
        return self.game.rpc(op, args, context=context)

    def save(self, label: str) -> Path:
        """The game names the folder after the avatar (the model's host member)."""
        self.karmic_at_save.append(self.model.roll_mode)
        d = self.saves / f"{self.model.party[0].name}-123__{label}"
        d.mkdir()
        (d / f"{label}.lsv").write_bytes(b"x")
        return d

    def make(self, name: str = "fight_act1_windmill", **kw: Any) -> Path:
        return mss.make(self.rpc, kw.pop("save", self.save), self.registered.append, name, log=self.logged.append, **kw)

    def test_makes_and_registers(self) -> None:
        self.model.party[0].name = "Claude"
        folder = self.make()
        self.assertEqual(folder.name, "Tav-123__SCN-fight_act1_windmill")
        self.assertEqual(self.registered, [folder])
        self.assertEqual(self.karmic_at_save, ["Random"])  # off while saving
        self.assertEqual(self.model.roll_mode, "WeightedRandom")  # back on afterwards
        ops = [op for op in self.fake.ops() if op.startswith("scn.")]
        self.assertEqual(ops, ["scn.status", "scn.difficulty", "scn.karmic", "scn.rename_avatar", "scn.undo"])
        self.assertIn("avatar 'Claude' -> 'Tav'", self.logged)
        self.assertIsNone(self.model.scn_avatar)  # no record left to rename that avatar at a later load
        self.assertEqual(self.model.party[0].name, "Claude")

    def test_karmic_already_off_stays_off(self) -> None:
        self.model.roll_mode = "Random"
        self.make()
        self.assertEqual(self.model.roll_mode, "Random")
        self.assertEqual([op for op in self.fake.ops() if op == "scn.karmic"], ["scn.karmic"])
        self.assertIn("undone: karmic Random, avatar Tav", self.logged)  # the original, written back

    def refused(self, match: str, **kw: Any) -> None:
        with self.assertRaisesRegex((mss.RefusedError, GameError), match):
            self.make(**kw)
        self.assertEqual(self.registered, [])
        self.assertEqual(self.model.roll_mode, "WeightedRandom")

    def test_refused_states_change_nothing(self) -> None:
        self.model.start_fight(["Goblin"])
        self.refused("in combat")
        self.model.end_fight()
        self.model.open_dialogue("Volo", "Hello", ["Bye"])
        self.refused("dialogue")
        self.assertNotIn("scn.karmic", self.fake.ops())
        self.assertNotIn("scn.rename_avatar", self.fake.ops())

    def test_refused_difficulty(self) -> None:
        self.model.difficulty = "Explorer"
        self.refused("'Explorer'.*Options > Gameplay")
        self.model.difficulty = "DIFFICULTY_HARD"
        self.refused("'DIFFICULTY_HARD'.*set Balanced")

    def test_balanced_by_its_ruleset_name_live6(self) -> None:
        # live6 §2: the mod reads the ruleset's name; Balanced set read DIFFICULTY_MEDIUM and every save was refused
        self.model.difficulty = "DIFFICULTY_MEDIUM"
        self.assertEqual(self.make().name, "Tav-123__SCN-fight_act1_windmill")

    def test_difficulty_names(self) -> None:
        for name in ("Balanced", "DIFFICULTY_MEDIUM", "DifficultyMedium", "difficultymedium"):
            self.assertEqual(difficulty.option_name(name), "Balanced")
        self.assertEqual(difficulty.option_name("DIFFICULTY_EASY"), "Explorer")
        for name in ("RulesetLarian", "", None, 2):
            self.assertIsNone(difficulty.option_name(name))

    def test_refused_without_the_switch(self) -> None:
        self.model.scenario_setup = False
        self.refused("BG3AI_SCENARIO_SETUP=1")

    def test_refused_names(self) -> None:
        self.refused("scenario name", name="Bad-Name")
        self.refused("owner", owner="claude bot")
        self.assertEqual(self.fake.ops(), [])

    def test_rename_not_on_the_folder(self) -> None:
        def old_owner(label: str) -> Path:
            d = self.saves / f"Claude-9__{label}"
            d.mkdir()
            return d

        self.refused("rename didn't reach", save=old_owner)

    def test_failed_save_restores_karmic(self) -> None:
        def broken(label: str) -> Path:
            raise mss.RefusedError("no save folder")

        self.refused("no save folder", save=broken)


class Register(unittest.TestCase):
    def test_command(self) -> None:
        cmd = mss.register_cmd(Path("/s/Tav-1__SCN-x"), Path("/store"), "x")
        self.assertEqual(cmd[:4], ["uv", "run", "--project", str(REPO / "packages" / "bench")])
        self.assertEqual(cmd[4:], ["python", "-m", "bg3bench.authoring", "register", "/s/Tav-1__SCN-x", "/store", "x"])

    def test_failure_is_refused(self) -> None:
        def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 2, "", "refused: /store/x exists")

        with self.assertRaisesRegex(mss.RefusedError, "exit 2.*exists"):
            mss.registrar(Path("/store"), "x", run)(Path("/s/Tav-1__SCN-x"))


class EndToEnd(unittest.TestCase):
    """The tool's main(), the bridge's save path and the real register, against the fake mod."""

    def test_tool(self) -> None:
        model = GameModel(clock=FakeClock())
        model.party[0].name = "Claude"
        with tempfile.TemporaryDirectory() as tmp:
            saves, store, runtime = Path(tmp, "saves"), Path(tmp, "store"), Path(tmp, "runtime")
            saves.mkdir()
            config: list[dict[str, Any]] = []

            def menu(ctx: str, args: dict[str, Any]) -> Any:
                if args.get("dry"):
                    return {"can": True}
                label = args["name"]
                d = saves / f"{model.party[0].name}-4242__{label}"
                d.mkdir()
                (d / f"{label}.lsv").write_bytes(lsv_bytes())
                return {"ms": {"open": 1, "accept": 1}}

            ops = Ops({"ui.config": lambda c, a: config.append(a) or {}, "ui.menu": menu})
            with FakeMod(model, ops) as fake:
                env = {**os.environ, "BG3LE_DEBUG_PORT": str(fake.port), "BG3AI_SAVE_DIR": str(saves),
                       "BG3AI_RUNTIME": str(runtime)}  # fmt: skip
                env.pop("BG3AI_PLAYTHROUGH", None)
                r = subprocess.run(
                    ["uv", "run", "--project", str(REPO / "bridge"), "python", str(HERE / "make_start_save.py"),
                     "fight_act1_windmill", "--store", str(store)],
                    capture_output=True, text=True, env=env, cwd=REPO, timeout=240,
                )  # fmt: skip
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                calls = fake.ops()
            reg = json.loads((store / "fight_act1_windmill" / "registration.json").read_text())
            self.assertEqual(reg["folder"], "Tav-4242__SCN-fight_act1_windmill")
            self.assertEqual(reg["owner"], "Tav")
            self.assertEqual(reg["inspector"], [])
            self.assertTrue((store / "fight_act1_windmill" / reg["folder"] / "SCN-fight_act1_windmill.lsv").is_file())
            self.assertEqual(config, [{"save_enabled": True}, {"save_enabled": False}])
            self.assertLess(calls.index("scn.rename_avatar"), calls.index("ui.menu"))
            self.assertIn("registered fight_act1_windmill", r.stdout)


if __name__ == "__main__":
    unittest.main()
