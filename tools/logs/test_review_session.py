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
        # vis V7: downs from the debrief's `downed:` line (the results had no +Downed: 0 and 0 before)
        self.assertIn("11:43:15 340 s r4 hp-421 downs 1 reload", f)
        self.assertIn("11:50:17 533 s r7 hp-790 downs 4 reload", f)
        self.assertIn("retry of S065 11:43:15 (reload, hp-421, downs 1): further (killed 2 vs 0)", f)   # vis V6
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
    def test_runner_top_only_from_the_sessions_own_events(self):
        # S083 (03:36-04:03): S082's crash (session S082) and game_oom (no session, 03:33) were S083's TOP 1.
        lo, hi = 1000.0, 2600.0
        evs = [{"t": lo - 125, "kind": "fault", "fault": "crash", "session": "S082"},
               {"t": lo - 123, "kind": "game_oom", "why": "ramwatch killed the game"},
               {"t": lo + 50, "kind": "fault", "fault": "hang", "session": "S083"},
               {"t": hi + 60, "kind": "recovered", "session": "S083"}]
        with tempfile.TemporaryDirectory() as d:
            out, issues = RS.runner(Path(d), "S083", evs, lo, hi)
        text = "\n".join(out)
        self.assertEqual([i["title"].split(":")[0] for i in issues], ["runner fault"])
        self.assertIn('"hang"', issues[0]["title"])
        self.assertNotIn("S082", text)   # another session's events are its own
        self.assertIn("**game_oom** (before S083 started)", text)
        self.assertIn("**recovered** (after S083 ended)", text)
        evs.append({"t": lo + 70, "kind": "game_oom", "why": "x"})   # no session, inside the window: this one's
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(len(RS.runner(Path(d), "S083", evs, lo, hi)[1]), 2)

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

    def test_session_watch_calls_only_row_skips_batches(self):
        """V13, S071 #100: a do_actions use_ability out of combat answered `FAILED — choose a variant`, and the
        combat_turn row "variant pick wording" read it as its FAIL. A row whose when says "calls only" matches its
        tools' own calls, not batches."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        doc = Path(tmp.name) / "SESSION-WATCH.md"
        row = ("| variant pick wording (act, S043+) | {when} \"Lay on Hands\"/\"Spiritual Weapon\" by base name | "
               "`Target_LayOnHands: took the biggest heal variant …` and no `choose a variant` in that result | "
               "`FAILED — choose a variant` in a combat_turn result |\n")
        head = "## Combat (act)\n| item | happens when | PASS | FAIL |\n|---|---|---|---|\n"
        batch = call("do_actions", "FAILED — choose a variant: Target_LayOnHands_SmallHeal, Target_LayOnHands_BigHeal, "
                     "Target_LayOnHands_Cure\nSTOPPED: step 1 use_ability failed",
                     {"actions": '[{"tool":"use_ability","ability":"Lay on Hands","target":"e1"}]'}, k=100)
        turn = call("combat_turn", "TURN e4 Claude (round 2)\nbonus: Target_LayOnHands: took the biggest heal variant "
                    "(Target_LayOnHands_BigHeal) on e1 Gale\nend_turn: done", {"plan": "{}"}, k=120)
        # the PASS cell's "in that result" is a condition the check can't judge (dropped): a combat_turn is "seen"
        for when, want in (("combat_turn calls only:", (1, 0, 0, "seen")), ("combat_turn", (2, 0, 1, "FAIL?"))):
            doc.write_text(head + row.format(when=when))
            v = {r[0]: r[1:5] for r in SW.check([batch, turn], SW.parse_watch(doc))}
            self.assertEqual(v["variant pick wording (act, S043+)"], want, when)

    def test_session_watch_heal_row_names_heals_only(self):
        """V21, S080 #39: the V16 row's `N hit … dmg` (its `ours:` is too short to match) read Gale's Magic Missile
        as a heal with damage, FAIL? x2. The row now names the heal spells."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        doc = Path(tmp.name) / "SESSION-WATCH.md"
        head = "## Fights (vis)\n| item | happens when | PASS | FAIL |\n|---|---|---|---|\n"
        new = ("a heal with dmg in ours (heal names only, vis V21: S080 matched Magic Missile): `Cure Wounds x… hit/` or "
               "`Healing Word x… hit/` or `Prayer of Healing x… hit/` or `Heal x… hit/` or `Lay on Hands x… hit/`")
        mm = call("load_game", "FIGHT DEBRIEF (reloaded, 5 rounds, 8m41s) vs Viconia DeVir\n  ours: Gale Magic Missile "
                  "x2: 11 hit/0 miss, up to 2 targets, 113 dmg; Claude Divine Smite x5: 14 hit/0 miss, 104 dmg", k=39)
        heal = call("load_game", "FIGHT DEBRIEF (reloaded, 4 rounds, 6m) vs Cazador Szarr\n  ours: Gale Scorching Ray "
                    "x1: 5 hit/0 miss, 52 dmg; Shadowheart Mass Cure Wounds x1: 4 hit/0 miss, 49 dmg", k=65)
        for fail, want in (("a heal with `N hit … dmg` in `ours:`", (1, 0, 1, "FAIL?")), (new, (0, 0, 0, "NOT-SEEN"))):
            doc.write_text(head + f"| fight facts V16 (vis V16) | any fight | x | {fail} |\n")
            v = {r[0]: r[1:5] for r in SW.check([mm], SW.parse_watch(doc))}
            self.assertEqual(v["fight facts V16 (vis V16)"], want, fail)
        self.assertEqual({r[0]: r[1:5] for r in SW.check([heal], SW.parse_watch(doc))}["fight facts V16 (vis V16)"],
                         (1, 0, 1, "FAIL?"))   # S074's real one still fails

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


