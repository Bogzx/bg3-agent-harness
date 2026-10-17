# BG3 AI: architecture

Goal: an AI agent (any MCP client; `claude -p` by default) plays Baldur's Gate 3 from start to finish through a mod that (1) exposes
everything the player can see, (2) lets it take every action a player can, (3) gives it the real screen
when needed, and (4) keeps a Markdown memory of the playthrough across sessions of up to an hour (20-30 min in the continuous runs). Two hard rules
apply throughout:

* **Fair:** the agent only gets information a human player could see. Hidden enemies, undiscovered
  traps, story flags and secret DCs are never exposed. General game knowledge (the wiki) is allowed.
* **Efficient**, in two senses:
  * **Engine first.** Whatever the engine can tell or do goes through the engine, as compact text and
    direct actions. Screenshots are opt-in, for the rare cases text can't cover.
  * **Several actions per decision.** One observation carries enough to plan a whole sequence
    (positions, distances, ranges, costs, remaining resources, options). `do_actions` executes the
    sequence in one call (move → attack → bonus action → end turn; a looting sweep; a dialogue chain) and
    stops only on a real interrupt, so play moves at human or better pace.
  * **Cutscenes are always skipped.** `Client/AutoSkip.lua` skips movies and cinematics and fast-forwards
    spoken lines, emitting each line as text. It never auto-picks answers, rolls or decisions.

The research behind every decision is in `research/` (01–06; this document is the synthesis).

```
┌──────────────── MCP client session (play/, claude -p by default, ~1 h) ──┐
│ CLAUDE.md rules · playbooks (knowledge/playbooks, read on demand)        │
│ built-in tools off (--tools ToolSearch): only mcp__bg3__*, no hooks      │
└───────────────┬──────────────────────────────────────────────────────────┘
                │ MCP (stdio; streamable HTTP for a client in a container)
┌───────────────▼──────────── bridge/ : bg3 MCP server (Python) ───────────┐
│ ~93 tools · short handles (e12) instead of GUIDs · narrated text · event  │
│ deltas · blocking actions with interrupts · session clock · wiki · shots  │
│ memory/ (git, read-write) + knowledge/ (read-only): memory_* tools, the   │
│ sandbox, lint, session_start/end_session (memtools.py)                    │
└──────┬──────────────────────────────┬────────────────────┬──────────────┘
       │ LuaDebug TCP :9998           │ \0bg3ai-shot        │ \0bg3ai-input
┌──────▼──────── BG3 (native Linux, Vulkan, inside Steam's sniper runtime) ─┐
│ libbg3le.so (LD_PRELOAD): Script Extender API on the native build         │
│   mod/Mods/BG3AI (Lua):                                                    │
│     server ctx: Visibility filter → State projections · Events · Guard →  │
│                 Actions (Osiris / engine requests)                         │
│     client ctx: Noesis UI reader (dialogue, rolls, popups) · UI commands  │
│ VK_LAYER_MESA_screenshot: exact frame capture on request                   │
│ libbg3sdl.so (LD_PRELOAD): SDL input injection (mouse/keys), game-only     │
└───────────────────────────────────────────────────────────────────────────┘
          ▲ runner/play.py: launches the game, loads saves, runs sessions,
          │ recovers crashes, commits memory. runner/dashboard: live view.
```

## Key decisions

