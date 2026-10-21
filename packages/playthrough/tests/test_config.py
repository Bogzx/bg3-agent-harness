import json
from dataclasses import replace
from pathlib import Path

import pytest

from bg3play import config as C

BRIEF = ("A level-headed half-elf fighter, once a Baldur's Gate city watch sergeant: protects the weak, keeps "
         "promises, distrusts mind magic and anyone who offers easy power.")
# the profile.json shapes on disk on 2026-10-21 (playthroughs/*/profile.json), plus the documented optional keys
OLD_PROFILES = [
    {"character": {"brief": BRIEF, "roleplay": "strict", "name": "Corran Vell"}},
    {"character": {"brief": BRIEF, "roleplay": "strict"}},
    {"character": {"brief": "a dwarf cleric", "roleplay": "light", "constraints": "no stealing"},
     "difficulty": "Tactician"},
    {"character": {"brief": "a gnome bard"}},                         # roleplay missing -> strict
    {"character": {"brief": "a gnome bard", "roleplay": "wild"}},     # bad roleplay -> strict (load_profile)
    {"character": {"brief": "   "}, "difficulty": "Balanced"},        # no brief -> no character
    {"difficulty": "Nightmare"},                                      # unknown difficulty dropped
    {},
]


def full_config() -> C.PlaythroughConfig:
    return C.PlaythroughConfig(
        name="ilse", difficulty="Balanced",
        agent=C.Agent(profile="or-deepseek", client="pi", model="deepseek/deepseek-v4", effort="high"),
        character=C.Character(brief='a "pious" dwarf\ncleric \\ with a tab\t', roleplay="light", name="Ilse",
                              constraints="never steal"),
        session=C.Session(minutes=45.0, hours=8.5), keeper=C.Keeper(enabled=False, model="sonnet", effort="medium",
                                                                    minutes=3.0),
        recording=C.Recording(enabled=False, fps=10, height=1080))


def test_round_trip_full():
    cfg = full_config()
    assert C.loads(C.dumps(cfg)) == cfg


def test_round_trip_defaults_and_every_key_written():
    cfg = C.validate({"v": 1, "name": "naut"})
    text = C.dumps(cfg)
    assert C.loads(text) == cfg
    for section in C.SECTIONS:
        assert f"[{section}]" in text
    assert cfg.recording.enabled and cfg.keeper.enabled and cfg.agent.client == "claude"


def test_save_load_files(tmp_path: Path):
    d = tmp_path / "ilse"
    d.mkdir()
    cfg = replace(full_config(), name="ilse")
    path = C.save(d, cfg)
    assert path == d / "playthrough.toml"
    assert C.load(d) == cfg
    assert sorted(p.name for p in d.iterdir()) == ["playthrough.toml"]  # no temp file left behind


@pytest.mark.parametrize("key,section,value", [
    ("client", "agent", "copilot"), ("effort", "agent", "extreme"), ("roleplay", "character", "method"),
    ("effort", "keeper", ""), ("difficulty", None, "Honour"),
])
def test_bad_enum_rejected(key, section, value):
    data = {"v": 1, "name": "x"}
    if section:
        data[section] = {key: value}
    else:
        data[key] = value
    with pytest.raises(C.ConfigError, match=key):
        C.validate(data)


@pytest.mark.parametrize("data,match", [
    ({"v": 2, "name": "x"}, "v:"),
    ({"name": "x"}, "v:"),
    ({"v": 1}, "name"),
    ({"v": 1, "name": "../etc"}, "name"),
    ({"v": 1, "name": "x", "sesion": {}}, "unknown key"),
    ({"v": 1, "name": "x", "agent": {"modle": "opus"}}, "agent: unknown"),
    ({"v": 1, "name": "x", "agent": "opus"}, "agent: expected a table"),
    ({"v": 1, "name": "x", "session": {"minutes": "60"}}, "session.minutes: expected float"),
    ({"v": 1, "name": "x", "session": {"minutes": 0}}, "session.minutes"),
    ({"v": 1, "name": "x", "session": {"hours": -1}}, "session.hours"),
    ({"v": 1, "name": "x", "recording": {"enabled": 1}}, "recording.enabled: expected bool"),
    ({"v": 1, "name": "x", "recording": {"fps": 15.0}}, "recording.fps: expected int"),
    ({"v": 1, "name": "x", "recording": {"fps": 0}}, "recording.fps"),
    ({"v": 1, "name": "x", "recording": {"height": 99999}}, "recording.height"),
    ({"v": 1, "name": "x", "agent": {"profile": "a b"}}, "agent.profile"),
    ({"v": 1, "name": "x", "agent": {"model": " "}}, "agent.model"),
])
def test_invalid_rejected(data, match):
    with pytest.raises(C.ConfigError, match=match):
        C.validate(data)


def test_int_minutes_accepted_as_float():
    assert C.validate({"v": 1, "name": "x", "session": {"minutes": 30}}).session.minutes == 30.0


def test_broken_toml_is_config_error():
    with pytest.raises(C.ConfigError, match="not valid TOML"):
        C.loads("v = 1\nname = ")


@pytest.mark.parametrize("profile", OLD_PROFILES)
def test_old_profile_loads_equal(tmp_path: Path, profile: dict):
    d = tmp_path / "corran"
    d.mkdir()
    (d / "profile.json").write_text(json.dumps(profile, indent=1))
    legacy = C.parse_profile_json((d / "profile.json").read_text())
    cfg = C.load(d)
    assert cfg.name == "corran"
    assert C.to_profile(cfg) == legacy
    assert C.migrate(d) == d / "playthrough.toml"
    assert C.to_profile(C.load(d)) == legacy
    assert (d / "profile.json").exists()  # the runner still reads it


def test_parse_profile_json_matches_load_profile_contract():
    # the documented load_profile behaviour (runner/play.py:1193), which this copy must keep
    assert C.parse_profile_json("not json") == {}
    assert C.parse_profile_json("[1]") == {}
    assert C.parse_profile_json('{"character": {"brief": "x", "roleplay": "bad"}}') == {
        "character": {"brief": "x", "roleplay": "strict"}}
    assert C.parse_profile_json('{"character": {"name": "A"}, "difficulty": "Balanced"}') == {"difficulty": "Balanced"}


def test_migrate_noop_when_toml_exists_or_nothing_to_migrate(tmp_path: Path):
    assert C.migrate(tmp_path) is None
    (tmp_path / "profile.json").write_text('{"character": {"brief": "x"}}')
    C.save(tmp_path, C.validate({"v": 1, "name": "keep"}))
    assert C.migrate(tmp_path) is None
    assert C.load(tmp_path).name == "keep"


def test_profile_with_unknown_keys_is_refused_not_dropped(tmp_path: Path):
    (tmp_path / "profile.json").write_text('{"character": {"brief": "x", "voice": "deep"}}')
    with pytest.raises(C.ConfigError, match="voice"):
        C.load(tmp_path)
    (tmp_path / "profile.json").write_text('{"character": {"brief": "x"}, "seed": 3}')
    with pytest.raises(C.ConfigError, match="seed"):
        C.load(tmp_path)


def test_defaults_without_files(tmp_path: Path):
    cfg = C.load(tmp_path)
    assert cfg == C.PlaythroughConfig(name=tmp_path.name)
    assert C.to_profile(cfg) == {}
