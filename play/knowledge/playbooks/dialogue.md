# Dialogue

**When:** Use whenever a conversation is open (talk_to result, INTERRUPTED — dialogue, dialogue_line events, a cutscene ending in choices) or before starting one with a named NPC. Covers get_dialogue/choose_dialogue/roll_check, batching obvious answers with do_actions, skill checks, approval, lies, and recording promises and decisions.

## Before you talk to a named NPC
- `Grep` for the name with path `memory` (npcs.md, decisions.md, quests.md). Check decisions.md for related
  threads and objectives.md for what you need from them.
- Big or risky conversation (an NPC who might turn hostile, a faction leader, a boss)? **Save first**
  (`save_game(label="Sxxx-pre-<name>")`).
- Want to pass a check? Pre-buff the speaker: Guidance gives +1d4 to ability checks. Cast it
  (`use_ability`) on the speaker just before talking. Choose who speaks: `talk_to(npc="e8",
  who="<best talker>")`.
- `look_at(target)` first if you need to know who someone is (the Examine panel, nothing hidden).

## In the conversation
When the next few answers are obvious (greetings, "continue", known safe choices), chain them in one
`do_actions` call (`choose_dialogue` steps). It stops at any roll, new choice set or the end of the
conversation, so you never pick blind. Read the result and decide the next real choice.

```
get_dialogue()                        # speaker, current line, numbered options with visible tags
                                      # ([Persuasion], [Insight], class/race tags, [Illithid]…), or the
                                      # roll / reaction / popup on screen with its displayed DC
choose_dialogue(option=2)             # returns the next line and options
roll_check(boosts="Guidance", reroll_on_fail=false)   # resolves the roll on screen; boosts only
                                      # from what the roll screen offers
do_actions(actions='[{"tool":"choose_dialogue","option":1},{"tool":"choose_dialogue","option":1}]')
                                      # chain obvious picks; stops at a roll or new choices
```
**Cutscenes and spoken lines are skipped automatically** by the mod: movies and cinematics are skipped
and voiced lines are fast-forwarded. Every line still reaches you as a `dialogue_line` event (speaker:
text) in the tool results. Don't `wait` for or watch cutscenes, and don't screenshot them. Read the
`dialogue_line` events, then `get_dialogue` for the options. Answers are **never** picked
automatically: every choice is yours.
- **Read every option** before choosing. Options that end the conversation early ("Leave", "Attack",
  "[Illithid]") are often irreversible.
- **Skill checks:** the game shows the DC before you roll. Prefer the check with the best modifier.
  Natural 20 always succeeds and natural 1 always fails on these `[wiki]`. If a failure would be
  catastrophic (combat you can't win, a companion leaving, a key NPC dying), make sure you saved first.
- **Reload policy:** don't reload to reroll a failed check (CLAUDE.md). Accept it, log it, adapt.
- **Insight / History / Arcana** options reveal information cheaply. Usually take them first.
- **[Illithid] powers and some tadpole options** have long-term consequences `[wiki]`. Make it a
  decisions.md entry before choosing.
- **Lies:** whatever an NPC claims is `[told]`, not `[seen]`. Write it that way.
- **Approval:** the game shows "<Companion> approves / disapproves". Note significant swings in the log
  (`[npc] Lae'zel disapproved: spared the goblin`). Track a companion's likes and dislikes in their
  npcs.md entry.

## Promises and decisions
- A promise ("I'll find your son", "I'll deal with the goblins") → PLAYTHROUGH.md "Standing
  commitments" plus an objectives.md entry.
- Irreversible choices (sides in a conflict, killing or sparing, giving away a key item, recruiting or
  rejecting a companion) → write the decisions.md entry **before** choosing: choice, why, expected
  consequences tagged `[wiki]` or `[guess]`. After seeing the outcome, add
  `Consequence: … [seen Sxxx]` lines.

## After the conversation
Log one line: `- HH:MM [npc] <Name>: <gist; what was learned or promised>`. Update the NPC entry with
tagged facts and new threads. Write new leads into quests.md or locations.md.

## Cutscenes and the camp
- After a cutscene, its lines are in the `dialogue_line` events. If `get_dialogue` shows no options and
  `get_status` reports a screen the tools don't model, `ui_dump()` shows what's on screen (and
  `ui_action` can act on it). Take a `screenshot` only if that dump doesn't explain it.
- At camp after a long rest, companions often want to talk (marked in `get_nearby`). These scenes drive
  their stories `[wiki]`. Do them unless time is short.
