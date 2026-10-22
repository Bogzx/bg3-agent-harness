# pyright: basic
"""bg3bench.resets: the FAIR-2 knowledge lock (moved from runner/test_runs.py; the series wiring stays there)."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from bg3bench import resets as R


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3bench-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class KnowledgeLock(TmpDir):
    """FAIR-2: play/knowledge's files are read-only for a --runs series, and their modes come back after."""

    def tree(self) -> Path:
        k = self.tmp / "knowledge"
        (k / "playbooks").mkdir(parents=True)
        (k / "a.md").write_text("a")
        (k / "playbooks" / "b.md").write_text("b")
        (k / "x.sh").write_text("#!/bin/sh")
        (k / "x.sh").chmod(0o755)
        (k / "a.md").chmod(0o640)
        return k

    def modes(self, k: Path) -> dict:
        return {str(p.relative_to(k)): p.stat().st_mode & 0o777 for p in k.rglob("*") if p.is_file()}

    def test_lock_unlock_restores_modes(self):
        k, rec = self.tree(), self.tmp / "rt" / "lock.json"
        before = self.modes(k)
        self.assertEqual(R.lock_tree(k, rec), 3)
        self.assertEqual(self.modes(k), {"a.md": 0o440, "playbooks/b.md": 0o444, "x.sh": 0o555})
        with self.assertRaises(PermissionError) if os.geteuid() else contextlib.nullcontext():
            (k / "a.md").write_text("agent edit")
        # an execv'd successor (or a restart after a kill) locks again: the record keeps the first modes
        self.assertEqual(R.lock_tree(k, rec), 3)
        self.assertEqual(R.unlock_tree(k, rec), 3)
        self.assertEqual(self.modes(k), before)
        self.assertFalse(rec.exists())
        self.assertEqual(R.unlock_tree(k, rec), 0)  # nothing locked: no-op
        self.assertEqual(R.lock_tree(self.tmp / "none", rec), 0)


HOST = "14e68d14-c5a2-1e68-d51f-c2fe2defd545"
OTHER = "ad9af97d-75da-406a-ae13-7071c563f604"


class NavIsolation(TmpDir):
    """NAV-3 per host: the per-user Script Extender folder is shared with every playthrough on the machine."""

    def se(self) -> Path:
        se = self.tmp / "se" / "BG3AI"
        se.mkdir(parents=True)
        for name in (f"nav_{HOST}.json", f"nav_guard_{HOST}.json", f"nav_{OTHER}.json", f"nav_guard_{OTHER}.json",
                     "nav_guard.json", "other.json"):  # fmt: skip
            (se / name).write_text(f"orig {name}")
        return se

    def names(self, d: Path) -> list[str]:
        return sorted(p.name for p in d.iterdir() if p.is_file())

    def test_a_new_game_moves_nothing(self):
        se = self.se()
        before = self.names(se)
        self.assertEqual(R.isolate_nav(se, self.tmp / "reset", self.tmp / "m.json", "new_game", None), [])
        self.assertEqual(self.names(se), before)
        self.assertFalse((self.tmp / "m.json").exists())

    def test_a_save_start_moves_only_its_hosts_two_files_and_restores_them(self):
        se, dest, marker = self.se(), self.tmp / "reset", self.tmp / "m.json"
        moved = R.isolate_nav(se, dest, marker, "save", HOST)
        self.assertEqual(moved, [f"nav_{HOST}.json", f"nav_guard_{HOST}.json"])
        self.assertEqual(
            self.names(se), sorted([f"nav_{OTHER}.json", f"nav_guard_{OTHER}.json", "nav_guard.json", "other.json"])
        )  # other playthroughs' files and the host-less legacy kill list stay
        self.assertEqual(self.names(dest), sorted(moved))
        (se / f"nav_{HOST}.json").write_text("the run's breadcrumbs")  # the run writes its own
        self.assertEqual(R.restore_nav(marker), moved)
        self.assertEqual((se / f"nav_{HOST}.json").read_text(), f"orig nav_{HOST}.json")
        self.assertEqual((se / f"nav_guard_{HOST}.json").read_text(), f"orig nav_guard_{HOST}.json")
        self.assertEqual((dest / "run" / f"nav_{HOST}.json").read_text(), "the run's breadcrumbs")  # kept to look at
        self.assertFalse(marker.exists())
        self.assertEqual(R.restore_nav(marker), [])  # idempotent

    def test_a_host_with_no_files_moves_nothing(self):
        se = self.se()
        new = "00000000-0000-0000-0000-000000000001"
        self.assertEqual(R.isolate_nav(se, self.tmp / "reset", self.tmp / "m.json", "save", new), [])
        self.assertFalse((self.tmp / "m.json").exists())

    def test_a_save_start_without_a_valid_host_is_refused(self):
        se = self.se()
        for host in (None, "Claude", "../nav"):
            with self.assertRaises(R.NavIsolationError):
                R.isolate_nav(se, self.tmp / "reset", self.tmp / "m.json", "save", host)
        self.assertEqual(len(self.names(se)), 6)

    def test_the_context_restores_on_a_crash(self):
        se, dest, marker = self.se(), self.tmp / "reset", self.tmp / "m.json"
        with self.assertRaises(RuntimeError), R.nav_isolated(se, dest, marker, "save", HOST) as moved:
            self.assertEqual(len(moved), 2)
            self.assertFalse((se / f"nav_{HOST}.json").exists())
            raise RuntimeError("the run died")
        self.assertEqual((se / f"nav_{HOST}.json").read_text(), f"orig nav_{HOST}.json")
        self.assertFalse(marker.exists())

    def test_a_killed_runner_is_recovered_from_the_marker(self):
        se, dest, marker = self.se(), self.tmp / "reset", self.tmp / "m.json"
        R.isolate_nav(se, dest, marker, "save", HOST)  # then SIGKILL: no finally ran
        self.assertTrue(marker.exists())
        self.assertEqual(R.recover_nav(marker), [f"nav_{HOST}.json", f"nav_guard_{HOST}.json"])
        self.assertEqual(len(self.names(se)), 6)
        self.assertEqual(R.recover_nav(marker), [])
        # the next isolate recovers a leftover marker before moving anything
        R.isolate_nav(se, dest, marker, "save", HOST)
        R.isolate_nav(se, self.tmp / "reset2", marker, "save", OTHER)
        self.assertEqual((se / f"nav_{HOST}.json").read_text(), f"orig nav_{HOST}.json")
        self.assertEqual(self.names(self.tmp / "reset2"), sorted([f"nav_{OTHER}.json", f"nav_guard_{OTHER}.json"]))

    def test_a_crash_between_moves_is_recovered(self):
        se, dest, marker = self.se(), self.tmp / "reset", self.tmp / "m.json"
        R.isolate_nav(se, dest, marker, "save", HOST)
        shutil.move(str(dest / f"nav_guard_{HOST}.json"), str(se / f"nav_guard_{HOST}.json"))  # never moved
        self.assertEqual(R.recover_nav(marker), [f"nav_{HOST}.json"])
        self.assertEqual(len(self.names(se)), 6)


