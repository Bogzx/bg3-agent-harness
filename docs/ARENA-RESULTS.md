# Arena results

How the arena's results table is computed (`bg3bench.arena.results`), the analysis rules fixed before the 40-seed
run, the match rules decided on 2026-11-05, the output fields a results page reads, and the arena v0 seat run's
result.

## Arena v0 seat run (2026-11-04)

Before models are compared, the seat run checks whether the turn order gives one side an edge.

**Setup.** Arena `arena-v0` (`packages/bench/arenas/arena-v0.json`): two against two at the crash site, a level 2
Fighter and Rogue per side, mirror twins with the same race, kit and gear (side A Ash and Cedar, side B Birch and
Dune), round cap 20. One model plays both sides: `profiles/deepseek-flash.toml` (DeepSeek flash through pi, effort
high). Matrix `packages/bench/matrices/arena-v0-seat-o1.toml`: one match per seed, seeds 1-20 counted. Code at
`arena/int-v010b` e7e0fcf1; `arena-v0.json` and the matrix file are the same in this tree. API cost: $0.77.

**Turn order (O1).** Both sides share one turn group and take turns one character at a time. The opening side is the
side whose character acts first in round 1, by initiative. It is not alternated across matches: side A opened 12 of
the 20 (two-sided binomial p = 0.50 against 0.5, so the opening side is not lopsided).

**Attempts.** 22 attempts, 20 counted. Not counted, both re-run on the same seed: seed 3 attempt 1 (stopped before
the fight, no fight recorded) and seed 13 attempt 1 (the game closed during round 1; graded invalid, reason fault).
All 20 counted matches ended in a wipe, with no draws and no cap ends, and all 20 passed the void checks (every turn
followed O1). Rounds per match: 1 to 7, median 4.

**Result** (the pre-registered tally, `tools/dev/arena_seat_tally.py` on `arena/seat` 64a34197, n = 20):

| Measure | Value |
|---|---|
| Opening side won | 8 of 20 (0.40), Wilson 95% interval [0.219, 0.613] |
| Side A won | 8 of 20, side B 12 |
| Verdict | `underpowered`: no first-mover edge detected, n = 20 is too small to rule out a moderate one |

The decision rule for n = 20 to 40 was fixed after seeds 1-2 and before seeds 3-20 were played (website repo
`ops/logs/arena-seat-sims.md`, "Pre-registered rule for the resumed run"; `tools/dev/arena_seat_tally.py` on
`arena/seat` 64a34197). At n = 20 the opening-side trigger is k = 16: P(X >= 16) is 0.019 for X ~ Bin(20, 0.55),
O1's simulated opening-side rate. 8 is far below it, and side A's or B's wins (8, 12) are below it too. Below n = 30
the rule gives no "keep" or "ambiguous" call, only `underpowered` with the interval. The power at n = 20 is 0.24 if
the opening side truly wins 70% of matches and 0.63 at 80%. So the run rules out a large first-mover edge only
loosely and says nothing about a small one.

Decision: arena v0 keeps O1. The alternative, a snake order (the opening side flips each round), was to be switched
on only on a clear opening-side edge, and the opening side won fewer than half of these matches.

Per match (seed, opening side, winner, rounds):

| Seed | Opened | Won | Rounds | | Seed | Opened | Won | Rounds |
|---|---|---|---|---|---|---|---|---|
| 1 | A | B | 6 | | 11 | A | B | 4 |
| 2 | A | B | 4 | | 12 | A | B | 4 |
| 3 | A | A | 5 | | 13 | A | A | 4 |
| 4 | B | A | 2 | | 14 | B | B | 3 |
| 5 | B | B | 4 | | 15 | B | B | 7 |
| 6 | B | A | 2 | | 16 | A | B | 5 |
| 7 | A | B | 3 | | 17 | A | A | 6 |
| 8 | B | A | 5 | | 18 | B | A | 3 |
| 9 | A | B | 3 | | 19 | A | B | 2 |
| 10 | A | A | 2 | | 20 | B | B | 1 |

