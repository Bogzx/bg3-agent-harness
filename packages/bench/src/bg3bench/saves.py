# pyright: basic
"""Start saves (saves.json): list a suite's saves, check them, install and remove them by explicit name.

saves.json holds, per suite version, every published start save: its folder name (`<Name>-<n>__SCN-<label>`), the
scenarios that start from it, the sha1 of its .lsv (the scenarios' `start.save_sha1`), the zip's name, storage key,
size and sha256 (null until the zip is published) and the start-state fingerprint (what the first facts snapshot
of an honest run shows; the validator's X01). Holdout scenarios' saves are never listed.

Installing downloads the zip through an injected Store, checks its size and sha256, checks every entry is a plain
file inside the one save folder (no `..`, no absolute paths, no links, a size cap), checks the .lsv's sha1, and only
then writes the folder into the Story folder. It never overwrites a folder that is there. Removing deletes only a
save this install put there (the install record) and only its plain files. A folder of a listed save that this
install did not put there is `stray`: Steam's local cloud restores moved bench saves into Story at every launch.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from bg3bench import suite

SCHEMA = 1
SAVES_JSON = suite.PACKAGE_ROOT / "saves.json"
SAVE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]{0,31}-\d{1,20}__SCN-[A-Za-z0-9_]{1,80}")
ZIP_NAME = re.compile(r"[A-Za-z0-9_.-]+\.zip")
FILE_NAME = re.compile(r"[A-Za-z0-9_. -]{1,120}")
MAX_ZIP_BYTES = 40 * 1024 * 1024
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
STATUSES = ("installed", "missing", "wrong_hash", "stray", "unknown", "none")

Json = dict[str, Any]


class SavesError(ValueError):
    pass


@dataclass(frozen=True)
class Save:
    name: str
    scenarios: tuple[str, ...]
    save_sha1: str
    zip: str
    key: str
    bytes: int | None
    sha256: str | None
    fingerprint: Json | None

    @classmethod
    def from_json(cls, d: Json, suite_tag: str) -> Save:
        s = cls(
            d["name"],
            tuple(d["scenarios"]),
            d["save_sha1"],
            d["zip"],
            d["key"],
            d.get("bytes"),
            d.get("sha256"),
            d.get("fingerprint"),
        )
        if not SAVE_NAME.fullmatch(s.name):
            raise SavesError(f"bad save name {s.name!r}")
        if not ZIP_NAME.fullmatch(s.zip) or s.zip != f"{s.name}.zip":
            raise SavesError(f"{s.name}: bad zip name {s.zip!r}")
        if s.key != f"saves/{suite_tag}/{s.zip}":
            raise SavesError(f"{s.name}: key {s.key!r} is not saves/{suite_tag}/{s.zip}")
        if not re.fullmatch(r"[0-9a-f]{40}", s.save_sha1):
            raise SavesError(f"{s.name}: bad save_sha1")
        if s.sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", s.sha256):
            raise SavesError(f"{s.name}: bad sha256")
        if s.bytes is not None and not 0 < s.bytes <= MAX_ZIP_BYTES:
            raise SavesError(f"{s.name}: size {s.bytes} outside (0, {MAX_ZIP_BYTES}]")
        return s


def load(path: Path = SAVES_JSON) -> dict[str, dict[str, Save]]:
    """{suite: {save name: Save}} from saves.json."""
    doc = json.loads(path.read_text())
    if doc.get("schema") != SCHEMA:
        raise SavesError(f"{path}: not a v{SCHEMA} saves file")
    return {tag: {s["name"]: Save.from_json(s, tag) for s in body["saves"]} for tag, body in doc["suites"].items()}


def disagreements(saves: Mapping[str, Save], scenarios: Mapping[str, Json]) -> list[str]:
    """Where a suite's saves and its scenario files disagree: every non-holdout scenario with a save is listed
    under that save with the same sha1, no holdout save is listed, no save names a scenario that doesn't start
    from it."""
    out = []
    listed = {sc: s for s in saves.values() for sc in s.scenarios}
    for name, sc in sorted(scenarios.items()):
        st = sc.get("start") or {}
        save = st.get("save")
        if sc.get("holdout"):
            if name in listed or any(s.name == save for s in saves.values()):
                out.append(f"{name}: a holdout scenario's save is listed")
            continue
        if not save:
            if name in listed:
                out.append(f"{name}: has no start save but is listed")
            continue
        s = listed.get(name)
        if s is None:
            out.append(f"{name}: its save {save} is not listed")
        elif s.name != save or s.save_sha1 != st.get("save_sha1"):
            out.append(f"{name}: listed as {s.name} {s.save_sha1[:8]}, the scenario says {save} {st.get('save_sha1')}")
    out += [f"{s.name}: names unknown scenario {n}" for s in saves.values() for n in s.scenarios if n not in scenarios]
    return out


class Store(Protocol):
    def get(self, key: str) -> bytes: ...


class Story(Protocol):
    """The game's Story save folder."""

    def exists(self) -> bool: ...
    def folders(self) -> set[str]: ...
    def lsv_sha1(self, folder: str) -> str | None: ...
    def write(self, folder: str, files: Mapping[str, bytes]) -> None: ...
    def remove(self, folder: str) -> None: ...


