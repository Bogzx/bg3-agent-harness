import io
import json
import unittest
from contextlib import redirect_stdout

from bg3play.memory import lint as L
from tests.memory._testutil import MemoryTestCase, character_text


def kinds(items):
    return [k for k, _ in items]


class TemplateTest(MemoryTestCase):
    def test_fresh_template_is_clean(self):
        r = L.lint(self.mem)
        self.assertEqual(r.errors, [], r.render(self.mem))

    def test_missing_file_is_error(self):
        (self.mem / "npcs.md").unlink()
        r = L.lint(self.mem)
        self.assertIn("missing", kinds(r.errors))

    def test_missing_memory_dir(self):
        r = L.lint(self.tmp / "nope")
        self.assertFalse(r.ok)


class CharacterTest(MemoryTestCase):
    def test_optional_until_committed_then_required(self):
        self.assertNotIn("character", kinds(L.lint(self.mem).errors))
        self.write("CHARACTER.md", character_text())
        self.assertEqual(L.lint(self.mem).errors, [], L.lint(self.mem).render(self.mem))
        self.commit("S001")
        (self.mem / "CHARACTER.md").unlink()
        errs = L.lint(self.mem).errors
        self.assertTrue(any(k == "deletion" and m.startswith("CHARACTER.md") for k, m in errs), errs)

    def test_sections_header_and_budget(self):
        self.write("CHARACTER.md", character_text().replace("## Flaws\n", "## Weaknesses\n"))
        self.assertTrue(any("'## Flaws'" in m for _, m in L.lint(self.mem).errors))
        self.write("CHARACTER.md", character_text().replace("Roleplay: strict", "Roleplay: sometimes"))
        self.assertTrue(any("strict or light" in m for _, m in L.lint(self.mem).errors))
        self.write("CHARACTER.md", character_text().replace("Deity: Moradin", "Deity: (deity, or none)"))
        self.assertTrue(any("'Deity: …'" in m for _, m in L.lint(self.mem).errors))
        self.write("CHARACTER.md", character_text().replace("## Backstory\n", "## Backstory\n" + "b" * 1700 + "\n"))
        self.assertTrue(any(k == "budget" and m.startswith("CHARACTER.md") for k, m in L.lint(self.mem).errors))
        # the development section grows without counting against the core budget
        self.write("CHARACTER.md", character_text(tuple(f"- S{n:03d}: " + "d" * 80 for n in range(2, 30))))
        self.assertEqual(L.lint(self.mem).errors, [])

    def test_template_and_playbook_format_match_lint(self):
        import re
        from bg3play.memory import memlib as M
        playbook = (M.REPO_ROOT / "play" / "knowledge" / "playbooks" / "character-creation.md").read_text()
        skeleton = playbook.split("## CHARACTER.md format", 1)[1].split("```")[1]
        template = M.strip_comments((M.TEMPLATE_DIR / "CHARACTER.md").read_text())
        for text in (skeleton, template):
            self.assertEqual(re.findall(r"^## (.+)$", text, re.MULTILINE), L.CHARACTER_SECTIONS)
            self.assertEqual(re.findall(r"^(\w+):", text, re.MULTILINE), L.CHARACTER_HEADER)

    def test_guarded_against_bulk_deletion(self):
        self.write("CHARACTER.md", character_text(tuple(f"- S{n:03d}: grew [seen]" for n in range(2, 20))))
        self.commit("S020")
        self.write("CHARACTER.md", character_text(()))
        self.assertTrue(any(k == "deletion" and m.startswith("CHARACTER.md") for k, m in L.lint(self.mem).errors))


class LessonsTest(MemoryTestCase):
    def test_lesson_lines_parse_or_error(self):
        self.write("lessons.md", "# Lessons\n<!-- - free text in a comment is fine -->\n## Lessons\n"
                   "- WHEN Undead, Ghoul: claws paralysed Karlach → Turn Undead first (S023) [seen]\n")
        self.assertEqual([e for e in L.lint(self.mem).errors if e[0] == "lessons"], [])
        self.append("lessons.md", "- Undead are dangerous (S023)\n")
        self.assertIn("lessons", kinds(L.lint(self.mem).errors))

    def test_optional(self):
        (self.mem / "lessons.md").unlink(missing_ok=True)
        self.assertNotIn("lessons", kinds(L.lint(self.mem).errors))
        self.assertNotIn("missing", kinds(L.lint(self.mem).errors))


class BudgetTest(MemoryTestCase):
    def test_line_budget(self):
        self.write("objectives.md", "# Objectives\n" + "".join(f"- O-{i} goal\n" for i in range(90)))
        r = L.lint(self.mem)
        self.assertTrue(any(k == "budget" and m.startswith("objectives.md") for k, m in r.errors))

    def test_comments_and_blank_lines_do_not_count(self):
        body = "# Objectives\n<!--\n" + "example line\n" * 200 + "-->\n" + "\n" * 50 + "- O-1 goal\n"
        self.write("objectives.md", body)
        self.assertEqual([e for e in L.lint(self.mem).errors if e[0] == "budget"], [])

    def test_char_budget_for_injected_files(self):
        self.write("HANDOFF.md", "# Handoff S000 → S001\n" + ("x" * 100 + "\n") * 40)
        r = L.lint(self.mem)
        self.assertTrue(any("content chars" in m for _, m in r.errors))

    def test_chapter_budget(self):
        self.write("chapters/C01_S001-S010_x.md", "# C1\n" + "line\n" * 151)
        self.assertIn("budget", kinds(L.lint(self.mem).errors))


