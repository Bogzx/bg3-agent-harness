"""A string pool candidate needs the guid, float and translated pools after it (patches/bg3le 0016). Compiled offline.

The bug (S5 launch 2026-11-06, 11 of 655 kept launches): find_string_pool took the array at -384 from the rarity run,
which held a dice string, instead of the real pool at +32. Applies patches/bg3le to a scratch export of the pinned
submodule commit, cuts find_string_pool, find_value_pools and build_from_run out of the patched src/vendor/stats.cpp
(real code), and compiles them with g++ against a fault-safe read and a fake RPGStats laid out as the logs show it.
Each scenario is its own run of the program. test_scenarios_fail_without_the_fix runs the series without 0016.
Run: python3 -m unittest tools/test_bg3le_stats_pool.py
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUBMODULE = REPO / "third_party" / "bg3le"
ALL_PATCHES = sorted((REPO / "patches" / "bg3le").glob("*.patch"))
BEFORE_THE_FIX = [p for p in ALL_PATCHES if p.name < "0016"]
SOURCE = "src/vendor/stats.cpp"

HARNESS = r"""
#include <cstdarg>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <sys/uio.h>
#include <unistd.h>
#include <vector>
// The scan walks every 4 bytes, so a misaligned candidate's Buffer is garbage: the read must survive it.
static bool safe_read(void const* a, void* o, std::size_t n) {
  iovec local{o, n}, remote{const_cast<void*>(a), n};
  return process_vm_readv(getpid(), &local, 1, &remote, 1, 0) == (ssize_t)n;
}
template <class T> bool read_as(void const* a, T* o) { return safe_read(a, o, sizeof(T)); }
static void logf(char const* fmt, ...) {
  va_list ap; va_start(ap, fmt); std::vprintf(fmt, ap); va_end(ap); std::putchar('\n');
}
static std::map<std::string, std::uint32_t> g_ids;
static std::map<std::uint32_t, std::string> g_texts;
extern "C" bool bg3le_fixed_string_index_of(char const* wanted, std::uint32_t* out) {
  auto at = g_ids.find(wanted);
  if (at == g_ids.end()) return false;
  *out = at->second;
  return true;
}
extern "C" char const* bg3le_fixed_string(std::uint32_t id, std::uint32_t*) {
  auto at = g_texts.find(id);
  return at == g_texts.end() ? nullptr : at->second.c_str();
}
HEADER
FOUND
Found& state() { static Found f; return f; }
// What build_from_run calls and this test isn't about.
static bool find_objects(unsigned long long, ArrayRef*, std::size_t*, void const**) { return true; }
static bool find_named_array(unsigned long long, char const*, std::size_t, std::size_t, std::size_t, ArrayRef*,
                             std::size_t*, char const*) { return true; }
static bool find_modifier_name_offset(ArrayRef const&, std::size_t*, std::size_t*) { return true; }
static bool find_object_offsets(Found const&, std::size_t*, std::size_t*) { return true; }
static void find_conditions(unsigned long long, Found*) {}
POOLS

constexpr std::size_t kWindowBytes = 16384;
struct Header { void const* Buffer; std::uint32_t Capacity, Size; };
static_assert(sizeof(Header) == 16, "layout");
alignas(16) static char g_stats[2 * kWindowBytes + 256];
static char* const g_run = g_stats + kWindowBytes;

constexpr std::uint32_t kDice = 700;      // small enough to be a NextIds value
constexpr std::uint32_t kHandle = 0x500010;
static std::vector<std::uint32_t> g_next(1512), g_object_next(15754), g_strings(21397), g_handles(64 * 4);
static std::vector<std::uint64_t> g_keys(1512), g_int64s(64), g_guids(64 * 2);
static std::vector<float> g_floats(64);

static void put(long at, void const* buffer, std::uint32_t size) {
  Header h{buffer, size, size};
  std::memcpy(g_run + at, &h, sizeof(h));
}

