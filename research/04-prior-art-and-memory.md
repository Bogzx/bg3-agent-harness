# 04 — Prior art on LLM agents in long games, plus harness and memory design

Scope: what long-running LLM game agents (2023–2026) teach us about state representation, action
abstraction, navigation, memory and context resets; what BG3-specific bridges exist and how they do IPC;
a concrete multi-session Markdown memory design; MCP server design for a game; and a recommended harness
for an agent that has to finish BG3 in ~1-hour sessions.

Researched 2026-10-09. Anything I could not confirm from a primary or near-primary source is marked
**UNVERIFIED**.

---

## 0. TL;DR

- **Every agent that finished a long game had a harness that turned pixels into text.** The exceptions are
  very recent frontier models (Fable 5 on FireRed, vision-only). Gemini 2.5 Pro beat Pokémon Blue on
  RAM-derived text, a text fog-of-war map and a pathfinding sub-agent. Prompting it to "check the screen"
  for PP failed; injecting PP from RAM worked. The author's lesson was that the scaffolding matters as much
  as the model.
- **Action abstraction is the biggest lever.** Pokémon agents spent most of their steps walking. BG3 has
  click-to-move with engine pathfinding, so a `move_to(target)` tool collapses that problem. NetHack work
  found that code-level skills roughly tripled progression and cut inference cost by 86% compared with
  primitive actions.
- **Long context degrades play.** Gemini looped after about 100k tokens even with a 1M window, and
  Continual Harness saw agents stop inventing new approaches after about 500 stalled turns. Fresh
  1-hour sessions with good notes are a feature, not a limitation.
