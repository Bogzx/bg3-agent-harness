"""Offline tests for tools/dev/probe_route_h.py (no game): the origin pick, and that each step fails closed against a
scripted game (no write after a read it can't trust).

Run: uv run pytest -q tools/dev/test_probe_route_h.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_route_h as rh

HOST = "aaaaaaaa-0000-0000-0000-000000000001"
HIRED = "bbbbbbbb-0000-0000-0000-000000000002"
ORIGINS = [
    {"i": i, "fields": {"Id": f"Hireling_{c}", "IsHenchman": True}}
    for i, c in enumerate(["Barbarian", "Fighter", "Sorcerer"], start=1)
]


class FakeGame:
    """Answers the probe's chunks by what they call; `sent` keeps every chunk's marker in order."""

    def __init__(
        self,
        henchmen_opens: bool = True,
        origins: list | None = None,
        respec_closes: bool = True,
        hire_works: bool = True,
    ) -> None:
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
            if name == "ExitCharacterRespec" and not dry and self.respec_closes:
                self.pages = []
            return json.dumps({"open": True, "ok": True})
        if "PI.set(d, 'SelectedOrigin'" in code:
            self.sent.append("select")
            return json.dumps({"open": True, "ok": True, "same": True})
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
    def test_exact_id(self):
        self.assertEqual(rh.pick_origin(ORIGINS, "Fighter")[0], 2)

    def test_one_loose_match(self):
        rows = [{"i": 1, "fields": {"Name": "The Fighter hireling"}}, {"i": 2, "fields": {"Name": "Sorcerer"}}]
        self.assertEqual(rh.pick_origin(rows, "Fighter")[0], 1)

    def test_no_guess_when_two_or_none(self):
        two = [{"i": 1, "fields": {"Name": "Fighter A"}}, {"i": 2, "fields": {"Name": "Fighter B"}}]
        self.assertIsNone(rh.pick_origin(two, "Fighter")[0])
        self.assertIsNone(rh.pick_origin(ORIGINS, "Wizard")[0])


class HireTest(unittest.TestCase):
    def test_pass(self):
        g = FakeGame()
        code, rec = run(g, "hire", "Fighter", "1-hire")
        self.assertEqual((code, rec["verdict"]), (0, "PASS"), rec["why"])
        self.assertEqual(g.sent, ["start_henchmen", "select", "FinishHenchmenCreating dry", "FinishHenchmenCreating"])
        self.assertEqual(rec["hired"][0]["uuid"], HIRED)

    def test_no_page_sends_nothing_more(self):
        g = FakeGame(henchmen_opens=False)
        code, rec = run(g, "hire", "Fighter", "1-hire")
        self.assertEqual((code, rec["verdict"]), (1, "FAIL"))
        self.assertEqual(g.sent, ["start_henchmen"])

    def test_no_unique_origin_cancels_without_finishing(self):
        g = FakeGame(origins=[{"i": 1, "fields": {"Id": "Hireling_Wizard"}}])
        code, rec = run(g, "hire", "Fighter", "1-hire")
        self.assertEqual((code, rec["verdict"]), (1, "FAIL"))
        self.assertEqual(g.sent, ["start_henchmen", "CancelHenchmenCreating"])

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
