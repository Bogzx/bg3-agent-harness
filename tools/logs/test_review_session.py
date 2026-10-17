"""review_session (and the log tools it reuses) on a fixture cut from the real claude-run-2 S065 (Act 3, Orin) and
S050 (Act 3, Lower City) files, plus small synthetic cases for the 2026-10-17 fixes of the older tools.

    python3 tools/logs/test_review_session.py              # the tests
    python3 tools/logs/test_review_session.py --rebuild    # re-cut fixtures/review_sessions.json.gz from runtime/

The fixture keeps every stream event the review reads, with each tool result's whole text (as triage.result_text
gives it), minus the stream's duplicate tool_use_result copies, thinking blocks and RPC op lists; the runner's
events and log lines of the sessions' windows. --rebuild also checks that the review of the cut equals the review
of the real files.
"""
import gzip
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import review_session as RS  # noqa: E402
import fights as F  # noqa: E402
import session_watch as SW  # noqa: E402
import context_model as CM  # noqa: E402
import triage  # noqa: E402

FIXTURE = HERE / "fixtures" / "review_sessions.json.gz"
SW.WATCH_FILE = HERE / "fixtures" / "session_watch.md"   # a fixed copy of docs/SESSION-WATCH.md's rows
RUN = "claude-run-2"
SIDS = ("S065", "S050")
WINDOW_S = 600


def slim_stream(path: Path) -> str:
    out = []
    for line in path.open(errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "result":
            keep = ("total_cost_usd", "usage", "duration_api_ms", "duration_ms", "num_turns")
            out.append({"type": "result", **{k: d.get(k) for k in keep}, "result": (d.get("result") or "")[:300]})
            continue
        m = d.get("message") or {}
        if t not in ("assistant", "user") or not isinstance(m.get("content"), list):
            continue
        blocks = []
        for b in m["content"]:
            if b.get("type") == "tool_use":
                blocks.append({k: b.get(k) for k in ("type", "id", "name", "input")})
            elif b.get("type") == "text" and t == "assistant":
                blocks.append({"type": "text", "text": b.get("text", "")[:300]})
            elif b.get("type") == "tool_result":
                blocks.append({"type": "tool_result", "tool_use_id": b["tool_use_id"], "is_error": b.get("is_error", False),
                               "content": [{"type": "text", "text": triage.result_text(b)}]})
        if blocks:
            out.append({"type": t, "message": {"id": m.get("id"), "content": blocks}})
    return "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out)


def rebuild(rt: Path) -> dict:
    sd = rt / "sessions" / RUN
    files, windows = {}, []
    for sid in SIDS:
        b = sd / sid
        files[f"{sid}.jsonl"] = slim_stream(Path(f"{b}.jsonl"))
        files[f"{sid}.turns.jsonl"] = Path(f"{b}.turns.jsonl").read_text()
        files[f"{sid}.metrics.jsonl"] = "".join(
            json.dumps({k: v for k, v in json.loads(l).items() if k != "ops"}) + "\n"
            for l in Path(f"{b}.metrics.jsonl").open() if l.strip())
        for ext in (".progress.json", ".progress.jsonl", ".latency.json"):
            files[sid + ext] = Path(f"{b}{ext}").read_text()
        files[f"{sid}-keeper.jsonl"] = slim_stream(Path(f"{b}-keeper.jsonl"))
        lat = json.loads(files[sid + ".latency.json"])
        windows.append((lat["start"] - WINDOW_S, lat["end"] + WINDOW_S))
    inside = lambda t: any(a <= t <= b for a, b in windows)  # noqa: E731
    files["runner_events.jsonl"] = "".join(l for l in (sd / "runner_events.jsonl").open()
                                           if inside(json.loads(l).get("t", 0)))
    log = []
    for line in (rt / "run-cont.log").open(errors="replace"):
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
        if m and inside(RS.time.mktime(RS.time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))):
            log.append(line)
    return {"sessions": files, "run-cont.log": "".join(log)}


def unpack(fx: dict, root: Path) -> tuple[Path, Path]:
    sd = root / "sessions" / RUN
    sd.mkdir(parents=True)
    for name, text in fx["sessions"].items():
        (sd / name).write_text(text)
    (root / "run-cont.log").write_text(fx["run-cont.log"])
    return root / "sessions", root


def body(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("(tools/logs/review_session.py"))


class ReviewFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        fx = json.loads(gzip.decompress(FIXTURE.read_bytes()))
        cls.sessions, cls.rt = unpack(fx, Path(cls.tmp.name))
        cls.r = {sid: RS.review(RUN, sid, cls.sessions, cls.rt) for sid in SIDS}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def section(self, sid, title):
        m = re.search(rf"^## {re.escape(title)}.*?(?=^## |\Z)", self.r[sid], re.M | re.S)
        self.assertIsNotNone(m, title)
        return m.group(0)

    def test_s065_headline(self):
        h = self.section("S065", "Headline")
        self.assertIn("**23 pts (59/h)**", h)
        self.assertIn("gaps >10 s (non-dialogue) 7 **FAIL**", h)
        self.assertIn("bridge RT median 70.3ms PASS", h)
        self.assertIn("fights: 2 (2 reloaded", h)
        self.assertIn("party deaths ['Karlach']", h)
        self.assertIn("play $5.37 + keeper $0.32", h)
        self.assertIn("pipelined 17.6 %", h)

    def test_s065_gaps_have_causes(self):
        g = self.section("S065", "Gaps > 10 s")
        self.assertEqual(len(re.findall(r"^- \d\d:\d\d:\d\d", g, re.M)), 7)
        self.assertIn("12:00:01 **32.6 s** explore → next `manage_inventory`: recovering from a refusal: camp: "
                      "FAILED cannot travel right now [act] — S065 #123 +1182s camp", g)
        self.assertIn("a reload (load_game) and re-orientation [act] — S065 #77 +516s load_game", g)

    def test_s065_refusals_ranked_by_model_seconds(self):
        r = self.section("S065", "Refusal episodes")
        secs = [float(x) for x in re.findall(r"^- \*\*([\d.]+) s\*\*", r, re.M)]
        self.assertEqual(secs, sorted(secs, reverse=True))
        self.assertIn("`do_actions: FAILED it is not this character's turn` [act] — S065 #88 +695s do_actions", r)

    def test_s065_fights_retry_and_debrief(self):
        f = self.section("S065", "Fights")
        self.assertIn("reload (reloaded) → S065-pre-orin: Death's Head of Bhaal Fustian, Invoker of Bhaal Hivune, Orin", f)
        self.assertIn("retry of S065 11:43:15 (reload, hp-421, downs 0): further (killed 2 vs 0)", f)   # vis V6
        self.assertNotIn("Spiritual Weapon", f.split("top")[0])

    def test_s065_watch_and_top(self):
        w = self.section("S065", "SESSION-WATCH")
        self.assertIn("FAIL? Spiritual Weapon variant (act, S034+) (trigger 39, pass 2, fail 1): #61 +258s combat_turn: "
                      "has no such action → its own weapon attack", w)
        # disarm 'Sanguine Vent is ambiguous': "when one is clearly nearer" is judged by eye, so only seen
        self.assertIn("seen (a tool the row names ran), no PASS/FAIL signature: nearest namesake ×4", w)
        self.assertIn("check from R/B/L/M by hand: Noesis releases bounded", w)
        self.assertNotIn("who-acting (act", w)   # the naut3 rows come from series_report.SIGNATURES only
        self.assertIn("FAILED who-acting", w)
        t = self.section("S065", "TOP 10")
        items = re.findall(r"^\d+\. \[(act|vis|setup)\] \*\*(\d+) s\*\*", t, re.M)
        self.assertEqual(len(items), 10)
        self.assertEqual([int(s) for _, s in items], sorted((int(s) for _, s in items), reverse=True))
        self.assertNotIn("fight reloaded", t)   # competence, not harness

    def test_s065_keeper_and_runner(self):
        self.assertIn("keeper: ok in 66.4 s; files 8; commit 1a87073", self.section("S065", "Memory / keeper"))
        u = self.section("S065", "Runner")
        self.assertIn("S065: early exit: claude ended with 6.6 of 30 min left", u)
        self.assertNotIn("S064", u)   # the previous session's lines are its own

    def test_s050(self):
        self.assertIn("**92 pts (243/h)**", self.section("S050", "Headline"))
        self.assertIn("FAIL? lockpick = right-click menu (trigger 5, pass 1, fail 4): #150 +1253s lockpick: could not "
                      "point at the target", self.section("S050", "SESSION-WATCH"))
        t = self.section("S050", "TOP 10")
        self.assertRegex(t, r"1\. \[setup\] \*\*64 s\*\*, ×1: gaps > 10 s: notes/memory during play")
        self.assertIn("refused: lockpick: FAILED could not point at the target", t)
        f = self.section("S050", "Fights")
        self.assertNotIn("INCIDENT: Spiritual Weapon", f)   # a summon's expiry is not a party death