| Question | Decision | Why (details in research/) |
|---|---|---|
| How does our code get into the game? | **bg3le** (github.com/lenonk/bg3le, pinned v0.3.6 as a submodule, built in Docker for the sniper runtime) injected with `LD_PRELOAD` into the **native Linux** build | The official Script Extender (bg3se) is Windows-only. bg3le ports its whole Lua API (`Ext.*`, `Osi.*`) to the native build and supports exactly our game version (v4.76.31.656). Writing our own injector would mean months of reverse engineering, since Larian's functions have no symbols. Fallback (planned, not implemented: no Proton launcher exists; HANDOFF §6): Proton + Windows build + official bg3se, which would run the *same* mod and the *same* IPC. (01, 01b) |
| How does the agent talk to the mod? | bg3le's built-in **LuaDebug TCP eval server** (127.0.0.1:9998). The bridge evaluates `return Mods.BG3AI.BG3AI.rpc('<json>')` in the server or client context | No file polling. ~1 frame latency. Wire-compatible with bg3se's debugger, so the Proton fallback works unchanged. Long actions become jobs that are polled. (01, PROTOCOL.md) |
| What state does the agent see? | **Server-side visibility filter** using the engine's own perception queries (`CanSee`, `HasLineOfSight`, `IsInvisible`, `IsTrapDiscovered`, `GetCanInteract`, ...), then a **UI-mirroring allowlist projection** that exports only fields the hover tooltip / Examine panel / turn-order bar / journal show, honouring the game's Custom-Mode HUD toggles | Client-side replicated state is *not* filtered (sneaking enemies exist there with full HP). Rendering state mixes culling and fades. Osiris perception queries are what the game's own story uses. An allowlist makes leaks a code-review problem. (02) |
| How does the agent act? | Layered: **UI ICommands** (dialogue answers, rolls, reactions, menus, trade, level-up) → **engine cast requests** (spells, attacks) → **guarded Osiris** (movement, interaction, party) → **SDL input injection** (fallback). Every action passes a guard (visibility of target, combat turn, cheat-call blocklist) and is **confirmed by events** | UI commands are fair by construction: they send the same request a button does. Osiris calls bypass costs for spells, so they're guarded. Pixel clicking is the last resort. (03) |
| Screen access? | **Mesa's `VK_LAYER_MESA_screenshot`**, enabled via env for the game only and triggered over an abstract socket | Already installed. It captures the exact presented frame, including the UI, independent of compositor, focus or occlusion, ~50 ms per capture. Verified inside the Steam container and on the live game. (05) |
| Input? | Engine first (`Ext.Input.InjectKeyPress`, Noesis `ICommand:Execute`). For raw mouse: our **SDL2 `LD_PRELOAD` shim** (`native/bg3sdl`) | KDE Wayland makes desktop-level injection fragile and it would hijack the user's desktop. The shim only touches the game and works unfocused. (05) |
| Memory? | `memory/` git repo per playthrough. **Curated** files (≤ budgets, rewritten), **append-only** session logs, **generated** snapshots of live state (never hand-maintained), provenance tags `[seen]/[told]/[wiki]/[guess]`, a forced HANDOFF at wrap-up, and a lint plus deletion guard | Lessons from Claude/Gemini Plays Pokémon and the Continual Harness paper: notes get hallucinated, over-deleted, or never re-read. Fresh 1-h contexts with good handoffs beat one long context. (04) |
| Knowledge? | The model's own BG3 knowledge (tagged `[wiki]`), a rate-limited **bg3.wiki tool**, and a spoiler-light **walkthrough checklist** of timers, points of no return and game-over traps | Most run-ending failures in BG3 are not combat: timers, irreversible choices, game-over dialogue options. (06) |

## The four steps

### 1. Expose the game state (fair, efficient)
`mod/…/Server/Visibility.lua`, `State.lua`, `Events.lua` and `Client/UIState.lua` produce: status (where,
party, mode), nearby visible characters and objects, the character sheet, usable actions with costs,
inventory, the journal, map and waypoints, turn order, and dialogue lines with options and skill tags,
plus the roll, reaction and popup screens. Events carry the visible combat log, quest updates, items,
rolls and level-ups. The bridge renders all of this as short text lines with handles (`e12`) and returns
only deltas after actions.

### 2. Playthrough memory (Markdown, multi-session)
`memory-template/`, `tools/memory/` (`new_playthrough.py`, `session.py`, `lint.py`, `snapshots.py`), and
the playbooks `session-start`, `session-wrapup` and `compaction` in `play/knowledge/playbooks/` (read-only
for the agent; it runs with `--disable-slash-commands`, so it has no skills). Since 2026-10-13 the whole harness
is the MCP server's (`bridge/src/bg3ai/memtools.py`), not the client's, so any MCP client gets it and the server
enforces the sandbox:
* The agent reaches its notes only through `memory_list/read/search/write/edit/append` (and `note`), confined to
  `memory/` and read-only `knowledge/` (no `..`, symlinks, `.git*`, snapshots, markers.json; session logs
  append-only; CHARACTER.md append-only once committed). Every write's result carries the lint errors about it.
* The runner opens the session (`session.py start`) and leads its kickoff with the briefing (CHARACTER.md,
  PLAYTHROUGH.md, HANDOFF.md). Without the runner the server's first tool call opens it; `session_start` shows it.
* A ⏱ clock in every tool result forces wrap-up near the deadline (manual sessions: an elapsed-time reminder).
* `end_session` lints and commits, or says what is missing (keep playing, the handoff, lint). When the client
  ends anyway, the runner makes the same check and resumes the conversation with the reason.
