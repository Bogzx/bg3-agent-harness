# Fairness: the agent only knows what a player could know

**Rule.** Everything the playing agent receives is something a human player could see on screen at that
moment, or remember having seen. General Baldur's Gate 3 knowledge (the model's own, or bg3.wiki
through a tool) is allowed, the way a player who read the wiki has it, and is tagged `[wiki]` in memory.

## Enforcement, layer by layer

| # | Layer | What it stops |
|---|---|---|
| 1 | **Perception gate** (`mod/.../Server/Visibility.lua`): an entity is reported only if a party member perceives it. That means engine `Osi.CanSee` (FOV, range, light, occlusion), or line of sight within that member's sight range (the free camera). | Enemies behind walls, out of range, or in darkness without darkvision |
| 2 | **Hidden-ness overrides** (same file): off-stage or script-invisible entities are dropped. Invisible enemies show only when revealed (See Invisibility, Truesight, Faerie Fire). Sneaking enemies show only once the engine sight sees them. Undiscovered traps are never flagged; trapped chests and doors look like ordinary objects. Secret doors are hidden until interactable. Scenery isn't listed. | Ambushes, stealthed or invisible creatures, traps and secret passages |
| 3 | **UI-mirroring allowlist projection** (`State.lua`): each output table is built fresh from whitelisted fields the in-game UI shows (tooltip, Examine panel, turn-order bar, party panel, journal). Names come from `GetDisplayName`, so disguises and "Hooded Figure" are kept. Statuses are limited to those with a portrait/Examine display. The game's Custom Mode HUD toggles (hide NPC HP, hide DCs, ...) are honoured. | True identities, technical or story statuses, internal template names, secret DCs, lock DCs outside the roll screen |
| 4 | **Container and journal gating**: container contents only while the loot window is open (or remembered from it). Only journal entries the journal shows. | Peeking into chests, reading quest flags or future quest steps |
| 5 | **Fair events** (`Events.lua`): the combat log, toasts and journal updates, with unseen actors anonymised. Failed perception checks never reveal their subject. | Learning about hidden things from side-channel events |
| 6 | **Opaque ids** (`bridge/render.py`): GUIDs are replaced by session-local handles (`e12`), and any GUID in free text is scrubbed. | Using GUIDs and template names as identity hints |
| 7 | **Sandbox, enforced by the MCP server** (`bridge/src/bg3ai/memtools.py`): the client runs with no built-in tools (`claude -p --tools ToolSearch --strict-mcp-config`), so the agent's only tools are the bg3 server's. Its memory tools read and write only `memory/` and read only `knowledge/` (shared by every playthrough, FAIR-2); `..`, absolute paths, symlinks, hidden and git files (`.git`, `.gitattributes`: FAIR-1), `snapshots/` and `markers.json` are refused. So: no shell, no web, no access to game files, saves, logs, the debug port or this repo's source. `play/.claude/settings.json` denies the built-ins again (defence in depth, no hooks). For a client in a container, `tools/container/` gives it a network that reaches only the server. | Bypassing layers 1–6 through the eval port, game data files or story files |
| 8 | **Fair actions** (`Guard.lua`): an action's targets must be perceivable. Cheat calls (teleport, add gold, unlock, apply status, set flags, ...) are blocked. Spells use the engine's checked path, so costs apply. | Acting on hidden things, and doing anything a player can't |

Screenshots (`screenshot`/`zoom`) are fair by construction: they are the rendered frame.

## Auditing
* **Per-session audit** (`runner/fairness.py`, a section of every `Sxxx.triage.md`): scans every game-tool result
  the agent got, and the runner's prompts (the `.log`'s `[prompt]` lines), for raw GUIDs, DCs outside the roll screen and the pickpocket window, passive-roll subjects, story-flag/template names,
  engine status ids, waypoint/level ids, side-channel flags (`unseen_combatants`, `geo_gen`), puzzle/lock verdicts
  from story data, an unseen target's fate and unlocalised engine text (LEAK), and hidden-state words on entity lines
  (CHECK). Memory, wiki and screenshot results are skipped. It is lexical: paths, maps and refusals built from hidden
  data pass it (audit C1, 2026-10-18). `python3 runner/fairness.py runtime/sessions/<run>/Sxxx` prints it alone.
* **Where the game itself shows a DC** (checked in the game's XAML, Game.pak + Patch8 overrides, 2026-10-17):
  the roll screen (`Library/DiceAnimation.xaml`: `Roll.DifficultyCheck`, hidden when `Roll.ShowDC` is false, which
  is how `DIALOGUE_DC_HIDE` reaches it; lockpick, disarm and dialogue checks all use it) and the pickpocket window
  (`Pages/Pickpocket.xaml`/`_c`: the selected item's `Roll.DifficultyCheck` under "Target", always shown, with the
  chance as a coloured pie). Nowhere else: not on dialogue options before the roll, not on locks or traps. So
  `pickpocket` prints "60% DC9" and `lockpick`/`disarm`/`roll_check` print the roll screen's DC, and nothing else does.
* `dev.fairness_probe` (server op) returns, for the current area, how many candidates each rule filtered.
  It returns counts only, never names.
* The fairness audit of 2026-10-10 is `research/fairness-audit-2026-10-10.md`.
* The agent is told to log `[note] fairness leak: …` if a tool ever shows it something a player couldn't
  see. The runner surfaces these notes.

## Known judgment calls
* Places are named as the map names them: get_map lists regions ("Shadow-Cursed Lands") and waypoint names
  ("Last Light Inn"), level_changed carries the region, and fast_travel/travel_to take those names. Level
  ids (WLD_Main_A) and waypoint ids (WAYP_CHA_Top) stay internal (decided 2026-10-10).
* "Perceived by any party member" is slightly *stricter* than a human with a free camera, who can pan to
  non-hidden NPCs far away. The screenshot tool covers that gap fairly.
* Examine-panel data (HP, AC, ability scores, resistances) is exported for visible creatures because the
  game shows it to every player via Examine.
* Operator text the agent reads (`knowledge/harness-news.md`, `tools.md`, tool docstrings) describes tool behaviour
  only: never a place, route, puzzle, item or encounter from a playthrough, and examples are made up (user,
  2026-10-18; bridge/tests/test_tool_docs.py guards the names that leaked). Game facts learned in a run go to
  research/ or docs/.
* The Faith-Leap Trial's floor mosaic is transcribed as a text picture (get_layout: its shape, 1 m per char,
  N up, no coordinates). No route along it and no warnings or refusals from the hidden platforms: a misstep
  has its in-game consequence (user, 2026-10-18).
