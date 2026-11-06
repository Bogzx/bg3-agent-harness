"""Offline tests for tools/dev/twins.py on a small git repo made in a temp dir.
Run: uv run pytest -q tools/dev/test_twins.py"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import twins

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}
GUARD = "mod/Mods/BG3AI/ScriptExtender/Lua/Server/Guard.lua"
VIS = "mod/Mods/BG3AI/ScriptExtender/Lua/Server/VisibilityObjects.lua"
RENDER = "bridge/src/bg3ai/render.py"
LONG = "render: a refusal states the fact only, with no next call"  # >= twins.SUBJECT_MIN


def g(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], env=ENV, check=True, capture_output=True,
                          text=True).stdout.strip()  # fmt: skip


def commit(repo: Path, msg: str, files: dict[str, str]) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        g(repo, "add", rel)
    g(repo, "commit", "-q", "-m", msg)
    return g(repo, "rev-parse", "HEAD")


def make(tmp: Path) -> tuple[Path, dict[str, str]]:
    """base; bench: A (Guard), B (render.py), C (Visibility, orphan), N (README only), Q (Guard), S (render.py),
    T (render.py, short subject). arena: R (README), A' (cherry-pick of A), B' (a hand port citing B), D (Guard,
    citing a commit that isn't on the bench side), E (Guard, orphan), P (a port of Q, citing it), S' (S's subject, a
    different patch), T' (T's short subject, a different patch), and a merge of a side branch."""
    repo = tmp / "r"
    repo.mkdir()
    g(repo, "init", "-q", "-b", "main")
    s = {"base": commit(repo, "base", {GUARD: "g0\n", VIS: "v0\n", RENDER: "r0\n", "README": "x\n"})}
    g(repo, "checkout", "-q", "-b", "bench")
    s["A"] = commit(repo, "guard: refuse a hidden target", {GUARD: "g0\ng1\n"})
    s["B"] = commit(repo, "render: hide the other side's hp", {RENDER: "r0\nr1\n"})
    s["C"] = commit(repo, "visibility: fog rule", {VIS: "v0\nv1\n"})
    s["N"] = commit(repo, "readme", {"README": "y\n"})
    s["Q"] = commit(repo, "guard: cast range", {GUARD: "g0\ng1\ng2\n"})
    s["S"] = commit(repo, LONG, {RENDER: "r0\nr1\ns\n"})
    s["T"] = commit(repo, "review fixes", {RENDER: "r0\nr1\ns\nt\n"})
    g(repo, "checkout", "-q", "-b", "arena", s["base"])
    commit(repo, "arena readme", {"README": "a\n"})  # else the cherry-pick below remakes A itself (same parent, second)
    g(repo, "cherry-pick", s["A"])
    s["A2"] = g(repo, "rev-parse", "HEAD")
    s["B2"] = commit(repo, f"Arena twin of bench ({s['B'][:8]}): render hides hp per side", {RENDER: "r0\nR\n"})
    s["D"] = commit(repo, "Guard: twin of bp/x deadbeef1", {GUARD: "g0\ng1\nd\n"})
    s["E"] = commit(repo, "Guard: arena-only rule", {GUARD: "g0\ng1\nd\ne\n"})
    s["P"] = commit(repo, f"Guard: port of {s['Q'][:7]}", {GUARD: "g0\ng1\nd\ne\nq\n"})
    s["S2"] = commit(repo, LONG, {RENDER: "r0\nR\ns\n"})  # same subject, other context: another patch-id
    s["T2"] = commit(repo, "review fixes", {RENDER: "r0\nR\ns\nt2\n"})
    g(repo, "checkout", "-q", "-b", "side", s["base"])
    commit(repo, "side work", {"SIDE": "z\n"})
    g(repo, "checkout", "-q", "arena")
    g(repo, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
    return repo, s


def test_orphans_are_fairness_commits_with_no_patch_id_twin_and_no_citation_either_way(tmp_path):
    repo, s = make(tmp_path)
    r = twins.orphans(repo, "bench", "arena")
    assert r.base == s["base"]
    # A: cherry-picked (patch-id); B: cited by B2; Q: cited by P; S: S2's subject; N: no fairness file; T: its
    # subject is too short to count
    assert [c.sha for c in r.orphans["bench"]] == [s["C"], s["T"]]
    assert r.orphans["bench"][0].files == [VIS]
    # A2: patch-id; B2: cites B; P: cites Q; S2: S's subject; D cites a sha that is not on the bench side; E: nothing;
    # T2: short subject; the merge: no
    assert [c.sha for c in r.orphans["arena"]] == [s["D"], s["E"], s["T2"]]
    assert r.fairness == {"bench": 6, "arena": 7} and r.commits == {"bench": 7, "arena": 9}


def test_twin_says_how_and_cites_needs_seven_hex_characters(tmp_path):
    repo, s = make(tmp_path)
    base = s["base"]
    bench, arena = twins.side(repo, base, "bench"), twins.side(repo, base, "arena")
    assert twins.twin(bench[s["A"]], arena, {}) == f"patch-id {s['A2'][:8]}"
    assert twins.twin(arena[s["P"]], bench, {}) == f"cites {s['Q'][:8]}"
    assert bench[s["S"]].patch_id != arena[s["S2"]].patch_id
    assert twins.twin(bench[s["S"]], arena, {}) == f"subject {s['S2'][:8]}"
    assert twins.cites(f"see {s['Q'][:6]}", bench) == set()  # 6 characters is not a citation
    assert twins.cites(f"see {s['Q'][:7]} and {s['C']}", bench) == {s["Q"], s["C"]}


def test_main_prints_the_orphans_and_a_git_error_exits_2(tmp_path, capsys):
    repo, s = make(tmp_path)
    assert twins.main(["bench", "arena", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "## bench: 2 orphan(s) of 6 fairness commits (7 non-merge commits)" in out
    assert f"- {s['E'][:8]} " in out and "Guard: arena-only rule" in out and f"  - files: {GUARD}" in out
    assert twins.main(["bench", "nope", "--repo", str(repo)]) == 2
    assert "twins: git merge-base" in capsys.readouterr().err
