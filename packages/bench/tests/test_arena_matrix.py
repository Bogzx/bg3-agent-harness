# pyright: basic
"""Arena pairing matrices: the TOML file, the planner, the two-sided budget and the resumable queue."""

import io
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from bg3bench import arena_matrix
from bg3bench.matrix import RunResult
from bg3bench.matrix_cli import MatrixFileError

PKG = Path(__file__).resolve().parents[1]
REPO = PKG.parents[1]
PI = (REPO / "profiles" / "opus-5.5-pi.toml").read_text().replace('status = "offline"', 'status = "verified"')
FLASH = (REPO / "profiles" / "deepseek-flash.toml").read_text()
CLAUDE = 'client = "claude"\nmodel = "claude-opus-5-5"\neffort = "high"\nstatus = "verified"\n'
DRAFT = json.loads((PKG / "arenas" / "draft_open_field.json").read_text())
SAVE = {"name": "Tav-1234__SCN-arena_open_field", "sha1": "0123456789abcdef0123456789abcdef01234567"}
FAIR = {"level": DRAFT["location"]["level"], "spawns": {"A": [0.0, 0.0, 0.0], "B": [0.0, 0.0, 20.0]}}  # check_geometry


def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "packages" / "bench" / "arenas").mkdir(parents=True)
    (repo / "profiles").mkdir()
    ready = {k: v for k, v in {**DRAFT, "name": "field", "save": SAVE, "location": FAIR}.items() if k != "draft"}
    for a in (ready, {**ready, "name": "ridge", "condition": "rules+wiki"}, DRAFT):
        (repo / "packages" / "bench" / "arenas" / f"{a['name']}.json").write_text(json.dumps(a))
    for name, text in (("opus.toml", PI), ("flash.toml", FLASH), ("claude.toml", CLAUDE)):
        (repo / "profiles" / name).write_text(text)
    return repo


BODY = """name = "arena-test"
models = ["profiles/opus.toml", "profiles/flash.toml", "profiles/claude.toml"]
arenas = ["field", "draft_open_field"]
repeats = 2

[budget]
file = "runtime/budget-arena.json"
max_cost = { field = 3.0 }

[budget.models]
"profiles/opus.toml" = ["runtime/budget-opus.json", "runtime/budget-anthropic.json"]
"profiles/flash.toml" = ["runtime/budget-deepseek.json"]
"""


def load(tmp_path, body=BODY):
    repo = fake_repo(tmp_path)
    (repo / "arena.toml").write_text(body)
    return arena_matrix.load(repo / "arena.toml", repo)


def test_the_plan_pairs_every_two_models_with_the_sides_swapped(tmp_path):
    m = load(tmp_path)
    assert m["skipped"] == {"draft_open_field": DRAFT["draft"]}
    jobs = m["jobs"]
    assert len(jobs) == 3 * 1 * 2 * 2  # 3 pairs x 1 playable arena x 2 repeats x 2 matches
    assert [j["repeat"] for j in jobs] == [1] * 6 + [2] * 6
    for one, two in zip(jobs[::2], jobs[1::2]):  # a pairing's two matches one after the other
        assert one["pairing"] == two["pairing"] and (one["match"], two["match"]) == (1, 2)
        assert one["sides"]["A"] == two["sides"]["B"] and one["sides"]["B"] == two["sides"]["A"]
    first = jobs[0]
    assert first["id"] == "pi_anthropic_claude-opus-5.5~vs~pi_deepseek-flash~field~r1~m1"
    assert first["sides"]["A"]["player"] == "pi/anthropic/claude-opus-5.5"
    assert first["sides"]["B"]["profile"] == "profiles/flash.toml"  # as the file names it
    assert (first["arena"], first["condition"], first["max_cost"]) == ("field", "rules", 3.0)
    assert first["budgets"] == {
        "own": "runtime/budget-arena.json",
        "A": ["runtime/budget-opus.json", "runtime/budget-anthropic.json"],
        "B": ["runtime/budget-deepseek.json"],
    }
    assert {j["sides"]["A"]["player"] for j in jobs} == {
        "pi/anthropic/claude-opus-5.5",
        "pi/deepseek-flash",
        "claude/claude-opus-5-5",
    }


def test_plan_is_the_same_every_time(tmp_path):
    assert load(tmp_path / "a")["jobs"] == load(tmp_path / "b")["jobs"]


