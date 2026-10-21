"""acceptance.py --check catches what would break a live item: each test mutates a copy of the acceptance package.

    uv run --project bridge python -m unittest tools/dev/test_acceptance_check.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "bridge" / "src"))
_scratch = tempfile.mkdtemp(prefix="acc-check-test-")
os.environ.setdefault("BG3AI_RUNTIME", str(Path(_scratch) / "rt"))
os.environ.setdefault("BG3AI_MEMORY_DIR", str(Path(_scratch) / "memory"))

import acceptance_check as AC  # noqa: E402
from server_view import S  # noqa: E402

SRC = {p.name: p.read_text() for p in sorted((HERE / "acceptance").glob("*.py"))}
LUA = AC.lua_index()
INTERNALS = AC.bg3le_internals()


def run(src: dict[str, str], luac: bool = False) -> dict[str, tuple[str, str]]:
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, text in src.items():
            p = Path(d) / name
            p.write_text(text)
            paths.append(p)
        rows = AC.check(paths, S, None, LUA, INTERNALS, run_luac=luac)
    return {what: (v, ev) for v, what, ev in rows}


def mutate(old: str, new: str) -> dict[str, str]:
    """The package with the first file (by name) holding `old` changed."""
    name = next((n for n, text in SRC.items() if old in text), None)
    assert name is not None, old
    return {**SRC, name: SRC[name].replace(old, new, 1)}


class TestCheck(unittest.TestCase):
    def test_current_file_clean(self):
        got = run(SRC)
        bad = {k: v for k, v in got.items() if v[0] == "FAIL"}
        self.assertEqual(bad, {})

    def test_unknown_tool(self):
        got = run(mutate('took("short_rest")', 'took("short_rests")'))
        self.assertEqual(got["bridge tools + keywords"][0], "FAIL")
        self.assertIn("short_rests", got["bridge tools + keywords"][1])

    def test_bad_keyword_and_missing_required(self):
        got = run(mutate('call("get_nearby", kinds="door", radius=20)', 'call("get_nearby", kind="door", radius=20)'))
        self.assertIn("get_nearby(kind=", got["bridge tools + keywords"][1])
        got = run(mutate('took("swap_party", into="Wyll", out="Gale")', 'took("swap_party", out="Gale")'))
        self.assertIn("required ['into']", got["bridge tools + keywords"][1])

    def test_literal_type(self):
        got = run(mutate('call("get_nearby", kinds="door", radius=20)', 'call("get_nearby", kinds=3, radius=20)'))
        self.assertIn("kinds=3", got["bridge tools + keywords"][1])

    def test_unknown_op_and_wrong_context(self):
        got = run(mutate('S.game.try_rpc("nav.wedged", {})', 'S.game.try_rpc("nav.wedgedx", {})'))
        self.assertIn("nav.wedgedx", got["RPC ops + context"][1])
        got = run(mutate('S.game.try_rpc("ui.travel_block", context="client")', 'S.game.try_rpc("ui.travel_block")'))
        self.assertIn("registered only in ['client']", got["RPC ops + context"][1])

    def test_lua_field_and_module_context(self):
        got = run(mutate("tostring(A.LADDER_WATCH_AT_USE)", "tostring(A.LADDER_WATCH_AT_USEX)"))
        self.assertEqual(got["Lua snippets: mod fields + context"][0], "FAIL")
        self.assertIn("Actions.LADDER_WATCH_AT_USEX", got["Lua snippets: mod fields + context"][1])
        got = run(mutate('local UI = Mods.BG3AI.BG3AI.UI local dc', 'local UI = Mods.BG3AI.BG3AI.UIx local dc'))
        self.assertIn("no BG3AI.UIx module", got["Lua snippets: mod fields + context"][1])
        # a server module read from a client eval (eval's default context is client)
        got = run(mutate('S.game.eval("Mods.BG3AI.BG3AI.__acceptance = true return 1", context="server")',
                         'S.game.eval("return Mods.BG3AI.BG3AI.Guard.pos ~= nil")'))
        self.assertIn("BG3AI.Guard lives in ['server'], not client", got["Lua snippets: mod fields + context"][1])

    def test_internal_and_non_string_code(self):
        got = run(mutate("Ext._Internal.UiStrict(true) return 1", "Ext._Internal.UiStrictX(true) return 1"))
        self.assertIn("UiStrictX", got["Lua snippets: mod fields + context"][1])
        # the 10-13 vis crash: g bound to a dict, then g + '...'
        got = run(mutate('    w = S.game.try_rpc("nav.wedged", {}) or {}\n',
                         '    g = S.game.try_rpc("nav.wedged", {}) or {}\n    w = g\n'))
        self.assertIn("built from non-strings ['g']", got["Lua snippets: mod fields + context"][1])

    def test_attribute_and_undefined_name(self):
        got = run(mutate("S._select_for_level_up(who)", "S._select_for_levelup(who)"))
        self.assertIn("S._select_for_levelup", got["Python attributes (S.*, imported modules)"][1])
        got = run(mutate("HB.cast_state(S.game, sel)", "HB.cast_states(S.game, sel)"))
        self.assertIn("HB.cast_states", got["Python attributes (S.*, imported modules)"][1])
        got = run(mutate("HB.cast_state(S.game, sel)", "HB.cast_state(S.game, sel, 1, 2, 3)"))
        self.assertIn("HB.cast_state(...): too many", got["Python attributes (S.*, imported modules)"][1])
        got = run(mutate('S._select_for_level_up(who)', 'S._select_for_level_up(whom=who)'))
        self.assertIn("S._select_for_level_up(...)", got["Python attributes (S.*, imported modules)"][1])
        got = run(mutate("    lg = bg3le_log()\n    cnt =", "    lg = bg3le_logs()\n    cnt ="))
        self.assertIn("bg3le_logs", got["names defined"][1])

    def test_a_section_must_import_what_it_reads(self):
        # each module's names are its own: one defined in core but not imported by the section is undefined there
        got = run(mutate("from .core import BASE, alive, bg3le_log, grep_crash, item, load_verified\n",
                         "from .core import BASE, alive, grep_crash, item, load_verified\n"))
        self.assertEqual(got["names defined"][0], "FAIL")
        self.assertIn("sec_loads:bg3le_log", got["names defined"][1])


    @unittest.skipUnless(shutil.which("docker"), "no docker")
    def test_luac_syntax(self):
        got = run(mutate("return tostring(A.LADDER_WATCH_AT_USE)", "return tostring(A.LADDER_WATCH_AT_USE"), luac=True)
        v, ev = got["Lua snippets compile (luac 5.4)"]
        if v == "SKIP":
            self.skipTest(ev)
        self.assertEqual(v, "FAIL")
        self.assertIn("sec_act", ev)


class TestSaves(unittest.TestCase):
    def test_needs(self):
        class FakeS:
            SAVE_DIR, SAVE_PREFIX = Path("/nowhere"), "Claude-"

            @staticmethod
            def _my_saves():
                return [Path("/nowhere/Claude-2__S050-end"), Path("/nowhere/Claude-1__S050-end")]

        info = {"Current Level": "CTY_Main_A", "Active Party": {"Characters": [
            {"Origin": "Generic", "Position": [-233.0, 4.5, -150.0], "Subregion": "LOW_City_SUB"}]}}
        old = AC.save_info
        AC.save_info = lambda d: info if d.name == "Claude-2__S050-end" else None
        try:
            rows = AC.check_saves(FakeS, {"S050-end": {"level": "CTY_Main_A", "near": ((-233.0, 4.5, -180.6), 8)},
                                          "S999-x": {}})
        finally:
            AC.save_info = old
        self.assertEqual(rows[0][0], "FAIL")
        self.assertIn("31 m from", rows[0][2])
        self.assertIn("2 folders end in __S050-end", rows[0][2])
        self.assertEqual(rows[1][0], "FAIL")


if __name__ == "__main__":
    unittest.main()
