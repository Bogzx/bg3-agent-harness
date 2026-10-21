# Playthrough: claude-run-2

Created: 2026-10-09 · Difficulty: Explorer · Character: Claude, Paladin (Oath of Devotion)
Now: ACT 3 · Cloister of Sombre Embrace (House of Grief), Viconia defeated (S082), Daughter of Darkness done (parents released, S083) · Mol found (Guildhall), Rolan met (Sundries) · S084: Counting House hall cleared · S085: all L12; Minsc killed in the Cistern; sided with Nine-Fingers vs Zhentarim (Guild = ally) · S086: Morphic Pool entered (point of no return), domination failed · S087: sided with the Emperor (Netherstones given, Orpheus assimilated), Upper City, allies gathered; at the High Hall Courtyard gate · S088: courtyard battle won (all 4 Mind Flayers dead) · S089: High Hall fight won (reached the brain stem door) · S090: Wyll revived, party restored at the brain stem; final fight tried 3x (portal reached once) · S091: final fight try 4, brain to 28/210 then game over (reloaded) · S092: final fight tries 5-6 lost (Arcanists) · S093: tries 7-8 lost (try 8 reached the psyche with 3; Gale fell) · S094: tries 9-10 lost (try 9 cleared countermeasures R5; try 10 rushed, Compulsion cast R3) · S095: try 12 reached the psyche (all Arcanists dead, Compulsion at the Crown entity; Shadowheart dead, Wyll downed at the portal) · Last session: S095 · Save: S095-psyche (in combat, portal open)
Main thread: Steel Watch disabled (S055), Gortash killed and Netherstone taken (S062), Ghislev ambush skipped (S064), Bhaal door opened (S065) → Orin killed + Gale freed (S067) → all 3 Netherstones; Wyrmway trials (S071) → Ansur (S072) → Cazador killed (S077) → Viconia (S082) → Aid the Underduke done (S085) → Morphic Pool, Astral Plane (S086) → Netherstones given to the Emperor, Orpheus assimilated, Upper City, allies gathered (S087) → Courtyard battle (S088) → High Hall (S089) → Wyll revived, Brain Stem (S090) → Netherbrain killed, tadpoles destroyed, Claude went to Avernus with Karlach (S096); story finished, nothing next. Chapters: chapters/C01, C02.

## Read when
- Starting a session: HANDOFF.md → objectives.md → `get_status` (then the session-start playbook)
- Before talking to a named NPC: Grep memory/ for the name
- Entering or leaving a region: locations.md (that region's section)
- Before any irreversible choice: decisions.md + hypotheses.md
- Before or after combat: strategy.md#combat
- Before leveling up: party-build.md
- Selling, discarding or using a rare item: inventory-notes.md
- After context compaction: this file, HANDOFF.md, and your own sessions/Sxxx.md log

## Party
- Claude: Paladin, Devotion, Sword of Justice (from Anders, S021) + shield; frontline.
- Lae'zel: Fighter; found caged on the Roadside Cliffs (S007). "Free Lae'zel" closed as "left to her fate"; she broke free, killed the tieflings and left. Not recruited.
- Shadowheart: Cleric; woken on the beach and rejoined (S003); ranged support/healer. Regrouped at the ruins (end of S005).
- Gale: Wizard; recruited S005 at the Roadside Cliffs sigil; Evocation, blaster.
- Wyll: Warlock (Blade of Frontiers); recruited S009 in the Hollow. In camp since S021; truce with Karlach (S021).
- Active party now: Claude, Wyll (swapped in S079 for Viconia: Devil's Sight; died S089 in the High Hall, revived S090), Shadowheart, Gale (blaster, Greater Invisibility; rejoined S068). In camp: Karlach, Halsin, Mizora (stays in camp S068). Gone: Lae'zel, Jaheira (died S043, not revived), Zevlor (killed by Orin in camp S048), Volo (killed by a mob S050, not revivable). S096 end: the Emperor died in the psyche (Marked); Wyll died r15, Shadowheart dead since S095 (not revived before the brain fell); epilogue: Claude left for Avernus with Karlach.
- Karlach: Barbarian (Berserker); recruited S021 on the Risen Road; we killed Anders' fake paladins for her.

## Standing commitments
- Swore to Nettie (S009) to drink the Wyvern Toxin if I start transforming into a mind flayer.
- Told Zevlor I'd try to help the refugees with Kagha (S009).
- Promised Mol's friends (Mattis, Ide) at Last Light to bring Mol home (S025).
- Mizora will amend Wyll's pact once we free Zariel's asset (S025).
- Promised Karlach (S062) to stay with her when her engine gives out.
- Promised Arabella to find her parents (S026): kept; found dead, told her, Withers guided her (quest complete).

## Top lessons
- Out of combat, never stand still: queue the next walk/actions, then think and write notes.
- Pathing: when travel_to refuses, read route() legs; jump_to force over fire/steps; move_to the legs.
- Look at an item's owner before picking it up in camps (S009 arrest).
