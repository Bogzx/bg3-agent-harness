# pyright: basic
import json
import shutil
import stat
from pathlib import Path

import pytest

from bg3bench.serve import panel, security
from bg3bench.serve.settings import Settings, SettingsError, set_dotenv

REPO = Path(__file__).resolve().parents[3]
SCENARIOS = REPO / "packages" / "bench" / "scenarios"


@pytest.fixture
def profiles(tmp_path: Path) -> Path:
    d = tmp_path / "profiles"
    d.mkdir()
    for name in ("deepseek-flash", "opus-5.5-pi", "sonnet-5.5-claude-code", "codex"):
        shutil.copy(REPO / "profiles" / f"{name}.toml", d)
    return d


@pytest.fixture
def scenarios() -> dict[str, dict]:
    return panel.load_scenarios(SCENARIOS)


def plan(scs, prof_dir, tmp_path, budget=None, verified=None, form=None):
    b = tmp_path / "budget.json"
    if budget is not None:
        b.write_text(json.dumps(budget))
    form = {"profiles": ["deepseek-flash"], "scenarios": ["act1_get_item"], "max_cost_usd": 1.5, **(form or {})}
    return panel.plan_run(form, scenarios=scs, prof_dir=prof_dir, budget=b, youtube_verified=verified)


def test_mask_never_shows_more_than_four_characters():
    assert security.mask(None) is None and security.mask("") is None
    assert security.mask("short-key") == "set"
    assert security.mask("fake-0123456789abcdefXYZW") == "…XYZW"


def test_host_ok():
    assert security.host_ok("127.0.0.1:8790", 8790) and security.host_ok("LOCALHOST:8790", 8790)
    for bad in (None, "", "127.0.0.1", "127.0.0.1:8791", "[::1]:8790", "evil.example:8790", "localhost:8790.evil"):
        assert not security.host_ok(bad, 8790)


def test_issue_token_is_private_and_fresh(tmp_path):
    a, b = security.issue_token(tmp_path / "cfg"), security.issue_token(tmp_path / "cfg")
    f = tmp_path / "cfg" / security.TOKEN_FILE
    assert a != b and f.read_text() == b + "\n" and stat.S_IMODE(f.stat().st_mode) == 0o600


def test_set_dotenv_replaces_one_line_and_keeps_the_rest(tmp_path):
    env = tmp_path / ".env"
    set_dotenv(env, "A_KEY", "v1")
    assert env.read_text() == "A_KEY=v1\n"
    env.write_text("X=1\nA_KEY=old\n# c\nA_KEY=dup\n")
    set_dotenv(env, "A_KEY", "v2")
    assert env.read_text() == "X=1\nA_KEY=v2\n# c\n"


def test_settings_view_masks_and_rejects_bad_prefs(tmp_path):
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=fake-abcdefghijklmnop1234\n")
    s = Settings(
        tmp_path / ".env", tmp_path / "cfg", lambda: ["OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"], ignored=lambda _: True
    )
    v = s.view()
    assert v["keys"] == [
        {"name": "DEEPSEEK_API_KEY", "masked": None},
        {"name": "OPENROUTER_API_KEY", "masked": "…1234"},
    ]
    assert v["prefs"] == {"youtube_verified": None, "record_cap_gb": 50}
    with pytest.raises(SettingsError):
        s.set_prefs({"youtube_verified": "yes"})
    with pytest.raises(SettingsError):
        s.set_prefs({"other": 1})


def test_story_saves_status(tmp_path):
    story = tmp_path / "Story"
    (story / "Tav-1__SCN-a").mkdir(parents=True)
    assert panel.StorySaves(story).status("Tav-1__SCN-a", "x")["status"] == "installed"
    assert panel.StorySaves(story).status("Tav-1__SCN-b", "x")["status"] == "missing"
    assert panel.StorySaves(story).status(None, None)["status"] == "none"
    assert panel.StorySaves(tmp_path / "nope").status("Tav-1__SCN-a", "x")["status"] == "unknown"
    assert panel.StorySaves(None).status("Tav-1__SCN-a", "x")["status"] == "unknown"


