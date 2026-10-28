"""bg3harness.bg3le_prebuilt on fake dists and packages in a temp dir: pack, fetch (file:// URLs or a stub opener; no
network), the refusals, and runner/launch_game.sh's stale check (--dry-run, fake game and sniper) on a fetched dist."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import IO

import pytest
from bg3harness import bg3le_buildinfo as bi
from bg3harness import bg3le_prebuilt as pb

LIB = b"\x7fELF fake libbg3le"


def make_patches(root: Path, names: tuple[str, ...] = ("0001-a.patch", "0002-b.patch")) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / n).write_text(f"diff for {n}\n")
    return root


def make_dist(root: Path, patches: Path, lib: bytes = LIB) -> Path:
    """A dist as tools/build_bg3le.sh leaves it for `patches`."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "libbg3le.so").write_bytes(lib)
    (root / "bg3lua").write_text("#!/usr/bin/env python3\n")
    lines = ["bg3le commit: 3728a68ef4a98fe0d25e3dac9b7d1f89d96f40ec"]
    lines += [f"patch: {p.name} {hashlib.sha256(p.read_bytes()).hexdigest()[:16]}" for p in sorted(patches.glob("*"))]
    lines += ["built: 2026-10-28T18:00:00+02:00", f"sha256: {hashlib.sha256(lib).hexdigest()}"]
    (root / "BUILDINFO").write_text("\n".join(lines) + "\n")
    return root


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    patches = make_patches(tmp_path / "patches")
    return make_dist(tmp_path / "built", patches), patches


def packed(tmp_path: Path, tree: tuple[Path, Path]) -> tuple[str, str]:
    path, sha = pb.pack(*tree, tmp_path / "out")
    return path.as_uri(), sha


def test_pack_is_reproducible_and_names_commit_and_patches(tmp_path: Path, tree: tuple[Path, Path]):
    a, sha_a = pb.pack(*tree, tmp_path / "a")
    b, sha_b = pb.pack(*tree, tmp_path / "b")
    assert sha_a == sha_b == hashlib.sha256(a.read_bytes()).hexdigest()
    assert a.name == b.name and a.name.startswith("bg3le-prebuilt-3728a68ef4a9-")
    with tarfile.open(a) as tar:
        assert sorted(tar.getnames()) == sorted(("PREBUILT.json", "libbg3le.so", "bg3lua", "BUILDINFO"))
        f = tar.extractfile("PREBUILT.json")
        assert f is not None
        m = json.loads(f.read())
    assert m["format"] == pb.FORMAT
    assert [p["name"] for p in m["patches"]] == ["0001-a.patch", "0002-b.patch"]
    assert m["files"][0] == {"name": "libbg3le.so", "sha256": hashlib.sha256(LIB).hexdigest(), "bytes": len(LIB)}


def test_pack_refuses_a_stale_dist(tmp_path: Path, tree: tuple[Path, Path]):
    dist, patches = tree
    (patches / "0003-c.patch").write_text("new\n")
    with pytest.raises(pb.PrebuiltError, match=r"missing 0003-c\.patch"):
        pb.pack(dist, patches, tmp_path / "out")


def test_pack_refuses_a_library_that_isnt_the_built_one(tmp_path: Path, tree: tuple[Path, Path]):
    dist, patches = tree
    (dist / "libbg3le.so").write_bytes(b"other")
    with pytest.raises(pb.PrebuiltError, match="is not the library BUILDINFO names"):
        pb.pack(dist, patches, tmp_path / "out")


def test_fetch_installs_a_dist_the_stale_check_accepts(tmp_path: Path, tree: tuple[Path, Path]):
    url, sha = packed(tmp_path, tree)
    dist = tmp_path / "repo" / "build" / "bg3le" / "dist"
    m = pb.fetch(url, sha, dist, tree[1])
    assert m["archive"] == {"url": url, "sha256": sha}
    assert (dist / "libbg3le.so").read_bytes() == LIB
    assert os.access(dist / "libbg3le.so", os.X_OK)
    assert pb.verify(dist, tree[1]) == []
    assert bi.problems(dist, tree[1]) == []
    assert sorted(p.name for p in dist.parent.iterdir()) == ["dist"]  # the staging dir is gone


