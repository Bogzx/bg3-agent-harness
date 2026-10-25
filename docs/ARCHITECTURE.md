# BG3 AI: architecture

Goal: an AI agent plays Baldur's Gate 3 through a mod that (1) exposes everything the player can see, (2) lets it
take every action a player can, (3) gives it the real screen when needed, and (4) for long playthroughs, keeps a
Markdown memory across sessions. The same game environment also serves a benchmark that measures models on short
scenarios. Two hard rules apply throughout:

* **Fair:** the agent only gets information a human player could see. Hidden enemies, undiscovered
  traps, story flags and secret DCs are never exposed. General game knowledge (the wiki) is allowed.
  Details: [FAIRNESS.md](FAIRNESS.md).
* **Efficient**, in two senses:
  * **Engine first.** Whatever the engine can tell or do goes through the engine, as compact text and
    direct actions. Screenshots are a tool the agent calls, for the cases text can't cover.
  * **Several actions per decision.** One observation carries enough to plan a whole sequence
    (positions, distances, ranges, costs, remaining resources, options). `do_actions` executes the
    sequence in one call (move → attack → bonus action → end turn; a looting sweep; a dialogue chain) and
    stops only on a real interrupt.
  * **Cutscenes are skipped by default.** `Client/AutoSkip.lua` skips movies and cinematics and fast-forwards
    spoken lines, emitting each line as text. It never auto-picks answers, rolls or decisions. It is a run setting
    (user 2026-10-22): `play.py run --no-autoskip`, a scenario's `game_settings.autoskip = false` or
    playthrough.toml `[game] autoskip = false` turn it off (`BG3AI_AUTOSKIP=0`: the runner's `ui.config` and no
    ESCAPE skipper).

The research behind the game-side decisions is in `research/` (01–06); the overhaul's is in `research/arch/`
(local). The target layout and the engineering standards are in [TARGET-ARCHITECTURE.md](TARGET-ARCHITECTURE.md),
the ordered plan and its decisions D1-D15 in [OVERHAUL-PLAN.md](OVERHAUL-PLAN.md).

## Three packages

Since the overhaul (branch `overhaul`, 2026-10-21) the code is split into three packages with one-way
dependencies, in one uv workspace (root `pyproject.toml`, one `uv.lock`; members `bridge` and `packages/*`):

| Package | Import name | What it holds |
|---|---|---|
| `packages/harness` (+ `bridge/`, `mod/`, `native/`, `patches/`, `third_party/`, `tools/container/`) | `bg3harness` (bridge: `bg3ai`) | the game environment and the agent clients: launch, save load, crash recovery, `run_session`, the clients and their profiles, the event log, the facts stream, the prompt texts |
| `packages/bench` | `bg3bench` | scenarios, graders over `facts.jsonl`, statistics, manifests and pins, baselines, the fairness probe table, reports and the leaderboard site |
| `packages/playthrough` | `bg3play` | the playthrough app: supervisor, memory and keeper (`bg3play.memory`), the API server and the Svelte frontend in `packages/playthrough/web` |

The benchmark and the app import only the harness, never each other, and the harness imports neither. This is
checked by import-linter contracts (`tools/ci/import_contracts.py`) and a boundary test (`tools/ci/test_boundaries.py`).
`runner/play.py` (4,933 lines, on the line-cap allowlist and shrinking) still drives play sessions; the modules
already moved out of `runner/` are one-line shims (`runner/_harness.py`, `tools/ci/runner_shims.py` checks them).