## Pre-registration (2026-11-04, before the 40-seed run)

These rules were set before the matches they apply to were played, so that no later choice can be read as a refit
picked for its outcome.

1. **The seat term is fit on self-matches and patterned matches.** The Bradley-Terry fit's first-seat factor uses
   every rated game whose round 1 has a pattern, plus each self-pairing's first valid match with a pattern. In a
   self-match the two strengths are the same model's and cancel, so the game says something about the seat and
   nothing about the model: it moves the seat factor and no model's strength, and a model seen only in self-matches
   gets no rating. A self-pairing's later valid matches replay the first one (same seed, mirror twins, same model)
   and stay out (REVIEW-7 f2, the `_distinct` rule). The bootstrap resamples those self-matches as their own units
   next to the rated pairings.
2. **Schedule.** Pairings are played as a balanced round robin until every pair has MIN_PAIRINGS (15) complete
   pairings. After that, extra pairings go only to adjacent pairs in the rating order whose Elo intervals overlap.
   Every match is logged and graded, crashes and invalid matches included (the `failures` table), and no match is
   re-run because of its result: a match is re-run only when it is invalid (`results.validity` or a fault end), and
   then always.
3. **What is reported.** Per pair: pentanomial counts, cap draws and an exact sign test, ahead of the ratings.
   Ratings are provisional below 50 pairings, carry rank ranges from the bootstrap, and are refit with 0.5 and 2
   virtual draws. A rating that moves by more than half its interval under those priors is flagged.

## Dice: per seat, the same in both matches of a pairing (decided 2026-11-04)

Every match of an arena applies the arena's seed before the fight (ArenaSetup -> `Scenario.set_seed`: one base seed,
each dice stream reset to its start). A face depends on its position in the stream, not on the action that rolls it.
So the two matches of a pairing open with the same dice for each seat (side and character), even when the models
act differently: in window 8, seed 7, the first five attack naturals of m2 were m1's, in the same order. The replay
lasts as long as the rolls come in the same order. A matrix's repeats use different seeds (seed + repeat - 1).

This is kept as a paired design (orchestrator, 2026-11-04, harness survey #4). It has these consequences:

- **Between models, luck cancels within a pairing.** Each model plays each seat once with that seat's dice, so a seat
  with lucky dice helps both models once. The pairing is the resampled unit in every bootstrap and the unit of the
  sign test, so nothing treats a pairing's two matches as two independent samples there.
- **Every rate interval resamples pairings.** A pairing's two matches are not independent. For a pair's
  `win_rate`, shared dice push a model's two results in opposite directions, so an interval that counted matches
  would be too wide. For `first_actor`, `favoured` and `by_pattern`, the shared initiative and dice stay with the
  same side and both matches lean the same way, so an interval that counted matches would be too narrow. Each of
  these intervals, like `side_a`'s, is a percentile bootstrap over pairings: a pairing's matches in the subset are
  drawn together. With one pairing the interval is that pairing's rate.
- **The seat term can't tell seat from luck within one seed.** The seat side keeps its dice in both matches, so a
  seed whose dice favour the seat side counts as seat advantage twice. Seeds are independent, so luck averages out
  only across seeds, and the seat interval (a bootstrap over pairings) reflects that.
- **Self-matches count once per seed.** A self-pairing's second match replays the first one (same model, same twins,
  same dice), so only its first valid match is used (`_distinct`, REVIEW-7 f2). The seat measurement plays one match
  per seed (`single = true` in the matrix). Its n is the number of seeds.

## Match rules decided on 2026-11-05 (owner)

### A6: context-length errors

The side that caused a context-length error forfeits. Which end applies depends on what filled the context:

| The error | End | Valid | Cause |
|---|---|---|---|
| Overflow after the side's first tool call, in any of its processes | `forfeit`, at the first error | yes, not rerun | `context_overflow` |
| Overflow before the side's first tool call | `client_fault`, after one resume | no, rerun | `kickoff_overflow` |
| Any other API error, 400s and invalid-request errors included | `client_fault`, after one resume | no, rerun | `api_error` |

