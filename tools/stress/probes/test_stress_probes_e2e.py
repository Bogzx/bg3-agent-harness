"""The probes that were `ready = false` in tools/stress/matrix.toml (C21 flee, P7 level-up offers, U13 recording fps),
run end to end against the fake mod (bg3harness.testing.fakemod, the game's LuaDebug protocol on a local port):

- P7 and U13 exactly as tools/stress/stress_io.WindowProbe runs a probe: `uv run --project bridge python <script>`
  with this directory on PYTHONPATH and the out dir as the working directory (U13 with stand-in xwininfo and ffmpeg
  on PATH, so no display or encoder is needed);
- C21 in this process through probelib.main and the bridge's real flee tool over the fake mod's socket; only the
  input device (the shim) and the Yes click on the game's box are stand-ins, as in bridge/tests/test_flee.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from bg3harness.testing.fakemod import FakeMod
from bg3harness.testing.scripted import GameModel

os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-probe-e2e-"))  # before the bridge's tools load

pytestmark = pytest.mark.e2e

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
P1, P2 = (m.uuid for m in GameModel().party)  # the scripted party's Tav and Shadowheart
FOE = "00000000-0000-0000-0000-000000000450"


class Extra:
    """Answers what GameModel doesn't: raw Lua by a substring, and ops by name (fn(args) or a value)."""

    def __init__(self, evals: list[tuple[str, Any]] | None = None, ops: dict[str, Any] | None = None):
        self.evals, self.ops = list(evals or []), dict(ops or {})

    def reply(self, ctx: str, op: str, args: dict[str, Any]) -> dict | None:
        if op not in self.ops:
            return None
        h = self.ops[op]
        return {"ok": True, "result": h(args) if callable(h) else h}

    def eval_raw(self, ctx: str, code: str) -> str | None:
        for needle, ans in self.evals:
            if needle in code:
                return ans(ctx, code) if callable(ans) else ans
        return None


def run_probe(script: str, args: list[str], port: int, out: Path, path_prefix: Path | None = None) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=str(HERE), BG3LE_DEBUG_PORT=str(port), BG3AI_STRESS_SAVE="")
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    argv = ["uv", "run", "--project", str(REPO / "bridge"), "python", str(HERE / script), *args]
    r = subprocess.run(argv, cwd=out, env=env, capture_output=True, text=True, timeout=240, check=False)
    return r.returncode, r.stdout + r.stderr


def stress_line(text: str) -> str:
    return [ln for ln in text.splitlines() if ln.startswith("STRESS ")][-1]


def test_p7_level_offers_end_to_end(tmp_path):
    offers = [
        {
            "uuid": P1,
            "name": "Tav",
            "classes": [{"class": "Fighter", "level": 3}],
            "known": [],
            "known_passives": ["Archery"],
            "known_feats": [],
            "repeatable_feats": ["Ability Improvement"],
            "levels": [
                {"level": 4, "feat": True, "skills": 0, "spells": [], "passives": []},
                {"level": 5, "feat": False, "skills": 0, "spells": [], "passives": []},
            ],
        },
        {
            "uuid": P2,
            "name": "Shadowheart",
            "classes": [{"class": "Cleric", "sub": "Trickery", "level": 3}],
            "known": ["Guidance"],
            "known_passives": [],
            "known_feats": [],
            "repeatable_feats": ["Ability Improvement"],
            "levels": [
                {
                    "level": 4,
                    "feat": True,
                    "skills": 0,
                    "passives": [],
                    "spells": [
                        {
                            "amount": 1,
                            "list": "Cleric cantrips",
                            "options": [{"name": "Guidance", "level": 0}, {"name": "Thaumaturgy", "level": 0}],
                        }
                    ],
                }
            ],
        },
    ]
    seen: dict[str, str] = {}

    def plan_offers(ctx: str, code: str) -> str:
        seen["head"] = code.split("\n", 2)[0]
        return json.dumps(offers)

    with FakeMod(GameModel(), Extra([("local MEMBERS", plan_offers)])) as fake:
        rc, out = run_probe("p7_level_offers.py", ["2"], fake.port, tmp_path)
    assert rc == 0, out
    assert stress_line(out).startswith("STRESS PASS 2 members (Tav Fighter 3; Shadowheart Cleric-Trickery 3)"), out
    assert seen["head"] == f'local MEMBERS = {{"{P1}", "{P2}"}}'
    assert json.loads((tmp_path / "offers.json").read_text()) == offers