class ProvenanceTest(MemoryTestCase):
    def test_untagged_bullet_reported_with_line_number(self):
        self.append("npcs.md", "\n## Kagha (Grove)\n- Hostile to tieflings\n- Thread: expose her\n")
        r = L.lint(self.mem)
        msgs = [m for k, m in r.errors if k == "provenance"]
        self.assertEqual(len(msgs), 1, msgs)
        lineno = self.read("npcs.md").splitlines().index("- Hostile to tieflings") + 1
        self.assertIn(f"npcs.md:{lineno}:", msgs[0])

    def test_tags_variants_accepted(self):
        self.append("npcs.md", "- a [seen S004]\n- b [told]\n- c [wiki]\n- d [guess S9]\n* e [SEEN s001]\n")
        self.assertNotIn("provenance", kinds(L.lint(self.mem).errors))

    def test_meta_labels_and_archive_lines_exempt(self):
        self.append("decisions.md", "## D-1 x (S001)\n- Choice: spared him\n- Why: mercy\n"
                                    "- Reversible: no\n- ARCHIVE: condensed\n")
        self.assertNotIn("provenance", kinds(L.lint(self.mem).errors))

    def test_untagged_consequence_in_decisions(self):
        self.append("decisions.md", "## D-1 x (S001)\n- Consequence: he ran away\n")
        self.assertIn("provenance", kinds(L.lint(self.mem).errors))

    def test_untagged_files_not_checked(self):
        self.append("strategy.md", "- grease plus fire works\n")
        self.assertNotIn("provenance", kinds(L.lint(self.mem).errors))

    def test_bogus_tag_warns(self):
        self.append("npcs.md", "- she is a druid [verified] [seen]\n")
        r = L.lint(self.mem)
        self.assertIn("provenance", kinds(r.warnings))


class HandoffTest(MemoryTestCase):
    def test_end_phase_requires_current_session(self):
        r = L.lint(self.mem, "end", 3)
        self.assertTrue(any(k == "handoff" and "S000" in m for k, m in r.errors))

    def test_end_phase_ok(self):
        self.write("HANDOFF.md", self.good_handoff(3))
        self.append("objectives.md", "- O-1 Escape the ship — why: it is crashing [seen S003]\n")
        self.assertEqual(L.lint(self.mem, "end", 3).errors, [])

    def test_end_phase_requires_sections(self):
        self.write("HANDOFF.md", "# Handoff S003 → S004\nSave to load: x\n")
        msgs = [m for k, m in L.lint(self.mem, "end", 3).errors if k == "handoff"]
        self.assertTrue(any("Doing right now" in m for m in msgs))
        self.assertTrue(any("Next steps" in m for m in msgs))

    def test_missing_heading(self):
        self.write("HANDOFF.md", "hello\n")
        self.assertIn("handoff", kinds(L.lint(self.mem).errors))

    def test_stale_handoff_warns_in_any_phase(self):
        r = L.lint(self.mem, "any", 5)
        self.assertIn("handoff", kinds(r.warnings))
        self.assertNotIn("handoff", kinds(r.errors))