def test_each_repeat_plays_its_own_seed_and_a_pairings_two_matches_share_it(tmp_path):
    """Fairness audit S1-4: every repeat played the arena's one seed, so the same initiative and opening dice counted
    again in every pairing. Repeat r plays the arena's seed + r - 1 (repeat 1 the arena's own)."""
    jobs = load(tmp_path)["jobs"]
    seed = DRAFT["seed"]
    assert [j["seed"] for j in jobs] == [seed] * 6 + [seed + 1] * 6
    for one, two in zip(jobs[::2], jobs[1::2]):
        assert one["seed"] == two["seed"]


def test_a_repeats_seed_past_the_largest_is_refused(tmp_path):
    repo = fake_repo(tmp_path)
    p = repo / "packages" / "bench" / "arenas" / "field.json"
    p.write_text(json.dumps({**json.loads(p.read_text()), "seed": 2**31 - 1}))
    (repo / "arena.toml").write_text(BODY)
    with pytest.raises(MatrixFileError, match="seed"):
        arena_matrix.load(repo / "arena.toml", repo)


def test_explicit_pairs_allow_a_self_match_and_take_the_arenas_condition(tmp_path):
    body = (
        BODY.replace(
            'models = ["profiles/opus.toml", "profiles/flash.toml", "profiles/claude.toml"]',
            'pairs = [["profiles/flash.toml", "profiles/flash.toml"]]',
        )
        .replace('arenas = ["field", "draft_open_field"]', 'arenas = ["ridge"]')
        .replace("field = 3.0", "ridge = 0.5")
    )
    body = body.replace('"profiles/opus.toml" = ["runtime/budget-opus.json", "runtime/budget-anthropic.json"]\n', "")
    jobs = load(tmp_path, body)["jobs"]
    assert [j["id"] for j in jobs] == [
        f"pi_deepseek-flash~vs~pi_deepseek-flash~ridge~r{r}~m{n}" for r in (1, 2) for n in (1, 2)
    ]
    assert {j["condition"] for j in jobs} == {"rules+wiki"}
    # both sides charge the same files: the reserve counts twice
    assert arena_matrix.reserves(jobs[0]) == {"runtime/budget-arena.json": 1.0, "runtime/budget-deepseek.json": 1.0}


def test_single_plays_a_self_pair_once_per_repeat_and_other_pairs_twice(tmp_path):
    # REVIEW-7 f2: a self-pair's m2 replays its m1 (same seed, mirror twins, same model)
    body = BODY.replace(
        'models = ["profiles/opus.toml", "profiles/flash.toml", "profiles/claude.toml"]',
        'pairs = [["profiles/flash.toml", "profiles/flash.toml"], ["profiles/flash.toml", "profiles/opus.toml"]]',
    ).replace('arenas = ["field", "draft_open_field"]', 'arenas = ["field"]\nsingle = true')
    m = load(tmp_path, body)
    ids = [j["id"] for j in m["jobs"]]
    assert ids == [
        f"{p}~field~r{r}~m{n}"
        for r in (1, 2)
        for p, ns in (
            ("pi_deepseek-flash~vs~pi_deepseek-flash", (1,)),
            ("pi_deepseek-flash~vs~pi_anthropic_claude-opus-5.5", (1, 2)),
        )
        for n in ns
    ]
    assert [j["seed"] for j in m["jobs"] if "vs~pi_deepseek-flash~" in j["id"]] == [1001, 1002]
    out = io.StringIO()
    arena_matrix.print_plan(m, out)
    assert out.getvalue().startswith("arena matrix arena-test: 6 matches, 4 pairings\n")
    with pytest.raises(MatrixFileError):
        load(tmp_path / "x", body.replace("single = true", 'single = "yes"'))


def test_reserves_and_charges_go_to_both_models_files(tmp_path):
    jobs = load(tmp_path)["jobs"]
    opus_flash = jobs[0]
    assert arena_matrix.reserves(opus_flash) == {
        "runtime/budget-arena.json": 6.0,
        "runtime/budget-opus.json": 3.0,
        "runtime/budget-anthropic.json": 3.0,
        "runtime/budget-deepseek.json": 3.0,
    }
    assert arena_matrix.charges(opus_flash, {"A": 1.25, "B": 0.5}) == {
        "runtime/budget-arena.json": 1.75,
        "runtime/budget-opus.json": 1.25,
        "runtime/budget-anthropic.json": 1.25,
        "runtime/budget-deepseek.json": 0.5,
    }
    with pytest.raises(ValueError, match="side B's cost is unknown"):
        arena_matrix.charges(opus_flash, {"A": 1.0, "B": None})
    vs_claude = next(j for j in jobs if j["sides"]["B"]["client"] == "claude")
    # a subscription side holds back nothing and is never charged
    assert arena_matrix.reserves(vs_claude)["runtime/budget-arena.json"] == 3.0
    assert arena_matrix.charges(vs_claude, {"A": 2.0, "B": 9.0})["runtime/budget-arena.json"] == 2.0


