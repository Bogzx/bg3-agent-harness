# Security

## Reporting a problem

Please report security issues privately through GitHub's private vulnerability reporting (the "Report a
vulnerability" button on the repository's Security tab), not in a public issue.

## What counts

The harness runs a model-driven agent next to your game and your files, so the boundaries around it matter:

- The playing agent's sandbox. The client runs with its built-in tools switched off, and the bg3 MCP server's
  memory tools may only read and write the playthrough's `memory/` folder and read `knowledge/`
  (`bridge/src/bg3ai/memtools.py`). A way for the agent to read or write anything else, or to run code, is a
  security issue.
- The container sandbox (`tools/container/`). A client in it should reach only the bg3 MCP server and, through the
  egress proxy, its own model API. A way out of that is a security issue.
- The game's Lua debug port. bg3le's debug server runs any Lua it is sent inside the game and listens on
  127.0.0.1:9998. Anything that exposes it beyond loopback is a security issue.

Ways for the agent to learn hidden game information are fairness bugs. Report those as normal issues
(docs/FAIRNESS.md).

## What it doesn't protect against

Any local process running as your user can reach the debug port, and the MCP server too when it runs over HTTP. Don't run the harness on a
machine shared with users you don't trust.
