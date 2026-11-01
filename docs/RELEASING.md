# Releasing

How a version is bumped and a benchmark release is tagged. The versioning rules themselves are at the top of
[CHANGELOG.md](../CHANGELOG.md); this page is the order of the steps. No tag exists yet. The first release,
`bench-v0.1`, waits for the owner's launch gate (Phase F2 of [OVERHAUL-PLAN.md](OVERHAUL-PLAN.md)).

## What carries a version

| Version | Written in | Read by | Tagged |
|---|---|---|---|
| harness | `packages/harness/pyproject.toml` (`bg3-agent-harness`) | `bg3harness.__version__` | no |
| benchmark | `packages/bench/pyproject.toml` (`bg3bench`) | `bg3bench.__version__` | no |
| benchmark suite | derived: `bench-vX.Y` of bg3bench `X.Y.Z` | `bg3bench.suite.suite_version()` | `bench-vX.Y` |
| playthrough app | `packages/playthrough/pyproject.toml` (`bg3play`) | the package metadata | no |

The pyproject is the only place a version is written. Run records, manifests and releases cite the harness, bench
and suite versions, so a result says what it was measured with even without a tag. The only git tags are the
benchmark suite's.

## Bumping a version

1. Decide the bump (CHANGELOG.md, "How versions and tags work"):
   - harness: minor for any change to what the agent sees or can do (tools, tool descriptions, rules text,
     fairness filters), patch for fixes that change none of that;
   - benchmark: minor for anything that can move a score (a scenario file, a grader, the conditions, the
     statistics, the invalid-run rule), patch for anything else;
   - app: no rule is written yet; its version changes no result.
2. Edit the `version` in that package's `pyproject.toml`, then refresh the lock, which records the workspace
   members' versions:

   ```bash
   uv lock
   ```

3. For a benchmark bump, pin the suite files' digest in `packages/bench/suite.json`. Print it with

   ```bash
   uv run --project packages/bench python -c "from bg3bench import suite; print(suite.digest())"
   ```

   - minor bump: add the new `bench-vX.Y` with that digest;
   - patch bump (no score moves): append the digest to the current version's list.
   - minor bump for a change outside these files (the mod or the bg3 tools a release pins): add the new
     `bench-vX.Y` with the same digest and name the pair in `shared` (`{"bench-vX.Y": "<previous>"}`); a digest
     pinned for two versions fails unless `shared` pairs them.

   A released suite's `packages/bench/releases/<suite>.json` is never rebuilt: the new suite gets its own file
   (`python -m bg3bench.submission.release --suite bench-vX.Y`) and its saves a `bench-vX.Y` entry in `saves.json`.
   Add the old file's sha256 to `FROZEN` in `packages/bench/tests/test_submission_release.py`.

   Never edit an existing entry: it is what published results were graded with.
   `packages/bench/tests/test_suite.py` fails until the pin matches.
4. In CHANGELOG.md, move the finished items from "Unreleased" into a section headed with the new versions and the
   date, and update the version table at the top.
5. Commit on a branch and open a PR to `main` (`main` is protected: no direct push).

## Tagging a benchmark release

A release is the tag `bench-vX.Y` on the commit its runs were played from, with bg3bench at `X.Y.0` there.

1. Check that the commit is on `main` and every CI job is green on it: Lint and types (ruff check and format, the
   legacy lint lists, pyright, import-linter, package boundaries, the 1000-line cap), pytest (offline, with the
   per-package coverage floors), e2e (web app in Playwright, bench offline pipeline), Lua lint and suites (Docker),
   and TypeScript. The suite check runs in the pytest job:

   ```bash
   gh run list --branch main --limit 1
   uv run python -c "import bg3bench; from bg3bench import suite; print(bg3bench.__version__, suite.suite_version(), suite.check())"
   ```

   The last line must print `X.Y.0 bench-vX.Y []`.
2. Run the matrix from that commit. Every run's pins cite it, and a release holds runs of one suite version only.
3. Build the release folder (`results.json` from `bg3bench.results`, the leaderboard from `bg3bench.site.build`,
   the Inspect logs from `bg3bench.export_inspect`) and write what would be published, for review, without
   uploading:

   ```bash
   uv run --project packages/bench python -m bg3bench.publish <release_dir> --tag bench-vX.Y --hf-repo OWNER/NAME --dry-run <out_dir>
   ```

   A holdout scenario's run refuses the whole release. Before anything is public, scrub `<out_dir>` for personal
   paths and keys.
4. Tag and push the tag (the owner does this):

   ```bash
   git tag -a bench-vX.Y <commit> -m "bench-vX.Y"
   git push origin bench-vX.Y
   ```

There is no uploader yet: `bg3bench.publish` only writes the dry-run tree. A GitHub release and a Hugging Face
dataset come after the launch gate.
