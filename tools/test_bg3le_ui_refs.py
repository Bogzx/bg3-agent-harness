"""A dying Noesis object never gets a reference, and no object goes during a C call (patches/bg3le 0014); the crash log
names the running Ext.UI call (0015). Compiled offline.

The crash (bg3le-20261106-011048, Noesis::bg3le_ui::l_get): o->GetClassType() through a free-list word, on a held box's
object. Applies patches/bg3le to a scratch export of the pinned submodule commit, cuts the "objects" and "serializing"
sections out of the patched src/vendor/bg3le_noesis_lua.inl (real code), and compiles them with g++ against a stub Lua
and stub objects. Nothing depends on timing: a stub safe_read frees the object at a fixed read, between
plausible_vtable and the use, and a stub C call releases the object at a fixed line. Each scenario runs as its own
process, so one that dies on the free-list word is seen as a signal. test_scenarios_fail_without_the_fix runs the same
scenarios on the series without the fix and requires them to die or report a failure. The crash log's scenarios link
the real src/stackdump.cpp and src/mem.cpp (a real read), with a stub log.h, and crash for real, some of them with dead
memory where the handler looks: it must get past it and reach the registers.
Run: python3 -m unittest tools/test_bg3le_ui_refs.py
"""

import re
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUBMODULE = REPO / "third_party" / "bg3le"
ALL_PATCHES = sorted((REPO / "patches" / "bg3le").glob("*.patch"))
BEFORE_THE_FIX = [p for p in ALL_PATCHES if p.name < "0014"]
INL = "src/vendor/bg3le_noesis_lua.inl"

SECTIONS = (
    ("// ---- objects ---", "bool is_a(BaseObject const* o"),
    ("std::atomic<bool> g_serialize{ true };", "// UiSerialize([on])"),
)

