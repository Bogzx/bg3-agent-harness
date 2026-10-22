# Benchmark bias: where the harness was tuned on Claude

Every session so far was played by Claude (Opus through `claude -p`; the memory keeper was Sonnet). When the agent
failed, we fixed the harness. Code fixes (a better route, a tool that no longer times out) help any model. Text
fixes are different: we wrote rules into the prompts that answer *Claude's* mistakes. A model with other mistakes
gets no such help. This file lists those places, what they bias, and what a fair cross-model benchmark should do.

Evidence: the agent-facing files themselves, the git log, and the developers' working notes (the per-area
implementation diaries and the backlog, which are not in the public repo).

## Where the text was tuned on Claude's failures

**play/CLAUDE.md** (the system-level rules, ~320 lines). Many rules exist because one Claude session broke them:
- "Their real names are `mcp__bg3__<name>`… a call to the bare name fails": the first call of S009–S013 used the
  bare name.
- "Never pass `wait=true` to walks", with the numbers: in claude-run-2 S001 all 31 walks after the first blocked,
  0 of 96 actions overlapped (from the working notes). The agent copied its own
  earlier calls.
- "Trust the result; don't re-read": S023–S026 made 11 calls a minute, a third of them re-reads.
- "Load others with ToolSearch only when you need one": S005 lost 10 turns to ToolSearch.
- Save discipline "never every few minutes": S009 saved 7 times in 20 minutes.
- "Stuck never ends a session": S001 wrapped up 16 minutes early at a bugged ladder.
- The notes-while-walking rules and `note()`: S023–S026 spent 20–30 s a session on memory edits while idle.

**The runner's kickoff** (`runner/play.py`):
- `PACE_PREFLIGHT`, a worked example in the first message, was added because S001 read the CLAUDE.md pipelining
  rules and blocked anyway.
- The full tool names in the kickoff (commit 5e12129, after S009–S013).
- `STUCK_DIRECTIVE` ("write three approaches that are genuinely different") answers Claude's repeat loops.

**Playbooks** (`play/knowledge/playbooks/`):
- session-wrapup: "don't save Sxxx-end again" (S025 saved it 4 times and checked list_saves 3 times); "never kill
  time" (S036 idled 2.5 minutes waiting for the wrap-up mark).
- exploration: food and gold rules from S025–S026 (two partial rests, 1000 of 1093 gold spent).
- combat: variant spell ids "that fail every session (S023–S025)".
- dialogue: send the best talker (S013 sent Shadowheart, Persuasion −1).
- party-and-leveling: never hand-drive the level-up screen (S018 lost ~10 minutes).

**Tool descriptions and result text** (`bridge/src/bg3ai/server.py`):
- The `wait` docs, the `PACE:` nudge, the pace guard note, "rarely needed" on `session_clock`, and the terse `⏱`
  line between full ones (S009 guessed the time was up 15 minutes early).
- `end_session`'s and the stop check's "keep playing" text: "your next message is a game tool call, not a
  summary… a text-only message ends your turn". It answers S030's 7 early stops, and it describes Claude Code's
  turn model.
- `ALWAYS_LOAD`: which tool schemas are loaded up front was chosen from Claude Code's ToolSearch counts in
  S001–S039, and it is sent as the Anthropic-specific `_meta "anthropic/alwaysLoad"`.

**The stop policy** (`tools/memory/session.py` `stop_decision`): the grace window, the cap on "keep playing"
blocks and the fix-notes pass were all tuned on Claude's early-stop habits (S018–S025 bounces, S036).

