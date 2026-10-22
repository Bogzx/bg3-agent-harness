#!/usr/bin/env python3
"""Docker-only smoke test of `play.py run --sandbox container`'s pieces (bg3harness/sandbox.py), no game, no model spend.

    python3 tools/container/sandbox_smoke.py [--firewalled] [--client claude|pi]

Uses the real docker daemon: prepare() (network, both images, egress proxy; the firewall gate only reads nft, and
without --firewalled it runs as --sandbox-unfirewalled), the real bg3 MCP server over http on the gateway with a
throwaway memory dir (no game needed for initialize/tools/list), then the agent image as the runner starts it:
  1. node in the client container: MCP initialize at http://172.30.99.1:8770/mcp answers; the egress proxy lets
     CONNECT api.anthropic.com:443 through and refuses example.com:443 and api.anthropic.com:80; a direct
     connection to the internet fails (the network is --internal).
  2. the real `claude -p` (the runner's exact sandboxed command, no credentials in the container): its init event
     lists bg3 as connected (the http server through the container network); with no login the API call fails
     with an auth error through the proxy and costs nothing.
With --client pi, instead of 1's API CONNECT and 2 (no real network at all; nothing leaves the machine):
  1. the proxy runs with pi's egress (openrouter.ai, api.deepseek.com); it refuses example.com:443 and
     api.deepseek.com:80 (decided before any connection);
  3. the stub model (tools/pi/conformance/stub_llm.py) in a container on the sandbox network, and the image's pi
     (bg3harness/pi_client.py's command with the image's pi and bg3.ts) against it: init lists bg3 connected with every
     tool, the model's tool call reaches the bg3 server over http, pi ends with the stub's final text; the stub saw
     the system text verbatim (D2), only mcp__bg3__ tools, and the key from the env var the profile names (passed
     by name); pi's session file landed in its agent dir on the host.
Cleans up the containers, the proxy and the server. Prints PASS or FAIL lines; exit 1 on any FAIL.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "harness" / "src"))
from bg3harness import clients as C  # noqa: E402
from bg3harness import pi_client as PC  # noqa: E402
from bg3harness import profiles as PR  # noqa: E402
from bg3harness import sandbox as S  # noqa: E402

STUB_IP, STUB_PORT, STUB_NAME = "172.30.99.3", 8000, "bg3ai-stub-llm"
STUB_KEY_ENV = "BG3AI_SMOKE_STUB_KEY"
PI_PROFILE = {"client": "pi", "model": "stub-model",
              "endpoint": {"provider": "stub", "base_url": f"http://{STUB_IP}:{STUB_PORT}/v1",
                           "api_key_env": STUB_KEY_ENV},
              "model_info": {"input": ["text"], "reasoning": False, "context_window": 200000, "max_tokens": 8000}}

NODE_PROBE = r"""
const net = require('net');
const [url, phost, pport, allowed, refused80] = process.argv.slice(1);
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
  if (allowed) out.api443 = await connectVia(allowed, 443);
  out.other443 = await connectVia('example.com', 443);
  out.api80 = await connectVia(refused80, 80);
  out.direct = await direct('1.1.1.1', 443);
  console.log(JSON.stringify(out));
})();
"""


def claude_smoke(box: S.Sandbox, tmp: Path, check) -> None:
    """2. the real `claude -p` in the container, with no login."""
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


def pi_smoke(box: S.Sandbox, tmp: Path, check) -> None:
    """3. (--client pi): the image's pi against the stub model in a container on the sandbox network."""
    work = tmp / "stub"
    work.mkdir()
    shutil.copy(REPO / "tools/pi/conformance/stub_llm.py", work / "stub_llm.py")
    (work / "script.json").write_text(json.dumps([
        {"tool_calls": [{"name": "mcp__bg3__memory_list", "arguments": {"path": "knowledge"}}]},
        {"text": "SMOKE-DONE"}]))
    subprocess.run([S.docker_bin(), "rm", "-f", STUB_NAME], capture_output=True)
    subprocess.run([S.docker_bin(), "run", "-d", "--name", STUB_NAME, "--label", f"{S.LABEL}=play", "--network",
                    S.NET, "--ip", STUB_IP, "--cap-drop", "ALL", "--read-only", "--user",
                    f"{os.getuid()}:{os.getgid()}", "-v", f"{work}:/work", "-e", f"STUB_LLM_BIND=0.0.0.0:{STUB_PORT}",
                    "--entrypoint", "python3", S.TOOLS_IMAGE, "/work/stub_llm.py", "/work/script.json",
                    "/work/requests.jsonl", "/work/port"], check=True, capture_output=True, timeout=60)
    prof = PR.parse(PI_PROFILE, "smoke")
    system = PC.write_system_text(tmp / "system.txt")
    client = C.make_http("pi", S.URL, profile=prof, system_file=system, binary=S.PI_BIN, extension=S.PI_EXTENSION)
    req = C.Request("Look at your notes.", prof.model, tmp, session="00000000-0000-4000-8000-0000000000a1",
                    tag="smoke")
    os.environ[STUB_KEY_ENV] = "stub-key-by-name"
    no_proxy = f"{S.GW},{STUB_IP}"   # the stub is plain http on the sandbox network: not through the proxy
    env = {**client.env(req), "NO_PROXY": no_proxy, "no_proxy": no_proxy}
    name = box.container_name("smoke-pi")
    argv = box.run_argv(name, client.command(req), env, [*client.write_config(req), system],
                        writable=[client.agent_dir(req)])
    check("pi key passed by name, not value", STUB_KEY_ENV in argv and not any("stub-key-by-name" in a for a in argv))
    r = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    box.remove(name)
    events = [e for ln in r.stdout.splitlines() for e in client.parse(ln)]
    init = next((e for e in events if e.kind == "init"), None)
    check("pi in the container: init lists bg3 connected", bool(init and "bg3:connected" in (init.mcp or [])),
          str(init.mcp if init else (r.stdout + r.stderr)[-500:]))
    tools = [e for e in events if e.kind == "tool_result"]
    check("pi: the model's tool call reached the bg3 server", bool(tools) and not tools[0].is_error,
          (tools[0].text[:200] if tools else (r.stdout + r.stderr)[-300:]))
    end = next((e for e in events if e.kind == "result"), None)
    said = [e.text for e in events if e.kind == "text"]
    check("pi: ends with the stub's final text, success",
          bool(end and not end.is_error and said[-1:] == ["SMOKE-DONE"]),
          f"{end.subtype if end else (r.stdout + r.stderr)[-300:]}; said {said[-1:]}")
    reqs = [json.loads(x) for x in (work / "requests.jsonl").read_text().splitlines()] \
        if (work / "requests.jsonl").exists() else []
    first = reqs[0] if reqs else {}
    msgs = (first.get("body") or {}).get("messages") or [{}]
    check("stub: the system text verbatim (D2)", msgs[0].get("content") == system.read_text(),
          str(msgs[0].get("content"))[:120])
    names = [t["function"]["name"] for t in (first.get("body") or {}).get("tools") or []]
    check("stub: only mcp__bg3__ tools", bool(names) and all(n.startswith("mcp__bg3__") for n in names),
          f"{len(names)} tools")
    auth = {k.lower(): v for k, v in (first.get("headers") or {}).items()}.get("authorization", "")
    check("stub: the key from the profile's env var", auth == "Bearer stub-key-by-name", auth[:40])
    sessions = list((client.agent_dir(req) / "sessions").rglob("*.jsonl"))
    check("pi's session file in its agent dir on the host", bool(sessions), str(client.agent_dir(req)))


