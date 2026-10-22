# Contributing

Bug reports, fixes, new scenarios and runs with other MCP clients are all useful. The project is
a work in progress, so open an issue first for anything larger than a small fix, and we can agree on the approach
before you spend time on it.

## Proposing a change

- For a bug, open an issue with the bug report template: what you ran, what happened, what you expected, and the
  game build (`python3 tools/steam_paths.py buildid`). Session logs from `runtime/sessions/<run>/` help a lot.
- Work on a branch and open a pull request. `main` is protected (no direct pushes). While the overhaul runs
  ([docs/OVERHAUL-PLAN.md](docs/OVERHAUL-PLAN.md)), pull requests go into the `overhaul` branch; `main` takes only
  critical fixes, each also picked onto `overhaul`. Once `overhaul` has replaced `main`, pull requests go into `main`.
- The pull request says why the change is needed, what changed, and how you verified it (the exact commands and their
  results). Keep it focused on one problem.
- Commit messages are a plain sentence about the behaviour that changed. No AI attribution lines in commits, pull
  requests or comments: no `Co-Authored-By: Claude ...`, `Claude-Session:` or "Generated with ..." lines.
- Every fix comes with a test that fails without it. Tests must be deterministic: fake clocks, no sleeps, no network.

## Running the tests

Everything below runs offline, without the game, a GPU, Steam or an API key. You need
[uv](https://docs.astral.sh/uv/), Docker (the Lua suites) and Node 22 (the TypeScript packages and the e2e job).
The repo root is a uv workspace with one `uv.lock` (the bridge and `packages/harness`, `packages/bench`,
`packages/playthrough`); run `uv sync --locked` once.

Before you open a pull request, run what CI runs:

```bash
uv run tools/ci/task.py ci          # every CI job below, keeps going, prints a summary
uv run tools/ci/task.py --list      # the commands, without running them
```

Each task is one CI job (`.github/workflows/ci.yml`) with the same commands; you can run them one at a time:

| Task | CI job | What it runs |
| --- | --- | --- |
| `check` | Lint and types | ruff check, ruff format --check, the legacy lists, pyright, the dependency rule, the line cap |
| `test` | pytest (offline) | `uv run pytest -m "not live and not e2e" --cov`, then the per-package coverage floors |
| `e2e` | e2e | builds the web app, installs pi, then `uv run pytest -m "e2e and not live"` |
| `lua` | Lua suites | `bash tools/ci/lua_suites.sh`: every `tools/dev/test_*.sh` and `tools/vis/test_*.sh` in Docker |
| `ts` | TypeScript | per package (`packages/playthrough/web`, `tools/pi`): npm ci, `tsc --noEmit`, check, lint, vitest |

`test` takes about 10 minutes and a few GB of RAM. On a shared machine cap it, for example
`systemd-run --user --scope -p MemoryMax=8G nice -n 15 uv run tools/ci/task.py test`. The e2e browser tests also
need chromium's system libraries and ffmpeg, which CI installs with sudo: run
`uv run playwright install-deps chromium` once (it asks for sudo) if they fail to start the browser.

While you work, run only the tests for what you changed:

```bash
uv run pytest bridge/tests/test_compact.py          # one file
uv run pytest -m "not live and not e2e" packages/bench/tests
bash tools/ci/lua_suites.sh tools/dev/test_save.sh   # one Lua suite
```

The pytest markers are `unit` (pure logic), `integration` (one real component against fakes), `e2e` (the whole
offline pipeline) and `live` (needs the game, Steam or a paid model; never in CI). Tests that need something a fresh
clone lacks are skipped with the reason by the root `conftest.py`.

### The fake mod and the offline end-to-end tests

`bg3harness.testing` holds a fake of the BG3 mod: it speaks the mod's debug wire protocol, plays a scripted game
(gold, items, fights, dialogue, area changes) and records and replays sessions. The bridge's own client and the real
MCP server run against it:

```bash
uv run pytest packages/harness/tests/test_fakemod.py packages/harness/tests/test_bridge_e2e.py
```

The end-to-end tests run a whole pipeline on top of it: the benchmark (scenario, pi with a scripted stub model, the
bridge, the fake mod, grading, report) and the playthrough app (the launcher and the web app in Playwright). They
need the web build and pi installed first:

```bash
(cd packages/playthrough/web && npm ci --ignore-scripts && npm run build)
(cd tools/pi && npm ci --ignore-scripts)
uv run playwright install chromium
uv run pytest -m "e2e and not live"
```

Changes that need the running game (new tools, actions, UI reading) also need a live check. Say in the pull request
what you checked live and what you could only test offline.

## Code rules

- Python: match the surrounding code. Lines go up to 120 characters, with one ruff config at the root. Check your
  files with `uv run ruff check <files>` and `uv run ruff format <files>`, and format only the files you changed.
  pyright is strict on `packages/` and `tools/ci`, basic elsewhere.
- Lua: match the style of the file you edit. The mod is loaded as a loose folder, so a syntax error breaks it at
  load; the Lua tests run `luac -p` on the files they cover.
- Keep changes small and on topic. Don't refactor nearby code in the same pull request.
- A change that alters a score or what the agent sees goes into [CHANGELOG.md](CHANGELOG.md) under "Unreleased".

### The 1000-line cap and the shrink-only lists

No file (Python, TypeScript, Lua, shell; source or test) grows past 1000 lines; `tools/ci/line_cap.py` checks it.
Older files over the cap are in `tools/ci/line_cap_allowlist.txt` at their current length: they may only shrink, and
no file joins the list. Split a file before it reaches the cap.

Legacy code is also exempt from parts of the strict tooling through lists that may only shrink: the per-file ignores
and the format excludes in `ruff.toml`, the `ignore` list in `pyrightconfig.json`, and `# pyright: basic` lines in
files under a strict path. `tools/ci/legacy_lint.py` fails when an entry is no longer needed, so when your change
cleans up a listed file, remove its entry (`uv run python tools/ci/legacy_lint.py --needed` prints what the lists must
hold). New code never goes on a list.

### The dependency rule

New code goes to its package: `packages/harness` (game, bridge and model plumbing), `packages/bench` (the
benchmark) or `packages/playthrough` (the app). The harness imports neither of the other two, and the benchmark and
the app never import each other ([docs/TARGET-ARCHITECTURE.md](docs/TARGET-ARCHITECTURE.md)). No `sys.path` edits and
no file paths into another package's directory: import the package instead. `tools/ci/import_contracts.py`
(import-linter) and `tools/ci/test_boundaries.py` check this, and CI fails on a violation (`enforce-boundaries` under
`[tool.bg3ci]` in `pyproject.toml`).

## The fairness rule

The agent may only receive what a human player could see on screen at that moment, or remember having seen. Any
change to what the agent sees (tool results, events, tool descriptions, the rules in `play/`, the knowledge files)
must keep that true. Read [docs/FAIRNESS.md](docs/FAIRNESS.md) first.

- New state goes out through the perception gate and the UI allowlist in the mod, never straight from engine data.
- No hidden information in any form: story flags, internal ids, hidden DCs, the contents of closed containers, or
  routes over ground the party hasn't seen.
- Text the agent reads describes how the tools behave. It names no place, route, puzzle, item or encounter from a
  playthrough, and examples are made up.
- Add a test that shows the hidden case stays hidden, and point it out in the pull request.

If a change is a judgment call (the game shows something only in some situations, for instance), say so in the
pull request so it can be recorded in the "Known judgment calls" list of docs/FAIRNESS.md.

## License

By contributing you agree that your contribution is licensed under the MIT license in [LICENSE](LICENSE).