After the first tool call, the model's own play has grown the conversation, and a resume would send the same full
conversation, so there is none. Before it, the context holds only the harness's prompt, so the model caused nothing. A
malformed request can come from our client, so it says nothing about the model's play either.

Only text that names a full context counts in a match (`pi_client.context_full`). The list is short on purpose: a wrong
forfeit is a valid loss, and a miss is only a rerun. "Too many tokens" is not on it, because throttles say it, so a
provider's real overflow in those words ends as `client_fault`. Neither is a 400 on a parameter such as `max_tokens`.
Anthropic's "input length and `max_tokens` exceed context limit" is on it. Bench and playthrough runs keep the parser's
wider `OVERFLOW_RE`, because there an overflow only ends the session.

One known misread: "maximum context length is 131072 tokens. However, you requested 140000 tokens (2000 in the messages,
138000 in the completion)" counts as a full context, though `max_tokens` caused it. A profile with a bad `max_tokens`
hits it on the first call, before any tool call, so it ends as `kickoff_overflow`, not a forfeit.

None of the 26 paid matches on record had a context-length error.

### R4: money caps

A model's cap per side per match is at least 3x its measured per-side p95, per arena. `tools/dev/arena_caps.py` computes
it from run folders. The sample is every side of a match that ran to its end, over-budget matches included, because
leaving those out would drop the long matches the cap has to allow for. Matches the game or a client cut short are left
out. Costs are taken at list price, because DeepSeek bills half off-peak and a cap has to hold at peak. A model needs
20 sides on an arena to get a cap: below that, the p95 is just the sample's maximum.

An over-budget match is still invalid and is not rerun. The results publish each model's count (`failures[]`:
`over_budget`, `over_budget_rate`) and flag a rate above 5% (`over_budget_flag`). A rerun with a doubled cap is not
approved; a flagged model is the reason to ask for it.

Measured on 2026-11-05 from the 26 paid matches on record (the seat run and the crash-companions pilot, 52 sides):

| Model | Arena | Sides (matches) | Cut short | Billed p95 | List p95 | Max | Cap | Over budget |
|---|---|---|---|---|---|---|---|---|
| DeepSeek flash | arena-v0 | 40 (20) | 1 | $0.0386 | $0.0606 | $0.0719 | $0.19 | 0 of 21 |
| DeepSeek flash | crash-companions-v1 | 4 (3) | 2 | $0.0351 | $0.0595 | $0.0595 | too thin | 0 of 5 |
| Claude Haiku 5.5 | crash-companions-v1 | 2 (2) | 2 | $0.0073 | $0.0073 | $0.0073 | too thin | 0 of 4 |

Opus 5.5 and Sonnet 5.5 have no arena matches on record, and Haiku has none on arena-v0, so none of them has a measured
cap. The numbers in `arena-v0-pilot.toml` for those three are estimates that reserve money for the pilot. They get
measured caps from the pilot's own matches. arena-v0.1 is arena-v0 plus each kit's race id, the same fight, so
DeepSeek's cap carries over to it.

The 40 sides come from 20 matches of one model against itself, and the two sides of a match run long or short together,
so the p95 rests on fewer independent samples than 40. 87% of those responses were billed off-peak.

### R5: what the turn cap counts

Two rules, as played:

- Thinking during the other side's turn is free. The turn cap counts model time on a side's own turn only.
- Endpoint latency counts as model time. The clock runs from a tool result to the model's next tool call (`ModelClock`),
  so a slow endpoint or a long time to first token uses up the turn, and nothing is subtracted for it.

Stream check on 2026-11-05, over the same 26 matches (fault attempts included). A model window is the `ModelClock`
span in a side's event stream, and a phase runs from one `arena_turn` to the next:

| Model | Arena (turn cap) | Sides | Model time | Off-turn | Longest off-turn stretch |
|---|---|---|---|---|---|
| DeepSeek flash | arena-v0 (90 s) | 42 | 7038 s | 273 s (3.9%) | 11.1 s |
| DeepSeek flash | crash-companions-v1 (60 s) | 6 | 1476 s | 219 s (14.8%) | 20.6 s |
| Claude Haiku 5.5 | crash-companions-v1 (60 s) | 4 | 180 s | 85 s (47.2%) | 6.9 s |

No off-turn stretch reached a turn cap, and the longest was about a third of one. Haiku's share is high because its
total is small: 180 s over four sides, none of it in a long stretch. On this data there is no case for charging
off-turn generation to the next turn. The data has no Opus or Sonnet, so the check should be repeated on the v0 pilot's
streams. `arena_turn` times are stamped at the runner's poll, so a boundary
can be off by up to one poll.

## Output

`results(matches, seed, b=2000, alpha=0.05, min_pairings=15, attempts=None)` returns one JSON-ready dict. Rates
count a draw as half a win. Intervals are `[lo, hi]` at `1 - alpha`.

| Field | Content |
|---|---|
| `seed`, `b`, `alpha`, `min_pairings` | the inputs |
| `counts` | `matches`, `invalid`, `self`, `incomplete`, `pairings` (complete pairings) |
| `pairs[]` | one row per model pair over complete pairings, below |
| `side_a` | side A's win rate over valid matches: `n_matches`, `n_pairings`, `rate`, `ci` (bootstrap over pairings) |
| `by_pattern{}` | per round-1 pattern (`"none"` without one): `n_matches`, `n_pairings`, `side_a {rate, ci}`, `first_actor {rate, ci}` or null |
| `first_actor`, `favoured` | `{n_matches, n_pairings, rate, ci}`: round 1's first actor's and pair side's win rate |
| `seat` | `log_factor`, `p_favoured` (the seat side's win chance between equal models), `ci`, `n_games`, `n_self_games`; null with no rated pair |
| `prior` | `priors` ([0.5, 2.0] virtual draws), `max_shift` (largest Elo move of any rated model); null with no rated pair |
| `failures[]` | per model: `player`, `n_matches`, `client_faults`, `rate`, `causes {cause: n}`, `over_budget`, `over_budget_rate`, `over_budget_flag` (over 5%) |
| `models[]` | one row per model, below; rated rows first by Elo |

A `pairs[]` row, from the first model's (`a`, alphabetical) side:

| Field | Content |
|---|---|
| `a`, `b` | the two models |
| `won`, `split`, `lost` | pairings with more, equal or fewer than 1 of the 2 points |
| `pentanomial` | pairings by points: `{"2", "1.5", "1", "0.5", "0"}`; "1" holds both a split by wins and two draws |
| `w`, `d`, `l` | matches won, drawn, lost |
| `draws` | cap draws by reason: `{round_cap, wall_cap}` |
| `win_rate`, `ci` | the match win rate and its interval (bootstrap over the pair's pairings) |
| `sign_p` | exact two-sided sign test on `won` vs `lost` (splits left out); 1.0 with neither |
| `n_pairings`, `n_matches` | sizes |
| `rated` | `n_pairings >= min_pairings`; else a pilot row |
| `provisional` | rated with fewer than 50 pairings |

A `models[]` row:

| Field | Content |
|---|---|
| `player` | the model |
| `n_matches`, `n_pairings`, `points`, `w`, `d`, `l` | over its complete pairings |
| `rated` | it is in a rated pair |
| `bt`, `elo`, `elo_ci` | log strength, Elo (400 log10, centred on 1500 over the rated models) and its bootstrap interval |
| `provisional` | rated with fewer than 50 pairings in rated pairs |
| `rank`, `rank_ci` | 1 = highest Elo; the range of its rank over the bootstrap replicates (whole ranks; overlapping ranges read as tied) |
| `prior_shift`, `prior_flag` | its largest Elo move under the two priors; true when that move is over half its interval |

An unrated model has `bt`, `elo`, `elo_ci`, `rank`, `rank_ci` and `prior_shift` null, and `provisional` and
`prior_flag` false.