class StartHost(unittest.TestCase):
    """The start save's host uuid comes from the mod's inspector (a subprocess; its report's host_uuid)."""

    def runner(self, rc=0, out="", err=""):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, rc, out, err)

        return run, calls

    def test_the_inspectors_host_uuid(self):
        run, calls = self.runner(out=json.dumps({"host_uuid": HOST, "findings": []}))
        self.assertEqual(R.start_host(Path("/store/x/S.lsv"), run=run), HOST)
        self.assertEqual(calls[0][1:], [str(R.INSPECT), "/store/x/S.lsv", "--shallow"])
        self.assertTrue(R.INSPECT.is_file())

    def test_no_host_or_a_failed_inspector_is_refused(self):
        for run, _ in (self.runner(out=json.dumps({"findings": []})), self.runner(out="not json"),
                       self.runner(out=json.dumps({"host_uuid": "Claude"})), self.runner(rc=2, err="refused")):  # fmt: skip
            with self.assertRaises(R.NavIsolationError):
                R.start_host(Path("S.lsv"), run=run)


class AgentEdits(TmpDir):
    """FAIR-2: after a run, the knowledge files the playing agent wrote are put back; other changes are reported."""

    def stream(self, *blocks, extra: str = "") -> Path:
        s = self.tmp / f"stream{len(list(self.tmp.glob('stream*')))}.jsonl"
        lines = [json.dumps({"type": "assistant", "message": {"content": list(blocks)}}), extra]
        s.write_text("\n".join(lines))
        return s

    @staticmethod
    def tool(name: str, **inp) -> dict:
        return {"type": "tool_use", "name": name, "input": inp}

    def test_the_agents_writes_under_the_tree(self):
        k, cwd = self.tmp / "play" / "knowledge", self.tmp / "play"
        k.mkdir(parents=True)
        s1 = self.stream(
            self.tool("Edit", file_path="knowledge/a.md"),  # relative: from play/
            self.tool("Write", file_path=str(k / "new.md")),
            self.tool("NotebookEdit", notebook_path=str(k / "n.ipynb")),
            self.tool("Read", file_path=str(k / "read.md")),  # not a write
            self.tool("Write", file_path=str(self.tmp / "outside.md")),  # not under the tree
            self.tool("Write", file_path=""),
            {"type": "text", "text": "Edit knowledge/b.md"},
            "not a block",
            extra='{"type": "user", "message": {"content": [{"type": "tool_use", "name": "Write"}]}}\nnot json\n[1]',
        )
        missing = self.tmp / "no-such-stream.jsonl"
        self.assertEqual(R.agent_written([s1, missing], k, cwd), {"a.md", "new.md", "n.ipynb"})

    def test_restore_puts_back_only_the_agents_changes(self):
        k, keep = self.tmp / "knowledge", self.tmp / "keep"
        (k / "sub").mkdir(parents=True)
        for name in ("a.md", "b.md", "gone.md", "same.md", "sub/c.md"):
            (k / name).write_text(f"orig {name}")
        snap = R.snapshot_tree(k)
        (k / "a.md").write_text("agent edit")
        (k / "b.md").write_text("operator edit")
        (k / "gone.md").unlink()
        (k / "sub" / "c.md").unlink()
        (k / "sub").rmdir()
        (k / "added.md").write_text("agent note")
        self.assertEqual(
            R.tree_changes(k, snap),
            {"a.md": "changed", "added.md": "added", "b.md": "changed", "gone.md": "removed", "sub/c.md": "removed"},
        )
        out = R.restore_agent_edits(k, snap, {"a.md", "added.md", "gone.md", "sub/c.md"}, keep)
        self.assertEqual(
            out,
            {
                "agent_edits": ["a.md (changed)", "added.md (added)", "gone.md (removed)", "sub/c.md (removed)"],
                "other_changes": ["b.md (changed)"],
            },
        )
        self.assertEqual(R.snapshot_tree(k), {**snap, "b.md": "operator edit"})
        self.assertEqual(R.snapshot_tree(keep), {"a.md": "agent edit", "added.md": "agent note"})  # the agent's kept

    def test_a_failed_restore_is_reported(self):
        k, keep = self.tmp / "knowledge", self.tmp / "keep"
        k.mkdir()
        (k / "a.md").write_text("orig")
        snap = R.snapshot_tree(k)
        (k / "a.md").write_text("agent edit")
        keep.write_text("a file where the keep folder goes")
        out = R.restore_agent_edits(k, snap, {"a.md"}, keep)
        self.assertEqual(out["agent_edits"], [])
        self.assertRegex(out["other_changes"][0], r"^a\.md \(changed; restore failed: ")
        self.assertEqual((k / "a.md").read_text(), "agent edit")

    def test_snapshot_of_no_tree_or_an_unreadable_file(self):
        self.assertEqual(R.snapshot_tree(self.tmp / "none"), {})
        if not os.geteuid():
            self.skipTest("root reads a mode-000 file")
        (self.tmp / "ok.md").write_text("ok")
        (self.tmp / "secret.md").write_text("x")
        (self.tmp / "secret.md").chmod(0)
        self.addCleanup((self.tmp / "secret.md").chmod, 0o644)
        self.assertEqual(R.snapshot_tree(self.tmp), {"ok.md": "ok"})

    def test_unlock_skips_a_gone_file_and_a_bad_mode(self):
        k, rec = self.tmp / "knowledge", self.tmp / "lock.json"
        k.mkdir()
        (k / "a.md").write_text("a")
        (k / "a.md").chmod(0o444)
        rec.write_text(json.dumps({"a.md": 0o644, "gone.md": 0o644, "bad.md": "rw"}))
        (k / "bad.md").write_text("b")
        self.assertEqual(R.unlock_tree(k, rec), 1)
        self.assertEqual((k / "a.md").stat().st_mode & 0o777, 0o644)
        self.assertFalse(rec.exists())


class ArchiveNav(TmpDir):
    """The old NAV-3 rule: every nav_*.json moves into dest, nothing else."""

    def test_every_nav_file_moves(self):
        se, dest = self.tmp / "BG3AI", self.tmp / "archive"
        (se / "nav_dir.json").mkdir(parents=True)  # a folder, not a nav file
        for name in ("nav_guard.json", "nav_x.json", "other.json"):
            (se / name).write_text(name)
        self.assertEqual(R.archive_nav_state(se, dest), ["nav_guard.json", "nav_x.json"])
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["nav_guard.json", "nav_x.json"])
        self.assertEqual(sorted(p.name for p in se.iterdir()), ["nav_dir.json", "other.json"])
        self.assertEqual(R.archive_nav_state(self.tmp / "none", dest), [])

    def test_a_dest_that_cant_be_made_moves_nothing(self):
        se, dest = self.tmp / "BG3AI", self.tmp / "archive"
        se.mkdir()
        (se / "nav_x.json").write_text("x")
        dest.write_text("a file where the archive goes")
        self.assertEqual(R.archive_nav_state(se, dest), [])
        self.assertTrue((se / "nav_x.json").exists())
