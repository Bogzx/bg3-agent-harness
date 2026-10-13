# Character creation (phase 0)

**When:** the first session of a new playthrough, when the runner's kickoff says **CHARACTER CREATION** and quotes
a character brief. Without a brief, a new game uses the default build in `knowledge/setup.md` (session-start
playbook) and needs no persona. Here you design your own character from the brief, write who they are, create
them in the game, and from then on play as them.

## The order (don't skip or reorder)
1. `new_game(difficulty="<the kickoff's difficulty, else Explorer>")`.
2. `character_creation_options()`: what this screen offers (origins, races/subraces, classes/subclasses,
   backgrounds, deities, ability points, skills, spells). Use only names from this list.
3. **Design the build and the persona from the brief** (below). Think once, then write.
4. **Write `memory/CHARACTER.md`** in the format below, and the build plan into `memory/party-build.md`
   (the PC's section and its level plan lines, `knowledge/builds.md` for the class).
5. `create_character(plan='{"race":…,"subrace":…,"class":…,"subclass":…,"background":…,"deity":…,
   "abilities":…,"skills":…,"spells":…,"name":"<Name>"}')`. It types the name and finishes ("Venture
   forth"). If it reports the name wasn't set or creation isn't finished, call it again as its message says;
   never finish creation another way (saves are matched to this playthrough by the name).
6. `get_status`: read back the name, race and class. If the game shows other names than you wrote (a subrace
   or subclass spelled differently), fix CHARACTER.md's header lines to the game's.
7. `save_game(label="Sxxx-start")` (Sxxx = this session, from the injection) as soon as you control the
   character. It is the playthrough's first save: the runner learns the character's name (the save owner) from it.
8. PLAYTHROUGH.md: the `Character:` line (`<Name>, <race> <class> (<subclass>) · roleplay strict`) and the
   `Difficulty:` value. Log `- HH:MM [decision] created <Name>: <one line on why this build fits the brief>`.
   Then play the prologue as the character (session-start playbook, step 2 on).

## From brief to build
- **The brief wins.** Race, class, background, deity and name follow what it says or implies ("pious" → a
  deity and a faithful class; "distrusts mind flayers" → a reason in the backstory). The kickoff's
  constraints are hard rules.
- **Name:** the one the kickoff gives, if any (the operator's or the profile's, use it exactly). Otherwise
  choose one that fits the brief: letters, spaces, `'` or `-`, at most 20 characters.
- **Fill the gaps for robustness** (the run must reach the end): main stat 16-17 after racial bonuses, CON 14+,
  a class the brief allows that can heal, tank or hit hard. When the brief leaves the class open, Paladin,
  Cleric or Fighter are the safe picks (`knowledge/setup.md`). Tav, not the Dark Urge, unless the brief asks.
- **Build and persona must agree.** A Paladin's oath has tenets the game enforces (breaking them makes you an
  Oathbreaker): pick the oath whose tenets match the persona's values and copy them into `Won't do`. A
  cleric's deity is part of who they are.
- Appearance and voice can't be set by the tools yet (the game's defaults stay). Write `Look` anyway: it is
  how the character sees themself and how you describe them in notes.

## Writing the persona
Short, concrete lines you can act on in a dialogue, not adjectives. The part above `## Character development`
must stay under 1600 characters (lint). Good: `Values: the helpless first; a debt is always repaid.` Weak:
`Values: good, kind, brave.`
- **Voice:** how they speak (blunt, formal, dry jokes), so you can pick the answer that sounds like them.
- **Won't do:** hard lines (never abandons a child, never takes the tadpole powers, never lies to a priest).
- **How they decide:** the order of priorities for a hard choice, e.g. `the weak first, then the oath, then
  the odds`. This is what you apply when two answers both fit.
- **Roleplay:** the kickoff's mode, `strict` unless it says `light` (see `dialogue.md`, Playing your character).

Once written, the core is fixed for the whole game: it is injected at every session start and after
compaction. When the character changes in play (a vow kept or broken, a bond formed, a belief shaken), append
one line under `## Character development` (`- Sxxx: <what changed and why> [seen]`); never rewrite the core to
match. The memory keeper may only append there too.

## CHARACTER.md format
Same as `memory-template/CHARACTER.md` (keep the headings and header keys exactly; lint checks them):
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
Each `##` section gets one to five short lines (Backstory up to five). `Character development` is the last
section.
