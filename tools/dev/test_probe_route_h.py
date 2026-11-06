"""Offline tests for tools/dev/probe_route_h.py (no game): the origin pick, and that each step fails closed against a
scripted game (no write after a read it can't trust).

Run: uv run pytest -q tools/dev/test_probe_route_h.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_route_h as rh

CLIENT_LUA = Path(__file__).resolve().parents[2] / "mod/Mods/BG3AI/ScriptExtender/Lua/Client"
HOST = "aaaaaaaa-0000-0000-0000-000000000001"
HIRED = "bbbbbbbb-0000-0000-0000-000000000002"
ORIGINS = [  # as live window 3 read them: no readable id; the class shows only in the selected one's panel
    {"i": i, "fields": {"IsLockedRace": True, "IsSelectable": True}, "cls": c}
    for i, c in enumerate(["Barbarian", "Fighter", "Sorcerer"], start=1)
]

LOSE = {  # as live window 3 read it: the title and text handles don't resolve
    "id": "CancelRespecMsgID",
    "title": "Warning",
    "text": "[ForceUpdate]",
    "buttons": ["Yes", "No"],
}


class FakeGame:
    """Answers the probe's chunks by what they call; `sent` keeps every chunk's marker in order."""

    def __init__(
        self,
        henchmen_opens: bool = True,
        origins: list | None = None,
        respec_closes: bool = True,
        hire_works: bool = True,
        respec_box: dict | None = None,
        yes_closes: bool = True,
    ) -> None:
        self.respec_box, self.yes_closes, self.box = respec_box, yes_closes, None
        self.opens, self.origins, self.respec_closes, self.hire_works = (
            henchmen_opens,
            origins,
            respec_closes,
            hire_works,
        )
        self.pages: list[str] = []
        self.party, self.log, self.sent = [HOST], [], []

    def pre(self) -> dict[str, Any]:
        return {
            "host": {"uuid": HOST, "name": "Tav", "level": 3, "gold": 500},
            "party": {"party": list(self.party)},
            "max_party": 8,
            "flags": {},
            "hooks": {},
            "mark": 0,
        }

    def eval(self, code: str, context: str = "client", timeout: float = 12.0) -> str:
        code = code.replace(rh.SERVER_LIB, "").replace(rh.CLIENT_LIB, "")  # the chunk's own part
        if "GetMaxPartySize" in code:
            return json.dumps(self.pre())
        if 'P.call("PROC_StartHenchmen"' in code:
            self.sent.append("start_henchmen")
            if self.opens:
                self.pages = ["Henchmen"]
            return json.dumps({"ok": True})
        if 'P.call("AddGold"' in code:
            return json.dumps({"ok": True})
        if 'P.call("StartRespec"' in code:
            self.sent.append("start_respec")
            self.pages = ["CharacterRespec"]
            return json.dumps({"ok": True})
        if 'UI.command(d, "' in code:
            name = code.split('UI.command(d, "')[1].split('"')[0]
            dry = "nil, true)" in code
            self.sent.append(f"{name}{' dry' if dry else ''}")
            if name == "FinishHenchmenCreating" and not dry and self.hire_works:
                self.party.append(HIRED)
                self.log.append({"e": "HenchmanSelected", "args": [HOST, HIRED]})
            if name == "ExitCharacterRespec" and not dry and self.respec_box is not None:
                self.box = self.respec_box  # the page stays behind its question box
            elif name == "ExitCharacterRespec" and not dry and self.respec_closes:
                self.pages = []
            return json.dumps({"open": True, "ok": True})
        if "A.pressButton(yes.el" in code:  # box_yes: what its Lua decides, from the chunk's own needle
            self.sent.append("box_yes")
            if self.box is None:
                return json.dumps({"box": False})
            v = {"box": True, **self.box}
            need = code.split("if v.id ~= ")[1].split(" then")[0].strip('"')
            if self.box.get("id") != need:
                return json.dumps({**v, "refused": "not the expected box"})
            if not any(b.strip().lower() == "yes" for b in self.box["buttons"]):
                return json.dumps({**v, "refused": "no Yes button"})
            if self.yes_closes:
                self.box, self.pages = None, []
            return json.dumps({**v, "pressed": True, "via": "command"})
        if "A.confirmBox()" in code:
            return json.dumps({"box": True, **self.box} if self.box else {"box": False})
        if "PI.set(d, 'SelectedOrigin'" in code:
            self.sent.append("select")
            self.selected = int(code.split("if i == ")[1].split(" ")[0])
            return json.dumps({"open": True, "ok": True, "same": False})  # live: false for the same entry too
        if "Venture Forth" in code:  # the panel of the selected entry
            rows = self.origins if self.origins is not None else ORIGINS
            row = next((o for o in rows if o["i"] == getattr(self, "selected", None)), None)
            return json.dumps({"open": True, "cls": row and row["cls"], "race": "Some Race"})
        if "UI.isVisible(w.el) then out[#out + 1] = w.name" in code:
            return json.dumps(self.pages)
        if "local r = { open = true, fields" in code:
            return json.dumps(
                {
                    "open": True,
                    "fields": {},
                    "names": ["SelectedClass"],
                    "lists": {"SelectableOrigins": self.origins if self.origins is not None else ORIGINS},
                }
            )
        if "H.since(" in code:
            return json.dumps(self.log)
        if "H.member(" in code:
            return json.dumps({"uuid": HIRED, "name": "Hireling", "level": 1})
        if "P.who(" in code:
            return json.dumps({"u": HOST})
        raise AssertionError("unexpected chunk: " + code[-200:])


def run(game: FakeGame, step: str, *args: Any) -> tuple[int, dict[str, Any]]:
    with tempfile.TemporaryDirectory() as d:
        r = rh.RouteH(game, Path(d), sleep=lambda s: None, clock=iter(range(0, 10000)).__next__)
        code = getattr(r, step)(*args)
        (f,) = Path(d).glob("*.json")
        return code, json.loads(f.read_text())


class PickTest(unittest.TestCase):
    def test_the_one_entry_showing_the_class(self):
        self.assertEqual(rh.pick_origin(ORIGINS, "fighter")[0], 2)

    def test_no_guess_when_two_or_none(self):
        two = [{"i": 1, "cls": "Fighter"}, {"i": 2, "cls": "Fighter"}]
        self.assertIsNone(rh.pick_origin(two, "Fighter")[0])
        self.assertIsNone(rh.pick_origin(ORIGINS, "Wizard")[0])
        self.assertIsNone(rh.pick_origin([{"i": 1, "cls": None}], "Fighter")[0])


class HireTest(unittest.TestCase):
    def test_pass(self):
        g = FakeGame()
        code, rec = run(g, "hire", "Fighter", "1-hire")
        self.assertEqual((code, rec["verdict"]), (0, "PASS"), rec["why"])
        scan = ["select"] * 3
        self.assertEqual(
            g.sent, ["start_henchmen", *scan, "select", "FinishHenchmenCreating dry", "FinishHenchmenCreating"]
        )
        self.assertEqual(rec["pick"], "entry 2 shows Level 1 Fighter")
        self.assertEqual(rec["hired"][0]["uuid"], HIRED)

    def test_a_panel_that_shows_another_class_cancels(self):
        g = FakeGame()
        real = g.eval

        def lying(code: str, context: str = "client", timeout: float = 12.0) -> str:
            out = real(code, context, timeout)
            if "Venture Forth" in code and g.sent.count("select") > 3:  # the re-select's panel
                return json.dumps({"open": True, "cls": "Wizard"})
            return out

        g.eval = lying  # type: ignore[method-assign]
        code, rec = run(g, "hire", "Fighter", "1-hire")
        self.assertEqual((code, rec["verdict"]), (1, "FAIL"))
        self.assertEqual(g.sent[-1], "CancelHenchmenCreating")
        self.assertNotIn("FinishHenchmenCreating", g.sent)

    def test_no_page_sends_nothing_more(self):
        g = FakeGame(henchmen_opens=False)
        code, rec = run(g, "hire", "Fighter", "1-hire")
        self.assertEqual((code, rec["verdict"]), (1, "FAIL"))
        self.assertEqual(g.sent, ["start_henchmen"])

    def test_no_unique_origin_cancels_without_finishing(self):
        g = FakeGame(origins=[{"i": 1, "fields": {}, "cls": "Wizard"}])
        code, rec = run(g, "hire", "Fighter", "1-hire")
        self.assertEqual((code, rec["verdict"]), (1, "FAIL"))
        self.assertEqual(g.sent, ["start_henchmen", "select", "CancelHenchmenCreating"])

    def test_no_event_or_no_new_member_fails(self):
        g = FakeGame(hire_works=False)
        code, rec = run(g, "hire", "Fighter", "1-hire")
        self.assertEqual((code, rec["verdict"]), (1, "FAIL"))
        self.assertIn("not seen", rec["why"])


class RespecTest(unittest.TestCase):
    def test_pass_and_leave(self):
        g = FakeGame()
        code, rec = run(g, "respec", "Shadowheart", "0-respec-companion")
        self.assertEqual((code, rec["verdict"]), (0, "PASS"), rec["why"])
        self.assertEqual(g.sent, ["start_respec", "ExitCharacterRespec dry", "ExitCharacterRespec"])
        self.assertEqual(rec["race_fields"], [])

    def test_a_page_that_stays_is_stop(self):
        code, rec = run(FakeGame(respec_closes=False), "respec", "Shadowheart", "0")
        self.assertEqual((code, rec["verdict"]), (2, "STOP"))

    def test_the_unsaved_progress_box_gets_yes_and_the_page_closes(self):
        g = FakeGame(respec_box=LOSE)
        code, rec = run(g, "respec", "Shadowheart", "0")
        self.assertEqual((code, rec["verdict"]), (0, "PASS"), rec["why"])
        self.assertEqual(g.sent, ["start_respec", "ExitCharacterRespec dry", "ExitCharacterRespec", "box_yes"])
        self.assertEqual(rec["box_answer"]["pressed"], True)
        self.assertEqual(rec["box_after"], {"box": False})

    def test_any_other_box_is_stop_with_nothing_pressed(self):
        other = {"id": "LeavePartyMsgID", "title": "Warning", "text": "[ForceUpdate]", "buttons": ["Yes", "No"]}
        g = FakeGame(respec_box=other)
        code, rec = run(g, "respec", "Shadowheart", "0")
        self.assertEqual((code, rec["verdict"]), (2, "STOP"))
        self.assertIn("not the expected box (nothing pressed)", rec["why"])
        self.assertNotIn("pressed", rec["box_answer"])
        self.assertIs(g.box, other)

    def test_no_yes_button_is_stop(self):
        g = FakeGame(respec_box={**LOSE, "buttons": ["Continue", "Back"]})
        code, rec = run(g, "respec", "Shadowheart", "0")
        self.assertEqual((code, rec["verdict"]), (2, "STOP"))
        self.assertIn("no Yes button", rec["why"])

    def test_yes_that_leaves_the_box_open_is_stop(self):
        g = FakeGame(respec_box=LOSE, yes_closes=False)
        code, rec = run(g, "respec", "Shadowheart", "0")
        self.assertEqual((code, rec["verdict"]), (2, "STOP"))
        self.assertIn("still open", rec["why"])

    def test_never_runs_the_apply(self):
        g = FakeGame(respec_box=LOSE)
        run(g, "respec", "Shadowheart", "0")
        self.assertFalse(any("FinishCreating" in x for x in g.sent))


class ModTest(unittest.TestCase):
    def test_every_mod_call_in_the_chunks_exists_in_this_tree(self):
        code = "\n".join(rh.chunks().values())
        mod = "\n".join(p.read_text(encoding="utf-8") for p in sorted(CLIENT_LUA.glob("*.lua")))
        called = set(re.findall(r"\b(UI|A)\.(\w+)\(", code))
        self.assertLessEqual({("A", "confirmBox"), ("A", "pressButton"), ("UI", "allText")}, called)
        missing = [f"{t}.{n}" for t, n in sorted(called) if f"function {t}.{n}(" not in mod]
        self.assertEqual(missing, [])
        inner = mod.split("P.inner = {", 1)[1].split("}", 1)[0]
        for name in set(re.findall(r"\bPI\.(\w+)\(", code)):
            self.assertIn(f"{name} = {name}", inner)


class HostCallTest(unittest.TestCase):
    def test_reads_before_and_after(self):
        g = FakeGame()
        code, rec = run(g, "host_call", "gold", "AddGold", 500, "gold")
        self.assertEqual((code, rec["verdict"]), (0, "PASS"))
        self.assertEqual(rec["why"], "host gold 500 -> 500")


class SaveTest(unittest.TestCase):
    def test_a_save_that_needs_the_shim_fails(self):
        class Job:
            status, result, error = "done", {"needs_text": True}, None

        g = FakeGame()
        g.rpc = lambda *a, **k: {}  # type: ignore[attr-defined]
        g.run_job = lambda *a, **k: Job()  # type: ignore[attr-defined]
        code, rec = run(g, "save", "ROUTEH_probe")
        self.assertEqual((code, rec["verdict"]), (1, "FAIL"))


class DryRunTest(unittest.TestCase):
    def test_writes_every_chunk(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rh.main(["hire", "--dry-run", "--out", d]), 0)
            self.assertEqual(sorted(p.stem for p in (Path(d) / "lua").glob("*.lua")), sorted(rh.chunks()))


if __name__ == "__main__":
    unittest.main()