HARNESS = r"""
#include <atomic>
#include <chrono>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <link.h>
#include <map>
#include <mutex>
#include <new>
#include <string>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <unordered_map>
#include <vector>

// ---- a stub Lua: a value stack, tables as maps; every userdata here is a Held box
enum { LUA_TNONE = -1, LUA_TNIL = 0, LUA_TBOOLEAN = 1, LUA_TLIGHTUSERDATA = 2, LUA_TSTRING = 4, LUA_TTABLE = 5,
       LUA_TFUNCTION = 6, LUA_TUSERDATA = 7 };
struct lua_State;
typedef int (*lua_CFunction)(lua_State*);
struct Slot { int t = LUA_TNIL; void* p = nullptr; std::string s; };
struct Table { std::map<void const*, Slot> m; };
struct lua_State { std::vector<Slot> st; lua_CFunction fn = nullptr; bool pump = false; };
#define LUA_REGISTRYINDEX (-1000000)
#define lua_upvalueindex(i) (LUA_REGISTRYINDEX - (i))
static Table g_registry;
static std::map<std::string, Table*> g_metas;
struct LuaError { std::string msg; };
static Slot& at(lua_State* L, int i) { return L->st[i > 0 ? i - 1 : (int)L->st.size() + i]; }
static void push(lua_State* L, int t, void* p = nullptr, std::string s = {}) {
  L->st.push_back(Slot{ t, p, std::move(s) });
}
static void lua_pop(lua_State* L, int n) { L->st.resize(L->st.size() - n); }
static void lua_pushnil(lua_State* L) { push(L, LUA_TNIL); }
static void lua_newtable(lua_State* L) { push(L, LUA_TTABLE, new Table); }
static void lua_createtable(lua_State* L, int, int) { lua_newtable(L); }
static void lua_pushliteral(lua_State* L, char const* s) { push(L, LUA_TSTRING, nullptr, s); }
static void lua_pushcfunction(lua_State* L, lua_CFunction) { push(L, LUA_TFUNCTION); }
static void lua_setfield(lua_State* L, int, char const*) { lua_pop(L, 1); }
static int lua_setmetatable(lua_State* L, int) { lua_pop(L, 1); return 1; }
static void lua_pushvalue(lua_State* L, int i) { Slot v = at(L, i); L->st.push_back(v); }
static void lua_remove(lua_State* L, int i) { L->st.erase(L->st.begin() + (i > 0 ? i - 1 : (int)L->st.size() + i)); }
static Table& table_at(lua_State* L, int i) {
  return i == LUA_REGISTRYINDEX ? g_registry : *static_cast<Table*>(at(L, i).p);
}
static int lua_rawgetp(lua_State* L, int i, void const* key) {
  Table& t = table_at(L, i); auto it = t.m.find(key);
  if (it == t.m.end()) { lua_pushnil(L); return LUA_TNIL; }
  L->st.push_back(it->second); return it->second.t;
}
static void lua_rawsetp(lua_State* L, int i, void const* key) {
  Table& t = table_at(L, i); t.m[key] = L->st.back(); lua_pop(L, 1);
}
static void* lua_newuserdata(lua_State* L, std::size_t n) {
  void* p = std::malloc(n); push(L, LUA_TUSERDATA, p); return p;
}
static int luaL_newmetatable(lua_State* L, char const* name) {
  auto& m = g_metas[name]; const bool fresh = m == nullptr; if (fresh) m = new Table;
  push(L, LUA_TTABLE, m); return fresh;
}
static void* luaL_testudata(lua_State* L, int i, char const*) {
  return i <= (int)L->st.size() && at(L, i).t == LUA_TUSERDATA ? at(L, i).p : nullptr;
}
static void* luaL_checkudata(lua_State* L, int i, char const* m) { return luaL_testudata(L, i, m); }
static int lua_islightuserdata(lua_State* L, int i) {
  return i <= (int)L->st.size() && at(L, i).t == LUA_TLIGHTUSERDATA;
}
static void* lua_touserdata(lua_State* L, int i) { return at(L, i).p; }
static char const* lua_pushfstring(lua_State*, char const* f, ...) { return f; }
static int lua_type(lua_State* L, int i) { return i <= (int)L->st.size() ? at(L, i).t : LUA_TNONE; }
static bool lua_isnoneornil(lua_State* L, int i) { return lua_type(L, i) <= LUA_TNIL; }
static char const* lua_tostring(lua_State* L, int i) { return at(L, i).s.c_str(); }
static lua_CFunction lua_tocfunction(lua_State* L, int) { return L->fn; }
static int lua_toboolean(lua_State* L, int i) { return i == lua_upvalueindex(2) && L->pump; }
static int luaL_error(lua_State*, char const* fmt, ...) {
  char buf[256]; va_list ap; va_start(ap, fmt); std::vsnprintf(buf, sizeof buf, fmt, ap); va_end(ap);
  throw LuaError{ buf };
}
static int luaL_argerror(lua_State*, int, char const* msg) { throw LuaError{ msg }; }

// ---- stub bg3le: safe_read reads, and frees the armed object at a fixed read
static void free_armed();
static int g_free_at_read = 0;
#ifdef WITH_CRASH_LOG
#include "log.h"
#include "mem.h"
#endif
namespace bg3le {
#ifndef WITH_CRASH_LOG
static void logf(char const* fmt, ...) {
  va_list ap; va_start(ap, fmt); std::vfprintf(stdout, fmt, ap); va_end(ap); std::printf("\n");
}
#endif
#ifndef WITH_CRASH_LOG
static bool safe_read(void const* addr, void* out, std::size_t n) {
  std::memcpy(out, addr, n);
  if (g_free_at_read != 0 && --g_free_at_read == 0) free_armed();
  return true;
}
static bool safe_cstr(void const* addr, char* buf, std::size_t n) {
  std::strncpy(buf, static_cast<char const*>(addr), n - 1); buf[n - 1] = 0; return true;
}
#endif
static char const* client_game_state() { return "Running"; }
static bool noesis_update_hooked() { return true; }
// A code range that holds none of the stub's functions: their vtables pass through own_code, the real one.
static bool text_range(std::uintptr_t* offset, std::size_t* size) { *offset = 0x1000; *size = 1; return true; }
static std::uintptr_t load_bias() { return 0; }
}
#define ERR(...) bg3le::logf(__VA_ARGS__)

// ---- stub Noesis: the game's layout, vtable at +0 with GetClassType in slot 2, the count at +8
namespace Noesis {
struct Symbol { char const* Str() const { return "?"; } };
struct Type { char const* Name; bool Component; char const* GetName() const { return Name; } };
struct BaseObject {
  virtual ~BaseObject() {}
  virtual Type const* GetClassType() const = 0;
};
static Type g_button_type{ "StubButton", true };
static Type g_type_type{ "StubType", false };
static int g_destroyed = 0;
static void* g_free_list_target[8];
// What the allocator leaves in a freed block: a free-list word at +0, whose "slot 2" is not code, and a count of 0.
static void to_free_list(void* block, std::atomic<int32_t>& refs) {
  refs.store(0);
  g_free_list_target[2] = reinterpret_cast<void*>(0x5dd90a8da00a0000ull);  // non-canonical, as the crash's
  void* word = g_free_list_target;
  std::memcpy(block, &word, sizeof word);
}
struct BaseComponent : BaseObject {
  std::atomic<int32_t> mRefCount{ 1 };
  int32_t AddReference() const { return ++const_cast<std::atomic<int32_t>&>(mRefCount); }
  int32_t Release() const {
    int32_t n = --const_cast<std::atomic<int32_t>&>(mRefCount);
    if (n == 0) {
      ++g_destroyed;
      to_free_list(const_cast<BaseComponent*>(this), const_cast<std::atomic<int32_t>&>(mRefCount));
    }
    return n;
  }
};
struct Button : BaseComponent { Type const* GetClassType() const override { return &g_button_type; } };
// A Type is a BaseObject with no count: what lies at +8 is its own.
struct TypeObject : BaseObject { int32_t Own = 77; Type const* GetClassType() const override { return &g_type_type; } };
struct TypeHelpers { static bool IsDescendantOf(Type const* t, Type const*) { return t->Component; } };
template <class T> Type const* TypeOf() { return &g_button_type; }

namespace bg3le_ui {
SECTIONS
} }
HOOKS
using namespace Noesis;
using namespace Noesis::bg3le_ui;

static Button* g_armed = nullptr;
static void free_armed() { to_free_list(g_armed, g_armed->mRefCount); }

static int g_used = 0;
// The shape of l_get: the object, then a call through its vtable
static int l_use(lua_State* L) { auto o = object_arg(L, 1); o->GetClassType(); ++g_used; return 0; }
// The same, with the game dropping every reference it and the box have, at a fixed line between check and use
static int l_release_then_use(lua_State* L) {
  auto o = static_cast<BaseComponent*>(object_arg(L, 1));
  o->Release(); o->Release();
  o->GetClassType(); ++g_used; return 0;
}
static int g_own_in_call = 0;
static int l_read_own(lua_State* L) { g_own_in_call = static_cast<TypeObject*>(object_arg(L, 1))->Own; return 0; }
static int l_two(lua_State* L) { object_arg(L, 1); object_arg(L, 2); return 0; }
static int l_two_then_raise(lua_State* L) { object_arg(L, 1); object_arg(L, 2); return luaL_error(L, "raised"); }

static int call(lua_State* L, lua_CFunction fn, std::string* error = nullptr) {
  L->fn = fn;
  try { return serialized(L); } catch (LuaError const& e) { if (error != nullptr) *error = e.msg; return -1; }
}
static Button* new_button() { return new (std::aligned_alloc(16, sizeof(Button))) Button; }

static int fails = 0;
static void check(char const* what, bool ok) { std::printf("%s %s\n", ok ? "ok" : "FAIL", what); fails += !ok; }

// A box whose object is held by the box alone
static void boxed(lua_State* L, Button* o) { push_object(L, o); o->Release(); }

// A nested call: what the outer object's count is once the inner has returned and the outer has not
static int g_count_after_inner = 0;
static int l_nested(lua_State* L) {
  auto o = static_cast<Button*>(object_arg(L, 1));
  lua_State inner; boxed(&inner, new_button());
  call(&inner, l_use);
  g_count_after_inner = o->mRefCount.load();
  return 0;
}

static void freed_between() {
  lua_State L; Button* o = new_button(); boxed(&L, o);
  g_armed = o; g_free_at_read = 2;  // plausible_vtable's second read: the vtable has passed, the use is ahead
  std::string error; call(&L, l_use, &error);
  check("the armed free ran inside the check", g_free_at_read == 0);
  check("refused with the error try() handles", error == "Noesis object is gone");
  check("the call did not go on", g_used == 0);
}

static void released_during_call() {
  lua_State L; Button* o = new_button(); push_object(&L, o);  // the game's reference and the box's
  std::string error; call(&L, l_release_then_use, &error);
  check("the object lived through the call", g_used == 1 && g_destroyed == 0 && error.empty());
  check("the last reference waits for the UI thread", g_releases.size() == 1 && o->mRefCount.load() == 1);
  g_releases[0].Object->Release();
  check("given back there, it goes", g_destroyed == 1);
}

static void push_dying() {
  lua_State L; Button* o = new_button(); o->mRefCount.store(0);  // OnDestroy running: the count is 0, the vtable intact
  push_object(&L, o);
  check("a dying object is nil", lua_type(&L, -1) == LUA_TNIL);
  check("its count stays 0", o->mRefCount.load() == 0);
  check("one value pushed", L.st.size() == 1);
  check("nothing cached for it",
        lua_rawgetp(&L, LUA_REGISTRYINDEX, &g_held_cache_key) == LUA_TTABLE && lua_rawgetp(&L, -1, o) == LUA_TNIL);
}

static void counts() {
  lua_State L; Button* a = new_button(); Button* b = new_button();
  push_object(&L, a); check("a box holds one reference", a->mRefCount.load() == 2);
  call(&L, l_use); check("a call gives its reference back", a->mRefCount.load() == 2 && g_used == 1);
  push_object(&L, a); check("the cached box takes none", a->mRefCount.load() == 2 && L.st.size() == 2);
  L.st.resize(1);
  push_object(&L, b);
  call(&L, l_two); check("two arguments, both back", a->mRefCount.load() == 2 && b->mRefCount.load() == 2);
  std::string error; call(&L, l_two_then_raise, &error);
  check("an error gives them back too", error == "raised" && a->mRefCount.load() == 2 && b->mRefCount.load() == 2);
  call(&L, l_nested);
  check("a nested call leaves the outer call's reference", g_count_after_inner == 3 && a->mRefCount.load() == 2);
  g_serialize = false;
  call(&L, l_use); check("serializing off, the same", a->mRefCount.load() == 2);
  g_serialize = true;
  check("nothing queued while others hold it", g_releases.empty());

  lua_State T; TypeObject* type = new TypeObject; push_object(&T, type);
  call(&T, l_read_own);
  check("a Type's +8 is not touched, in the call or after", g_own_in_call == 77 && type->Own == 77);

  lua_State F; Button* freed = new_button(); to_free_list(freed, freed->mRefCount); push_object(&F, freed);
  check("a block on the free list is nil, not a call through it", lua_type(&F, -1) == LUA_TNIL && F.st.size() == 1);
  L.st.clear(); Button* dead = new_button(); push_object(&L, dead);
  to_free_list(dead, dead->mRefCount);
  error.clear(); call(&L, l_use, &error);
  check("a box on a freed block is refused", error == "Noesis object is gone");
}

#ifdef WITH_CRASH_LOG
#include "stackdump.h"
// Not static, and linked -rdynamic: the crash log names the function by its symbol
int l_crash(lua_State*) { *reinterpret_cast<int volatile*>(std::uintptr_t{ 8 }) = 1; return 0; }
int l_crash_with_object(lua_State* L) { object_arg(L, 1); return l_crash(L); }
int l_crash_nested(lua_State* L) {
  object_arg(L, 1);
  lua_State inner; boxed(&inner, new_button()); lua_pushliteral(&inner, "Inner");
  return call(&inner, l_crash_with_object);
}
static void crash(lua_CFunction fn) {
  bg3le::install_crash_handler();
  lua_State L; Button* o = new_button(); boxed(&L, o); lua_pushliteral(&L, "Text");
  std::printf("object %p\n", static_cast<void*>(o));
  call(&L, fn);
}
static void crash_no_call() { bg3le::install_crash_handler(); lua_State L; l_crash(&L); }

// Dead memory where the crash handler looks
static void* unmapped_page() {
  void* p = mmap(nullptr, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  munmap(p, 4096);
  return p;
}
// object_arg notes the object, then refuses it; the call goes on and dies
int l_crash_after_refusal(lua_State* L) {
  try { object_arg(L, 1); } catch (LuaError const&) {}
  return l_crash(L);
}
static void crash_dead_box(bool class_too) {
  bg3le::install_crash_handler();
  lua_State L; boxed(&L, new_button()); lua_pushliteral(&L, "Text");
  auto h = static_cast<Held*>(at(&L, 1).p);
  h->Object = static_cast<BaseObject*>(unmapped_page());
  if (class_too) h->ClassName = static_cast<char const*>(unmapped_page());
  std::printf("object %p\n", static_cast<void*>(h->Object));
  call(&L, l_crash_after_refusal);
}
int l_crash_note_unmapped(lua_State* L) { t_call_note = static_cast<CallNote*>(unmapped_page()); return l_crash(L); }
int l_crash_note_low(lua_State* L) {
  t_call_note = reinterpret_cast<CallNote*>(std::uintptr_t{ 0x10 });
  return l_crash(L);
}
int l_crash_note_cycle(lua_State* L) { t_call_note->Outer = t_call_note; return l_crash(L); }
int l_crash_note_junk(lua_State* L) {
  std::memset(t_call_note->Key, 'A', sizeof t_call_note->Key);
  t_call_note->Fn = reinterpret_cast<lua_CFunction>(std::uintptr_t{ 0xdeadbeef000 });
  return l_crash(L);
}
#endif

int main(int argc, char** argv) {
  prctl(PR_SET_DUMPABLE, 0);  // a scenario that dies leaves no core
  setvbuf(stdout, nullptr, _IOLBF, 0);
#ifdef WITH_CRASH_LOG
  // The handler's stack scan reads 1024 words up from the fault: mapped here, however small the environment is.
  char volatile room[16384]; room[0] = 0;
#endif
  std::string s = argc > 1 ? argv[1] : "";
  if (s == "freed_between") freed_between();
  else if (s == "released_during_call") released_during_call();
  else if (s == "push_dying") push_dying();
  else if (s == "counts") counts();
#ifdef WITH_CRASH_LOG
  else if (s == "crash_with_object") crash(l_crash_with_object);
  else if (s == "crash_before_object") crash(l_crash);
  else if (s == "crash_nested") crash(l_crash_nested);
  else if (s == "crash_no_call") crash_no_call();
  else if (s == "dead_object") crash_dead_box(false);
  else if (s == "dead_class") crash_dead_box(true);
  else if (s == "note_unmapped") crash(l_crash_note_unmapped);
  else if (s == "note_low") crash(l_crash_note_low);
  else if (s == "note_cycle") crash(l_crash_note_cycle);
  else if (s == "note_junk") crash(l_crash_note_junk);
#endif
  else { std::printf("FAIL no scenario %s\n", s.c_str()); return 2; }
  return fails != 0;
}
"""

