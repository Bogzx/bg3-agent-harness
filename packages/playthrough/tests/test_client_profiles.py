from pathlib import Path

from bg3play import client_profiles as CP

REPO_PROFILES = Path(__file__).resolve().parents[3] / "profiles"


def test_the_repo_profiles_parse_through_runner_profiles():
    rows = {r["name"]: r for r in CP.read_profiles(REPO_PROFILES)}
    assert rows["deepseek-flash"] == {"name": "deepseek-flash", "client": "pi", "model": "deepseek-flash",
                                      "effort": "high", "provider": "deepseek", "api_key_env": "DEEPSEEK_API_KEY",
                                      "error": None}
    cc = rows["opus-5.5-claude-code"]
    assert (cc["client"], cc["model"], cc["provider"], cc["error"]) == ("claude", "claude-opus-5-5", None, None)
    assert all(r["error"] is None for r in rows.values())


def test_bad_and_missing(tmp_path: Path):
    (tmp_path / "a.toml").write_text('client = "claude"\nmodel = "opus"\n[endpoint]\nprovider = "x"\n')
    (tmp_path / "b.toml").write_text("not toml [")
    (tmp_path / "notes.txt").write_text("ignored")
    rows = CP.read_profiles(tmp_path)
    assert [r["name"] for r in rows] == ["a", "b"]
    assert "only apply to client" in rows[0]["error"] and str(tmp_path) not in rows[0]["error"]
    assert "not valid TOML" in rows[1]["error"]
    assert CP.read_profiles(tmp_path / "none") == [] and CP.read_profiles(None) == []


def test_injected_loader(tmp_path: Path):
    (tmp_path / "x.toml").write_text("")

    class P:
        client, model, effort, endpoint = "codex", "gpt", None, None

    assert CP.read_profiles(tmp_path, load=lambda p: P())[0]["effort"] == ""
