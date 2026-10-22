# The bg3 MCP server

The bridge (`bridge/`, package `bg3ai`) is a normal MCP server. It reads the game through the BG3AI mod and acts in
it, and it is the only thing a playing agent talks to. The runner starts it for every session. This page is for
running it yourself, with an MCP client of your own.

You need the game running with the mod and bg3le ([SETUP.md](SETUP.md)), and uv.

## Start it

```bash
uv run --project bridge bg3ai-mcp                                                  # stdio: one client
uv run --project bridge bg3ai-mcp --transport http --host 127.0.0.1 --port 8770    # MCP streamable HTTP at /mcp
```

Over HTTP the server binds to 127.0.0.1 and port 8770 unless told otherwise. A client in a container reaches it on
the docker network's gateway address ([tools/container/README.md](../tools/container/README.md)).

The server reads its settings from the environment once, at start (`bridge/src/bg3ai/settings.py` for paths, the
session deadline, the port and the RPC pool). The ones a standalone client may want:

| Variable | Default | What |
|---|---|---|
| `BG3LE_DEBUG_PORT` | `9998` | bg3le's debug port in the game process, where the server talks to the mod |
| `BG3AI_MEMORY_DIR` | `play/memory` | the playthrough's memory, which the memory tools read and write |
| `BG3_KNOWLEDGE_DIR` | `play/knowledge` | read-only reference notes, shared by every playthrough |
| `BG3AI_RUNTIME` | `runtime` | where the server writes its logs (`actions.jsonl`, metrics) |
| `BG3AI_SAVE_DIR` | the Larian profile's save folder | the saves `list_saves` and `load_game` see |
| `BG3AI_PROMPT_CONDITION` | `rules` | which rules text the server serves (below) |
| `BG3AI_AUTOSKIP` | on | `0` = the run lets cutscenes play: the server starts no movie skipper (no shim ESCAPE) |
| `BG3AI_TOOLS`, `BG3AI_TOOLS_OFF` | every tool | which tools it serves (below) |

## A memory

The memory tools need a memory folder. Create one with

```bash
python3 tools/memory/new_playthrough.py my-run                                  # playthroughs/my-run/memory, linked from play/memory
python3 tools/memory/new_playthrough.py my-run --memory-dir DIR --no-link       # DIR, and play/memory is left alone
```

and point `BG3AI_MEMORY_DIR` at it if it isn't `play/memory`. With a memory, the first tool call opens session
S001 (`memory/sessions/S001.md`) and its result begins with the rules of play. Without one, no session opens, but
the first result still begins with the same rules.

## The rules and the server instructions

The server sends the rules of play four ways, so a client needs no files from this repo:

- in the `instructions` of its initialize result: the server's own notes on the tools, then `=== RULES ===` and
  the rules text;
- at the top of the first tool result, once per session (per server process when there is no memory).
  `session_start(rules=true)` shows them again;
- as the MCP prompt `rules`;
- as the resource `bg3://rules`.

`BG3AI_PROMPT_CONDITION` picks the text:

- `rules` (the default): how the game, the tools, fairness and the session clock work. This is the ranked
  benchmark's condition. The text leaves out the rules of a tool group that is switched off.
- `rules+coaching`: the rules plus how to play well and the memory protocol, about three times as long. Only in
  this condition do tool results carry the coaching nudges (`NEXT:`, `PACE:`, `NOTES WINDOW`).

`BG3AI_SERVE_RULES=0` takes the rules out of the instructions and the first result. The prompt and the resource
still serve them. It is meant for Claude Code in `play/`, which reads the same text from `play/CLAUDE.md`, so with it
set and no condition given, the condition is `rules+coaching`. `BG3AI_RULES_PATH=FILE` serves that file verbatim
instead of the assembled text. The rule files themselves are in `packages/harness/prompts/`.

## Tools

Every tool belongs to one group. The `game` group is every tool in no other group, and it is always on.

| Group | Tools |
|---|---|
| `memory` | `memory_list`, `memory_read`, `memory_search`, `memory_write`, `memory_edit`, `memory_append`, `note`, `session_start`, `end_session`, `recall` |
| `wiki` | `wiki_search`, `wiki_page` |
| `reload` | `load_game` |
| `saves` | `save_game`, `list_saves` |
| `screenshots` | `screenshot`, `zoom` |
| `raw_input` | `press_key`, `click`, `ui_dump` |

