# Party and leveling

**When:** Use when a level up is available, when choosing or swapping companions, at character creation, when preparing spells, or when a build plan needs revising. Covers get_level_up_options/level_up, feats and ASIs, spell selection, party composition, and party-build.md upkeep.

## Character creation (first session only)
Follow the build and name in `knowledge/setup.md` if present (session-start step 0). Tools:
`new_game(difficulty="Balanced")` → `character_creation_options()` (what the screen offers) →
`create_character(plan='{"race":"…","class":"…","subclass":"…","background":"…","abilities":"recommended","name":"…"}')`.
Omitted fields keep the game's recommendations. Without a configured build:
- Choose something robust and easy to play well: e.g. Cleric (Life) or Paladin for survivability and
  healing, or Fighter for simplicity. A support caster as the player character means the party always
  has a healer, since companions can leave.
- High score in the main stat (16–17 after racial bonuses), CON 14+. A good WIS or CHA helps dialogue.
- Record the build plan in party-build.md right away (role, stat priorities, feat or ASI plan by level,
  key spells), plus the character line in PLAYTHROUGH.md.

## Level up procedure
1. Read party-build.md for that character.
2. `get_level_up_options(who="<name>")` lists what the level-up screen offers (class or subclass,
   feats or ability points, spells, skills).
3. Follow the plan unless the options show it's impossible or clearly worse. If you deviate, update
   the plan and say why.
4. `level_up(who="<name>", plan='{"feat":"Ability Improvement","abilities":{"Wisdom":2},"spells":["Spirit Guardians"]}')`.
   Omitted fields take the game's defaults; a multiclass level adds `"class":"<Class>"`. Check the
   result with `get_character(who="<name>", sections="resources")`.
5. Log `- HH:MM [level] <char> → L<n>: <key picks>`.

**Defaults when no plan exists:**
- Level 4/8/12 (and Fighter 6, Rogue 10): +2 to the main stat until 20, unless a key feat is clearly
  better. Good feats: Alert, War Caster (concentration casters), Great Weapon Master or Sharpshooter
  (martial damage), Tough `[wiki]`.
- Spells: prioritise control and area spells (Sleep, Hold Person, Web, Hypnotic Pattern, Spirit
  Guardians, Fireball), the bonus-action heal Healing Word, and utility you lack (Misty Step, Longstrider,
  Aid). Prepared casters (Cleric, Druid, Wizard) can change prepared spells outside combat, so prepare
  for the next known fight.
- Don't multiclass unless the plan says so.

## Party composition (4 active)
- Aim for: a frontline (Fighter, Barbarian, Paladin), a healer or support (Cleric, Druid, Bard), an
  arcane or area caster (Wizard, Sorcerer, Warlock), and a skill character (Rogue, Ranger, Bard) with
  Sleight of Hand and Perception for locks and traps.
- Swap at camp when a region needs different strengths (e.g. a strong talker for a dialogue-heavy
  area): `camp(action="go")`, `talk_to` the companion and pick the add or remove option in the
  dialogue, then `camp(action="leave")`. If no option appears, `ui_dump` → `ui_action`.
- Companion approval and stories matter for the ending and for keeping them. Note their reactions in
  npcs.md, and avoid repeatedly angering a companion you rely on.
- Respecs (the camp/Withers NPC, costs gold `[wiki]`) only if a build is clearly failing. Log it as a
  decision.

## party-build.md upkeep
Intentions only: role, plan per level, spells to prepare, gear wishlist, and notes like "bad at
ranged". Never stats. Update it after every level up that changes the plan, and when gear on the
wishlist is obtained.