* The pace guard (`wait=true` ignored on walks outside combat) is in the move tools.

### 3. Hands: verified actions and navigation
`Server/Guard.lua`, `Server/Actions.lua`, `Client/UIActions.lua` and `native/bg3sdl`. Actions are jobs
with interrupts: `move_to` stops when combat or dialogue starts. Engine pathfinding means "go to e12"
replaces walking pixel by pixel, the biggest cost in the Pokémon agents. Map markers persist in memory.

### 4. Autonomy: runner, recovery and observability
`runner/launch_game.sh` launches with all injections and no Steam config changes; install, profile and runtime
paths come from env or are found at run time (`tools/steam_paths.py`; table in README "Machine configuration"). `runner/play.py` runs
sessions in a loop: it starts the game, loads the playthrough's save, opens the memory session, starts the
client (`--client claude|codex|gemini`, `runner/clients.py`; `claude -p` with built-in tools off by default) in `play/` with a deadline, then saves, snapshots and commits. It also recovers from crashes, hangs and game overs, and
detects stalls across sessions. `runner/dashboard/` is a live web view of the game screen, party, the
agent's tool calls and its notes.

## Verified live (2026-10-09, native build v4.76.31.656)
* bg3le injected via `runner/launch_game.sh`. The debug server listened within 2 s of launch.
* Lua eval in both contexts over TCP. Our mod loaded from the loose folder, and `BG3AI.rpc` answered
  in both contexts. `Ext.Debug.Reset()` hot-reloads it.
* Noesis UI read (tree walk, DataContexts). `gui::DCMainMenu.ContinueGameCommand:Execute()` loaded a
  save with no clicks. `Ext.Input.InjectKeyPress("SPACE")` passed the splash screen.
* Osiris (`GetHostCharacter`, `DB_PartyMembers`, `GetDisplayName` + `Ext.Loca`) on a loaded save.
* Mesa layer capture of the real game (5120×1440 in 0.4 s including JPEG encode).
* MCP server: 43 tools registered then (93 on 2026-10-16, 78 always loaded; list: `play/knowledge/tools.md`). Saves and wiki work end to end.

## Repo map
| Path | What |
|---|---|
| `research/` | 01 runtime · 01b bg3le practical notes · 02 visibility · 03 actions · 04 prior art & memory · 05 screen/input · 06 knowledge & completion |
| `docs/PROTOCOL.md` | the mod↔bridge contract (ops, fairness, efficiency, live-dev rules) |
| `third_party/bg3le` | pinned submodule; `tools/build_bg3le.sh` builds it into `build/bg3le/dist/` |
| `mod/Mods/BG3AI` | the Lua mod (loose folder, loaded via `BG3LE_MOD_PATH`; `tools/pak.py create` packs it) |
| `native/bg3sdl` | SDL input shim |
| `bridge/` | Python: `luadebug.py` (TCP client), `game.py` (RPC), `render.py`, `server.py` (MCP), `memtools.py` (memory sandbox + session lifecycle), `screen.py`, `input.py`, `wiki.py` |
| `play/` | where the playing agent runs: CLAUDE.md, `.mcp.json`, `.claude/settings.json` (defence in depth, no hooks), `knowledge/` (incl. `playbooks/`), `memory -> ../playthroughs/<name>/memory` |
| `tools/container/` | recipe: an MCP client in a container whose network reaches only the bg3 server (HTTP) |
| `memory-template/`, `tools/memory/` | memory layout and tooling |
| `runner/` | launcher, session runner, dashboard |

## Learning from fights: debrief → lessons (act ↔ setup interface, proposed by setup 2026-10-10 19:0x)

Fair: everything below comes from what the player saw (combat log events, our own action results, the combat
read). Nothing from game files or hidden state.

**1. Fight debrief (act builds it, bridge).** At combat end, at a wipe, or at a reload during combat:
- **Result text** (combat-end result, ≤ 4 lines): `DEBRIEF <grade>: <summary>` plus key facts.
- **Session-log line** via `_append_note(text, "fight")`. ONE line, fixed field order, `|`-separated, so the keeper
  and grep can read it:
  `- HH:MM [fight] <grade> @<region> vs <Enemy xN (race)>, … | rounds N | downed: <Member> by <Enemy> …
  | dealt A / taken B | resisted: <Enemy>:<DamageType> … | unused: <item> … | <short summary>`
