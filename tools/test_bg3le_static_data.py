"""The resource-manager matcher in patches/bg3le 0009 (src/vendor/static_data.cpp), compiled offline.

Pulls hash_chains_hold + read_definitions out of the patched source, compiles them with g++ against
stub safe_read/read_as (plain memcpy), and feeds them HashMaps laid out like bg3se's: the real shape,
and the false positive that crashed S060 09:11 (HashKeys pointing past Keys → `index < size_`).
Run: python3 -m unittest tools/test_bg3le_static_data.py
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "third_party" / "bg3le" / "src" / "vendor" / "static_data.cpp"

HARNESS = r'''
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
static bool safe_read(void const* a, void* o, std::size_t n) { if (!a) return false; std::memcpy(o, a, n); return true; }
template <class T> bool read_as(void const* a, T* o) { return safe_read(a, o, sizeof(T)); }
CONSTS
FUNCS
struct Map {  // bg3se HashMap<int32, void*>: HashKeys (StaticArray), NextIds, Keys (Array), Values
  void* hb; std::uint32_t hn, pad0;
  void* nb; std::uint32_t ncap, nn;
  void* kb; std::uint32_t kcap, kn;
  void* vb; std::uint32_t vn, pad1;
};
static_assert(sizeof(Map) == 64, "layout");
int main() {
  int fails = 0;
  auto check = [&](char const* what, bool got, bool want) {
    if (got != want) { std::printf("FAIL %s: got %d\n", what, got); ++fails; } else std::printf("ok %s\n", what);
  };
  // 10 keys, 4 buckets, chained: bucket b holds keys with i % 4 == b
  std::vector<std::int32_t> keys, next(10, -1), heads(4, -1);
  std::vector<void*> vals;
  for (int i = 0; i < 10; ++i) { keys.push_back(100 + i * 3); vals.push_back((void*)(std::uintptr_t)(0x1000 + i)); }
  for (int i = 9; i >= 0; --i) { next[i] = heads[i % 4]; heads[i % 4] = i; }
  auto make = [&](std::vector<std::int32_t>& h, std::vector<std::int32_t>& n, std::vector<std::int32_t>& k) {
    Map m{}; m.hb = h.data(); m.hn = (std::uint32_t)h.size(); m.nb = n.data(); m.nn = (std::uint32_t)n.size();
    m.kb = k.data(); m.kn = (std::uint32_t)k.size(); m.vb = vals.data(); m.vn = (std::uint32_t)vals.size(); return m;
  };
  { Map m = make(heads, next, keys); check("real map passes", hash_chains_hold((char const*)&m, m.kb, m.kn), true);
    std::vector<std::int32_t> k; std::vector<void*> b;
    check("read_definitions reads it", read_definitions(&m, &k, &b) && k == keys && b == vals, true); }
  { auto h = heads; h[2] = 10;  // the S060 shape: a head past Keys
    Map m = make(h, next, keys); check("head past Keys rejected", hash_chains_hold((char const*)&m, m.kb, m.kn), false); }
  { auto n = next; for (auto& v : n) if (v == -1) v = -2 - (&v - n.data());  // the live map: chain ends -2, -3, ...
    Map m = make(heads, n, keys); check("negative chain ends pass (live layout 2026-10-13)", hash_chains_hold((char const*)&m, m.kb, m.kn), true); }
  { auto n = next; n[9] = 1;   // cycle / shared tail
    Map m = make(heads, n, keys); check("chain cycle rejected", hash_chains_hold((char const*)&m, m.kb, m.kn), false); }
  { auto h = heads; h[3] = -1;  // keys unreachable
    Map m = make(h, next, keys); check("unreachable keys rejected", hash_chains_hold((char const*)&m, m.kb, m.kn), false); }
  { auto n = next; n.pop_back(); Map m = make(heads, n, keys); m.nn = 9;
    check("NextIds size mismatch rejected", hash_chains_hold((char const*)&m, m.kb, m.kn), false); }
  { auto k = keys; k[5] = k[1]; Map m = make(heads, next, k);
    check("duplicate key rejected", hash_chains_hold((char const*)&m, m.kb, m.kn), false); }
  { std::vector<std::int32_t> h; Map m = make(h, next, keys); m.hb = (void*)&m; m.hn = 0;
    check("no buckets rejected", hash_chains_hold((char const*)&m, m.kb, m.kn), false); }
  { Map m = make(heads, next, keys); m.kn = 401; std::vector<std::int32_t> k; std::vector<void*> b;
    check("implausible count refused", read_definitions(&m, &k, &b), false); }
  return fails;
}
'''


def extract(src, name):
    m = re.search(r"^bool " + name + r"\(.*?^}\n", src, re.S | re.M)
    if not m:
        raise AssertionError(f"{name} not found in {SRC}")
    return m.group(0)


@unittest.skipUnless(SRC.exists() and shutil.which("g++"), "bg3le source or g++ missing")
class StaticDataMatcherTest(unittest.TestCase):
    def test_matcher(self):
        src = SRC.read_text()
        if "hash_chains_hold" not in src:
            self.skipTest("patch 0009 not applied to the submodule (tools/build_bg3le.sh --patch)")
        consts = "\n".join(re.findall(r"^constexpr std::size_t k\w+ = \d+;$", src, re.M))
        code = (HARNESS.replace("CONSTS", consts)
                .replace("FUNCS", extract(src, "hash_chains_hold") + extract(src, "read_definitions")))
        with tempfile.TemporaryDirectory() as tmp:
            cpp, exe = Path(tmp) / "t.cpp", Path(tmp) / "t"
            cpp.write_text(code)
            c = subprocess.run(["g++", "-std=c++17", "-Wall", "-Werror", "-o", str(exe), str(cpp)], capture_output=True, text=True)
            self.assertEqual(c.returncode, 0, c.stderr)
            r = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ok head past Keys rejected", r.stdout)


if __name__ == "__main__":
    unittest.main()
