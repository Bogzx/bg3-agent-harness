# bg3ai bridge: tool interface changes

Every change to what a client sees (tools/list, server instructions, tool result wording a model acts on) gets an
entry here, and the golden dump (tests/golden/tools_list.json) is regenerated with
`cd bridge && uv run python tests/test_tools_golden.py --update` in the same commit. The package version stays
0.1.0 until uv.lock can be relocked.

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