- `BG3AI_TOOLS=wiki,saves` keeps the game tools plus the groups and single tool names listed.
- `BG3AI_TOOLS_OFF=wiki,reload` removes groups or single tool names, on top of `BG3AI_TOOLS`.
- The tools are removed when the server starts, before any client lists them, so a switched-off tool can't be
  called at all.
- An unknown name stops the server, so a typo can't serve the wrong set:

  ```
  ValueError: BG3AI_TOOLS: unknown tool group or tool name 'memroy' (groups: game, memory, wiki, reload, saves, screenshots, raw_input)
  ```

A model without image input should play with `BG3AI_TOOLS_OFF=screenshots`.

## What the agent never sees

Everything a tool returns is what a player could see on screen or remember having seen. The layers that make
that true are in [FAIRNESS.md](FAIRNESS.md): the mod's perception gate, the UI-mirroring projections, opaque
handles (`e12`) instead of game GUIDs, and the rest.

Some data the mod keeps is for the benchmark's graders only, and no tool can reach it:

- the referee ring (`ref.*` ops): crimes nobody witnessed, kills out of sight;
- the roll log (`roll.*`): every die rolled;
- the scenario setup (`scn.*`): dice and names set up for a seeded run. The mod refuses every `scn.*` op unless
  the game process was started with `BG3AI_SCENARIO_SETUP=1`. The runner sets it only for a seeded run, and
  `tools/saves/make_start_save.py` needs it to make a start save; a playthrough never has it.

Only the harness calls these ops (`bridge/src/bg3ai/referee.py`, `bg3harness/game_settings.py`), and it writes
what it reads to `facts.jsonl` and `rolls.jsonl` in the run's directory, outside the memory and knowledge folders.
`bridge/tests/test_referee.py` fails if any other bridge module names one of these ops or imports the reader, or if
a tool is named for them.

## Rules your client must keep

The comparison between agents is only fair if the bg3 tools are the agent's only way to the game:

- Give the agent no other tools: no file, shell or web tools, and no other MCP servers. With a shell or file access
  it could read game files, saves, logs or this repo, or send Lua to the debug port, which skips every filter above.
- The memory lives in the memory folder and is read and written through the memory tools only.
- Your own prompts must not add game information a player wouldn't have. General Baldur's Gate 3 knowledge is
  allowed, as for a player who has read the wiki.
- If you can't switch your client's built-in tools off, run it in the container sandbox, where it can reach only
  the bg3 server and its model API ([tools/container/README.md](../tools/container/README.md)).

Results from your own client are a separate "bring your own agent" condition, apart from the ranked track.

Two practical points. Give tool calls a long timeout: some tools wait for the game, and `play/.mcp.json` uses 110
seconds. The harness's clients call the tools one at a time (pi's extension, `tools/pi/bg3.ts`, makes its calls
sequential); do the same.

## A minimal client config

Many MCP clients read an `mcpServers` JSON file. For the bg3 server over stdio:

```json
{
  "mcpServers": {
    "bg3": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/bg3-ai/bridge", "bg3ai-mcp"],
      "env": {"BG3AI_MEMORY_DIR": "/path/to/bg3-ai/playthroughs/my-run/memory"},
      "timeout": 110000
    }
  }
}
```

`play/.mcp.json` is the same shape (with `BG3AI_SERVE_RULES=0`, for Claude Code). Over HTTP, point the client at
`http://127.0.0.1:8770/mcp`. With the Python MCP SDK, a client that starts the server from that file and reads
the game state is:

```python
import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main(config: str) -> None:
    s = json.load(open(config))["mcpServers"]["bg3"]
    params = StdioServerParameters(command=s["command"], args=s["args"], env=s.get("env"))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        init = await session.initialize()
        print(len(init.instructions or ""), "characters of instructions")
        print(len((await session.list_tools()).tools), "tools")
        print((await session.call_tool("get_status", {})).content[0].text)

asyncio.run(main(sys.argv[1]))
```

Run it in the bridge's environment: `uv run --project bridge python client.py mcp.json`.

`tools/pi/test_docs.py` runs this config and this client against the fake mod (no game), along with the tool
groups, the typo refusal and the conditions above.