class Record(Protocol):
    """The saves this install put into Story (the panel's ~/.config/bg3bench/installed-saves.json)."""

    def names(self) -> set[str]: ...
    def add(self, name: str) -> None: ...
    def discard(self, name: str) -> None: ...


@dataclass(frozen=True)
class DirStore:
    """A store on disk (`<root>/<key>`): a local fake of the site's storage."""

    root: Path

    def get(self, key: str) -> bytes:
        if ".." in key.split("/") or key.startswith("/"):
            raise SavesError(f"bad key {key!r}")
        return (self.root / key).read_bytes()


@dataclass(frozen=True)
class DirStory:
    root: Path

    def exists(self) -> bool:
        return self.root.is_dir()

    def folders(self) -> set[str]:
        return {p.name for p in self.root.iterdir() if p.is_dir()} if self.root.is_dir() else set()

    def lsv_sha1(self, folder: str) -> str | None:
        found = [p for p in (self.root / folder).glob("*.lsv") if p.is_file()]
        return hashlib.sha1(found[0].read_bytes()).hexdigest() if len(found) == 1 else None

    def write(self, folder: str, files: Mapping[str, bytes]) -> None:
        d = self.root / _checked(folder)
        d.mkdir()
        for name, data in files.items():
            (d / name).write_bytes(data)

    def remove(self, folder: str) -> None:
        """Delete the folder's plain files, then the folder; refuses one holding anything else."""
        d = self.root / _checked(folder)
        entries = list(d.iterdir())
        if any(p.is_symlink() or not stat.S_ISREG(p.lstat().st_mode) for p in entries):
            raise SavesError(f"{folder} holds more than plain files: not removed")
        for p in entries:
            p.unlink()
        d.rmdir()


@dataclass(frozen=True)
class FileRecord:
    path: Path

    def names(self) -> set[str]:
        try:
            return set(json.loads(self.path.read_text()).get("installed") or [])
        except FileNotFoundError:
            return set()

    def _write(self, names: Iterable[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"installed": sorted(names)}, indent=1) + "\n")

    def add(self, name: str) -> None:
        self._write(self.names() | {name})

    def discard(self, name: str) -> None:
        self._write(self.names() - {name})


def _checked(folder: str) -> str:
    if not SAVE_NAME.fullmatch(folder):
        raise SavesError(f"bad save name {folder!r}")
    return folder


