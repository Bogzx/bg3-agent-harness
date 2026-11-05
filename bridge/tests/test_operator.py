"""Offline tests: the app's operator messages (bg3ai.operator) in tool results: pending/delivered/redeliver from the
files, the block once and never again in the result the client gets (never a nested call's, nor a batch step's: the
batch's own end), nothing opened without $BG3AI_OPERATOR_DIR.

Run: cd bridge && uv run python -m unittest tests.test_operator
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_turn_macros import Base  # sets BG3AI_RUNTIME before importing the server

# isort: split
from bg3ai import operator as op
from bg3ai import settings
from bg3ai.tools import batch, core, reporting, session

T = 1_762_340_531.0  # a sent time


def write(path: Path, *rows: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class Files(unittest.TestCase):
    def setUp(self) -> None:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.inbox, self.done = self.dir / op.INBOX, self.dir / op.DELIVERED

    def test_pending_delivered_and_redeliver(self):
        inbox = [{"id": "m1", "t": T, "text": "a"}, {"id": "m2", "t": T + 6, "text": "b"}]
        self.assertEqual([r["id"] for r in op.pending(inbox, [])], ["m1", "m2"])
        done = [{"id": "m1", "t": T + 1, "session": "S001", "tool": "look"}]
        self.assertEqual([r["id"] for r in op.pending(inbox, done)], ["m2"])
        done.append({"id": "m1", "t": T + 9, "redeliver": True})
        self.assertEqual([r["id"] for r in op.pending(inbox, done)], ["m1", "m2"])
        done.append({"id": "m1", "t": T + 12, "session": "S001", "tool": "travel_to"})
        self.assertEqual(op.delivery(done)["m1"], done[-1])

    def test_a_block_once_then_never_again(self):
        write(self.inbox, {"id": "m1", "t": T, "text": "go north"}, {"id": "m2", "t": T + 6, "text": "then rest"})
        box = op.Outbox(self.dir)
        block, ids = box.take("S004", "travel_to", now=T + 20)
        self.assertEqual(block, f"OPERATOR MESSAGE (from the person running this playthrough, sent {op.clock(T)}): "
                                f"go north\n{op.line(T + 6, 'then rest')}")  # fmt: skip
        self.assertEqual(ids, ["m1", "m2"])
        rows = op.read_rows(self.done)
        self.assertEqual(rows, [{"id": m, "t": T + 20, "session": "S004", "tool": "travel_to"} for m in ("m1", "m2")])
        self.assertEqual(box.take("S004", "look"), ("", []))
        self.assertEqual(op.Outbox(self.dir).take("S005", "look"), ("", []))  # a new server (a resume) too

    def test_a_redelivered_message_goes_out_again(self):
        write(self.inbox, {"id": "m1", "t": T, "text": "go north"})
        box = op.Outbox(self.dir)
        box.take("S004", "look")
        write(self.done, {"id": "m1", "t": T + 30, "redeliver": True})
        block, ids = op.Outbox(self.dir).take("S004", "get_status")
        self.assertEqual((block, ids), (op.line(T, "go north"), ["m1"]))

    def test_unchanged_files_are_not_read_again(self):
        write(self.inbox, {"id": "m1", "t": T, "text": "x"})
        box = op.Outbox(self.dir)
        self.assertEqual(box.take("S001", "look")[1], ["m1"])
        self.assertEqual(box.take("S001", "look"), ("", []))  # its own delivered rows changed the file: one read
        with mock.patch.object(op, "read_rows", side_effect=AssertionError("read")):
            self.assertEqual(box.take("S001", "look"), ("", []))  # unchanged since: one stat, no read
        write(self.inbox, {"id": "m2", "t": T + 6, "text": "y"})
        self.assertEqual(box.take("S001", "look")[1], ["m2"])

    def test_no_dir_opens_nothing(self):
        box = op.Outbox(None)
        self.assertEqual(box.take("S001", "look"), ("", []))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_no_inbox_writes_nothing(self):
        self.assertEqual(op.Outbox(self.dir).take("S001", "look"), ("", []))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_settings(self):
        self.assertIsNone(settings.from_env({}, Path("/r"), Path("/h")).operator_dir)
        self.assertIsNone(settings.from_env({"BG3AI_OPERATOR_DIR": ""}, Path("/r"), Path("/h")).operator_dir)
        s = settings.from_env({"BG3AI_OPERATOR_DIR": "/rt/sessions/pt"}, Path("/r"), Path("/h"))
        self.assertEqual(s.operator_dir, Path("/rt/sessions/pt"))


class InResults(Base):
    def setUp(self) -> None:
        super().setUp()
        self.dir = self.tmp / "op"
        self.dir.mkdir()
        p = mock.patch.object(reporting, "OPERATOR", op.Outbox(self.dir))
        p.start()
        self.addCleanup(p.stop)
        self.recs: list[dict] = []
        m = mock.patch.object(core.METRICS, "write", self.recs.append)
        m.start()
        self.addCleanup(m.stop)
        e = mock.patch.dict("os.environ", {"BG3AI_SESSION": "S004"})
        e.start()
        self.addCleanup(e.stop)
        write(self.dir / op.INBOX, {"id": "m1", "t": T, "text": "help Shadowheart first"})

    def served(self, name: str, fn):
        """`fn` as the client calls it (the MCP layer, session._compacted)."""
        return session._compacted(name, fn)

    def finish(self, tool: str = "look") -> str:
        return self.served(tool, lambda: reporting._finish(tool, {}, "done", time.monotonic(), events=False))()

    def delivered(self) -> list[dict]:
        return op.read_rows(self.dir / op.DELIVERED)

    def told(self) -> list[dict]:
        return [r for r in self.recs if r.get("type") == "operator"]

    def test_the_next_result_carries_it_once(self):
        out = self.finish("travel_to")
        self.assertTrue(out.endswith("\n" + op.line(T, "help Shadowheart first")), out)
        self.assertEqual([(r["tool"], r["ids"]) for r in self.told()], [("travel_to", ["m1"])])
        row = self.delivered()[0]
        self.assertEqual((row["id"], row["session"], row["tool"]), ("m1", "S004", "travel_to"))
        again = self.finish()
        self.assertNotIn("OPERATOR MESSAGE", again)
        self.assertEqual(len(self.told()), 1)

    def test_a_nested_call_never_takes_it(self):
        # REVIEW-CHAT-INT #1: swap_party's _walk_up_to drops its wait_for's result; when that wait took the message,
        # the model never saw it and it was marked delivered
        seen: list[list[dict]] = []

        @reporting._guard
        def swap_party_stub() -> str:
            t0 = time.monotonic()
            batch.wait_for(target="idle", timeout_s=1, lead_s=0)  # the inner result, dropped
            seen.append(self.delivered())
            return reporting._finish("swap_party", {}, "swapped", t0, events=False)

        out = self.served("swap_party", swap_party_stub)()
        self.assertEqual(seen, [[]])  # still pending after the inner call
        self.assertIn(op.line(T, "help Shadowheart first"), out)
        self.assertEqual([(r["tool"], r["session"]) for r in self.delivered()], [("swap_party", "S004")])

    def test_a_return_without_finish_carries_it(self):
        @reporting._guard
        def flee_stub() -> str:
            return "FAILED: no way out"

        self.assertIn(op.line(T, "help Shadowheart first"), self.served("flee", flee_stub)())

    def test_not_in_a_batch_step_but_in_the_batch_end(self):
        steps: list[tuple[str, list[dict]]] = []
        real = reporting._finish

        def finish(tool: str, *a, **kw) -> str:
            r = real(tool, *a, **kw)
            if reporting._batch["active"]:
                steps.append((tool, self.delivered()))
            return r

        with mock.patch.object(reporting, "_finish", finish):
            end = self.served("do_actions", batch.do_actions)(json.dumps([{"tool": "interact", "target": self.chest}]))
        self.assertEqual(steps, [("interact", [])])
        self.assertIn(op.line(T, "help Shadowheart first"), end)
        self.assertEqual(self.delivered()[0]["tool"], "do_actions")


if __name__ == "__main__":
    unittest.main()
