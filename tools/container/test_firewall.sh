#!/bin/sh
# Test firewall.sh in Docker only (the host firewall is never touched; no root, no --privileged).
#
#   tools/container/test_firewall.sh        # prints one line per check and PASS or FAIL; exit 0 on PASS
#
# Two containers with --network none and --cap-add NET_ADMIN, each its own network namespace:
#   "host"   stands in for the machine: a bridge br-0123456789ab at 172.30.99.1/24 + fd30:99::1/64 (the sandbox
#            network's gateway), a dummy "LAN" address 10.99.0.1, listeners on tcp 8770 / tcp 22 / udp 5353 on all
#            interfaces, and firewall.sh up/down run inside it.
#   "client" stands in for the sandbox container: the other end of a veth pair on that bridge, 172.30.99.2.
# The host container shares the client's pid namespace only to move the veth end into the client's netns.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
IMAGE=bg3ai/fw-test:latest
TAG=bg3ai-fwtest-$$
C=$TAG-client
H=$TAG-host
BR=br-0123456789ab

docker build -q -t "$IMAGE" - >/dev/null <<'EOF'
FROM debian:trixie-slim
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends nftables iproute2 iputils-ping python3 \
    >/dev/null && rm -rf /var/lib/apt/lists/*
EOF

cleanup() { docker rm -f "$C" "$H" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
docker run -d --name "$C" --network none --cap-add NET_ADMIN "$IMAGE" sleep infinity >/dev/null
docker run -d --name "$H" --network none --cap-add NET_ADMIN --pid "container:$C" \
    -v "$HERE/firewall.sh:/fw/firewall.sh:ro" "$IMAGE" sleep infinity >/dev/null
hx() { docker exec "$H" "$@"; }
cx() { docker exec "$C" "$@"; }

# The wiring. The client's sleep is pid 1 in the shared pid namespace.
hx sh -ec "
    ip link add $BR type bridge
    ip link add vh type veth peer name vc
    ip link set vh master $BR
    ip addr add 172.30.99.1/24 dev $BR
    ip addr add fd30:99::1/64 dev $BR nodad
    ip link add lan type dummy
    ip addr add 10.99.0.1/24 dev lan
    ip link set lan up; ip link set vh up; ip link set $BR up; ip link set lo up
    ip link set vc netns 1"
cx sh -ec "
    ip link set vc name eth0
    ip addr add 172.30.99.2/24 dev eth0
    ip addr add fd30:99::2/64 dev eth0 nodad
    ip link set eth0 up; ip link set lo up
    ip route add default via 172.30.99.1"

# Listeners: tcp echo on 8770 and 22, udp echo on 5353 (all dual-stack, all interfaces) on the host; tcp 9000 on
# the client, for a connection the host opens.
LISTEN='
import socket, sys, threading
def tcp(port):
    s = socket.create_server(("::", port), family=socket.AF_INET6, dualstack_ipv6=True)
    while True:
        c, _ = s.accept()
        c.sendall(c.recv(64)); c.close()
def udp(port):
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    s.bind(("::", port))
    while True:
        d, a = s.recvfrom(64); s.sendto(d, a)
for a in sys.argv[1:]:
    proto, port = a.split(":")
    threading.Thread(target=tcp if proto == "tcp" else udp, args=(int(port),), daemon=True).start()
threading.Event().wait()
'
docker exec -d "$H" python3 -c "$LISTEN" tcp:8770 tcp:22 udp:5353
docker exec -d "$C" python3 -c "$LISTEN" tcp:9000

# probe proto host port -> open (an echo came back) | refused | timeout | <error>
PROBE='
import socket, sys
proto, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
fam = socket.AF_INET6 if ":" in host else socket.AF_INET
s = socket.socket(fam, socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM)
s.settimeout(2)
try:
    s.connect((host, port)); s.send(b"ping")
    print("open" if s.recv(64) == b"ping" else "garbled")
except ConnectionRefusedError:
    print("refused")
except TimeoutError:
    print("timeout")
except OSError as e:
    print(type(e).__name__)
'
FAILS=0
expect() {   # expect <where: client|host> <want> <proto> <addr> <port> <what>
    got=$(docker exec "$([ "$1" = client ] && echo "$C" || echo "$H")" python3 -c "$PROBE" "$3" "$4" "$5")
    if [ "$got" = "$2" ]; then echo "ok    $6: $got"; else echo "FAIL  $6: $got, want $2"; FAILS=$((FAILS + 1)); fi
}
expect_ping() {   # expect_ping <ok|blocked> <addr> <what>
    if cx ping -c1 -W2 "$2" >/dev/null 2>&1; then got=ok; else got=blocked; fi
    if [ "$got" = "$1" ]; then echo "ok    $3: $got"; else echo "FAIL  $3: $got, want $1"; FAILS=$((FAILS + 1)); fi
}
fw() { hx env BG3AI_SANDBOX_BRIDGE=$BR BG3AI_SANDBOX_SUBNET6=fd30:99::/64 /fw/firewall.sh "$@"; }
sleep 1

echo "-- no firewall: the hole the rules close"
expect client open tcp 172.30.99.1 8770 "tcp gw:8770"
expect client open tcp 172.30.99.1 22   "tcp gw:22"
expect client open udp 172.30.99.1 5353 "udp gw:5353"
expect client open tcp fd30:99::1 22    "tcp [gw6]:22"
expect client open tcp 10.99.0.1 22     "tcp lan:22"
expect_ping ok 172.30.99.1              "icmp gw"

echo "-- firewall.sh up (twice: idempotent)"
fw up >/dev/null
first=$(hx nft list table inet bg3ai_sandbox)
fw up
if [ "$(hx nft list table inet bg3ai_sandbox)" = "$first" ] && [ "$(hx nft list tables | grep -c bg3ai_sandbox)" = 1 ]
then echo "ok    second up: same single table"; else echo "FAIL  second up changed the rule set"; FAILS=$((FAILS + 1)); fi
expect client open    tcp 172.30.99.1 8770 "tcp gw:8770 (the bg3 server)"
expect client refused tcp 172.30.99.1 22   "tcp gw:22"
expect client refused tcp 172.30.99.1 9    "tcp gw:9 (closed anyway)"
expect client refused udp 172.30.99.1 5353 "udp gw:5353"
expect client refused tcp 10.99.0.1 22     "tcp lan:22 (another host address)"
expect client refused tcp 10.99.0.1 8770   "tcp lan:8770 (server port, wrong address)"
expect client refused tcp fd30:99::1 22    "tcp [gw6]:22"
expect client refused tcp fd30:99::1 8770  "tcp [gw6]:8770"
expect client refused udp fd30:99::1 5353  "udp [gw6]:5353"
expect_ping blocked 172.30.99.1            "icmp gw"
expect_ping blocked fd30:99::1             "icmp6 gw6"
expect host open tcp 172.30.99.2 9000      "host -> client tcp 9000 (replies come back in)"
expect host open tcp 172.30.99.1 22        "host -> its own gw:22 (lo is untouched)"

echo "-- the network recreated under another bridge name: the subnet rule still holds"
hx sh -ec "ip link set $BR down; ip link set $BR name br-fedcba987654; ip link set br-fedcba987654 up"
sleep 1
expect client open    tcp 172.30.99.1 8770 "tcp gw:8770"
expect client refused tcp 172.30.99.1 22   "tcp gw:22"
expect client refused udp 172.30.99.1 5353 "udp gw:5353"
hx sh -ec "ip link set br-fedcba987654 down; ip link set br-fedcba987654 name $BR; ip link set $BR up"
sleep 1

echo "-- firewall.sh down (twice: idempotent)"
fw down >/dev/null
fw down
if hx nft list tables | grep -q bg3ai_sandbox; then echo "FAIL  table still there"; FAILS=$((FAILS + 1))
else echo "ok    table gone"; fi
expect client open tcp 172.30.99.1 22   "tcp gw:22 open again"
expect client open udp 172.30.99.1 5353 "udp gw:5353 open again"

if [ "$FAILS" = 0 ]; then echo PASS; else echo "FAIL ($FAILS checks)"; exit 1; fi
