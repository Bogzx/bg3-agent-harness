#!/usr/bin/env python3
"""Docker-only smoke test of `play.py run --sandbox container`'s pieces (runner/sandbox.py), no game, no model spend.

    python3 tools/container/sandbox_smoke.py [--firewalled]

Uses the real docker daemon: prepare() (network, both images, egress proxy; the firewall gate only reads nft, and
without --firewalled it runs as --sandbox-unfirewalled), the real bg3 MCP server over http on the gateway with a
throwaway memory dir (no game needed for initialize/tools/list), then the agent image as the runner starts it:
  1. node in the client container: MCP initialize at http://172.30.99.1:8770/mcp answers; the egress proxy lets
     CONNECT api.anthropic.com:443 through and refuses example.com:443 and api.anthropic.com:80; a direct
     connection to the internet fails (the network is --internal).
  2. the real `claude -p` (the runner's exact sandboxed command, no credentials in the container): its init event
     lists bg3 as connected (the http server through the container network); with no login the API call fails
     with an auth error through the proxy and costs nothing.
Cleans up the containers, the proxy and the server. Prints PASS or FAIL lines; exit 1 on any FAIL.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "runner"))
import clients as C  # noqa: E402
import sandbox as S  # noqa: E402

NODE_PROBE = r"""
const net = require('net');
const [url, phost, pport] = process.argv.slice(1);
function connectVia(host, port) {
  return new Promise(res => {
    const s = net.connect(+pport, phost, () => s.write(`CONNECT ${host}:${port} HTTP/1.1\r\nHost: ${host}:${port}\r\n\r\n`));
    s.setTimeout(20000, () => { s.destroy(); res('timeout'); });
    s.once('data', d => { res(d.toString().split(' ')[1]); s.destroy(); });
    s.on('error', e => res('error ' + e.code));
  });
}
function direct(host, port) {
  return new Promise(res => {
    const s = net.connect(port, host, () => { s.destroy(); res('connected'); });
    s.setTimeout(5000, () => { s.destroy(); res('timeout'); });
    s.on('error', e => res('error ' + e.code));
  });
}
(async () => {
  const out = {};
  try {
    const r = await fetch(url, {method: 'POST', headers: {'content-type': 'application/json',
      accept: 'application/json, text/event-stream'}, body: JSON.stringify({jsonrpc: '2.0', id: 1,
      method: 'initialize', params: {protocolVersion: '2025-06-18', capabilities: {},
      clientInfo: {name: 'sandbox-smoke', version: '0'}}})});
    const t = await r.text();
    out.mcp = r.status + (t.includes('serverInfo') ? ' serverInfo' : ' ' + t.slice(0, 200));
  } catch (e) { out.mcp = 'error ' + e; }
  out.api443 = await connectVia('api.anthropic.com', 443);
  out.other443 = await connectVia('example.com', 443);
  out.api80 = await connectVia('api.anthropic.com', 80);
  out.direct = await direct('1.1.1.1', 443);
  console.log(JSON.stringify(out));
})();
"""


def main() -> int:
    firewalled = "--firewalled" in sys.argv
    fails = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""), flush=True)
        if not ok:
            fails.append(name)

    tmp = Path(tempfile.mkdtemp(prefix="bg3ai-sandbox-smoke-"))
    box = srv = None
    try:
        box = S.prepare("claude", tmp / "home", unfirewalled=not firewalled, log=print, pid_file=tmp / "srv.pid")
        check("prepare (network, images, proxy)", True, json.dumps(box.info))
        mem = tmp / "memory"
        mem.mkdir()
        env = {**os.environ, "BG3AI_MEMORY_DIR": str(mem), "BG3AI_AUTO_SESSION": "0", "BG3AI_MOVIE_SKIP": "0",
               "BG3AI_METRICS_SAMPLE_S": "0", "BG3AI_SERVE_RULES": "1"}
        srv = box.start_server(C.bg3_server(), env, C.PLAY_DIR, tmp / "server.log")
        check("bg3 server listening on the gateway", S.listening(), S.URL)

        name = box.container_name("smoke-node")
        r = subprocess.run(box.run_argv(name, ["node", "-e", NODE_PROBE, S.URL, S.PROXY_IP, str(S.PROXY_PORT)]),
                           capture_output=True, text=True, timeout=120)
        box.remove(name)
        try:
            got = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            got = {}
            check("node probe ran", False, (r.stdout + r.stderr)[-500:])
        if got:
            check("MCP initialize from the client container", got.get("mcp") == "200 serverInfo", got.get("mcp"))
            check("proxy: CONNECT api.anthropic.com:443 allowed", got.get("api443") == "200", got.get("api443"))
            check("proxy: example.com:443 refused", got.get("other443") == "403", got.get("other443"))
            check("proxy: api.anthropic.com:80 refused", got.get("api80") == "403", got.get("api80"))
            check("no direct route out (internal network)", got.get("direct") != "connected", got.get("direct"))

        client = C.make_http("claude", S.URL)
        req = C.Request("Say hi.", "haiku", tmp, session="00000000-0000-4000-8000-000000000001", tag="smoke")
        name = box.container_name("smoke-claude")
        for k in S.AUTH_ENV["claude"]:   # no credentials in the container (run_argv passes only what is set)
            os.environ.pop(k, None)
        argv = box.run_argv(name, client.command(req), client.env(req), client.write_config(req))
        r = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        box.remove(name)
        events = [e for ln in r.stdout.splitlines() for e in client.parse(ln)]
        init = next((e for e in events if e.kind == "init"), None)
        check("claude in the container: init lists bg3 connected", bool(init and "bg3:connected" in (init.mcp or [])),
              str(init.mcp if init else (r.stdout + r.stderr)[-500:]))
        end = next((e for e in events if e.kind == "result"), None)
        check("claude without a login: an auth error, no spend", bool(end and end.is_error and not end.cost_usd),
              (end.text[:200] if end else (r.stdout + r.stderr)[-300:]))
        logs = subprocess.run([S.docker_bin(), "logs", S.PROXY_NAME], capture_output=True, text=True).stdout
        allowed = [ln for ln in logs.splitlines() if "-> api.anthropic.com:443" in ln]
        check("proxy log: the API connection went through it", bool(allowed), allowed[-1] if allowed else logs[-300:])
    except S.SandboxError as e:
        check("sandbox", False, str(e))
    finally:
        if srv is not None:
            srv.stop()
        S.shutdown()
        subprocess.run(["rm", "-rf", str(tmp)])
    print("FAIL" if fails else "PASS", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