```
┌──────────── agent client (one play process; Claude Code by default) ─────────┐
│ system text from packages/harness/prompts (rules, optional coaching)         │
│ no built-in tools: only the bg3 server's                                     │
└───────────────┬──────────────────────────────────────────────────────────────┘
                │ MCP (stdio; streamable HTTP for a client in a container)
┌───────────────▼──────────── bridge/ : bg3 MCP server (bg3ai) ────────────────┐
│ server.py (165 lines) + tools/*.py: 95 tools in 23 group modules             │
│ BG3AI_TOOLS / BG3AI_TOOLS_OFF remove tool groups at startup (capabilities.py)│
│ short handles (e12) instead of GUIDs · narrated text · event deltas ·        │
│ blocking actions with interrupts · session clock · wiki · screenshots ·      │
│ memory tools over memory/ (read-write) and knowledge/ (read-only)            │
└──────┬──────────────────────────────┬────────────────────┬──────────────────┘
       │ LuaDebug TCP :9998           │ \0bg3ai-shot        │ \0bg3ai-input
┌──────▼──────── BG3 (native Linux, Vulkan, inside Steam's sniper runtime) ────┐
│ libbg3le.so (LD_PRELOAD): Script Extender API on the native build            │
│   mod/Mods/BG3AI (Lua):                                                       │
│     server ctx: Visibility → State · Events · Navigation · Guard → Actions   │
│                 harness only: Referee ring · RollLog · Scenario setup        │
│     client ctx: Noesis UI reader (dialogue, rolls, popups) · UI commands     │
│ VK_LAYER_MESA_screenshot: exact frame capture on request                      │
│ libbg3sdl.so (LD_PRELOAD): SDL input injection (mouse/keys), game-only        │
└──────────────────────────────────────────────────────────────────────────────┘
          ▲ runner/play.py + bg3harness: launch the game, load saves, run sessions
          │ (run_session), recover crashes, write events.jsonl / facts.jsonl / rolls.jsonl
```

## Key decisions

| Question | Decision | Why (details in research/) |
|---|---|---|
| How does our code get into the game? | **bg3le** (github.com/lenonk/bg3le, pinned v0.3.6 as a submodule, built in Docker for the sniper runtime) injected with `LD_PRELOAD` into the **native Linux** build | The official Script Extender (bg3se) is Windows-only. bg3le ports its whole Lua API (`Ext.*`, `Osi.*`) to the native build and supports exactly our game version (v4.76.31.656). Writing our own injector would mean months of reverse engineering, since Larian's functions have no symbols. Fallback (planned, not implemented: no Proton launcher exists): Proton + Windows build + official bg3se, which would run the *same* mod and the *same* IPC. (01, 01b) |
| How does the agent talk to the mod? | bg3le's built-in **LuaDebug TCP eval server** (127.0.0.1:9998). The bridge evaluates `return Mods.BG3AI.BG3AI.rpc('<json>')` in the server or client context | No file polling. ~1 frame latency. Wire-compatible with bg3se's debugger, so the Proton fallback works unchanged. Long actions become jobs that are polled. (01, PROTOCOL.md) |
| What state does the agent see? | **Server-side visibility filter** using the engine's own perception queries (`CanSee`, `HasLineOfSight`, `IsInvisible`, `IsTrapDiscovered`, `GetCanInteract`, ...), then a **UI-mirroring allowlist projection** that exports only fields the hover tooltip / Examine panel / turn-order bar / journal show, honouring the game's Custom-Mode HUD toggles | Client-side replicated state is *not* filtered (sneaking enemies exist there with full HP). Rendering state mixes culling and fades. Osiris perception queries are what the game's own story uses. An allowlist makes leaks a code-review problem. (02) |
| How does the agent act? | Layered: **UI ICommands** (dialogue answers, rolls, reactions, menus, trade, level-up) → **engine cast requests** (spells, attacks) → **guarded Osiris** (movement, interaction, party) → **SDL input injection** (fallback). Every action passes a guard (visibility of target, combat turn, cheat-call blocklist) and is **confirmed by events** | UI commands are fair by construction: they send the same request a button does. Osiris calls bypass costs for spells, so they're guarded. Pixel clicking is the last resort. (03) |
| How does the benchmark know what happened? | Hidden rings in the mod (`Server/Referee.lua`, `Server/RollLog.lua`) that only the harness reads, plus the progress poller's state reads, written to `facts.jsonl` and `rolls.jsonl`. The agent never sees them | Graders need facts the agent may not see (a theft nobody witnessed, every die rolled). Keeping them in separate ops that no tool calls keeps the agent's side fair |
| Screen access? | **Mesa's `VK_LAYER_MESA_screenshot`**, enabled via env for the game only and triggered over an abstract socket | It captures the exact presented frame, including the UI, independent of compositor, focus or occlusion, ~50 ms per capture. Verified inside the Steam container and on the live game. (05) |
| Input? | Engine first (`Ext.Input.InjectKeyPress`, Noesis `ICommand:Execute`). For raw mouse: our **SDL2 `LD_PRELOAD` shim** (`native/bg3sdl`) | KDE Wayland makes desktop-level injection fragile and it would hijack the user's desktop. The shim only touches the game and works unfocused. (05) |
| Memory? | `memory/` git repo per playthrough (playthrough app only). **Curated** files (≤ budgets, rewritten), **append-only** session logs, **generated** snapshots of live state, provenance tags `[seen]/[told]/[wiki]/[guess]`, a forced HANDOFF at wrap-up, and a lint plus deletion guard | Lessons from Claude/Gemini Plays Pokémon and the Continual Harness paper: notes get hallucinated, over-deleted, or never re-read. Fresh contexts with good handoffs beat one long context. (04) |
| Knowledge? | The model's own BG3 knowledge (tagged `[wiki]`), a rate-limited **bg3.wiki tool**, and a spoiler-light **walkthrough checklist** of timers, points of no return and game-over traps | Most run-ending failures in BG3 are not combat: timers, irreversible choices, game-over dialogue options. (06) |