def test_a_models_own_max_cost_is_its_sides_cap_and_reserve(tmp_path):
    # arena v0 pilot: Opus costs ~30x DeepSeek per match; one per-arena cap is either too tight for Opus or loose for
    # DeepSeek. budget.max_cost_models gives a model's side its own cap; the other side keeps the arena's.
    body = BODY + '\n[budget.max_cost_models]\n"profiles/opus.toml" = 2.5\n'
    opus_flash = load(tmp_path, body)["jobs"][0]
    assert (opus_flash["sides"]["A"]["max_cost"], opus_flash["sides"]["B"]["max_cost"]) == (2.5, 3.0)
    assert arena_matrix.reserves(opus_flash) == {
        "runtime/budget-arena.json": 5.5,
        "runtime/budget-opus.json": 2.5,
        "runtime/budget-anthropic.json": 2.5,
        "runtime/budget-deepseek.json": 3.0,
    }
    assert arena_matrix.side_cap(opus_flash, "A") == 2.5
    # a queue planned before max_cost_models (no per-side value) keeps the arena's cap
    old = {
        **opus_flash,
        "sides": {s: {k: v for k, v in opus_flash["sides"][s].items() if k != "max_cost"} for s in ("A", "B")},
    }
    assert arena_matrix.side_cap(old, "A") == 3.0


def test_over_budget_names_every_file_the_reserve_does_not_fit(tmp_path):
    job = load(tmp_path)["jobs"][0]
    budgets = {f: {"cap": 10.0, "spent": 0.0} for f in arena_matrix.reserves(job)}
    assert arena_matrix.over_budget(job, budgets) == []
    budgets["runtime/budget-deepseek.json"]["spent"] = 8.0
    budgets["runtime/budget-arena.json"]["spent"] = 4.5
    assert arena_matrix.over_budget(job, budgets) == ["runtime/budget-arena.json", "runtime/budget-deepseek.json"]


def test_the_queue_resumes_with_bench_matrix_semantics(tmp_path):
    jobs = load(tmp_path)["jobs"]
    t = [0.0]
    q = arena_matrix.open_queue(tmp_path / "q", tmp_path / "budget.json", now=lambda: t[0])
    q.create("arena-test", jobs)
    q._append({"type": "start", "job": jobs[0]["id"], "attempt": 1, "t": 1.0})
    q._append(
        {
            "type": "end",
            "job": jobs[0]["id"],
            "attempt": 1,
            "t": 2.0,
            "result": asdict(RunResult("r1", True, None, 1.0)),
        }
    )
    q._append({"type": "start", "job": jobs[1]["id"], "attempt": 1, "t": 3.0})
    again = arena_matrix.open_queue(tmp_path / "q", tmp_path / "budget.json", now=lambda: t[0])
    again.create("arena-test", jobs)  # the same plan: kept
    st = again.state()
    assert (st[jobs[0]["id"]]["status"], st[jobs[1]["id"]]["status"], st[jobs[2]["id"]]["status"]) == (
        "done",
        "interrupted",
        "pending",
    )
    with pytest.raises(ValueError, match="another plan"):
        again.create("arena-test", jobs[:2])
    with pytest.raises(NotImplementedError):
        again.run_one(jobs[0])