- **Notes fail in three recurring ways:** (1) hallucinated or wrong beliefs become sticky goals
  (Gemini's "TEA", a 1,003-turn loop driven by a memory directive written from a broken tool);
  (2) useful facts get deleted during "overzealous memory cleanups" (Opus 4.6); (3) most authored memory
  is never read again (Continual Harness). The fixes: separate facts from hypotheses, record provenance,
  generate anything the game already tracks from live state instead of hand-maintaining it, keep git
  history of the notes, and reconcile against the game at every session start.
- **No public project lets an LLM play BG3.** Existing BG3 bridges are one-way data dumps
  (Inworld: `Ext.IO.SaveFile` of `data.json` every 5 s), Twitch reward mods (file drop plus Lua), Crowd
  Control (apparently the BG3SE Lua debugger TCP port 9998), and a modding MCP (`bg3-data-mcp`: console
  injection plus reading the SE log back). The macOS SE rewrite exposes a **Unix domain socket**
  (`/tmp/bg3se.sock`), which is the closest model for the native Linux build used here.
- **Recommended harness:** a stdio MCP server (Python) that talks to an in-game bridge over a Unix
  socket. It has roughly 35 mid-level tools that block until the action settles or a bounded timeout
  passes, return compact text deltas, and add screenshots only on request. Claude Code runs it with a
  `CLAUDE.md` plus a few skills (combat, dialogue, exploration, session-wrapup). Memory is a git-tracked
  `memory/` folder in which the agent writes only judgments and the harness generates all game-tracked
  facts. Hooks inject the handoff at start and force a save plus handoff at about 50 minutes.

---

## 1. Prior art: LLM agents in long games

### 1.1 Claude Plays Pokémon (Anthropic, David Hershey, June 2024 → finished Red May 2026)

**Harness.** As described by Hershey on Latent Space, summarised by ZenML, and in Bradshaw's LessWrong posts:
- **Input:** a screenshot after every action plus a small amount of RAM-derived text (current location and
  similar). The screenshot has a coordinate grid overlay.
- **Tools:** a button-press tool (A/B/Start/Select/D-pad sequences), a **navigator** ("as long as it
  requests to be moved to a valid set of coordinates from the screenshot overlay grid, the tool will move
  it there"; on-screen targets only, so routing around walls is still the model's job), and a
  **knowledge base** tool (a persistent store embedded in the prompt that the model can add to, edit and
  delete from, organised however it likes).
- **Context management:** "accordion" summarisation. After a block of roughly 30 actions, that block is
  summarised and older summaries are compressed further. The knowledge base carries facts across
  summaries because "summaries alone lose details." Coverage from 2025 notes that when the knowledge base
  itself grew to fill the context, it also had to be summarised, and that compression loses fine detail.
- **Critic:** a "Critic Claude" instance periodically reviewed the main agent. It was later removed.
- **Harness philosophy:** Hershey "has actually stripped out complexity over time rather than adding it."
  The most common edit between model versions was deleting "band-aid" prompt text for weaknesses the new
  model no longer had. Later changelog items: navigator support for Surf; spin and teleport tiles marked
  non-navigable; waiting for movement to stop before taking the next screenshot.
- **Changes for Opus 4.7 (2026):** a **zoom tool** to crop and magnify part of the screen; the ability to
  **save screenshots or crops as visual memory** (it had failed to write down which Cerulean building was
  the Pokémon Center); multiple A presses per step in menus. Critic, visual hints and some advice were
  removed, and the memory file limits were changed.

**How it went.** Sonnet 3.5 (June 2024) could not leave the starting house. Claude 3.7 (Feb 2025) got a
few badges, then became famously stuck in Mt. Moon and other mazes. Opus 4.5 (late 2025/Jan 2026) took
about 29k steps to Silph Co., then 50k+ steps stuck there because it ignored the Card Key item on the
floor, and about 112k steps in Cinnabar Mansion. Opus 4.6 did Cinnabar in about 3k steps but stalled for
months on the Victory Road boulder puzzles. **Opus 4.7 finished Red in May 2026**, with "an almost
unbearable amount of unfocused wandering and sheer forgetfulness."

**What failed and why (relevant to BG3):**
- **Vision.** The model walked into walls, missed items on the ground, missed switches, and took a door
  for a "conveyer belt." It hallucinated expected objects near its goals.
- **Notes.** Better note-taking let 4.5 finish the Safari Zone efficiently, and 4.6 used notes to track
  the Cinnabar switches. But 4.6 **deleted the note describing what the switch looked like in an
  "overzealous memory cleanup."** "He really needs his notes."
- **Long-term planning.** It ignored the Gold Teeth needed for Strength, a dependency several hours away.
- Persistence eventually wins, but the step cost is enormous.

**Fable 5 (June 2026)** beat FireRed with "a minimal, vision-only harness … raw game screenshots … no
maps, navigation aids, or extra game-state information" (Shacknews, reporting Anthropic's timelapse).
Shacknews also reports that Fable 5 was trained partly on Slay the Spire for long-context and memory
behaviour. **UNVERIFIED**: no step or duration figures, and no detail on whether it had a notes tool.

Sources: [Latent Space, "How Claude Plays Pokémon was made"](https://www.latent.space/p/how-claude-plays-pokemon-was-made),
[ZenML case study](https://www.zenml.io/llmops-database/building-and-deploying-a-pokemon-playing-llm-agent-at-anthropic),
[Bradshaw, "Is Gemini now better than Claude at Pokémon?"](https://www.lesswrong.com/posts/7mqp8uRnnPdbBzJZE/is-gemini-now-better-than-claude-at-pokemon),
[Opus 4.5 follow-up](https://www.lesswrong.com/posts/gogZyeistdaDFuhbG/claude-plays-pokemon-opus-4-5-follow-up),
[A Year Late, Claude Finally Beats Pokémon](https://www.lesswrong.com/posts/sehJYg5Yny9fvpbpt/a-year-late-claude-finally-beats-pokemon),
[Shacknews on Fable 5 FireRed](https://www.shacknews.com/article/149574/claude-ai-pokemon-firered-fable-5),
[Guild of Scientific Troubadours, "AI can't beat Pokémon"](https://guildofscientifictroubadours.com/?p=14521).

### 1.2 Gemini Plays Pokémon (Joel Zhang, independent; Blue May 2025, Yellow Legacy, Crystal)

**Harness (v1, Blue):**
- **Input:** an annotated screenshot plus a **text dump** of on-screen objects, tile properties and
  navigability. The harness's central idea was "translating the visual game world into a structured,
  textual format." Party moves and PP came from RAM. Per Google's technical report, Gemini played
  "roughly as well as without the vision information."
- **Map memory:** a fog-of-war minimap of every tile seen, stored as JSON and shown to the model as XML.
  Each turn the harness lists **"reachable unseen tiles,"** the exploration frontier.
- **Sub-agents:** a `pathfinder` (a fresh Gemini 2.5 Pro instance given the map and tile mechanics) and a
  `boulder_puzzle_strategist`. The pathfinder solved the Rocket Hideout spinner maze on the first try but
  sometimes hallucinated shortcuts through walls.
- **Goals:** primary, secondary and tertiary goals, reinserted after every context reset.
- **Context:** every 100 turns, recent events are summarised and a fresh context is started with the
  summary plus persistent information. The author saw looping past about 100k tokens despite the 1M
  window.
- **Critic:** a "critiquer" instance that wrote a walkthrough for the current goal. Sometimes it broke
  loops; sometimes it hallucinated, for example "go southeast" when the exit was west.
- **Dropped:** a "World Knowledge Graph," because the agent added redundant nodes and didn't use them.

**Harness v2 (Yellow Legacy, Crystal):** the specialised agents and most tile hints were removed and
replaced with **meta-tools**: `define_agent`/`call_agent`/`delete_agent`, `run_code` (for example, writing
a Python pathfinder), `notepad_edit` (committed to a public git repo in real time), and
`define_map_marker`/`delete_map_marker`. The map tool was renamed from "Map Memory" to **"Mental Map"**
after the model assumed it reflected live state. It only updates from tiles that have been visible on
screen. That is a fairness constraint very like ours.

**Runs:** Blue took about **813 hours** (finished May 2, 2025, with light human intervention). A second,
fully autonomous Blue run took about 406 hours (June 9, 2025). **Gemini 3 Pro beat Crystal** without
losing a battle: turn 24,178, about 1.88 billion tokens, using roughly half the turns of 2.5 Pro.

**Failures that matter to us:**
- **Training-data contamination.** It hunted for "TEA," an item from the remakes, for many hours.
  Telling it to ignore prior knowledge produced a "Full Heal restores HP" delusion and about 100 hours of
  looping. Telling it to ignore internal knowledge also hid knowledge that was correct. BG3 has the same
  risk: Early Access and patch-era knowledge differs from Patch 8.
- **Context poisoning.** A hallucinated goal entered the goal list and the model fixated on it.
- **Notes reinforcing a wrong premise.** In Goldenrod Underground it built a full truth table in the
  notepad on the false assumption that switch order didn't matter, so the table never converged. Once an
  NPC hint went into the notepad as "CRITICAL HINT," the puzzle was solved within hours. Notes need a
  place for *assumptions under test*.
- **Untested assumptions.** It assumed post-battle NPC dialogue was unimportant and never checked. In a
  dialogue-heavy CRPG this is fatal.
- **"Panic"** when party HP was low degraded its reasoning.
- **Exploration compulsion.** A "uncover every tile" directive made it refuse to enter key buildings over
  a single unexplored tile.

Sources: [The Making of Gemini Plays Pokémon](https://blog.jcz.dev/the-making-of-gemini-plays-pokemon),
[Gemini 3 Pro vs 2.5 Pro in Crystal](https://blog.jcz.dev/gemini-3-pro-vs-25-pro-in-pokemon-crystal),
[public notepad repo](https://github.com/waylaidwanderer/gemini-plays-pokemon-public),
[dbreunig case study of the Gemini 2.5 tech report](https://www.dbreunig.com/2025/06/17/an-agentic-case-study-playing-pok%C3%A9mon-with-gemini.html),
[TechCrunch](https://techcrunch.com/2025/05/03/googles-gemini-has-beaten-pokemon-blue-with-a-little-help).

### 1.3 Continual Harness (arXiv 2605.09998, 2026)

A **Refiner** runs every F steps. It reads the recent trajectory and applies CRUD to four parts of the
harness: prompt, sub-agents, skills (code) and memory. Memory updates "add entries to fill gaps, update
stale entries, and demote importance for areas the agent has moved past." On Emerald with Gemini 3 Pro it
reached 100% of milestones at a median cost of $130, versus $215 for the baseline. Findings that bear
directly on our memory design:
- **"Most authored entries sit unused."** Retrieval has to be designed in, not assumed.
- In a **1,003-turn loop**, the agent wrote a memory directive based on a broken tool and then ignored
  contrary evidence. **Memory writes must be checked against environment feedback.**
- New tools and ideas appear in the first 50–200 turns of a new bottleneck. After about 500 stalled turns
  the agent stops inventing and repeats itself. This argues for a stuck detector that forces a change of
  approach.
- Gains depend on the model: weak models get worse with self-editing harnesses.

Source: [arXiv 2605.09998](https://arxiv.org/html/2605.09998v1).

### 1.4 GPT plays Pokémon (o3, GPT-5)

o3 finished Red in 18,184 steps. GPT-5 finished it in **6,470 steps** (about 7 days), mostly by
over-levelling one Pokémon. Bradshaw notes that the o3 run used an advanced custom harness like Gemini's.
**UNVERIFIED:** harness specifics (the Clad3815 stream); I found no primary write-up. The step counts
show how much a strong model plus a strong harness compresses the game.
Sources: [TechRadar](https://www.techradar.com/ai-platforms-assistants/chatgpt/gpt-5-just-completed-pokemon-red-in-a-new-world-record-time-claude-gemini-and-chatgpt-o3-arent-even-close),
[Bradshaw shortform](https://www.lesswrong.com/posts/ekF2EDwKyZJNuxBTb/julian-bradshaw-s-shortform).

A related 2026 data point is **Jev** (TypeSafe AI), a non-LLM decision model that picks from a list of
options. It beat Red in under a week, with Claude Opus 5 acting as a *coach* that watched the game log and
rewrote the option list. Source: [Tom's Hardware](https://www.tomshardware.com/tech-industry/artificial-intelligence/developer-says-jev-decision-model-beat-pokemon-red-in-under-a-week-non-llm-engine-succeeds-where-traditional-chatbots-stalled-for-months-but-claude-opus-5-coached-the-model-through-its-dead-ends).
The lesson is that good **enumerated action menus** (like BG3's own action bar and dialogue options) are
very powerful.

### 1.5 Minecraft: Voyager and Mindcraft

- **Voyager** (Wang et al., 2023; [arXiv 2305.16291](https://arxiv.org/abs/2305.16291)). The state is
  text from the Mineflayer API (no pixels). Actions are **JavaScript programs**. Three components: an
  automatic curriculum, a **skill library** of verified programs retrieved by embedding of their
  description, and iterative prompting with environment feedback, execution errors and **self-verification
  by a critic**. It reports 3.3× more unique items and tech-tree milestones up to 15.3× faster than prior
  methods. Lessons: (a) store *procedures that worked* as reusable code or recipes; (b) a skill is only
  added after it is verified in the environment.
- **Mindcraft** ([github.com/kolbytn/mindcraft](https://github.com/kolbytn/mindcraft), 2024–2025;
  MineCollab paper 2025). Built on Mineflayer with `!command`-style tools (`!goToPlayer`, `!collectBlocks`,
  `!craftRecipe`, …), each a blocking, high-level action implemented in JS. It keeps a per-bot memory
  summary that is rewritten when the conversation history is trimmed (**UNVERIFIED** detail: file
  `bots/<name>/memory.json`). The design point that transfers: high-level, engine-implemented actions with
  interrupt semantics, not raw inputs.

### 1.6 Cradle (BAAI; RDR2, ICML 2025)

General computer control from **screenshots only**, acting through keyboard and mouse. It has six modules:
information gathering, self-reflection, task inference, **skill curation** (procedural memory as code),
action planning, and memory. Episodic memory stores key screenshots plus reasoning, and recent multimodal
history is periodically summarised into long-term summaries. Cradle was the first agent to follow the RDR2
main story through roughly 40-minute missions. A 2026 third-party comparison (SPIKE) puts it at 43.6%
success on 13 RDR2 tasks. The lesson: pure-pixel general control works for short missions, but is slow,
expensive and brittle for a 100-hour game. BG3 should not depend on it.
Sources: [arXiv 2403.03186](https://arxiv.org/pdf/2403.03186), [GitHub BAAI-Agents/Cradle](https://github.com/BAAI-Agents/Cradle),
[SPIKE](https://arxiv.org/pdf/2605.18636).

### 1.7 Benchmarks: VideoGameBench and BALROG

- **VideoGameBench** ([arXiv 2505.18134](https://arxiv.org/abs/2505.18134v2)). The VG-Agent is ReAct plus
  a textual scratchpad written after every step, playing from raw frames. The best model (Gemini 2.5 Pro)
  averaged **0.48%** completion in real time and 1.6% in the paused "Lite" mode. No model reached the first
  checkpoint in 9 of 10 games. Raw-pixel agents without a harness barely progress, and real-time pressure
  makes things worse. BG3's pause-friendly, turn-based combat is an advantage, and the harness should
  **pause the world** (turn-based mode or menus) while the agent thinks wherever it can.
- **BALROG** ([arXiv 2411.13543](https://arxiv.org/pdf/2411.13543), ICLR 2025). NetHack is the hardest
  environment: about 1% progression for 2024 frontier models. Vision input often *hurt* compared with text.
  The live leaderboard is at balrogai.com.

### 1.8 NetHack: NetPlay and code skills

- **NetPlay** ([arXiv 2403.00690](https://arxiv.org/pdf/2403.00690)). GPT-4 picks from **predefined
  skills**, and an **event monitor interrupts a running skill when something important happens** (a
  monster appears, HP drops). It works best with detailed context and struggles with ambiguous tasks.
  `move_to` in BG3 needs exactly this: it should return early when combat starts, dialogue triggers, or a
  trap or perception check pops.
- **"Up and Down the Abstraction Ladder" / CodeHack.** Code-based skills **nearly tripled progression and
  cut inference cost per episode by 86%** compared with primitive actions.

### 1.9 Card and strategy games: Slay the Spire, Balatro, Civilization

- **STS2MCP** ([github.com/Gennadiyev/STS2MCP](https://github.com/Gennadiyev/STS2MCP)). A C# mod serves
  **localhost REST on :15526**, and a separate Python MCP server wraps it. The tools include
  `get_game_state`, a capped `get_compendium`, and `search_wiki(query, item_type, limit)`, which **only
  searches entries the player has discovered**. That is a nice fairness pattern. A full Ironclad run cost
  about **8M tokens** with Sonnet 4.6.
- **STSAgent** (ifree): a Java mod that reads game memory, with MCP control and analyze/play/chat modes.
- **BalatroLLM / BalatroBot** ([github.com/coder/balatrollm](https://github.com/coder/balatrollm)). A Lua mod
  exposes **JSON-RPC 2.0** endpoints, the LLM decides, and results are on balatrobench.com. The repo ships
  a `CLAUDE.md`.
- **civ6-mcp** ([github.com/lmwilki/civ6-mcp](https://github.com/lmwilki/civ6-mcp)). The best design
  reference for a turn-based MCP:
  - stdio MCP ↔ **persistent TCP to the FireTuner debug port 4318**. It **generates Lua at runtime** and
    runs it in GameCore (reads) or InGame (writes). This is the same pattern as the BG3SE debugger port.
  - **76 tools**, mid-level (`get_cities`, `city_action`, `get_map_area`, `get_pathing_estimate`,
    `dismiss_popup`, …), plus "advisor" tools that pre-digest options.
  - **State is returned as narrated text**, not raw JSON.
  - **`end_turn` requires five non-empty reflection fields** (tactical, strategic, tooling, planning,
    hypothesis). They are written to a diary *before* the turn resolves, and the tool returns a
    **before/after diff** (damaged units, grown cities, completed builds, threats). `get_diary(last_n)` is
    called at session start or after compaction. **This is the strongest single pattern to copy**: a
    forced, structured memory write tied to a natural checkpoint.
- **Vox Deorum** (Civ V, 2025). The LLM sets strategy and the built-in AI executes it. Across 1,408 full
  games, LLMs "survive as long as the game goes," while earlier pure-LLM agents could not finish a game.
  This is hybrid delegation: the LLM decides *what*, engine code decides *how*.

### 1.10 CRPGs: Divinity, Disco Elysium, BG3

I found **no public report of an LLM agent autonomously playing a full CRPG** (Divinity: Original Sin 1/2,
Pillars, Disco Elysium or BG3) as of 2026-10-09. The closest projects:
- **BaldurRL** ([github.com/vrk210/BaldurRL](https://github.com/vrk210/BaldurRL)): an RL project whose first
  milestone is a *simulated* 1v1 Level 2 Fighter vs Goblin. It does not talk to the game.
- **bg3-companion** ([github.com/mdn87/bg3-companion](https://github.com/mdn87/bg3-companion), 2026,
  Windows): screenshot → Codex → advice, with opt-in "Smart next move" of up to three mouse or keyboard
  actions, checking the screen after each. No mod and no game state. Only a limited live check (Sept 5,
  2026).

### 1.11 Cross-cutting summary

| Project | State repr. | Action level | Navigation help | Memory | Context resets | Outcome / length |
|---|---|---|---|---|---|---|
| Claude Plays Pokémon | screenshot + grid + little RAM | buttons + on-screen navigator | on-screen click-to-go | self-organised KB in prompt | accordion summaries (~30 actions) | Red finished May 2026 after ~2 years of model iterations; 100k+ step stalls |
| Gemini Plays Pokémon | screenshot + **rich text from RAM** + text map | buttons, sub-agents, later code | pathfinder sub-agent, frontier list | goals, notepad (git), markers, mental map | summary every 100 turns | Blue 813 h → 406 h; Crystal at turn 24k |
| GPT-5 Pokémon | custom (UNVERIFIED) | — | yes | — | — | Red in 6,470 steps |
| Voyager | text API | JS programs | engine | verified skill library | — | strong Minecraft tech tree |
| Cradle | pixels only | keyboard/mouse + curated skills | none | episodic + procedural | periodic summaries | 40-min RDR2 missions |
| NetPlay / CodeHack | text | skills + interrupts | skills | history | — | skills ≈3× progress, −86% cost |
| civ6-mcp | narrated text | 76 mid-level tools | pathing estimate | forced per-turn diary | get_diary after compaction | full games playable (no published results) |
| VideoGameBench | raw frames | raw inputs | none | scratchpad | — | <2% completion |

**Takeaways for BG3:**
1. Text first, screenshots second. Expose structured, *visible* state as text. Screenshots are for
   ambiguity, spatial layout and catching UI states the bridge doesn't model.
2. Use the highest action level the game already offers: engine pathfinding, the action bar, and
   dialogue option lists.
3. Long actions need interrupts (NetPlay).
4. Force structured memory writes at checkpoints (civ6-mcp diary).
5. Keep facts and hypotheses apart, record provenance, and keep history of the notes (Gemini, Claude 4.6).
6. Prefer fresh short contexts plus good handoffs over one long context (Gemini's 100k looping).
7. Add a stuck detector and a forced change of strategy (Continual Harness, both Pokémon runs).
8. Watch for wrong game-knowledge recall (TEA). Tag wiki recollections as unverified until seen in game.

---

## 2. Existing BG3 bridges and how they do IPC

**Important for this repo:** BG3SE (Norbyte's Script Extender, latest release **v32, 2026-06-21**) is a
Windows DLL (`DWrite.dll` proxy). On Linux it works **only under Proton**, not with the native build this
machine runs ([wiki.bg3.community](https://wiki.bg3.community/Tutorials/Mod-Use/How-to-install-Script-Extender)).
Every BG3 bridge below sits on top of BG3SE except bg3-companion and the macOS rewrite.

| Project | Direction | IPC mechanism | Details |
|---|---|---|---|
| **Inworld BG3 mod** ([repo](https://github.com/BlocTheWorker/Inworld-BaldursGate-Mod), [Nexus 8179](https://www.nexusmods.com/baldursgate3/mods/8179)) | game → app | **File drop.** `Server/Inworld.lua` runs an Osiris timer (`Osi.TimerLaunch("BG3InworldTrigger", 5000)`) that writes `data.json` (host position, party equipment from `DB_PartyMembers`, `DB_CurrentLevel`) with SE's file-save helper (`Ext.IO.SaveFile` → the SE data dir) every 5 s. | The app → game direction is a **DirectX hook overlay** plus a local Node/fastify socket between the desktop app and the connector, with gRPC to the Inworld cloud. The game never receives commands over IPC. Verified by reading the Lua source. |
| **TwitchPointsCast** ([Nexus 13479](https://nexusmods.com/baldursgate3/mods/13479)) | app → game | **File drop.** SAMMI (plus Node.js) writes per-reward `.txt` files into the mod's `Rewards` folder. Lua reads them and applies statuses or spells listed in `rewardsListData.json`. | Polling via `Ext.IO.LoadFile` on a timer (**UNVERIFIED**: exact poll loop). |
| **Crowd Control for BG3** ([guide](https://crowdcontrol.live/guides/baldursgate3)) | bidirectional | Requires `ScriptExtenderSettings.json`: `"EnableLuaDebugger": true, "LuaDebuggerPort": 9998`. Warns that "other mods that use this port for Lua communication will not be compatible." So CC almost certainly connects to the **BG3SE Lua debugger TCP server** and uses its evaluate messages as RPC (**UNVERIFIED**, inferred). | Requires DX11 and Patch 7+. |
| **BG3SE Lua debugger** ([Docs/Debugger.md](https://github.com/Norbyte/bg3se/blob/main/Docs/Debugger.md)) | bidirectional | **TCP 127.0.0.1:9998**, protobuf (`BG3Extender/Lua/Debugger/LuaDebug.proto`, "DBG protocol version 3"). Messages: `DbgConnectRequest`, `DbgEvaluate` → `BkEvaluateResponse`, `DbgGetVariables`, `DbgReset`, `DbgFetchMods`, `BkDebugOutput` (prints). | A generic remote-eval channel. You send Lua and get results and print output back. Statement evaluation *in a stack frame* isn't supported, but top-level evaluation is. Also an Osiris debugger (`osidebug.proto`). |
| **bg3-data-mcp** ([repo](https://github.com/holypolarpanda7/bg3-data-mcp)) | bidirectional (modding) | **Console keystroke injection.** A persistent PowerShell process uses `AttachConsole` + `WriteConsoleInput` into the SE console window (about 0.34 s per call). Output is read back by **tailing the "Extender Runtime" log**. Each eval is wrapped in unique BEGIN/END markers and `pcall`. Access is serialised. | MCP tools: `bg3_se_status`, `bg3_se_eval(code, context)`, `bg3_se_command(line)`, `bg3_se_log`, `bg3_se_hot_load`, … Windows/WSL only. A hacky but proven pattern: a marker-delimited eval over a log. |
| **bg3se-macos** ([repo](https://github.com/tdimino/bg3se-macos)) | bidirectional | A **ground-up rewrite** of SE for macOS ARM64. Exposes a **Unix domain socket `/tmp/bg3se.sock`** (`bg3se-console`, or `socat - UNIX-CONNECT:/tmp/bg3se.sock`), plus file commands in `~/Library/Application Support/BG3SE/commands.txt`. A Python harness (`bg3se_harness run "<lua>"`, 37 commands, JSON on stdout) can `launch --headless`. | **The closest analogue to what this project needs on native Linux:** an injected native library hosting Lua plus a Unix socket. Its `.mcp.json` only wires Ghidra/search MCPs for development, not a gameplay MCP. |
| **BG3 official Twitch extension** (Larian) | game → Twitch | A built-in, closed integration. Viewers vote on dialogue choices and can inspect party stats, spells and inventory. | Proves Larian already serialises "what the player sees" (party, dialogue options) for an external consumer. Not usable directly. |
| **bg3-companion** | none (pixels) | Screenshots, plus Windows input injection | — |
| **Baldur's Forge** ([repo](https://github.com/patriot1burke/baldurs-forge)) | offline | RAG over an item DB | No live game link. |

**BG3SE primitives available for a bridge** (from [Docs/API.md](https://github.com/Norbyte/bg3se/blob/main/Docs/API.md)):
- `Ext.IO.LoadFile(path, [context])`, `Ext.IO.SaveFile(path, content)`: file-drop IPC.
- `Ext.Json.Stringify/Parse`, `Ext.DumpExport(obj)`: serialisation.
- `Ext.Timer.WaitForRealtime(ms, cb)` (OS clock, keeps ticking when the game is paused) and
  `Ext.Timer.WaitFor` (game clock): polling loops. **Use realtime, or the bridge stalls while paused.**
- `Ext.Osiris.RegisterListener(event, arity, "before"|"after", fn)`: event push (dialogue started,
  combat started, `TimerFinished`, …).
- `Ext.Events.GameStateChanged`: detect load or menu transitions.
- `Ext.RegisterConsoleCommand`: a console entry point.
- **No socket or HTTP API in Lua.** External IPC is files, the debugger port, or native code in the host
  (the bg3se-macos approach).

**Implication** (to be settled by the bridge-feasibility research, not this file): either (a) run under
Proton with stock BG3SE, using the **debugger port 9998** for request/response plus Osiris listeners
writing an **event file** for push; or (b) stay native and build a bg3se-macos-style injected `.so` that
exposes a **Unix socket**. Either way, the MCP server should talk to a small, stable protocol, for example
newline-delimited JSON `{id, op, args}` → `{id, ok, result|error}` plus an event stream. Then the bridge
can change without touching the agent-facing tools.

---

## 3. Memory design for a multi-session playthrough

### 3.1 Principles (each traceable to a failure above)

1. **Don't hand-maintain what the game already tracks.** Party, HP, levels, inventory, gold, the quest
   journal, the known waypoint list and the current region are **generated by the harness from live,
   player-visible state** into read-only snapshot files, or simply queried with tools. Gemini's PP
   hallucination was fixed by injecting RAM data, not by prompting the model to look. The agent's notes
   hold what the game *doesn't* hold: intentions, judgments, promises, suspicions, plans, lessons and
   the reasons behind choices.
2. **Separate fact, hypothesis and plan.** Every non-trivial note is tagged
   `[seen]` (observed in this playthrough), `[told]` (an NPC said it, which may be a lie), `[wiki]`
   (recalled from general knowledge, unverified here) or `[guess]`. Hypotheses live in a dedicated
   section with a test plan, so a wrong premise can't silently become a truth table (Goldenrod).
3. **Provenance and dates.** Use `(S042)`, the session number, on each fact. Stale entries become visible.
4. **Two kinds of file.** Curated files are small, rewritten in place and always loaded. Logs are
   append-only, never rewritten and loaded on demand. Deletion only happens in curated files and goes
   through git, so an "overzealous cleanup" (Opus 4.6) can be diffed and reverted.
5. **Hard size budgets** keep the always-loaded set small (target ≤ 8k tokens) so every session starts
   with most of the context free.
6. **Retrieval is designed, not hoped for.** The index file names *when* to read each file, and skills
   say "before a dialogue with a named NPC, grep `npcs.md` for them." This counters Continual Harness's
   "most entries sit unused."
7. **Reconcile against live state each session** and record discrepancies as lessons.

### 3.2 File layout

```
memory/                         # git repo (or subdir of this repo); committed by the harness
  PLAYTHROUGH.md                # INDEX + "you are here". Always loaded. ≤ 120 lines.
  HANDOFF.md                    # written at end of each session, read first next session. ≤ 60 lines.
  objectives.md                 # current goal stack (main / side / opportunistic) + blockers. ≤ 80 lines.
  hypotheses.md                 # open questions & assumptions under test, each with a test. ≤ 60 lines.
  strategy.md                   # lessons-learned & playbook (combat tactics, harness quirks). ≤ 150 lines.
  decisions.md                  # irreversible choices, why, and known/expected consequences. Append-mostly.
  npcs.md                       # one entry per significant NPC: where met, attitude, promises, threads.
  locations.md                  # per region: waypoints, unexplored leads, locked doors, merchants, hazards.
  quests.md                     # agent's *commentary* on journal quests: plan, leads, dead-ends (not the journal text).
  party-build.md                # build intentions per companion (class plan, ASI plan, gear wishlist), not current stats.
  inventory-notes.md            # only judgments: "keep X for Y", "sell list", key items and what they open.
  markers.json                  # tool-managed map markers {id, region, xyz|entity, label, session}
  snapshots/                    # GENERATED by harness at session end — read-only to the agent
    party.md  inventory.md  journal.md  waypoints.md  status.md
  sessions/
    S001.md ... S042.md         # append-only per-session log (timestamped bullets), ≤ 300 lines each
  chapters/
    act1-part1.md ...           # compacted summaries of every ~10 sessions (written by compaction step)
  screenshots/                  # saved visual memory (Opus 4.7 lesson), referenced by path from notes
```

**PLAYTHROUGH.md** (always loaded; the only file whose structure is fixed):

```markdown
# Playthrough index
Character: Tav "Ilse", Half-Elf Cleric (Life) L5 · Difficulty: Balanced · Started S001
Now: Act 1 · Region: Blighted Village (WP: Blighted Village) · Session S042 · Save: S042-end
Main thread: find a cure for the tadpole → currently: reach Grymforge via Underdark (see objectives.md)

## Read when
- Starting a session: HANDOFF.md, objectives.md, then snapshots/status.md
- Before talking to a named NPC: grep npcs.md
- Entering/leaving a region: locations.md#<region>
- Before an irreversible choice: decisions.md + hypotheses.md
- Before/after combat: strategy.md#combat
- Before level-up: party-build.md

## Party (one line each; numbers come from snapshots)
- Ilse (Cleric) · Shadowheart · Lae'zel · Astarion — Gale at camp
## Standing commitments (things we promised / must not forget) — max 8
- Promised Mol to ... (S031) [told]
## Top 3 lessons (full list in strategy.md)
1. Always check `get_dialogue` roll DC display before choosing skill options; save first.
```

**HANDOFF.md** (rewritten each session; the next agent reads it first):

```markdown
# Handoff S042 → S043  (2026-10-09 21:55, game time: day 7)
Save to load: "S042-end" (quick-check: party at Blighted Village well, out of combat, no dialogue)
## Doing right now
Heading to the Windmill to free the gnome (objective O-12). Path cleared; 2 goblins at the windmill top unaware.
## Next 3 steps
1. Long rest? NO — resources fine (Ilse 2/4 L1 slots) — rest after windmill.
2. Talk to the gnome; expect [Persuasion]; ask about Underdark.
3. Return to the Blighted Village waypoint; then objective O-9.
## Watch out
- Lae'zel approval low after S041 choice; avoid sparing goblins in front of her? [guess]
## Unresolved / in-progress
- H-3: is the cellar hatch in the apothecary locked or just hidden? test: inspect hatch (hypotheses.md)
## Session stats
Actions 214 · combats 2 (won) · deaths 0 · new quests 1 · notes edited: npcs.md, locations.md
## Discrepancies found at start of this session
- notes said Astarion had the Rune of Dendar; inventory shows Lae'zel has it → fixed notes.
```

**Entry formats** (short, greppable, one entry per heading):

```markdown
## Kagha  (Emerald Grove, Druid)          ← npcs.md
- Met S004 at Grove ritual [seen]; hostile to tieflings [seen]
- Claims shadow druids are allies [told]; suspicious letter found S006 [seen]
- Thread: decide whether to expose her → decisions.md#D-07
- Last updated S012

## D-07 Exposed Kagha to Zevlor/ Rath (S013)   ← decisions.md
Choice: showed the letter publicly. Why: protect tieflings, gain Druid trust.
Visible consequences: Kagha dropped the ritual [seen S013]. Expected later: Act 2 shadow druid hostility [wiki].
Reversible? no.

## H-3 Apothecary cellar  (open, S041)     ← hypotheses.md
Belief: there's a hidden hatch [guess]. Evidence: draft marker on screenshot screenshots/S041-apoth.jpg.
Test: high-perception character searches near the counter; inspect. Close if 2 searches fail.
```

### 3.3 Update protocol

| When | What | Who |
|---|---|---|
| **Session start** (hook) | Inject PLAYTHROUGH.md + HANDOFF.md + a fresh `status` dump. Agent loads save named in handoff if the game isn't already there. | harness |
| **Start reconciliation** (first 5 min) | Agent compares HANDOFF/objectives/party-build claims vs live `get_party`, `get_inventory`, `get_journal`, `get_location`. Every mismatch → fix note + one line in HANDOFF "Discrepancies". | agent, via skill |
| **Event-triggered writes** (immediately, small) | After: a quest update · an irreversible decision · a new named NPC conversation · a promise · a death/near-wipe · a puzzle solved · a new waypoint · a hypothesis confirmed/refuted. One-line append to `sessions/Sxxx.md` always; curated file only if it changes the plan. | agent (skills say so) |
| **Checkpoint writes** (forced) | `end_combat_report` and `long_rest` tools require a short `reflection` arg (what worked / what to change / next goal) — civ6-mcp pattern; harness appends it to the session log. | harness-enforced |
| **Pre-irreversible** | Before choices flagged irreversible (leave act, kill/spare major NPC, long rest with pending timed quests): save (`save_game("S042-pre-D08")`), read decisions.md + hypotheses.md, write the decision entry *before* choosing. | agent |
| **Session wrap-up** (hook at ~50 min) | Harness: stop accepting game actions except movement out of danger → agent writes HANDOFF.md, updates objectives.md, trims curated files to budget → harness saves game `Sxxx-end`, regenerates `snapshots/`, runs size lint, `git commit -m "S042"`. | both |
| **Compaction** (every 10 sessions or when a file exceeds budget) | Dedicated short session (or wrap-up extension): merge `sessions/S0xx` into `chapters/<act>-<n>.md` (≤ 150 lines); prune curated files: resolved hypotheses → one line in strategy or decisions; finished regions in locations.md → collapse to 3 lines. **Never delete a `[seen]` fact from decisions.md or npcs.md; demote instead** (move to an `## Archive` section). | agent, guided by skill |

**Size limits** are enforced by a lint script run in the wrap-up hook, which prints offenders so the agent
must trim them. Limits: PLAYTHROUGH ≤ 120 lines, HANDOFF ≤ 60, objectives ≤ 80, hypotheses ≤ 60,
strategy ≤ 150, other curated files ≤ 400 (they are grepped, not loaded whole).

### 3.4 Avoiding drift and hallucinated memories

- **Generated snapshots are authoritative** for anything the game shows. If a note contradicts a
  snapshot, the snapshot wins and the conflict is logged as a lesson.
- **Facts carry provenance tags.** The harness's lint flags a curated line with no tag.
- **Wiki knowledge stays `[wiki]` until confirmed in game.** A `[wiki]` item is never a top-level
  objective unless the journal or an NPC corroborates it. This guards against Gemini's TEA and Patch 8 vs
  Early Access differences.
- **Hypotheses expire.** A hypothesis open for 3 sessions without a test forces either a test or a demotion.
- **Git as the safety net.** The wrap-up hook prints `git diff --stat` and **refuses to commit if more than
  N lines were deleted from decisions.md or npcs.md** without an `ARCHIVE:` reason. A human can audit and
  revert.
- **A goal stack with a "why" per goal** lets a later agent drop goals whose reason was a guess.
- **Stuck detector (harness).** If the region and journal are unchanged and there has been no XP or gold
  change and no new NPC dialogue for K actions (about 60), inject a structured nudge: "List 3 alternative
  approaches, pick one not tried, write it in hypotheses.md." Both Pokémon runs and Continual Harness show
  loops are the main cost driver.
- **Fairness.** Snapshots and tools only contain information the player can see, so the memory can't be
  poisoned with hidden information.

---

## 4. MCP server design for a game

### 4.1 Tool granularity

- **Mid-level, engine-backed verbs**: `move_to(target)`, `talk_to(npc)`, `use_ability(ability, target)`,
  `choose_dialogue(n)`. Not raw clicks, and not mega-macros like "clear the goblin camp." The evidence:
  Pokémon navigator vs raw buttons, Mindcraft `!commands`, the NetHack skills result, civ6-mcp's 76 tools.
- **Targets use short session-stable handles** (`e14`, `i203`, `wp:BlightedVillage`), not 36-character
  GUIDs. The server maps handles to engine IDs. Handles appear in every state dump, which saves a lot of
  tokens and avoids GUID transcription errors.
- **Read tools are separate from act tools.** Act tools are idempotent where possible, and dangerous ones
  are marked (`destructiveHint`) and named clearly (`end_turn`, `long_rest`, `load_game`).
- **Advisor tools** are optional and pre-digest visible information only. Examples: `list_interactables`
  (visible objects within N m, sorted by distance), `get_frontier` (visible unexplored exits and doors),
  `ability_preview(ability, target)` (hit chance or damage as the game's own tooltip shows it, nothing
  more).
- **Keep the tool count manageable.** About 30–40 tools; Claude Code's tool search can defer rarely used
  ones. Group rare operations (`manage_inventory(op, …)`) instead of making ten tiny tools.
- **An escape hatch for UI states the bridge doesn't model**: `press_key(key)`, `click(x, y)` and
  `screenshot()`. Every unmodelled modal (a tutorial popup, a reward screen) is otherwise a hard stall.
  Log every use of the escape hatch so the bridge can model those states later.

### 4.2 Long-running actions

- **Default: block until settled, with a bounded timeout and interrupts.** `move_to` returns when the
  character arrives, **or** when an interrupt fires (combat started, dialogue started, a
  perception/trap/stealth popup, a party member downed, a cutscene), **or** after a timeout of about 20 s,
  with `status: arrived|interrupted:<reason>|timeout|blocked` plus the current position. This is NetPlay's
  event monitor. The result always includes **what changed** (a delta), so the agent rarely needs a
  separate `get_state`.
- **Why not async start/poll?** Polling costs tool calls and tokens. In Claude Code, an MCP call still
  running after **2 minutes is moved to a background task automatically**
  (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`; the per-server `timeout` in `.mcp.json` defaults to about
  28 hours). Keep every call well under 2 minutes. The 2025-11-25 MCP **Tasks** primitive
  (call-now/fetch-later, now the `io.modelcontextprotocol/tasks` extension in the 2026-07-28 spec) exists,
  but isn't needed for these durations, and client support is unclear (**UNVERIFIED** for Claude Code).
- **Things that are truly long** (`long_rest` with camp dialogues, `fast_travel` with a loading screen,
  `end_turn` while enemies act) also block until a stable state: the party's turn again, out of the
  loading screen, or a dialogue awaiting input. Cap them at about 90 s and return
  `status: still_running` with a `wait()` tool to continue.
- **"Stable state" detection lives in the bridge** (loading flag, dialogue state, combat turn owner,
  movement velocity ≈ 0). This mirrors Claude Plays Pokémon's "wait for the player to stop moving before
  the next screenshot."

### 4.3 Screenshots in tool results

- MCP tools can return `{"type":"image","data":<base64>,"mimeType":"image/jpeg"}`. Claude Code shows PNG,
  JPEG, GIF and WebP results **inline** to the model (possibly downscaled) and saves the original. Image
  results count toward `MAX_MCP_OUTPUT_TOKENS` (**warning at 10k tokens, default cap 25k**).
- Claude's image cost is about `width × height / 750` tokens: **1280×720 ≈ 1.2k tokens**, 1920×1080 is
  downscaled first. At ~300 actions per hour, a screenshot on every call would cost about 370k tokens per
  hour. So:
  - `screenshot(region?, scale?)` is **on demand**, defaulting to 1280×720 JPEG at quality ~75.
  - A **`zoom(x, y, w, h)`** tool returns a crop at native resolution (the Opus 4.7 lesson).
  - Action tools accept `with_screenshot: bool` (default false). The harness auto-attaches a screenshot
    only on *unexpected* states (an unknown modal, an interrupt it couldn't classify).
  - `save_screenshot(label)` writes to `memory/screenshots/` so notes can reference visual memory.
- Wayland capture is covered in the screen-capture research. The MCP server just needs a
  `capture() -> JPEG bytes` backend.

### 4.4 Token budgeting of state dumps

- **Narrated, line-oriented text**, not pretty JSON. JSON key overhead roughly doubles tokens. civ6-mcp
  returns narrated text for the same reason. For example:

```
STATUS  Act1 · Blighted Village (outdoor) · day 7 · out of combat · TB-mode off · gold 412
PARTY   *Ilse  Cleric5  HP 31/38  AC18  slots L1 2/4 L2 1/3 L3 2/2  cond: Blessed(2t)
        Shad   Cleric5  HP 24/36  AC17  slots L1 0/4 ...         cond: —
        Laez   Fighter5 HP 49/49  AC19  actsurge 1  superiority 4/4
        Asta   Rogue5   HP 12/33  AC16  cond: Poisoned(3t)
NEARBY  (visible, ≤25m)  e14 Goblin Archer  hostile  18m NE  [roof]   e15 Goblin  hostile 21m NE
        e20 Barcus Wroot  neutral 6m W (talk)   i31 Chest 4m S (locked)   d2 Door 9m N
EVENTS  since last call: Asta took 4 poison dmg; Laez gained 50 XP; quest "Rescue the Gnome" updated
```

- Defaults stay short (about 300–800 tokens). Detail comes from `get_character(id, sections=…)`,
  `get_inventory(owner, filter=…)` and `get_journal(quest?)`.
- **Deltas over dumps.** Each act tool returns `EVENTS since last call` and only the changed party lines.
- **Distance and visibility filters live in the bridge.** Visible means in line of sight or revealed, never
  stealthed or unrevealed. That is both a token filter and the fairness gate.
- **Caps and pagination** on lists (`limit`, `offset`), following STS2MCP's capped run history and
  10-result wiki search.
- **Budget target:** ≤ 1.5k tokens for the average tool result and ≤ 4k for any text result. Set
  `anthropic/maxResultSizeChars` per tool if needed.

### 4.5 Turn-based combat loop

BG3 combat is round-based with initiative. Party members adjacent in initiative can act in any order.
Each creature has an action, a bonus action, movement and a reaction.

```
get_combat_state()  →
  ROUND 2 · your turn: Ilse (action ✓ bonus ✓ move 9.0m reaction ✓)  next: e14, Laez, e15, Shad, Asta
  ENEMIES (visible) e14 Goblin Archer HP ~"Wounded" 14m roof high-ground  e15 Goblin HP "Healthy" 5m
  ALLIES  Laez 4m (engaged e15) ...
  SURFACES grease 3m S (visible)
  ACTIONS (Ilse) a1 Guiding Bolt(L1+) a2 Sacred Flame a3 Healing Word(bonus, L1+) a4 Shove a5 Dash ...
use_ability(ability="a1", target="e14", slot=1)  →  "Guiding Bolt hit e14 for 14 (Badly Wounded)..."  + updated economy
move_to(target="xyz|e15", mode="safe")         →  path, opportunity-attack warning as the game shows it
end_turn()  → blocks through enemy turns until next party turn or combat end; returns combat log of
              what was *visible* (enemy moves/attacks seen), HP deltas, new conditions
```

- HP for enemies is shown the way the game shows it: exact if the UI shows numbers. Use the game's
  setting. **Never show hidden stats or AC unless the UI exposes them.**
- **Hit chances**: the game shows a percentage when targeting, so `ability_preview` may return exactly
  that.
- **Combat skill file** (see Recommendation): pre-combat checklist (positioning, a buff, surprise),
  per-turn heuristic (focus the lowest-HP visible threat; control > damage; don't waste a reaction), and
  post-combat `end_combat_report(reflection)`, which is forced and loots.
- **Defeat handling**: on a party wipe, `load_game(last_autosave|named)`, then a mandatory lesson entry.
  Save-scumming policy is the **user's decision** (see open questions).

---

## 5. Recommendation

### 5.1 Architecture

```
┌──────────────────────── Claude Code session (≈1 h) ────────────────────────┐
│ CLAUDE.md (rules, fairness, loop) · skills/ · hooks (SessionStart, Stop,     │
│ timer) · built-in Read/Edit/Grep on memory/                                  │
└───────────────┬──────────────────────────────────────────────────────────────┘
                │ MCP stdio
┌───────────────▼──────────────── bg3-mcp (Python) ──────────────────────────┐
│ tool layer: handles↔engine IDs, narration, deltas, interrupts, timeouts,      │
│ fairness filter (visible-only), screenshot capture/zoom, save naming,         │
│ stuck detector, action log (JSONL), snapshot generator                       │
└───────────────┬──────────────────────────────────────────────────────────────┘
                │ NDJSON over Unix socket ($XDG_RUNTIME_DIR/bg3-bridge.sock)
┌───────────────▼──────────────── in-game bridge ────────────────────────────┐
│ native: injected .so w/ Lua or direct ECS reads (bg3se-macos model)  — OR —   │
│ Proton: BG3SE mod; RPC via Lua debugger :9998, events via Ext.IO file/Osiris │
└──────────────────────────────────────────────────────────────────────────────┘
```

Splitting the MCP server from the bridge keeps the agent-facing tools stable while the bridge mechanism
(native vs Proton) is still being decided in the other research files.

### 5.2 MCP tools (about 36)

**Observe:**
`get_status()` (the compact dump above) · `get_party()` · `get_character(id, sections)` ·
`get_nearby(radius=25, kinds?)` · `get_inventory(owner?, filter?, limit?)` · `get_journal(quest?)` ·
`get_map(region?)` (discovered waypoints, discovered POIs, harness markers; text) ·
`get_frontier()` (visible unexplored exits/doors/ladders) · `get_dialogue()` · `get_combat_state()` ·
`get_events(since?)` · `screenshot(region?, scale?)` · `zoom(x, y, w, h)`

**Act, exploration:**
`move_to(target, mode=walk|sneak|safe, timeout_s=20)` · `interact(target, verb?)` (open, pick up, lever,
read) · `talk_to(npc)` · `loot(container|corpse, take=all|list)` · `search_area()` (an active perception
search, if the game provides one; returns whatever the game reveals) · `jump_to(target)` ·
`set_party(formation|chain|unchain|select)` · `fast_travel(waypoint)` · `toggle_turn_based()`

**Act, dialogue:**
`choose_dialogue(option_n)` → returns the next line/options, including any visible roll and its displayed
DC · `roll_dialogue_check(use_inspiration?, add_guidance?)`

**Act, combat:**
`use_ability(ability, target?, slot?, position?)` · `ability_preview(ability, target)` · `end_turn()` ·
`end_combat_report(reflection)` (forced reflection, civ6 style)

**Act, management:**
`manage_inventory(op=move|equip|unequip|sell|buy|drop|use|split, …)` · `trade(npc)` (opens and returns
the barter lists) · `level_up(character, choices)` (with `get_level_up_options(character)`) ·
`short_rest()` · `long_rest(reflection)` (forced reflection; returns camp events/dialogues) ·
`camp(action)` (swap companions, chest)

**Meta:**
`save_game(label)` · `load_game(label|"latest")` · `list_saves()` · `wait(seconds)` (for still_running) ·
`place_marker(label, target)` / `delete_marker(id)` · `press_key(key)` / `click(x, y)` (escape hatch,
logged)

Every act tool returns `status` + `EVENTS` delta + changed lines, and accepts `with_screenshot`.

### 5.3 Prompt and skill files

- **`CLAUDE.md`** (≤ 2k tokens): mission (finish the main story); fairness rule (use only tools, no file
  reading of game data); the core loop (observe → plan briefly → act → read the delta); memory protocol
  summary and size limits; when to take screenshots; save discipline (named save before irreversible
  choices and before boss fights); "if stuck for 3 attempts, change approach and write a hypothesis";
  knowledge-tagging rules (`[seen]/[told]/[wiki]/[guess]`). Keep it short and prune band-aids as models
  improve (the Hershey lesson).
- **Skills** (`.claude/skills/<name>/SKILL.md`, loaded on demand):
  - `session-start`: load the save, reconcile notes against live state, set the session goal.
  - `session-wrapup`: handoff template, trim to budgets, save `Sxxx-end`, commit.
  - `combat`: pre-fight checklist, turn heuristics, action economy, surfaces, focus fire, retreat rules,
    post-combat report.
  - `dialogue`: read the whole option list; check npcs.md and decisions.md first; skill-check policy;
    record promises.
  - `exploration`: frontier-driven exploration, loot policy, traps and perception as surfaced by the game,
    when to leave an area (avoid the Gemini "every tile" compulsion).
  - `party-and-leveling`: build plans and level-up procedure.
  - `compaction`: merge session logs into chapters and archive rules.
  - `stuck`: a structured unsticking procedure (alternatives, screenshot plus zoom, check the journal,
    re-read hypotheses, try a different party member or skill).

### 5.4 Memory layout

As in §3.2: the git-tracked `memory/` with curated, generated and append-only tiers, provenance tags,
budgets enforced by lint, and HANDOFF.md as the session baton.

### 5.5 Session lifecycle

1. **Launcher script** (outside Claude): ensure the game is running and the bridge socket is up; pick the
   session number; run `claude -p` or an interactive session with the `--mcp-config` and a SessionStart
   hook.
2. **SessionStart hook** injects PLAYTHROUGH.md + HANDOFF.md + `get_status()` output. The agent runs
   `session-start`: load the save if needed, reconcile, then write the session goal into `sessions/Sxxx.md`.
3. **Play loop** (about 45–50 min). Event-triggered note writes; forced reflections at combat end and
   long rest. The harness stuck detector injects nudges through tool results (an `ADVICE:` line).
   Claude Code auto-compaction may happen mid-session. CLAUDE.md tells the agent to re-read HANDOFF and
   `sessions/Sxxx.md` after compaction (the civ6 `get_diary` pattern).
4. **Wrap-up** (triggered by the MCP server: at T+50 min every tool result carries
   `WRAPUP: finish current action, then run session-wrapup`, and at T+58 act tools refuse). The agent
   gets to a safe state (out of combat and dialogue where possible), writes the handoff and trims notes;
   the harness saves `Sxxx-end`, regenerates snapshots, runs lint and the deletion guard, and commits.
5. **Every 10 sessions** the next session starts with `compaction`.
6. **Human review hooks**: `git log` and diffs of `memory/`, the JSONL action log, and the escape-hatch
   usage log (a backlog of bridge features to build).

### 5.6 Why this design

- It matches what demonstrably worked: text-first state (Gemini), engine-level navigation (Pokémon
  navigators, Mindcraft), interrupts (NetPlay), forced checkpoint reflections and diaries (civ6-mcp),
  zoom and saved screenshots (Opus 4.7), and short contexts with summaries (Gemini's 100k finding).
- It addresses what demonstrably failed: hallucinated or sticky beliefs (provenance, hypotheses that
  expire, generated snapshots), lost notes (git plus a deletion guard), unused notes ("read when" index
  and skills), loops (stuck detector), and training-data contamination (`[wiki]` tagging).
- It respects the fairness rule structurally. The only data path is the visible-only bridge filter and
  real screenshots, and the memory can only contain what the agent saw or recalled.
- It costs little to run. Mid-level tools and deltas keep each session in the low hundreds of thousands
  of tokens, against about 8M for a full Slay the Spire 2 run (STS2MCP data). A full BG3 run will
  probably take hundreds of sessions (**UNVERIFIED** estimate; human main-story time is roughly 70–100 h).

### 5.7 Open questions for the user

- **Save-scumming policy**: reload on a party wipe only, or also on failed dialogue rolls? This affects
  how strong the "player-fair" claim is.
- **Difficulty** (Explorer/Balanced/Tactician) and whether the agent may change it.
- **Wiki use**: the brief allows general knowledge. Should the agent also get a web search tool, or only
  what it already knows?

---

## Sources (consolidated)

- Claude Plays Pokémon: https://www.latent.space/p/how-claude-plays-pokemon-was-made ·
  https://www.zenml.io/llmops-database/building-and-deploying-a-pokemon-playing-llm-agent-at-anthropic ·
  https://www.lesswrong.com/posts/7mqp8uRnnPdbBzJZE/is-gemini-now-better-than-claude-at-pokemon ·
  https://www.lesswrong.com/posts/gogZyeistdaDFuhbG/claude-plays-pokemon-opus-4-5-follow-up ·
  https://www.lesswrong.com/posts/sehJYg5Yny9fvpbpt/a-year-late-claude-finally-beats-pokemon ·
  https://guildofscientifictroubadours.com/?p=14521 ·
  https://www.shacknews.com/article/149574/claude-ai-pokemon-firered-fable-5
- Gemini Plays Pokémon: https://blog.jcz.dev/the-making-of-gemini-plays-pokemon ·
  https://blog.jcz.dev/gemini-3-pro-vs-25-pro-in-pokemon-crystal ·
  https://github.com/waylaidwanderer/gemini-plays-pokemon-public ·
  https://www.dbreunig.com/2025/06/17/an-agentic-case-study-playing-pok%C3%A9mon-with-gemini.html ·
  https://techcrunch.com/2025/05/03/googles-gemini-has-beaten-pokemon-blue-with-a-little-help
- Continual Harness: https://arxiv.org/html/2605.09998v1
- GPT/Jev: https://www.techradar.com/ai-platforms-assistants/chatgpt/gpt-5-just-completed-pokemon-red-in-a-new-world-record-time-claude-gemini-and-chatgpt-o3-arent-even-close ·
  https://www.tomshardware.com/tech-industry/artificial-intelligence/developer-says-jev-decision-model-beat-pokemon-red-in-under-a-week-non-llm-engine-succeeds-where-traditional-chatbots-stalled-for-months-but-claude-opus-5-coached-the-model-through-its-dead-ends
- Voyager https://arxiv.org/abs/2305.16291 · Mindcraft https://github.com/kolbytn/mindcraft ·
  Cradle https://arxiv.org/pdf/2403.03186 https://github.com/BAAI-Agents/Cradle · SPIKE https://arxiv.org/pdf/2605.18636
- VideoGameBench https://arxiv.org/abs/2505.18134v2 · BALROG https://arxiv.org/pdf/2411.13543 ·
  NetPlay https://arxiv.org/pdf/2403.00690
- STS2MCP https://github.com/Gennadiyev/STS2MCP · BalatroLLM https://github.com/coder/balatrollm ·
  civ6-mcp https://github.com/lmwilki/civ6-mcp (via https://glama.ai/mcp/servers/byfx9pqhvs)
- BG3: https://github.com/Norbyte/bg3se (Docs/API.md, Docs/Debugger.md, LuaDebug.proto; release v32) ·
  https://wiki.bg3.community/Tutorials/Mod-Use/How-to-install-Script-Extender ·
  https://github.com/BlocTheWorker/Inworld-BaldursGate-Mod · https://nexusmods.com/baldursgate3/mods/13479 ·
  https://crowdcontrol.live/guides/baldursgate3 · https://github.com/holypolarpanda7/bg3-data-mcp ·
  https://github.com/tdimino/bg3se-macos · https://github.com/mdn87/bg3-companion ·
  https://github.com/vrk210/BaldurRL · https://github.com/patriot1burke/baldurs-forge
- MCP/Claude Code: https://code.claude.com/docs/en/mcp · https://modelcontextprotocol.io/seps/1686-tasks ·
  https://aaif.io/blog/handle-long-running-mcp-tool-calls-with-the-mcp-tasks-extension