STUB_LOG_H = """#pragma once
namespace bg3le { void logf(const char* fmt, ...); void log_mark_crash(); }
"""
STUB_LOG_CPP = """#include "log.h"
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
namespace bg3le {
void logf(const char* fmt, ...) {
  // A log that never ends (a note chain with no bound) exits here at once, not at the timeout with the output in RAM
  static int lines = 0;
  if (++lines > 500) std::_Exit(77);
  va_list ap; va_start(ap, fmt); std::vfprintf(stdout, fmt, ap); va_end(ap); std::printf("\\n");
}
void log_mark_crash() {}
}
"""


def cut(src, start, end):
    i = src.index(start)
    return src[i : src.index(end, i)]


def crash_hook(src):
    """The crash log's hook, from 0015 on."""
    m = re.search(r'^extern "C" void bg3le_ui_log_crash_call\(.*?^}\n', src, re.DOTALL | re.MULTILINE)
    return m.group(0) if m else ""


@unittest.skipUnless((SUBMODULE / ".git").exists() and shutil.which("g++"), "submodule or g++ missing")
class UiRefsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, patches, name, crash_log=False):
        root = Path(self.tmp.name) / name
        src = root / "bg3le"
        src.mkdir(parents=True)
        touched = sorted(
            {
                line[len("+++ b/") :].strip()
                for p in ALL_PATCHES
                for line in p.read_text().splitlines()
                if line.startswith("+++ b/")
            }
        )
        archive = subprocess.run(
            ["git", "archive", "HEAD", "src/stackdump.h", "src/mem.h", "src/mem.cpp", *touched],
            cwd=SUBMODULE,
            check=True,
            capture_output=True,
        ).stdout
        subprocess.run(["tar", "-x"], cwd=src, input=archive, check=True)
        subprocess.run(["git", "init", "-q"], cwd=src, check=True)
        for p in patches:
            subprocess.run(["git", "apply", str(p)], cwd=src, check=True)
        inl = (src / INL).read_text()
        cpp = root / "refs.cpp"
        code = HARNESS.replace("SECTIONS", "\n".join(cut(inl, a, b) for a, b in SECTIONS))
        cpp.write_text(code.replace("HOOKS", crash_hook(inl)))
        exe = root / "refs"
        cmd = ["g++", "-std=c++20", "-O1", "-pthread", "-w", "-o", str(exe), str(cpp)]
        if crash_log:
            (root / "log.h").write_text(STUB_LOG_H)
            (root / "log.cpp").write_text(STUB_LOG_CPP)
            cmd += ["-DWITH_CRASH_LOG", "-rdynamic", "-include", "cstdint", f"-I{root}", f"-I{src / 'src'}"]
            cmd += [str(src / "src" / "stackdump.cpp"), str(src / "src" / "mem.cpp"), str(root / "log.cpp"), "-ldl"]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return exe

    def run_scenario(self, exe, name):
        return subprocess.run([str(exe), name], capture_output=True, text=True, timeout=60, check=False)

    def test_scenarios_pass_with_the_fix(self):
        exe = self.build(ALL_PATCHES, "fixed")
        for name in ("freed_between", "released_during_call", "push_dying", "counts"):
            with self.subTest(name):
                res = self.run_scenario(exe, name)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertNotIn("FAIL", res.stdout)
                self.assertIn("ok ", res.stdout)

    def test_scenarios_fail_without_the_fix(self):
        exe = self.build(BEFORE_THE_FIX, "before")
        for name in ("freed_between", "released_during_call"):
            with self.subTest(name):
                res = self.run_scenario(exe, name)
                self.assertEqual(res.returncode, -signal.SIGSEGV, res.stdout + res.stderr)
        res = self.run_scenario(exe, "push_dying")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("FAIL its count stays 0", res.stdout)

    def test_the_crash_log_names_the_call(self):
        exe = self.build(ALL_PATCHES, "crash", crash_log=True)

        res = self.run_scenario(exe, "crash_with_object")
        self.assertEqual(res.returncode, -signal.SIGSEGV, res.stdout + res.stderr)
        object_at = re.search(r"^object (0x[0-9a-f]+)$", res.stdout, re.MULTILINE)
        self.assertIsNotNone(object_at, res.stdout)
        assert object_at is not None
        call = re.search(
            r"^crash: Ext\.UI call 0: (\S+) key 'Text' object (\S+) class 'StubButton' boxed 1 counted 1$",
            res.stdout,
            re.MULTILINE,
        )
        self.assertIsNotNone(call, res.stdout)
        assert call is not None
        self.assertIn("l_crash_with_object", call.group(1))
        self.assertEqual(call.group(2), object_at.group(1))
        self.assertIn("word at +8 2", res.stdout)  # the box's reference and the call's
        self.assertLess(res.stdout.index("Ext.UI call 0"), res.stdout.index("crash:   pc"))  # before what can fault

        res = self.run_scenario(exe, "crash_before_object")
        self.assertEqual(res.returncode, -signal.SIGSEGV, res.stdout + res.stderr)
        self.assertRegex(res.stdout, r"Ext\.UI call 0: \S*l_crash\S* key 'Text' object \(nil\) class '\?' boxed 0")

        res = self.run_scenario(exe, "crash_nested")
        self.assertEqual(res.returncode, -signal.SIGSEGV, res.stdout + res.stderr)
        self.assertRegex(res.stdout, r"Ext\.UI call 0: \S*l_crash_with_object\S* key 'Inner'")
        self.assertRegex(res.stdout, r"Ext\.UI call 1: \S*l_crash_nested\S* key 'Text'")

        res = self.run_scenario(exe, "crash_no_call")
        self.assertEqual(res.returncode, -signal.SIGSEGV, res.stdout + res.stderr)
        self.assertIn("crash: no Ext.UI call on this thread", res.stdout)

    def test_the_crash_log_gets_past_dead_memory(self):
        exe = self.build(ALL_PATCHES, "dead", crash_log=True)

        def log_of(name):
            res = self.run_scenario(exe, name)
            self.assertEqual(res.returncode, -signal.SIGSEGV, name + "\n" + res.stdout + res.stderr)
            self.assertIn("crash:   pc", res.stdout, name)
            self.assertIn("crash: register dump and stack scan done", res.stdout, name)
            return res.stdout

        log = log_of("dead_object")
        printed = re.search(r"^object (0x[0-9a-f]+)$", log, re.MULTILINE)
        self.assertIsNotNone(printed, log)
        assert printed is not None
        self.assertRegex(
            log, rf"call 0: \S*l_crash_after_refusal\S* key 'Text' object {printed.group(1)} class 'StubButton' "
        )
        self.assertRegex(log, r"boxed 1 counted 1\n")
        self.assertNotIn("its first word", log)

        log = log_of("dead_class")
        self.assertRegex(
            log, r"call 0: \S*l_crash_after_refusal\S* key 'Text' object \S+ class '\?' boxed 1 counted 1\n"
        )
        self.assertNotIn("its first word", log)

        self.assertRegex(log_of("note_unmapped"), r"crash: Ext\.UI call 0: note at 0x[0-9a-f]+ unreadable\n")
        self.assertIn("crash: Ext.UI call 0: note at 0x10 unreadable\n", log_of("note_low"))

        calls = re.findall(r"^crash: Ext\.UI call (\d+):", log_of("note_cycle"), re.MULTILINE)
        self.assertEqual(calls, ["0", "1", "2", "3"])

        self.assertRegex(log_of("note_junk"), r"crash: Ext\.UI call 0: \? key 'A{63}' object ")


if __name__ == "__main__":
    unittest.main()
