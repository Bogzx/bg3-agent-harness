# Party and leveling

**When:** Use when a level up is available (a `LEVEL UP:` line in get_status/look, or a `LEVEL UP pending`
reminder), when a `PARTY PLAN` line asks for plan lines or a composition decision, when choosing or swapping companions, at character creation, when preparing spells, or when a
build plan needs revising. Covers level_up, party-build.md plans, party composition, feats and ASIs, spells.
Standard builds per class: `knowledge/builds.md`.

## Level ups: do them at once, in one call each
A level up is free power: take it at the **next safe moment** (out of combat and dialogue), before the next
fight. get_status/look print `LEVEL UP: e3 Gale (2) → level_up(who) now…` while one is pending; other tool
results say `LEVEL UP pending` as soon as the bridge sees it (it checks about once a minute). In combat,
finish the fight first. Write plans **ahead**, not at the level up: a `NOTES WINDOW` (walking time) may carry
`PARTY PLAN: no plan line yet for Gale L4, …; party lacks skills/locks/traps (Astarion)`. Write those lines
then, so the level up itself is one instant call. Levels without required picks (a Paladin or Cleric level
with no new subclass, feat, spell or passive) still go through with the game's defaults when no line exists,
but the next feat or spell level will fail without one.
1. **Plan line exists?** party-build.md has one line per character level under `## Level-up plans`:
   `- Gale L2: {"subclass": "Evocation", "spells": ["Sleep", "Thunderwave"]}`. If the LEVEL UP line says
   "no plan yet", write it first: look at `knowledge/builds.md` for the class and, if unsure what the screen
   offers, `get_level_up_options(who="Gale")`.
2. `level_up(who="Gale")` with no plan uses the stored line for that level. One call, the whole level.
   (An explicit `plan='{…}'` overrides it.) Keys: `subclass`, `feat`, `abilities` ({"Strength":2} or
   {"Wisdom":1,"Constitution":1}), `spells`, `cantrips`, `skills`, `passives` (fighting style, …),
   `class` only for a planned multiclass.
3. **Subclass levels** (Wizard and Druid 2; Fighter, Rogue, Ranger, Barbarian, Monk, Bard 3; Cleric,
   Paladin, Warlock, Sorcerer pick at 1) refuse to run without `subclass` and list the choices: a subclass
   is permanent. **Feat levels** (4, 8, 12; Fighter also 6, Rogue also 10) default to +2 on the main ability.
   `"feat": "Ability Improvement"` + `"abilities"`: `{"Strength":2}`, `{"Strength":1,"Charisma":1}` or
   `["Strength","Charisma"]` (+1 each); max 20. Half feats (Athlete, Resilient, ...) give 1 point: the main
   ability if the feat offers it, else name it (`"abilities":{"Dexterity":1}`). A feat's own choice (Elemental
   Adept's element, ...) goes in `passives`. A feat level that also learns spells/cantrips (Cleric, Wizard,
   Warlock at 4) needs those in the same plan: `{"feat":"Alert","cantrips":["Toll the Dead"]}`.
4. **Spells, cantrips and passives have no default either**: a level that learns spells (Wizard 2 picks 2,
   Warlock 2 picks 1, ...) or picks passives (Warlock invocations at 2, fighting styles) fails until the plan
   names them, and the error lists exactly what can be picked (`can pick: ...`), so copy names from it.
   E.g. `- Wyll L2: {"passives": ["Agonizing Blast", "Repelling Blast"], "spells": ["Hex"]}`. Spells already
   known are not offered. British and US spellings both match ("Agonising"/"Agonizing").
5. If the result lists problems (a spell not offered, choice incomplete), it did not finish: fix the plan
   line from the problems and call again. Never drive the level-up screen by hand (press_key, clicks): the
   tool does every click itself. If it fails twice on the same plan, it is a harness bug: press_key ESCAPE,
   note it in the session log, and keep playing (S018 lost ~10 min to hand-driving). `note("[level] Gale → L2: Evocation, Sleep, Thunderwave")`.
6. Several pending: level each one back to back (Claude, then the companions); no reads in between.

