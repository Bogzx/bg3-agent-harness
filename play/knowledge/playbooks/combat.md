# Combat

**When:** Use whenever combat starts or is about to start (a result says INTERRUPTED — combat, enemies turn hostile, or you plan to attack). Covers pre-fight setup, the batched turn loop (get_combat_state + get_actions → do_actions), path_preview, reactions, 5e tactics, downed allies, retreat, and the post-combat report.

## Before a fight you choose to start
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
s = get_combat_state()      # whose turn, action/bonus/movement/reaction left, initiative order,
                            # visible enemies (health as the UI shows it, distance, line of sight), allies
a = get_actions()           # usable abilities (ids for use_ability) with range, target type, costs, uses
→ plan the WHOLE turn from these two reads
   path_preview(target=…)   # if the move might not fit remaining movement or needs a jump
   ability_preview(ability, target)  # only if a hit chance or save DC decides between options
→ do_actions(actions='[{"tool":"move_to","target":"e12"},
                       {"tool":"use_ability","ability":"<id from get_actions>","target":"e12"},
                       {"tool":"use_ability","ability":"<bonus action id>"},
                       {"tool":"end_turn"}]')
                            # stops by itself on an interrupt (reaction prompt, target down, failure)
→ if it stopped early: read why, re-plan the rest of the turn, do_actions again
```
One observation, one batched turn: that's the target pace. `end_turn` blocks through the enemy turns and
returns what was visible, plus HP and condition changes. Re-observe only after that, or when a batch
stops early.
Several party members may act in a row when their initiative is adjacent. Take each one's turn fully
before `end_turn`; give `who=` on steps for a character other than the active one.
**Reaction prompts** (Shield, Counterspell, opportunity attack) stop a batch: `get_dialogue` shows
the prompt, and `react(use=true, which="…")` or `react(use=false)` answers it.
**Movement:** commit combat movement only after `path_preview` says the destination is reachable and
fits the remaining movement. A failed move wastes the turn plan.

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
- **Party wipe** → `list_saves`, then `load_game(save="<folder of the pre-fight save>")`. Log `[death] wipe vs <enemy>; reloaded <save>`
  and add a lesson about what to do differently. Don't repeat the identical opening.
- If the same fight is lost twice: run the `stuck` playbook (a different approach: level up first, rest,
  other route, avoid, talk).

## After combat (mandatory)
1. `end_combat_report(what_worked="…", what_to_change="…", next_goal="…")`. It goes into your
   session log. Keep it concrete, e.g. "grease + fire at the door took out 4" / "Astarion got caught
   alone on the roof" / "loot, then short rest".
2. Loot the bodies nearby in one `do_actions` sweep (`interact` corpse → `loot` …; see
   knowledge/tools.md). Be quick (see `exploration` for what to keep).
3. Heal: short rest if anyone is below half and short rests remain. Long rest when slots are low and
   nothing urgent is pending.
4. Log `- HH:MM [combat] <enemy group> at <place>: won/lost, downs N`.
5. If a companion died: get a Scroll of Revivify, or recover them at camp later (the camp NPC resurrects
   for gold `[wiki]`). Note it in objectives.
