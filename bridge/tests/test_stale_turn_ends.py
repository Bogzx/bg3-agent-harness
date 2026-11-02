"""EVENTS: a turn group's close starts the new round's turns before the game ends the old round's (phases4 load1,
rounds 2-4: round_started N, turn_started x8, then the 8 turn_ended of round N-1 1-3 ms later). With the mod's
`round` on turn events the old round's ends are dropped (render_events.StaleTurnEnds), with compaction on or off and
when the burst is split across two reads; events without a round are shown as before.

Run: cd bridge && uv run python -m unittest tests/test_stale_turn_ends.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_turn_macros import Base  # noqa: F401  (sets BG3AI_RUNTIME before importing the server)

# isort: split
from bg3ai import compact, render
from bg3ai.render import Handles
from bg3ai.render_events import StaleTurnEnds
from bg3ai.tools import core, turn_plan

# phases4 load1 round 1 -> 2: the shared group's 8 members (Nautiloid Tank and the chest are no characters)
NAMES = ["Astarion", "Wyll", "Lae'zel", "Minthara", "Shadowheart", "Gale", "Karlach", "Cave Bear"]
PARTY = [(f"00000000-0000-0000-0000-{n:012d}", name) for n, name in enumerate(NAMES, 1)]


def turn(kind: str, u: str, name: str, rnd: int | None) -> dict:
    d = {"uuid": u, "name": name}
    if rnd is not None:
        d["round"] = rnd
    return {"kind": kind, "data": d}


def starts(rnd: int | None) -> list[dict]:
    return [{"kind": "round_started", "data": {"round": 2}}] + [turn("turn_started", u, n, rnd) for u, n in PARTY]


def ends(rnd: int | None) -> list[dict]:
    return [turn("turn_ended", u, n, rnd) for u, n in PARTY]


class G:
    port, pool_cap, job_push = 1, 1, False

    def __init__(self, reads: list[list[dict]]):
        self.reads = reads

    def drain_events(self, ctx, limit=None):
        return self.reads.pop(0) if ctx == "server" and self.reads else []


class StaleEnds(unittest.TestCase):
    def setUp(self):
        saved = (core.H, core.game, render.STALE_ENDS.started.copy())

        def restore():
            core.H, core.game = saved[:2]
            render.STALE_ENDS.started.clear()
            render.STALE_ENDS.started.update(saved[2])

        self.addCleanup(restore)
        core.H = Handles()
        render.STALE_ENDS.started.clear()
        p = mock.patch.object(turn_plan, "_party_cached", lambda: {u for u, _ in PARTY})
        p.start()
        self.addCleanup(p.stop)

    def results(self, reads: list[list[dict]], compacted: bool) -> list[str]:
        """Each read as one wait_for result's text, as the agent gets it."""
        core.game = G([list(r) for r in reads])
        c = compact.Compactor()
        out = []
        with mock.patch.object(compact, "COMPACT", compacted):
            for _ in reads:
                out.append(c.apply("wait_for", "wait_for: turn\n" + core._events_text()))
        return out

    def test_one_read_compaction_off(self):
        (text,) = self.results([starts(2) + ends(1)], compacted=False)
        self.assertNotIn("turn_ended", text)
        names = "; ".join(f"e{i} name={n}" for i, n in enumerate(NAMES, 1))
        self.assertEqual(text.split("\n")[-1], f"  turn_started {names}")  # as today: the round tag is not printed

    def test_one_read_compaction_on(self):
        (text,) = self.results([starts(2) + ends(1)], compacted=True)
        self.assertNotIn("turn_ended", text)
        self.assertIn("round 2", text)
        self.assertIn("turn e1 Astarion; e2 Wyll; e3 Lae'zel", text)

    def test_burst_split_across_two_reads(self):
        for compacted in (False, True):
            render.STALE_ENDS.started.clear()
            first, second = self.results([starts(2), ends(1)], compacted)
            self.assertIn("e8", first, compacted)
            self.assertNotIn("turn_ended", second, compacted)
            self.assertNotIn("EVENTS", second, compacted)

    def test_events_without_a_round_as_before(self):
        for compacted in (False, True):
            render.STALE_ENDS.started.clear()
            _, second = self.results([starts(None), ends(None)], compacted)
            self.assertIn("turn_ended e1", second, compacted)
            self.assertIn("e8", second, compacted)

    def test_an_end_of_the_current_round_is_kept(self):
        f = StaleTurnEnds()
        u, n = PARTY[0]
        evs = [turn("turn_started", u, n, 2), turn("turn_ended", u, n, 2)]
        self.assertEqual(f.filter(evs), evs)

    def test_a_new_combat_starts_again_at_round_one(self):
        f = StaleTurnEnds()
        u, n = PARTY[0]
        f.filter([turn("turn_started", u, n, 5)])
        evs = [turn("turn_started", u, n, 1), turn("turn_ended", u, n, 1)]
        self.assertEqual(f.filter(evs), evs)

    def test_only_that_members_older_end_is_dropped(self):
        f = StaleTurnEnds()
        (a, an), (b, bn) = PARTY[:2]
        kept = turn("turn_ended", b, bn, 1)  # b's round-2 turn has not started: its round-1 end is news
        out = f.filter([turn("turn_started", a, an, 2), turn("turn_ended", a, an, 1), kept])
        self.assertEqual(out, [turn("turn_started", a, an, 2), kept])


if __name__ == "__main__":
    unittest.main()
