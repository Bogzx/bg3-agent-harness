# bg3ai bridge: tool interface changes

Every change to what a client sees (tools/list, server instructions, tool result wording a model acts on) gets an
entry here, and the golden dump (tests/golden/tools_list.json) is regenerated with
`cd bridge && uv run python tests/test_tools_golden.py --update` in the same commit. The package version stays
0.1.0 until uv.lock can be relocked.

## 2026-11-01: fast_travel from camp names the conversation screen; get_status stops re-sending a refused re-chain

- fast_travel from camp with a conversation screen up fails "FAILED: a conversation is open, nothing was pressed;
  the game doesn't answer the camp button while a conversation screen is up" instead of "can't fast travel now
  (Condition)" (live-v09c 1c: ui.fast_travel's dry run reads TravelError before IsInCamp, so under the dream's
  narrator screen it refused "(Condition)" and the leave-camp-first check never ran).
- get_status/look re-chain an unchained member once per spot: when the game didn't attach it (act.chain failed or
  ran out its 3 s), it is sent again only after the member or the leader moved 5 m (live-v09c c2: 3 reads at 3.1 s
  each with "Shadowheart 99m, unchained"; 76 of 245 logged re-chains timed out, all 78 m or more apart). The
  PARTY APART line is unchanged.
- act.chain (mod ActionsParty.lua): an attach the game doesn't take fails once the member is still not in the
  leader's group 0.6 s after the order ("the game didn't attach <who> to <leader>'s group: still apart 0.6 s after
  the order (<n> m away)"), not after the job's 3 s timeout. The game gives no refusal of its own (the Osiris call
  returns, nothing is logged); the 60 logged re-chains that took showed InSamePartyGroup within 182 ms of the order.
  The first get_status after such a split, regroup's and set_party's chains wait 0.6 s instead of 3 s.

## 2026-11-01: camp leave with a conversation screen up refuses at once; no "[ForceUpdate]" notice

- camp leave / fast_travel from camp: with a conversation screen up on the client (ui.travel_block dialogue), they
  fail at once with "FAILED: a conversation is open, nothing was pressed; the game doesn't answer the camp button
  while a conversation screen is up" and press nothing (live-v09b: right after the 1st long rest's dream, the
  rest's narrator screen still up, the server's conversation check let the press through; it went unanswered for
  13.5 s and failed "cannot travel right now" with no reason).
- A press the game still doesn't answer ("cannot travel right now": act.leave_camp's wait ran out) gets on its
  FAILED line " The game didn't answer the camp button: the party stayed at camp. When it was pressed: <facts>.
  After the wait: <facts>." (facts from ui.travel_block: night at camp, the travel buttons greyed out
  (<TravelError>), the HUD hidden) instead of the block read only after the wait.
- get_status NOTICE (ui.state notifications, mod UIState.lua): a TextBlock whose Text is the formatter's
  "[ForceUpdate]" placeholder reads its inlines; with none it is left out (live-v09b: "NOTICE [ForceUpdate]" after
  a camp leave).

## 2026-11-01: use_ability's opener, camp leave in a night camp talk and WHERE after a fast travel say what is true

