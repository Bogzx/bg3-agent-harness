#!/usr/bin/env python3
"""bg3harness.game_settings: the settings readout from profile files and roll.snapshot; scn.seed / scn.undo.

uv run pytest packages/harness/tests/test_game_settings.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))
from bg3harness import game_settings as gs

LSX = """<node id="ConfigEntry">
    <attribute id="MapKey" type="FixedString" value="ScreenHeight"/>
    <attribute id="Type" type="int32" value="0"/>
    <attribute id="Value" type="int32" value="1080"/>
</node>
<node id="ConfigEntry">
    <attribute id="MapKey" type="FixedString" value="ScreenWidth"/>
    <attribute id="Type" type="int32" value="0"/>
    <attribute id="Value" type="int32" value="1920"/>
</node>"""
VDF = '"AppState"\n{\n\t"appid"\t"1086940"\n\t"UserConfig"\n\t{\n\t\t"language"\t\t"english"\n\t}\n}\n'


def attrs(**entries):
    out = [SimpleNamespace(name="Version64", value=1)]
    for k, v in entries.items():
        out += [
            SimpleNamespace(name="MapKey", value=k),
            SimpleNamespace(name="Type", value=0),
            SimpleNamespace(name="Value", value=v),
        ]
    return out


class Rpc:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, op, args):
        self.calls.append((op, args))
        r = self.replies[op]
        if isinstance(r, Exception):
            raise r
        return r


class Settings(unittest.TestCase):
    def test_readout(self):
        import steam_paths

        files = {
            "graphics": lambda: LSX,
            "config": lambda: attrs(ShowTutorials=0, UIScaleFactor=120),
            "manifest": lambda: steam_paths.parse_vdf(VDF),
        }
        got = gs.readout(files, Rpc({"roll.snapshot": {"mode": "WeightedRandom"}}))
        self.assertEqual(
            got,
            {
                "resolution": "1920x1080",
                "ui_scale": 120,
                "language": "english",
                "difficulty": None,
                "roll_mode": "WeightedRandom",
                "karmic_dice": True,
            },
        )

    def test_unreadable(self):
        boom = lambda: (_ for _ in ()).throw(OSError("missing"))  # noqa: E731
        got = gs.readout(
            {"graphics": boom, "config": lambda: attrs(ShowTutorials=0), "manifest": boom},
            Rpc({"roll.snapshot": OSError("no game")}),
        )
        self.assertEqual(
            got,
            {
                "resolution": None,
                "ui_scale": None,
                "language": None,
                "difficulty": None,
                "roll_mode": None,
                "karmic_dice": None,
            },
        )
        self.assertEqual(gs.karmic(Rpc({"roll.snapshot": {"mode": "Random"}})), ("Random", False))

    def test_seed_and_undo(self):
        rpc = Rpc({"scn.seed": {"streams": 3}, "scn.undo": {"restored": 0}})
        self.assertEqual(gs.apply_seed(rpc, 12345), {"streams": 3})
        self.assertEqual(gs.undo(rpc), {"restored": 0})
        self.assertEqual(rpc.calls, [("scn.seed", {"seed": 12345}), ("scn.undo", {})])


if __name__ == "__main__":
    unittest.main()
