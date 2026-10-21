"""Publish a results release: GitHub release assets (results.json and the site as one zip) and a Hugging Face
dataset (per-run trajectories and the Inspect .eval logs), through an injected `Uploader`.

Nothing here talks to a network. `plan(release_dir, ...)` decides exactly which files go where; `publish(plan,
uploader)` hands them over. `DryRun(out_dir)` is the uploader that writes the exact file tree instead
(out/github/<tag>/<asset>, out/hf/<repo>/<path>) plus `publish.json` listing every file's sha256, so a release can be
reviewed before a real uploader (gh, huggingface_hub; not written yet) is wired in.

A release directory, as the matrix and results tooling leave it:

    results.json                      results v1 (holdout runs already left out: results.build_results(holdout=))
    site/                             the static leaderboard (site/build.py) and anything it links
    runs/<run_id>/                    manifest.json, result.json, facts.jsonl, rolls.jsonl, events.jsonl, *.events.jsonl
    eval/*.eval                       export_inspect.py output

Only runs listed in results.json are published, and from a run directory only the files in RUN_FILES (anything else
there, such as screenshots or logs with local paths, is skipped and listed in the plan). A run of a holdout scenario
refuses the whole plan: a published release never contains one (contamination.holdout_names).

    python -m bg3bench.publish RELEASE_DIR --tag bench-v0.1 --hf-repo OWNER/bg3-bench --dry-run OUT_DIR
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import zipfile
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from bg3bench import results as results_mod

RUN_FILES = ("manifest.json", "result.json", "facts.jsonl", "rolls.jsonl", "events.jsonl", "*.events.jsonl")
ZIP_TIME = (2026, 1, 1, 0, 0, 0)  # fixed, so the same site gives the same zip bytes


class PublishError(ValueError):
    pass


@dataclass(frozen=True)
class Asset:
    path: str  # where it lands: the asset name on GitHub, the repo path on Hugging Face
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class Plan:
    tag: str
    hf_repo: str
    github: list[Asset] = field(default_factory=list)
    hf: list[Asset] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def listing(self) -> dict:
        def rows(assets: list[Asset]) -> list[dict]:
            return [{"path": a.path, "bytes": len(a.data), "sha256": a.sha256} for a in assets]

        return {
            "tag": self.tag,
            "hf_repo": self.hf_repo,
            "github": rows(self.github),
            "hf": rows(self.hf),
            "skipped": self.skipped,
        }


class Uploader(Protocol):
    def github_release(self, tag: str, assets: Sequence[Asset]) -> str: ...

    def hf_dataset(self, repo: str, files: Sequence[Asset], message: str) -> str: ...


def site_zip(site_dir: Path) -> bytes:
    """The site as one deterministic zip (sorted names, fixed times)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(x for x in site_dir.rglob("*") if x.is_file()):
            info = zipfile.ZipInfo(p.relative_to(site_dir).as_posix(), ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes())
    return buf.getvalue()


def _wanted(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in RUN_FILES)


def dataset_card(release: dict, tag: str) -> str:
    scenarios = sorted({r["scenario"] for r in release["runs"]})
    models = sorted({r["model"] for r in release["runs"]})
    return (
        f"# BG3 benchmark {tag}\n\n"
        f"Trajectories and Inspect logs of every published run: {len(release['runs'])} runs, "
        f"{len(models)} models, {len(scenarios)} scenarios.\n\n"
        "- `results.json`: results v1, the statistics per model and condition (valid runs only).\n"
        "- `runs/<scenario>/<run_id>/`: the run's manifest, result, facts, roll log and events.\n"
        "- `eval/`: Inspect AI `.eval` logs, one per model and condition.\n\n"
        f"Scenarios: {', '.join(scenarios)}.\n"
    )


