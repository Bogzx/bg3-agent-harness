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

The client needs no files from the repo: the server serves the rules of play (assembled from packages/harness/prompts) in its MCP
`instructions`, at the top of the first `session_start` of each session, and as the `rules` prompt and the
`bg3://rules` resource. With `BG3AI_SESSION_DEADLINE` set, it also refuses game actions 5 minutes after the
deadline (`BG3AI_STOP_GRACE_S`) and tells the agent to call `end_session` (docs/BENCHMARK-BIAS.md).

The runner does all of this for you: next section.

## The runner: `play.py run --sandbox container`

```sh
sudo tools/container/firewall.sh up                       # once per boot (root); the runner refuses without it
export CLAUDE_CODE_OAUTH_TOKEN=...                        # or ANTHROPIC_API_KEY (codex: OPENAI_API_KEY, gemini:
                                                          # GEMINI_API_KEY, pi: the profile's api_key_env)
python3 runner/play.py run --playthrough box1 --sandbox container [--client codex] ...
```

```
host                                            internal network bg3ai-sandbox (172.30.99.0/24)
  runner ── per play process ──┐
  bg3ai-mcp --transport http   │   <── 8770 ──   bg3ai-play-<session>  (bg3ai/agent-client: claude/codex/gemini)
    --host 172.30.99.1, the    │                   --cap-drop ALL, no-new-privileges, read-only root, host uid,
    session's env, cwd play/   └── docker run ──>  HOME = runtime/sessions/<pt>/sandbox-home (rw), nothing else
  game, repo, saves, memory                          │ HTTPS_PROXY=http://172.30.99.2:3128
  internet <── default bridge ── bg3ai-egress (172.30.99.2): CONNECT <the client's API host>:443 only
```

What `runner/sandbox.py` does:
- **Once per runner start** (`prepare`): `docker version` answers; the network exists (created like `run.sh`); both
  images are built (`Dockerfile` = smoke client, firewall probe and proxy; `Dockerfile.agent` = node 22 + the three
  CLIs, the play client's version pinned to the host's `<cli> --version`, + pi at `/opt/pi`: `npm ci` of
  `tools/pi`'s package-lock with `bg3.ts`, passed as the build context `pi`); the **firewall gate**; play/keeper
  containers a dead runner left are removed; the egress proxy is (re)started for the client's API hosts
  (`sandbox.EGRESS`; override with `BG3AI_SANDBOX_EGRESS=host1,host2`).
- **Firewall gate**: `nft list table inet bg3ai_sandbox` (or `sudo -n nft list ...`; read-only) must show the table
  with the accept rule for `172.30.99.1:8770`: missing or stale → refused with the `sudo ... firewall.sh up` hint.
  Then `firewall.sh check` from a container must print OK (nothing but the server reachable). Where nft can't be
  read without root and `sudo -n` isn't allowed, the probe alone decides. `--sandbox-unfirewalled` skips the gate
  (dev only): the run record gets `sandbox_unfirewalled: true`.
- **Per play process** (every session, resume and fix-notes pass): the bg3 server starts on the host over http with
  exactly the env a stdio server would get (the runner's env, `play/.mcp.json`'s, the session's: deadline, save
  identity, metrics path, `BG3AI_SESSION`...) plus `BG3AI_SERVE_RULES=1` (the container has no `play/CLAUDE.md`),
  log `runtime/sessions/<pt>/<sid>.mcp-http.log`, pid in `runtime/sandbox-server.pid` (a dead runner's stale server
  is stopped first). Then `docker run --rm` of the client's usual command (`clients.make_http`: the bg3 server is its
  only MCP server, at the http URL; claude also gets play/.claude/settings.json as `--settings`). The client's stdout
  is the stream the runner already parses: log, latency, stop check, caps, RUN-7/RUN-8 all work unchanged. Signals:
  the docker CLI forwards SIGINT/SIGTERM to the container (tini); after the process ends the container is
  `docker rm -f`'d (a SIGKILL to the CLI doesn't reach it) and the server stopped. A server that can't start (port
  taken, crash) is RUN-8's `mcp_failed` setup failure.
- **Memory keeper**: the same, in a `keeper` container against the keeper-mode server over http (always the MCP
  pass: no file tools in the container, also for claude).
- **Records**: runs.jsonl gets `sandbox: "container"` (+ `sandbox_unfirewalled`).
- **Credentials**: passed by name (`docker run -e NAME`: never in argv or logs) when set in the runner's env:
  claude `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`), codex `OPENAI_API_KEY` /
  `CODEX_API_KEY`, gemini `GEMINI_API_KEY` / `GOOGLE_API_KEY`, pi the variable its profile names
  (`[endpoint] api_key_env`, `prepare(..., auth_env=...)`). A login kept in the sandbox home works too. The
  host's own `~/.claude` / `~/.codex` are never mounted.