## Party composition: 4 roles
Keep four active members covering these roles; fill gaps by recruiting, swap at camp when a region needs it.
Write the current answer under `## Party composition` in party-build.md (who fills each role, the gap, the
next recruit and where); older memories without that section or `## Level-up plans`: add both now (format:
the comment at the top of a fresh party-build.md, `memory-template/party-build.md`). Re-check it at every
level up, every companion met or lost, and before a region whose needs you know.
| Role | Does | Good fits (companion: class) |
|---|---|---|
| **Frontline tank** | holds the line, high AC/HP, protects casters | Claude (Paladin), Lae'zel (Fighter), Karlach (Barbarian), Minsc (Ranger, Act 3) |
| **Healer / support** | Healing Word, Bless, Spirit Guardians, revives | Shadowheart (Cleric), Jaheira or Halsin (Druid, Act 2) |
| **Arcane damage / control** | area damage, crowd control (Sleep, Web, Hold, Fireball, Hypnotic Pattern) | Gale (Wizard), Wyll (Warlock) |
| **Skills / rogue-ish** | locks, traps, Perception, Sleight of Hand, stealth, ranged | Astarion (Rogue), a Bard or Ranger |
- **Now (Act 1):** Claude (Paladin, tank + face) · Shadowheart (healer) · Gale (control) · the 4th slot is
  the gap: **recruit Astarion** (skills/locks/traps; on the beach near the crash site `[wiki]`) or Lae'zel
  (second frontliner; she joins again if found and talked to) / Karlach (Act 1, later). Prefer Astarion
  while locked chests, traps and stealth come up; switch to Karlach or Lae'zel for a hard melee fight.
- Recruit every companion you meet (talk, accept): a full camp roster lets you cover any gap later.
  Lae'zel "left behind" is not gone: she can be recruited where she is, or waits at camp once recruited.
- **Swapping: `swap_party(into, out)`**, one call (camp → join answer → leave camp → regroup). Then
  `level_up` the newcomer at once: camp companions gain XP but their level ups wait (`LEVEL UP` line).
  `get_party(camp=true)` shows who waits at camp. **When to swap** (the `PARTY:` line in get_status/look
  flags most of these; `knowledge/companions.md` is the table it uses):
  1. **Their own story is here.** A camp companion tied to the region or an active quest: Halsin in the
     Shadow-Cursed Lands (Thaniel, lifting the curse), Shadowheart at the Gauntlet of Shar, Karlach for
     Dammon (her engine), Wyll for Mizora/Duke Ravengard, Lae'zel at the creche, Astarion for the Gur and
     Cazador, Gale for Elminster. Their scenes, approval and rewards only happen with them present.
  2. **A missing role** for the next known fight: no healer, no frontline, no ranged or area damage.
  3. **A skill gap** for the area: many locks/traps (Sleight of Hand: Astarion), talky story checks (a
     high-CHA face), hidden traps (Perception).
  4. **A dead member** you can't revive yet: bring a camp companion until Withers/Revivify.
  - **Swap back** when the reason ends (the quest step is done, you leave the region). Keep the strongest
    four otherwise; never swap mid-dungeon just to try someone out (each swap is ~1 min of play).
  - Note the reason: `note("[decision] swap Gale→Halsin: Thaniel quest in SCL")`.
- **Still recruitable in Act 2** `[wiki]`: Halsin (if not yet; joins after the goblin leaders fall),
  Jaheira (Last Light, after the Last Light/Moonrise events), Minthara (Moonrise, only if knocked out
  in Act 1, not killed). Act 3: Minsc (with Jaheira). Recruit every one you can: a full camp covers gaps.
- Companion approval matters for keeping them and for the ending: note reactions in npcs.md and avoid
  repeatedly angering one you rely on.
- Respecs (Withers at camp, 100 gold `[wiki]`) only if a build is clearly failing; log it as a decision.

## Plan to level 12 in party-build.md
For each active character (and a likely swap-in) write once, then follow at every level:
- role, subclass, main/secondary ability, feat or ASI at 4/8/12 (6/10 where the class gets one);
- key spells by level (what to learn, what to prepare), fighting style, notable gear wanted;
- the per-level plan lines (`- Name Ln: {json}`) for at least the **next two levels** of each character.
Take builds from `knowledge/builds.md` unless there's a reason (gear found, party gap). If you deviate,
update the plan and say why in one line.

## Character creation (first session only)
Follow the build and name in `knowledge/setup.md` if present. Tools: `new_game(difficulty="Balanced")` →
`character_creation_options()` → `create_character(plan='{"race":"…","class":"…","subclass":"…","background":"…","abilities":"recommended","name":"…"}')`.
Choose something robust (Paladin, Cleric, Fighter), main stat 16–17, CON 14+. Write the build plan in
party-build.md right away.

## Spells and preparation
Prepared casters (Cleric, Druid, Paladin, Wizard's spellbook) change prepared spells outside combat:
prepare for the next known fight (control and area spells, Healing Word, Bless, utility you lack such as
Misty Step, Longstrider, Aid). Don't multiclass unless the plan says so.

## party-build.md upkeep
Intentions only: composition, roles, per-level plans, spells to prepare, gear wishlist. Never current
stats (get_character). Update after every level up that changes the plan, and when wishlist gear is found.
