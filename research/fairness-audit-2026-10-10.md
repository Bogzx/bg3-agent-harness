# Fairness audit, 2026-10-10 (commits 7419fab..5066879)

Rule (docs/FAIRNESS.md): the playing agent gets only what a human player could see on screen at that moment, or
remember having seen; general wiki knowledge is fine. Scope: everything added to `bridge/`, `mod/`, `play/`
since 7419fab, with the specific questions from the task. Offline audit (code reading + offline tests); the
game was running a session, so nothing was probed live.

## Verdicts

| # | Area | Verdict | Notes / fix |
|---|---|---|---|
| 1 | `nav.route`: path legs, climbs/jumps/drops, path length | OK | The engine's own path to the goal, the same thing the in-game movement preview draws (jump/climb icons included). BG3 has no in-world fog: the camera shows the geometry around the party, so a route to a spot within `PATH_MAX_M` (80 m) is what clicking there shows. A `target` goal must pass `characterVisible`/`itemVisible`. |
| 2 | `nav.route` connectors (ladders/doors ranked) | OK | Candidates go through `V.itemVisible` (perception gate + secret-door/trap/scenery rules). Ladder height comes from the visible ladder's bounds. |
| 3 | `nav.route` `levels_goal` (floor heights at the goal) | **LEAK (minor), fixed** | Was computed for any goal, even past 80 m (`too_far`): floor heights (a hidden cellar, a ledge) at any coordinate the agent typed. Now only within path range. `Navigation.lua` `N.routeAhead`. |
| 4 | Route exposure (fire along the path) | OK | Harmful surfaces on the engine path; the game's path preview warns about surfaces on the path. Invisible surfaces are never reported (`surfaceAt`). |
| 5 | Hazard patches (`nav.hazards`, frontier hazards, `look` delta `hazards` part) | **LEAK, fixed** | `hazardPatches` scanned tiles up to 20 m around the character with no sight test: fire/acid behind a wall, in the next room or a floor below (±3 m) was listed. Now a patch is listed only if it is underfoot or a party member sees part of it: within sight range and an eye ray reaching up to 5 sampled cells (`V.pointSeen`, new). If the ray API is unusable (uncalibrated/broken, `nil`) patches are kept (fail-safe for walking; matches the path preview). `N.lastHiddenPatches` counts the dropped ones for dev checks. |
| 6 | Arrival / delta lists (`delta.py`: `+ e7 Imp`, `~ HP`, `GONE`) | OK | Built only from `state.nearby`/`state.status`/`nav.markers`/`ui.state` payloads, all already visibility-filtered; HP diffs honour `HIDE_NPC_HP` at the source. `GONE` = left view (a player notices that too). |
| 7 | REMEMBERED (`state.nearby.remembered`, `V.recall`) | OK | `V.remember` is only called for characters shown in `state.nearby` (perceived); snapshots are never refreshed while unseen (last-seen pos, name, relation, age), same level only. |
| 8 | `get_nearby` counts (`+N more`, `more_items`) | OK | `more_items` counts items that already passed `itemVisible` (only cut by the list limit); the frontier `more` likewise. No counts of hidden things are exported (only `dev.fairness_probe`, a dev op). |
| 9 | Skill-check annotations on dialogue options | OK | Tag + the speaker's own modifier (+ party best) from the character sheet; no DC (the DC appears only on the roll screen). Tags without a matching skill/ability say "class/race/background option". |
| 10 | Fast travel waypoint lists (`state.map`, `ui.fast_travel`) | OK | `state.map` reads `PartyWaypoints` (unlocked only); `ui.fast_travel` picks from the map UI's own waypoint list. |
| 11 | `get_explored` (`nav.explored` recent places "near <waypoint>") | **LEAK (minor), fixed** | Named places after *any* waypoint shrine of the level within 60 m (`DB_WaypointInfo`), including undiscovered ones. Now only unlocked ones (`N.unlockedWaypointIds`, the `PartyWaypoints` set); fail closed. |
| 12 | Objective markers (`nav.markers`, `o1`) | **LEAK, fixed** | Active objectives walked every quest prototype, including technical quests the journal never shows (`QuestVisiblity=false`; `State.journal` already skipped them). Their markers could point at hidden story targets. Now skipped in `stepObjectives`. Also: area markers (radius > 0, a circle on the map) now say `area_m` so the agent doesn't treat the centre as an exact spot. Residual judgment call: the marker position of a character target is its live position, as the map's tracked marker is. |
| 13 | Combat (enemy HP, turn order) | OK | `S.combat` lists only visible combatants (`unseen_combatants` flag only); `hp_pct` honours `HIDE_NPC_HP`; `S.examine` same. |
| 14 | Level-up options | OK | Read from the level-up UI the player sees. |
| 15 | `state.entity` for a far target (bridge `_ent_pos` / `_far_move`) | OK | `state.entity` re-checks `characterVisible`/`itemVisible` on every call; an unseen character returns only the remembered `last_pos` (no `pos`), so `_far_move` cannot get a live position of something out of sight. |
| 16 | Loop watchdog (`A.loop_watch`, `loop_stopped`) | OK | Only about party members using an item (the player watches their own character re-climbing). |
| 17 | `act.watch` / `act.target_state` | OK | Party rows (portraits), visible characters only (`hostile` = the red highlight); target state gated by `G.visible`. |
| 18 | `loot_corpse` / `pickpocket` refusal order (Actions.lua) | **LEAK (minor), not fixed here** | `G.is_dead(npc)` is checked *before* `G.target_visible`, so a stale handle of a character now out of sight answers "they are not dead" / "nobody to pickpocket" vs "not visible": a dead/alive oracle. Fix (act owns Actions.lua): move `G.target_visible(corpse/npc)` above the `is_dead` checks in `loot_corpse` and `pickpocket`. |
| 19 | Guard refusal texts with distances (`too far away (37m ...)`) | OK | `G.within` runs after `G.target_visible` in every caller; destination coordinates are the agent's own. |
| 20 | Frontier waypoint label | OK (judgment) | A visible shrine is labelled with its name; the game shows the name on hover. |
| 21 | Dialogue `speakers`, `automated` flag | OK | Participants of the agent's own conversation. |

