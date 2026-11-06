# Arena releases

One file per released arena (`<arena>.json`, kind `bg3bench.arena_release`). A release fixes the arena definition and
the matrix by sha256 and the pins its published matches played with. An edit to the arena or the matrix file is a new
arena, not an edit of the release.

- `arena-v0.json`: the seat run (matrix `arena-v0-seat-o1`, docs/ARENA-RESULTS.md). `packages/bench/arenas/arena-v0.json`
  stays byte for byte as that run played it. Code after the builds check (arena/builds) wants each kit's race part to
  name its race, so this file validates only with the code its pins name. Re-grading the published seat-run table
  (`runner/play.py arena-matrix --matrix packages/bench/matrices/arena-v0-seat-o1.toml --table`) therefore needs a
  checkout of the release's pinned commit e7e0fcf1.
- `arena-v0.1` (`packages/bench/arenas/arena-v0.1.json`, no release yet: it gets one with pins when it is played) is
  arena-v0 with one identity field added: each kit's race part names its race (`race.race`, the pair's own race).
  Nothing a twin plays with differs: boosts, passives, spells, gear, spawns, seed and caps are v0's
  (`test_arena_v0_1_is_arena_v0_with_the_race_identity_fields`). New matrices and the builds work use v0.1.