class MultiStream(unittest.TestCase):
    """vis V19: S078 crashed at 1.2 min and its resume S078-r1 made 88 pts in 23.5 min, but S078.review.md read
    "1.2 min ... 0 pts (0/h)", 0 fights and stream 1's cost and latency only. The headline, the gaps, the fights and
    the cost now count every stream (Sxxx, Sxxx-r1, ...); the latency targets pool the streams' gaps and reads."""

    def stream(self, sd, stem, t, acts, reads, cost, prog, lat):
        uses, turns, recs = [], [], []
        for i, (name, t0, t1) in enumerate(sorted([(n, a, b) for n, a, b in acts] + [("look", a, a + ms / 1000)
                                                                                    for a, ms in reads],
                                                  key=lambda x: x[1])):
            uses.append({"type": "assistant", "message": {"id": f"{stem}m{i}", "content": [
                {"type": "tool_use", "id": f"{stem}{i}", "name": f"mcp__bg3__{name}", "input": {}}]}})
            uses.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"{stem}{i}",
                                                                  "content": [{"type": "text", "text": "done"}]}]}})
            turns += [{"type": "use", "t": t + t0, "id": f"{stem}{i}"}, {"type": "result", "t": t + t1, "id": f"{stem}{i}"}]
            recs.append({"type": "tool", "tool": name, "t0": t + t0, "t1": t + t1, "ms": round((t1 - t0) * 1000, 1)})
        uses.append({"type": "result", "total_cost_usd": cost})
        (sd / f"{stem}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in uses))
        (sd / f"{stem}.turns.jsonl").write_text("".join(json.dumps(e) + "\n" for e in turns))
        (sd / f"{stem}.metrics.jsonl").write_text("".join(json.dumps(e) + "\n" for e in recs))
        (sd / f"{stem}.progress.json").write_text(json.dumps(prog))
        (sd / f"{stem}.latency.json").write_text(json.dumps(dict(lat, start=t, end=t + 60 * lat["minutes"])))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rt = Path(self.tmp.name)
        sd = self.rt / "sessions" / RUN
        sd.mkdir(parents=True)
        t0 = 1792275500.0
        # S100: one explore gap of 19 s, a 50 ms read; S100-r1: gaps 0.5, 1 and 26 s, reads of 150 and 170 ms
        self.stream(sd, "S100", t0, [("move_to", 0, 1), ("move_to", 20, 21)], [(21.2, 50)], 0.30,
                    {"minutes": 1.2, "points": 0, "per_hour": 0.0, "events": 0, "by_kind": {},
                     "longest_no_progress_min": 1.2, "stuck_episodes": 0, "stuck_min": 0.0},
                    {"minutes": 1.2, "apm": 1.7, "targets": {}})
        self.stream(sd, "S100-r1", t0 + 300, [("move_to", 0, 1), ("move_to", 1.5, 2), ("move_to", 3, 4),
                                              ("move_to", 30, 31)], [(31.5, 150), (32.0, 170)], 5.56,
                    {"minutes": 23.5, "points": 88, "per_hour": 224.7, "events": 48,
                     "by_kind": {"talk": 17, "explore": 12}, "longest_no_progress_min": 6.9, "stuck_episodes": 1,
                     "stuck_min": 2.0},
                    {"minutes": 23.5, "apm": 5.96, "targets": {}})
        keeper = {"type": "result", "total_cost_usd": 0.31}
        (sd / "S100-keeper.jsonl").write_text(json.dumps(keeper) + "\n")
        self.r = RS.review(RUN, "S100", self.rt / "sessions", self.rt)

    def section(self, title):
        m = re.search(rf"^## {re.escape(title)}.*?(?=^## |\Z)", self.r, re.M | re.S)
        self.assertIsNotNone(m, title)
        return m.group(0)

    def test_headline_counts_every_stream(self):
        h = self.section("Headline")
        self.assertIn("24.7 min (1.2 + 23.5: S100, S100-r1), 9 calls (2 stream(s))", h)   # old: 1.2 min
        self.assertIn("story: **88 pts (214/h)**, 48 progress events (pts: talk 17, explore 12); longest without "
                      "progress 6.9 min; stuck episodes 1 (2.0 min)", h)                       # old: 0 pts (0/h)
        self.assertIn("cost: play $5.86 + keeper $0.31", h)                                    # old: $0.30
        # pooled: explore gaps 19, 0.5, 1, 26 → 10.0 (S100 alone 19, S100-r1 alone 1); reads 50, 150, 170 → 150
        self.assertIn("latency: idle median (explore) 10.0s **FAIL** (target <2.0s); gaps >10 s (non-dialogue) 2 "
                      "**FAIL** (target 0); bridge RT median 150.0ms **FAIL** (target <100.0ms) (all streams)", h)
        self.assertIn("- live feel S100: APM 1.7", h)
        self.assertIn("- live feel S100-r1: APM 5.96", h)

    def test_gaps_of_every_stream(self):
        g = self.section("Gaps > 10 s")
        self.assertIn("(2; target 0)", g)
        self.assertIn("**19.0 s** explore", g)
        self.assertIn("**26.0 s** explore", g)   # S100-r1's: old code read S100's metrics only

    def test_fights_of_every_stream(self):
        seen = []
        mine = RS.F.mine
        RS.F.mine = lambda run, sid: seen.append(sid) or []
        try:
            RS.fights_section(self.rt / "sessions", RUN, "S100", self.rt / "none.jsonl")
        finally:
            RS.F.mine = mine
        self.assertEqual(seen, ["S100", "S100-r1"])


def overlap_refusal(c):
    return RS.overlap.refusal(c)


class EffectNotCall(unittest.TestCase):
    """vis V9: S066 PASS? verdicts on calls that did nothing (the real call texts, the rows as SESSION-WATCH.md has
    them): a point scroll cast on its reader, and a 2-target note under a cast the game never started."""

    ROWS = ("| scroll by name (setup, DONE-OFFLINE) | use_ability with a carried scroll | `(Scroll of … read, cast on …` "
            "| `no action called 'Scroll of …'`, or click_on right after a scroll use |\n"
            "| Bless on 3 (act, S031+) | use_ability/combat_turn with 3+ targets | `targets: cast on the first 2 only` and "
            "`done` | `more than two targets is not supported yet` |\n"
            "| hotbar N-target cast (se + act wiring) | use_ability / a combat_turn step with 3+ targets | with the switch "
            "off: `cast on the first 2 only, 1 of 3 NOT targeted (the hotbar route for more is off)` | `fired_early` |\n")
    EB = ("TURN e2 Wyll (round 1) — waited 7.1s for the turn\n"
          "action: Projectile_EldritchBlast: FAILED — the game never started it (3 s, no cast, no step)\n"
          "targets: cast on the first 2 only, 1 of 3 NOT targeted (the hotbar route for more is off); left out: e356 Orin\n"
          "end_turn: done (still to act: e3)")
    CONE = ('[{"who":"Wyll","plan":{"action":[{"ability":"Scroll of Cone of Cold","x,y,z":"56,-53,996.5"},'
            '{"ability":"Eldritch Blast","target":"e354"}],"on_reaction":"skip"}}]')

    def rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        doc = Path(tmp.name) / "SESSION-WATCH.md"
        doc.write_text("## Combat (act)\n| item | happens when | PASS | FAIL |\n|---|---|---|---|\n" + self.ROWS)
        return SW.parse_watch(doc)

    def verdicts(self, calls):
        return {r[0].split(" (")[0]: r[1:5] for r in SW.check(calls, self.rows())}

    def test_s066_59_a_cast_the_game_never_started_is_no_pass(self):
        v = self.verdicts([call("combat_turn", self.EB, {"who": "Wyll", "plan": "{}"}, k=58)])
        self.assertEqual(v["Bless on 3"], (1, 0, 0, "seen"))
        self.assertEqual(v["hotbar N-target cast"], (1, 0, 0, "seen"))
        done = self.EB.replace("FAILED — the game never started it (3 s, no cast, no step)",
                               "done — damage=[e356 Orin 1 Force; e356 Orin 1 Force]")
        v = self.verdicts([call("combat_turn", done, {"who": "Wyll", "plan": "{}"}, k=58)])
        self.assertEqual(v["Bless on 3"], (1, 1, 0, "PASS?"))
        self.assertEqual(v["hotbar N-target cast"], (1, 1, 0, "PASS?"))

    def test_s066_a_point_scroll_cast_on_its_reader_is_a_fail(self):
        misty = call("use_ability", "use_ability: done — used after={@(17.8,-42.8,1004.8)} (Scroll of Misty Step read, "
                     "cast on e1 Karlach by a click)\nEVENTS\n  used e1: e17 Scroll of Misty Step success",
                     {"who": "Karlach", "ability": "Scroll of Misty Step", "x": 24, "y": -43, "z": 1005}, k=142)
        cone = call("combat_turn", "TURN e1 Karlach (round 3)\nend_turn: done\nTURN e2 Wyll (round 3) — waited 13.9s for "
                    "the turn\naction: Scroll of Cone of Cold: done — read in combat used (Scroll of Cone of Cold read, "
                    "cast on e2 Wyll by a click); its action is spent", {"plan": self.CONE}, k=65)
        for c in (misty, cone):
            self.assertEqual(self.verdicts([c])["scroll by name"], (1, 0, 1, "FAIL?"), c["k"])
        fear = call("combat_turn", "TURN e2 Wyll (round 2)\naction: Scroll of Fear e353: done — read in combat used "
                    "(Scroll of Fear read, cast on e353 Reaper of Bhaal Hiila by a click); its action is spent",
                    {"plan": '[{"who":"Wyll","plan":{"action":[{"ability":"Scroll of Fear","target":"e353"}]}}]'}, k=61)
        self.assertEqual(self.verdicts([fear])["scroll by name"], (1, 1, 0, "PASS?"))
        armour = call("use_ability", "use_ability: done — (Scroll of Mage Armour read, cast on e1 Karlach by a click)",
                      {"who": "Karlach", "ability": "Scroll of Mage Armour"}, k=3)   # a self-buff scroll: no point
        self.assertEqual(self.verdicts([armour])["scroll by name"], (1, 1, 0, "PASS?"))


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