def main() -> int:
    firewalled = "--firewalled" in sys.argv
    client_name = sys.argv[sys.argv.index("--client") + 1] if "--client" in sys.argv else "claude"
    fails = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""), flush=True)
        if not ok:
            fails.append(name)

    tmp = Path(tempfile.mkdtemp(prefix="bg3ai-sandbox-smoke-"))
    box = srv = None
    try:
        box = S.prepare(client_name, tmp / "home", unfirewalled=not firewalled, log=print, pid_file=tmp / "srv.pid",
                        auth_env=(STUB_KEY_ENV,) if client_name == "pi" else None)
        check("prepare (network, images, proxy)", True, json.dumps(box.info))
        mem = tmp / "memory"
        mem.mkdir()
        env = {**os.environ, "BG3AI_MEMORY_DIR": str(mem), "BG3AI_AUTO_SESSION": "0", "BG3AI_MOVIE_SKIP": "0",
               "BG3AI_METRICS_SAMPLE_S": "0", "BG3AI_SERVE_RULES": "1"}
        srv = box.start_server(C.bg3_server(), env, C.PLAY_DIR, tmp / "server.log")
        check("bg3 server listening on the gateway", S.listening(), S.URL)

        name = box.container_name("smoke-node")
        allowed, refused80 = ("", "api.deepseek.com") if client_name == "pi" else ("api.anthropic.com",) * 2
        r = subprocess.run(box.run_argv(name, ["node", "-e", NODE_PROBE, S.URL, S.PROXY_IP, str(S.PROXY_PORT),
                                               allowed, refused80]), capture_output=True, text=True, timeout=120)
        box.remove(name)
        try:
            got = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            got = {}
            check("node probe ran", False, (r.stdout + r.stderr)[-500:])
        if got:
            check("MCP initialize from the client container", got.get("mcp") == "200 serverInfo", got.get("mcp"))
            if allowed:
                check(f"proxy: CONNECT {allowed}:443 allowed", got.get("api443") == "200", got.get("api443"))
            check("proxy: example.com:443 refused", got.get("other443") == "403", got.get("other443"))
            check(f"proxy: {refused80}:80 refused", got.get("api80") == "403", got.get("api80"))
            check("no direct route out (internal network)", got.get("direct") != "connected", got.get("direct"))

        if client_name == "pi":
            check("proxy: pi's egress", box.info["egress"] == ["openrouter.ai", "api.deepseek.com"],
                  str(box.info["egress"]))
        (pi_smoke if client_name == "pi" else claude_smoke)(box, tmp, check)
    except S.SandboxError as e:
        check("sandbox", False, str(e))
    finally:
        if srv is not None:
            srv.stop()
        subprocess.run([S.docker_bin(), "rm", "-f", STUB_NAME], capture_output=True)
        S.shutdown()
        subprocess.run(["rm", "-rf", str(tmp)])
    print("FAIL" if fails else "PASS", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
