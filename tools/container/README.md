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
`firewall.sh check` was run against a scratch internal network on the test machine without the rules: it reports tcp 22, 1716,
27036 and several udp ports reachable, as expected. **Not yet run:** `firewall.sh up` on a real host (no root here),
then `check` reporting OK. Alternatively, bind those host services to specific addresses.
