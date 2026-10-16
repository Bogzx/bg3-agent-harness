# New-game setup and standing settings

The aim is to **finish** the campaign, so every choice here trades flair for robustness. If the
operator's instructions say otherwise, follow those instead.

## Difficulty

- **Explorer** is the default for a new game (`new_game` uses it), unless the kickoff gives the profile's
  difficulty. Enemies have less HP, the player side gets a bonus, and every timer is longest (Iron Throne: 8
  turns vs 6 on Balanced). Long rests cost 40 camp supplies.
- **Karmic Dice: on** (Options → Gameplay). It smooths out streaks of bad rolls.
- **Never** Honour mode, and never Custom with "Honour single save". A party wipe would end the run.
- You can raise the difficulty later (to Balanced) once the operator agrees; it's an options change, not a new game.
  `[wiki]` Confirm the option is there in this patch before relying on it.
- If the operator chooses Custom, these settings help without changing the game much: "No death saving
  throws" on (downed allies stay stable), camp cost ×1.

## Main character (Tav)

**With a character brief** (the kickoff says CHARACTER CREATION), the character is yours to design from it:
follow `playbooks/character-creation.md`; the build below is only the fallback for what the brief leaves open.
**Without a brief**, create this default build, named as the kickoff says (no name in the kickoff: choose a
distinctive one; it becomes the playthrough's save owner):

- **Tav, not the Dark Urge.** The Urge adds forced events and extra irreversible choices.
- **Half-Orc Paladin, Oath of Devotion.** Heavy armour, Lay on Hands for emergency healing, Divine Smite for
  burst damage, and Charisma for dialogue. The PC speaks in almost every conversation, and the checks that
  matter most (Persuasion and Intimidation) use Charisma. Half-Orc's Relentless Endurance saves one knockout
  per long rest. (Githyanki is a fine alternative: a free Misty Step.)
- Abilities (point buy): STR 16 · DEX 10 · CON 14 · INT 8 · WIS 10 · CHA 16, putting the racial bonuses into
  STR and CHA.
- Background: **Soldier** or **Noble** (Athletics, Intimidation, Persuasion).
- Fighting style: **Great Weapon Fighting** with a two-handed weapon (or Defence with a shield).
- Level 4 feat: +2 STR. Level 8: Great Weapon Master or +2 CHA. **Stay single-class.**
- Spells to keep prepared: Bless, Cure Wounds, Shield of Faith, Command. Later: Aid, Revivify (level 5+).
- Record the plan in `memory/party-build.md`.

## Party

| Slot | Companion | Role | Why |
|---|---|---|---|
| PC | your character (default: Paladin) | front line, face, emergency heals | see above |
| 2 | **Lae'zel** (Fighter, Battle Master or Champion) | damage and tank | simple to play, hits hard |
| 3 | **Shadowheart** (Cleric, Life domain) | healer and support | Healing Word, Bless, Spirit Guardians, revives |
| 4 | **Astarion** (Rogue, Thief) with a bow | ranged damage, locks, traps | lockpicking, disarming traps, Sleight of Hand, Perception |

- **Gale** is strong but brings extra risk: his orb has to be fed magic items, and his death starts a
  3-long-rest countdown to a Game Over. Keep him safe in camp and handle his requests, or use him only
  when you need teleports (Dimension Door) for the Gauntlet or the Iron Throne.
- **Karlach** is the backup for Lae'zel: a simple, durable melee fighter.
- Keep at least three characters in the active party at all times (some camp scenes are deadly with only two).
- Approval: a pragmatic, kind play style keeps this group together. Don't side against a companion in
  their personal quest unless you've decided to, and recorded it in decisions.md.

## Low-maintenance play

- Level up with the game's suggested picks unless party-build.md says otherwise.
- Everyone carries 3+ healing potions. Shadowheart keeps a Revivify scroll once you can get them.
- Send junk and food to camp; the Paladin holds gold and quest items.
- Keep 80+ camp supplies in camp. Food is everywhere, so pick it up.
- Equip an upgrade only when the tooltip shows a clear gain (AC, damage). Don't theory-craft.

## Reaction settings (no prompts)

Prompts pause combat and are easy to miss. Set each character's reactions (character sheet → Reactions)
so that nothing asks:

| Reaction | Setting |
|---|---|
| Opportunity Attack | on, never ask |
| Divine Smite (Paladin) | off. Smite deliberately as an attack variant on your turn instead |
| Hellish Rebuke, Shield, Counterspell, Uncanny Dodge, etc. | on, never ask |
| Riposte, other Battle Master manoeuvres | on, never ask |

Also keep the custom-difficulty display option "Always prompt reactions" off (its default) `[wiki]`.

## Saving

Follow `CLAUDE.md` → Save discipline. Name saves `Sxxx-<slug>`. In short:

- **Quicksave** before every fight you start, every dialogue with a visible skill check, every puzzle,
  every jump across a gap, and any lockpicking or trap attempt that matters.
- **Named hard save** at session start, after each main-quest step, at each new region, and **always** before
  any point of no return in walkthrough.md (`Sxxx-pre-shadowfell`, `Sxxx-pre-ironthrone`,
  `Sxxx-pre-morphic`, …).
- Never save while someone is dying, in a fight that's going badly, or right after a choice you regret.
- **Reload** only after a party wipe or Game Over, or a clearly unintended catastrophe such as a misclick or bug.
  Don't reload to reroll a failed dialogue check. Log every reload with the reason.
- **After a wipe:** load the latest save. If that save is doomed (mid-fight with everyone down), load the
  previous hard save. Before retrying, write down what you'll change: rest first, a different approach,
  consumables, positioning, or skipping the fight. If the same fight has wiped you twice, change the plan.

## Rest cadence

- You get **2 short rests per long rest**. Use them after any fight that cost more than a quarter of
  party HP.
- **Long rest** when party spell slots or HP are below about half after short rests, when the journal asks for
  it ("celebrate", "rest and reflect"), and before each point of no return.
- **Don't long rest** while a deadline in walkthrough.md ("Before every long rest") is open.
- After each long rest, walk around camp and talk to anyone with a "!" marker before leaving.