## The game environment (harness)

### The mod (`mod/Mods/BG3AI`)
Loaded as a loose folder through `BG3LE_MOD_PATH`. `BootstrapServer.lua` and `BootstrapClient.lua` load the
modules. Every file is under the 1000-line cap; the big modules were split, and the first file of each group loads
the rest at its end with `Ext.Require`:

| Module | Files |
|---|---|
| Visibility | `Server/Visibility.lua`, `VisibilityObjects.lua` |
| State | `Server/State.lua`, `StateWorld.lua`, `StateOps.lua` |
| Navigation | `Server/Navigation.lua`, `NavigationReach.lua`, `NavigationTerrain.lua`, `NavigationRoute.lua`, `NavigationOps.lua`, `NavigationDoors.lua` (data: `Server/Data/JournalMarkers.lua`, generated, exempt from the cap) |
| Guard | `Server/Guard.lua`, `GuardGate.lua`, `GuardSpells.lua`, `GuardCast.lua` |
| Actions | `Server/Actions.lua`, `ActionsMove.lua`, `ActionsInteract.lua`, `ActionsCast.lua`, `ActionsParty.lua`, `ActionsClick.lua` |
| Events | `Server/Events.lua` |
| Harness only | `Server/Referee.lua`, `Server/RollLog.lua`, `Server/Scenario.lua` |
| UI actions | `Client/UIActions.lua`, `UIActionsPrompts.lua`, `UIActionsMenu.lua` |
| Services | `Client/Services.lua`, `ServicesRest.lua`, `ServicesWaypoints.lua`, `ServicesItems.lua` |
| Progression | `Client/Progression.lua`, `ProgressionCC.lua`, `ProgressionLevelUp.lua` |
| Other client | `Client/UI.lua`, `UIState.lua`, `UISettings.lua`, `AutoSkip.lua`, `Hotbar.lua`, `Pick.lua`, `Spellbook.lua`, `PickpocketOps.lua`, `FleeOps.lua` |
| Shared | `Shared/RPC.lua`, `Util.lua`, `Mem.lua`, `HotbarMap.lua` |

The op catalogue is in [PROTOCOL.md](PROTOCOL.md). The Lua is linted by luacheck against a baseline
(`tools/ci/lua_lint.py`, `.luacheckrc`) and tested in the Docker suites (`tools/dev/test_*.sh`, run by
`tools/ci/lua_suites.sh`).

The three harness-only files are not agent-facing:
* **Referee ring** (`Referee.lua`, ops `ref.since` / `ref.snapshot`): what the party really did, hidden facts
  included (trades, pickpockets, thefts, crimes, kills, loot), in the facts v1 event shapes.
* **Roll log** (`RollLog.lua`, ops `roll.since` / `roll.snapshot`): every die roll the game makes, for the
  benchmark's luck number (D9). `natural` and `dc` are filled where the game exposes them, which on this build is
  passive and skill/ability checks (the RequestedRoll component); saving throws and attack rolls are logged with
  `natural`/`dc` null (success, roller and stream counts only), so the luck number is the checks' luck. Closed
  2026-10-24 after two live probe passes found no source for them (`tools/dev/probe_roll_sources.py`; evidence in
  research/fix/vis.md "R2" and research/fix/live.md Window D2: `runtime/probes/fix-live/rolls-dump.txt`,
  `rolls2-dump.txt`, `runtime/probes/fix-vis/rolls.json`, `rolls2.json`; the passive checks' PASS in
  `fix-live/rolllog-a.json`).
