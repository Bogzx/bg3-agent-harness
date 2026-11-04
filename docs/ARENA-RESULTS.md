# Arena results

How the arena's results table is computed (`bg3bench.arena.results`), the analysis rules fixed before the 40-seed
run, and the output fields a results page reads.

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
| `failures[]` | per model: `player`, `n_matches`, `client_faults`, `rate`, `causes {cause: n}` |
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
