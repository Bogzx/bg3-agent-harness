# Combat

**When:** combat starts or is about to start (INTERRUPTED — combat, enemies turn hostile, you plan to attack).

## Before a fight you choose to start
0. **Lessons:** a `LESSONS:` block in a result (at a fight start, or on entering a place) holds your own past
   failures against these enemies/this place, from `memory/lessons.md`. Plan the fight around them; don't
   repeat what failed.
1. **Save** (`save_game(label="Sxxx-pre-<enemy>")`) for anything bigger than 2–3 weak enemies.
2. **Scout** with `get_nearby` and `get_combat_state` once combat starts: hostiles, distances,
   elevation, and visible surfaces and objects (ladders, explosive barrels, grease or water). A
   screenshot only for a complex layout the text can't convey (multi-level arenas, choke points).
3. **Position:** move ranged characters to high ground (attack bonus from height `[wiki]`), keep the
   frontline between the enemies and the casters, and use `set_party(action="unchain", who=…)` for
   anyone you position separately. Sneaking up (`set_party(action="sneak_on")`, then `move_to`) can
   grant a surprise round.
4. **Buff** with long-duration effects if available (e.g. Longstrider, Aid, Mage Armor). Save
   concentration spells such as Bless for the first round.
5. Open with your strongest control or area effect on clustered enemies.

## Each turn (loop)
```
s = get_combat_state()      # ONLY when no result has shown the fight yet (COMBAT STARTED and end_turn's
                            # hand-over already carry it): turn order, resources left, enemies (health,
                            # distance, line of sight), allies, the acting character's actions
   path_preview(target=…)   # only if the move might not fit remaining movement or needs a jump
   ability_preview(ability, target)  # only if a hit chance or save DC decides between options
→ one action per call: use_ability / move_to / walk_toward / jump_to / react. Each returns when the action is
   over, with after={…} (HP, action, bonus, movement left) and CHANGES (HP, deaths, moves).
→ end_turn(who=…) ends that member's turn; it returns when a party member can act, with their YOUR ACTIONS.
```
BG3 has **no Dodge action**; use only ids `get_actions` lists.
**Ranged and spells:** `move_to(target="e12", within=<range in m>)` stops inside that range.
**Reaction prompts** (Shield, Counterspell, Divine Smite, opportunity attacks): `set_reactions` sets the game's
own prompt setting. A prompt that appears: `get_dialogue` shows it, `react(use=true, which="…")` or
`react(use=false)` answers it.
**Movement:** in a fight `walk_toward` cuts a walk longer than the movement left to what is left; it takes an
objective id (`o3`) too. A single tool with no `who` acts as the one member whose turn it is.
Check a tight or vertical route with `path_preview` first; a jump is `jump_to`.
`do_actions` and `queue_actions` run no steps in combat.

**Ids that fail every session (S023-S025):** spells with variants need the variant id, never the base
one: `Target_SpiritualWeapon_Greatsword` (or _Maul, _Halberd…), `Shout_SpiritGuardians_Radiant`
(or _Necrotic), likewise Chromatic Orb, Enhance Ability, Protection from Energy; `get_actions` lists
them. Multi-target spells take at most two targets (`"targets":"e1,e3"`; Bless on 3 is refused).
To open a fight from exploration, attack with `use_ability`.

**Priorities each turn:**
1. **Downed ally?** Healing Word (bonus action, ranged) or Help (action, adjacent) gets them up. A
   downed character makes death saves; damage while downed counts as failures.
2. **Can you remove or disable a threat this turn?** Focus fire on the most dangerous enemy you can
   actually reach. Finish badly wounded enemies.
3. **Control beats damage:** Command, Sleep, Hold Person, Web, Grease, prone via Shove (bonus action),
   or pushing enemies off ledges.
4. **Don't waste economy.** Use the bonus action every turn (Healing Word, Shove, Jump, Disengage for
   Rogues, off-hand attack, Hunter's Mark). Keep a reaction ready (Shield, Hellish Rebuke, opportunity
   attacks).
5. **Concentration:** only one spell at a time per caster. Keep concentrating casters out of melee.
6. **Positioning:** ranged attacks in melee are at a disadvantage. Leaving melee provokes opportunity
   attacks unless you Disengage. Avoid harmful surfaces: fire, acid, and ice (risk of falling prone).
7. **Combos you can see:** grease + fire, water + lightning or cold, explosive barrels near enemy
   groups. Never next to your own party.

**Resources:** spell slots and once-per-rest features are finite. On trash fights, use cantrips and
weapons. Spend slots on fights that threaten someone going down. Use potions or healing when a
character is below ~35% and in reach of enemies.

## When to retreat or reset
- Two party members down and no healing → retreat, or use everything you have now (scrolls, potions,
  big spells). Leaving combat: Dash or Disengage away, break line of sight, or jump to unreachable
  ground.
- **Party wipe or a reload mid-fight** → `list_saves`, then `load_game(save="<folder of the pre-fight save>")`.
  Read the `LAST ATTEMPT:` line its result carries (the harness's debrief of the failed try), then write ONE
  lesson before the retry: `note("[lesson] WHEN <enemy or stem>, <place or situation>: <what failed> → <what to
  do instead>")` (e.g. `WHEN Justiciar Soulhunter, ranged: they shot us down from 20 m → Fireball the archer
  cluster first, fight from cover`). Then plan a DIFFERENT opening: pre-buff, another position, another first
  target, the consumable you didn't use. Never the identical opening.
- If the same fight is lost twice: run the `stuck` playbook (a different approach: level up first, rest,
  other route, avoid, talk).

## After combat (mandatory)
1. `end_combat_report(what_worked="…", what_to_change="…", next_goal="…")`. It goes into your
   session log. Keep it concrete, e.g. "grease + fire at the door took out 4" / "Astarion got caught
   alone on the roof" / "loot, then short rest".
2. Loot the bodies and chests nearby with `loot_area()` (one call; it skips locked and owned ones).
   Be quick (see `exploration` for what to keep). `regroup()` if the party is scattered.
3. Heal: `rest_if_needed()` (short rest if anyone is at or below half). Long rest when slots are low and
   nothing urgent is pending.
4. `note("[combat] <enemy group> at <place>: won/lost, downs N")` (instant; or note= on your next walk).
   A costly fight (someone went down, or a `DEBRIEF costly/near_wipe` line): add one
   `[lesson] WHEN …: … → …` line too. The memory keeper turns these into `memory/lessons.md`, which the
   harness shows at the next matching fight.
5. If a companion died: get a Scroll of Revivify, or recover them at camp later (the camp NPC resurrects
   for gold `[wiki]`). Note it in objectives.
