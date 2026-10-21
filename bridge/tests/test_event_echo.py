"""EVENTS rendering: one event per line (queue_interrupted last=), and the same event twice is shown once (EventEcho)."""
import unittest
from unittest import mock

from bg3ai import actq, render
from bg3ai.render import Handles
from bg3ai.tools import core, queueing, reporting, turn_plan

DOOR = "aaaaaaaa-0000-0000-0000-000000000021"
TAV = "bbbbbbbb-0000-0000-0000-000000000002"

# naut-r2 golden 129: the travel_to step's whole result was the queue_interrupted event's last= field
STEP = ("1. travel_to (-47.4,13.2,-426): 68m\nleg 1: move_to: FAILED — character is in a conversation "
        "(dialog nil res=nil)\nSTOPPED: leg 1 failed\nCHANGES\n  e2 Corran Vell moved 3m W\nEVENTS\n  used x")


def used(t_ms, item=DOOR):
    return {"kind": "used", "t": t_ms, "data": {"char": {"uuid": TAV, "name": "Tav"},
                                                 "item": {"uuid": item, "name": "Sphincter"}, "success": True}}


class OneLine(unittest.TestCase):
    def test_step_result_gist(self):
        self.assertEqual(render.one_line(STEP), "1. travel_to (-47.4,13.2,-426): 68m | leg 1: move_to: FAILED — "
                                               "character is in a conversation (dialog nil res=nil) | "
                                               "STOPPED: leg 1 failed")

    def test_short_and_capped(self):
        self.assertEqual(render.one_line("1. interact e5: done — opened"), "1. interact e5: done — opened")
        self.assertEqual(len(render.one_line("x" * 500, cap=100)), 100)

    def test_render_keeps_an_event_on_one_line(self):
        H = Handles()
        out = render.render_events([{"kind": "queue_interrupted", "data": {"who": "e2", "done": 0, "last": STEP}}], H)
        lines = out.split("\n")
        self.assertEqual(lines[0], "EVENTS")
        self.assertEqual(len(lines), 2, out)
        self.assertIn("done=0", lines[1])
        self.assertIn("STOPPED: leg 1 failed", lines[1])


class Echo(unittest.TestCase):
    def test_same_event_within_window_once(self):
        e = render.EventEcho()
        self.assertEqual(len(e.filter([used(1000)])), 1)
        self.assertEqual(e.filter([used(2900)]), [])            # the late UseFinished in the next result
        self.assertEqual(len(e.filter([used(2900 + 6000)])), 1)  # a real use later
        self.assertEqual(len(e.filter([used(9000, item="cccccccc-0000-0000-0000-000000000022")])), 1)

    def test_within_one_batch_and_other_kinds_untouched(self):
        e = render.EventEcho()
        died = {"kind": "died", "t": 1000, "data": {"uuid": TAV}}
        self.assertEqual(len(e.filter([used(1000), used(1400), died, dict(died)])), 3)

    def test_item_added_count_change_is_news(self):
        e = render.EventEcho()
        a = {"kind": "item_added", "t": 1000, "data": {"item": {"uuid": DOOR}, "count": 2}}
        b = {"kind": "item_added", "t": 1200, "data": {"item": {"uuid": DOOR}, "count": 3}}
        self.assertEqual(len(e.filter([a, dict(a), b])), 2)

    def test_events_text_drops_the_echo_across_calls(self):
        drained = {"server": [[used(1000)], [used(2500)]], "client": [[], []]}
        class G:  # no parallel(): _par runs the drains one after another
            def drain_events(self, ctx):
                return drained[ctx].pop(0)
        g = G()
        with mock.patch.object(core, "game", g), mock.patch.object(render, "ECHO", render.EventEcho()), \
                mock.patch.object(turn_plan, "_party_cached", lambda: set()), \
                mock.patch.object(reporting, "_fight_feed", lambda evs: ""):
            first = core._events_text("interact: done — closed")
            second = core._events_text("ROUTE ...")
        self.assertIn("used", first)
        self.assertNotIn("used", second)


class QueueReport(unittest.TestCase):
    def test_interrupted_event_last_is_one_line(self):
        sent = []
        conn = mock.Mock()
        conn.rpc = lambda op, args, **kw: sent.append((op, args))
        rep = actq.Report(done=0, stopped="step 1 travel_to interrupted", lines=[STEP])
        with mock.patch.object(queueing, "_watch_conn", lambda: conn), \
                mock.patch.object(queueing, "_rechain_after", lambda who: None):
            queueing._on_queue_report(TAV, rep)
        (op, args), = sent
        self.assertEqual((op, args["kind"]), ("act.note", "queue_interrupted"))
        self.assertNotIn("\n", args["data"]["last"])
        self.assertIn("FAILED — character is in a conversation", args["data"]["last"])


if __name__ == "__main__":
    unittest.main()
