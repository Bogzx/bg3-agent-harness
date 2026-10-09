"""patches/bg3le applies to the pinned bg3le commit, and tools/build_bg3le.sh applies it idempotently.

Works on a scratch export of the submodule's HEAD (`git archive`), never on the submodule itself.
Run: python3 -m unittest tools/test_bg3le_patches.py
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUBMODULE = REPO / "third_party" / "bg3le"
PATCHES = sorted((REPO / "patches" / "bg3le").glob("*.patch"))
SCRIPT = REPO / "tools" / "build_bg3le.sh"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@unittest.skipUnless((SUBMODULE / ".git").exists(), "submodule not checked out")
class PatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "bg3le"
        self.src.mkdir()
        # Only the files the patches touch, from the pinned commit (HEAD of the submodule).
        touched = sorted({line[len("+++ b/"):].strip() for p in PATCHES
                          for line in p.read_text().splitlines() if line.startswith("+++ b/")})
        archive = subprocess.run(["git", "archive", "HEAD", "CMakeLists.txt", *touched],
                                 cwd=SUBMODULE, check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x"], cwd=self.src, input=archive, check=True)
        git("init", "-q", cwd=self.src)
        git("add", "-A", cwd=self.src)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pinned", cwd=self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args):
        env = dict(os.environ, BG3LE_SRC=str(self.src))
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)

    def test_there_are_patches(self):
        self.assertTrue(PATCHES, "patches/bg3le/*.patch is empty")

    def test_apply_is_idempotent_and_reversible(self):
        first = self.run_script("--patch")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.count("patch applied:"), len(PATCHES), first.stdout)
        patched = git("diff", cwd=self.src)
        self.assertTrue(patched)

        again = self.run_script("--patch")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn(f"patches already applied: {len(PATCHES)}", again.stdout)
        self.assertEqual(git("diff", cwd=self.src), patched)

        back = self.run_script("--unpatch")
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertEqual(git("status", "--porcelain", cwd=self.src), "")

    def test_conflicting_tree_is_refused(self):
        target = self.src / "src" / "vendor" / "bg3le_noesis_lua.inl"
        text = target.read_text()
        target.write_text(text.replace("void push_object(lua_State* L, void const* o)",
                                       "void push_object(lua_State* L, void const* edited)"))
        res = self.run_script("--patch")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("does not apply", res.stderr)

    def test_patch_keeps_identity_and_reference_contract(self):
        # The prelude compares proxies by tostring() of the pointer; the box must print the Noesis
        # address as a light userdata would, and hold/release a reference.
        self.assertEqual(self.run_script("--patch").returncode, 0)
        inl = (self.src / "src" / "vendor" / "bg3le_noesis_lua.inl").read_text()
        for needle in ('"userdata: %p"', "AddReference()", "->Release()", "__gc",
                       "luaL_testudata", "lua_islightuserdata", '"__mode"'):
            self.assertIn(needle, inl)
        ui = (self.src / "src" / "noesis_ui.cpp").read_text()
        self.assertEqual(ui.count("    bg3le_ui_drain_releases();"), 2)  # both Update hooks
        # 0002 + 0003: the serializer, on unless BG3LE_UI_SERIALIZE=0 / UiSerialize(false); held by
        # both Update hooks and the input hooks (one template), timed on both sides
        self.assertEqual(ui.count("= bg3le_ui_frame_begin();"), 3)
        self.assertEqual(ui.count("bg3le_ui_frame_end(taken);"), 3)
        for needle in ("_ZN6Noesis4View9MouseMoveEii", "_ZThn16_N6Noesis4View9MouseMoveEii",
                       "_ZN6Noesis4View7KeyDownENS_3KeyE", "hook_input("):
            self.assertIn(needle, ui)
        for needle in ("std::atomic<bool> g_serialize{ true };", '"BG3LE_UI_SERIALIZE"', '"UiSerialize"',
                       "try_lock_for(std::chrono::milliseconds(kUiWaitMs))"):
            self.assertIn(needle, inl)
        # 0003: a release only in the generation its reference was taken in, else leaked; the
        # generation moves on a View replacement and on loads
        for needle in ("h->Generation = g_generation.load();", "r.Generation != g_generation.load()",
                       "!ui_settled()", 'extern "C" void bg3le_ui_new_generation(char const* why)',
                       "g_releases.clear();"):
            self.assertIn(needle, inl)
        self.assertIn('bg3le_ui_new_generation("View replaced");', ui)
        gs = (self.src / "src" / "game_state.cpp").read_text()
        self.assertIn("if (!in_play(last) || !in_play(now)) bg3le_ui_new_generation(now);", gs)

    def test_partly_applied_is_refused(self):
        # Only the first patch in place (an older checkout of patches/): neither state, so a stop.
        if len(PATCHES) < 2:
            self.skipTest("one patch")
        git("apply", str(PATCHES[0]), cwd=self.src)
        res = self.run_script("--patch")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("does not apply", res.stderr)
        self.assertNotEqual(self.run_script("--unpatch").returncode, 0)


if __name__ == "__main__":
    unittest.main()