def unpack(data: bytes, folder: str) -> dict[str, bytes]:
    """A save zip's files ({file name: bytes}) after checking it holds only plain files inside `folder`/."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise SavesError(f"{folder}: not a zip ({e})") from None
    files: dict[str, bytes] = {}
    total = 0
    with zf:
        for info in zf.infolist():
            if info.is_dir() and info.filename == f"{folder}/":
                continue
            top, _, name = info.filename.partition("/")
            mode = info.external_attr >> 16
            if top != folder or not FILE_NAME.fullmatch(name) or name in (".", "..") or info.is_dir():
                raise SavesError(f"{folder}: zip entry {info.filename!r} is not a file inside {folder}/")
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG):  # 0: the zip records permissions only
                raise SavesError(f"{folder}: zip entry {info.filename!r} is not a plain file")
            total += info.file_size
            if total > MAX_UNZIPPED_BYTES:
                raise SavesError(f"{folder}: unzips to over {MAX_UNZIPPED_BYTES} bytes")
            files[name] = zf.read(info)
    if sum(n.endswith(".lsv") for n in files) != 1:
        raise SavesError(f"{folder}: the zip holds {sum(n.endswith('.lsv') for n in files)} .lsv files, not 1")
    return files


@dataclass(frozen=True)
class Saves:
    """One suite's saves over a Story folder and this install's record."""

    index: Mapping[str, Save]
    story: Story
    record: Record

    def status(self, save: str | None, sha1: str | None) -> dict[str, Any]:
        """{"status": installed|missing|wrong_hash|stray|unknown|none, "detail"} of a scenario's start save."""
        if not save:
            return {"status": "none", "detail": "this scenario starts without a save"}
        if save not in self.index:
            return {"status": "unknown", "detail": f"{save} is not a published save of this suite"}
        if not self.story.exists():
            return {"status": "unknown", "detail": "no Story folder"}
        if save not in self.story.folders():
            return {"status": "missing", "detail": "not in the Story folder"}
        want = sha1 or self.index[save].save_sha1
        got = self.story.lsv_sha1(save)
        if got != want:
            return {"status": "wrong_hash", "detail": f"its .lsv sha1 is {got}, the scenario's {want}"}
        if save not in self.record.names():
            return {"status": "stray", "detail": "a copy this panel did not install (Steam's cloud restores them)"}
        return {"status": "installed", "detail": "sha1 checked"}

    def install(self, name: str, store: Store) -> None:
        """Download, verify and write one save into Story; refuses a folder that is already there."""
        s = self.index.get(name)
        if s is None:
            raise SavesError(f"{name} is not a published save of this suite")
        if s.sha256 is None or s.bytes is None:
            raise SavesError(f"{name}: its zip is not published yet")
        if not self.story.exists():
            raise SavesError("no Story folder")
        if name in self.story.folders():
            raise SavesError(f"{name} is already in the Story folder: not overwritten")
        data = store.get(s.key)
        if len(data) != s.bytes or hashlib.sha256(data).hexdigest() != s.sha256:
            raise SavesError(f"{name}: the download's size or sha256 differs from saves.json")
        files = unpack(data, name)
        lsv = next(d for n, d in files.items() if n.endswith(".lsv"))
        if hashlib.sha1(lsv).hexdigest() != s.save_sha1:
            raise SavesError(f"{name}: the .lsv's sha1 differs from saves.json")
        self.story.write(name, files)
        self.record.add(name)

    def remove(self, name: str) -> None:
        """Delete a save this install put into Story, by its exact name; refuses any other."""
        if name not in self.index:
            raise SavesError(f"{name} is not a published save of this suite")
        if name not in self.record.names():
            raise SavesError(f"{name} was not installed by this panel: not removed")
        if name in self.story.folders():
            self.story.remove(name)
        self.record.discard(name)


def for_suite(story_dir: Path | None, record_path: Path, tag: str | None = None, path: Path = SAVES_JSON) -> Saves:
    """The installed suite's saves (default: this bg3bench's suite version) over a Story folder on disk."""
    index = load(path).get(tag or suite.suite_version(), {})
    return Saves(index, DirStory(story_dir or Path("/nonexistent")), FileRecord(record_path))
