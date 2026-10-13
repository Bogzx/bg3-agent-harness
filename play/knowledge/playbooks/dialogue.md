# Dialogue

**When:** Use whenever a conversation is open (talk_to result, INTERRUPTED — dialogue, dialogue_line events, a cutscene ending in choices) or before starting one with a named NPC. Covers playing your character (CHARACTER.md, roleplay strict/light), get_dialogue/choose_dialogue/roll_check, batching obvious answers with do_actions, skill checks, approval, lies, and recording promises and decisions.

## Before you talk to a named NPC
- `Grep` for the name with path `memory` (npcs.md, decisions.md, quests.md). Check decisions.md for related
  threads and objectives.md for what you need from them.
- Big or risky conversation (an NPC who might turn hostile, a faction leader, a boss)? **Save first**
  (`save_game(label="Sxxx-pre-<name>")`).
- **Choose who speaks before you start.** The speaker can't change mid-conversation. Send your best talker
  for the checks you expect: `talk_to(npc="e8", who="<best talker>")`. Charisma (Persuasion, Deception,
  Intimidation) for most NPCs, and Insight to read them. A party member's skill bonuses are on
  `get_character`, and the tagged options in a dialogue name the party's best for next time.
- **Conversations that start by themselves** (ambushes, NPCs who call out as you walk past) are spoken by
  whoever walked into them. In goblin/bandit/cultist areas, walk with your best talker in front
  (`travel_to(..., who="<best talker>")`). S013: Shadowheart (Persuasion −1) got the goblin talks that
  Claude (+6) should have had.
- **Pre-buff the speaker** when a check is likely: Guidance (+1d4 to ability checks, a cantrip) cast with
  `use_ability` just before talking, or Bardic Inspiration once you have a bard. It costs one action out
  of combat, so do it for every named-NPC talk where memory or the wiki expects a check. (Through S014 it
  was never used.)
- `look_at(target)` first if you need to know who someone is (the Examine panel, nothing hidden).

## Playing your character
With a `memory/CHARACTER.md` (injected at session start), every real choice is made **as that character**:
first ask which answers they could say (Values, Won't do, Voice, How they decide), then use the rules below
(odds of a check, safety, companion approval) to choose among those. Its `Roleplay:` line sets the weight:
- **strict** (the default): in-character first. The odds, safety and approval rules are **tie-breakers among
  in-character answers**. Never pick an answer the character wouldn't give because its check has better odds or
  a companion would approve; if every in-character answer is risky, save first and take the best of them. An
  answer that breaks a `Won't do` line is out, whatever it gains.
- **light**: the persona is one weight among equals: weigh in-character fit, odds, safety and approval together
  and take the best overall; still never break a `Won't do` line.
- A choice the character makes that a careful player wouldn't is fine: log it, live with the result (no
  reload to undo it).
- Each `[decision]` note and decisions.md entry gives the **in-character reason**:
  `note("[decision] Mira refused the tadpole power: she never lets a mind flayer in her head")`.
- Without CHARACTER.md (the default build), use the rules below as they stand.

## In the conversation
Only filler is clicked for you: a Continue prompt or a bare "…"/End. The `talk_to` result (or any
result where a conversation opened) and every `choose_dialogue` result already show the next real choice,
roll or the end, so you don't need `get_dialogue`/`dialogue_advance` then. **Every answer, even the only one,
is yours to give.** When the next few answers are obvious (greetings, known safe choices), you can chain
them in one `do_actions` call (`choose_dialogue` steps). It stops at any roll, new choice set or the end.
`dialogue_advance()` waits out a line that is still playing, and it also says a lone plain answer for you.

Tagged options show the check and your speaker's own bonus. The DC appears only on the roll screen, after
you pick:
```
  2. [PERSUASION] Let me through.  ← Persuasion check: e2 +5 (party best e1 +6: they can start the next talk)
  3. [WIZARD] The runes are Netherese.  ← class/race/background option
```

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
`dialogue_line` events. The options come with the result (else `get_dialogue`). Answers are **never**
picked automatically: every choice is yours.
- **Read every option** before choosing. Options that end the conversation early ("Leave", "Attack",
  "[Illithid]") are often irreversible.
- **Look for the check options first** (among in-character answers when roleplay is strict).
  [Persuasion]/[Intimidation]/[Deception], ability checks and class or race options usually give the better
  outcome. Examples: a fight avoided, a better price, extra
  information, inspiration and XP, companion approval `[wiki]`. Take one when the odds are good:
  - a bonus of +3 or more on a routine check (most early DCs are 10–15 `[wiki]`);
  - or Guidance or Bardic Inspiration is available (the roll screen offers it: `roll_check(boosts="Guidance")`);
  - or a failure is cheap (the NPC only refuses).
  - With a low bonus and a costly failure (the NPC turns hostile, a companion leaves), pick the safe answer
    or save first.
- **Class/race/background options** ([CLERIC], [HALF-ORC], [SOLDIER]…) need no roll and often give unique,
  in-character results. Prefer them when they fit your goals.
- Natural 20 always succeeds and natural 1 always fails on dialogue checks `[wiki]`. Inspiration points
  allow one reroll (`reroll_on_fail=true`). Use it on checks that matter.
- **Think harder on story choices.** For companions, factions, sparing or killing, [Illithid] and promises,
  read every option and check memory (decisions.md, npcs.md) and CHARACTER.md (How they decide). Write the
  decisions.md entry first, with the in-character reason. Routine
  answers (greetings, asking questions) deserve no more than a moment.
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
