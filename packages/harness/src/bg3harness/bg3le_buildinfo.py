# pyright: basic
#!/usr/bin/env python3
"""Is build/bg3le/dist built with every patch in patches/bg3le? Stdlib only, read-only.

    python3 tools/bg3le_buildinfo.py [--dist DIR] [--patches DIR]

tools/build_bg3le.sh writes one "patch: <name> <sha256[:16]>" line per applied patch into dist/BUILDINFO. A dist
whose lines don't name every patches/bg3le/*.patch with the same hash was built before a patch was added or changed
(2026-10-13 audit UI-1: one machine's dist predated all nine crash patches and passed every check). Prints OK or
each difference plus the rebuild command; exits 0 when current, STALE_EXIT when not (or no BUILDINFO).

A downloaded prebuilt dist (bg3harness.bg3le_prebuilt: its BUILDINFO plus PREBUILT.json) passes the same check, and
fails it when a file no longer matches PREBUILT.json's sha256 or the package's full-hash patch list isn't patches/bg3le.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
DIST = REPO / "build" / "bg3le" / "dist"
PATCHES = REPO / "patches" / "bg3le"
REBUILD = "tools/build_bg3le.sh"   # from the repo root, with the game not running (the build refuses while bg3 runs)
FETCH = "tools/bg3le_prebuilt.py fetch"   # instead of building: the verified prebuilt (patches/bg3le-prebuilt.toml)
STALE_EXIT = 4


def built_patches(text: str) -> dict[str, str]:
    """BUILDINFO text -> {patch name: sha256[:16]}."""
    return {m.group(1): m.group(2) for m in re.finditer(r"^patch: (\S+) ([0-9a-f]+)\s*$", text, re.M)}


def repo_patches(patches: Path = PATCHES) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in sorted(patches.glob("*.patch"))}


def problems(dist: Path = DIST, patches: Path = PATCHES) -> list[str]:
    """What makes the dist stale; [] when it was built with exactly patches/bg3le."""
    try:
        text = (dist / "BUILDINFO").read_text()
    except OSError:
        return [f"no {dist / 'BUILDINFO'} (not built by {REBUILD})"]
    have, want = built_patches(text), repo_patches(patches)
    out = []
    for name, h in want.items():
        if name not in have:
            out.append(f"missing {name}")
        elif have[name] != h:
            out.append(f"changed {name} (built {have[name]}, now {h})")
    out += [f"built with {name}, which is not in {patches}" for name in have if name not in want]
    from bg3harness import bg3le_prebuilt  # it imports this module

    out += [f"prebuilt: {p}" for p in bg3le_prebuilt.verify(dist, patches) or []]
    m = re.search(r"^built: (\S+)", text, re.M)
    if out and m:
        out.append(f"dist built {m.group(1)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dist", type=Path, default=DIST)
    ap.add_argument("--patches", type=Path, default=PATCHES)
    a = ap.parse_args()
    bad = problems(a.dist, a.patches)
    if not bad:
        print(f"OK: {a.dist} has all {len(repo_patches(a.patches))} patches")
        return 0
    print(f"STALE bg3le build {a.dist}: " + "; ".join(bad))
    print(f"rebuild: cd {REPO} && {REBUILD} (or fetch the prebuilt: {FETCH})")
    return STALE_EXIT


if __name__ == "__main__":
    sys.exit(main())
