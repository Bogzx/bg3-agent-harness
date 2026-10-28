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
| 6 | **Opaque ids** (`bridge/src/bg3ai/render.py`): GUIDs are replaced by session-local handles (`e12`), and any GUID in free text is scrubbed. | Using GUIDs and template names as identity hints |
| 7 | **Sandbox, enforced by the MCP server** (`bridge/src/bg3ai/memtools.py`): every client runs with no built-in tools (Claude Code: `claude -p --tools ToolSearch --strict-mcp-config`; pi: no built-ins, only the bg3 tools through `tools/pi/bg3.ts`; Codex: its shell, file, web and agent features disabled; Gemini, unsupported: its built-in tools excluded; `bg3harness/clients.py`), so the agent's only tools are the bg3 server's. Its memory tools read and write only `memory/` and read only `knowledge/` (shared by every playthrough, FAIR-2); `..`, absolute paths, symlinks, hidden and git files (`.git`, `.gitattributes`: FAIR-1), `snapshots/` and `markers.json` are refused. So: no shell, no web, no access to game files, saves, logs, the debug port or this repo's source. `play/.claude/settings.json` denies the built-ins again (defence in depth, no hooks). For a client in a container, `tools/container/` gives it a network that reaches only the server and, through an egress proxy, its own model API. | Bypassing layers 1–6 through the eval port, game data files or story files |
| 8 | **Fair actions** (`Guard.lua` and `GuardGate.lua`, `GuardSpells.lua`, `GuardCast.lua`): an action's targets must be perceivable. Cheat calls (teleport, add gold, unlock, apply status, set flags, ...) are blocked. Spells use the engine's checked path, so costs apply. | Acting on hidden things, and doing anything a player can't |
| 9 | **Harness-only data stays out of the tools** (`Server/Referee.lua`, `Server/RollLog.lua`, `Server/Scenario.lua`): the referee ring and the roll log record hidden facts for the graders, and the scenario setup changes dice and names. None of them calls `RPC.push`, so nothing reaches the event stream. Only the harness calls their ops (`ref.*`, `roll.*`, `scn.*`), through `bridge/src/bg3ai/referee.py` and `bg3harness/game_settings.py`; `facts.jsonl` and `rolls.jsonl` are written to the run's directory, outside `memory/` and `knowledge/`. `bridge/tests/test_referee.py` fails if any bridge module under `bridge/src/bg3ai/`, the `tools/` subpackage included, names one of these ops or imports the reader, or if a registered tool is named for them. | The agent learning hidden outcomes, other rolls or the setup from the benchmark's own records |
| 10 | **Setup switch** (`Scenario.lua`): every `scn.*` op is refused unless the game process was started with `BG3AI_SCENARIO_SETUP=1`. The runner puts it in the game's env only for a seeded run (`Runner.setup_env`, `runner/play.py`); `tools/saves/make_start_save.py` needs it too. A normal playthrough never has it. | Setup ops running in a game that isn't a benchmark run or a start-save session |
| 11 | **Capabilities** (`bridge/src/bg3ai/capabilities.py`): `BG3AI_TOOLS` / `BG3AI_TOOLS_OFF` remove whole tool groups (memory, wiki, reload, saves, screenshots, raw_input) or single tools when the server starts, before any client lists them, and the rules text leaves out the rules of a group that is off. An unknown name stops the server. | A condition's switched-off tool (the wiki in a closed-book run, `load_game` with no reloads) being callable anyway |

Screenshots (`screenshot`/`zoom`) are fair by construction: they are the rendered frame.

## Auditing
* **Per-session audit** (`packages/harness/src/bg3harness/fairness.py`; a section of every `Sxxx.triage.md`): scans every game-tool result
  the agent got, and the runner's prompts (the `.log`'s `[prompt]` lines), for raw GUIDs, DCs outside the roll screen and the pickpocket window, passive-roll subjects, story-flag/template names,
  engine status ids, waypoint/level ids, side-channel flags (`unseen_combatants`, `geo_gen`), puzzle/lock verdicts
  from story data, an unseen target's fate and unlocalised engine text (LEAK), and hidden-state words on entity lines
  (CHECK). Memory, wiki and screenshot results are skipped. It is lexical: paths, maps and refusals built from hidden
  data pass it (audit C1, 2026-10-18). `uv run python -m bg3harness.fairness runtime/sessions/<run>/Sxxx` prints it alone.