## Changed files

* `mod/.../Server/Visibility.lua`: `V.pointSeen(ctx, pt, lift)` (sight range + eye ray to a ground spot;
  `nil` when the ray API is unusable).
* `mod/.../Server/Navigation.lua`: `patchSeen` filter in `N.hazardPatches` (+ `N.lastHiddenPatches`);
  `stepObjectives` skips invisible quests; `N.unlockedWaypointIds` + `nav.explored` names only unlocked
  waypoints; `levels_goal` only within path range; markers carry `area_m`.
* `tools/dev/test_fairness_lua.lua` + `.sh`: 10 offline tests (Lua 5.3/5.4 in Docker).

## Tests

`tools/dev/test_fairness_lua.sh` 10/10; regression: `tools/dev/test_nav_lua.sh` 45/45,
`tools/vis/test_route.sh` 30/30, `tools/vis/test_read_cost.sh` 9/9, `tools/vis/test_lua.sh` 22/22.

## Needs a live check

* `V.pointSeen` against real geometry: a burning floor 8-12 m away in the open must still be listed
  (the ray to 0.5 m above a floor at a shallow angle may clip uneven ground; 5 samples per patch and the
  0.5 m slack should cover it). Probe: `nav.hazards` next to a fire, then behind a wall from it; compare
  `BG3AI.Nav.lastHiddenPatches`. If open-ground fires drop, raise `V.POINT_LIFT` to 1.0.
* `nav.explored` "near" after a waypoint is unlocked (the `PartyWaypoints` entries' `Name` must equal the
  `DB_WaypointInfo` id, as `State.map` already assumes).
