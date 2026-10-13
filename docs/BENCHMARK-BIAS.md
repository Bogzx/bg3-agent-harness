# Benchmark bias: where the harness was tuned on Claude

Every session so far was played by Claude (Opus through `claude -p`; the memory keeper was Sonnet). When the agent
failed, we fixed the harness. Code fixes (a better route, a tool that no longer times out) help any model. Text
fixes are different: we wrote rules into the prompts that answer *Claude's* mistakes. A model with other mistakes
gets no such help. This file lists those places, what they bias, and what a fair cross-model benchmark should do.

Evidence: the agent-facing files themselves, `research/impl-*.md`, `docs/BACKLOG.md` and the git log (the last two
are dev notes, not in the public repo).

## Where the text was tuned on Claude's failures

**play/CLAUDE.md** (the system-level rules, ~320 lines). Many rules exist because one Claude session broke them:
- "Their real names are `mcp__bg3__<name>`… a call to the bare name fails": the first call of S009–S013 used the
  bare name.
- "Never pass `wait=true` to walks", with the numbers: in claude-run-2 S001 all 31 walks after the first blocked,
  0 of 96 actions overlapped (research/impl-setup.md, "Why wait=true on every walk"). The agent copied its own
  earlier calls.
- "Trust the result; don't re-read": S023–S026 made 11 calls a minute, a third of them re-reads.
- "Load others with ToolSearch only when you need one": S005 lost 10 turns to ToolSearch.
- Save discipline "never every few minutes": S009 saved 7 times in 20 minutes.
- "Stuck never ends a session": S001 wrapped up 16 minutes early at a bugged ladder.
- The notes-while-walking rules and `note()`: S023–S026 spent 20–30 s a session on memory edits while idle.

**The runner's kickoff** (`runner/play.py`):
- `PACE_PREFLIGHT`, a worked example in the first message, was added because S001 read the CLAUDE.md pipelining
  rules and blocked anyway (research/impl-setup.md).
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
