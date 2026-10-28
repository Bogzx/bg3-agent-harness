"""A prebuilt bg3le: package build/bg3le/dist with a manifest, or download one and verify it instead of building.
Stdlib only.

    python3 tools/bg3le_prebuilt.py pack [--out DIR]           # dist -> bg3le-prebuilt-<commit>-<patches>.tar.gz
    python3 tools/bg3le_prebuilt.py fetch [--url U --sha256 H] # download, verify, install into dist
    python3 tools/bg3le_prebuilt.py verify                     # is dist a verified prebuilt?

The package is a gzip'd tar of four regular files at its top level: libbg3le.so, bg3lua, BUILDINFO (as
tools/build_bg3le.sh wrote them) and PREBUILT.json, the manifest:

    {"format": "bg3le-prebuilt/1", "bg3le_commit": ..., "patches": [{"name", "sha256"}],
     "files": [{"name", "sha256", "bytes"}]}

`patches` is every patches/bg3le/*.patch the library was built with (full sha256), `files` the other three. pack
refuses a stale dist (bg3le_buildinfo.problems) and writes a reproducible tar (sorted, mtime 0, uid 0), so packing
the same dist twice gives the same sha256.

fetch takes the URL and the package's sha256 from patches/bg3le-prebuilt.toml (`url`, `sha256`; the environment's
BG3AI_BG3LE_PREBUILT_URL / BG3AI_BG3LE_PREBUILT_SHA256 win, and so do --url / --sha256). It refuses a package whose
sha256 differs, whose members aren't exactly those four regular files, whose files don't match the manifest, or
whose patch list isn't this checkout's patches/bg3le. Only then are the files moved into dist, with PREBUILT.json
recording where the package came from. The launcher's stale check (bg3le_buildinfo.problems) accepts such a dist,
and refuses it once a file no longer matches the manifest.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, cast

from bg3harness import bg3le_buildinfo

FORMAT = "bg3le-prebuilt/1"
MANIFEST = "PREBUILT.json"
FILES = ("libbg3le.so", "bg3lua", "BUILDINFO")
CONFIG = bg3le_buildinfo.REPO / "patches" / "bg3le-prebuilt.toml"
MAX_BYTES = 512 * 2**20  # the library is ~57 MB; a bigger download is not a bg3le package
SHA256 = re.compile(r"[0-9a-f]{64}")


class PrebuiltError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def patch_list(patches: Path) -> list[dict[str, str]]:
    return [{"name": p.name, "sha256": sha256_file(p)} for p in sorted(patches.glob("*.patch"))]


def manifest_for(dist: Path, patches: Path) -> dict[str, Any]:
    m = re.search(r"^bg3le commit: (\S+)", (dist / "BUILDINFO").read_text(), re.M)
    return {
        "format": FORMAT,
        "bg3le_commit": m.group(1) if m else None,
        "patches": patch_list(patches),
        "files": [{"name": n, "sha256": sha256_file(dist / n), "bytes": (dist / n).stat().st_size} for n in FILES],
    }


def package_name(manifest: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(manifest["patches"], sort_keys=True).encode()).hexdigest()[:12]
    return f"bg3le-prebuilt-{str(manifest['bg3le_commit'] or 'unknown')[:12]}-{digest}.tar.gz"


def _add(tar: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def pack(dist: Path, patches: Path, out_dir: Path) -> tuple[Path, str]:
    """dist -> <out_dir>/<package_name>; returns (path, its sha256). Refuses a stale or inconsistent dist."""
    bad = bg3le_buildinfo.problems(dist, patches)
    if bad:
        raise PrebuiltError(f"stale bg3le build {dist}: " + "; ".join(bad))
    missing = [n for n in FILES if not (dist / n).is_file()]
    if missing:
        raise PrebuiltError(f"{dist} has no {', '.join(missing)}")
    manifest = manifest_for(dist, patches)
    built = re.search(r"^sha256: ([0-9a-f]{64})", (dist / "BUILDINFO").read_text(), re.M)
    lib = manifest["files"][0]["sha256"]
    if built and built.group(1) != lib:
        raise PrebuiltError(f"{dist}/libbg3le.so (sha256 {lib}) is not the library BUILDINFO names ({built.group(1)})")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / package_name(manifest)
    buf = io.BytesIO()
    # gzip's own header carries a name and an mtime: fixed, so the same dist packs to the same bytes
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        _add(tar, MANIFEST, (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode(), 0o644)
        for n in FILES:
            _add(tar, n, (dist / n).read_bytes(), 0o644 if n == "BUILDINFO" else 0o755)
    path.write_bytes(buf.getvalue())
    return path, hashlib.sha256(buf.getvalue()).hexdigest()


def _entries(value: object) -> dict[str, dict[str, Any]]:
    """A manifest list of {"name", ...} objects -> {name: object}; anything malformed is left out."""
    out: dict[str, dict[str, Any]] = {}
    for item in cast(list[object], value) if isinstance(value, list) else []:
        if isinstance(item, dict):
            d = cast(dict[str, Any], item)
            if isinstance(d.get("name"), str):
                out[d["name"]] = d
    return out


def manifest_problems(manifest: object, files: Path, patches: Path) -> list[str]:
    """What makes the manifest not describe the files in `files` built with exactly `patches`; [] when it does."""
    m = cast(dict[str, Any], manifest) if isinstance(manifest, dict) else {}
    if m.get("format") != FORMAT:
        return [f"not a {FORMAT} manifest"]
    out: list[str] = []
    listed = _entries(m.get("files"))
    if set(listed) != set(FILES):
        out.append(f"manifest lists {sorted(listed)}, want {sorted(FILES)}")
    for name in FILES:
        entry, path = listed.get(name), files / name
        if entry is None:
            continue
        if not path.is_file():
            out.append(f"no {path}")
        elif path.stat().st_size != entry.get("bytes") or sha256_file(path) != entry.get("sha256"):
            out.append(f"{name} differs from the manifest (sha256 {sha256_file(path)[:16]})")
    want = {p["name"]: p["sha256"] for p in patch_list(patches)}
    have = {name: p.get("sha256") for name, p in _entries(m.get("patches")).items()}
    for name, h in want.items():
        if name not in have:
            out.append(f"package lacks {name}")
        elif have[name] != h:
            out.append(f"package built with another {name}")
    out += [f"package built with {name}, which is not in {patches}" for name in have if name not in want]
    return out


def verify(dist: Path, patches: Path) -> list[str] | None:
    """None when dist isn't a prebuilt (no PREBUILT.json); else what's wrong with it ([] = verified)."""
    try:
        text = (dist / MANIFEST).read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        return [f"can't read {dist / MANIFEST}: {e}"]
    try:
        manifest = json.loads(text)
    except ValueError:
        return [f"{dist / MANIFEST} is not JSON"]
    return manifest_problems(manifest, dist, patches)


def config(env: dict[str, str], path: Path = CONFIG) -> tuple[str, str]:
    """(url, sha256): the environment's BG3AI_BG3LE_PREBUILT_URL / _SHA256, else patches/bg3le-prebuilt.toml."""
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    url = env.get("BG3AI_BG3LE_PREBUILT_URL") or str(data.get("url") or "")
    sha = env.get("BG3AI_BG3LE_PREBUILT_SHA256") or str(data.get("sha256") or "")
    return url, sha


