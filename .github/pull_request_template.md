<!-- Base branch: `overhaul` while the overhaul runs, `main` after (CONTRIBUTING.md). -->

## Why

## What changed

## How it was verified

The exact commands and their results. Say what was tested live in the game and what only offline.

- [ ] `uv run tools/ci/task.py ci` passes (or name the job that fails and why it isn't this change).
- [ ] Each fix has a test that fails without it; tests are deterministic (no sleeps, no network).
- [ ] No file over 1000 lines; no new entry on a legacy list (line-cap allowlist, ruff ignores, pyright ignore).
- [ ] New code is in its package and keeps the dependency rule (no `sys.path` edits, no cross-package paths).
- [ ] No AI attribution lines in commits or this description (`Co-Authored-By: Claude`, "Generated with ...").
- [ ] If this changes what the agent sees or can do, it follows docs/FAIRNESS.md and has a test for the hidden case.
