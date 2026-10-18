# Contributing

Bug reports, fixes, new scenarios and runs with other MCP clients are all useful. The project is
a work in progress, so open an issue first for anything larger than a small fix, and we can agree on the approach
before you spend time on it.

## Proposing a change

- For a bug, open an issue with the bug report template: what you ran, what happened, what you expected, and the
  game build (`python3 tools/steam_paths.py buildid`). Session logs from `runtime/sessions/<run>/` help a lot.
- For a fix, open a pull request against `main`. Say why the change is needed, what changed, and how you verified it
  (the exact commands and their results). Keep it focused on one problem.
- Every fix comes with a test that fails without it. Tests must be deterministic, with no timing races.

## Running the tests

All of these run offline, without the game. The Lua tests run in Docker.

```bash
# bridge (the MCP server)
(cd bridge && uv run python -m unittest discover -s tests)

# runner, dashboard and memory tooling
python3 -m unittest discover -s runner -p 'test_*.py'
python3 -m unittest discover -s runner/dashboard -p 'test_*.py'
python3 -m unittest discover -s tools/memory -p 'test_*.py'

# the Lua mod
for t in tools/vis/test_lua.sh tools/dev/test_*.sh; do "$t" || echo "FAILED: $t"; done
```

The full set takes a while and some files use a fair amount of RAM, so while you work, run the test files for the
parts you changed, for example `(cd bridge && uv run python -m unittest discover -s tests -p test_compact.py)`.

Changes that need the running game (new tools, actions, UI reading) also need a live check. Say in the pull request
what you checked live and what you could only test offline.

## Code style

- Python: match the surrounding code. Lines go up to 120 characters. Check your files with
  `uvx ruff check --line-length 120 <files>` and format only the files you changed.
- Lua: match the style of the file you edit. The mod is loaded as a loose folder, so a syntax error breaks it at
  load; the Lua tests run `luac -p` on the files they cover.
- Keep changes small and on topic. Don't refactor nearby code in the same pull request.

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