def plan(release_dir: str | Path, *, tag: str, hf_repo: str, holdout: Collection[str] = ()) -> Plan:
    root = Path(release_dir)
    results_raw = (root / "results.json").read_bytes()
    release = results_mod.load(root / "results.json")
    if release["bench_tag"] != tag:
        raise PublishError(f"results.json is release {release['bench_tag']!r}, not {tag!r}")
    if leaked := sorted({r["scenario"] for r in release["runs"]} & set(holdout)):
        raise PublishError(f"holdout scenarios in results.json: {', '.join(leaked)}; never publish them")
    site = root / "site"
    if not (site / "index.html").is_file():
        raise PublishError(f"{site}/index.html is missing: build the site first (bg3bench.site.build)")
    out = Plan(tag=tag, hf_repo=hf_repo)
    out.github = [Asset("results.json", results_raw), Asset(f"site-{tag}.zip", site_zip(site))]
    out.hf = [Asset("README.md", dataset_card(release, tag).encode()), Asset("results.json", results_raw)]
    listed = {r["run_id"]: r["scenario"] for r in release["runs"]}
    runs_dir = root / "runs"
    for run_id, scenario in sorted(listed.items()):
        d = runs_dir / run_id
        if not (d / "manifest.json").is_file() or not (d / "result.json").is_file():
            raise PublishError(f"run {run_id} is in results.json but {d} has no manifest.json and result.json")
        for p in sorted(x for x in d.rglob("*") if x.is_file()):
            rel = p.relative_to(d).as_posix()
            if "/" not in rel and _wanted(rel):
                out.hf.append(Asset(f"runs/{scenario}/{run_id}/{rel}", p.read_bytes()))
            else:
                out.skipped.append(f"runs/{run_id}/{rel}")
    if runs_dir.is_dir():
        out.skipped += [
            f"runs/{d.name}/ (not in results.json)" for d in sorted(runs_dir.iterdir()) if d.name not in listed
        ]
    for p in sorted((root / "eval").glob("*.eval")):
        out.hf.append(Asset(f"eval/{p.name}", p.read_bytes()))
    return out


def publish(p: Plan, uploader: Uploader) -> dict:
    """Hand the plan to the uploader; returns where each part went."""
    return {
        "github": uploader.github_release(p.tag, p.github),
        "hf": uploader.hf_dataset(p.hf_repo, p.hf, f"Release {p.tag}"),
    }


@dataclass
class DryRun:
    """Writes the exact tree a real uploader would send, never anything else."""

    out_dir: Path
    calls: list[str] = field(default_factory=list)

    def _write(self, base: Path, assets: Sequence[Asset]) -> None:
        for a in assets:
            dest = base / a.path
            if base.resolve() not in dest.resolve().parents:
                raise PublishError(f"asset path {a.path!r} leaves {base}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(a.data)

    def github_release(self, tag: str, assets: Sequence[Asset]) -> str:
        base = Path(self.out_dir) / "github" / tag
        self._write(base, assets)
        self.calls.append(f"github {tag} {len(assets)}")
        return str(base)

    def hf_dataset(self, repo: str, files: Sequence[Asset], message: str) -> str:
        base = Path(self.out_dir) / "hf" / repo
        self._write(base, files)
        self.calls.append(f"hf {repo} {len(files)} {message}")
        return str(base)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("release_dir")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--hf-repo", required=True)
    ap.add_argument("--holdout", default="", help="comma-separated holdout scenario names (refused)")
    ap.add_argument("--dry-run", metavar="OUT_DIR", help="write the file tree here instead of uploading")
    args = ap.parse_args(argv)
    if not args.dry_run:
        ap.error("only --dry-run exists: no real uploader is wired in yet")
    p = plan(args.release_dir, tag=args.tag, hf_repo=args.hf_repo, holdout=[h for h in args.holdout.split(",") if h])
    out = Path(args.dry_run)
    where = publish(p, DryRun(out))
    (out / "publish.json").write_text(json.dumps({**p.listing(), "where": where}, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
