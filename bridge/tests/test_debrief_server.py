"""Fight debriefs through the MCP tools: the block at combat end, the [fight] log line + fights.jsonl, a reload
during a fight (LAST ATTEMPT) and the retried fight's first turn (research/fight-debrief.md)."""
import json
import os
import unittest
from unittest import mock

from bg3ai import debrief, server
from bg3ai.game import JobResult
from test_turn_macros import GOB, P1, P2, Base, combat


def ref(u, n):
    return {"uuid": u, "name": n}


class FightServer(Base):
    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "sessions").mkdir()
        (self.tmp / "sessions" / "S099.md").write_text("# S099\n")
        os.environ["BG3AI_SESSION"] = "S099"
        self.addCleanup(os.environ.pop, "BG3AI_SESSION", None)
        server._party_memo.update(t=0.0)
        self.g.inv = {"items": [{"name": "Scroll of Fireball", "uuid": "u-sc"}, {"name": "Longsword", "uuid": "u-ls"}]}

    def bad_fight(self, end: bool = True) -> None:
        self.g.push("combat_started", {})
        self.g.push("round_started", {"round": 1})
        self.g.push("damage", {"source": ref(GOB, "Goblin"), "target": ref(P2, "Lae'zel"), "amount": 30,
                               "type": "Slashing"})
        self.g.push("downed", ref(P2, "Lae'zel"))
        self.g.push("cast", {"caster": ref(P1, "Claude"), "spell": "x", "name": "Main Hand Attack"})
        self.g.push("damage", {"source": ref(P1, "Claude"), "target": ref(GOB, "Goblin"), "amount": 7,
                               "type": "Slashing"})
        if end:
            self.g.push("combat_ended", {})

    def test_combat_end_debrief_log_and_store(self):
        self.bad_fight()
        out = server.note("hello")
        self.assertIn("FIGHT DEBRIEF (costly, 1 round,", out)
        self.assertIn("downed: Lae'zel r1 by Goblin", out)
        self.assertIn("carried, unused: Scroll of Fireball", out)
        log = (self.tmp / "sessions" / "S099.md").read_text()
        self.assertIn("[fight] costly @", log)
        recs = [json.loads(x) for x in (self.tmp / "fights.jsonl").read_text().splitlines()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["session"], "S099")
        self.assertEqual(recs[0]["downed"][0]["name"], "Lae'zel")

    def test_reload_mid_fight_then_retry_hint(self):
        self.bad_fight(end=False)
        server.note("x")   # the fight is open
        save = self.tmp / "saves" / "P__S099-pre"
        save.mkdir(parents=True)
        with mock.patch.object(server, "SAVE_DIR", self.tmp / "saves"), mock.patch.object(server, "SAVE_PREFIX", "P"), \
                mock.patch.object(server, "_enable_save_gates", lambda: None), \
                mock.patch.object(server.SAVER, "settle", lambda: ""), \
                mock.patch.object(server, "_game_process_alive", lambda: True), \
                mock.patch.object(server, "_server_boot", lambda: "b"), \
                mock.patch.object(server, "_load_once", lambda *a: (JobResult("done", {"loaded": "S099-pre"}), False)):
            out = server.load_game("S099-pre")
        self.assertIn("FIGHT DEBRIEF (reloaded", out)
        self.assertIn("LAST ATTEMPT: reloaded (was costly) vs Goblin", out)
        self.assertIn("protect Lae'zel", out)
        self.assertTrue(server._retry["status"])
        # the retried fight's first turn repeats the key facts once
        self.g.combat = combat()
        hint = server._fight_hint(self.g.combat, {P1, P2})
        self.assertTrue(hint.startswith("RETRY of the fight that went costly: Lae'zel went down first"), hint)
        self.assertEqual(server._fight_hint(self.g.combat, {P1, P2}), "")

    def test_past_debrief_warns_in_the_next_fight(self):
        self.bad_fight()
        server.note("x")
        self.g.push("combat_started", {})
        server.note("y")
        self.g.combat = combat()
        hint = server._fight_hint(self.g.combat, {P1, P2})
        self.assertIn("LAST TIME vs Goblin (costly, S099): Lae'zel went down first", hint)
        self.g.push("damage", {"source": ref(GOB, "Goblin"), "target": ref(P2, "Lae'zel"), "amount": 30,
                               "type": "Slashing"})
        self.g.push("downed", ref(P2, "Lae'zel"))
        out = server.note("z")
        self.assertIn("WARNING: Lae'zel went down first again", out)

    def test_runner_path_and_cross_session_history(self):
        runs = self.tmp / "run"
        runs.mkdir()
        old = {"v": 1, "session": "S098", "grade": "wipe", "enemies": [{"name": "Goblin", "count": 1}],
               "downed": [{"name": "Lae'zel", "round": 1, "by": "Goblin"}], "key_facts": ["Lae'zel went down first"]}
        (runs / "S098.fights.jsonl").write_text(json.dumps(old) + "\n")
        os.environ["BG3AI_METRICS_PATH"] = str(runs / "S099.metrics.jsonl")
        self.addCleanup(os.environ.pop, "BG3AI_METRICS_PATH", None)
        self.g.push("combat_started", {})
        server.note("x")
        hint = server._fight_hint(combat(), {P1, P2})
        self.assertIn("LAST TIME vs Goblin (wipe, S098)", hint)
        self.g.push("combat_ended", {})
        server.note("y")
        self.assertTrue((runs / "S099.fights.jsonl").is_file())
        rec = json.loads((runs / "S099.fights.jsonl").read_text().splitlines()[-1])
        self.assertEqual(rec["session"], "S099")   # from the metrics path (the runner sets no BG3AI_SESSION; S035)
        self.assertFalse((self.tmp / "fights.jsonl").exists())

    def test_clean_fight_no_inventory_read(self):
        self.g.push("combat_started", {})
        self.g.push("damage", {"source": ref(P1, "Claude"), "target": ref(GOB, "Goblin"), "amount": 7,
                               "type": "Slashing"})
        self.g.push("combat_ended", {})
        out = server.note("x")
        self.assertIn("FIGHT DEBRIEF (clean", out)
        self.assertEqual(self.g.ops("state.inventory"), [])
        self.assertIsNone(server._fights().retry_of())


if __name__ == "__main__":
    unittest.main()