**harness-news.md**: entries that "retire" lessons Claude wrote down wrongly (for example the S002 "tutorial
popup" lesson). They correct one model's notes.

**Budgets and client details**: the 9.5 KB memory briefing (Claude Code's 10,000-char hook cap), the 110 s tool
timeout (Claude Code backgrounds calls at 2 minutes), the byte-identical prompt prefix for Anthropic's prompt
cache, and the ~1 hour session length chosen around Claude's context size.

**The memory keeper** used Sonnet on Opus's notes, so a run mixed two models. Since 2026-10-13 it defaults to the
play model (`--keeper-model same`).

## What this biases

- **Toward Claude.** Claude's score includes weeks of coaching aimed at its own failure modes. Another model's
  failure modes have no coaching yet, so it will look worse than it is.
- **Toward long, rule-heavy prompts.** ~320 lines of CLAUDE.md plus playbooks reward a model that follows long
  instructions well. A model that does better with less text is not measured at its best.
- **Claude's history as the agent's own.** Lines like "In S001 the agent…" tell any model about another model's
  past as if it were its own.
- **Client semantics.** "Ends your turn", ToolSearch, `mcp__bg3__` names and the alwaysLoad hint describe Claude
  Code. A different client or model may read them as noise or as wrong instructions.
- **The latency metrics** were tuned with Claude's response times in mind; a faster or slower API changes them
  without any change in play.

The 2026-10-13 change moved memory, the sandbox, session start/end and the pace guard from Claude Code hooks into
the MCP server, so those now work the same for any client. The text above did not change.

## How another client gets the rules (2026-10-16)

Claude Code reads play/CLAUDE.md itself. Any other MCP client gets the same file from the server
(`bridge/src/bg3ai/rules.py`): appended to the `instructions` of the initialize result, prepended to the first
briefing of each session (`session_start`, or the first tool result in manual mode, for a client that ignores
`instructions`), and as the `rules` prompt and the `bg3://rules` resource. `session_start(rules=true)` shows them
again (after a context compaction). `$BG3AI_RULES_PATH` overrides the file; `BG3AI_SERVE_RULES=0` (set for Claude
Code, which already has the file) turns off the instructions and briefing copies.

The server also stops the session for any client: after the deadline + `$BG3AI_STOP_GRACE_S` (default 5 min;
the runner's hard stop is at + 10 min), every tool except reads, memory tools, `note`, `save_game`, `list_saves`
and the session tools is refused with "call end_session now".

**Rewritten at serve time** (`rules.ADAPT`; a test fails when play/CLAUDE.md stops matching):
- the tool-names paragraph: the `mcp__bg3__` prefix, "a call to the bare name fails", which tools are loaded up
  front, and ToolSearch. Served as: the bare names, "your client may show them with a prefix", call them directly.

**Served as written, still Claude-Code-flavoured** (harmless or roughly true for other clients; candidates for the
rules/coaching split above):
- "End your turn once it reports the commit", "if you end your turn anyway": Claude Code's turn model. For a
  client without turns it means "stop calling tools".
- "your first message starts with the memory briefing": true only under the runner's kickoff; elsewhere the
  first `session_start` (or first tool result) carries it.
- "the runner" in the clock and stop text, and the SESSION CLOCK reminder (was a Claude Code hook; now the clock
  line in results).
- "You have no shell, no web access and no file tools": true in the runner's Claude Code config; for another client
  it is a rule to follow, and only the container recipe (`tools/container/`) enforces it.
- "context compaction": Claude Code's name for it; other clients may truncate or summarise differently.
- The session anecdotes (S001, S005, S009, S023–S026 …) stay in the served text, so other models read Claude's
  history as their own (see above).

## Status on the overhaul (2026-10-22, pi round 8)

Each item above checked against the `overhaul` branch. Fixed = no longer biased in what a non-Claude model is
served; still biased = needs a change (owner named); n/a = only Claude Code sees it or it applies to every model
alike. "Rules" below is the harness default condition (packages/harness/prompts/assemble.py:69 `CONDITIONS`).

| Item | Status 2026-10-22 | Evidence |
|---|---|---|
| play/CLAUDE.md rules with S001–S039 stories | fixed | the text is now assembled from packages/harness/prompts (rules/ vs coaching/, assemble.py:1-15); play/CLAUDE.md is the generated rules+coaching copy (play/CLAUDE.md:1); prompts/tests/test_prompts.py `ANECDOTE_RE` lints every part (no `S\d{3}`) |
| Rules vs coaching split (fair-benchmark step 2) | fixed | `assemble("rules")` is the default for every non-Claude-Code client (bridge rules.py:42-52, `$BG3AI_PROMPT_CONDITION`); coaching only in `rules+coaching` |
| `mcp__bg3__` names, "bare name fails", ToolSearch in the rules | fixed | only clients/claude.md (and clients/claude-kickoff.md) carry them, appended for `--client claude` only (assemble.py:102-103); rules/core.md:3-5 is neutral |
| "End your turn" / Claude Code turn model in the rules | fixed | test_prompts.py `CLIENT_ONLY_RE` (your turn, context compaction, ToolSearch, mcp__) on every rules part |
| Server instructions (sent to every client, every condition) | fixed in this round | they held coaching ("Pipeline like a human clicking ahead", "keep planning while the character walks; wait_for only when…", "Use screenshot only when…"): now tool facts only (bridge/src/bg3ai/tools/core.py:33-50); the coaching is in coaching/pace.md and efficiency.md. Test bridge/tests/test_served_text.py |
| Tool descriptions with session stories | fixed in this round | combat_turn ("S023-S026: 175 back-to-back calls") and wiki_page ("S018-S027 pages ran ~2.5 KB") lost them (tools/turn_tool.py:20, tools/session.py:97); test_served_text.py lints all 95 descriptions (memory/sessions/S012.md, a path example, is allowed) |
| JSON inside strings (D15) | fixed in this round | do_actions/queue_actions/combat_turn were done (CHANGELOG 2026-10-22 D15); level_up `plan`, create_character `plan` and set_reactions `settings` still said "JSON" on a string-only schema and now take the object too (tools/progression.py:60, tools/newgame.py:90, tools/uitools.py:151). Tests test_batch_args.py `ObjectArgs`, and test_served_text.py: any argument a description calls JSON must accept an object or array |
| "rules" prompt / bg3://rules descriptions said "play/CLAUDE.md, the rules Claude Code loads" | fixed in this round | tools/memory.py:156-163 |
| Server-side stop refusal named end_session, HANDOFF.md, save_game even with those tools off | fixed in this round | rules.py `stop_refusal(off=)` names only the tools the session has; reporting.py passes the removed ones. Test test_rules.py `test_the_refusal_names_only_the_tools_the_session_has` |
| end_session "NOT ENDED" text: "your next message is a game tool call, not a summary (… a text-only message ends your turn)", "the SESSION CLOCK reminder" | **still biased** (owner: playthrough, packages/playthrough/src/bg3play/memory/session.py:653, :679-684) | served to every client through end_session (bridge memtools.py:435-440). The CHANGELOG's D15 entry says end_session's results were made neutral; this reason text was not. Proposed: "do not stop: call a game tool next (get_status if you lost track)" and "when the ⏱ line says WRAP UP" |
| alwaysLoad `_meta "anthropic/alwaysLoad"` | n/a (Claude Code only) | tools/session.py:144-193: 78 of 95 tools marked; other clients ignore `_meta` and list every schema anyway. It brings Claude Code near the others; the 17 left deferred cost Claude Code a ToolSearch turn (slightly against Claude). Record `BG3AI_ALWAYS_LOAD` in the manifest for the Claude Code condition |
| 110 s tool timeout | n/a (same for every client) | play/.mcp.json `timeout` 110000; pi: bg3.ts:49 default 110 s and packages/harness/src/bg3harness/pi_client.py:349 passes the same 110 s. The server's own budgets (newgame.py `CC_BUDGET_S` 95 s, combat_turn chain ≤80 s) sit under it for every client |
| Runner kickoff (`PACE_PREFLIGHT`, mcp__bg3__ line, "play following CLAUDE.md", JSON-string `queue_actions('[…]')` example) | **still biased** (owner: harness/runner, D11) | runner/play.py:1073-1104 is still the old hardcoded text for every client and condition. assemble.pace_preflight(client) (assemble.py:126) already gives the coaching pre-flight with the Claude line only for claude; the kickoff should use it only in a coaching condition, say "the rules" instead of CLAUDE.md, and show `queue_actions([{"tool":"interact","target":"e15"}])` |
| `STUCK_DIRECTIVE` ("write three approaches…") | still coaching (owner: runner) | runner/play.py:1249; a playthrough feature (multi-session), not in a D5 one-session benchmark run. Should be off outside the app |
| Result nudges: `PACE: you waited right after issuing…`, `NOTES WINDOW: ~20s of walking left` | **still coaching in every condition** (owner: bridge; needs a decision) | tools/reporting.py:421-424, tools/batch.py:291-297. Served to every model alike, but they are coaching written on Claude's habits, so the "rules" condition is not rules only. Proposed: off unless the server's condition has coaching; that needs the runner to set `BG3AI_PROMPT_CONDITION=rules+coaching` for Claude Code (play/.mcp.json), else the app loses them. Orchestrator's call |
| The `⏱` clock line, "keep playing: you'll be told when to wrap up" | n/a (rule) | tools/core.py `KEEP_PLAYING`; described in rules/clock.md; same for every client |
| Tool count | fixed | 95 tools for every client; the init check counts the server's tools for pi and Claude alike (round 7, runner/clients.py); tools/pi/conformance/tools.json is regenerated from the server |
| Image handling per client | **still biased for text-only models** (owner: harness/runner) | screenshot/zoom return a JPEG (tools/observe.py:630, :651). Claude Code shows it. pi passes it on (pi-mcp toLlmContent) and, through OpenRouter's chat completions, sends it as a separate user message after the tool result ("Attached image(s) from tool result:", pi-ai openai-completions.js:1105-1120), but only when the model's `input` lists image; otherwise the model gets "(see attached image)" and no picture, from a tool the rules say shows the screen. Fix: turn the `screenshots` group off (`BG3AI_TOOLS_OFF=screenshots`) for a model without image input and record it in the manifest |
| Playbooks under knowledge/ with S0xx stories | **still biased where memory is on** (owner: play/knowledge) | play/knowledge/playbooks/*.md and tools.md still name sessions (S025, S036 …). The rules condition doesn't include them, but rules/capabilities/memory.md points at knowledge/ (playbooks), so a rules-condition run with the memory group on can read Claude's coaching. Fix: memory off in benchmark runs (D5), or a knowledge dir without playbooks for the rules condition |
| harness-news.md | still coaching (owner: play/knowledge) | play/knowledge/harness-news.md; reaches the model only through the memory briefing (playthrough app) |
| Stop policy tuned on Claude's early stops | n/a in a D5 benchmark run | packages/playthrough/src/bg3play/memory/session.py `stop_decision`; a benchmark run has no memory session; the server-side stop (rules.py) is the same for all |
| 9.5 KB briefing (Claude Code's hook cap), prompt-cache-stable prefix | n/a | briefings come from the server for every client now; the byte-stable text helps every cached API |
| Memory keeper on another model | fixed | `--keeper-model same` default (runner/play.py:1770); D5: no keeper in benchmark runs |
| Prompt hash in the manifest | **incomplete** (owner: harness, run_manifest.py) | `prompt.sha256` hashes assemble() only; pi's system text is the server instructions + the rules (tools/pi/dump_server.py:32), and Claude Code also gets the instructions. Hash what the client is actually given (the dumped system file for pi; CLAUDE.md + instructions for Claude) |

## What a fair cross-model benchmark should do

1. **Freeze the harness.** Tag one version of the code and every agent-facing text before measuring. No fixes
   during a benchmark run.
2. **Split rules from coaching.** Rules say how the game and tools work, and what is fair. Coaching says "don't do
   X" because a model did X. Run each model with rules only, and with rules plus coaching. Report both.
3. **Take the anecdotes out of the agent's text.** Keep session numbers and Claude's mistakes in dev notes. Write
   the rule, not the story.
4. **Equal tuning budget.** Either no model gets prompt tuning, or each gets the same: the same number of tuning
   sessions, the same process, the same person-hours.
5. **Same client for every model.** Use one generic MCP client (the HTTP transport and `tools/container/` make
   that possible), or report the client as part of the configuration. Don't rely on client-only features like
   alwaysLoad or ToolSearch.
6. **One model per run.** Keeper and player use the same model (`--keeper-model same`, the default), or the
   keeper is off (`--no-keeper`) for every model.
7. **Held-out starts.** Tuning happened on Act 1 and the Shadow-Cursed Lands with one character. Measure on other
   starts too: a new game with another brief, another class, a later save.
8. **Several runs per model.** Report the spread, the cost and the latency. Normalise latency for API speed.
9. **Keep this file current.** Any new text written because of one model's failure goes here.
