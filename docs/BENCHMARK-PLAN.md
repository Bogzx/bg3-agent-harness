# Benchmark plan: from one Claude run to a multi-model suite

The plan for turning the harness into a benchmark that any model can play through OpenRouter. Decided 2026-10-21
(user); nothing in it has run yet unless marked. The current protocol is [BENCHMARK.md](BENCHMARK.md), and the bar
for a fair cross-model result is [BENCHMARK-BIAS.md](BENCHMARK-BIAS.md).

## What the benchmark is for

Three questions that no other game benchmark answers for a CRPG:

1. How much of an agent's success comes from the harness and how much from the model? Claude Code went 9/9 on the
   Nautiloid. The same model through a neutral client is the first measurement.
2. How well do models fight turn-based D&D battles with real dice, and how well do they reach open goals in a 3D
   world they mostly see as text?
3. What do models choose when the story makes them choose, and how do they get there (quests, trade, theft,
   murder)?

No published BG3 or CRPG LLM benchmark exists as of 2026-10-21. The closest are CivBench (Civ VI over a 76-tool
MCP server, [arXiv 2609.02459](https://arxiv.org/html/2609.02459)), SpireBench (Slay the Spire, fixed harness,
held-out seeds, dollar cost per run, [arXiv 2610.12596](https://arxiv.org/html/2610.12596v1)) and PokéAgent
(milestones with time, tokens and cost each, [arXiv 2603.15563](https://arxiv.org/html/2603.15563)).

## Harness: one frozen client for every model

- Every model, Claude included, plays through **pi** (`@earendil-works/pi-coding-agent`, pinned version) as a
  fourth client in `bg3harness/clients.py`, talking to **OpenRouter** Chat Completions. Changing the model means
  changing the OpenRouter slug and nothing else.
- pi settings that must be overridden: auto-compaction off, MCP exposure `direct` (not codemode), tool calls run
  one at a time in the order the model sent them, MCP timeout 110 s. The built-in coding tools stay off
  (`--tools 'mcp__bg3__*'`).
- One OpenRouter provider pinned per model (`provider.only`, `allow_fallbacks: false`, `require_parameters: true`,
  `session_id` = episode id). Unpinned routing can switch provider mid-session, which breaks prompt caching, and
  can land on fp4 hosts.
- Cost comes from OpenRouter's billed `usage.cost` on every response, not from pi's catalog estimate. The cache-hit
  rate (`cached_tokens / prompt_tokens`) is tracked per request: a cache that misses makes Claude and GPT runs cost
  3-5 times more.
- Claude goes through pi with an API key (OpenRouter, or BYOK with an Anthropic key). pi's Claude Pro/Max login
  is not used: Anthropic's terms reserve subscription OAuth for Claude Code and its own apps.
- `claude -p` on the subscription stays as the Claude Code reference condition (the existing `ClaudeClient`).
- If pi fails the offline checks (phase 0), the fallback is a small Python loop on the OpenAI SDK plus the `mcp`
  client, writing the same event log.

## Scenario families

All scenarios start from a fixed state (a new game or a neutral start save: generic character name, standard
build), end on a predicate read from game state, and have caps. The agent's claims are never graded.

### 1. Fights (the core)

Set-piece battles from a save just before the fight. Turn-based combat waits while the model thinks, so a slow
model is not punished, the dice give real variance between runs, and knowing the wiki helps less than tactics.

- Graded: all hostiles dead or fled, no party wipe. Diagnostics: HP left, rounds, spell slots and consumables used.
- Candidates come from claude-run-2's 149 recorded fights (`tools/logs/fights.py`).
- Target size: 10-15 fights.

### 2. Sandbox goals

One open goal from a save, and the model works out how to reach it.

| Scenario | Start | Goal | Notes |
|---|---|---|---|
| `act1_gold_1000` (first) | Act 1, early | party gold ≥ 1000 | method is free: sell, quest, steal, pickpocket, kill traders |
| `act1_level_3` | Act 1, level 1 party | party level 3 | |
| `act1_recruit_3` | after the crash site | 3 companions recruited | |
| `act1_goblin_camp_no_fight` | Act 1 | reach the goblin camp without a fight | |
| `act1_get_item` | Act 1 | a named item in the party's inventory | needs the item milestone kind |
| `honour_survive_30m` | Act 1, Honour Mode | nobody dies for 30 minutes | |

How these are scored:

- Three numbers per run: goal reached within the cap (yes/no), time to the goal, and a progress curve (for gold:
  party gold at 10, 20 and 30 minutes). The curve is a continuous score, so 3-5 runs separate models much better
  than pass/fail alone.
- The method is recorded, not restricted. Anything the game allows is allowed, and how the model got there is part
  of the result ("sold loot and did quests" vs "pickpocketed Arron"). An optional `honestly` variant of a goal adds
  "without theft or murder" to the brief, to see which models change behaviour.
- The target is calibrated so the strongest model reaches it about half the time within the cap. One Opus pilot
  run sets it.
- These are exploration-heavy, so the game runs in real time while the model thinks. Latency is reported next to
  the result, and the family is treated as less clean than fights until the pause question below is answered.
- Estimated cost (not measured): about $10-30 per run on Opus at a 30-45 minute cap, under $1 on the cheap models.

Needs: a numeric milestone kind in `bridge/src/bg3ai/milestones.py` (gold, level, companion count already exists).
The mod already reports party gold (`bridge/src/bg3ai/delta.py:209`, `bridge/src/bg3ai/economy.py:451`).

### 3. Choices

Saves just before about 15 big story decisions (the grove, Gale, the tadpole, the Emperor, ...). Every model plays
the scene; the choice and the reason it gave are recorded. Not scored right or wrong, only compared across models,
with a neutral brief and with a given persona. claude-run-2's `decisions.md` lists the decision points.

### 4. Exploration legs and puzzles

`nautiloid` (ready), `crash_site_grove`, `act2_last_light`, Faith-Leap. Puzzles are reported as a separate
"knowledge" subset because walkthroughs contain the answers. Full-game runs stay showcases, never leaderboard
entries.

## Scoring and statistics

- Headline: first-try success with no reload, graded from game state. `load_game` is removed from the main track's
  tool list rather than forbidden in the prompt. "Success within R reloads" is a separate, declared condition.
- Per scenario: k/n with a Wilson 95% interval (2/5 is consistent with 12-77%). Next to it, pass^k for consistency.
- Aggregate: mean of per-scenario success rates, interval from a two-level bootstrap (scenario, then run). A
  head-to-head claim needs a paired per-scenario difference whose interval excludes zero; otherwise "not separable
  at this budget".
- Scale: 20-30 scenarios × 3-5 runs beats many runs of a few scenarios. Run counts, scenarios and the main metric
  are written down before any model runs.
- Cost and time on their own axes, never in the score: dollars per run and per success, tokens (input, cached,
  output), wall time, state-changing game actions. Shown as a success-vs-cost plot.
- Caps are identical for every model, set as tokens at a reference price (so a cheap model doesn't get more
  thinking for free) plus a time or round cap. Cumulative cost per step is logged, so "success if stopped at $2,
  $5, $10" comes from the same runs.

## Validity and fairness

- Each scenario has an oracle run (solved with the same tools), a do-nothing baseline and a random baseline. A
  baseline above ~5% means the grader is broken.
- A fairness regression test per tool: query something known to be hidden in each start save and expect a refusal.
- A crash or API failure makes the run invalid and it is rerun, never resumed and never scored as a loss. The
  invalid rate is published.
- Every surprising success is reviewed; a confirmed harness exploit counts as a failure and is published as an
  exploit rate.
- Wiki tools are off in the main track; wiki-on is a separate condition. A closed-book quiz per scenario ("where do
  you make money fast in Act 1?") shows how much of a result comes from memory.
- The agent's text is split into rules and coaching (BENCHMARK-BIAS.md) before cross-model results are published;
  rules-only is run as its own condition.
- Every run records a manifest: game build, bg3le, mod, harness commit, pi version, system-prompt hash, scenario
  digest, start-save hash, OpenRouter slug, provider requested and provider served per call, reasoning effort,
  caps, price-sheet date.
- Every run is screen-recorded.

## Phases

| Phase | Scope | Done when | Spend (est.) |
|---|---|---|---|
| 0 | pi client and offline checks against the real tool list and a stub MCP server: 94 tools exposed directly, sequential calls, no compaction, screenshots reach the model (canary), `usage.cost` matches `/generation`, provider is the pinned one | all checks pass, or switch to the Python loop | < $5 |
| 1 | Nautiloid on pi: Opus 5.5, GPT-5.6 Terra, Gemini 3.1 Pro, Kimi K3, DeepSeek V4.1 Flash, 3 runs each, recorded | 15 valid runs; Claude-on-pi vs the 9/9 Claude Code runs gives the harness effect | ~$40-60, ~3 h |
| 2 | rules/coaching split, neutral start saves, numeric and item milestone kinds, baselines, fairness tests; first fights, `act1_gold_1000` pilot, first choice scenes; variance pilot (2 models × 10 scenarios × 6 runs) | each scenario has an oracle solve and baselines; run count per scenario fixed from measured variance | ~$150-400 |
| 3 | suite v1: 20-30 scenarios across the four families, a private holdout, Pareto plot, per-run manifest, leaderboard version tied to the harness hash | first published leaderboard | ~$800 per frontier model, far less for cheap ones |

Each paid phase pilots on one or two models first and does not scale without the owner's go-ahead.

## Open questions

- Can the mod hold the game in BG3's turn-based mode (or pause it) outside combat while the model thinks? That
  would make exploration and sandbox goals fair across fast and slow models. Unverified.
- Can the Script Extender seed the dice, so every model faces the same rolls? Can it perturb encounters (shuffle,
  rename) to measure memorisation? Unverified.
- Does pi's `compaction.enabled: false` also stop its one-time compact-and-retry on context overflow?
- Can BG3 save files be published with the benchmark?
- Throughput: one game per machine, about 5 Nautiloid runs an hour on dev2.