- **Machine record**: one JSON line per fight in `runtime/sessions/<playthrough>/<Sxxx>.fights.jsonl`. The path is
  `BG3AI_METRICS_PATH` with `.metrics.jsonl` → `.fights.jsonl`. The logs worker's retry metric reads it.
  ```json
  {"v": 1, "t": 1760111111.0, "session": "S033", "region": "Shadow-Cursed Lands",
   "group": "justiciar avenger|justiciar soulhunter|umbral tremor",
   "grade": "clean|costly|near_wipe|wipe|reloaded", "rounds": 8,
   "enemies": [{"name": "Justiciar Soulhunter", "race": "Human", "n": 8, "killed": 3,
                "used": ["Shadow Arrow"], "resisted": ["Necrotic"]}],
   "party": [{"name": "Wyll", "dealt": 40, "taken": 70, "downed": 1, "downed_by": "Justiciar Soulhunter"}],
   "ours": [{"ability": "Fireball", "casts": 2, "hits": 5, "misses": 1, "saves": 2}],
   "position": ["surrounded"], "unused": ["Scroll of Revivify"], "summary": "≤ 200 chars"}
  ```
  `group` is the retry key: the sorted, lower-cased, de-duplicated enemy names (at most 4), joined with `|`.
  A reload and the same fight again give the same group.
- Grades: `clean` (nobody downed, taken < 30% of party max HP), `costly` (someone downed, or ≥ 30%),
  `near_wipe` (≥ half the party downed, or one member left standing), `wipe` (all downed), `reloaded` (a reload
  happened during the fight). act owns the exact thresholds.

**2. Lessons store (setup): `memory/lessons.md`.** The keeper maintains it. It is distilled from `[fight]` and
`[lesson]` log lines, deduplicated, and holds at most 60 lessons. One lesson per line:
`- WHEN <trigger>, <trigger>, …: <what failed> → <what to do instead> (S033) [seen]`
- Triggers are short terms the harness can match: enemy names (`Justiciar Soulhunter`), name stems (`Justiciar`),
  races/creature kinds as the combat read shows them (`Undead`, `Githyanki`), regions/places
  (`Gauntlet of Shar`, `Shadow-Cursed Lands`), or situations: `ambush`, `escort`, `ranged`, `surrounded`, `boss`,
  `wipe`.
- The player writes raw lessons with `note(tag='lesson')` in the same `WHEN …: … → …` shape. The keeper merges
  them in.

**3. Retrieval (setup, bridge `lessons.py`, no model calls).** When a fight starts (combat_started /
COMBAT STARTED), a result carries `LESSONS:` with at most 3 matching lines. A trigger matches, as a
case-insensitive whole word or phrase, any enemy name or race in the combat read, or the current region.
A lesson is shown once per fight, and once per region entry (on a region change with no fight). The file is
re-read when its mtime changes.

**4. LAST ATTEMPT (act).** After a reload within ~10 min of a bad fight, load_game's result and the first
get_status carry `LAST ATTEMPT: <summary> — plan a different approach (…)`. The first turn of the retried fight
repeats 2-3 key facts. setup's LESSONS line is separate and comes from the store.

**act implementation (2026-10-10 19:50):** debrief.py + server.py hooks, as specified above, with act's extra fields
and the exact grade thresholds in research/fight-debrief.md (`t` is an ISO string; `ours[].saves` is null: events
don't show saves). LAST ATTEMPT / RETRY / LAST TIME / WARNING lines as in 4.

**v2 records (vis V6, 2026-10-17, from the Act 3 boss retries S059-S065).** One hit = one damage event per type,
read together: only a hit's main part counts for a damage type (a 1-3 Radiant rider beside 12 Slashing is no
resistance), and an enemy whose hits did 1 per part often enough (≥ 4 hits, ≥ 1/3) gets `dmg_cap` (Orin's
Unstoppable) instead of resisted types. New fields: `unseen` {dmg, types} (damage from no visible source: traps,
surfaces, hidden attackers; a key fact when ≥ 25 % of all taken and more than any enemy did), `deaths` and
`downed[].ability` (the enemy's cast that round), `kills` (kill order), `left` (surviving hurt enemies: lowest and
last hp_pct). Summons' downs are left out. v1 records lose their `low` flags and "did little" facts when loaded.
LAST TIME / RETRY show 4 key facts and cite the latest attempt that went badly (not a reload of a fight going
clean); YOUR LESSON counts from the chain's first attempt (`chain_t`, else the same session's reloaded attempts
just before). Fights with no enemy, damage or down are not recorded.
