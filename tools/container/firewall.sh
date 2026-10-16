#!/bin/sh
# Close the host to the sandbox network except for the bg3 MCP server (README.md, "Close the host").
#
#   sudo tools/container/firewall.sh up       # apply (or replace) the rules; idempotent
#   sudo tools/container/firewall.sh down     # remove them; idempotent
#   tools/container/firewall.sh print         # print the rule set `up` would load (no root)
#   tools/container/firewall.sh check         # from a client container: which host ports it reaches (no root)
#
# One nftables table, `inet bg3ai_sandbox`, separate from Docker's own rules. Traffic from the sandbox bridge (or
# from the sandbox subnet on any interface but lo, in case the network was recreated under a new bridge name) to the
# host is rejected unless it is tcp to $GW:$PORT or part of a connection the host opened: IPv4 and IPv6, tcp, udp,
# icmp, everything. In nftables a reject in any table is final, so the other tables' accepts don't reopen it.
#
# Env as run.sh: BG3AI_SANDBOX_NET (bg3ai-sandbox), BG3AI_SANDBOX_SUBNET (172.30.99.0/24), BG3AI_SANDBOX_GW
# (172.30.99.1), BG3AI_MCP_PORT (8770); BG3AI_SANDBOX_BRIDGE and BG3AI_SANDBOX_SUBNET6 override what is read from
# `docker network inspect` (the test sets them; it has no docker). Not persistent: run `up` again after a reboot.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
NET=${BG3AI_SANDBOX_NET:-bg3ai-sandbox}
SUBNET=${BG3AI_SANDBOX_SUBNET:-172.30.99.0/24}
GW=${BG3AI_SANDBOX_GW:-172.30.99.1}
PORT=${BG3AI_MCP_PORT:-8770}
IMAGE=bg3ai/mcp-client:latest
TABLE=bg3ai_sandbox

inspect() { docker network inspect -f "$1" "$NET" 2>/dev/null; }

# The bridge is br-<first 12 of the network id> unless the network names its own (com.docker.network.bridge.name).
bridge() {
    if [ -n "${BG3AI_SANDBOX_BRIDGE:-}" ]; then echo "$BG3AI_SANDBOX_BRIDGE"; return; fi
    id=$(inspect '{{.Id}}') || { echo "network $NET not found (run.sh creates it)" >&2; exit 1; }
    name=$(inspect '{{index .Options "com.docker.network.bridge.name"}}')
    if [ -n "$name" ] && [ "$name" != "<no value>" ]; then echo "$name"; else echo "br-$(echo "$id" | cut -c1-12)"; fi
}

# The network's IPv6 subnet, if it has one (run.sh's network has none: Docker sets disable_ipv6 in the container).
subnet6() {
    if [ -n "${BG3AI_SANDBOX_SUBNET6+x}" ]; then echo "$BG3AI_SANDBOX_SUBNET6"; return; fi
    [ "$(inspect '{{.EnableIPv6}}')" = true ] || return 0
    inspect '{{range .IPAM.Config}}{{.Subnet}} {{end}}' | tr ' ' '\n' | grep : | head -n1
}

ruleset() {
    br=$(bridge)
    s6=$(subnet6)
    # `table` + `delete table` first: the load replaces the table atomically whether or not it existed.
    echo "table inet $TABLE"
    echo "delete table inet $TABLE"
    echo "table inet $TABLE {"
    echo "    chain input {"
    echo "        type filter hook input priority filter - 10; policy accept;"
    echo "        iifname \"$br\" jump from_sandbox"
    echo "        iifname != \"lo\" ip saddr $SUBNET jump from_sandbox"
    if [ -n "$s6" ]; then echo "        iifname != \"lo\" ip6 saddr $s6 jump from_sandbox"; fi
    echo "    }"
    echo "    chain from_sandbox {"
    echo "        ct state established,related accept"
    echo "        ip daddr $GW tcp dport $PORT ct state new accept"
    echo "        meta l4proto tcp reject with tcp reset"
    echo "        reject"
    echo "    }"
    echo "}"
}

# From a container on the sandbox network, try every host tcp/udp port listening on all interfaces, plus $PORT.
# tcp: connected = reachable. udp (listening ports only): refused = blocked, no answer = the datagram got through.
check() {
    tcp=$( (ss -ltnH 2>/dev/null || true) | awk '{print $4}' | grep -E '^(0\.0\.0\.0|\*|\[::\]):' | sed 's/.*://' \
          | sort -un | grep -vx "$PORT" | tr '\n' ' ' || true)
    udp=$( (ss -lunH 2>/dev/null || true) | awk '{print $4}' | grep -E '^(0\.0\.0\.0|\*|\[::\]):' | sed 's/.*://' \
          | sort -un | tr '\n' ' ' || true)
    docker run --rm --network "$NET" --cap-drop ALL --security-opt no-new-privileges --read-only \
        --entrypoint python3 "$IMAGE" -c '
import socket, sys
gw, port, tcp, udp = sys.argv[1], int(sys.argv[2]), sys.argv[3].split(), sys.argv[4].split()
leaks = 0
def tcp_open(p):
    try:
        socket.create_connection((gw, p), timeout=2).close()
        return True
    except OSError:
        return False
print(f"tcp {port} (bg3 server):", "reachable" if tcp_open(port) else "not reachable (is the server up?)")
for p in map(int, tcp):
    state = "REACHABLE" if tcp_open(p) else "blocked"
    leaks += state != "blocked"
    print(f"tcp {p}:", state)
for p in map(int, udp):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    try:
        s.connect((gw, p)); s.send(b"x"); s.recv(1); state = "REACHABLE"
    except ConnectionRefusedError:
        state = "blocked"
    except OSError:
        state = "REACHABLE (no answer, no reject)"
    leaks += state != "blocked"
    print(f"udp {p}:", state)
print("OK: only the bg3 server is reachable" if not leaks else f"{leaks} host port(s) reachable: sudo {sys.argv[5]} up")
sys.exit(1 if leaks else 0)
' "$GW" "$PORT" "$tcp" "$udp" "$HERE/firewall.sh"
}

case "${1:-}" in
    up)
        ruleset | nft -f -
        echo "nft table inet $TABLE loaded: from $(bridge) / $SUBNET only tcp $GW:$PORT reaches the host"
        ;;
    down)
        printf 'table inet %s\ndelete table inet %s\n' "$TABLE" "$TABLE" | nft -f -
        echo "nft table inet $TABLE removed"
        ;;
    print) ruleset ;;
    check) check ;;
    *) echo "usage: $0 up|down|print|check" >&2; exit 2 ;;
esac