def test_a_tampered_prebuilt_is_stale(tmp_path: Path, tree: tuple[Path, Path]):
    dist = tmp_path / "dist"
    pb.fetch(*packed(tmp_path, tree), dist, tree[1])
    (dist / "libbg3le.so").write_bytes(LIB + b"x")
    bad = bi.problems(dist, tree[1])
    assert any(b.startswith("prebuilt: libbg3le.so differs from the manifest") for b in bad), bad


def test_a_built_dist_is_not_a_prebuilt(tree: tuple[Path, Path]):
    assert pb.verify(*tree) is None
    assert bi.problems(*tree) == []


def test_wrong_sha256_changes_nothing(tmp_path: Path, tree: tuple[Path, Path]):
    url, sha = packed(tmp_path, tree)
    dist = make_dist(tmp_path / "dist", tree[1], lib=b"old")
    with pytest.raises(pb.PrebuiltError, match="sha256"):
        pb.fetch(url, "0" * 64, dist, tree[1])
    assert (dist / "libbg3le.so").read_bytes() == b"old"
    assert not (dist / "PREBUILT.json").exists()
    assert sha != "0" * 64


def test_a_package_for_other_patches_is_refused(tmp_path: Path, tree: tuple[Path, Path]):
    url, sha = packed(tmp_path, tree)
    (tree[1] / "0002-b.patch").write_text("changed\n")
    with pytest.raises(pb.PrebuiltError, match=r"package built with another 0002-b\.patch"):
        pb.fetch(url, sha, tmp_path / "dist", tree[1])
    assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize(
    ("url", "sha", "match"),
    [
        ("http://example.invalid/p.tar.gz", "a" * 64, "only https://"),
        ("https://example.invalid/p.tar.gz", "A" * 64, "64 lowercase hex"),
        ("", "a" * 64, "no prebuilt URL"),
    ],
)
def test_bad_url_or_hash_refused_before_any_download(tmp_path: Path, url: str, sha: str, match: str):
    def opener(u: str) -> IO[bytes]:
        raise AssertionError(f"downloaded {u}")

    with pytest.raises(pb.PrebuiltError, match=match):
        pb.fetch(url, sha, tmp_path / "dist", make_patches(tmp_path / "patches"), opener)


def test_https_goes_through_the_opener(tmp_path: Path, tree: tuple[Path, Path]):
    path, sha = pb.pack(*tree, tmp_path / "out")
    seen: list[str] = []

    def opener(u: str) -> IO[bytes]:
        seen.append(u)
        return io.BytesIO(path.read_bytes())

    pb.fetch("https://example.invalid/pkg.tar.gz", sha, tmp_path / "dist", tree[1], opener)
    assert seen == ["https://example.invalid/pkg.tar.gz"]


def evil(tmp_path: Path, tree: tuple[Path, Path], extra: tarfile.TarInfo, data: bytes = b"") -> tuple[str, str]:
    """The real package's members plus `extra`."""
    good, _ = pb.pack(*tree, tmp_path / "good")
    out = tmp_path / "evil.tar.gz"
    with tarfile.open(good) as src, tarfile.open(out, "w:gz") as tar:
        for m in src.getmembers():
            if m.name != extra.name:
                tar.addfile(m, src.extractfile(m))
        tar.addfile(extra, io.BytesIO(data) if extra.isreg() else None)
    return out.as_uri(), hashlib.sha256(out.read_bytes()).hexdigest()


def test_an_extra_member_is_refused(tmp_path: Path, tree: tuple[Path, Path]):
    info = tarfile.TarInfo("../escape")
    info.size = 1
    with pytest.raises(pb.PrebuiltError, match="package members"):
        pb.fetch(*evil(tmp_path, tree, info, b"x"), tmp_path / "dist", tree[1])
    assert not (tmp_path / "escape").exists()


def test_a_link_member_is_refused(tmp_path: Path, tree: tuple[Path, Path]):
    info = tarfile.TarInfo("bg3lua")
    info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
    with pytest.raises(pb.PrebuiltError, match="bg3lua is not a regular file"):
        pb.fetch(*evil(tmp_path, tree, info), tmp_path / "dist", tree[1])