class DeletionGuardTest(MemoryTestCase):
    def setUp(self):
        super().setUp()
        self.facts = [f"- fact number {i} [seen S001]" for i in range(15)]
        self.append("npcs.md", "## Someone\n" + "\n".join(self.facts) + "\n")
        self.commit("S001")

    def _remove(self, n):
        text = self.read("npcs.md")
        for line in self.facts[:n]:
            text = text.replace(line + "\n", "")
        self.write("npcs.md", text)

    def test_small_deletion_ok(self):
        self._remove(5)
        self.assertNotIn("deletion", kinds(L.lint(self.mem).errors))

    def test_bulk_deletion_refused_and_quoted(self):
        self._remove(12)
        msgs = [m for k, m in L.lint(self.mem).errors if k == "deletion"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("12 lines removed", msgs[0])
        self.assertIn("fact number 0", msgs[0])

    def test_archive_marker_allows(self):
        self._remove(12)
        self.append("npcs.md", "## Archive\n- ARCHIVE: condensed early NPCs\n")
        r = L.lint(self.mem)
        self.assertNotIn("deletion", kinds(r.errors))
        self.assertIn("deletion", kinds(r.warnings))

    def test_moved_lines_do_not_count(self):
        self._remove(15)
        self.append("npcs.md", "## Archive\n" + "\n".join(self.facts) + "\n")
        self.assertNotIn("deletion", kinds(L.lint(self.mem).errors))

    def test_threshold_configurable(self):
        self._remove(5)
        self.assertIn("deletion", kinds(L.lint(self.mem, max_deleted=3).errors))

    def test_deleted_required_file(self):
        (self.mem / "decisions.md").unlink()
        self.assertIn("deletion", kinds(L.lint(self.mem).errors))

    def test_session_log_append_only(self):
        self.write("sessions/S001.md", "# Session S001\n- 10:00 [quest] got quest\n")
        self.commit("S001b")
        self.append("sessions/S001.md", "- 10:05 [npc] met someone\n")
        self.assertNotIn("append-only", kinds(L.lint(self.mem).errors))
        self.write("sessions/S001.md", "# Session S001\n- 10:00 [quest] got a different quest\n")
        msgs = [m for k, m in L.lint(self.mem).errors if k == "append-only"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("got quest", msgs[0])


class MiscTest(MemoryTestCase):
    def test_markers_validation(self):
        self.write("markers.json", "{nope")
        self.assertIn("markers", kinds(L.lint(self.mem).errors))
        self.write("markers.json", json.dumps({"markers": [{"id": "m1"}]}))
        self.assertIn("markers", kinds(L.lint(self.mem).errors))
        self.write("markers.json", json.dumps({"markers": [{"id": "m1", "label": "door"}]}))
        self.assertNotIn("markers", kinds(L.lint(self.mem).errors))
        # the shape the MCP server's place_marker writes
        self.write("markers.json", json.dumps([{"id": "m1", "label": "door", "pos": [1, 2, 3], "region": "R"}]))
        self.assertNotIn("markers", kinds(L.lint(self.mem).errors))
        self.write("markers.json", json.dumps("nope"))
        self.assertIn("markers", kinds(L.lint(self.mem).errors))

    def test_stale_hypothesis_warns(self):
        self.append("hypotheses.md", "## H-3 Cellar (open, S002)\n- Belief: hatch [guess]\n- Test: look\n")
        self.assertNotIn("hypothesis", kinds(L.lint(self.mem, session=5).warnings))
        self.assertIn("hypothesis", kinds(L.lint(self.mem, session=6).warnings))

    def test_compaction_warning(self):
        self.assertNotIn("compaction", kinds(L.lint(self.mem, session=10).warnings))
        self.assertIn("compaction", kinds(L.lint(self.mem, session=11).warnings))
        self.write("chapters/C01_S001-S010_act1.md", "# Chapter 1\n")
        self.assertNotIn("compaction", kinds(L.lint(self.mem, session=11).warnings))

    def test_cli_exit_codes(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(L.main([str(self.mem)]), 0)
            self.write("markers.json", "{nope")
            self.assertEqual(L.main([str(self.mem)]), 1)


class StalenessTest(MemoryTestCase):
    def commit_session(self, n, rel=None):
        if rel:
            self.append(rel, f"- touched in S{n:03d} [seen S{n:03d}]\n")
        self.write(f"sessions/S{n:03d}.md", f"# Session S{n:03d}\n\n## Log\n")
        self.commit(f"S{n:03d}")

    def test_fresh_files_are_not_stale(self):
        self.assertNotIn("stale", kinds(L.lint(self.mem, "any", 2).warnings))

    def test_objectives_stale_warns_then_errors_at_end(self):
        for n in (1, 2, 3):
            self.commit_session(n)
        r = L.lint(self.mem, "any", 4)
        self.assertIn("stale", kinds(r.warnings))
        self.assertNotIn("stale", kinds(r.errors))
        r = L.lint(self.mem, "end", 4)
        self.assertTrue(any(k == "stale" and m.startswith("objectives.md") for k, m in r.errors))

    def test_commit_or_uncommitted_edit_refreshes(self):
        for n in (1, 2):
            self.commit_session(n)
        self.commit_session(3, "objectives.md")
        self.assertNotIn("objectives.md", dict(L.stale_files(self.mem, 5)))
        self.assertIn(("objectives.md", 6), L.stale_files(self.mem, 9))
        self.append("objectives.md", "- O-2 new goal\n")  # being edited in the open session
        self.assertNotIn("objectives.md", [f for f, _ in L.stale_files(self.mem, 9)])

    def test_slower_files_have_longer_thresholds(self):
        self.commit_session(1)
        stale = dict(L.stale_files(self.mem, 4))
        self.assertIn("objectives.md", stale)
        self.assertNotIn("npcs.md", stale)
        self.assertIn("npcs.md", dict(L.stale_files(self.mem, 5)))

    def test_no_session_no_staleness(self):
        self.assertEqual(L.stale_files(self.mem, None), [])

    def test_party_levels_in_playthrough_warn(self):
        self.append("PLAYTHROUGH.md", "")
        text = self.read("PLAYTHROUGH.md").replace("## Party\n", "## Party\n- Gale: Wizard 2, Evocation\n")
        self.write("PLAYTHROUGH.md", text)
        self.assertIn("copy", kinds(L.lint(self.mem).warnings))
        self.write("PLAYTHROUGH.md", text.replace("Wizard 2, Evocation", "Wizard (Evocation), blaster"))
        self.assertNotIn("copy", kinds(L.lint(self.mem).warnings))


if __name__ == "__main__":
    unittest.main()
