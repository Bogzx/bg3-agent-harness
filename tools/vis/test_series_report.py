"""series_report on a small fixture cut from the real naut2 (runs 1-3) and naut (run 1) transcripts, plus one
synthetic post-fix run whose lines are the bridge's new texts (ACT-P1..P4, helm chain, vis objectives, FIRST LOOK).

    python3 tools/vis/test_series_report.py              # the tests
    python3 tools/vis/test_series_report.py --rebuild    # re-cut fixtures/series_report.json from runtime/sessions

The fixture keeps every call of a run (tool, input, think s, call s) and, of each result, the first line and the
lines the phases and signatures read (KEEP), so the verdicts equal the real files' (the rebuild checks that).
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import overlap  # noqa: E402
import series_report as SR  # noqa: E402
from test_overlap import write_session  # noqa: E402

FIXTURE = HERE / "fixtures" / "series_report.json"
RUNS = {"naut2": ["naut2", "naut2-r2", "naut2-r3"], "naut": ["naut"]}
KEEP = re.compile(r"combat_started|COMBAT STARTED|-> COMBAT|combat_ended|above you|climbed=|Helm|Transponder|"
                  r"Wilderness|whose turn it is|objective marker|not this character's turn|→ partial|beyond movement|"
                  r"cut to the movement left|early wake|arrived|: done|No map change|Here:|moved on|is gone now|"
                  r"travel_to objective|FIRST LOOK|wedged|no walkable path|then interact|don't jump there|STOPPED|"
                  r"INTERRUPTED|REFUSED|FAILED|^\s*\d+\. |^move ")
STAGE = [{"t": 1792169675.0, "op": "create_character", "naming_ms": 3288, "via": "typed",
          "naming_stages": {"popup": 1218, "type": 1606}}]


def cut(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(ln for i, ln in enumerate(lines) if i == 0 or KEEP.search(ln))


def rebuild(root: Path) -> dict:
    out = {}
    for runs in RUNS.values():
        for run in runs:
            cs = overlap.load(root / run / "S001")
            out[run] = [(c["name"], c["input"], cut(c["text"]), round(c["think"], 2), round(c["dur"], 2)) for c in cs]
    return out


# A post-fix Nautiloid run, cut down: the bridge's new texts (server.py / route.py / setup part 52)
NAUT3 = [
    ("create_character", {}, "character created and finished: 'Corran Vell' (name checked in game)\n"
     "FIRST LOOK (this is get_status + look: don't read again before moving):\nWHERE Nautiloid", 0.5, 6.0),
    ("travel_to", {"objective": "o1"}, "travel_to objective Escape Route: started (34.2m); 34m left", 2.0, 0.2),
    ("wait_for", {"target": "queue"}, "queue e1 Corran Vell: done (1 steps)\n1. travel_to objective Escape Route: "
     "34.2m\no1 Escape Route moved on: now 66.8m W: travel_to(objective=\"o1\") again", 0.1, 8.0),
    ("travel_to", {"objective": "o1"}, "travel_to objective Escape Route: started (66.8m); 67m left", 1.5, 0.2),
    ("wait_for", {"target": "queue"}, "queue e1: done (1 steps)\nMODE EXPLORING -> COMBAT turn-based", 1.5, 9.0),
    ("combat_turn", {"plan": {}}, "TURN e29 Lae'zel (round 1)\nmove e34: done — in_reach", 2.0, 4.0),
    ("combat_turn", {"plan": {}}, "TURN e29 Lae'zel (round 2)\nmove e36: beyond movement (path 11.5 m, 9.0 m left)"
     " → partial 8.7m toward it: done — @(-45,0.8,-390.6)\nEVENTS\n  combat_ended", 2.0, 5.0),
    ("get_map", {}, "No map change (Nautiloid; 1 objectives, 0 markers). Here: o2 Helm 46.7m N [Escape the "
     "Nautiloid]. full=true lists everything.", 1.5, 0.1),
    ("travel_to", {"objective": "o2"}, "travel_to objective Helm: REFUSED: the goal is 11m above you and no walk on "
     "this floor reaches it. The way there: interact e56, then interact e57 (or queue_actions [interact e56, "
     "interact e57, travel_to ...])", 2.0, 0.3),
    ("queue_actions", {"actions": '[{"tool":"interact","target":"e56"},{"tool":"interact","target":"e57"}]'},
     "RUNNING queue e1 Corran Vell: starting then interact e56, interact e57", 2.0, 0.2),
    ("wait_for", {"target": "queue"}, "queue e1 Corran Vell: done (2 steps)\n1. interact: done — climbed=up dy=2.9 "
     "used\n2. interact: done — climbed=up dy=10 used", 0.2, 14.0),
    ("travel_to", {"objective": "o2"}, "travel_to objective Helm: started (8.5m)", 1.5, 0.2),
    ("get_combat_state", {}, "COMBAT round=3 party_can_act=[e1] Transponder", 2.0, 0.1),
    ("use_ability", {"ability": "Shout_Dash"}, "use_ability: done — cast=Shout_Dash\n(who: e1 Corran Vell, whose "
     "turn it is; the selected e29 Lae'zel can't act now)", 2.0, 0.5),
    ("walk_toward", {"objective": "o3", "max_m": 9}, "heading W toward objective Transponder: 9m (path 9.6m); cut to "
     "the movement left (9m): 8.1m\nwalk_toward: started j65", 2.0, 0.2),
    ("combat_turn", {"who": "Corran Vell", "plan": {"move": "o3"}}, "TURN e1 Corran Vell (round 4)\nmove o3 "
     "(objective Transponder): beyond movement (path 20 m, 18 m left) → partial 17.7m toward it: done — @(-71,17,"
     "-388)", 2.0, 6.0),
    ("dialogue_advance", {}, "STOPPED: loading on screen\nWHERE Nautiloid -> Wilderness", 2.0, 3.0),
]
NAUT3_STAGE = {"t": None, "op": "create_character", "naming_ms": 1400, "typed": "over_selection",
               "naming_stages": {"popup": 120, "type": 300}}


class SeriesReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "sessions"
        data = json.loads(FIXTURE.read_text())
        for run, rows in data.items():
            d = cls.root / run
            d.mkdir(parents=True)
            write_session(d, [r[:5] for r in rows])
        d = cls.root / "naut3"
        d.mkdir()
        write_session(d, NAUT3)
        stage = [*STAGE, {**NAUT3_STAGE, "t": 1001.0}]
        (cls.root.parent / "stage_ms.jsonl").write_text("".join(json.dumps(r) + "\n" for r in stage))
        # naut2's runs.jsonl names its runs; naut has none (still running: the dirs that exist)
        (cls.root / "naut2" / "runs.jsonl").write_text("".join(
            json.dumps({"series": "naut2", "run": k + 1, "playthrough": p, "wall_min": 10.0}) + "\n"
            for k, p in enumerate(RUNS["naut2"])))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def calls(self, run):
        cs, _ = SR.load_run(self.root / run)
        return cs

    def test_series_runs_from_records_and_dirs(self):
        self.assertEqual([p for p, _ in SR.series_runs("naut2", self.root)], RUNS["naut2"])
        self.assertEqual([p for p, r in SR.series_runs("naut", self.root)], ["naut"])

    def test_phases_on_naut2_run1(self):
        rows = SR.phase_rows(self.calls("naut2"), [])
        self.assertEqual(rows["helm ladder"]["refused"], 2)
        self.assertEqual(rows["helm ladder"]["turns"], 7)
        self.assertEqual(rows["transponder dash"]["refused"], 4)
        self.assertAlmostEqual(rows["transponder dash"]["wall"], 199, delta=5)   # 199 s on the real timeline
        r3 = SR.phase_rows(self.calls("naut2-r3"), [])
        self.assertEqual(r3["helm ladder"]["refused"], 5)   # run 3's 95 s ladder episode
        self.assertGreater(r3["helm ladder"]["wall"], 90)

    def test_naut2_signatures_fail_with_the_known_lines(self):
        cs, cs2, cs3 = (self.calls(r) for r in RUNS["naut2"])
        ctx = {"stage_ms": [{**STAGE[0], "t": cs[0]["t"] + 30}]}   # the fixture's clock starts at 1000
        v = lambda key, c: SR.verdict(c, dict((k, f) for k, _, f in SR.SIGNATURES)[key], ctx)  # noqa: E731
        st, ev, *_ = v("who-acting", cs)
        self.assertEqual((st, cs[ev[0]]["name"]), ("FAILED", "use_ability"))
        self.assertIn("not this character's turn", ev[1])
        self.assertIn("objective marker", v("move-oN", cs)[1][1])
        self.assertIn("→ partial 8.7m: FAILED — beyond movement", v("partial", cs)[1][1])
        self.assertEqual(v("walk-clip", cs)[0], "FAILED")
        self.assertIn("early wake", v("wait-arrival", cs)[1][1])
        self.assertIn("e56 climbed again", v("helm-once", cs)[1][1])
        self.assertEqual(v("helm-chain", cs)[0], "NOT-SEEN")         # run 1 climbed one call at a time
        self.assertIn("2. interact: FAILED — no walkable path", v("helm-chain", cs2)[1][1])
        self.assertIn("first helm climb down", v("helm-hint", cs)[1][1])
        self.assertIn("jump_to", v("wedged", cs3)[1][1])
        self.assertIn("started (66.8m)", v("moved-on", cs)[1][1])
        self.assertIn("get_map right after the arrival", v("arrival-reread", cs)[1][1])
        self.assertEqual(v("map-objectives", cs)[0], "FAILED")
        self.assertIn("no FIRST LOOK", v("cc-first-look", cs)[1][1])
        self.assertIn("naming_ms=3288", v("cc-naming", cs)[1][1])

    def test_post_fix_run_proves_every_signature(self):
        cs = self.calls("naut3")
        ctx = {"stage_ms": [{**NAUT3_STAGE, "t": cs[0]["t"]}]}
        got = {k: SR.verdict(cs, f, ctx)[0] for k, _, f in SR.SIGNATURES}
        self.assertEqual({k for k, v in got.items() if v != "PROVED"}, {"wedged"}, got)  # no wedged hint: NOT-SEEN
        ph = SR.phase_rows(cs, [])
        self.assertTrue(all(ph[p] is not None for p in SR.PHASES), ph)
        self.assertEqual(ph["helm ladder"]["refused"], 1)

    def test_report_runs_end_to_end(self):
        out = SR.report("naut2", "naut", self.root)
        self.assertIn("SERIES naut2 vs naut", out)
        self.assertIn("helm ladder", out)
        self.assertRegex(out, r"FAILED\s+who-acting .*\n\s+runs: naut2:FAILED, naut2-r2:FAILED, naut2-r3:FAILED"
                              r"\s+\(was naut: FAILED\)")
        self.assertIn("unneeded model turns:", out)
        self.assertIn("use_ability: FAILED it is not this character's turn", out)


if __name__ == "__main__":
    if "--rebuild" in sys.argv:
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(json.dumps(rebuild(SR.sessions_root()), ensure_ascii=False, separators=(",", ":")))
        print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size // 1024} KB)")
        sys.exit(0)
    unittest.main()
