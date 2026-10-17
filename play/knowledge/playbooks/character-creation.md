# Character creation (phase 0)

**When:** the kickoff says **CHARACTER CREATION** and quotes a character brief (without one: the default build,
session-start playbook). Design the character from the brief, write who they are, create them, then play as them.

## The order (don't skip or reorder)
1. `new_game(difficulty="<the kickoff's difficulty, else Explorer>")`. It may return at once ("loading
   character creation"): the screen takes ~10 s to load, and the next game tool waits for it.
2. **While it loads: design the build and the persona from the brief** (below; the game's names are in
   `knowledge/setup.md` and `knowledge/builds.md`). Think once, then **write `memory/CHARACTER.md`** (format
   below) and the PC's section + level plan lines in `memory/party-build.md`. Memory tools don't wait for the
   game.
3. `character_creation_options()`: what this screen offers (origins, races/subraces, classes/subclasses,
   backgrounds, deities, ability points, skills, spells). Use only names from this list in the plan. Fix a
   name in CHARACTER.md only if the list spells it differently.
4. `create_character(plan='{"race":…,"subrace":…,"class":…,"subclass":…,"background":…,"deity":…,
   "abilities":…,"skills":…,"spells":…,"name":"<Name>"}')`: it types the name and presses "Venture forth".
   If it says the name wasn't set or creation isn't finished, call it again as it says; never finish another
   way (saves are matched to this playthrough by the name).
   Its result carries:
   - the **FIRST LOOK**: status (name, race, class as the game spells them), objectives and what's nearby.
     It is your get_status + look, so don't read again.
   - the first save `Sxxx-start`, queued for you. The runner learns the save owner (the character's name) from
     it. Only if the result says it wasn't started: `save_game(label="Sxxx-start")`.
5. **Move first:** issue the first action now (towards the objective the FIRST LOOK shows, e.g. `travel_to`
   or `route(..., go=true)`), then, while it runs:
   - PLAYTHROUGH.md: the `Character:` line (`<Name>, <race> <class> (<subclass>) · roleplay strict`) and the
     `Difficulty:` value.
   - `note("[decision] created <Name>: <one line on why this build fits the brief>")`.
   - Fix CHARACTER.md's header lines only if the FIRST LOOK spells name, race or class differently.

   Then play the prologue as the character (session-start playbook, step 2 on).

## From brief to build
- **The brief wins.** Race, class, background, deity and name follow what it says or implies ("pious" → a
  deity and a faithful class). The kickoff's constraints are hard rules.
- **Name:** the kickoff's, exactly, if it gives one; else one that fits the brief: letters, spaces, `'` or `-`,
  ≤ 20 characters.
- **Fill the gaps for robustness** (the run must reach the end): main stat 16-17 after racial bonuses, CON 14+,
  a class the brief allows that can heal, tank or hit hard. When the brief leaves the class open, Paladin,
  Cleric or Fighter are the safe picks (`knowledge/setup.md`). Tav, not the Dark Urge, unless the brief asks.
- **Build and persona must agree.** A Paladin's oath has tenets the game enforces (breaking them makes you an
  Oathbreaker): pick the oath whose tenets match the persona's values and copy them into `Won't do`. A
  cleric's deity is part of who they are.
- The tools can't set appearance or voice (game defaults stay); write `Look` anyway, for your notes.

## Writing the persona
Short, concrete lines you can act on in a dialogue, not adjectives (`the helpless first; a debt is always
repaid`, not `good, kind, brave`). Above `## Character development`: under 1600 characters (lint).
- **Voice:** how they speak (blunt, formal, dry jokes), so you can pick the answer that sounds like them.
- **Won't do:** hard lines (never abandons a child, never takes the tadpole powers, never lies to a priest).
- **How they decide:** the order of priorities for a hard choice, e.g. `the weak first, then the oath, then
  the odds`. This is what you apply when two answers both fit.
- **Roleplay:** the kickoff's mode, `strict` unless it says `light` (see `dialogue.md`, Playing your character).

The core is then fixed for the whole game (injected at every session start). When the character changes in
play (a vow kept or broken, a bond, a belief shaken), append `- Sxxx: <what changed and why> [seen]` under
`## Character development`; never rewrite the core.

## CHARACTER.md format
Keep the headings and header keys exactly (lint checks them):
```
# Character: <Name>

Name: <Name>
Race: <race> / <subrace>
Class: <class> / <subclass>
Background: <background>
Deity: <deity, or none>
Roleplay: strict

## Look
## Voice
## Backstory
## Values
## Flaws
## Goals
## Won't do
## How they decide
## Character development
- S001: created from the brief: "<the brief, shortened>" [seen]
```
Each `##` section: one to five short lines. `Character development` stays last.