* **Scenario setup** (`Scenario.lua`, ops `scn.seed`, `scn.karmic`, `scn.difficulty`, `scn.rename`,
  `scn.rename_avatar`, `scn.undo`, `scn.status`): seeded dice, Karmic Dice, the difficulty read, the avatar's name
  (a bench save start plays under the scenario's character; the mod writes it again after every load) and the
  renames used by benchmark runs and start-save authoring (`tools/saves/make_start_save.py`). Every `scn.*` op is
  refused unless the game process runs with `BG3AI_SCENARIO_SETUP=1`. The runner sets it in the game's env only
  for a run with a seed or a scenario that fixes a setting or the avatar's name (`ChecksSide.setup_env`); a normal
  playthrough never has it.

The bridge reads the rings through `bridge/src/bg3ai/referee.py`; the harness's facts writer
(`bg3harness.facts.RingFeed`) drains them into `facts.jsonl` and `rolls.jsonl` in the run's directory.

### The bridge (`bridge/`, package `bg3ai`)
`server.py` is now 165 lines: it builds the server and keeps the memory keeper's separate server
(`BG3AI_KEEPER_MODE=1`). The tools live in `bridge/src/bg3ai/tools/`, one module per group (`core`, `reporting`,
`plumbing`, `overview`, `observe`, `world_tools`, `travel`, `dialogue`, `combat`, `memory`, `inventory`,
`progression`, `uitools`, `newgame`, `batch`, `queueing`, `turn_plan`, `turn_slots`, `turn_tool`, `macro_tools`,
`travel_macro`, `saving`, `session`), registered in the `MODULES` order of `tools/__init__.py`. The full registry
is 95 tools; `play/knowledge/tools.md` lists them.

**Capabilities.** `bridge/src/bg3ai/capabilities.py` groups the optional tools: `memory` (the memory tools, `note`,
`session_start`, `end_session`, `recall`), `wiki`, `reload` (`load_game`), `saves`, `screenshots` and `raw_input`
(`press_key`, `click`, `ui_dump`). Every other tool is in the `game` group, which is always on.
`BG3AI_TOOLS` (a comma list of groups and tool names to keep; unset means every tool) and `BG3AI_TOOLS_OFF` (removed
on top) are read once at startup, and the server removes the other tools before any client lists them. An unknown
name fails the server's startup. The rules text a session gets leaves out the rules of a group that is off
(`bridge/src/bg3ai/rules.py`, `packages/harness/prompts/rules/capabilities/`). A profile's `tools_off` and the
benchmark's conditions reach the server through `BG3AI_TOOLS_OFF`.

**RPC recorder.** With `BG3AI_RPC_RECORD=<file>` the bridge appends every Lua eval it sends and the game's answer
as JSONL (`bridge/src/bg3ai/rpc_record.py`), decoded into mod ops, with absolute paths scrubbed so the files can be
committed. Recordings from the live probes are in `packages/harness/tests/fixtures/rpc/`.

**Fake mod.** `bg3harness.testing` holds the offline doubles: `fakemod.FakeMod` (a TCP server that speaks the
LuaDebug protocol and answers the bridge's RPCs), `scripted.GameModel` (a scripted game state, rings and `scn.*`
included), `fixtures.FixtureStore` (replays recorder files), `mcp_bridge` (starts `bg3ai-mcp` against a fake mod
and calls its tools as an MCP client) and `judge.FakeJudge`. `packages/harness/tests/test_bridge_e2e.py` runs the
real bridge against them.

### Sessions, clients and records (`packages/harness`, `runner/`)
* **`run_session(spec, hooks)`** (`bg3harness/session.py`) runs one play session's agent processes: the kickoff,
  resumes after an early stop the stop check refuses, game recovery after a crash or hang, and the end. With
  `Recovery(max_resumes=0)` (a benchmark run) a fault ends the session as invalid. Everything outside comes in
  through `SessionHooks`; `runner/play.py` passes `RunnerSessionHooks`, the tests a fake client and a fake game.
* **`GameController`** (`bg3harness/game/controller.py`): launch, kill, the waits up to the menu or a loaded save,
  launch retries and the OOM check. `runner/launch_game.sh` launches with all injections; paths come from env or
  are found at run time (`tools/steam_paths.py`).
* **Clients** (`bg3harness/clients.py`, `pi_client.py`): one `Client` interface (command line, config files, env,
  a stream parser to normalized events), picked with `play.py run --client` or a profile:
  * **pi** (`tools/pi`, pi 1.3.0 pinned by its lockfile, extension `tools/pi/bg3.ts`): any model behind an
    OpenAI-compatible endpoint. The run's profile names the endpoint, model and effort; the system text is
    written to a file and sent verbatim (`BG3AI_SERVE_RULES=0`, so the server doesn't add its own copy).
  * **Claude Code** (the default): `claude -p` with no built-in tools (`--tools ToolSearch
    --strict-mcp-config`) in `play/`, whose `CLAUDE.md` is generated from the prompt files.
  * **Codex CLI**: untested live. Its command passes an offline check against codex-cli 0.157.1, a stub model and
    a stub MCP server (`tools/codex/conformance/`).
  * **Gemini CLI**: unsupported (D14). The client class exists but has never run: the CLI is not installed here.
* **Profiles** (`profiles/*.toml`, `bg3harness/profiles.py`): one TOML per model condition, for every client:
  client, model, effort; for pi also the endpoint, the env var that holds the key (never the key), OpenRouter
  routing and the model definition.
* **Prompts** (`packages/harness/prompts/`): `rules/` (how the game, the tools, fairness and the clock work),
  `rules/capabilities/` (one file per tool group), `coaching/` (how to play well, the playthrough protocol) and
  `clients/` (what one client needs on top). `assemble.py` builds the text (`rules` for the benchmark default,
  `rules+coaching` for `play/CLAUDE.md`) with a stable hash for the run manifest.
* **Records**: `Sxxx.events.jsonl` (normalized events, `schemas/events.v1.schema.json`), `facts.jsonl`
  (`schemas/facts.v1.schema.json`) and `rolls.jsonl` per run, and the run manifest (`run_manifest.py`).
* **Sandbox**: the client sees only the bg3 server's tools; `--sandbox container` (`tools/container/`) runs it in a
  container whose network reaches only the bg3 server and its model API.

## The playthrough app (`packages/playthrough`)
`memory-template/` and `bg3play.memory` (`session.py`, `lint.py`, `snapshots.py`, `keeper.py`, ...; the old
`tools/memory/*.py` paths are shims) keep the playthrough's notes. The bridge's memory tools
(`bridge/src/bg3ai/memtools.py`) enforce the sandbox, so any MCP client gets it:
* The agent reaches its notes only through `memory_list/read/search/write/edit/append` (and `note`), confined to
  `memory/` and read-only `knowledge/` (no `..`, symlinks, `.git*`, snapshots, markers.json; session logs
  append-only; CHARACTER.md append-only once committed). Every write's result carries the lint errors about it.
* The runner opens the session and leads its kickoff with the briefing (CHARACTER.md, PLAYTHROUGH.md, HANDOFF.md).
* A ⏱ clock in every tool result forces wrap-up near the deadline.
* `end_session` lints and commits, or says what is missing. When the client ends anyway, the runner makes the same
  check and resumes the conversation with the reason.

A benchmark run has no keeper and no resume (D5); its condition removes the wiki tools unless it says `+wiki`, and
`load_game` unless it allows reloads (`bg3bench/conditions.py`). The app's API server and
frontend (`bg3play/server.py`, `packages/playthrough/web`) replace `runner/dashboard/` over time.

## Verified live (2026-10-09, native build v4.76.31.656)
* bg3le injected via `runner/launch_game.sh`. The debug server listened within 2 s of launch.
* Lua eval in both contexts over TCP. Our mod loaded from the loose folder, and `BG3AI.rpc` answered
  in both contexts. `Ext.Debug.Reset()` hot-reloads it.
* Noesis UI read (tree walk, DataContexts). `gui::DCMainMenu.ContinueGameCommand:Execute()` loaded a
  save with no clicks. `Ext.Input.InjectKeyPress("SPACE")` passed the splash screen.
* Osiris (`GetHostCharacter`, `DB_PartyMembers`, `GetDisplayName` + `Ext.Loca`) on a loaded save.
* Mesa layer capture of the real game (5120×1440 in 0.4 s including JPEG encode).

## Repo map
| Path | What |
|---|---|
| `research/` | 01 runtime · 01b bg3le practical notes · 02 visibility · 03 actions · 04 prior art & memory · 05 screen/input · 06 knowledge & completion |
| `docs/PROTOCOL.md` | the mod↔bridge contract (ops, fairness, efficiency, live-dev rules) |
| `docs/FAIRNESS.md` | what the agent may know and how each layer enforces it |
| `third_party/bg3le` | pinned submodule; `tools/build_bg3le.sh` builds it into `build/bg3le/dist/` |
| `mod/Mods/BG3AI` | the Lua mod (loose folder, loaded via `BG3LE_MOD_PATH`; `tools/pak.py create` packs it) |
| `native/bg3sdl` | SDL input shim |
| `bridge/` | the bg3 MCP server: `server.py`, `tools/` (tool groups), `capabilities.py`, `game.py` (RPC), `luadebug.py` (TCP client), `referee.py`, `rpc_record.py`, `render*.py`, `memtools.py`, `screen.py`, `input.py`, `wiki.py` |
| `packages/harness` | `bg3harness`: session, clients, profiles, game controller, events, facts, fairness audit, testing doubles; `prompts/`, `schemas/` |
| `packages/bench` | `bg3bench`: scenarios, graders, stats, series, baselines, `fairness_probes.json`, report and site |
| `packages/playthrough` | `bg3play`: supervisor, memory, keeper, API server; `web/` frontend |
| `profiles/` | client profiles (TOML) |
| `play/` | where the playing agent runs: generated CLAUDE.md, `.mcp.json`, `.claude/settings.json`, `knowledge/` (incl. `playbooks/`), `memory -> ../playthroughs/<name>/memory` |
| `tools/pi`, `tools/codex` | the pi install and extension; the Codex conformance check |
| `tools/container/` | an MCP client in a container whose network reaches only the bg3 server (HTTP) |
| `tools/ci/` | line cap, import contracts, boundaries, coverage gate, Lua lint and suites |
| `runner/` | `play.py` (sessions, series), `launch_game.sh`, shims to the packages, `dashboard/` |

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
and the exact grade thresholds in `bridge/src/bg3ai/debrief.py` (`t` is an ISO string; `ours[].saves` is null: events
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

**Since vis V16 (2026-10-18, S074 Cazador try 1).** The mod sends a hit's damage events before its cast event:
a member's damage is held until that member's next cast (a summon's cast takes its owner's held damage), and a
turn/round boundary gives what no cast claimed to the source's latest cast. Heals no longer collect another
member's damage (S074: Gale's Scorching Ray 49 under Shadowheart's Mass Cure Wounds). A down's `ability` is the
enemy's cast that follows the hit. A hostile that had turns but never acted, was never hit by anything and never
statused by us is `idle` ({name, died}): not an enemy, not a kill (S074's six Sacrificial Lamb spawn,
`killed 3/27`); the render lists them apart. A damage type is `low` only from ≥ 2 hits (`LOW_MIN_HITS`); stored
records' 1-hit lows are dropped on load like v1's.

**v3 records (vis V18b, 2026-10-18, S077's WARNINGs from S076's records).** A damage type is `low` ("did little")
only when ≥ 3 of its hits on that enemy (`LOW_MIN_HITS`) that were no killing blow (a `died` right after the hit's
damage run) did under half (`LOW_SHARE`) of what the same attack (caster + ability, known from the cast that
follows the hits or the turn's settle) of that type did to other enemies in the fight; hits whose attack hit no
other enemy are not compared. The row carries `compared`, `got`, `expected`; the facts and WARNINGs read `(3 hits,
avg 3.3 vs 13.5 from the same attacks on other enemies)`. v2 records' `low` flags (they compared a type with the
other attacks' damage and counted killing blows) are dropped on load like v1's.

**Since vis V18 (2026-10-18, S076 Gale asleep 3 rounds).** state.combat's party entries carry `disabled`
(Guard.disabling: statuses of StatusType INCAPACITATED/FEAR/KNOCKED_DOWN or a disabling StatusGroup, DOWNED
excluded), each `{name, turns, effect, fix}` read from the game's stats: `cannot act` / `cannot move` / Prone's
stand-up, and the fix (Target_Help's RemoveStatus list, `OnDamage`, a save each turn). The bridge prints one
`DISABLED eN Name: …` line per such member (turn.disabled_lines) on every combat read: the TURN block's NOW/NEXT
(`_turn_next`, or after an interrupted turn), get_combat_state, end_turn/wait_for my_turn, set_party select. The
Guard's `cannot act while X` refusal names the fix too.