- **pi**: egress `openrouter.ai` and `api.deepseek.com` (the profiles' endpoints). The runner builds the client with
  `clients.make_http("pi", url, profile=..., system_file=..., binary=sandbox.PI_BIN, extension=sandbox.PI_EXTENSION)`
  and runs it with `run_argv(..., files=[*written, system_file], writable=[client.agent_dir(req)])`: the system
  text read-only, pi's agent dir (its config and session files) read-write at the same path. pi's HTTP client
  honours `HTTPS_PROXY` / `NO_PROXY` (undici's EnvHttpProxyAgent), so the bg3 server is reached directly and the
  model API through the proxy.
- At the runner's end the proxy and any container left are removed.

What it doesn't cover: DNS from the client container goes to Docker's embedded resolver (on an `--internal` network
it doesn't forward outside: the clients need none, the proxy resolves); the proxy's allowlist is by host name, so the
client can still send anything to its own model API (it can anyway). The model API is the only way out.

**Tested.** Offline: `runner/test_sandbox.py` (fake docker, fake nft/sudo, throwaway server processes; the whole
run_claude path through a fake `docker run`; the keeper path). Docker-only smoke without a game or model spend:
`python3 tools/container/sandbox_smoke.py [--client pi]` (prepare with the real daemon, the real bg3 server over http, node in the
agent image: MCP initialize, the proxy allows only the API host on 443, no direct route; the real `claude -p` in the
container sees `bg3: connected` and, with no login, fails auth through the proxy; with `--client pi`, no outside
network at all: the image's pi against the stub model in a container on the sandbox network plays one tool call
through the http server, the stub sees the system text verbatim, only `mcp__bg3__` tools and the key passed by its
env var name; passed 2026-10-21, docker 29.7.2). **Not yet run:** a real session
(game + a logged-in client) and the gate with the firewall loaded (`--firewalled` on the smoke).

## What the network does not stop, and the firewall that does

`--internal` drops traffic to the outside world, but the host itself is a peer at the gateway address: every host
service listening on all interfaces (`0.0.0.0`, `::`) is reachable from the container, at the gateway or at any
other host address. On the dev machines that is sshd (22), Steam (27036, udp too), KDE Connect (1716) and mDNS/other
udp ports. The game's debug port (127.0.0.1:9998) is loopback-only and is not reachable. `run.sh` runs
`firewall.sh check`, which tries each of those ports from a client container and lists what gets through.

`firewall.sh` closes them. It loads one nftables table, `inet bg3ai_sandbox`, whose input chain takes everything
arriving on the sandbox bridge (and, should the network be recreated under a new bridge name, anything from its
subnet on any interface but `lo`) and accepts only tcp to `172.30.99.1:8770` and packets of connections the host
opened; the rest is rejected (tcp reset, icmp port-unreachable otherwise): other tcp ports, udp, icmp, IPv4 and
IPv6, and every other host address. The one step that needs root, for you to run on the host after the network
exists (`run.sh` creates it) and again after each reboot:

```sh
sudo tools/container/firewall.sh up      # load (or atomically replace) the table; idempotent
tools/container/firewall.sh check        # no root: from a client container, expect "OK: only the bg3 server is reachable"
sudo tools/container/firewall.sh down    # remove it again; idempotent
tools/container/firewall.sh print        # no root: the exact rule set `up` loads
```

Why nft and its own table, not `iptables -I INPUT`: Debian 13 uses nftables (`iptables` there is iptables-nft, a
front end onto the same kernel tables). A table of our own covers IPv4 and IPv6 in one `inet` family (iptables needs
ip6tables too), is replaced and removed atomically as a whole, and never touches the chains Docker manages and
rewrites on restart. A reject in any nftables table is final, so Docker's or anyone else's accepts don't reopen it.

IPv6: `run.sh`'s network has none (`EnableIPv6` false); Docker sets `disable_ipv6` on the container's interface, so
the container has no IPv6 at all, though the host bridge carries a link-local address. The rules cover IPv6 anyway
(by bridge name; `firewall.sh` also adds the network's IPv6 subnet if it has one). Forwarding to other networks is
Docker's own `--internal` rule, not this table.

**Verified** (2026-10-16, docker 29.7.2, Debian 13 nft in `debian:trixie-slim`) by `tools/container/test_firewall.sh`,
which needs no root and touches nothing on the host: two `--network none --cap-add NET_ADMIN` containers stand in
for the host (a bridge at 172.30.99.1 and fd30:99::1, a dummy "LAN" address, listeners on tcp 8770, tcp 22, udp
5353) and for the client (a veth end on that bridge); `firewall.sh up` runs in the host one. It checks that before
the rules everything connects, and with them only tcp 172.30.99.1:8770 does: tcp 22, udp, ping, IPv6, the LAN
address and port 8770 on other addresses are refused; connections the host opens to the client still work, as does
the host's own traffic to its gateway address; after renaming the bridge the subnet rule still holds; `up` twice
leaves one identical table; `down` (twice) removes it and everything connects again. It prints PASS.
`firewall.sh check` was run against a scratch internal network on the test box without the rules: it reports tcp 22, 1716,
27036 and several udp ports reachable, as expected. **Not yet run:** `firewall.sh up` on a real host (no root here),
then `check` reporting OK. Alternatively, bind those host services to specific addresses.
