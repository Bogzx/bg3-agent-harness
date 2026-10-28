# pyright: basic
"""bg3bench.submission.release: the suite's release file, built offline from a server dump, against the hashes a
real run recorded (the committed pilot fixture), its schema, and its refusals."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from bg3bench import saves, suite
from bg3bench.submission import pack, release, validate

FIXTURE = Path(__file__).parent / "fixtures" / "submission" / "fight_act2_balthazar.bundle.tar.gz"
PINS = {"game_build": "20981537", "bg3le_sha256": "a" * 64, "mod_sha1": "b" * 40}
VERSIONS = {"harness": "1.2.3", "bench": "0.8.0"}


def fixture_files() -> dict[str, bytes]:
    with tarfile.open(FIXTURE) as tar:
        return {m.name: (tar.extractfile(m) or b"").read() for m in tar.getmembers()}  # type: ignore[union-attr]


def fake_dump(files: dict[str, bytes], tools: list[dict] | None = None):
    """The server dump a pilot run recorded (bg3-tools.json, bg3-instructions.txt, pi-system.txt)."""

    def dump(system: Path, tools_path: Path, instructions: Path, repo: Path) -> None:
        system.write_bytes(files["pi-system.txt"])
        tools_path.write_text(json.dumps(tools) if tools is not None else files["bg3-tools.json"].decode())
        instructions.write_bytes(files["bg3-instructions.txt"])

    return dump


@pytest.fixture(scope="module")
def served() -> release.Served:
    return release.serve("rules", ["pi", "claude"], Path("/nonexistent"), environ={}, dump=fake_dump(fixture_files()))


def test_served_matches_what_the_pilot_run_recorded(served):
    man = json.loads(fixture_files()["manifest.json"])
    assert man["client"]["name"] == "pi" and man["condition"] == "rules"
    doc = release.build(
        "bench-v0.8", VERSIONS, PINS, pack.load_scenarios(), saves.load()["bench-v0.8"], {"rules": served}
    )
    cond = doc["conditions"]["rules"]
    assert cond["tools"]["allowlist_sha256"] == man["tools"]["allowlist_sha256"]
    assert cond["tools"]["count"] == man["tools"]["count"] == len(cond["tools"]["names"])
    assert cond["prompts"]["pi"] == {"sha256": man["prompt"]["sha256"], "condition": "rules"}
    assert cond["prompts"]["claude"]["condition"] == "rules+coaching"  # claude is served play/CLAUDE.md
    assert not {"load_game", "wiki_search", "memory_read"} & set(cond["tools"]["names"])
    assert release.errors(doc) == []


def test_session_env_turns_off_memory_and_the_conditions_tools():
    env = release.session_env("rules", {"BG3AI_TOOLS": "x", "PATH": "/bin"})
    assert env == {
        "PATH": "/bin",
        "BG3AI_PROMPT_CONDITION": "rules",
        "BG3AI_TOOLS_OFF": "memory,wiki_search,wiki_page,load_game",
    }
    env = release.session_env("rules+coaching@edaa24f8+wiki+reloads:2", {})
    assert env == {"BG3AI_PROMPT_CONDITION": "rules+coaching", "BG3AI_TOOLS_OFF": "memory"}


def test_release_document(served):
    scenarios = pack.load_scenarios()
    index = saves.load()["bench-v0.8"]
    doc = release.build("bench-v0.8", VERSIONS, PINS, scenarios, index, {"rules": served})
    assert doc["schema"] == 1 and doc["kind"] == release.KIND and doc["about"]
    assert not {n for n, sc in scenarios.items() if sc.get("holdout")} & set(doc["scenarios"])
    fight = doc["scenarios"]["fight_act2_balthazar"]
    assert fight["save_sha1"] == scenarios["fight_act2_balthazar"]["start"]["save_sha1"]
    assert fight["fingerprint"] == index[fight["save"]].fingerprint
    assert len(doc["saves"]) == len(index) and doc["saves"][0]["key"].startswith("saves/bench-v0.8/")
    assert json.loads(json.dumps(doc)) == doc
    r = validate.Release.from_json(doc)
    assert r.pins == PINS and r.conditions["rules"]["prompts"]["pi"]["sha256"] == served.prompts["pi"]["sha256"]
    assert "fight_act2_balthazar" not in release.unfingerprinted(doc)


def test_release_file_shape_is_enforced(served):
    doc = release.build(
        "bench-v0.8", VERSIONS, PINS, pack.load_scenarios(), saves.load()["bench-v0.8"], {"rules": served}
    )
    for broken in ({**doc, "schema": 2}, {k: v for k, v in doc.items() if k != "conditions"}, {**doc, "pins": {}}):
        assert release.errors(broken)
        with pytest.raises(ValueError, match="release.v1"):
            validate.Release.from_json(broken)


def test_build_refuses_missing_pins_and_disagreeing_saves(served):
    scenarios, index = pack.load_scenarios(), saves.load()["bench-v0.8"]
    with pytest.raises(release.ReleaseError, match="missing pins: mod_sha1"):
        release.build("bench-v0.8", VERSIONS, {**PINS, "mod_sha1": None}, scenarios, index, {"rules": served})
    name = next(iter(index))
    wrong = {**index, name: saves.Save(**{**index[name].__dict__, "save_sha1": "0" * 40})}
    with pytest.raises(release.ReleaseError, match="disagree"):
        release.build("bench-v0.8", VERSIONS, PINS, scenarios, wrong, {"rules": served})


def test_reference_runs_with_different_pins_are_refused():
    man = {"run_id": "r1", "pins": {"game_build": "1", "bg3le": {"sha256": "a" * 64}, "mod": {"sha1": "b" * 40}}}
    same = {**man, "run_id": "r2"}
    assert release.common_pins([man, same]) == {"game_build": "1", "bg3le_sha256": "a" * 64, "mod_sha1": "b" * 40}
    other = {**man, "run_id": "r3", "pins": {**man["pins"], "game_build": "2"}}
    with pytest.raises(release.ReleaseError, match="different pins"):
        release.common_pins([man, same, other])
    assert release.common_pins([]) == {}


def test_clients_served_different_tools_are_refused():
    files = fixture_files()
    tools = json.loads(files["bg3-tools.json"])
    calls = iter([tools, tools[:-1]])

    def dump(system: Path, tools_path: Path, instructions: Path, repo: Path) -> None:
        fake_dump(files, next(calls))(system, tools_path, instructions, repo)

    with pytest.raises(release.ReleaseError, match="different tools"):
        release.serve("rules", ["pi", "claude"], Path("/nonexistent"), environ={}, dump=dump)


def test_cli_refuses_another_suite(tmp_path, capsys):
    assert release.main(["--suite", "bench-v0.1", "--out", str(tmp_path / "r.json")]) == 1
    assert "not the installed suite" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()


def test_default_location_and_the_validator_cli(tmp_path, capsys, served):
    assert release.default_path("bench-v0.8") == release.RELEASES / "bench-v0.8.json"
    assert release.default_path().name == f"{suite.suite_version()}.json"
    assert validate.main([str(tmp_path), "--release", str(tmp_path / "none.json")]) == 2
    (tmp_path / "bad.json").write_text('{"schema": 1}')
    assert validate.main([str(tmp_path), "--release", str(tmp_path / "bad.json")]) == 2
    assert "release.v1" in capsys.readouterr().err


def test_a_profile_variant_serves_fewer_tools_and_another_prompt(served):
    files = fixture_files()
    noshot = release.serve(
        "rules", ["pi"], Path("/nonexistent"), environ={}, dump=fake_dump(files), groups_off=("screenshots",)
    )
    gone = set(served.names) - set(noshot.names)
    assert gone == {"screenshot", "zoom"} and set(noshot.names) < set(served.names)  # the screenshots group
    assert noshot.prompts["pi"]["sha256"] != served.prompts["pi"]["sha256"]  # no screenshots rules served
    key = release.variant_key(("screenshots",))
    doc = release.build(
        "bench-v0.8",
        VERSIONS,
        PINS,
        pack.load_scenarios(),
        saves.load()["bench-v0.8"],
        {"rules": served},
        {"rules": {key: noshot}},
    )
    assert release.errors(doc) == []
    var = doc["conditions"]["rules"]["variants"][key]
    assert var["tools"]["count"] == len(noshot.names) and var["prompts"] == {"pi": noshot.prompts["pi"]}
    cond = doc["conditions"]["rules"]
    assert release.condition_entry(cond, var["tools"]["allowlist_sha256"]) == var
    base = release.condition_entry(cond, cond["tools"]["allowlist_sha256"])
    assert base == {"tools": cond["tools"], "prompts": cond["prompts"]}
    assert release.condition_entry(cond, "0" * 64) == base  # unknown tools: checked against the condition (C01 fails)
    assert release.session_env("rules", {}, ("screenshots",))["BG3AI_TOOLS_OFF"].endswith(",screenshots")
