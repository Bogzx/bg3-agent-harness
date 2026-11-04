"""Offline tests: reporting._merge_dialogue_tags, the answer rows' tags from state.dialogue_tags (REVIEW-10).

v010 window 10 L1 (ops/logs/live.md "## L1"): Lae'zel's cage shows 6 answers, 3 = [PERSUASION], 4 = [PALADIN]
[DECEPTION]. The client reads one unreadable ls.LSRun per tag (runs 1 and 2) and each row's AnswerIdx; the server's
NodeSelection gives Persuasion (its Tag REALLY_SHADOWHEART flag is false: no label) and Deception + Paladin.

Run: cd bridge && uv run python -m pytest tests/test_dialogue_tags.py
"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bg3ai.tools import core, reporting
from test_turn_macros import (
    Base,  # noqa: F401  (sets BG3AI_RUNTIME before importing server)
)

TEXTS = [
    "Rather demanding for a woman in your position. Why should I?",
    "Don't worry - I'll get them out of here, one way or another.",
    "Shoot the trap down. We must kill it before it harms someone.",
    "Your quarry looks dangerous, but I've handled worse. Leave it to me, get to safety.",
    "Attack the tieflings.",
    "Leave.",
]
RUNS = [0, 0, 1, 2, 0, 0]
# state.dialogue_tags as Server/StateDialog.lua answers it for ans5.json's NodeSelection
CAGE = {
    "answers": [
        {"idx": 0, "tags": []},
        {"idx": 1, "tags": []},
        {"idx": 2, "roll": "Persuasion", "tags": []},
        {"idx": 3, "roll": "Deception", "tags": ["Paladin"]},
        {"idx": 4, "tags": []},
        {"idx": 5, "tags": []},
    ]
}


def ui(runs=RUNS, idx=True, **extra) -> dict:
    opts = [
        {"i": k + 1, "text": t, "runs": r, **({"idx": k} if idx else {})}
        for k, (t, r) in enumerate(zip(TEXTS, runs, strict=True))
    ]
    return {
        "screen": "dialogue",
        "dialogue": {"speaker": "Lae'zel", "text": "Get rid of them.", "options": opts, **extra},
    }


class Fake:
    def __init__(self, answer: dict) -> None:
        self.answer, self.calls = answer, []

    def try_rpc(self, op, args=None, **kw):
        self.calls.append(op)
        return self.answer if op == "state.dialogue_tags" else {}


class MergeDialogueTags(unittest.TestCase):
    def merge(self, u: dict, answer: dict = CAGE) -> tuple[list[dict], Fake, list[str]]:
        fake, logged = Fake(answer), []
        with (
            unittest.mock.patch.object(core, "game", fake),
            unittest.mock.patch.object(core, "_log", lambda tool, args, msg, ms, *a, **k: logged.append(msg)),
        ):
            out = reporting._merge_dialogue_tags(u)
        return out["dialogue"]["options"], fake, logged

    def test_the_cage_gets_the_screens_tags_and_never_the_false_flag(self):
        opts, _, logged = self.merge(ui())
        self.assertEqual(
            [o.get("tags") for o in opts], [None, None, ["PERSUASION"], ["PALADIN", "DECEPTION"], None, None]
        )
        self.assertNotIn("SHADOWHEART", str(opts))
        self.assertEqual(logged, [])
        for o in opts:  # the merge keys never reach the agent
            self.assertNotIn("idx", o)
            self.assertNotIn("runs", o)

    def test_a_label_count_that_differs_from_the_rows_runs_gives_no_labels(self):
        hidden = {"answers": [dict(a) for a in CAGE["answers"]]}
        hidden["answers"][2]["tags"] = ["Shadowheart"]  # a hidden condition would make 2 labels on a 1-run row
        opts, _, logged = self.merge(ui(), hidden)
        self.assertTrue(all("tags" not in o for o in opts))
        self.assertEqual(
            logged, ["no labels: answer 3: idx 2, 1 tag runs on the row, 2 labels in the game's selection"]
        )

    def test_a_stale_selection_gives_no_labels(self):
        five = {"answers": CAGE["answers"][:5]}
        opts, _, logged = self.merge(ui(), five)
        self.assertTrue(all("tags" not in o for o in opts))
        self.assertEqual(logged, ["no labels: 5 answers in the game's selection, 6 in the list"])

    def test_an_option_without_answer_index_gives_no_labels(self):
        opts, _, logged = self.merge(ui(idx=False))
        self.assertTrue(all("tags" not in o for o in opts))
        self.assertEqual(len(logged), 1)

    def test_the_ops_refusal_is_logged_once(self):
        opts, _, logged = self.merge(ui(), {"why": "no open dialogue"})
        self.assertTrue(all("tags" not in o for o in opts))
        self.assertEqual(logged, ["no labels: no open dialogue"])

    def test_no_tag_run_on_any_row_asks_nothing(self):
        opts, fake, logged = self.merge(ui(runs=[0] * 6))
        self.assertEqual(fake.calls, [])
        self.assertEqual(logged, [])
        self.assertNotIn("idx", opts[0])

    def test_a_scrolled_window_counts_the_answers_out_of_view(self):
        u = ui()
        u["dialogue"]["options"] = u["dialogue"]["options"][2:]  # answers 3-6 shown, 2 above
        u["dialogue"]["more_above"] = 2
        opts, _, logged = self.merge(u)
        self.assertEqual(opts[1]["tags"], ["PALADIN", "DECEPTION"])
        self.assertEqual(logged, [])

    def test_tags_the_client_read_stay_and_the_cached_options_are_not_changed(self):
        u = ui()
        u["dialogue"]["options"][3]["tags"] = ["PALADIN", "DECEPTION"]
        before = [dict(o) for o in u["dialogue"]["options"]]
        opts, _, _ = self.merge(u)
        self.assertEqual(opts[3]["tags"], ["PALADIN", "DECEPTION"])
        self.assertEqual(u["dialogue"]["options"], before)  # _complete_answers caches these dicts

    def test_rendered_for_the_agent(self):
        fake = Fake(CAGE)
        with unittest.mock.patch.object(core, "game", fake):
            text = "\n".join(reporting._render_ui(ui()))
        self.assertIn("[PALADIN][DECEPTION]", text)
        self.assertIn("[PERSUASION]", text)
        self.assertNotIn("idx", text)


if __name__ == "__main__":
    unittest.main()
