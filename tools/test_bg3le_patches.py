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
                       "try_lock_for(std::chrono::milliseconds(strict ? kUiStrictWaitMs : kUiWaitMs))"):
            self.assertIn(needle, inl)
        # 0010 (audit UI-5): strict mode refuses a Lua call on a lock timeout (a Lua error, the caller
        # retries) instead of running it unserialized; off unless BG3LE_UI_STRICT / UiStrict(true); both
        # sides' timeouts are logged
        for needle in ("std::atomic<bool> g_strict{ false };", '"BG3LE_UI_STRICT"', '"UiStrict"',
                       'return luaL_error(L, "bg3le: UI busy (frame lock not taken in %d ms); retry", kFrameWaitMs);',
                       '"noesis: Lua frame-lock timeout #%llu (%d ms)%s"',
                       '"noesis: UI-thread frame-lock timeout #%llu (%d ms): frame ran unguarded"'):
            self.assertIn(needle, inl)
        # 0011 (S22, the S086 hang): after a Lua lock timeout, later calls are refused at once until the
        # lock is taken again (by a frame or a call that finds it free); on unless BG3LE_UI_BUSY_REFUSE=0
        for needle in ("std::atomic<bool> g_busy_refuse{ true };", '"BG3LE_UI_BUSY_REFUSE"', '"UiBusy"',
                       'return luaL_error(L, "bg3le: UI busy (frame lock not taken since a timeout); retry");',
                       "g_busy.store(false, std::memory_order_relaxed);  // taken again"):
            self.assertIn(needle, inl)
        # 0003 + 0005: the generation moves on a View replacement and on loads; a reference taken
        # under an older View leaks, one of an older load generation is parked and given back once
        # the UI has settled (BG3LE_UI_PARK=0: leaked)
        for needle in ("h->Generation = g_generation.load();",
                       "h->ViewGeneration = g_view_generation.load();",
                       "r.ViewGeneration != view", "g_parked.push_back(r);", '"BG3LE_UI_PARK"',
                       "clock - settled_from >= std::chrono::milliseconds(kParkSettleMs)",
                       'extern "C" void bg3le_ui_new_generation(char const* why)',
                       'bg3le_ui_new_generation("View replaced");'):
            self.assertIn(needle, inl)
        self.assertNotIn("g_releases.clear();", inl)  # a new generation no longer drops the queue
        self.assertIn("bg3le_ui_view_replaced();", ui)
        gs = (self.src / "src" / "game_state.cpp").read_text()
        self.assertIn("if (!in_play(last) || !in_play(now)) bg3le_ui_new_generation(now);", gs)
        # 0006 (S017's SIGSEGV at 0x100 in push_boxed_or_object): a property value is checked before
        # it is boxed, stored values are copied out with safe reads, a DependencyObject's values are
        # enumerated by key (looked up again each) and only under the UI lock; counted in UiSerialize
        for needle in ("bool plausible_vtable(void const* o)", 'bad_value("push_boxed_or_object", name, val);',
                       "if (val != nullptr && !plausible_vtable(val)) {",
                       "!bg3le::safe_read(o, vbuf, sizeof(vbuf))",
                       "!bg3le::safe_read(v.value.complex, cbuf, sizeof(cbuf))",
                       "for (auto& entry : dep->mValues) keys.push_back(entry.key);",
                       "push_dependency(L, dep, key);", "thread_local bool t_framed = false;",
                       "t_framed = held.owns_lock();", 'if (which != "direct" && t_framed',
                       "lua_pushinteger(L, (lua_Integer)g_bad_values.load());", "return 8;"):
            self.assertIn(needle, inl)
        self.assertNotIn("push_stored(L, entry.key->GetType(), entry.value, type, &name);", inl)

    def test_prefix_applied_gets_the_rest(self):
        # The series up to the newest patch in place (a patch was added since the last build): the
        # rest is applied, giving the same tree as a fresh apply; --unpatch reverts a prefix too.
        if len(PATCHES) < 2:
            self.skipTest("one patch")
        self.assertEqual(self.run_script("--patch").returncode, 0)
        full = git("diff", cwd=self.src)
        self.assertEqual(self.run_script("--unpatch").returncode, 0)
        for p in PATCHES[:-1]:
            git("apply", str(p), cwd=self.src)
        res = self.run_script("--patch")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"patches already applied: {len(PATCHES) - 1} of {len(PATCHES)}", res.stdout)
        self.assertEqual(res.stdout.count("patch applied:"), 1, res.stdout)
        self.assertEqual(git("diff", cwd=self.src), full)

        self.assertEqual(self.run_script("--unpatch").returncode, 0)
        git("apply", str(PATCHES[0]), cwd=self.src)
        back = self.run_script("--unpatch")
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertIn("reverted 1 patch(es)", back.stdout)
        self.assertEqual(git("status", "--porcelain", cwd=self.src), "")


if __name__ == "__main__":
    unittest.main()
