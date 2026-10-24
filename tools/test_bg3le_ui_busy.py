"""The frame lock's busy refusal in patches/bg3le 0011 (src/vendor/bg3le_noesis_lua.inl), compiled offline.

Applies patches/bg3le to a scratch export of the pinned submodule commit, pulls serialized() and the
UI-thread frame hooks out of the patched source, compiles them with g++ against a stub Lua (luaL_error
throws), and replays S086's shape with two threads: the UI thread holds the frame lock (waiting for the
client Lua lock in View::Update) while one Tick makes call after call. Checks counts, not timings.
Run: python3 -m unittest tools/test_bg3le_ui_busy.py
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUBMODULE = REPO / "third_party" / "bg3le"
PATCHES = sorted((REPO / "patches" / "bg3le").glob("*.patch"))
INL = "src/vendor/bg3le_noesis_lua.inl"

NAMES = (
    "g_serialize",
    "g_frame",
    "g_serialize_timeouts",
    "g_ui_timeouts",
    "kFrameWaitMs",
    "kUiWaitMs",
    "g_strict",
    "g_strict_refusals",
    "kUiStrictWaitMs",
    "g_busy_refuse",
    "g_busy",
    "g_busy_refusals",
    "t_framed",
)

HARNESS = r"""
#include <atomic>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <future>
#include <mutex>
#include <string>
#include <thread>
struct lua_State { int (*fn)(lua_State*); bool pump = false; };
typedef int (*lua_CFunction)(lua_State*);
#define lua_upvalueindex(i) (-10000 - (i))
static lua_CFunction lua_tocfunction(lua_State* L, int) { return L->fn; }
static int lua_toboolean(lua_State* L, int i) { return i == lua_upvalueindex(2) && L->pump; }
struct LuaError { std::string msg; };
static int luaL_error(lua_State*, char const* fmt, ...) {
  char buf[256]; va_list ap; va_start(ap, fmt); std::vsnprintf(buf, sizeof buf, fmt, ap); va_end(ap);
  throw LuaError{ buf };
}
namespace bg3le { static void logf(char const* fmt, ...) {
  va_list ap; va_start(ap, fmt); std::vfprintf(stdout, fmt, ap); va_end(ap); std::printf("\n");
} }
namespace Noesis { namespace bg3le_ui {
GLOBALS
FUNCS
} }
HOOKS
using namespace Noesis::bg3le_ui;
static int runs = 0;
static int ui_call(lua_State*) { ++runs; return 0; }
// One Tick's n calls: runs, refusals, the first refusal's text
struct Tick { int ran = 0, refused = 0; std::string first; };
static Tick tick(int n) {
  Tick t; lua_State L{ ui_call }; const int before = runs;
  for (int i = 0; i < n; ++i) {
    try { serialized(&L); } catch (LuaError const& e) { if (t.refused++ == 0) t.first = e.msg; }
  }
  t.ran = runs - before; return t;
}
// One Ext._Internal.RunTimers: the prelude's pump (a UiTake* call), then the timers and the Tick event, which run
// only if the pump did not raise
static int timers_ran = 0;
static void run_timers() {
  lua_State L{ ui_call, true };
  try { serialized(&L); } catch (LuaError const&) { return; }
  ++timers_ran;
}
// The UI thread: takes the lock as a frame does, holds it until told, then ends that frame and runs one more
struct UiThread {
  std::promise<void> held, release; std::thread th;
  UiThread() {
    th = std::thread([this] {
      const bool taken = bg3le_ui_frame_begin(); held.set_value();
      release.get_future().wait(); bg3le_ui_frame_end(taken);
      bg3le_ui_frame_end(bg3le_ui_frame_begin());
    });
    held.get_future().wait();
  }
  void finish() { release.set_value(); th.join(); }
};
int main() {
  int fails = 0;
  auto check = [&](char const* what, bool ok) { std::printf("%s %s\n", ok ? "ok" : "FAIL", what); fails += !ok; };
  auto busy_text = [](std::string const& s) { return s.rfind("bg3le: UI busy", 0) == 0; };
  g_serialize = true;
  { Tick t = tick(3);
    check("lock free: calls run, no timeout", t.ran == 3 && t.refused == 0 && g_serialize_timeouts == 0); }
  { UiThread ui; Tick t = tick(20);  // S086: the UI thread holds the lock for the whole Tick
    check("held, not strict: the first call waits and runs unserialized", t.ran == 1 && g_serialize_timeouts == 1);
    check("held: the other 19 are refused at once (no more waits)", t.refused == 19 && g_busy_refusals == 19);
    check("refusal text starts 'bg3le: UI busy'", busy_text(t.first));
    check("busy while not taken again", g_busy.load());
    ui.finish(); }
  check("a frame took the lock again: not busy", !g_busy.load());
  { Tick t = tick(2);
    check("after: calls run serialized again", t.ran == 2 && t.refused == 0 && g_serialize_timeouts == 1); }
  { g_busy = true; Tick t = tick(1);  // no frame since (e.g. no View updates): a call that finds it free
    check("busy, lock free: the call takes it and runs", t.ran == 1 && t.refused == 0 && !g_busy.load()); }
  g_strict = true;
  { UiThread ui; Tick t = tick(5);
    check("strict: the timed-out call is refused too, the rest at once",
          t.ran == 0 && t.refused == 5 && g_serialize_timeouts == 2 && g_strict_refusals == 1 && g_busy_refusals == 23);
    check("strict refusal text starts 'bg3le: UI busy'", busy_text(t.first));
    ui.finish(); }
  g_strict = false; g_busy_refuse = false;
  { UiThread ui; Tick t = tick(2);
    check("UiBusy(false): every call waits and runs unserialized",
          t.ran == 2 && t.refused == 0 && g_serialize_timeouts == 4);
    ui.finish(); }
  g_busy_refuse = true;
  { UiThread ui; tick(1);  // live D1: a frame holds the lock after a timeout, ticks go on
    const int before = runs; const auto refused = g_busy_refusals.load();
    for (int i = 0; i < 10; ++i) run_timers();
    check("busy: RunTimers' pump takes nothing, the timers and Tick still run every tick", timers_ran == 10);
    check("busy: the pump left its queue alone (counted as refused)",
          runs == before && g_busy_refusals == refused + 10);
    ui.finish(); }
  { run_timers();
    check("frame taken again: the pump runs", timers_ran == 11 && !g_busy.load()); }
  g_strict = true;
  { UiThread ui; const int before = runs; run_timers();
    check("strict: a pump call that times out takes nothing, RunTimers goes on",
          timers_ran == 12 && runs == before && g_serialize_timeouts == 6);
    ui.finish(); }
  return fails;
}
"""


def extract_function(src, head):
    m = re.search(r"^" + re.escape(head) + r"\(.*?^}\n", src, re.DOTALL | re.MULTILINE)
    if not m:
        raise AssertionError(f"{head} not found in the patched {INL}")
    return m.group(0)


def extract_global(src, name):
    m = re.search(r"^(?:std::[\w:<>, ]+|constexpr int|thread_local bool) " + name + r"\b[^;\n]*;$", src, re.MULTILINE)
    if not m:
        raise AssertionError(f"{name} not found in the patched {INL}")
    return m.group(0)


@unittest.skipUnless((SUBMODULE / ".git").exists() and shutil.which("g++"), "submodule or g++ missing")
class UiBusyTest(unittest.TestCase):
    def test_busy_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "bg3le"
            src.mkdir()
            touched = sorted(
                {
                    line[len("+++ b/") :].strip()
                    for p in PATCHES
                    for line in p.read_text().splitlines()
                    if line.startswith("+++ b/")
                }
            )
            archive = subprocess.run(
                ["git", "archive", "HEAD", *touched], cwd=SUBMODULE, check=True, capture_output=True
            ).stdout
            subprocess.run(["tar", "-x"], cwd=src, input=archive, check=True)
            subprocess.run(["git", "init", "-q"], cwd=src, check=True)
            for p in PATCHES:
                subprocess.run(["git", "apply", str(p)], cwd=src, check=True)
            inl = (src / INL).read_text()
            code = (
                HARNESS.replace("GLOBALS", "\n".join(extract_global(inl, n) for n in NAMES))
                .replace(
                    "FUNCS", "\n".join(extract_function(inl, h) for h in ("bool log_this_count", "int serialized"))
                )
                .replace(
                    "HOOKS",
                    "\n".join(
                        extract_function(inl, h)
                        for h in ('extern "C" bool bg3le_ui_frame_begin', 'extern "C" void bg3le_ui_frame_end')
                    ),
                )
            )
            cpp = Path(tmp) / "busy.cpp"
            cpp.write_text(code)
            exe = Path(tmp) / "busy"
            build = subprocess.run(
                ["g++", "-std=c++17", "-O1", "-pthread", "-o", str(exe), str(cpp)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            res = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertNotIn("FAIL", res.stdout)


if __name__ == "__main__":
    unittest.main()