Opener = Callable[[str], IO[bytes]]


def _urlopen(url: str) -> IO[bytes]:
    return urllib.request.urlopen(url, timeout=60)  # the scheme is checked in download


def download(url: str, sha256: str, dest: Path, opener: Opener = _urlopen) -> None:
    """url -> dest, refused unless https:// (or file://) and the bytes' sha256 is `sha256`."""
    if not SHA256.fullmatch(sha256):
        raise PrebuiltError(f"the package sha256 must be 64 lowercase hex digits, got {sha256!r}")
    if not url.startswith(("https://", "file://")):
        raise PrebuiltError(f"refusing {url!r}: only https:// (or file://) URLs")
    h, n = hashlib.sha256(), 0
    with opener(url) as src, dest.open("wb") as out:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            n += len(chunk)
            if n > MAX_BYTES:
                raise PrebuiltError(f"{url} is bigger than {MAX_BYTES >> 20} MB: not a bg3le package")
            h.update(chunk)
            out.write(chunk)
    if h.hexdigest() != sha256:
        raise PrebuiltError(f"{url}: sha256 {h.hexdigest()}, want {sha256}")


def unpack(archive: Path, into: Path) -> None:
    """The package's four files into `into`; refuses any other member, a link or a path."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = sorted(m.name for m in members)
        if names != sorted((MANIFEST, *FILES)):
            raise PrebuiltError(f"package members {names}, want {sorted((MANIFEST, *FILES))}")
        for m in members:
            if not m.isreg():
                raise PrebuiltError(f"package member {m.name} is not a regular file")
            src = tar.extractfile(m)
            assert src is not None  # a regular file
            (into / m.name).write_bytes(src.read())
            os.chmod(into / m.name, 0o644 if m.name in (MANIFEST, "BUILDINFO") else 0o755)


def fetch(url: str, sha256: str, dist: Path, patches: Path, opener: Opener = _urlopen) -> dict[str, Any]:
    """Download, verify and install a package into dist; returns the installed manifest. Nothing in dist changes
    unless every check passed."""
    if not url:
        raise PrebuiltError(f"no prebuilt URL: set url/sha256 in {CONFIG} or BG3AI_BG3LE_PREBUILT_URL/_SHA256")
    dist.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bg3le-prebuilt-", dir=dist.parent) as tmp:
        stage = Path(tmp)
        download(url, sha256, stage / "package.tar.gz", opener)
        files = stage / "files"
        files.mkdir()
        unpack(stage / "package.tar.gz", files)
        try:
            manifest = json.loads((files / MANIFEST).read_text())
        except ValueError:
            raise PrebuiltError(f"{url}: {MANIFEST} is not JSON") from None
        bad = manifest_problems(manifest, files, patches)
        if bad:
            raise PrebuiltError(f"{url}: " + "; ".join(bad))
        manifest["archive"] = {"url": url, "sha256": sha256}
        (files / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
        dist.mkdir(parents=True, exist_ok=True)
        for name in (*FILES, MANIFEST):  # the manifest last: a dist is a prebuilt only once its files are in
            os.replace(files / name, dist / name)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dist", type=Path, default=bg3le_buildinfo.DIST)
    ap.add_argument("--patches", type=Path, default=bg3le_buildinfo.PATCHES)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack", help="package dist")
    p.add_argument("--out", type=Path, default=bg3le_buildinfo.REPO / "build" / "bg3le")
    f = sub.add_parser("fetch", help="download, verify and install a package into dist")
    f.add_argument("--url")
    f.add_argument("--sha256")
    sub.add_parser("verify", help="is dist a verified prebuilt?")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "pack":
            path, sha = pack(a.dist, a.patches, a.out)
            print(f"{path}\nsha256 {sha}")
        elif a.cmd == "fetch":
            url, sha = config(dict(os.environ))
            m = fetch(a.url or url, a.sha256 or sha, a.dist, a.patches)
            print(f"OK: {a.dist} = prebuilt bg3le {m.get('bg3le_commit')} with {len(m['patches'])} patches")
        else:
            bad = verify(a.dist, a.patches)
            if bad is None:
                print(f"{a.dist} is not a prebuilt (no {MANIFEST})")
                return 1
            if bad:
                print(f"NOT VERIFIED {a.dist}: " + "; ".join(bad))
                return 1
            print(f"OK: {a.dist} is a verified prebuilt")
    except (PrebuiltError, OSError, tarfile.TarError) as e:
        print(f"bg3le_prebuilt: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
