# Quickstart

Three ways in: run your own model through the harness, plug your own MCP client into the bg3 server, or start the
playthrough app to watch a long run. All three need the game set up first: [SETUP.md](SETUP.md) (Linux, a Steam
copy of BG3 on the pinned build, bg3le built), then `python3 tools/bench_preflight.py` with no FAIL line.

Status on 2026-10-21: only Claude through Claude Code has played live. The pi client (any OpenAI-compatible
endpoint) is tested offline and through one real API call, not yet in the game. `--profile` and the spend caps
below are on the harness branch (`ovh/harness`) and not yet merged into `overhaul`.

## Test your own model

Every model on the ranked track plays through pi, one frozen client, so changing the model means changing one
file. You need an API key for an OpenAI-compatible endpoint (OpenRouter, DeepSeek, or your own).

### 1. A profile

Copy one of `profiles/*.toml` and change the model. For OpenRouter:

```toml
# profiles/my-model.toml
client = "pi"
model = "vendor/model-id"          # the OpenRouter slug
effort = "high"

[endpoint]
provider = "openrouter"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY" # the name of the variable, never the key

[routing]
only = ["vendor"]                  # pin one provider: no mid-run switches, no surprise quantization

[model_info]
input = ["text", "image"]
reasoning = true
context_window = 200000
max_tokens = 32000
cost = { input = 1.0, output = 4.0, cache_read = 0.1 }   # USD per million tokens, pi's own estimate only
```

Put the key in the repo's gitignored `.env` (`OPENROUTER_API_KEY=...`) or in the environment. The runner reads it
from there and never writes it to a file or a log. `runner/profiles.py` documents every field; a profile with a
field its client doesn't use is refused. `profiles/deepseek-flash.toml` is a direct DeepSeek example (no
OpenRouter, so no `[routing]`).

### 2. One command

```bash
python3 tools/bench_preflight.py
python3 runner/play.py run --scenario nautiloid --runs 3 --playthrough naut-my-model \
    --profile profiles/my-model.toml --max-cost 5 --no-keeper --no-judge
python3 runner/play.py runs --playthrough naut-my-model
```

- `--scenario nautiloid` plays the opening chapter from a new game until the party escapes the Nautiloid. It is
  the only scenario with a live history; `python3 runner/play.py scenarios` lists which others are ready.
- `--max-cost` is required for pi: the runner kills the play process when the billed cost reaches it and records
  the run as over budget. A series keeps the caps of its first run.