- use_ability out of combat at a creature: when a conversation is open after the hit (live-v09: the game's "You
  picked a fight" scene, with combat on 0.3 s after the result), the result says "combat: not started — a
  conversation is open, opened by the hit" (without ", opened by the hit" when one was open before the cast)
  instead of "combat: NOT started — … no fight began (dormant or unaware …)".
- camp leave / fast_travel at night camp during a camp conversation fail at once with "FAILED: a conversation is
  open, nothing was pressed; at night in camp, leaving first ends the night (the party sleeps: a long rest)" instead
  of long_rest's "… then long_rest".
- get_status after a teleport (fast_travel, travel_to's fast-travel leg, camp go/leave): while the minimap's
  subregion line (WHERE's subregion) still shows the name it had when the trip left, WHERE leaves it out and a line
  says "SUBREGION not updated since <the trip to <waypoint> | the trip to camp | leaving camp>: the minimap still
  shows '<name>', as it did before" (live-v09: "WHERE Blighted Village" 0.6 s after arriving at Roadside Cliffs).
  Nothing waits for the line.

## 2026-10-30: a give the game refuses fails; get_character shows status names only; a busy UI reads as busy

- manage_inventory op=give: when the game leaves the item with the giver (no error, no move: a story item), the
  give fails after its 8 s wait with "the game kept it with <giver>: it can't be handed over" instead of
  "TIMEOUT — timed out" (mod act.transfer on_timeout).
- get_character shows each status by the name the UI shows, without the engine's status id, on a party member's
  sheet and on an examined character, as look_at already did. It served ids like DIFFICULT_TERRAIN_DEEPWATER.
- camp leave / fast_travel at camp: when the game's UI is busy (bg3le refuses the read of whether a long rest's night
  still blocks travel) they fail at once with "FAILED: can't read whether a long rest's night at camp still blocks
  travel: bg3le: UI busy …" instead of reading it as no night and failing later "cannot travel right now".
- pick_up on an item the game can't pick up (fixed in place, listed `<usable>` like a wall torch) fails at once with
  "that can't be picked up: it is fixed in place (interact uses it)" instead of "TIMEOUT — timed out" after 40 s.
- With a dead member that still holds the game's ClientControl (its corpse once selected), the selected member is
  the living one set_party selected: get_status/look's active member and PARTY APART, fast_travel's LEFT BEHIND
  and "already stands at the waypoint" answer, and nav reads measured from the corpse before. A fast_travel to the
  waypoint the selected member stood on then timed out after 66 s instead of answering at once. fast_travel also
  watches the living holder for the arrival, so a trip that moved it no longer waits on the corpse.
- short_rest in combat, or while any party member is in a conversation (also one that isn't selected), fails at once
  with "not pressed: in combat" / "a party member is in a conversation" instead of "TIMEOUT — timed out" after 15 s:
  the game ignored the press there while the selected member's rest button still showed as available.

## 2026-10-30: do_actions stops at a 90 s time budget

- do_actions (the blocking call) stops before its next step once it has run 90 s (`actq.BATCH_BUDGET_S`), and a
  wait step is cut to the time left ("wait: waited 6.0s of 25s (time budget)"). The result ends
  "STOPPED: time budget (90 s per call), N steps left; not run: …". The clients give up on an MCP request after
  110 s: six 30 s waits failed the call as "MCP request timed out" while the bridge ran the batch on for 70 s more.
  queue_actions (do_actions wait=false) has no budget: it returns at once and runs while the agent plays on. Its
  description names the budget.

## 2026-10-24: feed_gale closes the donation window and checks Gale took the item

- feed_gale donates through the window's DonateItemsCommand, clicks the window's X (Gale absorbs the item when it
  closes) and says done only when the item left the party and Gale's Arcane Hunger is gone. Live D10 it said
  "done — donated=e132 window_open" with nothing given. Its description says it donates and closes the window.

## 2026-10-22: served rules follow the served tools; no screenshots for a text-only model

- In the rules condition the served rules (instructions, first briefing, `rules` prompt, bg3://rules, pi's system
  text) leave out the rules part of a tool group the session switched off (BG3AI_TOOLS / BG3AI_TOOLS_OFF): with
  screenshots off they still described `screenshot` and `zoom`. rules+coaching keeps the whole text (it needs every
  group).
- A pi profile whose model has no image input runs with the screenshots group off (bg3harness profiles.tools_off,
  PiClient's server env): it was told "(see attached image)" and given no picture.

## 2026-10-22: coaching nudges only in a coaching condition

- The `PACE: you waited right after issuing…` and `NOTES WINDOW: ~Ns of walking left…` (with its PARTY PLAN line)
  result nudges are coaching: served only when `$BG3AI_PROMPT_CONDITION` is `rules+coaching` (rules.coaching).
  Rules-only sessions (the default, benchmark ranked runs) get neither. Playthroughs set rules+coaching (runner;
  play/.mcp.json for Claude Code).

## 2026-10-22: live4 §2/§5 fixes (pickpocket, summon attack, flee, trade table)

- pickpocket: a window it didn't open is closed and then waited for (≤2 s) before this NPC's is opened; the open
  sent right after the close went with it ("FAILED — no pickpocket window is open" 2/2 after another process left
  the window open). A window that stays open says so.
- pickpocket(take=...): the POCKETS line after a steal is read again (it still listed the stolen Gold x15).
- combat_turn / use_ability: a guessed Target_MainHandAttack for a character with no weapon attack becomes its
  Unarmed Strike (a summoned Cambion: "the character does not have that action").
- flee's "enemy too close" names the enemy from the fight's turn order ("e7 e7" before).
- The trade table says "the trader pays you Ng on confirm" for an overpay their gold covers (confirm presses the
  game's Balance; "you overpay 30g" was followed by +30 gold), and what is still overpaid when it doesn't.

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
