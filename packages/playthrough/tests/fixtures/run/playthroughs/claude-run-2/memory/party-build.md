# Party build

## Party composition
- Claude (Paladin, Devotion): frontline tank/smiter, party face (but Wyll has the best CHA skills).
- Shadowheart (Cleric, Trickery): healer/support, Guiding Bolt, Hold Person, Spiritual Weapon.
- Gale (Wizard, Evocation): blaster/control (Magic Missile, Sleep, Grease, Scorching Ray).
- Karlach (Barbarian, Berserker): main melee damage since S021; Wyll moved to camp (truce made S021).
- Wyll (Warlock, Fiend, Pact of the Blade): in camp; ranged Eldritch Blast, Intimidation/Persuasion speaker.
- Uncovered: locks/traps/Sleight of Hand (no rogue; Astarion never recruited). Workaround: Gale/Claude try locks with Thieves' Tools, break crates/doors with attacks, Wyll's Eldritch Blast for cracked walls. [guess]

## Level-up plans
- S023: party still L4 (Claude, Karlach, SH, Gale); Wyll L3 (devil now). Karlach wears Githyanki Half Plate, SH Scale Mail, Claude has a Periapt of Wound Closure; L5 = Extra Attack / 3rd-level slots [seen S023]
- Claude L4: DONE S018 (Tough; Ability Improvement failed in the tool)
- Last updated: S095 (no change)
- S024: Claude, Karlach, Shadowheart, Gale reached L5 at camp (Gale took Fireball + Counterspell; SH has all cleric L3 spells incl. Spirit Guardians, Mass Healing Word, Revivify). Claude has -2 WIS from the zaith'isk (cured by eating a tadpole [wiki]; not done).
- Claude L5: {}
- Claude L6: {}
- NOTE S018: level_up can't click spell/cantrip picks ("no spell button found") and fails with Ability Improvement: SH/Gale/Wyll L4 pending. Retry at a calm moment; else pick feats with no choices (Alert, Tough, War Caster) and spells via a different try.
- Shadowheart L4 alt: {"feat":"War Caster","cantrips":["Toll the Dead"]}
- Shadowheart L4: {"feat":"Ability Improvement","abilities":{"Wisdom":1,"Constitution":1}}
- Shadowheart L5: {}
- Shadowheart L6: {}
- Karlach L4: DONE S021 (Berserker, Ability Improvement STR+2)
- Karlach L5: {}
- Karlach L6: {}
- Gale L4: DONE S021 by hand (INT+2, Toll the Dead, Knock, Web). Manual click recipe in sessions/S021.md 05:31.
- Gale L5: {"spells":["Fireball","Counterspell"]}
- Gale L6: {"spells":["Haste","Slow"]}
- Claude L7: {}
- Shadowheart L7: {}
- Karlach L7: {}
- Gale L7: {"spells":["Ice Storm","Greater Invisibility"]}
- Claude L8: {"feat":"Alert"}
- NOTE S047: Claude L8 line rewritten from the game's offer list (no 'Ability Improvement' offered; 41 numbered feat choices, Alert is a no-choice feat [guess])
- Shadowheart L8: {"feat":"Ability Improvement","abilities":{"Wisdom":2}}
- Karlach L8: {"feat":"Great Weapon Master"}
- NOTE S037: L8 lines are [guess] defaults; check get_level_up_options when L8 arrives
- Claude L9: {}
- Shadowheart L9: {"spells":[]}
- NOTE S044: Shadowheart L9 line rewritten from the game's offer list (no choices offered)
- Karlach L9: {}
- NOTE S051: Karlach L9 line rewritten from the game's offer list (no picks offered; failure was the page opened for another character: select Karlach, then level_up(who="Karlach") out of combat)
- S051: Claude and Shadowheart reached L9 [seen]; Wyll and Karlach L9 pending
- NOTE S044: L9 lines are [guess] (Paladin/Barbarian L9 = no choices; Cleric L9 prepares spells: check get_level_up_options)
- S044: Claude now wields Infernal Rapier (from Mizora) [seen S044]
- Wyll L4: {"feat":"Ability Improvement","abilities":{"Charisma":2},"cantrips":["Booming Blade"],"spells":["Scorching Ray"]}
- Wyll L5: {"passives":["Fiendish Vigour"],"spells":["Hypnotic Pattern"]}
- Wyll L6: {"spells":["Hunger of Hadar"]}
- NOTE S032: Wyll L6 line rewritten from the game's offer list (failure was the tool's page, fixed; Hunger of Hadar kept)
- Wyll L7: {"spells":["Banishment"],"passives":["Devil's Sight"]}
- NOTE S029: Wyll L7 line rewritten from the game's offer list (Agonising Blast already known; invocation picked from its L7 list)
- Wyll L8: {"feat":"Ability Improvement","abilities":{"Charisma":1,"Constitution":1},"spells":["Blight"]}
- NOTE S048: Wyll L8 line rewritten from the game's offer list (Charisma was 19; done S048 with CHA+1 CON+1, Counterspell)
- Wyll L10: {"cantrips":["Toll the Dead"],"spells":["Cone of Cold"]}
- NOTE S048: Wyll L10 line rewritten from the game's offer list
- Wyll L11: {"spells":["Circle of Death","Flame Strike"]}
- NOTE S052: Wyll L11 line rewritten from the game's offer list (Mystic Arcanum 6 + Fiend level-5 pick)
- Shadowheart L10: {"cantrips":["Produce Flame"]}
- Karlach L10: {}
- Karlach L11: {}
- Karlach L12: {"feat":"Ability Improvement","abilities":{"Strength":2}}
- Claude L10: {}
- Claude L11: {}
- Shadowheart L11: {}
- Claude L12: {"feat":"Ability Improvement","abilities":{"Strength":2}} (done S085; Tough/Alert already taken)
- NOTE S057: Claude L12 line rewritten from the game's offer list (offers: feat; Tough chosen as a no-choice feat [guess]; if refused use Alert-style no-choice feat from its list)
- NOTE S053: Karlach/Claude L10-11 and Shadowheart L11 have no picks to make by default (paladin prepares spells; barbarian/cleric L11 have no choices) [guess]; if level_up fails, rewrite from its offer list
- NOTE S048: Shadowheart L10 line rewritten from the game's offer list
- NOTE S033: Wyll L8 line rewritten from the game's offer list (1 spell of level 4: Blight chosen from the offers)
- Wyll L9: {"spells":["Hold Monster"],"passives":["Mire the Mind"]}
- NOTE S042: Wyll L9 line rewritten from the game's offer list (1 Fiend spell of level 5 + 1 invocation)
- S042: whole party reached L7 at the end of the session; Wyll took Banishment; Wyll wields Moonlight Glaive + Circle of Bones; Claude wears Plate Armour (AC 18); Shadowheart lost her spear (Shar), uses Handaxe +1 + Absolute's Protector shield [seen S042]
- S062: Claude, Wyll, Shadowheart reached L10 (Shadowheart Thaumaturgy; Wyll Toll the Dead + Cone of Cold); level_up spell picks work after set_party select <who> [seen]
- Wyll L12: {"spells":["Wall of Fire"],"passives":["Book of Ancient Secrets"],"feat":"Alert"}
- NOTE S062: Wyll L12 line rewritten from the game's offer list (Fiend level 5 spell, invocation, feat; feat Alert is a no-choice [guess])
- Shadowheart L12: {"feat":"Alert"}
- NOTE S062: Shadowheart L12 line rewritten from the game's offer list (feat only; no-choice feat [guess])
- S083: active party Claude, Wyll, Gale, Shadowheart; no level-ups since S078. Gale's Thunderwave breaks hidden/invisible walls (piercing does 0) [seen S083]
- Last updated: S090 (no change; Wyll revived S090, party Claude/Wyll/Gale/Shadowheart). Earlier: S083 (no change to levels). S078 note: (Wyll's Devil's Sight (L7) is the planned answer to Sharran Darkness at Viconia) [seen S078]
- Gale L11: {"spells":["Chain Lightning","Otiluke's Freezing Sphere"]}
- NOTE S068: Gale L11 line rewritten from the game's offer list
- Gale L12: {"feat":"Alert","spells":["Disintegrate","Globe of Invulnerability"]}
- NOTE S068: Gale L12 line rewritten from the game's offer list (feat Alert is a no-choice feat [guess])
- Wyll L6: DONE S033 (Hunger of Hadar) [seen]
- Gale L6: DONE S029 (Haste, Slow; worked on 4th try)
- Gale L8: {"feat":"Ability Improvement","abilities":{"Intelligence":2},"spells":["Polymorph","Wall of Fire"]}
- NOTE S029: Gale L8 line written from the game's offer list (pick 2 of Wizard level 4 + feat)
- S029: Claude, Karlach, Shadowheart, Gale all L6 [seen]
- S028: Wyll levelled L4+L5 at camp; Claude/Karlach/Shadowheart/Gale L5. Potent Robe (CHA-caster robe) in Gale's bag for Wyll [seen]
- NOTE 2026-10-10 (harness check vs the game's offers): Wyll's Pact of the Blade extra attack (Deepened Pact) comes automatically at L5, it is NOT an invocation; clerics (Shadowheart) prepare spells and pick none at level-up; Gale already knows Misty Step. Wyll L5 offers: invocations Mire the Mind, Sign of Ill Omen, Armour of Shadows, Beast Speech, Beguiling Influence, Devil's Sight, Fiendish Vigour, Mask of Many Faces, One with Shadows, Thief of Five Fates + one spell up to 3rd level.

## Claude (main)
- Paladin, Oath of Devotion; warhammer + shield; frontline tank/smiter and party face. [seen S001]
- L2 fighting style picked as Dueling (result said "passive defaulted": verify on get_party) [seen S003]
- Plan [wiki]: STR > CHA > CON; Divine Smite on crits and bosses; Bless, Shield of Faith, Command; L4 +2 STR; Extra Attack at L5 (knowledge/builds.md).

## Shadowheart
- Cleric, Trickery; healer/support; Guiding Bolt is her best attack. L4 +1 WIS +1 CON [wiki]

## Gale
- Wizard, Evocation; has Scorching Ray, Misty Step, Magic Missile, Sleep, Grease, Thunderwave, Shield [seen S018]. L5 Fireball.

## Wyll
- Warlock (The Fiend), Pact of the Blade, Misty Step [seen S010]. Eldritch Blast + Agonizing Blast [wiki]

## ARCHIVE
- ARCHIVE: a duplicate "Claude (main)" section (S001 text: frontline tank/smiter; Divine Smite for crits/bosses; Bless/Shield of Faith early) merged into the one above in S018.
- S068: Gale levelled 6→10 (L8 ASI +1 Int +1 Con; spells Ice Storm, Greater Invisibility, Wall of Fire, Banishment, Cone of Cold, Hold Monster, Wall of Stone, Otiluke's Resilient Sphere; cantrip Bone Chill). Party now Claude, Karlach, Shadowheart, Gale; Wyll at camp [seen S068]
