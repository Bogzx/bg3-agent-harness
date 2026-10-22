# bg3ai bridge: tool interface changes

Every change to what a client sees (tools/list, server instructions, tool result wording a model acts on) gets an
entry here, and the golden dump (tests/golden/tools_list.json) is regenerated with
`cd bridge && uv run python tests/test_tools_golden.py --update` in the same commit. The package version stays
0.1.0 until uv.lock can be relocked.

## 2026-10-22: benchmark neutrality audit (docs/BENCHMARK-BIAS.md status table)

- The server instructions say what the tools do, not how to play well: "Pipeline like a human clicking ahead",
  "keep planning while the character walks; wait_for only when ..." and "Use screenshot only when ..." are gone
  (coaching/pace.md and efficiency.md have them for the coaching condition). Every client and condition got them.
- level_up's and create_character's `plan` and set_reactions' `settings` take a JSON object as well as the same
  JSON as a string (D15); their schemas said `string` while the descriptions said "plan: JSON like {...}".
- combat_turn's and wiki_page's descriptions lost their session stories (S023-S026, S018-S027).
- The `rules` prompt and bg3://rules resource no longer describe themselves as "play/CLAUDE.md, the rules Claude
  Code loads".
- The server-side stop refusal names only the tools the session has: with the memory group off it no longer says
  "call end_session" or "HANDOFF.md", with saves off it no longer names save_game.

## 2026-10-22: run 1 result wording (live3, pi + DeepSeek Flash)

- A do_actions/queue_actions/combat_turn step whose tool can't be a step (look, loot_area, ...) is refused with the
  list of tools a step can be, and "call other tools on their own".
- insert_item on an object that answers with a conversation (the Nautiloid's rune console) says "using it opened a
  conversation instead" when the conversation comes up after the interact returns too; it said "no Insert Item
  panel opened ... not every pedestal takes items" while the conversation offered the insert.

## 2026-10-22: D15 tool interface review (one interface for every model)

- do_actions and queue_actions take `actions` as a JSON array (or the `{"actions": [...]}` object), and combat_turn
  takes `plan` as a JSON object or array of member plans. The JSON string form still works. The schemas used to say
  `string` only, so a model that follows a schema literally had to escape JSON inside a string.
- The server instructions are built from the served tool set (capabilities.py): with the memory group off they no
  longer name memory_* tools, knowledge/, session_start or end_session, and with screenshots off they no longer
  mention screenshot.
- Wording for any client, not only Claude Code: "end your turn" became "stop (no more tool calls)" in the
  instructions, end_session's description and its results. note's description says memory_read/memory_edit
  instead of Claude Code's file Read/Edit.
- server.INSTRUCTIONS and server.RULES exist again (tools/pi/dump_server.py reads them; the server.py split had
  dropped them, so `dump_server.py --system` failed with AttributeError).
- Size (all 94 tools, chars; ~chars/4 tokens): tools/list wire form 68,499 -> 69,023 (+524, the anyOf schemas),
  descriptions 35,909 -> 36,010 (+101), instructions 1,398 -> 1,410. With BG3AI_TOOLS=game (74 tools) the
  instructions are 869 chars.

## 2026-10-22: pickpocket fixes (research/ovh/live2.md §1)

- pickpocket(take=...) clicks Steal for real: the mod selects the item and reads its odds without pressing
  (ui.pickpocket min_chance=101), then hoverclick.py moves the cursor until the mod's new ui.steal_probe
  (Client/PickpocketOps.lua) says it is over StealBtn, clicks, and reads the roll. The old Command press never
  started a steal (14 live tries). min_chance is checked in the bridge. A window that closes right after the click
  reads "CAUGHT?".
- pickpocket(npc) closes a pickpocket window this server didn't open for that NPC before opening theirs (a stale
  window of another NPC was listed as this NPC's pockets).
- The description and the pockets listing say the shown odds assume nobody sees the thief.

## 2026-10-22: flee tool (research/ovh/live2.md §2)

- New tool `flee(who)`: the HUD's Flee for one character. Refused while a hostile the character can see is closer
  than 27 m (the button's tooltip rule; state.combat lists only seen combatants). Otherwise it selects the
  character, runs the HUD's flee command when a page has one (Client/FleeOps.lua ui.flee_probe / ui.flee), or clicks
  the Flee button hover-checked (hoverclick.py), answers "Do you want to flee from combat?" with Yes (boxclick.py)
  and checks the character left the fight. The flee command and button names were not read live yet: FleeOps finds
  them by "flee" in their names. The tool count goes from 94 to 95.
