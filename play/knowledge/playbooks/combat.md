# Combat

**When:** Use whenever combat starts or is about to start (a result says INTERRUPTED — combat, enemies turn hostile, or you plan to attack). Covers pre-fight setup, the one-call turn loop (get_combat_state + get_actions → combat_turn), path_preview, reactions, 5e tactics, downed allies, retreat, and the post-combat report.

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
                            # visible enemies (health as the UI shows it, distance, line of sight), allies,
                            # and the acting character's actions (ids for use_ability, range, costs)
→ plan the WHOLE turn from this one read, with a fallback for each slot
   path_preview(target=…)   # only if the move might not fit remaining movement or needs a jump
   ability_preview(ability, target)  # only if a hit chance or save DC decides between options
→ combat_turn(plan='{"move":{"target":"e12","within":1.5},
                     "action":[{"ability":"Target_MainHandAttack","target":"e12"},
                               {"ability":"Projectile_ThrowWeapon","target":"e12"},
                               "Shout_Dodge"],
                     "bonus":{"ability":"<bonus action id>","if":"hp_below:50"}}')
→ it returns right after end_turn: "NEXT e14 Goblin, e15 Imp, then Lae'zel (yours)".
   Plan Lae'zel's turn NOW, while the enemies act; then wait_for("my_turn") and send it.
   ("NOW Lae'zel's turn" + her actions = she is up already: send her combat_turn at once.)
→ if it says STOPPED / "turn NOT ended": read why, then finish the turn (combat_turn with what's left)
```
One observation, one call per turn: that's the target pace. Think during the enemy turns, not during yours.
**Fallbacks are the plan's safety net.** The usual chain: the attack you want → the ranged option (throw,
cantrip, bow) → `Shout_Dash` with `"then_move":true` (if you must close in) → `Shout_Dodge` or
`Shout_Disengage` (if nothing reaches). A dead or vanished target skips to the next alternative, so you can
also list a second target (`{"ability":"…","target":"e15"}`).
**Ranged and spells:** `"move":{"target":"e12","within":<range in m>}` walks only until in range (and not
at all if already in range); keep casters out of melee.
**Extra Attack** (level 5 martials): add `"extra":[[{"ability":"Target_MainHandAttack","target":"e12"},
{"ability":"Target_MainHandAttack","target":"e15"}]]`.
Several party members may act in a row when their initiative is adjacent: one `combat_turn` each, with
`who=`.
**Reaction prompts** (Shield, Counterspell, Divine Smite, opportunity attacks): decide them up front.
`"reactions":{"Divine Smite":"always"}` sets the game's own prompt setting; `"on_reaction":"use"` or
`"skip"` answers prompts that appear during this turn. Otherwise a prompt stops the turn: `get_dialogue`
shows it, `react(use=true, which="…")` or `react(use=false)` answers it, then finish the turn.
**Movement:** a move that doesn't fit the movement left walks as far as it can (then the fallbacks decide).
Check a tight or vertical route with `path_preview` first; a jump is `{"tool":"jump_to",…}` as an alternative.
`do_actions` still works for odd turns (e.g. two characters' steps in one list), but `combat_turn` is the default.

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
2. Loot the bodies and chests nearby with `loot_area()` (one call; it skips locked and owned ones).
   Be quick (see `exploration` for what to keep). `regroup()` if the party is scattered.
3. Heal: `rest_if_needed()` (short rest if anyone is at or below half). Long rest when slots are low and
   nothing urgent is pending.
4. Log `- HH:MM [combat] <enemy group> at <place>: won/lost, downs N`.
5. If a companion died: get a Scroll of Revivify, or recover them at camp later (the camp NPC resurrects
   for gold `[wiki]`). Note it in objectives.