def call(name, text, inp=None, k=0, t=100.0):
    return {"name": name, "input": inp or {}, "text": text, "t": t + k, "k": k}


class OlderToolFixes(unittest.TestCase):
    def test_fights_party_from_turn_headers_not_summons(self):
        calls = [
            {"tool": "get_status", "res": "PARTY e1 Corran Vell Fighter1 HP 24/24\nPARTY e29 Lae'zel Fighter1 HP 24/24"},
            {"tool": "combat_turn", "res": "TURN e1 Corran Vell (round 1)\nmove e33: done\nTURN e9 Spiritual Weapon "
                                           "(round 1)\n"},
            {"tool": "look", "res": "PARTY e9 Spiritual Weapon L1 HP 20/20 summon of e9"},
        ]
        party = F.party_names(calls)
        self.assertIn("Corran Vell", party)
        self.assertIn("Lae'zel", party)
        self.assertNotIn("Spiritual Weapon", party)
        self.assertNotIn("Spiritual", party)

    def test_fights_debrief(self):
        db = F.debrief("x\nFIGHT DEBRIEF (near_wipe, 6 rounds, 3m02s) vs Lesser Imp x4, Commander Zhalk +1 more\n")
        self.assertEqual(db, ("near_wipe", 6, ["Lesser Imp", "Commander Zhalk"]))
        self.assertIsNone(F.debrief("no debrief"))
        f = {"outcome": "won", "downs": 0, "party_died": [], "debrief": "near_wipe"}
        self.assertTrue(F.bad(f))

    def test_session_watch_counts_calls_not_text_copies(self):
        calls = [call("Read", "lockpick: FAILED — could not point at the target  # the playbook quotes it", k=0),
                 call("lockpick", "lockpick: FAILED — could not point at the target\ncould not point at the target", k=1),
                 call("lockpick", "lockpick: done — click={ok tries=1 menu=Lockpick} opened=roll", {"target": "e5"}, k=2)]
        rows = {r[0]: r for r in SW.check(calls)}
        _, nt, np_, nf, verdict, ex = rows["lockpick = right-click menu"]
        self.assertEqual((nt, np_, nf, verdict), (2, 1, 1, "FAIL?"))
        self.assertTrue(ex.startswith("#1 +1s lockpick: could not point at the target"))

    def test_session_watch_reads_the_rows(self):
        """The rows come from the markdown: a row added there counts at once; | inside a signature stays in its cell;
        conditions it can't judge, negated and R/B-sourced signatures and bare keys are left out."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        doc = Path(tmp.name) / "SESSION-WATCH.md"
        doc.write_text(SW.WATCH_FILE.read_text() + "\n## New (vis)\n| item | happens when | PASS | FAIL |\n|---|---|---|---|\n"
                       "| brand new row (vis) | any get_map | `MAP eN at N m` / `x\\|y` | a bare `nothing` twice; "
                       "or `oops: N left` |\n")
        rows = {r["item"]: r for r in SW.parse_watch(doc)}
        new = rows["brand new row (vis)"]
        self.assertEqual(new["pass"], [[r"MAP\s+e\d+\s+at\s+\d+(?:\.\d+)?\s+m"]])   # x|y: < 6 letters, dropped
        self.assertEqual(new["fail"], [[r"oops:\s+\d+(?:\.\d+)?\s+left"]])         # "bare … twice": by eye
        self.assertEqual(rows["identical items no longer ambiguous"]["fail"], [])   # `ambiguous` alone: a bare key
        self.assertEqual(rows["Noesis releases bounded (se watch item)"]["pass"], [])  # B `…`: another file
        self.assertTrue(rows["who-acting (act, ACT-P2, BG3AI_COMBAT_WHO_ACTING)"]["series"])
        lock = rows["lockpick = right-click menu"]
        self.assertEqual(len(lock["fail"]), 3)   # three alternatives split on " / "; the B crash line left out
        self.assertEqual(len(lock["pass"][0]), 3)   # done AND click={ok … menu=Lockpick} AND opened=roll
        hit = SW.check([call("get_map", "MAP e12 at 4.5 m"), call("get_map", "oops: 3 left")], list(rows.values()))
        self.assertEqual({r[0]: r[1:5] for r in hit}["brand new row (vis)"], (2, 1, 1, "FAIL?"))
        self.assertNotIn("who-acting (act, ACT-P2, BG3AI_COMBAT_WHO_ACTING)", {r[0] for r in hit})
        self.assertEqual(SW.cells("| a `x|y` b | c \\| d |"), ["a `x|y` b", "c | d"])

    def test_session_watch_main_tree_from_worktree(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        main, wt = Path(tmp.name) / "main", Path(tmp.name) / "wt"
        (main / ".git/worktrees/wt").mkdir(parents=True)
        (main / "docs").mkdir()
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
        self.assertEqual(SW.main_tree(wt), main)
        self.assertEqual(SW.main_tree(main), main)
        old, SW.WATCH_FILE = SW.WATCH_FILE, None
        try:
            self.assertEqual(SW.watch_path(wt), main / "docs/SESSION-WATCH.md")
        finally:
            SW.WATCH_FILE = old

    def test_retry_only_cites_earlier_fights(self):
        """The fights store holds every session, later ones too (S063's review cited S064's fight as its past)."""
        def fight(sid, t, outcome="reload"):
            return {"session": sid, "start": f"t{t}", "t": t, "dur_s": 9, "rounds": 2, "hp_lost": 10, "downs": 0,
                    "outcome": outcome, "key": "A, B", "enemies": {"A": 1, "B": 1}, "party_died": [], "harness": {},
                    "abilities": {}, "reload_to": None}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "fights.jsonl"
        store.write_text("".join(json.dumps(f) + "\n" for f in (fight("S062", 50), fight("S064", 300))))
        mine = RS.F.mine
        RS.F.mine = lambda run, sid: [fight("S063", 100), fight("S063", 200)]
        try:
            out, _ = RS.fights_section(Path(tmp.name), RUN, "S063", store)
        finally:
            RS.F.mine = mine
        retries = [l for l in out if "retry of" in l]
        self.assertEqual([l.split("retry of ")[1][:9] for l in retries], ["S062 t50 ", "S063 t100"])

    def test_helm_signatures_only_on_the_nautiloid(self):
        climbs = [call("interact", "interact: done — used climbed=up dy=6.1", {"target": "e573"}, k=0),
                  call("interact", "interact: done — used climbed=down dy=-6.1", {"target": "e573"}, k=1)]
        for c in climbs:
            c.update(t0=100.0, t_res=c["t"] + 0.5, session="S1", dur=0.5, think=1.0)
            c["refusal"] = overlap_refusal(c)
        out, _ = RS.watch([dict(c) for c in climbs], {})
        self.assertNotIn("helm", "\n".join(out))
        nautiloid = [dict(c, text=c["text"] + "\nWHERE Nautiloid") for c in climbs]
        out, _ = RS.watch(nautiloid, {})
        self.assertIn("FAILED helm-", "\n".join(out))

    def test_context_model_one_session_fit(self):
        s = {"seq": [{"read": 4_000_000, "write": 64_000}], "cost": 1.0}
        r, w = CM.fit_prices([s])
        self.assertAlmostEqual(w, 12.5 * r)
        self.assertAlmostEqual(4.0 * r + 0.064 * w, 1.0, places=6)

    def test_gap_cause(self):
        calls = [call("interact", "FAILED: it is locked", k=0), call("wiki_page", "x", k=3), call("interact", "ok", k=12)]
        for c in calls:
            c["refusal"] = overlap_refusal(c)
            c["t_res"] = c["t"] + 0.5
            c["t0"], c["session"] = 100.0, "S1"
        cause, own, c = RS.gap_cause(calls, 100.5, 112.0)
        self.assertTrue(cause.startswith("recovering from a refusal: interact: FAILED it is locked"))
        self.assertEqual((own, c["k"]), ("act", 0))
        calls[0]["refusal"] = None
        self.assertEqual(RS.gap_cause(calls, 100.5, 112.0)[:2], ("wiki lookups", "setup"))
        self.assertEqual(RS.owner("travel_to"), "vis")
        self.assertEqual(RS.owner("memory_write"), "setup")
        self.assertEqual(RS.owner("combat_turn"), "act")


class SyntheticSession(unittest.TestCase):
    """mine_session / story_stalls / context_cost on a 3-call session: the old bridge's {"result": ...} string
    result and the new text-block result read alike; WHERE carried into a stall window; output tokens."""

    def setUp(self):
        import contextlib, io, mine_session as MS, story_stalls as SS, context_cost as CC  # noqa: E401
        self.MS, self.SS, self.CC, self.io, self.ctx = MS, SS, CC, io, contextlib
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        sd = root / "runtime" / "sessions" / "r"
        sd.mkdir(parents=True)
        ev = [
            {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "tool_use", "id": "a", "name": "mcp__bg3__look", "input": {}}],
                "usage": {"input_tokens": 1, "cache_read_input_tokens": 1000, "output_tokens": 2}}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a",
                                                      "content": json.dumps({"result": "WHERE Camp · Act 1\nok"})}]}},
            {"type": "assistant", "message": {"id": "m2", "content": [
                {"type": "tool_use", "id": "b", "name": "mcp__bg3__interact", "input": {"target": "e5"}}],
                "usage": {"input_tokens": 1, "cache_read_input_tokens": 1200, "output_tokens": 3}}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b", "content": [
                {"type": "text", "text": "FAILED: it is locked"}]}]}},
            {"type": "assistant", "message": {"id": "m3", "content": [
                {"type": "tool_use", "id": "c", "name": "mcp__bg3__wait_for", "input": {}}],
                "usage": {"input_tokens": 1, "cache_read_input_tokens": 1300, "output_tokens": 1}}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "c", "content": [
                {"type": "text", "text": "waited"}]}]}},
            {"type": "result", "total_cost_usd": 0.5, "usage": {"output_tokens": 777}},
        ]
        (sd / "S001.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ev))
        turns = [{"type": "use", "t": 1000.0, "id": "a"}, {"type": "result", "t": 1000.5, "id": "a"},
                 {"type": "use", "t": 1002.0, "id": "b"}, {"type": "result", "t": 1002.5, "id": "b"},
                 {"type": "use", "t": 1400.0, "id": "c"}, {"type": "result", "t": 1401.0, "id": "c"}]
        (sd / "S001.turns.jsonl").write_text("".join(json.dumps(e) + "\n" for e in turns))
        (sd / "S001.progress.jsonl").write_text(json.dumps({"type": "progress", "t": 1001.0, "kind": "talk",
                                                            "points": 2}) + "\n")
        MS.SESS, MS.OUT = str(root / "runtime" / "sessions"), str(root / "runtime" / "mined")
        SS.RUNTIME = CC.RUNTIME = str(root / "runtime")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, mod, argv):
        out = self.io.StringIO()
        old = sys.argv
        sys.argv = ["x"] + argv
        try:
            with self.ctx.redirect_stdout(out):
                mod.main()
        finally:
            sys.argv = old
        return out.getvalue()

    def test_mine_session_reads_both_result_formats(self):
        summ = self.MS.mine("r", "S001")
        recs = [json.loads(l) for l in (self.root / "runtime/mined/r/S001.calls.jsonl").open()]
        self.assertTrue(recs[0]["res"].startswith("WHERE Camp"))   # unwrapped, not '{"result": ...'
        self.assertEqual([r["failed"] for r in recs], [False, True, False])
        self.assertEqual(summ["failed"], 1)

    def test_story_stalls_carries_where(self):
        self.MS.mine("r", "S001")
        out = self.run_main(self.SS, ["r", "4", "S001"])
        self.assertIn("@ Camp · Act 1", out)   # the stall window itself has no WHERE line

    def test_context_cost_output_from_result(self):
        self.assertIn("output 1k", self.run_main(self.CC, ["r", "S001"]))   # 777 from the result, not 2+3+1


def overlap_refusal(c):
    return RS.overlap.refusal(c)


def main():
    if "--rebuild" in sys.argv:
        rt = RS.RUNTIME   # $BG3AI_RUNTIME, else the main tree's (also from a worktree)
        fx = rebuild(rt)
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_bytes(gzip.compress(json.dumps(fx, ensure_ascii=False).encode(), mtime=0))
        print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size // 1024} KB)")
        with tempfile.TemporaryDirectory() as tmp:
            sessions, cut_rt = unpack(fx, Path(tmp))
            for sid in SIDS:   # the cut must review like the real files (fights store: none in both)
                a = body(RS.review(RUN, sid, sessions, cut_rt))
                b = body(RS.review(RUN, sid, rt / "sessions", Path(tmp) / "no-runtime"))
                c = body(RS.review(RUN, sid, rt / "sessions", rt))
                runner_a = a.split("## Runner")[0]
                print(f"{sid}: cut == real (before the runner section): {runner_a == b.split('## Runner')[0]}; "
                      f"runner section == real runtime's: {a.split('## Runner')[1] == c.split('## Runner')[1]}")
        return
    unittest.main()


if __name__ == "__main__":
    main()