* **Where the game itself shows a DC** (checked in the game's XAML, Game.pak + Patch8 overrides, 2026-10-17):
  the roll screen (`Library/DiceAnimation.xaml`: `Roll.DifficultyCheck`, hidden when `Roll.ShowDC` is false, which
  is how `DIALOGUE_DC_HIDE` reaches it; lockpick, disarm and dialogue checks all use it) and the pickpocket window
  (`Pages/Pickpocket.xaml`/`_c`: the selected item's `Roll.DifficultyCheck` under "Target", always shown, with the
  chance as a coloured pie). Nowhere else: not on dialogue options before the roll, not on locks or traps. So
  `pickpocket` prints "60% DC9" and `lockpick`/`disarm`/`roll_check` print the roll screen's DC, and nothing else does.
* **Per-tool probe table** (`packages/bench/fairness_probes.json`, loaded and checked by
  `bg3bench/fairness_probes.py`): every tool of the bg3 server has probes (call it with a start save's hidden fact;
  expect the fact absent from the result, or a refusal) or a written exemption. The table is checked offline; the
  live half that runs the probes on a start save is not written yet.
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
* Operator text the agent reads (the prompt files in `packages/harness/prompts/`, `knowledge/harness-news.md`,
  `tools.md`, tool docstrings) describes tool behaviour only: never a place, route, puzzle, item or encounter from a playthrough, and examples are made up (user,
  2026-10-18; bridge/tests/test_tool_docs.py guards the names that leaked; packages/harness/prompts/tests/test_prompts.py lints the
  prompt files for session anecdotes). Game facts learned in a run go to
  research/ or docs/.
* The Faith-Leap Trial's floor mosaic is only what the screen shows: the agent reads it with screenshot/zoom.
  get_layout draws no picture of it (the old one was drawn from the hidden platforms). Nav never answers from
  the hidden walkway layer: route, path_preview, WAYS, walk_toward and travel_to read it as no floor, the same
  as the chasm beside it. A move_to or jump the agent aims there itself is not refused or proven: a misstep has
  its in-game consequence (user, 2026-10-18; audit C2/C3).
* Navigation knows only ground the party has seen or walked (`Navigation.lua` `N.known`): the 4 m breadcrumb
  cell a member stood in, or a point a member sees now (in sight range with a clear eye ray), remembered for the
  session once seen. Ground below a member's own floor also counts when the eye ray stops on that floor short of
  its drop edge and a ray over the edge reaches it, as the camera above shows a ledge past an edge: the ray must
  hit floor (not a wall or parapet face) within 5 m of the member, the edge must lie within 3 m past that hit, the
  spot at most 4 m below the member's feet, and the look over the edge no steeper than 45 degrees down, so the
  dead ground at the foot of a face stays unseen; a floor that goes on (a storey over a room) is no edge
  (`VisibilityObjects.lua` `overEdge`; live N14b; user, 2026-10-28). Rays that can't answer count as not seen.
  A goal off that ground gets its straight distance
  and direction only; walks and move_to/jump_to targets end on it; WAYS, jump solvers, LOWER GROUND, get_layout
  cells (`?`), hazards on a route and teleport-landing floors are limited to it. route prints no floor heights
  (they came through solid floors). Stricter than a human in one way: a spot seen from afar and not asked about
  then is not remembered (audit C1/C4/C5/C14/C15/C19, 2026-10-18).
* `knowledge/walkthrough.md` lists timers, deadlines, points of no return, game-over traps and irreversible
  choices without their solutions; puzzle and encounter answers are not in play/knowledge (look them up with
  `wiki_page`, tagged `[wiki]`) (user, 2026-10-18).
* No oath guard: a paladin's blow that breaks the oath is the agent's choice, and the game's consequence follows,
  as for a player (user, 2026-10-18).