// The real block at +32, TreasureTables at -384, as the logs read them; `scenario` breaks one thing.
static void lay_out(std::string const& scenario) {
  g_ids["1d8"] = kDice;
  g_texts[kHandle] = "h5fafec24g30d5g425cg952cga9c53752059c";
  for (std::size_t i = 0; i < g_next.size(); ++i) g_next[i] = 0xffffffffu;
  for (auto& v : g_object_next) v = 0xffffffffu;
  for (auto& v : g_int64s) v = 1;
  for (auto& v : g_guids) v = 0x1234;
  for (std::size_t i = 0; i < g_floats.size(); ++i) g_floats[i] = 0.5f + (float)i;
  for (std::size_t i = 0; i < g_handles.size(); i += 4) g_handles[i] = kHandle;
  g_strings[9000] = kDice;
  if (scenario == "dice_in_object_next_ids") g_object_next[5000] = kDice;
  else if (scenario != "good") g_next[321] = kDice;
  put(-576, g_object_next.data(), (std::uint32_t)g_object_next.size());
  put(-384, g_next.data(), (std::uint32_t)g_next.size());
  put(-368, g_keys.data(), (std::uint32_t)g_keys.size());
  if (scenario != "no_block") put(32, g_strings.data(), (std::uint32_t)g_strings.size());
  put(48, g_int64s.data(), (std::uint32_t)g_int64s.size());
  put(64, g_guids.data(), 64);
  put(80, g_floats.data(), (std::uint32_t)g_floats.size());
  put(96, g_handles.data(), 64);
  if (scenario == "no_block") { std::memset(g_run + 64, 0, 3 * sizeof(Header)); }
  if (scenario == "zero_guids") { std::memset(g_guids.data(), 0, g_guids.size() * sizeof(g_guids[0])); }
  if (scenario == "no_floats") { std::memset(g_run + 80, 0, sizeof(Header)); }
  if (scenario == "zero_floats") { std::memset(g_floats.data(), 0, g_floats.size() * sizeof(g_floats[0])); }
  if (scenario == "no_handles") { std::memset(g_handles.data(), 0, g_handles.size() * sizeof(g_handles[0])); }
}

int main(int, char** argv) {
  lay_out(argv[1]);
  build_from_run((unsigned long long)(std::uintptr_t)g_run);
  Found const& f = state();
  long pool = f.StringsHeader ? (long)((char const*)f.StringsHeader - g_run) : 0;
  std::printf("RESULT pool=%ld size=%u guids=%u floats=%u translated=%u attributes=%d\n", pool, f.Strings.Size,
              f.Guids.Size, f.Floats.Size, f.TranslatedStrings.Size, (int)f.Attributes);
  return 0;
}
"""

FIXED = "RESULT pool=32 size=21397 guids=64 floats=64 translated=64 attributes=1"
REFUSED = "RESULT pool=0 size=0 guids=0 floats=0 translated=0 attributes=0"
WRONG_POOL = "RESULT pool=-384 size=1512 guids=0 floats=0 translated=0 attributes=1"  # the S5 log's picture
WRONG_POOL_OBJECTS = "RESULT pool=-576 size=15754 guids=0 floats=0 translated=0 attributes=1"


def function(src: str, signature: str) -> str:
    m = re.search(r"^" + signature + r"\([^;]*?\{\n.*?^}\n", src, re.S | re.M)  # [^;]: not a forward declaration
    if not m:
        raise AssertionError(f"{signature} not found in {SOURCE}")
    return m.group(0)


def cut(src: str, start: str, end: str) -> str:
    return src[src.index(start) : src.index(end, src.index(start))]


def program(src: str) -> str:
    """The patched source's pool code, in its own order, with what each series has of it."""
    moved = ["bool plausible_floats", "bool plausible_guids", "bool pool_block_holds"]
    have = [function(src, s) for s in moved if re.search(r"^" + s + r"\(.*\{$", src, re.M)]
    consts = "\n".join(re.findall(r"^constexpr std::size_t k(?:Guids|Floats|Translated)At = \d+;$", src, re.M))
    pools = [
        cut(src, "// Strings the value pool must contain", "// RPGStats::FixedStrings, the pool"),
        function(src, "bool find_string_pool"),
        function(src, "bool plausible_handles"),
        consts,
        *have,
        function(src, "void find_value_pools"),
        function(src, "bool build_from_run"),
    ]
    header = function(src, "bool array_header_at")
    found = re.search(r"^struct Found \{.*?^\};\n", src, re.S | re.M)
    assert found, "struct Found not found"
    array_ref = re.search(r"^struct ArrayRef \{.*?^\};\n", src, re.S | re.M)
    assert array_ref, "struct ArrayRef not found"
    return (
        HARNESS.replace("HEADER", array_ref.group(0) + header)
        .replace("FOUND", found.group(0))
        .replace("POOLS", "\n".join(pools))
    )


