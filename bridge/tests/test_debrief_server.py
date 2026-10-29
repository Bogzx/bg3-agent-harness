"""Fight debriefs through the MCP tools: the block at combat end, the [fight] log line + fights.jsonl, a reload
during a fight (LAST ATTEMPT) and the retried fight's first turn (research/fight-debrief.md)."""
import json
import os
import time
import unittest
from unittest import mock

from bg3ai import debrief, server
from bg3ai.game import JobResult
from bg3ai.tools import core, memory, reporting, saving, turn_plan
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
        turn_plan._party_memo.update(t=0.0)
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
        out = memory.note("hello")
        self.assertIn("FIGHT DEBRIEF (costly, 1 round,", out)
        self.assertIn("downed: Lae'zel r1 by Goblin", out)
        self.assertIn("carried, unused: Scroll of Fireball", out)
        log = (self.tmp / "sessions" / "S099.md").read_text()
        self.assertIn("[fight] costly @", log)
        recs = [json.loads(x) for x in (self.tmp / "fights.jsonl").read_text().splitlines()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["session"], "S099")
        self.assertEqual(recs[0]["downed"][0]["name"], "Lae'zel")

    def test_server_exit_writes_the_open_fight(self):
        # A session that ends mid-fight: the record is written when the server stops, end session_end.
        self.bad_fight(end=False)
        memory.note("x")
        self.g.push("damage", {"source": ref(P1, "Claude"), "target": ref(GOB, "Goblin"), "amount": 4,
                               "type": "Slashing"})   # drained by no tool
        reporting._fight_at_exit()
        recs = [json.loads(x) for x in (self.tmp / "fights.jsonl").read_text().splitlines()]
        self.assertEqual([(r["end"], r["grade"]) for r in recs], [("session_end", "costly")])
        self.assertEqual(recs[0]["party"][0]["dealt"], 11)
        reporting._fight_at_exit()   # nothing open: nothing more
        self.assertEqual(len((self.tmp / "fights.jsonl").read_text().splitlines()), 1)

    def test_the_server_writes_it_when_its_client_exits(self):
        self.bad_fight(end=False)
        memory.note("x")
        with mock.patch.object(core.mcp, "run", side_effect=KeyboardInterrupt), \
                mock.patch.object(core.LIFE, "auto", False), \
                mock.patch.dict(os.environ, {"BG3AI_MOVIE_SKIP": "0", "BG3AI_METRICS_SAMPLE_S": "0"}), \
                self.assertRaises(KeyboardInterrupt):
            server.main([])
        recs = [json.loads(x) for x in (self.tmp / "fights.jsonl").read_text().splitlines()]
        self.assertEqual([r["end"] for r in recs], ["session_end"])

    def test_server_exit_writes_a_wipe_no_tool_saw(self):
        self.bad_fight(end=False)
        memory.note("x")
        self.g.push("downed", ref(P1, "Claude"))   # the last member down: Game Over, the agent's session ends
        reporting._fight_at_exit()
        recs = [json.loads(x) for x in (self.tmp / "fights.jsonl").read_text().splitlines()]
        self.assertEqual([(r["end"], r["grade"]) for r in recs], [("wipe", "wipe")])

    def test_reload_mid_fight_then_retry_hint(self):
        self.bad_fight(end=False)
        memory.note("x")   # the fight is open
        save = self.tmp / "saves" / "P__S099-pre"
        save.mkdir(parents=True)
        with mock.patch.object(core, "SAVE_DIR", self.tmp / "saves"), mock.patch.object(saving, "SAVE_PREFIX", "P"), \
                mock.patch.object(saving, "_enable_save_gates", lambda: None), \
                mock.patch.object(saving.SAVER, "settle", lambda: ""), \
                mock.patch.object(saving, "_game_process_alive", lambda: True), \
                mock.patch.object(saving, "_server_boot", lambda: "b"), \
                mock.patch.object(saving, "_load_once", lambda *a: (JobResult("done", {"loaded": "S099-pre"}), False)):
            out = saving.load_game("S099-pre")
        self.assertIn("FIGHT DEBRIEF (reloaded", out)
        self.assertIn("LAST ATTEMPT: reloaded (was costly) vs Goblin", out)
        self.assertIn("protect Lae'zel", out)
        self.assertTrue(reporting._retry["status"])
        # the retried fight's first turn repeats the key facts once
        self.g.combat = combat()
        hint = reporting._fight_hint(self.g.combat, {P1, P2})
        self.assertTrue(hint.startswith("RETRY of the fight that went costly: Lae'zel went down first"), hint)
        self.assertEqual(reporting._fight_hint(self.g.combat, {P1, P2}), "")

    def test_past_debrief_warns_in_the_next_fight(self):
        self.bad_fight()
        memory.note("x")
        self.g.push("combat_started", {})
        memory.note("y")
        self.g.combat = combat()
        hint = reporting._fight_hint(self.g.combat, {P1, P2})
        self.assertIn("LAST TIME vs Goblin (costly, S099 ", hint)
        self.assertIn("): Lae'zel went down first (r1, by Goblin)", hint)
        self.g.push("damage", {"source": ref(GOB, "Goblin"), "target": ref(P2, "Lae'zel"), "amount": 30,
                               "type": "Slashing"})
        self.g.push("downed", ref(P2, "Lae'zel"))
        out = memory.note("z")
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
        memory.note("x")
        hint = reporting._fight_hint(combat(), {P1, P2})
        self.assertIn("LAST TIME vs Goblin (wipe, S098)", hint)
        self.g.push("combat_ended", {})
        memory.note("y")
        self.assertTrue((runs / "S099.fights.jsonl").is_file())
        rec = json.loads((runs / "S099.fights.jsonl").read_text().splitlines()[-1])
        self.assertEqual(rec["session"], "S099")   # from the metrics path (the runner sets no BG3AI_SESSION; S035)
        self.assertFalse((self.tmp / "fights.jsonl").exists())

    def _load(self, name: str) -> str:
        save = self.tmp / "saves" / f"P__{name}"
        save.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(core, "SAVE_DIR", self.tmp / "saves"), mock.patch.object(saving, "SAVE_PREFIX", "P"), \
                mock.patch.object(saving, "_enable_save_gates", lambda: None), \
                mock.patch.object(saving.SAVER, "settle", lambda: ""), \
                mock.patch.object(saving, "_game_process_alive", lambda: True), \
                mock.patch.object(saving, "_server_boot", lambda: "b"), \
                mock.patch.object(saving, "_load_once", lambda *a: (JobResult("done", {"loaded": name}), False)):
            return saving.load_game(name)

    # BACKLOG ~1107 (S033 Balthazar's antechamber): attempt 1 lost → the agent wrote a [lesson] → reload → long rest →
    # attempt 2 vs the same enemies (lost again, reload) → next session attempt 3. Every retry's kickoff and the
    # reload's result carry the LAST ATTEMPT / RETRY facts AND the agent's own lesson.
    def test_s033_retry_shape_carries_last_attempt_and_own_lesson(self):
        log = self.tmp / "sessions" / "S099.md"
        before = time.strftime("%H:%M", time.localtime(time.time() - 1800))
        log.write_text(f"# S099\n- {before} [lesson] an old lesson about doors\n")   # before the fight: not shown
        self.bad_fight(end=False)
        memory.note("x")                                         # attempt 1 is open
        memory.note("kill Goblin spawners first, ranged", tag="lesson")
        out = self._load("S099-pre")                             # reload mid-fight
        self.assertIn("LAST ATTEMPT: reloaded (was costly) vs Goblin", out)
        self.assertIn("YOUR LESSON from that fight (session log): kill Goblin spawners first, ranged", out)
        self.assertNotIn("old lesson about doors", out)
        st = reporting._last_attempt_text(reporting._retry["rec"])     # what the first get_status repeats
        self.assertIn("YOUR LESSON", st)
        # attempt 2: the fight starts again; its kickoff (COMBAT STARTED block / first combat_turn) says RETRY + lesson
        self.g.combat = combat()
        self.g.push("combat_started", {})
        memory.note("y")
        hint = reporting._fight_hint(self.g.combat, {P1, P2})
        self.assertTrue(hint.startswith("RETRY of the fight that went costly"), hint)
        self.assertIn("YOUR LESSON from that fight (session log): kill Goblin spawners first, ranged", hint)
        # attempt 2 lost too: another reload; attempt 1's lesson still counts (the chain's first start)
        self.g.push("damage", {"source": ref(GOB, "Goblin"), "target": ref(P2, "Lae'zel"), "amount": 30,
                               "type": "Slashing"})
        self.g.push("downed", ref(P2, "Lae'zel"))
        memory.note("then focus the Goblin with Fire Bolt", tag="lesson")
        out = self._load("S099-pre")
        self.assertIn("LAST ATTEMPT: reloaded", out)
        self.assertIn("kill Goblin spawners first, ranged | then focus the Goblin with Fire Bolt", out)
        rec = json.loads((self.tmp / "fights.jsonl").read_text().splitlines()[-1])
        self.assertTrue(rec.get("chain_t"))
        # next session (a fresh bridge, history from fights.jsonl): LAST TIME + that session's lessons
        reporting._FIGHTS.update(tr=None, path=None)
        reporting._retry.update(rec=None, status=False, fight=False)
        self.g.push("combat_started", {})
        memory.note("z")
        hint = reporting._fight_hint(combat(), {P1, P2})
        self.assertIn("LAST TIME vs Goblin, Imp (reloaded, was costly, S099 ", hint)
        self.assertIn("YOUR LESSON from that fight (session log): kill Goblin spawners first, ranged | then focus", hint)

    def test_no_own_lesson_no_extra_line(self):
        self.bad_fight(end=False)
        memory.note("x")
        out = self._load("S099-pre")
        self.assertIn("LAST ATTEMPT", out)
        self.assertNotIn("YOUR LESSON", out)

    # vis V6 (S061 09:53/09:56 → S062): attempt 1 lost → reload; attempt 2 reloaded after 2 rounds while it went
    # fine (lure practice) → its RETRY carried only "unused: …". Now it also cites the last lost attempt's facts.
    def test_retry_after_a_clean_reload_cites_the_last_lost_attempt(self):
        self.bad_fight(end=False)
        memory.note("x")
        self._load("S099-pre")                                   # attempt 1: costly, reloaded
        self.g.events.clear()                                    # the loaded save's mod starts a new event log
        self.g.combat = combat()
        self.g.push("combat_started", {})
        self.g.push("damage", {"source": ref(P1, "Claude"), "target": ref(GOB, "Goblin"), "amount": 5,
                               "type": "Slashing"})
        memory.note("y")
        reporting._fight_hint(self.g.combat, {P1, P2})              # attempt 2's kickoff (RETRY of attempt 1)
        out = self._load("S099-pre")                             # reloaded while clean
        self.g.events.clear()
        self.assertIn("LAST ATTEMPT: reloaded (was clean)", out)
        self.g.push("combat_started", {})
        memory.note("z")
        hint = reporting._fight_hint(self.g.combat, {P1, P2})
        self.assertTrue(hint.startswith("RETRY of the fight that went clean: "), hint)
        self.assertIn("the last lost attempt vs them (reloaded, was costly, S099 ", hint)
        self.assertIn("): Lae'zel went down first (r1, by Goblin)", hint)

    def test_clean_fight_no_inventory_read(self):
        self.g.push("combat_started", {})
        self.g.push("damage", {"source": ref(P1, "Claude"), "target": ref(GOB, "Goblin"), "amount": 7,
                               "type": "Slashing"})
        self.g.push("combat_ended", {})
        out = memory.note("x")
        self.assertIn("FIGHT DEBRIEF (clean", out)
        self.assertEqual(self.g.ops("state.inventory"), [])
        self.assertIsNone(reporting._fights().retry_of())


if __name__ == "__main__":
    unittest.main()
