"""Offline tests: travel with a split party and story-area travel blocks (vis V8), from the real S064/S065 shapes.

S064 11:35:08 fast_travel(Temple of Bhaal) TIMEOUT after 66 s: Claude stood 2.9 m from the waypoint he had just
unlocked (the game moved him 2.9 m, under ui.fast_travel's 3 m mark) while Karlach/Wyll/Shadowheart stood 136 m away,
64 m above, unchained. S065 12:01:47 camp() 'done' without Wyll (3863 m away), long_rest failed 4 s later.
S065 12:00:01-12:01:12 camp, fast_travel and travel_to(waypoint) refused (Location) one tool at a time.

Run: cd bridge && uv run python -m unittest tests.test_travel_party
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_turn_macros import (
    Base,  # noqa: F401  (sets BG3AI_RUNTIME before importing server)
)

from bg3ai import areablock, longnav, party
from bg3ai.tools import core, reporting, overview, travel, inventory, batch, queueing, travel_macro, saving
from bg3ai.game import GameError, JobResult

CLAUDE = "aaaaaaaa-0000-4000-8000-000000000004"
KARLACH = "aaaaaaaa-0000-4000-8000-000000000001"
WYLL = "c774d764-4a17-48dc-b470-32ace9ce447d"
SHADOWHEART = "3ed74f06-3c60-42dc-83f6-f034cb47c679"
LVL = "LOW_Main_A"
TEMPLE_WP = {"name": "Temple of Bhaal", "id": "WAYP_LOW_BhaalTemple", "pos": [-58.4, -46.3, 1005.9]}
RUINS_WP = {"name": "Undercity Ruins", "id": "WAYP_LOW_UndercityRuins", "pos": [-130.0, 6.0, 935.0]}


def row(uuid, name, pos, chained=None, **kw):
    r = {"uuid": uuid, "name": name, "pos": pos, "region": LVL, "hp": 100, "max_hp": 100}
    if chained is not None:
        r["chained"] = chained
    r.update(kw)
    return r


def s064_status() -> dict:
    """S064 11:35-11:36: Claude at the Temple of Bhaal circle; the others 136 m away, 64 m above, unchained."""
    return {"active": CLAUDE, "level": LVL, "party": [
        row(CLAUDE, "Claude", [-55.5, -46.3, 1005.7]),
        row(KARLACH, "Karlach", [-154.2, 17.9, 937.8], chained=False),
        row(WYLL, "Wyll", [-156.4, 18.0, 938.8], chained=False),
        row(SHADOWHEART, "Shadowheart", [-157.2, 17.0, 936.8], chained=False)]}


def s065_camp_status(wyll_at_camp: bool = False) -> dict:
    """S065 12:01:47: Claude, Karlach, Shadowheart at the Lower City camp; Wyll left at the Undercity bridge."""
    camp = [-1422.2, 8.4, -2601.2]
    off = lambda dx, dz: [camp[0] + dx, camp[1], camp[2] + dz]
    return {"active": CLAUDE, "level": LVL, "party": [
        row(CLAUDE, "Claude", off(0, 0)),
        row(KARLACH, "Karlach", off(30, 18), chained=True),
        row(WYLL, "Wyll", off(5, 3) if wyll_at_camp else [-41.5, -46.3, 1002.9], chained=True),
        row(SHADOWHEART, "Shadowheart", off(-20, 15), chained=True)]}


class Fake:
    """Scripted replies: status (dict or callable), travel_block, waypoints, can_rest; run_job records and is done."""

    def __init__(self, status):
        self.status, self.block, self.wps, self.can = status, {"hud": True}, [], {}
        self.ft_error = None   # ui.fast_travel's Lua refusal (raised by game.rpc, as live)
        self.jobs, self.calls = [], []
        self.event_seq = {"server": 0, "client": 0}

    def rpc(self, op, args=None, context="server", timeout=3.0):
        self.calls.append(op)
        if op == "state.status":
            return self.status() if callable(self.status) else self.status
        if op == "ui.travel_block":
            return dict(self.block)
        if op == "nav.waypoints":
            st = self.status() if callable(self.status) else self.status
            here = party.leader_row(st)["pos"]
            return {"level": LVL, "waypoints": [dict(w, dist=round(party._d(here, w["pos"]), 1)) for w in self.wps]}
        if op == "camp.can_rest":
            return self.can
        if op == "ui.fast_travel":
            return {"can": True}
        if op == "events.since":
            return {"events": []}
        return {}

    try_rpc = rpc

    def run_job(self, op, args, context="server", wait_s=25.0, on_start=None, **kw):
        self.jobs.append((op, args))
        if op == "ui.fast_travel" and self.ft_error:
            raise GameError(self.ft_error)
        if op == "act.chain":
            return JobResult("failed", None, "timeout")  # S064 11:35: act.chain x3 timed out (136 m away)
        if op == "ui.fast_travel":
            return JobResult("done", {"arrived": args.get("waypoint"), "region": "Baldur's Gate"})
        if op == "act.go_to_camp":
            return JobResult("done", {"at_camp": True})
        return JobResult("done", {})

    def drain_events(self, ctx):
        return []

    def ops(self) -> list[str]:
        return [o for o, _ in self.jobs]


class Case(unittest.TestCase):
    def use(self, status) -> Fake:
        fake = Fake(status)
        saved = (core.game, queueing._watch_game, travel.AREA, travel.CAMP_GATHER_S)
        self.addCleanup(lambda: (setattr(core, "game", saved[0]), setattr(queueing, "_watch_game", saved[1]),
                                 setattr(travel, "AREA", saved[2]), setattr(travel, "CAMP_GATHER_S", saved[3])))
        core.game, queueing._watch_game = fake, fake
        travel.AREA = areablock.AreaBlock()
        travel.CAMP_GATHER_S = 0.3
        overview._stay.clear()
        self.addCleanup(overview._stay.clear)
        for u, n in ((CLAUDE, "Claude"), (KARLACH, "Karlach"), (WYLL, "Wyll"), (SHADOWHEART, "Shadowheart")):
            core.H.get(u, n)
        return fake


class FastTravelSplitParty(Case):
    def test_s064_at_the_waypoint_with_the_party_apart_answers_at_once(self):
        fake = self.use(s064_status())
        fake.wps = [TEMPLE_WP, RUINS_WP]
        t0 = time.monotonic()
        out = travel.fast_travel("Temple of Bhaal")
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertNotIn("ui.fast_travel", fake.ops())  # was: a 66 s TIMEOUT
        self.assertIn("fast_travel: FAILED — not travelled", out)
        self.assertIn("Claude already stands at the Temple of Bhaal waypoint (2.9m)", out)
        for who in ("Karlach 136m, 64m above, unchained", "Wyll 137m", "Shadowheart 138m"):
            self.assertIn(who, out)
        steps = json.loads(out.split("do_actions ", 1)[1].split(" (S064", 1)[0])
        self.assertEqual(steps[:2], [{"tool": "set_party", "action": "select", "who": "Karlach"},
                                     {"tool": "travel_to", "waypoint": "Temple of Bhaal"}])
        self.assertEqual(len(steps), 6)
        self.assertEqual(reporting._batch.get("last"), "failed")

    def test_at_the_waypoint_with_everyone_there_is_nothing_to_travel(self):
        st = s064_status()
        for r in st["party"][1:]:
            r.update(pos=[-56.0, -46.3, 1004.0], chained=True)
        fake = self.use(st)
        fake.wps = [TEMPLE_WP]
        out = travel.fast_travel("temple of bhaal")
        self.assertIn("fast_travel: nothing to travel — ", out)
        self.assertNotIn("ui.fast_travel", fake.ops())

    def test_a_split_trip_goes_and_says_who_stayed(self):
        # S064 11:36:28: Karlach selected, travelled alone; Wyll and Shadowheart stayed 138 m away.
        before = s064_status()
        before["active"] = KARLACH
        after = s065_camp_status()  # any far-apart shape: here Wyll 3800 m+ from the active Claude
        calls = {"n": 0}

        def status():
            calls["n"] += 1
            return before if calls["n"] == 1 else after
        fake = self.use(status)
        fake.wps = [TEMPLE_WP]
        out = travel.fast_travel("Temple of Bhaal")
        self.assertIn("ui.fast_travel", fake.ops())
        self.assertIn("fast_travel: done", out)
        self.assertIn("LEFT BEHIND by the trip: ", out)
        self.assertIn("Wyll", out.split("LEFT BEHIND", 1)[1])
        self.assertIn('{"tool": "set_party", "action": "select", "who": "Wyll"}', out)

    def test_unchained_near_member_is_chained_before_the_trip(self):
        st = s064_status()
        st["party"][1].update(pos=[-57.0, -46.3, 1007.0])  # Karlach 2 m away but unchained
        fake = self.use(st)
        fake.wps = [RUINS_WP]
        travel.fast_travel("Undercity Ruins")
        self.assertEqual([a["char"] for o, a in fake.jobs if o == "act.chain"], [KARLACH])
        self.assertIn("ui.fast_travel", fake.ops())

    def test_together_trip_is_untouched(self):
        st = s064_status()
        for r in st["party"][1:]:
            r.update(pos=[-57.0, -46.3, 1007.0], chained=True)
        fake = self.use(st)
        fake.wps = [TEMPLE_WP, RUINS_WP]
        out = travel.fast_travel("Undercity Ruins")
        self.assertIn("fast_travel: done", out)
        self.assertNotIn("LEFT BEHIND", out)
        self.assertNotIn("act.chain", fake.ops())


class CampLeftBehind(Case):
    def test_s065_camp_names_who_stayed_behind(self):
        calls = {"n": 0}

        def status():  # the field before pressing, then the camp without Wyll
            calls["n"] += 1
            return s064_status() if calls["n"] == 1 else s065_camp_status()
        fake = self.use(status)
        out = travel.camp()
        self.assertIn("act.go_to_camp", fake.ops())
        self.assertIn("LEFT BEHIND: ", out)
        tail = out.split("LEFT BEHIND: ", 1)[1]
        self.assertIn("Wyll", tail)
        self.assertNotIn("Karlach", tail)
        self.assertIn("set_party select Wyll, then camp()", tail)

    def test_a_member_arriving_a_moment_later_is_not_reported(self):
        calls = {"n": 0}

        def status():
            calls["n"] += 1
            return s064_status() if calls["n"] == 1 else s065_camp_status(wyll_at_camp=calls["n"] > 2)
        self.use(status)
        self.assertNotIn("LEFT BEHIND", travel.camp())

    def test_left_behind_active_member_doesnt_flip_the_camp(self):
        # the active member is the one left out: the camp is the biggest group, so Wyll alone is named
        st = s065_camp_status()
        st["active"] = WYLL
        rows = party.camp_behind(st)
        self.assertEqual([r["name"] for r in rows], ["Wyll"])


class LongRestNotAtCamp(Case):
    def test_s065_long_rest_refused_before_pressing(self):
        fake = self.use(s065_camp_status())
        fake.can = {"long_rest_state": [], "global_disabled": 0,
                    "blocked": [{"name": "Wyll", "uuid": WYLL, "flags": ["Location"]}]}  # S065 1791709309.266
        t0 = time.monotonic()
        out = inventory.long_rest()
        self.assertLess(time.monotonic() - t0, 1.0)  # was: 4 s, then 'rest screen didn't open'
        self.assertNotIn("ui.long_rest", fake.ops())
        self.assertIn("long_rest: FAILED — not pressed: not everyone is at camp: ", out)
        self.assertIn("Wyll", out)
        self.assertIn("set_party select Wyll, then camp()", out)

    def test_location_flag_on_a_member_at_camp_doesnt_refuse(self):
        fake = self.use(s065_camp_status(wyll_at_camp=True))
        fake.can = {"blocked": [{"name": "Wyll", "uuid": WYLL, "flags": ["Location"]}]}
        self.assertEqual(inventory._rest_not_at_camp(fake.can), "")


class StoryAreaBlock(Case):
    def s065(self) -> Fake:
        fake = self.use(s064_status())
        for r in fake.status["party"][1:]:
            r.update(pos=[22.0, -49.0, 1000.0], chained=True)
        fake.status["party"][0]["pos"] = [21.0, -49.0, 1001.0]  # inside the Bhaal Temple
        fake.block = {"hud": True, "error": "Location"}
        fake.wps = [TEMPLE_WP, RUINS_WP]
        return fake

    def test_s065_camp_then_fast_travel_then_travel_to(self):
        fake = self.s065()
        out = travel.camp()
        self.assertIn("(Location): this area does not let the party go to camp", out)
        self.assertIn("fast travel is blocked too", out)
        self.assertNotIn("fast_travel to a waypoint", out)  # the S065 text sent the agent to fast_travel
        self.assertIn("Way out: walk out the way you came, e.g. toward the Temple of Bhaal waypoint", out)
        self.assertNotIn("Same story-area block", out)
        out = travel.fast_travel("Temple of Bhaal")
        self.assertIn("can't fast travel now (Location)", out)
        self.assertIn("Same story-area block as camp at ", out)
        self.assertIn("camp, fast_travel and travel_to(waypoint) are all refused here", out)
        out = travel_macro.travel_to(waypoint="Temple of Bhaal")
        self.assertIn("Same story-area block as camp", out)
        self.assertNotIn("act.go_to_camp", fake.ops())
        self.assertNotIn("ui.fast_travel", fake.ops())

    def test_s074_travel_to_far_coordinates_walks_with_the_area_text(self):
        # S074 23:00:54: travel_to(x=-67,y=40,z=-45) from inside Szarr Palace, 1.9 km: the fast-travel shortcut's Lua
        # refusal ended the whole call as a bare `FAILED: can't fast travel now (Location)` (14 s to the next try)
        fake = self.s065()
        fake.ft_error = "can't fast travel now (Location)"
        wp = dict(RUINS_WP, to_goal_m=60.0)
        ft = lambda: batch._fast_travel_first(CLAUDE, [-67.0, 40.0, -45.0], 1900.0)
        with mock.patch.object(travel_macro, "_pos_now", return_value=[21.0, -49.0, 1001.0]), \
                mock.patch.object(longnav, "pick_waypoint", return_value=wp), \
                mock.patch.object(travel, "_leave_camp_first", return_value=""):
            line, left = ft()
            self.assertIsNone(left)                                               # the walk goes on from here
            self.assertTrue(line.startswith("fast travel to Undercity Ruins: can't fast travel now (Location): this "
                                            "area does not allow travel"), line)
            self.assertIn("Way out: walk out the way you came, e.g. toward the Temple of Bhaal waypoint", line)
            self.assertTrue(line.endswith("Walking instead."), line)
            self.assertIsNotNone(travel.AREA.current(LVL))                      # the ALERTS line has it now
            n = fake.ops().count("ui.fast_travel")
            line, _ = ft()                                                        # the next long trip: no try
            self.assertTrue(line.startswith("fast travel to Undercity Ruins skipped: the game blocks travel here"), line)
            self.assertIn("Same story-area block as travel_to at ", line)
            self.assertEqual(fake.ops().count("ui.fast_travel"), n)
            fake.ft_error = "can't fast travel in combat"                         # any other refusal: walk, as before
            travel.AREA.block = None
            line, _ = ft()
            self.assertEqual(line, "fast travel to Undercity Ruins failed (can't fast travel in combat): walking instead")

    def test_way_out_is_where_travel_last_worked(self):
        fake = self.s065()
        fake.block = {"hud": True}
        fake.status["party"][0]["pos"] = [-45.0, -46.3, 1005.8]  # S065 12:01:47: camp worked here
        travel.AREA.cleared(LVL, [-45.0, -46.3, 1005.8])
        fake.status["party"][0]["pos"] = [21.0, -49.0, 1001.0]
        fake.block = {"hud": True, "error": "Location"}
        out = travel.camp()
        self.assertIn("Way out: walk to (-45,-46,1006) 66m W (travel_to x,y,z), where travel last worked", out)

    def test_alert_until_the_party_walks_out(self):
        fake = self.s065()
        travel.camp()
        st = fake.status
        alert = overview._area_alert(st)
        self.assertTrue(alert.startswith("travel blocked here (story area, Location): no camp, fast_travel and "
                                         "travel_to(waypoint); way out: "), alert)
        st["party"][0]["pos"] = [16.0, -49.0, 1001.0]  # 5 m on: no re-read, the same phrase (no reprint)
        n = fake.calls.count("ui.travel_block")
        self.assertEqual(overview._area_alert(st), alert)
        self.assertEqual(fake.calls.count("ui.travel_block"), n)
        st["party"][0]["pos"] = [0.0, -49.0, 1001.0]  # 21 m on, still blocked: kept, confirmed here
        self.assertEqual(overview._area_alert(st), alert)
        self.assertEqual(fake.calls.count("ui.travel_block"), n + 1)
        fake.block = {"hud": True}
        st["party"][0]["pos"] = [-45.0, -46.3, 1005.8]  # out of it: the HUD says no block → forgotten
        self.assertEqual(overview._area_alert(st), "")
        self.assertIsNone(travel.AREA.current(LVL))

    def test_alerts_line_shows_it(self):
        fake = self.s065()
        travel.camp()
        self.assertIn("travel blocked here (story area, Location)", overview._alerts_line(fake.status, None, force=True))

    def test_load_game_forgets_the_block(self):
        src = (Path(saving.__file__)).read_text()
        body = src[src.index("def load_game("):]
        self.assertIn("travel.AREA.block = None", body[:body.index("\ndef ", 10)])

    def test_another_level_drops_the_block(self):
        a = areablock.AreaBlock()
        a.blocked(LVL, [0, 0, 0], "Location", "camp")
        self.assertEqual(a.alert("CTY_Main_A", [0, 0, 0]), "")
        self.assertIsNone(a.current(LVL))


class Stay(Case):
    """live9 S002 17:27-17:33: stay(Sazza) said "stays put" while the game kept the attached follower in Vessa's
    group (no PARTY APART row for her); she followed Vessa into the statue fire twice."""

    def stay(self, unchain: JobResult) -> tuple[Fake, str]:
        fake = self.use(s065_camp_status(wyll_at_camp=True))
        run_job = fake.run_job

        def jobs(op, args, context="server", wait_s=25.0, **kw):
            if op == "act.unchain":
                fake.jobs.append((op, args))
                return unchain
            return run_job(op, args, context, wait_s, **kw)
        fake.run_job = jobs
        return fake, overview.stay("Wyll")

    def test_unchain_not_taken_is_said_and_not_marked_staying(self):
        fake, out = self.stay(JobResult("failed", None, "timeout"))
        self.assertEqual(fake.ops(), ["act.unchain"])
        self.assertIn("stay: FAILED — e3 Wyll is still chained to the group (timeout): it follows the next walk", out)
        self.assertIn("set_party unchain who=<them>", out)
        self.assertNotIn(WYLL, overview._stay)

    def test_unchain_taken_stays(self):
        _, out = self.stay(JobResult("done", {"unchained": True}))
        self.assertIn("stays put", out)
        self.assertIn(WYLL, overview._stay)


class PartyPure(unittest.TestCase):
    def test_travel_behind_skips_staying_and_chained(self):
        st = s064_status()
        st["party"][3]["chained"] = True
        rows = party.apart(st, stay={WYLL})
        self.assertEqual([r["name"] for r in party.travel_behind(rows)], ["Karlach"])

    def test_camp_behind_ignores_dead_and_summons(self):
        st = s065_camp_status()
        st["party"][2]["dead"] = True
        st["party"].append(row("bbbbbbbb-0000-4000-8000-000000000009", "Spiritual Weapon", [0, 0, 0], summon=True))
        self.assertEqual(party.camp_behind(st), [])


if __name__ == "__main__":
    unittest.main()