def test_u13_rec_fps_end_to_end(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "xwininfo").write_text(
        '#!/bin/sh\necho \'  0x3400002 "Baldur\'\\\'\'s Gate 3 (1920x1080) - (Vulkan)": ("bg3" "bg3")\'\n'
    )
    (bin_dir / "ffmpeg").write_text('#!/bin/sh\necho "$@" > ffmpeg.args\nexit 0\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    drains = {"n": 0}

    def drain(ctx: str, code: str) -> str:
        assert ctx == "client"
        drains["n"] += 1
        return json.dumps([18.0] * 50 + [20.0] * 5)  # 55 frames over 1 s of frame time

    extra = Extra([("Ext.Events.Tick:Subscribe", "ok"), ("_G.PROBE_FT={}", drain)])
    with FakeMod(GameModel(), extra) as fake:
        rc, text = run_probe("u13_rec_fps.py", ["0.2", "0.2", "0.2", "0.1"], fake.port, out, bin_dir)
    assert rc == 0, text
    assert stress_line(text).startswith("STRESS PASS fps before 55.0 (min 55.0), recording 55.0"), text
    args = (out / "ffmpeg.args").read_text()
    assert "-window_id 0x3400002" in args and args.strip().endswith("rec.mp4")
    phases = json.loads((out / "fps.json").read_text())
    assert [ph["phase"] for ph in phases] == ["before", "recording", "after"] and drains["n"] >= 6


class FakeInput:
    def window_size(self):
        return (1920, 1080)

    def move(self, x, y, sync=True):
        pass

    def click(self, x, y, button="L", count=1, sync=True):
        pass

    def wait_frames(self, n, sync=True):
        pass


def test_c21_flee_end_to_end(tmp_path, capsys):
    sys.path.insert(0, str(REPO / "tools" / "dev"))
    import c21_flee
    import probelib
    from bg3ai import boxclick
    from bg3ai.game import Game
    from bg3ai.render import Handles
    from bg3ai.tools import combat, core, world_tools
    from server_view import S

    model = GameModel()
    model.party[0].name = "Claude"
    state = {"teleported": False, "fled": False, "box": False, "ops": []}
    pos = {P1: [0.0, 5.0, 0.0], P2: [1.0, 5.0, 0.0], FOE: [3.0, 5.0, 4.0]}

    def combat_state(args):
        foe_m = 50.0 if state["teleported"] else 5.5
        order = [
            {"uuid": P2, "name": "Shadowheart", "relation": "party"},
            {"uuid": FOE, "name": "Aelis", "relation": "hostile", "from": {P1: [foe_m, 1]}},
        ]
        if not state["fled"]:
            order.insert(0, {"uuid": P1, "name": "Claude", "relation": "party"})
        return {"active": True, "round": 2, "order": order, "current": P2}

    def teleport(ctx: str, code: str) -> str:
        state["teleported"] = True
        return "ok"

    def flee_cmd(args):
        state["ops"].append("ui.flee")
        state["box"] = True
        return {"pressed": args["name"]}

    extra = Extra(
        [
            ("TeleportToPosition", teleport),
            ("Transform", lambda ctx, code: json.dumps(next(v for k, v in pos.items() if k in code))),
        ],
        {
            "state.combat": combat_state,
            "ui.selected": {"uuid": P1},
            "ui.flee_probe": {"commands": [{"widget": "HotBar", "name": "RequestFlee", "can": True}]},
            "ui.flee": flee_cmd,
            "ui.box_open": lambda a: {"box": "Dialog_box"} if state["box"] else {},
        },
    )

    def click_box(rpc, inp, answer="yes", **kw):  # the shim's click on "Yes" (bridge/tests/test_flee.py does the same)
        state["fled"] = state["box"]
        return {"clicked": "Yes", "closed": True, "tries": 1}

    saved = (core.H, core.game, world_tools._inp, combat.FLEE_BOX_S, combat.FLEE_LEFT_S)
    with FakeMod(extra, model) as fake:  # extra first: its state.combat has the real shape (relation hostile, from)
        try:
            core.H, core.game = Handles(), Game(port=fake.port)
            world_tools._inp = lambda: FakeInput()
            combat.FLEE_BOX_S = combat.FLEE_LEFT_S = 0.0
            p = probelib.Probe(core.game, S, tmp_path, [], sleep=lambda s: None, log=lambda s: None)
            with mock.patch.object(boxclick, "click_box", click_box):
                assert probelib.main(c21_flee.run, probe=p) == 0
        finally:
            core.H, core.game, world_tools._inp, combat.FLEE_BOX_S, combat.FLEE_LEFT_S = saved
    line = stress_line(capsys.readouterr().out)
    assert line.startswith("STRESS PASS fled: Claude left the fight and is at camp; still fighting: Shadowheart"), line
    assert "combat_ended not fired" in line and state["ops"] == ["ui.flee"]
    assert state["teleported"]
