# An MCP client in a container that can reach only the bg3 server

The bg3 MCP server enforces the playing agent's sandbox itself (`bridge/src/bg3ai/memtools.py`): its memory tools
reach only `memory/` and read-only `knowledge/`. A client that has its own file, shell or web tools (another agent
framework, a model you don't want to trust) can still step around that on the host. This recipe puts such a client
in a container: no repo, game files, saves or logs inside, and a network whose only useful peer is the bg3 server.

```
host                                              container (bg3ai/mcp-client)
  bg3ai-mcp --transport http                        --network bg3ai-sandbox (docker --internal)
    --host 172.30.99.1 --port 8770   <── HTTP ───    MCP client → http://172.30.99.1:8770/mcp
  game, saves, repo, memory                          no internet, no host files, --cap-drop ALL, read-only root
```

## Run it

```sh
tools/container/run.sh                  # creates the network, builds the image, starts the server, runs the smoke test
tools/container/run.sh --no-server      # the server already listens on 172.30.99.1:8770
tools/container/run.sh -- sh            # anything else in the client container
```

The smoke test (`mcp_smoke.py`) lists the tools, calls `memory_list`, checks that `memory/.git/config` is refused,
and checks the container can't reach the internet. It prints `OK`. Tested 2026-10-13 (docker 29.7.2).

The server takes its usual env from the shell that starts it (`BG3AI_MEMORY_DIR`, `BG3AI_SESSION_DEADLINE`,
`BG3AI_SESSION`, ...). To serve a session with a deadline, start it yourself with that env:
`uv run --project bridge bg3ai-mcp --transport http --host 172.30.99.1 --port 8770`. One server serves one game; with
`BG3AI_SESSION` unset, `session_start` opens the next memory session and `end_session` commits it, so a long-lived
HTTP server can serve session after session.

## A real agent client

The client needs no files from the repo: the server serves the rules of play (play/CLAUDE.md) in its MCP
`instructions`, at the top of the first `session_start` of each session, and as the `rules` prompt and the
`bg3://rules` resource. With `BG3AI_SESSION_DEADLINE` set, it also refuses game actions 5 minutes after the
deadline (`BG3AI_STOP_GRACE_S`) and tells the agent to call `end_session` (docs/BENCHMARK-BIAS.md).

Put the agent in the same image (or one like it) and point its MCP config at `http://172.30.99.1:8770/mcp`
(streamable HTTP). The model API is outside the internal network: give the container a second network to an
egress proxy that allows only the API host (not part of this recipe). Without it the client can talk to nothing but
the server, which is the point.

## What the network does not stop

`--internal` drops traffic to the outside world, but the host itself is a peer at the gateway address: every host
service listening on all interfaces (`0.0.0.0`, `::`) is reachable from the container. On the dev machine that is
sshd (22), Steam (27036) and KDE Connect (1716); run.sh prints a warning listing them. The game's debug port
(127.0.0.1:9998) is loopback-only and is not reachable. To close the rest, as root:

```sh
BR=br-$(docker network inspect -f '{{.Id}}' bg3ai-sandbox | cut -c1-12)
iptables -I INPUT -i "$BR" -p tcp ! --dport 8770 -j REJECT
iptables -I INPUT -i "$BR" -p udp -j REJECT
```

(Not tested here: this machine has no root.) Or bind those services to specific addresses.
