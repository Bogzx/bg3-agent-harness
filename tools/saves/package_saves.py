# pyright: basic
"""Package a suite's start saves for publishing: one zip per save, checked, plus their size and sha256 in saves.json.

    nice -n 15 uv run python -I tools/saves/package_saves.py [--suite bench-v0.8] [--story DIR] [--out DIR]

For each save saves.json lists for the suite (the Tav-*__SCN-* folders the scenarios start from, holdouts never
listed), read-only from the Story folder: the folder must hold one .lsv whose sha1 is saves.json's and its
thumbnail (.webp), nothing else. The .lsv is copied out and scanned with inspect.py (deep); a save with any finding
(a Steam ID, an e-mail, a user path) or a member it couldn't read is refused. The zip (entries `<folder>/<file>`,
sorted, fixed times and modes, so the same save gives the same bytes) is read back with bg3bench.saves.unpack and its
.lsv sha1 checked again before it is written to --out (default runtime/saves/<suite>/, gitignored) with
SHA256SUMS and index.json (bytes, sha256, the inspect counts). Only when every save passes are saves.json's bytes
and sha256 filled. Nothing in the Story folder is written, moved or deleted.

Run with -I: this folder's inspect.py shares its name with the stdlib module. Exit 0 all packaged, 1 refused.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from bg3bench import saves, suite
from bg3bench.submission import pack

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
STORY = (
    Path.home() / ".local/share/Larian Studios/Baldur's Gate 3/PlayerProfiles/Public/Savegames/Story"
)  # the Linux native install's; --story for another
SUFFIXES = (".lsv", ".webp")  # the save and its thumbnail
ZIP_TIME = (1980, 1, 1, 0, 0, 0)

Json = dict[str, Any]


class PackageError(Exception):
    pass


def _inspect_module():
    spec = importlib.util.spec_from_file_location("saves_inspect", HERE / "inspect.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_folder(folder: Path) -> dict[str, bytes]:
    """The save folder's files ({name: bytes}), read only; refuses links, subfolders and anything but one .lsv and
    its thumbnail."""
    if folder.is_symlink() or not folder.is_dir():
        raise PackageError(f"{folder.name}: not a folder in the Story folder")
    files: dict[str, bytes] = {}
    for p in sorted(folder.iterdir()):
        if p.is_symlink() or not stat.S_ISREG(p.lstat().st_mode):
            raise PackageError(f"{folder.name}: {p.name} is not a plain file")
        if not p.name.lower().endswith(SUFFIXES) or not saves.FILE_NAME.fullmatch(p.name):
            raise PackageError(f"{folder.name}: unexpected file {p.name}")
        files[p.name] = p.read_bytes()
    if sum(n.lower().endswith(".lsv") for n in files) != 1:
        raise PackageError(f"{folder.name}: not exactly one .lsv")
    return files


def zip_bytes(folder: str, files: Mapping[str, bytes]) -> bytes:
    """A deterministic zip of the save: `<folder>/<file>` entries, sorted, 1980-01-01, mode 644, deflated."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(f"{folder}/{name}", ZIP_TIME)
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[name])
    return out.getvalue()