BAD = {
    "both models and pairs": ("repeats = 2", 'repeats = 2\npairs = [["profiles/opus.toml", "profiles/flash.toml"]]'),
    "one model": ('"profiles/flash.toml", "profiles/claude.toml"', ""),
    "a pair twice": (
        'models = ["profiles/opus.toml", "profiles/flash.toml", "profiles/claude.toml"]',
        'pairs = [["profiles/opus.toml", "profiles/flash.toml"], ["profiles/flash.toml", "profiles/opus.toml"]]',
    ),
    "no arena file": ('"field", "draft_open_field"', '"nowhere"'),
    "an arena twice": ('"field", "draft_open_field"', '"field", "field"'),
    "max_cost for an arena not in it": ("field = 3.0", "field = 3.0, ridge = 1.0"),
    "a paid side with no max_cost": ("max_cost = { field = 3.0 }", ""),
    "the own file as a model file": ('["runtime/budget-deepseek.json"]', '["runtime/budget-arena.json"]'),
    "a model file for a profile not in it": ('"profiles/flash.toml" = [', '"profiles/other.toml" = ['),
    "an unknown key": ("repeats = 2", 'repeats = 2\norder = "cell"'),
    "repeats 0": ("repeats = 2", "repeats = 0"),
    "allow_drift not a bool": ("repeats = 2", 'repeats = 2\nallow_drift = "yes"'),
    "a bad name": ('name = "arena-test"', 'name = "arena test"'),
    "max_cost_models for a profile not in it": (
        '"profiles/flash.toml" = ["runtime/budget-deepseek.json"]',
        '"profiles/flash.toml" = ["runtime/budget-deepseek.json"]\n\n[budget.max_cost_models]\n"profiles/x.toml" = 1.0',
    ),
    "max_cost_models not USD > 0": (
        '"profiles/flash.toml" = ["runtime/budget-deepseek.json"]',
        '"profiles/flash.toml" = ["runtime/budget-deepseek.json"]\n\n[budget.max_cost_models]\n"profiles/opus.toml" = 0',
    ),
}


@pytest.mark.parametrize("why", sorted(BAD))
def test_a_bad_matrix_file_is_refused(tmp_path, why):
    old, new = BAD[why]
    assert old in BODY
    with pytest.raises(MatrixFileError):
        load(tmp_path, BODY.replace(old, new))


def test_an_offline_profile_needs_allow_unverified(tmp_path):
    repo = fake_repo(tmp_path)
    (repo / "profiles" / "opus.toml").write_text(PI.replace('status = "verified"', 'status = "offline"'))
    (repo / "arena.toml").write_text(BODY)
    with pytest.raises(MatrixFileError, match="offline"):
        arena_matrix.load(repo / "arena.toml", repo)
    (repo / "arena.toml").write_text("allow_unverified = true\n" + BODY)
    assert arena_matrix.load(repo / "arena.toml", repo)["jobs"]


def test_allow_drift_is_read_and_off_by_default(tmp_path):
    """REVIEW-16 A2: the matrix file's allow_drift (the queue stops a match that played with other pins unless it is
    true)."""
    assert load(tmp_path)["allow_drift"] is False
    assert load(tmp_path / "b", "allow_drift = true\n" + BODY)["allow_drift"] is True


def test_plan_cli_prints_the_matches_and_writes_nothing(tmp_path):
    repo = fake_repo(tmp_path)
    (repo / "arena.toml").write_text(BODY)
    before = sorted(p for p in repo.rglob("*"))
    out = io.StringIO()
    assert arena_matrix.main(["plan", "--matrix", "arena.toml"], repo=repo, out=out) == 0
    text = out.getvalue()
    assert text.startswith("arena matrix arena-test: 12 matches, 6 pairings\n")
    assert "skipped draft_open_field: placeholder" in text
    assert "A=pi/anthropic/claude-opus-5.5  B=pi/deepseek-flash  reserve runtime/budget-anthropic.json $3.00" in text
    assert sorted(p for p in repo.rglob("*")) == before
    (repo / "bad.toml").write_text("name = 1\n")
    assert arena_matrix.main(["plan", "--matrix", "bad.toml"], repo=repo, out=io.StringIO()) == 2


def test_the_shipped_v0_1_smoke_loads_and_reserves_two_paid_sides_per_match():
    m = arena_matrix.load(PKG / "matrices" / "arena-v0.1-smoke.toml", REPO)
    assert m["name"] == "arena-v0.1-smoke"
    assert [(j["arena"], j["max_cost"]) for j in m["jobs"]] == [("arena-v0.1", 0.2)] * 2
    assert m["budget"]["file"] == "runtime/budget-arena-v0.json"


@pytest.mark.parametrize("name", ["arena-v0-smoke", "arena-v0-seed3-smoke"])
def test_the_smokes_on_a_pinned_arena_stay_records_this_code_refuses(name):
    with pytest.raises(MatrixFileError, match="race"):
        arena_matrix.load(PKG / "matrices" / f"{name}.toml", REPO)