def test_config_reads_the_toml_and_the_env_wins(tmp_path: Path):
    cfg = tmp_path / "prebuilt.toml"
    cfg.write_text('url = "https://example.invalid/a.tar.gz"\nsha256 = "' + "b" * 64 + '"\n')
    assert pb.config({}, cfg) == ("https://example.invalid/a.tar.gz", "b" * 64)
    assert pb.config({"BG3AI_BG3LE_PREBUILT_URL": "https://x.invalid/b"}, cfg) == ("https://x.invalid/b", "b" * 64)
    assert pb.config({}, tmp_path / "none.toml") == ("", "")


def test_the_checkout_config_parses():
    url, sha = pb.config({})
    assert url == "" or url.startswith("https://")
    assert sha == "" or pb.SHA256.fullmatch(sha)


def test_cli_pack_then_fetch_then_verify(tmp_path: Path, tree: tuple[Path, Path], capsys: pytest.CaptureFixture[str]):
    dist, patches = tree
    assert pb.main(["--dist", str(dist), "--patches", str(patches), "pack", "--out", str(tmp_path / "out")]) == 0
    path, label, sha = capsys.readouterr().out.split()
    assert label == "sha256" and sha == hashlib.sha256(Path(path).read_bytes()).hexdigest()
    target = ["--dist", str(tmp_path / "dist"), "--patches", str(patches)]
    assert pb.main([*target, "verify"]) == 1
    assert pb.main([*target, "fetch", "--url", Path(path).as_uri(), "--sha256", sha]) == 0
    assert pb.main([*target, "verify"]) == 0
    assert pb.main([*target, "fetch", "--url", Path(path).as_uri(), "--sha256", "c" * 64]) == 1


def test_a_local_build_drops_the_prebuilt_manifest():
    # tools/build_bg3le.sh overwrites the dist's files; a PREBUILT.json left behind would make it stale
    sh = (bi.REPO / "tools" / "build_bg3le.sh").read_text()
    assert 'rm -f "$OUT/dist/PREBUILT.json"' in sh
    assert sh.index('rm -f "$OUT/dist/PREBUILT.json"') < sh.index('install -m 0755 "$LIB"')


class TestLaunchStaleCheck:
    """runner/launch_game.sh --dry-run (fake game and sniper, the checkout's patches/bg3le): nothing launches."""

    def launch(self, tmp_path: Path, dist: Path) -> subprocess.CompletedProcess[str]:
        game, sniper = tmp_path / "game", tmp_path / "sniper"
        (game / "bin").mkdir(parents=True)
        sniper.mkdir()
        for exe in (game / "bin" / "bg3", sniper / "run"):
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
        env = {**os.environ, "BG3_DIR": str(game), "BG3AI_SNIPER_DIR": str(sniper),
               "BG3AI_BG3LE_LIB": str(dist / "libbg3le.so"), "BG3AI_RUNTIME": str(tmp_path / "runtime"),
               "BG3AI_GAME_SCOPE": "0", "BG3LE_DEBUG_PORT": "59996"}  # fmt: skip
        env.pop("BG3AI_ALLOW_STALE_BG3LE", None)
        return subprocess.run(["bash", str(bi.REPO / "runner" / "launch_game.sh"), "--dry-run", "--no-input-shim"],
                              capture_output=True, text=True, env=env, timeout=60, check=False)  # fmt: skip

    def fetched(self, tmp_path: Path) -> Path:
        built = make_dist(tmp_path / "built", bi.PATCHES)
        path, sha = pb.pack(built, bi.PATCHES, tmp_path / "out")
        dist = tmp_path / "dist"
        pb.fetch(path.as_uri(), sha, dist, bi.PATCHES)
        return dist

    def test_a_verified_prebuilt_launches(self, tmp_path: Path):
        r = self.launch(tmp_path, self.fetched(tmp_path))
        assert r.returncode == 0, r.stderr
        assert "STALE" not in r.stderr
        assert "./bin/bg3" in r.stdout

    def test_a_tampered_prebuilt_is_refused(self, tmp_path: Path):
        dist = self.fetched(tmp_path)
        (dist / "libbg3le.so").write_bytes(b"patched by hand")
        r = self.launch(tmp_path, dist)
        assert r.returncode == bi.STALE_EXIT, r.stderr
        assert "REFUSING TO LAUNCH" in r.stderr
        assert "prebuilt: libbg3le.so differs from the manifest" in r.stderr