- `--no-keeper` is required for pi for now (the memory keeper can't run through pi yet). `--no-judge` skips the
  persona judge, which would otherwise run on Claude after each run.
- `--plan` prints what the series would freeze and the first session's command without starting anything.

The matrix runner (`packages/bench`, `bg3bench.matrix`) queues the same command over models x scenarios x
conditions x repeats under one budget. It has no command line yet.

### What it costs

Cost is what the endpoint bills for the tokens. The numbers we have are for Claude Opus 5.5 through Claude Code on
the Nautiloid, 9 to 13 minutes per run: a mean of $4.34 per run in the first series, $3.30 in the second and $1.46
in the third, as the harness got better. No model has played through pi yet, so there is no measured cost for it.
The plan's estimate for the pi pilot is $40-60 for 5 models x 3 Nautiloid runs.

What bounds your spend:

- `--max-cost` per run, from the billed cost as it streams.
- `runtime/budget.json`, `{"cap": 20, "spent": 0}`, a global budget across runs: a paid run refuses to start once
  it is used up, and its per-run cap is cut to what is left. Every run adds its cost when it ends.
- The worst case for a series is `--runs` x `--max-cost`.

### Where results land

Everything is under `runtime/sessions/` (or `$BG3AI_RUNTIME/sessions/`). Run 1 of the series is the playthrough
name itself, the later runs are `<name>-r2`, `<name>-r3` and so on.

| File | What |
|---|---|
| `<name>/series.json` | what the series froze: scenario digest, caps, client, model, pins (game build, bg3le, mod, repo commit) |
| `<name>/runs.jsonl` | one record per run: outcome, wall time, cost, tokens, failures by class |
| `<run>/Sxxx.events.jsonl` | the normalized event log of each play process |
| `<run>/Sxxx.log` | the readable session log |

`python3 runner/play.py runs --playthrough NAME [--failures]` prints the table. The benchmark's per-run manifest
and result files, the series report (`python3 -m bg3bench.report`) and the leaderboard read the same data once the
harness writes them per run ([packages/bench/README.md](../packages/bench/README.md)).

Every published result cites the benchmark tag (`bench-vX.Y`) and the harness version
([CHANGELOG.md](../CHANGELOG.md)). Don't compare runs made at different pins: the runner refuses a run of a series
whose pins changed, unless `--allow-version-drift`, which it records.

## Bring your own MCP client

The bg3 server is a normal MCP server. Any MCP client can play through it; the runner just automates launching the
game and the client.

```bash
runner/launch_game.sh --continue                              # the game with the mod (or start it with the runner)
uv run --project bridge bg3ai-mcp                             # stdio, one client
uv run --project bridge bg3ai-mcp --transport http --host 127.0.0.1 --port 8770   # MCP streamable HTTP at /mcp
```

`play/.mcp.json` is a working stdio config (it is what Claude Code uses). Give the call a long timeout: some tools
wait for the game, and the config uses 110 seconds.

- Rules. The server sends the rules of play in its MCP `instructions`, at the top of the first `session_start`,
  as the `rules` prompt and as the `bg3://rules` resource, so the client needs no files from the repo.
  `BG3AI_PROMPT_CONDITION=rules+coaching` adds the coaching text; `BG3AI_SERVE_RULES=0` turns the rules off (Claude
  Code reads them from `play/CLAUDE.md` instead).
- Tools. `BG3AI_TOOLS_OFF=wiki,reload` (groups: `memory`, `wiki`, `reload`, `saves`, `screenshots`,
  `raw_input`, or single tool names) removes tools at startup; `BG3AI_TOOLS` keeps only the groups named plus the
  game tools. An unknown name stops the server, so a typo can't serve the wrong set.
- Memory. The memory tools reach only `$BG3AI_MEMORY_DIR` (default `play/memory`) and the read-only
  `play/knowledge/`. Create a memory with `python3 tools/memory/new_playthrough.py NAME`.
- Your client's own tools. Switch off its file, shell and web tools, or the comparison isn't fair. If you can't,
  run it in the container sandbox, where it can reach only the bg3 server and its model API:
  [tools/container/README.md](../tools/container/README.md).

Results from your own client are reported as a separate "bring your own agent" condition, apart from the ranked
track. The fairness rules every client plays under are in [FAIRNESS.md](FAIRNESS.md).

## The playthrough app

The app plays the whole game for people to watch, with a launcher, a live view, the memory, a timeline and
recording.

```bash
(cd packages/playthrough/web && npm ci && npm run build)       # once
uv run --project packages/playthrough bg3play-serve --runtime runtime --playthroughs playthroughs --supervise --record
# http://127.0.0.1:8780/
```

- Launcher (needs `--supervise`): pick the profile, client, model, effort, character brief, roleplay strictness,
  difficulty, session length and total hours, then start, stop or resume. Each session is a `runner/play.py run`;
  the supervisor runs the memory keeper between sessions, nudges the agent after two sessions without progress and
  stops after three crashes in a row. Stop is soft: the running session finishes first.
- Watch, Memory, Timeline: the screen, the agent's reasoning, the party and cost; the memory files and their
  commits; the choices and fights.
- Recording (`--record`): ffmpeg records the game window into MP4 segments under `runtime/video/`, with a 20 GB
  cap (`--video-cap-gb`) and a pause when RAM or heat runs high. It needs the game's `DISPLAY` and `XAUTHORITY`.
  The NVENC encoder is the default; `--encoder libx264` works without an NVIDIA GPU.
- Overlay and Replay: `/overlay` is a transparent 1920x1080 page for OBS; Replay steps through a session's
  events beside its recorded video.

The server binds to localhost. From another machine, start it with `--host` and `--token` (or `$BG3PLAY_TOKEN`);
writes then need that bearer token. More in [packages/playthrough/README.md](../packages/playthrough/README.md).

The older dashboard (`python3 runner/dashboard/serve.py --port 8765`) still works and shows the same runs.
