# Companions: where each one matters

The bridge reads the table below (bridge/src/bg3ai/companions.py): when a companion waiting at camp is tied
to the current region or to an active quest, get_status prints a `PARTY` hint (→ `swap_party`). Keep the
format: one row per companion, `;`-separated lists, matching is case-insensitive substring on the region name
and on active quest titles/objective names. From bg3.wiki; check `wiki_page` before a big decision.

| Companion | Class | Regions | Quests / keywords | Why bring them |
|---|---|---|---|---|
| Halsin | Druid | Shadow-Cursed Lands; Last Light; Reithwin | Lift the Shadow Curse; Thaniel; Oliver; Last Light; Halsin | his own quest: only he can enter Thaniel's realm with you and lift the curse; healer, wild shape tank |
| Wyll | Warlock | Wyrm's Crossing; Rivington; Lower City; Iron Throne | Blade of Frontiers; Grand Duke; Ravengard; Mizora; Avernus; Wyll | his pact (Mizora) and his father (Duke Ravengard) scenes need him present; ranged Eldritch Blast, face (CHA) |
| Karlach | Barbarian | Last Light; Wyrm's Crossing; Lower City | Hunt the Devil; Our Fiery Friend; Dammon; infernal engine; Gortash; Zariel | Dammon (Last Light, later Rivington) upgrades her engine only when she is with you; best tank |
| Shadowheart | Cleric | Shadow-Cursed Lands; Gauntlet of Shar; Lower City | Daughter of Darkness; Nightsong; Gauntlet of Shar; Shar; Selûne; House of Grief; Shadowheart | the Gauntlet of Shar and the Nightsong are her story: bring her there; best healer |
| Gale | Wizard | Lower City; Rivington; Upper City | Netherese; Elminster; Mystra; Karsus; Wizard of Waterdeep; Ramazith; Gale | Elminster and the Netherese orb scenes need him; arcane AoE |
| Lae'zel | Fighter | Rosymorn Monastery; Githyanki Creche; Mountain Pass; Lower City | Githyanki Creche; Vlaakith; Voss; Orpheus; Find a Cure; Lae'zel | the creche and Vlaakith/Orpheus scenes are hers; strong frontline |
| Astarion | Rogue | Shadow-Cursed Lands; Lower City; Cazador | Cazador; Gur; Vampire; Szarr; Astarion | the Gur hunters (Act 2) and Cazador (Act 3) are his; locks, traps, Sleight of Hand, stealth |
| Jaheira | Druid | Last Light; Lower City; Baldur's Gate | Harpers; Isobel; Minsc; Jaheira; High Harper | recruit at Last Light after Moonrise; Harper quests and Minsc need her |
| Minsc | Ranger | Lower City; Baldur's Gate | Minsc; Stone Lord; Boo; Counting House | Act 3; Jaheira is the key to recruiting him |
| Minthara | Paladin | Moonrise Towers; Lower City | Minthara; Absolute; Moonrise | only if recruited (Act 2, knocked out not killed in Act 1) |

## Skill needs (what the PARTY hint checks)
- **Locks and traps**: Sleight of Hand (Astarion, any Rogue). Below +4 in the party → many locks fail.
- **Face**: Persuasion / Deception / Intimidation (CHA: Claude-Paladin, Wyll, a Bard). Story checks hinge on it.
- **Perception**: hidden traps, secret doors (Wisdom: Shadowheart, Halsin, Jaheira, Astarion).
- **Composition**: one healer (Cleric/Druid/Bard), one frontline (Fighter/Barbarian/Paladin), ranged
  damage (Warlock, Ranger, Wizard, Rogue), area control (Wizard, Druid, Sorcerer, Cleric).