def package(save: saves.Save, story: Path, scan: Callable[[Path], Json], tmp: Path) -> tuple[bytes, Json]:
    """One save's zip and its index entry; PackageError when it can't be published."""
    files = read_folder(story / save.name)
    lsv_name, lsv = next((n, d) for n, d in files.items() if n.lower().endswith(".lsv"))
    if hashlib.sha1(lsv).hexdigest() != save.save_sha1:
        raise PackageError(f"{save.name}: its .lsv sha1 is not saves.json's {save.save_sha1}")
    copy = tmp / f"{save.name}.lsv"
    copy.write_bytes(lsv)
    try:
        report = scan(copy)
    finally:
        copy.unlink()
    if report["findings"] or report["unread"]:
        kinds = sorted({h["kind"] for h in report["findings"]})
        raise PackageError(f"{save.name}: personal data or unread members: {kinds} {report['unread'][:3]}")
    data = zip_bytes(save.name, files)
    back = saves.unpack(data, save.name)
    if back != files or hashlib.sha1(back[lsv_name]).hexdigest() != save.save_sha1:
        raise PackageError(f"{save.name}: the zip doesn't read back")
    if len(data) > saves.MAX_ZIP_BYTES:
        raise PackageError(f"{save.name}: zip of {len(data)} bytes over {saves.MAX_ZIP_BYTES}")
    entry = {
        "zip": save.zip,
        "key": save.key,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "save_sha1": save.save_sha1,
        "inspect": {"findings": 0, "install_paths": len(report["install_paths"]), "members": len(report["members"])},
    }
    return data, entry


def fill(saves_json: Path, tag: str, index: Mapping[str, Json]) -> None:
    """Write each packaged save's bytes and sha256 into saves.json (the file's own layout kept)."""
    doc = json.loads(saves_json.read_text(encoding="utf-8"))
    for s in doc["suites"][tag]["saves"]:
        if s["name"] in index:
            s["bytes"], s["sha256"] = index[s["name"]]["bytes"], index[s["name"]]["sha256"]
    saves_json.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    saves.load(saves_json)  # still a valid saves file


def run(
    tag: str,
    story: Path,
    out: Path,
    saves_json: Path,
    scan: Callable[[Path], Json],
    log: Callable[[str], None],
    scenarios: Mapping[str, Json] | None = None,
) -> int:
    """Package every save saves_json lists for `tag`; scenarios: the suite's scenario files, checked against it."""
    index_all = saves.load(saves_json)
    if tag not in index_all:
        raise PackageError(f"{saves_json} lists no saves for {tag}")
    listed = index_all[tag]
    if scenarios is not None and (bad := saves.disagreements(listed, scenarios)):
        raise PackageError("saves.json and the scenarios disagree: " + "; ".join(bad))
    out.mkdir(parents=True, exist_ok=True)
    index: dict[str, Json] = {}
    refused: list[str] = []
    with tempfile.TemporaryDirectory(prefix="bg3-save-scan-") as tmp:
        for name, save in sorted(listed.items()):
            try:
                data, entry = package(save, story, scan, Path(tmp))
            except (PackageError, saves.SavesError) as e:
                refused.append(str(e))
                log(f"REFUSED {e}")
                continue
            (out / save.zip).write_bytes(data)
            index[name] = entry
            log(f"ok {save.zip} {entry['bytes']} bytes sha256 {entry['sha256'][:12]}")
    (out / "SHA256SUMS").write_text("".join(f"{e['sha256']}  {e['zip']}\n" for _, e in sorted(index.items())))
    (out / "index.json").write_text(json.dumps({"suite": tag, "saves": index}, indent=1, sort_keys=True) + "\n")
    if refused:
        log(f"{len(refused)} refused: saves.json not changed")
        return 1
    fill(saves_json, tag, index)
    log(f"{len(index)} saves packaged into {out}; {saves_json} filled")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--suite", default=suite.suite_version())
    ap.add_argument("--story", type=Path, default=STORY, help="the game's Story save folder (read only)")
    ap.add_argument("--out", type=Path, help="default runtime/saves/<suite>/")
    ap.add_argument("--saves-json", type=Path, default=saves.SAVES_JSON)
    a = ap.parse_args(argv)
    out = a.out or REPO / "runtime" / "saves" / a.suite
    if out.resolve().is_relative_to(a.story.resolve()):
        print("refused: --out is inside the Story folder", file=sys.stderr)
        return 1
    scanner = _inspect_module()
    try:
        scenarios = pack.load_scenarios() if a.suite == suite.suite_version() else None
        return run(a.suite, a.story, out, a.saves_json, lambda p: scanner.inspect_save(p), print, scenarios)
    except PackageError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
