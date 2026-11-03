# pyright: basic
"""The suite version (bench-vX.Y) names exactly the scenario and grading files it was pinned with (suite.json)."""

import json
import shutil

import pytest

import bg3bench
from bg3bench import suite


@pytest.fixture
def copy(tmp_path):
    """The suite's files and suite.json in a scratch package root."""
    for rel in suite.suite_files():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(suite.PACKAGE_ROOT / rel, tmp_path / rel)
    shutil.copyfile(suite.PINS, tmp_path / suite.PINS.name)
    return tmp_path


def test_the_current_suite_files_are_pinned_for_the_current_version():
    assert suite.check() == []
    assert suite.suite_version() == "bench-v" + ".".join(bg3bench.__version__.split(".")[:2])


def test_suite_version_of_a_bench_version():
    assert (suite.suite_version("0.1.0"), suite.suite_version("1.12.3")) == ("bench-v0.1", "bench-v1.12")


def test_a_changed_scenario_without_a_new_suite_version_fails(copy):
    assert suite.check(copy) == []
    path = copy / "scenarios" / "nautiloid.json"
    path.write_text(path.read_text(encoding="utf-8").replace("}", " }", 1), encoding="utf-8")
    (err,) = suite.check(copy)
    assert "not pinned for " + suite.suite_version() in err
    # the fix: a new suite version with the new digest (or, for a change no score sees, the digest appended)
    pins = suite.load_pins(copy / suite.PINS.name)
    assert suite.check(copy, {**pins, "bench-v9.9": [suite.digest(copy)]}, "bench-v9.9") == []


@pytest.mark.parametrize(
    "change",
    [
        lambda root: (root / "scenarios" / "new.json").write_text("{}"),  # a scenario added
        lambda root: (root / "scenarios" / "quizzes" / "nautiloid.json").unlink(),  # a quiz removed
        lambda root: (root / "src" / "bg3bench" / "graders.py").write_text("# another grader\n"),
        lambda root: (root / "src" / "bg3bench" / "conditions.py").write_text("# other conditions\n"),
    ],
)
def test_every_suite_file_is_in_the_digest(copy, change):
    before = suite.digest(copy)
    change(copy)
    assert suite.digest(copy) != before


def test_a_digest_pinned_for_two_versions_or_an_old_digest_fails(copy):
    pins = suite.load_pins(copy / suite.PINS.name)
    now = suite.digest(copy)
    errs = suite.check(copy, {**pins, "bench-v0.0": [now]})
    first = next(v for v, ds in pins.items() if now in ds)  # the version that pinned these files first
    assert errs == [f"digest {now} is pinned for both {first} and bench-v0.0"]
    # the files of an older version, run as a newer one: names the version they belong to
    errs = suite.check(copy, {"bench-v0.0": [now]}, "bench-v0.1")
    assert errs and "it is bench-v0.0's" in errs[0]


def test_pins_file_shape():
    data = json.loads(suite.PINS.read_text(encoding="utf-8"))
    assert all(v.startswith("bench-v") and ds and len(set(ds)) == len(ds) for v, ds in data["versions"].items())


def test_a_version_shares_its_predecessors_digest_only_when_suite_json_pairs_them(copy):
    """A version whose score moves outside the suite files (the mod its release pins) re-pins the files of the
    version before it; suite.json's `shared` names that pair, and an unnamed pair still fails."""
    now = suite.digest(copy)
    both = {"bench-v9.8": [now], "bench-v9.9": [now]}
    assert suite.check(copy, both, "bench-v9.9", {"bench-v9.9": "bench-v9.8"}) == []
    (err,) = suite.check(copy, both, "bench-v9.9", {"bench-v9.9": "bench-v0.1"})
    assert err == f"digest {now} is pinned for both bench-v9.8 and bench-v9.9"
    assert suite.check(copy, both, "bench-v9.9", {}) == [err]


def test_shared_names_pinned_versions_that_share_a_digest():
    pins, shared = suite.load_pins(), suite.load_shared()
    for new, old in shared.items():
        assert new != old and set(pins[new]) & set(pins[old]), (new, old)