def test_profiles_offer_pi_only(profiles):
    rows = {r["name"]: r for r in panel.profile_rows(profiles, lambda n: n == "DEEPSEEK_API_KEY")}
    assert rows["deepseek-flash"]["offered"] and rows["deepseek-flash"]["key_set"] is True
    assert rows["opus-5.5-pi"]["key_set"] is False and rows["opus-5.5-pi"]["status"] == "offline"
    claude = rows["sonnet-5.5-claude-code"]
    assert not claude["offered"] and claude["why"].startswith("v1 offers pi only: a Claude Code run is a pilot")
    assert claude["api_key_env"] is None
    assert rows["codex"]["why"] == "v1 offers pi only"


def test_plan_jobs_reserve_and_budget(scenarios, profiles, tmp_path):
    r = plan(scenarios, profiles, tmp_path, budget={"cap": 5.0, "spent": 1.0}, form={"repeats": 2})
    assert [j["repeat"] for j in r["jobs"]] == [1, 2] and r["reserve_usd"] == 3.0 and r["fits_budget"]
    assert r["gaps"] == {"deepseek-flash": []} and r["skipped"] == {}
    r = plan(scenarios, profiles, tmp_path, budget={"cap": 5.0, "spent": 4.0}, form={"repeats": 2})
    assert not r["fits_budget"]


def test_plan_without_a_budget_file_never_fits(scenarios, profiles, tmp_path):
    r = plan(scenarios, profiles, tmp_path)
    assert not r["fits_budget"] and "no budget file" in r["budget"]["error"]


def test_plan_paid_run_needs_a_max_cost(scenarios, profiles, tmp_path):
    r = plan(scenarios, profiles, tmp_path, budget={"cap": 5.0, "spent": 0.0}, form={"max_cost_usd": None})
    assert r["needs_max_cost"] == ["act1_get_item"] and not r["fits_budget"]


def test_plan_youtube_note(scenarios, profiles, tmp_path):
    r = plan(scenarios, profiles, tmp_path, verified=True)
    assert r["youtube"]["long_scenarios"] == ["act1_get_item"] and not r["youtube"]["warn"]
    assert plan(scenarios, profiles, tmp_path, verified=False)["youtube"]["warn"]


def test_plan_skips_scenarios_that_are_not_ready(scenarios, profiles, tmp_path):
    not_ready = sorted(n for n, sc in scenarios.items() if panel.scenario.not_ready(sc))
    if not not_ready:
        pytest.skip("every scenario of the suite is ready")
    r = plan(scenarios, profiles, tmp_path, form={"scenarios": [not_ready[0], "act1_get_item"]})
    assert list(r["skipped"]) == [not_ready[0]] and {j["scenario"] for j in r["jobs"]} == {"act1_get_item"}


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"profiles": ["nope"]}, "unknown profile"),
        ({"profiles": ["../profiles/deepseek-flash"]}, "unknown profile"),
        ({"profiles": ["opus-5.5-pi"]}, "never played live"),
        ({"scenarios": ["nope"]}, "unknown scenarios"),
        ({"scenarios": []}, "non-empty list"),
        ({"repeats": 0}, "repeats"),
        ({"repeats": True}, "repeats"),
        ({"max_cost_usd": -1}, "max_cost_usd"),
        ({"condition": "rules+bogus"}, "condition"),
        ({"condition": 3}, "condition must be"),
        ({"profiles": ["deepseek-flash", "deepseek-flash"]}, "duplicate"),
    ],
)
def test_plan_refusals(scenarios, profiles, tmp_path, body, error):
    with pytest.raises(panel.PanelError, match=error):
        plan(scenarios, profiles, tmp_path, form=body)


def test_plan_allow_unverified(scenarios, profiles, tmp_path):
    r = plan(scenarios, profiles, tmp_path, form={"profiles": ["opus-5.5-pi"], "allow_unverified": True})
    assert r["jobs"][0]["client"] == "pi" and r["jobs"][0]["profile"] == "opus-5.5-pi"


