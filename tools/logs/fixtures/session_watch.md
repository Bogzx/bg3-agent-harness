# Session watch: fixes to confirm in the first play sessions


Fixture for tools/logs/test_review_session.py: rows copied from docs/SESSION-WATCH.md on 2026-10-17 (kept fixed so
the tests don't move with the live file).

## UI, clicks, rolls (se)
| item (BACKLOG) | happens when | PASS signature | FAIL signature |
|---|---|---|---|
| lockpick = right-click menu | the agent picks any lock | `lockpick: done` with `click={ok … menu=Lockpick}` and `opened=roll` | `lockpick: FAILED — the right-click menu had no such entry` / `could not point at the target` / `the target is not on screen`; or a game crash within 30 s of the call (B `crash: signal`) |
| roll_check retrigger | roll_check right after a click opened the roll (lockpick, disarm, dialogue) | `roll: … → SUCCESS`/`→ FAILED` line (since 16:0x; `start re-sent Nx` is fine) | `roll_check: TIMEOUT` with the roll screen still up |

## Load, crash, reliability (se/act/orch)
| item | happens when | PASS | FAIL |
|---|---|---|---|
| Noesis releases bounded (se watch item) | always | B `UI generation N (…); … P parked, … L leaked in all`: L ≤ 10, and P returns to 0 within a minute of `(Running)` | L > 10 and growing across sessions, or P > 0 for minutes while Running (grep successive generation lines) |

## Movement, perception (vis/act)
| item | happens when | PASS | FAIL |
|---|---|---|---|
| nearest namesake (act, ACT-7 2026-10-16) | disarm/lockpick/loot_corpse by a name two visible things share (two Fire Traps, two Goblin bodies) | the nearer one is acted on, no extra call | `is ambiguous: eN, eM` when one is clearly nearer (> 0.5 m) |

## Party, economy, memory (setup/act)
| item | happens when | PASS | FAIL |
|---|---|---|---|
| identical items no longer ambiguous | equip/give of duplicated items | the batch goes on | `ambiguous` stops a batch |

## Combat (act)
| item | happens when | PASS | FAIL |
|---|---|---|---|
| Spiritual Weapon variant (act, S034+) | combat_turn bonus "Spiritual Weapon" | `took the first variant Target_SpiritualWeapon_… → … done` | `has no such action → its own weapon attack` |
| stuck shared turn (act, S056+, TOP) | a shared turn group (e.g. Shadowheart + Wyll + Spiritual Weapon) where one member ended while others still acted | the group's last end_turn shows `ended_again=[eX]` or the next member's TURN plays; no "can't act" stall, no save+reload "turnfix" | `it is not this character's turn` for the member the game shows active, `nothing to end — its turn already ended` for it, or a mid-fight save+reload to clear a stuck turn |
| enemy cast ids hidden (act, ACT-14 2026-10-16) | an enemy casts a spell in view | the event shows `cast` with the caster and its display name only | an enemy `cast` event with `spell=<engine id>` (e.g. `spell=Target_…`) |

### naut3 series: think-while-acting, helm ladders, objectives, CC first look (vis, 2026-10-17)

**One command checks every row:** `python3 tools/vis/series_report.py naut3 --vs naut2` (section (c): PROVED / FAILED
/ NOT-SEEN per row with the transcript line; `-v` every run's line). Trigger = the Nautiloid scenario's usual spots.

| item | happens when | PASS | FAIL |
|---|---|---|---|
| who-acting (act, ACT-P2, BG3AI_COMBAT_WHO_ACTING) | a single tool (use_ability, walk_toward, move_to, interact) without who in a fight while another member is selected (transponder dash) | `(who: eN X, whose turn it is; the selected Y can't act now)`; 0 × `it is not this character's turn` on a call without who | `FAILED — it is not this character's turn` on a use_ability/walk_toward/move_to without who |
