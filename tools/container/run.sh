#!/bin/sh
# Run an MCP client in a container whose network reaches only the bg3 MCP server (README.md).
#
#   tools/container/run.sh                 # build, start the server, run the smoke client, stop the server
#   tools/container/run.sh --no-server     # the server already runs on $GW:$PORT (e.g. under the runner)
#   tools/container/run.sh -- <cmd ...>    # run <cmd> in the client container instead of the smoke test
#
# Env: BG3AI_SANDBOX_NET (bg3ai-sandbox), BG3AI_SANDBOX_SUBNET (172.30.99.0/24), BG3AI_SANDBOX_GW (172.30.99.1),
# BG3AI_MCP_PORT (8770). The server gets this shell's env (BG3AI_MEMORY_DIR, BG3AI_SESSION_DEADLINE, ...).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
NET=${BG3AI_SANDBOX_NET:-bg3ai-sandbox}
SUBNET=${BG3AI_SANDBOX_SUBNET:-172.30.99.0/24}
GW=${BG3AI_SANDBOX_GW:-172.30.99.1}
PORT=${BG3AI_MCP_PORT:-8770}
IMAGE=bg3ai/mcp-client:latest
START_SERVER=1
if [ "${1:-}" = "--no-server" ]; then START_SERVER=0; shift; fi
if [ "${1:-}" = "--" ]; then shift; fi

# 1. An internal network: containers on it reach each other and the host's address on it, nothing else.
if ! docker network inspect "$NET" >/dev/null 2>&1; then
    docker network create --internal --subnet "$SUBNET" --gateway "$GW" "$NET" >/dev/null
    echo "network $NET ($SUBNET, internal) created"
fi

# 2. The client image.
docker build -q -t "$IMAGE" "$HERE" >/dev/null
echo "image $IMAGE built"

# 3. The bg3 MCP server on the host, bound to the network's gateway address only (not 0.0.0.0).
listening() { python3 -c "import socket,sys; socket.create_connection(('$GW', $PORT), 1).close()" 2>/dev/null; }
SERVER_PID=
if [ "$START_SERVER" = 1 ] && ! listening; then
    setsid uv run --project "$REPO/bridge" bg3ai-mcp --transport http --host "$GW" --port "$PORT" \
        > "${TMPDIR:-/tmp}/bg3ai-mcp-http.log" 2>&1 &
    SERVER_PID=$!   # its own process group: uv and the server stop together
    trap 'kill -- -$SERVER_PID 2>/dev/null || true' EXIT INT TERM
    i=0
    until listening; do
        i=$((i + 1))
        if [ $i -gt 60 ]; then echo "server did not start: ${TMPDIR:-/tmp}/bg3ai-mcp-http.log" >&2; exit 1; fi
        sleep 0.5
    done
    echo "server listening on http://$GW:$PORT/mcp (pid $SERVER_PID)"
fi

# The internal network still reaches every host port bound to all interfaces, at $GW (README.md: the firewall rule).
OPEN=$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E '^(0\.0\.0\.0|\*|\[::\]):' | sed 's/.*://' | sort -un \
       | grep -vx "$PORT" | tr '\n' ' ' || true)
if [ -n "$OPEN" ]; then
    echo "WARNING: host ports open on all interfaces, reachable from the sandbox at $GW: $OPEN(see README.md)"
fi

# 4. The client: that network only, no capabilities, read-only root, nothing of the host mounted.
if [ $# -gt 0 ]; then
    CMD=$1
    shift
    docker run --rm --network "$NET" --cap-drop ALL --security-opt no-new-privileges --read-only \
        --tmpfs /tmp --tmpfs /home/agent --entrypoint "$CMD" "$IMAGE" "$@"
else
    docker run --rm --network "$NET" --cap-drop ALL --security-opt no-new-privileges --read-only \
        --tmpfs /tmp --tmpfs /home/agent "$IMAGE" "http://$GW:$PORT/mcp"
fi
