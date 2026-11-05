"""legacy_map_node in patches/bg3le 0012 (src/vendor/component_meta.cpp), compiled offline.

Applies patches/bg3le to a scratch export of the pinned submodule commit, pulls legacy_map_node out of the
patched source, compiles it with g++ against a fake LegacyRefMap (plain memory, safe_read = memcpy), and replays
windows 8-9's shape: the map's one entry (key 214) is read, then replaced by another (key 215) at the same count;
the next read must give the new node, not the kept walk's freed one. Also: a pass over a two-entry map reads both.
Run: python3 -m unittest tools/test_bg3le_legacy_map_walk.py
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
SRC = "src/vendor/component_meta.cpp"

HARNESS = r"""
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

static bool safe_read(const void* a, void* out, std::size_t n) { std::memcpy(out, a, n); return true; }
static std::size_t safe_read_some(const void* a, void* out, std::size_t n) { std::memcpy(out, a, n); return n; }

struct Internals { std::uint32_t ItemCount; std::uint32_t HashSize; void** HashTable; };
template <class I> struct LegacyMapBase {
    using KeyType = int;
    using ValueType = void*;
    struct Node { Node* Next; int Key; void* Value; };
};
template <class M> struct LegacyInternalsOf;
template <class I> struct LegacyInternalsOf<LegacyMapBase<I>> { using type = I; };
using Map = LegacyMapBase<Internals>;
using Node = Map::Node;

FUNC

static int fails = 0;
static void check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "ok  " : "FAIL", what);
    if (!ok) ++fails;
}

int main() {
    Node a{nullptr, 214, nullptr}, b{nullptr, 215, nullptr}, c{nullptr, 7, nullptr};
    void* table[2] = {&a, nullptr};
    Internals m{1, 2, table};
    check(legacy_map_node<Map>(&m, 0) == &a, "one entry, key 214");
    // the dialog ends (its node is freed) and the next one takes its place: same map, same count
    table[0] = &b;
    Node* n = legacy_map_node<Map>(&m, 0);
    check(n == &b, "replaced at the same count: the new node (key 215), not the kept walk's");
    // a pass over two entries: entry 0 walks, entry 1 comes from that walk
    table[1] = &c;
    m.ItemCount = 2;
    Node* n0 = legacy_map_node<Map>(&m, 0);
    Node* n1 = legacy_map_node<Map>(&m, 1);
    check(n0 == &b && n1 == &c, "two entries in one pass");
    check(legacy_map_node<Map>(&m, 2) == nullptr, "past the count: none");
    return fails == 0 ? 0 : 1;
}
"""


def extract_legacy_map_node(src):
    m = re.search(r"^template <class M>\ntypename M::Node\* legacy_map_node\(.*?^}\n", src, re.DOTALL | re.MULTILINE)
    if not m:
        raise AssertionError(f"legacy_map_node not found in the patched {SRC}")
    return m.group(0)


def run_harness(func_src, tmp):
    cpp = Path(tmp) / "walk.cpp"
    cpp.write_text(HARNESS.replace("FUNC", func_src))
    exe = Path(tmp) / "walk"
    build = subprocess.run(
        ["g++", "-std=c++17", "-O1", "-o", str(exe), str(cpp)], capture_output=True, text=True, check=False
    )
    if build.returncode != 0:
        raise AssertionError(build.stderr)
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, check=False)


@unittest.skipUnless((SUBMODULE / ".git").exists() and shutil.which("g++"), "submodule or g++ missing")
class LegacyMapWalkTest(unittest.TestCase):
    def test_replaced_entry_at_same_count(self):
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
            res = run_harness(extract_legacy_map_node((src / SRC).read_text()), tmp)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertNotIn("FAIL", res.stdout)


if __name__ == "__main__":
    unittest.main()