def test_results_and_queue_ignore_broken_files(tmp_path):
    (tmp_path / "sessions" / "broken").mkdir(parents=True)
    (tmp_path / "sessions" / "broken" / "manifest.json").write_text("{")
    (tmp_path / "sessions" / "broken" / "result.json").write_text("{}")
    run = tmp_path / "sessions" / "ok"
    (run / "saves" / "a").mkdir(parents=True)
    (run / "saves" / "a" / "x.lsv").write_bytes(b"12345")
    (run / "manifest.json").write_text(json.dumps({"run_id": "ok-r1"}))
    (run / "result.json").write_text(json.dumps({"valid": False, "invalid_reason": "operator", "pilot": ["p"]}))
    (row,) = panel.results(tmp_path / "sessions")
    assert row["run_id"] == "ok-r1" and row["saves_bytes"] == 5 and row["pilot"] == ["p"] and row["valid"] is False
    assert panel.results(tmp_path / "none") == []
    (tmp_path / "matrix" / "m").mkdir(parents=True)
    (tmp_path / "matrix" / "m" / "progress.json").write_text("[]")
    assert panel.queue(tmp_path / "matrix") == []


def test_results_send_created_as_epoch_seconds_newest_first(tmp_path):
    """manifest.v1's created is epoch seconds: the API sends a number (or null), sorted as a number."""
    for name, created in (("a", 9.5), ("b", 10), ("c", "2026-10-25T10:00:00Z"), ("d", True), ("e", None)):
        run = tmp_path / "sessions" / name
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(json.dumps({"run_id": name, "created": created}))
        (run / "result.json").write_text("{}")
    rows = panel.results(tmp_path / "sessions")
    assert [(r["dir"], r["created"]) for r in rows] == [
        ("b", 10.0), ("a", 9.5), ("c", None), ("d", None), ("e", None)
    ]  # fmt: skip


def test_panel_checks():
    lines = panel.panel_checks(which=lambda _: None, env={}, disk_free=lambda: 5 * 2**30)
    assert [s for s, _, _ in lines] == ["FAIL", "FAIL", "WARN"]
    lines = panel.panel_checks(which=lambda n: f"/usr/bin/{n}", env={"DISPLAY": ":1"}, disk_free=lambda: 100 * 2**30)
    assert [s for s, _, _ in lines] == ["PASS", "PASS", "PASS"]
    assert panel.preflight_view(lines) == {
        "checks": [{"status": s, "name": n, "detail": d} for s, n, d in lines],
        "fails": 0,
        "warns": 0,
        "ready": True,
    }


def test_git_ignored(tmp_path, monkeypatch):
    from bg3bench.serve import settings

    assert not settings.git_ignored(tmp_path / ".env")  # not a checkout
    assert settings.git_ignored(REPO / ".env")  # the repo's .gitignore

    def no_git(*a, **kw):
        raise OSError("no git")

    monkeypatch.setattr(settings.subprocess, "run", no_git)
    assert not settings.git_ignored(REPO / ".env")


def test_broken_profile(profiles, scenarios, tmp_path):
    (profiles / "broken.toml").write_text("client = [")
    rows = {r["name"]: r for r in panel.profile_rows(profiles, lambda n: False)}
    assert rows["broken"]["why"] == "the profile doesn't load" and not rows["broken"]["offered"]
    with pytest.raises(panel.PanelError, match="broken"):
        plan(scenarios, profiles, tmp_path, form={"profiles": ["broken"]})


def test_settings_odd_inputs(tmp_path):
    s = Settings(tmp_path / ".env", tmp_path, lambda: ["DEEPSEEK_API_KEY"], ignored=lambda _: True)
    (tmp_path / "panel.json").write_text("[1]")
    assert s.prefs()["youtube_verified"] is None
    for bad in ("two words", '"quoted', "tab\tkey"):
        with pytest.raises(SettingsError, match="whitespace"):
            s.set_site_token(bad)
