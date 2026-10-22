# pyright: basic
import io
import json
import zipfile
from pathlib import Path

import pytest

from bg3bench import manifest, publish, results, suite
from bg3bench.site import build

HERE = Path(__file__).resolve().parent
NAUT2 = HERE / "fixtures" / "naut2"
TAG = suite.suite_version()  # the installed bench's: a release holds runs of its own suite
REPO = "owner/bg3-bench"


def make_release(root: Path) -> Path:
    series = json.loads((NAUT2 / "series.json").read_text())
    records = [json.loads(line) for line in (NAUT2 / "runs.jsonl").read_text().splitlines()]
    pairs = []
    for rec in records:
        man = manifest.from_series(series, rec, timing="realtime", seed=1001)
        res = results.from_record(rec, man["run_id"])
        pairs.append((man, res))
        d = root / "runs" / man["run_id"]
        d.mkdir(parents=True)
        manifest.write(man, d / "manifest.json")
        results.write(res, d / "result.json")
        (d / "facts.jsonl").write_text('{"v": 1}\n')
        (d / "S001.events.jsonl").write_text('{"kind": "tool_use"}\n')
        (d / "S001.log").write_text("/home/someone/secret path\n")  # not a published file
        (d / "shots").mkdir()
        (d / "shots" / "a.jpg").write_bytes(b"\xff\xd8")
    (root / "runs" / "naut2-r999").mkdir()  # a run results.json doesn't list
    release = results.build_results(pairs, seed=0, created=1792300000.0, bench_tag=TAG, b=20)
    results.write(release, root / "results.json")
    (root / "site").mkdir()
    (root / "site" / "index.html").write_text(build.render(release))
    (root / "site" / "runs").mkdir()
    (root / "site" / "runs" / "note.txt").write_text("x")
    (root / "eval").mkdir()
    (root / "eval" / "opus_rules.eval").write_bytes(b"PK fake eval")
    return root


class FakeUploader:
    def __init__(self):
        self.calls = []

    def github_release(self, tag, assets):
        self.calls.append(("github", tag, [a.path for a in assets]))
        return f"https://github.example/releases/{tag}"

    def hf_dataset(self, repo, files, message):
        self.calls.append(("hf", repo, [a.path for a in files], message))
        return f"https://hf.example/datasets/{repo}"


def test_plan_picks_exactly_the_published_files(tmp_path):
    p = publish.plan(make_release(tmp_path), tag=TAG, hf_repo=REPO)
    assert [a.path for a in p.github] == ["results.json", f"site-{TAG}.zip"]
    hf = [a.path for a in p.hf]
    assert hf[:2] == ["README.md", "results.json"] and hf[-1] == "eval/opus_rules.eval"
    assert [x for x in hf if x.startswith("runs/")][:4] == [
        "runs/nautiloid/naut2-r001/S001.events.jsonl",
        "runs/nautiloid/naut2-r001/facts.jsonl",
        "runs/nautiloid/naut2-r001/manifest.json",
        "runs/nautiloid/naut2-r001/result.json",
    ]
    assert len(hf) == 2 + 3 * 4 + 1
    assert "runs/naut2-r001/S001.log" in p.skipped and "runs/naut2-r001/shots/a.jpg" in p.skipped
    assert "runs/naut2-r999/ (not in results.json)" in p.skipped
    card = p.hf[0].data.decode()
    assert card.startswith(f"# BG3 benchmark {TAG}") and "3 runs, 1 models, 1 scenarios" in card
    names = zipfile.ZipFile(io.BytesIO(p.github[1].data)).namelist()
    assert names == ["index.html", "runs/note.txt"]
    again = publish.plan(tmp_path, tag=TAG, hf_repo=REPO)
    assert again.github[1].sha256 == p.github[1].sha256  # the zip is deterministic


def test_publish_hands_the_plan_to_the_uploader(tmp_path):
    p = publish.plan(make_release(tmp_path), tag=TAG, hf_repo=REPO)
    up = FakeUploader()
    where = publish.publish(p, up)
    assert where == {"github": f"https://github.example/releases/{TAG}", "hf": f"https://hf.example/datasets/{REPO}"}
    assert up.calls[0] == ("github", TAG, ["results.json", f"site-{TAG}.zip"])
    assert up.calls[1][:2] == ("hf", REPO) and up.calls[1][3] == f"Release {TAG}"
    assert up.calls[1][2] == [a.path for a in p.hf]


def test_dry_run_writes_the_exact_tree(tmp_path):
    p = publish.plan(make_release(tmp_path / "rel"), tag=TAG, hf_repo=REPO)
    out = tmp_path / "out"
    dry = publish.DryRun(out)
    publish.publish(p, dry)
    written = sorted(x.relative_to(out).as_posix() for x in out.rglob("*") if x.is_file())
    expect = sorted([f"github/{TAG}/{a.path}" for a in p.github] + [f"hf/{REPO}/{a.path}" for a in p.hf])
    assert written == expect
    for a in p.hf:
        assert (out / "hf" / REPO / a.path).read_bytes() == a.data
    assert dry.calls == [f"github {TAG} 2", f"hf {REPO} {len(p.hf)} Release {TAG}"]
    with pytest.raises(publish.PublishError, match="leaves"):
        dry.hf_dataset(REPO, [publish.Asset("../escape.txt", b"x")], "m")
    assert not (out / "hf" / "owner" / "escape.txt").exists()


def test_plan_refuses(tmp_path):
    root = make_release(tmp_path)
    with pytest.raises(publish.PublishError, match="not 'bench-v9'"):
        publish.plan(root, tag="bench-v9", hf_repo=REPO)
    with pytest.raises(publish.PublishError, match="holdout scenarios in results.json: nautiloid"):
        publish.plan(root, tag=TAG, hf_repo=REPO, holdout={"nautiloid", "other"})
    (root / "runs" / "naut2-r002" / "result.json").unlink()
    with pytest.raises(publish.PublishError, match="naut2-r002 is in results.json"):
        publish.plan(root, tag=TAG, hf_repo=REPO)
    (root / "site" / "index.html").unlink()
    with pytest.raises(publish.PublishError, match="build the site first"):
        publish.plan(root, tag=TAG, hf_repo=REPO)


def test_listing_and_cli(tmp_path):
    root = make_release(tmp_path / "rel")
    out = tmp_path / "out"
    assert publish.main([str(root), "--tag", TAG, "--hf-repo", REPO, "--dry-run", str(out)]) == 0
    listing = json.loads((out / "publish.json").read_text())
    assert listing["tag"] == TAG and listing["where"]["hf"] == str(out / "hf" / REPO)
    assert listing["github"][0]["path"] == "results.json" and len(listing["github"][0]["sha256"]) == 64
    assert (out / "github" / TAG / "results.json").read_bytes() == (root / "results.json").read_bytes()
    with pytest.raises(SystemExit):
        publish.main([str(root), "--tag", TAG, "--hf-repo", REPO])  # no real uploader: refused
    with pytest.raises(publish.PublishError, match="holdout"):
        publish.main([str(root), "--tag", TAG, "--hf-repo", REPO, "--holdout", "nautiloid", "--dry-run", str(out)])
