"""Tests for tools/ci/scrub.py: every kind on planted fixtures, the allowlist, and main() on a tmp repo.

Planted secrets and names are built at runtime ("sk-" + ...), so this file itself scans clean.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import scrub

KEY = "sk-" + "or-v1-" + "0123456789abcdef" * 4
GH = "gh" + "p_" + "A1b2C3d4" * 5
HOME = "/" + "home/" + "alice"
MAC = "/" + "Users/" + "alice"
WIN = "C:" + "\\Users\\" + "alice"
MAIL = "alice" + "@" + "corp.test"
STEAM = "7656119" + "8012345678"
TS = "dev-box." + "tail1234." + "ts.net"


def kinds(text: str, path: str = "f.txt", **kw: object) -> list[tuple[int, str]]:
    return [(h.line, h.kind) for h in scrub.scan_text(path, text, **kw)]  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"log at {HOME}/x", "home_path"),
        (f"log at {MAC}/x", "home_path"),
        (f"log at {WIN}\\x", "home_path"),
        (f"mail {MAIL}", "email"),
        (f"key {KEY}", "api_key"),
        (f"token {GH}", "api_key"),
        ("-----BEGIN " + "OPENSSH PRIVATE KEY-----", "api_key"),
        ("DEEPSEEK_API_" + "KEY=Zx9" + "q" * 20, "secret"),
        (f"ssh {TS}", "tailscale"),
        ("ip 100." + "101.2.3", "tailscale"),
        (f"steam {STEAM}", "steam_id"),
        ("Steam/" + "userdata/" + "123456789/config", "steam_id"),
    ],
)
def test_each_kind_is_found_on_its_line(text: str, kind: str) -> None:
    assert (2, kind) in kinds(f"clean line\n{text}\n")


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "/home/<user>/x and $HOME and ~/x",
        "bot" + "@" + "users.noreply.github.com, noreply" + "@" + "corp.test, a" + "@" + "example.com",
        "sk-... and sk-<key>",
        'STUB_KEY_ENV = "BG3AI_SMOKE_STUB_KEY"',
        'KEY = "abababab-0000-0000-0000-000000000007"',
        "ip 100.63.0.1 and 100.128.0.1",
        "76561190 is short",
    ],
)
def test_placeholders_and_near_misses_are_clean(text: str) -> None:
    assert kinds(text) == []


@pytest.mark.unit
def test_env_files_are_hits_but_templates_are_not() -> None:
    assert kinds("", path="deploy/.env") == [(0, "env_file")]
    assert kinds("", path=".env.local") == [(0, "env_file")]
    assert kinds("", path=".env.example") == []


@pytest.mark.unit
def test_path_itself_is_scanned_as_line_zero() -> None:
    assert kinds("", path=f"logs{HOME}.txt") == [(0, "home_path")]


@pytest.mark.unit
def test_env_values_are_found_verbatim_and_never_printed() -> None:
    env = scrub.parse_env("# c\nexport OPENROUTER_API_KEY='Zq" + "9" * 12 + "'\nSHORT=abc\n")
    assert env == {"OPENROUTER_API_KEY": "Zq" + "9" * 12}
    hits = scrub.scan_text("t.jsonl", 'x\n{"auth": "Zq' + "9" * 12 + '"}\n', env=env)
    assert [(h.line, h.kind, h.text) for h in hits] == [(2, "env_value", "OPENROUTER_API_KEY")]
    assert hits[0].render() == "t.jsonl:2: env_value"
    assert scrub.Hit("a", 1, "api_key", KEY).render() == "a:1: api_key"
    assert scrub.Hit("a", 1, "email", MAIL).render() == f"a:1: email: {MAIL}"


@pytest.mark.unit
def test_terms_match_whole_words_case_insensitively() -> None:
    pat = scrub.term_pattern(["Alice", "box7", "ab", ""])
    assert pat is not None
    assert kinds("by ALICE on box7.", terms=pat) == [(1, "term"), (1, "term")]
    assert kinds("malice box70 ab", terms=pat) == []  # inside longer words; "ab" is under MIN_TERM
    assert scrub.term_pattern(["", "x"]) is None


@pytest.mark.unit
def test_read_terms_skips_comments_and_blanks(tmp_path: Path) -> None:
    f = tmp_path / "terms.txt"
    f.write_text("# machines\nbox7\n\n  Alice  # owner\n")
    assert scrub.read_terms(f) == ["box7", "Alice"]


@pytest.mark.unit
def test_allowlist_parses_and_filters_by_kind_glob_and_text() -> None:
    allow = scrub.parse_allowlist(
        "# header\nhome_path * /home/agent  # container user\nterm LICENSE  # authorship line\n* docs/*.md  # docs\n"
    )
    assert allow[0] == scrub.Allow("home_path", "*", "/home/agent", "container user")
    hits = [
        scrub.Hit("a.py", 1, "home_path", "/home/AGENT"),
        scrub.Hit("a.py", 2, "home_path", HOME),
        scrub.Hit("LICENSE", 3, "term", "Alice"),
        scrub.Hit("NOTICE", 3, "term", "Alice"),
        scrub.Hit("docs/x.md", 4, "email", MAIL),
    ]
    assert scrub.filter_allowed(hits, allow) == [hits[1], hits[3]]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("line", "error"),
    [
        ("term LICENSE", "needs a reason"),
        ("term  # why", "expected"),
        ("bogus * x  # why", "unknown kind"),
    ],
)
def test_bad_allowlist_lines_raise(line: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        scrub.parse_allowlist(line)


@pytest.mark.unit
def test_repo_allowlist_parses() -> None:
    assert scrub.parse_allowlist(scrub.ALLOWLIST.read_text())


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "clean.py").write_text("x = 1\n")
    (repo / "leak.md").write_text(f"one\nsee {HOME}/notes\n")
    (repo / "untracked.md").write_text(f"{KEY}\n")
    subprocess.run(["git", "add", "clean.py", "leak.md"], cwd=repo, check=True)
    return repo


def _main(repo: Path, allow: Path, *extra: str) -> int:
    return scrub.main(["--root", str(repo), "--allowlist", str(allow), "--no-local-terms", *extra])


@pytest.mark.integration
def test_main_reports_tracked_hits_only_and_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    allow = tmp_path / "allow.txt"
    allow.write_text("")
    assert _main(repo, allow, "--terms-file", str(tmp_path / "none-needed")) == 2  # a named terms file must exist
    assert _main(repo, allow, "--term", "Notes") == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [f"leak.md:2: home_path: {HOME}", "leak.md:2: term: notes"]


@pytest.mark.integration
def test_main_exits_0_when_every_hit_is_allowed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    allow = tmp_path / "allow.txt"
    allow.write_text(f"home_path leak.md {HOME}  # planted\n")
    assert _main(repo, allow) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.integration
def test_main_scans_artifacts_and_env_values(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    allow = tmp_path / "allow.txt"
    allow.write_text(f"home_path leak.md {HOME}  # planted\n")
    env = tmp_path / "real.env"
    env.write_text("DEEPSEEK_API_KEY=" + "Qw" * 10 + "\n")
    out_dir = tmp_path / "release"
    (out_dir / "runs" / "r1").mkdir(parents=True)
    (out_dir / "runs" / "r1" / "transcript.jsonl").write_text('{"k": "' + "Qw" * 10 + '"}\n' + f"{MAIL}\n")
    (out_dir / "results.json").write_text("{}\n")
    assert _main(repo, allow, "--artifacts", str(out_dir), "--env-file", str(env)) == 1
    assert capsys.readouterr().out.splitlines() == [
        "release/runs/r1/transcript.jsonl:1: env_value",
        f"release/runs/r1/transcript.jsonl:2: email: {MAIL}",
    ]


@pytest.mark.integration
@pytest.mark.parametrize("case", ["bad_allowlist", "no_artifacts", "not_a_repo"])
def test_main_exits_2_on_config_errors(tmp_path: Path, case: str) -> None:
    repo = _repo(tmp_path)
    allow = tmp_path / "allow.txt"
    allow.write_text("term x\n" if case == "bad_allowlist" else "")
    extra = ["--artifacts", str(tmp_path / "missing")] if case == "no_artifacts" else []
    if case == "not_a_repo":
        repo = tmp_path / "plain"
        repo.mkdir()
    assert _main(repo, allow, *extra) == 2
