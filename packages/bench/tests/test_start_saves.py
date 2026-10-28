# pyright: basic, reportAttributeAccessIssue=false
"""bg3bench.saves: saves.json agrees with the scenarios; status, install and remove over fakes (no real Story
folder, no network)."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass, field

import pytest

from bg3bench import saves
from bg3bench.submission import pack

NAME = "Tav-123__SCN-test_start"
LSV = b"the save itself"


def make_zip(entries: dict[str, bytes], links: tuple[str, ...] = ()) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return out.getvalue()


GOOD_ZIP = make_zip({f"{NAME}/{NAME}.lsv": LSV, f"{NAME}/{NAME}.WebP": b"img"})


def entry(data: bytes = GOOD_ZIP, **over) -> saves.Save:
    d = {
        "name": NAME,
        "scenarios": ["test_start"],
        "save_sha1": hashlib.sha1(LSV).hexdigest(),
        "zip": f"{NAME}.zip",
        "key": f"saves/bench-v0.8/{NAME}.zip",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "fingerprint": None,
    } | over
    return saves.Save.from_json(d, "bench-v0.8")


@dataclass
class FakeStory:
    folders_: dict[str, dict[str, bytes]] = field(default_factory=dict)
    present: bool = True

    def exists(self) -> bool:
        return self.present

    def folders(self) -> set[str]:
        return set(self.folders_)

    def lsv_sha1(self, folder: str) -> str | None:
        lsv = [d for n, d in self.folders_[folder].items() if n.endswith(".lsv")]
        return hashlib.sha1(lsv[0]).hexdigest() if len(lsv) == 1 else None

    def write(self, folder: str, files) -> None:
        self.folders_[folder] = dict(files)

    def remove(self, folder: str) -> None:
        del self.folders_[folder]


@dataclass
class FakeRecord:
    installed: set[str] = field(default_factory=set)

    def names(self) -> set[str]:
        return set(self.installed)

    def add(self, name: str) -> None:
        self.installed.add(name)

    def discard(self, name: str) -> None:
        self.installed.discard(name)


@dataclass
class FakeStore:
    data: bytes
    asked: list[str] = field(default_factory=list)

    def get(self, key: str) -> bytes:
        self.asked.append(key)
        return self.data


def panel(s: saves.Save | None = None, story: FakeStory | None = None) -> saves.Saves:
    s = s or entry()
    return saves.Saves({s.name: s}, story or FakeStory(), FakeRecord())


def test_saves_json_agrees_with_the_scenarios():
    index = saves.load()
    scenarios = pack.load_scenarios()
    for tag, entries in index.items():
        assert saves.disagreements(entries, scenarios) == [], tag
    assert all(not scenarios[n].get("holdout") for e in index["bench-v0.8"].values() for n in e.scenarios)


def test_disagreements_found():
    scenarios = {
        "test_start": {"start": {"save": NAME, "save_sha1": "f" * 40}},
        "secret": {"holdout": True, "start": {"save": "Tav-9__SCN-secret", "save_sha1": "0" * 40}},
        "unlisted": {"start": {"save": "Tav-8__SCN-unlisted", "save_sha1": "0" * 40}},
    }
    hold = entry(name="Tav-9__SCN-secret", zip="Tav-9__SCN-secret.zip", key="saves/bench-v0.8/Tav-9__SCN-secret.zip")
    found = saves.disagreements({NAME: entry(), hold.name: hold}, scenarios)
    assert any("test_start: listed as" in f for f in found)
    assert any("secret: a holdout" in f for f in found)
    assert any("unlisted: its save" in f for f in found)


@pytest.mark.parametrize(
    "over",
    [{"name": "../x"}, {"zip": "other.zip"}, {"key": "saves/bench-v0.9/x.zip"}, {"bytes": saves.MAX_ZIP_BYTES + 1}],
)
def test_bad_entries_refused(over):
    with pytest.raises(saves.SavesError):
        entry(**over)


def test_status():
    lsv_sha1 = hashlib.sha1(LSV).hexdigest()
    p = panel()
    assert p.status(None, None)["status"] == "none"
    assert p.status("Tav-1__SCN-other", None)["status"] == "unknown"
    assert panel(story=FakeStory(present=False)).status(NAME, lsv_sha1)["status"] == "unknown"
    assert p.status(NAME, lsv_sha1)["status"] == "missing"
    p.story.write(NAME, {f"{NAME}.lsv": b"edited"})
    assert p.status(NAME, lsv_sha1)["status"] == "wrong_hash"
    p.story.write(NAME, {f"{NAME}.lsv": LSV})
    assert p.status(NAME, lsv_sha1)["status"] == "stray"
    p.record.add(NAME)
    st = p.status(NAME, lsv_sha1)
    assert st["status"] == "installed" and set(st) == {"status", "detail"}
    assert set(saves.STATUSES) == {"installed", "missing", "wrong_hash", "stray", "unknown", "none"}


def test_install_and_remove():
    p, store = panel(), FakeStore(GOOD_ZIP)
    p.install(NAME, store)
    assert store.asked == [f"saves/bench-v0.8/{NAME}.zip"]
    assert p.story.folders_[NAME] == {f"{NAME}.lsv": LSV, f"{NAME}.WebP": b"img"}
    assert p.status(NAME, None)["status"] == "installed"
    with pytest.raises(saves.SavesError, match="already in the Story folder"):
        p.install(NAME, store)
    p.remove(NAME)
    assert NAME not in p.story.folders() and p.record.names() == set()


def test_remove_refuses_what_it_did_not_install():
    p = panel()
    p.story.write(NAME, {f"{NAME}.lsv": LSV})  # a stray copy
    with pytest.raises(saves.SavesError, match="not installed by this panel"):
        p.remove(NAME)
    with pytest.raises(saves.SavesError, match="not a published save"):
        p.remove("Tav-1__SCN-other")
    assert NAME in p.story.folders()


BAD_ZIPS = {
    "parent path": make_zip({f"{NAME}/../evil.lsv": LSV}),
    "absolute": make_zip({f"/{NAME}/{NAME}.lsv": LSV}),
    "other folder": make_zip({f"{NAME}/{NAME}.lsv": LSV, "Tav-9__SCN-x/a.lsv": LSV}),
    "nested": make_zip({f"{NAME}/{NAME}.lsv": LSV, f"{NAME}/sub/x": b""}),
    "link": make_zip({f"{NAME}/{NAME}.lsv": LSV}, links=(f"{NAME}/link",)),
    "two lsv": make_zip({f"{NAME}/a.lsv": LSV, f"{NAME}/b.lsv": LSV}),
    "lsv sha1": make_zip({f"{NAME}/{NAME}.lsv": b"edited"}),
    "not a zip": b"not a zip",
}


@pytest.mark.parametrize("what", sorted(BAD_ZIPS))
def test_install_refuses_bad_zips(what):
    data = BAD_ZIPS[what]
    p = panel(entry(data))
    with pytest.raises(saves.SavesError):
        p.install(NAME, FakeStore(data))
    assert p.story.folders() == set() and p.record.names() == set()


def test_install_refuses_unpublished_and_tampered_downloads():
    with pytest.raises(saves.SavesError, match="not published yet"):
        panel(entry(sha256=None)).install(NAME, FakeStore(GOOD_ZIP))
    p = panel()
    with pytest.raises(saves.SavesError, match="size or sha256"):
        p.install(NAME, FakeStore(GOOD_ZIP[:-1] + b"x"))
    with pytest.raises(saves.SavesError, match="not a published save"):
        p.install("Tav-1__SCN-other", FakeStore(GOOD_ZIP))
    assert p.story.folders() == set()


def test_dir_story_store_and_record(tmp_path):
    story, store_root = tmp_path / "Story", tmp_path / "store"
    story.mkdir()
    (store_root / "saves" / "bench-v0.8").mkdir(parents=True)
    (store_root / "saves" / "bench-v0.8" / f"{NAME}.zip").write_bytes(GOOD_ZIP)
    (story / "Someone-1__Mine").mkdir()  # a save of the player's own: never touched
    p = saves.Saves({NAME: entry()}, saves.DirStory(story), saves.FileRecord(tmp_path / "cfg" / "installed.json"))
    p.install(NAME, saves.DirStore(store_root))
    assert (story / NAME / f"{NAME}.lsv").read_bytes() == LSV
    assert json.loads((tmp_path / "cfg" / "installed.json").read_text()) == {"installed": [NAME]}
    assert p.status(NAME, None)["status"] == "installed"
    (story / NAME / "sub").mkdir()
    with pytest.raises(saves.SavesError, match="more than plain files"):
        p.remove(NAME)
    (story / NAME / "sub").rmdir()
    p.remove(NAME)
    assert sorted(x.name for x in story.iterdir()) == ["Someone-1__Mine"]
    with pytest.raises(saves.SavesError, match="bad key"):
        saves.DirStore(store_root).get("../x")


def test_for_suite_without_a_story_folder(tmp_path):
    p = saves.for_suite(None, tmp_path / "installed.json")
    assert p.index and p.status(next(iter(p.index)), None)["status"] == "unknown"