@unittest.skipUnless((SUBMODULE / ".git").exists() and shutil.which("g++"), "submodule or g++ missing")
class StatsPoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, patches, name):
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
            ["git", "archive", "HEAD", *touched], cwd=SUBMODULE, check=True, capture_output=True
        ).stdout
        subprocess.run(["tar", "-x"], cwd=src, input=archive, check=True)
        subprocess.run(["git", "init", "-q"], cwd=src, check=True)
        for p in patches:
            subprocess.run(["git", "apply", str(p)], cwd=src, check=True)
        cpp, exe = root / "pool.cpp", root / "pool"
        cpp.write_text(program((src / SOURCE).read_text()))
        c = subprocess.run(["g++", "-std=c++17", "-Wall", "-o", str(exe), str(cpp)], capture_output=True, text=True)
        self.assertEqual(c.returncode, 0, c.stderr)
        return exe

    def run_scenario(self, exe, scenario):
        r = subprocess.run([str(exe), scenario], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_the_real_pool_is_taken_past_a_false_match(self):
        exe = self.build(ALL_PATCHES, "all")
        self.assertIn(FIXED, self.run_scenario(exe, "good"))
        out = self.run_scenario(exe, "dice_in_next_ids")
        self.assertIn(FIXED, out)
        self.assertIn('(-384 from the run) rejected: 1512 entries, "1d8" (id 0x2bc) at entry 321', out)
        self.assertIn("string pool at", out)
        self.assertIn('its dice string: "1d8" (id 0x2bc) at entry 9000', out)
        self.assertIn("attributes available", out)
        out = self.run_scenario(exe, "dice_in_object_next_ids")  # bg3le-20261106-235702: the Objects map's NextIds
        self.assertIn(FIXED, out)
        self.assertIn('(-576 from the run) rejected: 15754 entries, "1d8" (id 0x2bc) at entry 5000', out)

    def test_a_block_with_any_pool_missing_is_refused(self):
        exe = self.build(ALL_PATCHES, "all")
        for scenario in ("no_block", "zero_guids", "no_floats", "zero_floats", "no_handles"):
            with self.subTest(scenario):
                out = self.run_scenario(exe, scenario)
                self.assertIn(REFUSED, out)
                self.assertIn("no string pool found", out)
                self.assertIn("attributes unavailable", out)

    def test_scenarios_fail_without_the_fix(self):
        exe = self.build(BEFORE_THE_FIX, "before")
        self.assertIn(FIXED, self.run_scenario(exe, "good"))  # the fake is the game's shape on a good launch
        out = self.run_scenario(exe, "dice_in_next_ids")
        self.assertIn(WRONG_POOL, out)
        self.assertIn(WRONG_POOL_OBJECTS, self.run_scenario(exe, "dice_in_object_next_ids"))
        self.assertIn("attributes available", out)


if __name__ == "__main__":
    unittest.main()
