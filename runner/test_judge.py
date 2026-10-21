#!/usr/bin/env python3
"""Unit tests for runner/judge.py: the decision extractor over fixture streams (dialogue options and choices, batched
choices, [decision] notes, CHARACTER.md appends, the run window, dedup, the cap), the run/dir/character lookup, the
judge run with a fake client and the CLI (--profile, --dry-run). The judge itself (prompt, parsing, retries, clients,
the cost cap) is tested in packages/harness/tests/test_llm_judge.py. Never launches claude or the game.

    python3 -m unittest runner/test_judge.py
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge as J  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

T0 = 1_000_000.0
CHARACTER = """# Character: Orsen Vale

## Won't do
- Never uses tadpole powers.

## Character development
- S001: created from the brief [seen]
"""
LAEZEL = ("dialogue_advance: 0 filler line(s)\nDIALOGUE Lae'zel: Together we might survive.\n"
          "  1. Who are you?\n  2. What do you suggest?\nSTOPPED: a real choice (2 options)\nEVENTS\n"
          "  dialogue_line speaker=Lae'zel text=Abomination.")


def _say(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _use(i: str, name: str, inp: dict) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": f"mcp__bg3__{name}",
                                                          "input": inp}]}}


def _res(i: str, text: str) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i,
                                                     "content": [{"type": "text",
                                                                  "text": json.dumps({"result": text})}]}]}}


def _write(d: Path, sid: str, events: list[dict], times: dict[str, float] | None = None) -> Path:
    p = d / f"{sid}.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in events))
    if times is not None:
        (d / f"{sid}.turns.jsonl").write_text("".join(json.dumps({"type": "use", "t": t, "id": i, "name": "x"}) + "\n"
                                                      for i, t in times.items()))
    return p


class FakeClient:
    """Answers from a list (a str: the model's text; an exception: raised), records the prompts."""
    model = "judge-model-x"

    def __init__(self, answers: list):
        self.answers, self.prompts = list(answers), []

    def __call__(self, client, prompt):   # the `call` hook of judge_chunk / judge_run
        self.prompts.append(prompt)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a, {"cost_usd": 0.01}


def _verdict(score, per: dict[int, float]) -> str:
    return json.dumps({"score": score, "summary": "fine",
                       "decisions": [{"id": i, "score": s, "why": f"why {i}"} for i, s in per.items()]})


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bg3ai-judge-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


# ======================================================================== extract

class Extract(TmpDir):
    def stream(self):
        return _write(self.tmp, "S001", [
            _use("a", "note", {"text": "[decision] created Orsen: fighter, shield\n[npc] met nobody"}),
            _res("a", "Noted 2 lines"),
            _use("b", "dialogue_advance", {}), _res("b", LAEZEL),
            _say("Asking for her plan, like a sergeant would."),
            _use("c", "choose_dialogue", {"option": 2}),
            _res("c", "choose_dialogue: done\nSTOPPED: conversation ended"),
            _use("d", "memory_append", {"path": "memory/CHARACTER.md", "text": "- S001: spared the imp\n"}),
            _res("d", "Appended"),
            _use("e", "memory_append", {"path": "memory/npcs.md", "text": "- Lae'zel\n"}), _res("e", "Appended"),
            _use("f", "choose_dialogue", {"option": 1}), _res("f", "choose_dialogue: done"),   # nothing on screen
        ], {"a": T0 + 1, "b": T0 + 2, "c": T0 + 3, "d": T0 + 4, "e": T0 + 5, "f": T0 + 6})

    def test_dialogue_note_and_development(self):
        ds = J.stream_decisions(self.stream())
        self.assertEqual([d["kind"] for d in ds], ["note", "dialogue", "development", "dialogue"])
        note, laezel, dev, blind = ds
        self.assertEqual(note["chosen"], "created Orsen: fighter, shield")
        self.assertEqual(laezel["chosen"], "What do you suggest?")
        self.assertEqual(laezel["offered"], ["1. Who are you?", "2. What do you suggest?"])
        self.assertEqual((laezel["speaker"], laezel["line"]), ("Lae'zel", "Together we might survive."))
        self.assertEqual(laezel["reason"], "Asking for her plan, like a sergeant would.")
        self.assertEqual((laezel["session"], laezel["t"]), ("S001", T0 + 3))
        self.assertEqual(dev["chosen"], "S001: spared the imp")
        self.assertIsNone(note["reason"])
        # the conversation ended: the next choice was made with no options seen
        self.assertEqual(blind["chosen"], "option 1")
        self.assertNotIn("offered", blind)

    def test_run_window(self):
        s = self.stream()
        self.assertEqual([d["kind"] for d in J.stream_decisions(s, since=T0 + 2.5, until=T0 + 4)],
                         ["dialogue", "development"])
        self.assertEqual(J.stream_decisions(s, since=T0 + 100), [])

    def test_no_turns_file_uses_mtime(self):
        s = _write(self.tmp, "S002", [_use("b", "dialogue_advance", {}), _res("b", LAEZEL),
                                      _use("c", "choose_dialogue", {"option": 1}), _res("c", "done")])
        self.assertEqual(len(J.stream_decisions(s, since=0)), 1)
        self.assertEqual(J.stream_decisions(s, since=s.stat().st_mtime + 10), [])

    def test_batched_choices_walk_the_result_blocks(self):
        nxt = ("1. done\nDIALOGUE Shadowheart: Now what?\n  1. Rest.\n  2. Move on.\n"
               "DIALOGUE Shadowheart: Lead the way.\n  1. Let's go.\n  2. Wait.\nSTOPPED")
        s = _write(self.tmp, "S003", [
            _use("b", "get_dialogue", {}), _res("b", LAEZEL),
            _use("c", "do_actions", {"actions": json.dumps([{"tool": "choose_dialogue", "option": 1},
                                                            {"tool": "choose_dialogue", "option": 2},
                                                            {"tool": "wait"}]),
                                     "note": "[decision] trust Lae'zel for now"}),
            _res("c", nxt),
            _use("d", "choose_dialogue", {"option": 1}), _res("d", "conversation ended"),
        ], {})
        ds = J.stream_decisions(s)
        self.assertEqual([(d["kind"], d["chosen"]) for d in ds],
                         [("dialogue", "Who are you?"), ("dialogue", "Move on."), ("note", "trust Lae'zel for now"),
                          ("dialogue", "Let's go.")])

    def test_single_option_is_no_decision(self):
        s = _write(self.tmp, "S004", [
            _use("b", "dialogue_advance", {}), _res("b", "DIALOGUE Narrator: *You wake.*\n  1. Continue.\nSTOPPED"),
            _use("c", "choose_dialogue", {"option": 1}), _res("c", "done")], {})
        self.assertEqual(J.stream_decisions(s), [])

    def test_dialogue_blocks(self):
        bl = J.dialogue_blocks("x\nDIALOGUE A: hi\n  1. one\n  2. two\nEVENTS\n  1. not an option\nDIALOGUE B: bye")
        self.assertEqual(bl, [{"speaker": "A", "line": "hi", "options": {1: "one", 2: "two"}},
                              {"speaker": "B", "line": "bye", "options": {}}])

    def test_dedupe_and_cap(self):
        d = {"kind": "dialogue", "speaker": "A", "line": "l", "offered": ["1. x", "2. y"], "chosen": "x",
             "reason": None}
        out = J.dedupe([d, dict(d, reason="again"), dict(d, chosen="y")])
        self.assertEqual(len(out), 2)
        self.assertEqual((out[0]["n"], out[0]["reason"]), (2, "again"))
        items = [{"i": i} for i in range(10)]
        kept, dropped = J.cap(items, 4)
        self.assertEqual(([k["i"] for k in kept], dropped), ([0, 3, 6, 9], 6))
        self.assertEqual(J.cap(items, 20), (items, 0))
        self.assertEqual(J.cap(items, 1), ([items[0]], 9))

    def test_extract_numbers_skips_other_streams(self):
        self.stream()
        _write(self.tmp, "S001-keeper", [_use("x", "note", {"text": "[decision] keeper"}), _res("x", "ok")], {})
        ds, dropped = J.extract(self.tmp, limit=3)
        self.assertEqual(([d["id"] for d in ds], dropped), ([1, 2, 3], 1))
        self.assertNotIn("keeper", json.dumps(ds))


# ======================================================================== the run

class RunLookup(TmpDir):
    def setUp(self):
        super().setUp()
        self.base, self.r2 = self.tmp / "naut", self.tmp / "naut-r2"
        self.base.mkdir()
        self.r2.mkdir()
        recs = [{"series": "naut", "run": 1, "playthrough": "naut", "started": T0, "t": T0 + 10},
                {"series": "naut", "run": 2, "playthrough": "naut-r2", "started": T0 + 20, "t": T0 + 30}]
        (self.base / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs) + "garbage\n")
        (self.base / "series.json").write_text(json.dumps({"profile": {"character": {"brief": "A watch sergeant",
                                                                                     "roleplay": "strict"}}}))
        self.pt = self.tmp / "playthroughs"
        (self.pt / "naut-r2" / "memory").mkdir(parents=True)
        (self.pt / "naut-r2" / "memory" / "CHARACTER.md").write_text(CHARACTER)
        old, J.PLAYTHROUGHS = J.PLAYTHROUGHS, self.pt
        self.addCleanup(setattr, J, "PLAYTHROUGHS", old)

    def test_find_record_and_streams_dir(self):
        self.assertEqual(J.find_record(self.base, 1)["playthrough"], "naut")
        self.assertEqual(J.find_record(self.base)["run"], 2)            # the last
        self.assertEqual(J.find_record(self.r2)["run"], 2)              # a run dir: its sibling series dir
        self.assertIsNone(J.find_record(self.base, 7))
        self.assertEqual(J.streams_dir(self.base, J.find_record(self.base, 2)), self.r2)
        self.assertEqual(J.streams_dir(self.base, J.find_record(self.base, 1)), self.base)

    def test_character_md_then_brief(self):
        text, src = J.load_character({"playthrough": "naut-r2", "series": "naut"}, self.r2)
        self.assertIn("Orsen Vale", text)
        self.assertTrue(src.endswith("CHARACTER.md"))
        text, src = J.load_character({"playthrough": "naut", "series": "naut"}, self.base)
        self.assertIn("A watch sergeant", text)
        self.assertTrue(src.endswith("series.json"))
        self.assertEqual(J.load_character({"playthrough": "x"}, self.tmp / "x"), (None, ""))
        self.assertEqual(J.load_character(None, self.base, self.tmp / "missing.md"), (None, ""))

    def test_judge_run_end_to_end(self):
        _write(self.r2, "S001", [
            _use("a", "note", {"text": "[decision] before the run"}), _res("a", "ok"),
            _use("b", "dialogue_advance", {}), _res("b", LAEZEL),
            _use("c", "choose_dialogue", {"option": 2}), _res("c", "conversation ended"),
        ], {"a": T0 + 5, "b": T0 + 21, "c": T0 + 22})
        fake = FakeClient([_verdict(8, {1: 8})])
        res = J.judge_run(self.base, J.find_record(self.base, 2), client=fake, call=fake)
        self.assertEqual((res["status"], res["score"], res["decisions"], res["judge_model"]),
                         ("ok", 8.0, 1, "judge-model-x"))
        self.assertEqual(res["items"][0]["chosen"], "What do you suggest?")
        self.assertEqual(res["items"][0]["why"], "why 1")
        self.assertIn("Orsen Vale", fake.prompts[0])
        self.assertNotIn("before the run", fake.prompts[0])
        p = J.persona_field(res)
        self.assertEqual((p["status"], p["score"]), ("ok", 8.0))
        self.assertNotIn("items", p)

    def test_judge_run_no_decisions_no_character_same_model(self):
        fake = FakeClient([])
        res = J.judge_run(self.base, J.find_record(self.base, 2), client=fake, call=fake)
        self.assertEqual((res["status"], res["score"]), ("no_decisions", None))
        res = J.judge_run(self.tmp / "nowhere", {"playthrough": "nowhere"}, client=fake, call=fake)
        self.assertEqual(res["status"], "no_character")
        res = J.judge_run(self.base, {"model": "opus"}, client=J.ClaudeCLI("claude-opus-5-5"), call=fake)
        self.assertEqual(res["status"], "judge_failed")
        self.assertIn("model under test", res["error"])
        self.assertEqual(fake.prompts, [])

    def test_cli_dry_run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(J.main([str(self.base), "--run", "2", "--dry-run"]), 0)
        self.assertIn("Orsen Vale", buf.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(J.main([str(self.base), "--run", "9", "--dry-run"]), 2)


# ======================================================================== the shim's wiring (the judge itself:
# packages/harness/tests/test_llm_judge.py)

class Shim(TmpDir):
    def test_default_client_is_claude_with_the_pinned_model(self):
        self.assertIs(J.ClaudeCLI, J._core.ClaudeCLI)
        seen = []
        res = J.judge_run(self.tmp, {"model": J.DEFAULT_MODEL}, call=lambda c, p: seen.append(c))
        self.assertIn("model under test", res["error"])
        self.assertEqual(res["judge_model"], J.DEFAULT_MODEL)

    def test_cli_profile(self):
        prof = REPO / "profiles" / "deepseek-flash.toml"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(J.main([str(self.tmp), "--profile", str(prof), "--dry-run"]), 2)
        self.assertIn("needs a cost cap", err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(J.main([str(self.tmp), "--profile", str(prof), "--max-cost", "1", "--dry-run"]), 0)
        self.assertIn("judge deepseek-flash run None", out.getvalue())
        bad = self.tmp / "bad.toml"
        bad.write_text("client = 'nope'")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(J.main([str(self.tmp), "--profile", str(bad)]), 2)


if __name__ == "__main__":
    unittest.main()
