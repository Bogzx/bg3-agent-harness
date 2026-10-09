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
| 7 | **Sandbox** (`play/.claude/settings.json`, `play/.claude/hooks/fairness_guard.py`): the playing agent has no Bash, no web fetch, and no access to game files, saves, logs, the debug port, or this repo's source. It reads and writes only `memory/` and `knowledge/`. | Bypassing layers 1–6 through the eval port, game data files or story files |
| 8 | **Fair actions** (`Guard.lua`): an action's targets must be perceivable. Cheat calls (teleport, add gold, unlock, apply status, set flags, ...) are blocked. Spells use the engine's checked path, so costs apply. | Acting on hidden things, and doing anything a player can't |

Screenshots (`screenshot`/`zoom`) are fair by construction: they are the rendered frame.

## Auditing
* `dev.fairness_probe` (server op) returns, for the current area, how many candidates each rule filtered.
  It returns counts only, never names.
* Visibility probes and their live results are in `research/impl-vis.md`.
* The agent is told to log `[note] fairness leak: …` if a tool ever shows it something a player couldn't
  see. The runner surfaces these notes.

## Known judgment calls
* "Perceived by any party member" is slightly *stricter* than a human with a free camera, who can pan to
  non-hidden NPCs far away. The screenshot tool covers that gap fairly.
* Examine-panel data (HP, AC, ability scores, resistances) is exported for visible creatures because the
  game shows it to every player via Examine.
