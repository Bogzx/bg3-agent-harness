#!/usr/bin/env python3
"""Unit tests for run --sandbox container (bg3harness/sandbox.py and its wiring in clients.py, play.py and
memory_keeper.py). Offline: a fake `docker` script ($BG3AI_DOCKER), fake nft/sudo answers, fake or throwaway servers.
Never touches the real docker daemon, nft or the game.

    python3 -m unittest runner/test_sandbox.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testenv  # noqa: F401  (first: the game's profile folders are a temp dir before play)
os.environ.setdefault("BG3AI_RUNTIME", tempfile.mkdtemp(prefix="bg3ai-test-runtime-"))
os.environ.setdefault("BG3AI_KNOWLEDGE_LOCK", "0")
from bg3harness import clients as C  # noqa: E402
import play as P  # noqa: E402
from bg3harness.runner import agent_side as ragent
from bg3harness.runner import codewatch as rcode
from bg3harness.runner import env as renv
from bg3harness.runner import policy as rpolicy
from bg3harness.runner import state as rstate
import legacy_cli as lcli
import legacy_playthrough as lpt
import legacy_series as lser
from bg3harness import profiles as PR  # noqa: E402
from bg3harness import sandbox as S  # noqa: E402

PI_PROFILE = PR.parse({"client": "pi", "model": "deepseek-flash",
                       "endpoint": {"provider": "deepseek", "base_url": "https://api.deepseek.com",
                                    "api_key_env": "DEEPSEEK_API_KEY"},
                       "model_info": {"input": ["text"], "reasoning": True, "context_window": 1000000,
                                      "max_tokens": 32000,
                                      "cost": {"input": 1.0, "output": 4.0, "cache_read": 0.1}}}, "test")

CLAUDE_STREAM = [
    {"type": "system", "subtype": "init", "session_id": "c-1", "model": "opus", "tools": ["x"],
     "mcp_servers": [{"name": "bg3", "status": "connected"}]},
    {"type": "assistant", "session_id": "c-1", "message": {"id": "m1", "content": [
        {"type": "text", "text": "Looking around."},
        {"type": "tool_use", "id": "t1", "name": "mcp__bg3__session_start", "input": {}}]}},
    {"type": "user", "session_id": "c-1", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "ok"}]}]}},
    {"type": "result", "subtype": "success", "session_id": "c-1", "is_error": False, "num_turns": 1,
     "duration_ms": 1000, "total_cost_usd": 0.01, "result": "done", "usage": {}},
]


def fake_docker(d: Path, stream: list[dict] | None = None, fail: tuple[str, ...] = ()) -> Path:
    """A `docker` that logs each call (one JSON argv per line in calls.jsonl) and, for `run` without -d, prints
    `stream` like the client in the container would. A first argument in `fail` exits 1."""
    calls, out = d / "calls.jsonl", d / "stream.jsonl"
    out.write_text("".join(json.dumps(e) + "\n" for e in stream or []))
    script = d / "docker"
    script.write_text(f"""#!{sys.executable}
import json, sys
argv = sys.argv[1:]
with open({str(calls)!r}, "a") as f:
    f.write(json.dumps(argv) + "\\n")
if argv and argv[0] in {list(fail)!r}:
    sys.stderr.write("boom\\n"); sys.exit(1)
if argv[:1] == ["network"] and argv[1:2] == ["inspect"]:
    sys.exit(1)   # no network yet: prepare creates it
if argv[:1] == ["run"] and "-d" not in argv:
    sys.stdout.write(open({str(out)!r}).read())
    sys.exit(0)
if argv[:1] == ["ps"]:
    print("abc123")
""")
    script.chmod(0o755)
    return script


def calls(d: Path) -> list[list[str]]:
    p = d / "calls.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def cp(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, err)


class FakeServer:
    def __init__(self):
        self.stopped = False

    def stop(self, grace: float = 10.0):
        self.stopped = True


class Firewall(unittest.TestCase):
    RULE = f"table inet bg3ai_sandbox {{ chain from_sandbox {{ ip daddr {S.GW} tcp dport {S.PORT} ct state new accept"

    def runner(self, answers: dict):
        seen = []

        def run(argv, timeout=60, check=False):
            seen.append(argv)
            a = answers.get(argv[0])
            if isinstance(a, Exception):
                raise a
            return a if a is not None else cp(1, "", "unexpected")
        run.seen = seen
        return run

    def test_table_states(self):
        loaded = self.runner({"nft": cp(0, self.RULE)})
        self.assertEqual(S.table_state(loaded), "loaded")
        self.assertEqual(S.table_state(self.runner({"nft": cp(0, "table inet bg3ai_sandbox { ip daddr 1.2.3.4 }")})),
                         "stale")
        self.assertEqual(S.table_state(self.runner({"nft": cp(1, "", "Error: No such file or directory; did you "
                                                                     "mean table 'filter' in family inet?")})),
                         "missing")
        # no root: nft refuses, sudo -n wants a password → unknown (the probe decides)
        nope = self.runner({"nft": cp(1, "", "netlink: Error: Operation not permitted"),
                            "sudo": cp(1, "", "sudo: a password is required")})
        self.assertEqual(S.table_state(nope), "unknown")
        self.assertEqual(nope.seen[1][:3], ["sudo", "-n", "nft"])
        self.assertIn("list", nope.seen[1])   # read-only, never `up`
        via_sudo = self.runner({"nft": S.SandboxError("nft not found"), "sudo": cp(0, self.RULE)})
        self.assertEqual(S.table_state(via_sudo), "loaded")

    def test_gate_refuses_without_the_table(self):
        run = self.runner({"nft": cp(1, "", "Error: No such file or directory")})
        with self.assertRaises(S.SandboxError) as e:
            S.firewall_gate(False, lambda m: None, run)
        self.assertIn("firewall.sh up", str(e.exception))
        self.assertIn("--sandbox-unfirewalled", str(e.exception))
        self.assertFalse(any(str(S.FIREWALL) in a for a in run.seen))   # no probe needed to refuse

    def test_gate_refuses_a_leaking_probe(self):
        run = self.runner({"nft": cp(0, self.RULE), str(S.FIREWALL): cp(1, "tcp 22: REACHABLE\n1 host port(s)")})
        with self.assertRaises(S.SandboxError) as e:
            S.firewall_gate(False, lambda m: None, run)
        self.assertIn("tcp 22: REACHABLE", str(e.exception))

    def test_gate_passes(self):
        run = self.runner({"nft": cp(0, self.RULE), str(S.FIREWALL): cp(0, "OK: only the bg3 server is reachable")})
        self.assertEqual(S.firewall_gate(False, lambda m: None, run),
                         {"table": "loaded", "probe_ok": True, "unfirewalled": False})
        self.assertEqual(run.seen[-1], [str(S.FIREWALL), "check"])
        logs = []
        run = self.runner({"nft": cp(1, "", "Operation not permitted"), "sudo": cp(1, "", "password"),
                           str(S.FIREWALL): cp(0, "OK")})
        self.assertEqual(S.firewall_gate(False, logs.append, run)["table"], "unknown")
        self.assertIn("probe", logs[0])

    def test_unfirewalled_only_warns(self):
        logs = []
        run = self.runner({"nft": cp(1, "", "No such file or directory")})
        got = S.firewall_gate(True, logs.append, run)
        self.assertEqual(got, {"table": "missing", "probe_ok": None, "unfirewalled": True})
        self.assertIn("WARNING", logs[0])


class DockerSide(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="bg3ai-sandbox-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.docker = fake_docker(self.d)
        env = mock.patch.dict(os.environ, {"BG3AI_DOCKER": str(self.docker)})
        env.start()
        self.addCleanup(env.stop)

    def test_prepare_order(self):
        logs = []
        with mock.patch.object(S, "cli_version", lambda name: "2.1.301"):
            box = S.prepare("claude", None, False, logs.append, gate=lambda u, log: {"table": "loaded"})
        argvs = calls(self.d)
        verbs = [a[0] if a[0] != "network" else " ".join(a[:2]) for a in argvs]
        self.assertEqual(verbs[:5], ["version", "network inspect", "network create", "build", "build"])
        create = argvs[2]
        self.assertIn("--internal", create)
        self.assertEqual(create[-1], S.NET)
        agent_build = argvs[4]
        self.assertIn("CLAUDE_VERSION=2.1.301", agent_build)
        self.assertIn(str(S.CONTAINER_DIR / "Dockerfile.agent"), agent_build)
        proxy = next(a for a in argvs if a[:2] == ["run", "-d"])
        self.assertEqual(proxy[proxy.index("--ip") + 1], S.PROXY_IP)
        self.assertEqual(proxy[proxy.index("--network") + 1], S.NET)
        self.assertEqual(proxy[proxy.index("--allow") + 1], "api.anthropic.com,console.anthropic.com")
        self.assertIn(["network", "connect", "bridge", S.PROXY_NAME], argvs)
        self.assertIn(["rm", "-f", "abc123"], argvs)   # a dead runner's play container
        self.assertEqual(box.info["egress"], ["api.anthropic.com", "console.anthropic.com"])
        self.assertEqual(box.record(), {"sandbox": "container"})

    def test_prepare_pi(self):
        with mock.patch.object(S, "cli_version", side_effect=AssertionError("pi has no host CLI")):
            box = S.prepare("pi", None, False, lambda m: None, gate=lambda u, log: {"table": "loaded"},
                            auth_env=(PI_PROFILE.endpoint.api_key_env,))
        argvs = calls(self.d)
        agent_build = [a for a in argvs if a[0] == "build"][1]
        self.assertEqual(agent_build[agent_build.index("--build-context") + 1], f"pi={S.PI_DIR}")
        self.assertFalse(any(a.startswith("--build-arg") for a in agent_build))
        proxy = next(a for a in argvs if a[:2] == ["run", "-d"])
        self.assertEqual(proxy[proxy.index("--allow") + 1], "openrouter.ai,api.deepseek.com")
        self.assertEqual(box.info["client_version"], "1.3.0")   # tools/pi/package.json's pin
        self.assertEqual(box.auth_env, ("DEEPSEEK_API_KEY",))

    def test_every_agent_build_has_the_pi_context(self):
        with mock.patch.object(S, "cli_version", lambda name: None):
            S.prepare("claude", None, False, lambda m: None, gate=lambda u, log: {})
        agent_build = [a for a in calls(self.d) if a[0] == "build"][1]
        self.assertIn(f"pi={S.PI_DIR}", agent_build)
        dockerfile = (S.CONTAINER_DIR / "Dockerfile.agent").read_text()
        self.assertIn("COPY --from=pi package.json package-lock.json bg3.ts /opt/pi/", dockerfile)
        self.assertIn("npm ci", dockerfile)
        self.assertEqual(S.PI_BIN, "/opt/pi/node_modules/.bin/pi")
        self.assertEqual(S.PI_EXTENSION, "/opt/pi/bg3.ts")

    def test_prepare_refuses(self):
        fake_docker(self.d, fail=("version",))
        with self.assertRaises(S.SandboxError) as e:
            S.prepare("claude", None, False, lambda m: None, gate=lambda u, log: {})
        self.assertIn("docker doesn't answer", str(e.exception))

        def closed(u, log):
            raise S.SandboxError("firewall is missing")
        fake_docker(self.d)
        with self.assertRaises(S.SandboxError):
            S.prepare("claude", None, False, lambda m: None, gate=closed)
        self.assertFalse(any(a[:2] == ["run", "-d"] for a in calls(self.d)))   # no proxy without the firewall

    def test_egress_override(self):
        with mock.patch.dict(os.environ, {"BG3AI_SANDBOX_EGRESS": "a.example, b.example"}):
            self.assertEqual(S.egress_hosts("claude"), ["a.example", "b.example"])
        self.assertEqual(S.egress_hosts("codex")[0], "api.openai.com")

    def test_run_argv(self):
        box = S.Sandbox("claude", self.d / "home")
        cfg = self.d / "S001.gemini-settings.json"
        cfg.write_text("{}")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-secret", "CLAUDE_CODE_OAUTH_TOKEN": ""}):
            argv = box.run_argv("bg3ai-play-x", ["claude", "-p", "go"], {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
                                [cfg])
        self.assertEqual(argv[:3], [str(self.docker), "run", "--rm"])
        self.assertEqual(argv[-4:], [S.CLIENT_IMAGE, "claude", "-p", "go"])
        for flag, val in (("--network", S.NET), ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"),
                          ("--label", "bg3ai.sandbox=play"), ("--name", "bg3ai-play-x")):
            self.assertEqual(argv[argv.index(flag) + 1], val)
        self.assertIn("--read-only", argv)
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        self.assertEqual(mounts, [f"{self.d / 'home'}:/home/agent", f"{cfg.resolve()}:{cfg.resolve()}:ro"])
        self.assertFalse(any(str(renv.REPO) in m for m in mounts))   # nothing of the repo, memory or saves
        envs = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        self.assertIn("ANTHROPIC_API_KEY", envs)                   # by name: the value is docker's to copy
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", envs)           # unset/empty: not passed
        self.assertFalse(any("sk-secret" in a for a in argv))
        self.assertIn(f"HTTPS_PROXY=http://{S.PROXY_IP}:{S.PROXY_PORT}", envs)
        self.assertIn(f"NO_PROXY={S.GW}", envs)
        self.assertIn("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", envs)
        self.assertIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY=1", envs)
        self.assertTrue((self.d / "home").is_dir())

    def test_run_argv_pi(self):
        box = S.Sandbox("pi", self.d / "home", auth_env=("DEEPSEEK_API_KEY",))
        agent = self.d / "S001.pi"
        agent.mkdir()
        cfg, system = agent / "bg3.json", self.d / "system.txt"
        cfg.write_text("{}")
        system.write_text("rules")
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-ds-secret", "OPENROUTER_API_KEY": "sk-or"}):
            argv = box.run_argv("bg3ai-play-x", [S.PI_BIN, "--mode", "json"], {"PI_OFFLINE": "1"}, [cfg, system],
                                writable=[agent])
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        self.assertEqual(mounts, [f"{self.d / 'home'}:/home/agent", f"{agent.resolve()}:{agent.resolve()}",
                                  f"{system.resolve()}:{system.resolve()}:ro"])   # cfg rides on the rw agent dir
        envs = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        self.assertIn("DEEPSEEK_API_KEY", envs)            # the profile's key, by name
        self.assertNotIn("OPENROUTER_API_KEY", envs)       # not this profile's
        self.assertFalse(any("sk-" in a for a in argv))
        self.assertIn("PI_OFFLINE=1", envs)
        self.assertEqual(argv[-3:], [S.PI_BIN, "--mode", "json"])

    def test_remove_and_shutdown(self):
        S.Sandbox.remove("bg3ai-play-x")
        S.shutdown()
        argvs = calls(self.d)
        self.assertEqual(argvs[0], ["rm", "-f", "bg3ai-play-x"])
        self.assertIn(["rm", "-f", S.PROXY_NAME], argvs)


class Server(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="bg3ai-sandbox-srv-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_argv_from_the_stdio_spec(self):
        argv = S.server_argv(C.bg3_server(), C.PLAY_DIR)
        self.assertEqual(argv[:2], ["uv", "run"])
        self.assertEqual(argv[argv.index("--project") + 1], str((renv.REPO / "bridge").resolve()))
        self.assertEqual(argv[-6:], ["--transport", "http", "--host", S.GW, "--port", str(S.PORT)])

    def test_port_taken(self):
        with self.assertRaises(S.SandboxError) as e:
            S.start_server(C.bg3_server(), {}, C.PLAY_DIR, self.d / "x.log", is_up=lambda: True,
                           popen=lambda *a, **k: self.fail("must not start"))
        self.assertIn("already taken", str(e.exception))

    def spec(self, code: str) -> dict:
        script = self.d / "srv.py"
        script.write_text(code)
        return {"command": sys.executable, "args": [str(script)]}

    def test_real_process_up_and_stopped(self):
        # a throwaway "server" that sleeps; up once it wrote its env; stop() ends its process group
        spec = self.spec("import json, os, sys, time\n"
                         f"open({str(self.d / 'env.json')!r}, 'w').write(json.dumps([sys.argv, dict(os.environ)]))\n"
                         "time.sleep(60)\n")
        pid_file = self.d / "srv.pid"
        up = lambda: (self.d / "env.json").exists()   # noqa: E731
        srv = S.start_server(spec, {"BG3AI_SESSION": "S007", "PATH": os.environ["PATH"]}, self.d, self.d / "x.log",
                             pid_file, timeout=20, is_up=up)
        argv, env = json.loads((self.d / "env.json").read_text())
        self.assertEqual(argv[1:], ["--transport", "http", "--host", S.GW, "--port", str(S.PORT)])
        self.assertEqual(env["BG3AI_SESSION"], "S007")
        self.assertEqual(int(pid_file.read_text()), srv.proc.pid)
        srv.stop(grace=5)
        self.assertIsNotNone(srv.proc.poll())
        self.assertFalse(pid_file.exists())

    def test_server_that_dies(self):
        spec = self.spec("import sys; sys.exit(3)\n")
        with self.assertRaises(S.SandboxError) as e:
            S.start_server(spec, dict(os.environ), self.d, self.d / "x.log", timeout=20, is_up=lambda: False)
        self.assertIn("exited 3", str(e.exception))

    def test_stale_server_of_a_dead_runner(self):
        pid_file = self.d / "srv.pid"
        pid_file.write_text("999999999\n")
        self.assertIsNone(S.kill_stale_server(pid_file))   # gone: nothing to kill, file dropped
        self.assertFalse(pid_file.exists())
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "bg3ai-mcp", "--transport",
                              "http"], start_new_session=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        pid_file.write_text(f"{p.pid}\n")
        self.assertIn("stale", S.kill_stale_server(pid_file))
        self.assertEqual(p.wait(timeout=10), -signal.SIGTERM)
        q = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(lambda: q.poll() is None and q.kill())
        pid_file.write_text(f"{q.pid}\n")
        self.assertIsNone(S.kill_stale_server(pid_file))   # not a bg3 server (a reused pid): left alone
        self.assertIsNone(q.poll())


class HttpClients(unittest.TestCase):
    def req(self, d: Path) -> C.Request:
        return C.Request("go", "m", d, session="s1", env={"BG3AI_SESSION": "S001"}, tag="S001")

    def test_claude(self):
        c = C.make_http("claude", S.URL)
        cmd = c.command(self.req(Path("/tmp")))
        cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])
        self.assertEqual(cfg, {"mcpServers": {"bg3": {"type": "http", "url": S.URL, "timeout": 110_000}}})
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "ToolSearch")
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertEqual(settings["permissions"]["allow"], ["mcp__bg3__*"])
        self.assertIn("Bash", settings["permissions"]["deny"])
        self.assertNotIn("$schema", settings)

    def test_codex(self):
        c = C.make_http("codex", S.URL)
        cfg = c.config(self.req(Path("/tmp")))
        self.assertEqual(cfg["mcp_servers.bg3.url"], S.URL)
        for k in ("command", "args", "env", "env_vars", "cwd"):
            self.assertNotIn(f"mcp_servers.bg3.{k}", cfg)
        self.assertEqual(cfg["mcp_servers.bg3.default_tools_approval_mode"], "approve")
        self.assertIn(f'mcp_servers.bg3.url="{S.URL}"', c.command(self.req(Path("/tmp"))))

    def test_gemini(self):
        with tempfile.TemporaryDirectory() as d:
            c = C.make_http("gemini", S.URL)
            [path] = c.write_config(self.req(Path(d)))
            bg3 = json.loads(path.read_text())["mcpServers"]["bg3"]
        self.assertEqual(bg3, {"httpUrl": S.URL, "timeout": 110_000, "trust": True})

    def test_pi(self):
        with tempfile.TemporaryDirectory() as d:
            system = Path(d) / "system.txt"
            system.write_text("rules")
            c = C.make_http("pi", S.URL, profile=PI_PROFILE, system_file=system, binary=S.PI_BIN,
                            extension=S.PI_EXTENSION)
            req = C.Request("go", PI_PROFILE.model, Path(d), session="s1", env={"BG3AI_SESSION": "S001"}, tag="S001")
            cmd = c.command(req)
            files = c.write_config(req)
            bg3 = json.loads((c.agent_dir(req) / "bg3.json").read_text())
        self.assertEqual(cmd[0], S.PI_BIN)
        self.assertEqual(cmd[cmd.index("-e") + 1], S.PI_EXTENSION)
        self.assertEqual(bg3, {"server": {"url": S.URL, "timeout": 110.0}, "systemPromptFile": str(system)})
        self.assertTrue(all(f.parent == c.agent_dir(req) for f in files))

    def test_stdio_unchanged(self):
        c = C.make("codex")
        cfg = c.config(self.req(Path("/tmp")))
        self.assertNotIn("mcp_servers.bg3.url", cfg)
        self.assertEqual(cfg["mcp_servers.bg3.env_vars"], ["BG3AI_SESSION"])


class PlayWiring(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="bg3ai-sandbox-play-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        for p in (mock.patch.object(ragent.client_setup, "run_files", lambda *a: {}),   # main(): no uv tool dump
                  mock.patch.dict(os.environ)):   # main(): session_env sets BG3AI_PROMPT_CONDITION
            p.start()
            self.addCleanup(p.stop)

    def test_options_pick_the_http_client(self):
        self.assertEqual(rstate.make_client(rstate.Options("pt", None, 60, "opus")).mcp_config, C.MCP_CONFIG)
        c = rstate.make_client(rstate.Options("pt", None, 60, "opus", sandbox="container"))
        self.assertIn(S.URL, c.mcp_config)

    def test_flags(self):
        with mock.patch.object(lcli, "cmd_run", lambda a: (a.sandbox, a.sandbox_unfirewalled)):
            self.assertEqual(P.main(["run", "--playthrough", "x"]), ("none", False))
            self.assertEqual(P.main(["run", "--playthrough", "x", "--sandbox", "container",
                                     "--sandbox-unfirewalled"]), ("container", True))

    def _cmd_run(self, argv: list[str], prepare) -> tuple[int, str]:
        err = []
        with mock.patch.object(lcli, "acquire_lock", lambda: None), \
                mock.patch.object(S, "prepare", prepare), \
                mock.patch.object(renv, "log", lambda m: None), \
                mock.patch.object(lpt, "Runner", side_effect=AssertionError("must not run")), \
                mock.patch("sys.stderr", SimpleNamespace(write=err.append, flush=lambda: None)):
            rc = P.main(["run", "--playthrough", "sbx", *argv])
        return rc, "".join(err)

    def test_cmd_run_refuses_without_the_firewall(self):
        def prepare(*a, **k):
            raise S.SandboxError(f"the sandbox firewall (nft table inet bg3ai_sandbox) is missing: {S.LOAD_HINT}")
        rc, err = self._cmd_run(["--sandbox", "container"], prepare)
        self.assertEqual(rc, 2)
        self.assertIn("refusing --sandbox container", err)
        self.assertIn("sudo", err)
        self.assertIn("firewall.sh up", err)

    def test_unfirewalled_needs_container(self):
        rc, err = self._cmd_run(["--sandbox-unfirewalled"], lambda *a, **k: self.fail("no prepare"))
        self.assertEqual(rc, 2)
        self.assertIn("goes with --sandbox container", err)

    def test_prepared_box_reaches_the_runner(self):
        box = S.Sandbox("claude", None, True)
        seen = {}

        def runner(opt):
            seen["opt"] = opt
            return SimpleNamespace(run=lambda: 0, stop_requested=False)
        with mock.patch.object(lcli, "acquire_lock", lambda: None), \
                mock.patch.object(S, "prepare", lambda client, home, unf, log, pid: seen.update(unf=unf) or box), \
                mock.patch.object(S, "shutdown", lambda: seen.update(shutdown=True)), \
                mock.patch.object(renv, "log", lambda m: None), mock.patch.object(lpt, "Runner", runner), \
                mock.patch("bg3ai.skipper.start_background", lambda *a: None), \
                mock.patch.object(rpolicy.ramwatch, "start_background", lambda *a: None), \
                mock.patch.object(ragent.signal, "signal", lambda *a: None):
            rc = P.main(["run", "--playthrough", "sbx", "--sandbox", "container", "--sandbox-unfirewalled"])
        self.assertEqual(rc, 0)
        self.assertTrue(seen["unf"])
        self.assertIs(seen["opt"].box, box)
        self.assertEqual(seen["opt"].sandbox, "container")
        self.assertTrue(seen["shutdown"])

    def _series(self, meta: dict | None, argv: list[str]) -> tuple[int, str, list, dict]:
        """cmd_run of a --runs series (sbx) whose series.json is meta: (rc, stderr, log lines, what reached
        run_series / prepare)."""
        rt = self.d / "runtime"
        (rt / "sessions" / "sbx").mkdir(parents=True, exist_ok=True)
        (rt / "sessions" / "sbx" / "series.json").unlink(missing_ok=True)
        if meta is not None:
            (rt / "sessions" / "sbx" / "series.json").write_text(json.dumps(meta))
        err, logs, seen = [], [], {}
        with mock.patch.object(renv, "RUNTIME", rt), mock.patch.object(lcli, "acquire_lock", lambda: None), \
                mock.patch.object(S, "prepare", lambda client, home, unf, log, pid: seen.update(prepared=unf)
                                  or S.Sandbox("claude", None, unf)), \
                mock.patch.object(S, "shutdown", lambda: None), mock.patch.object(renv, "log", logs.append), \
                mock.patch.object(lser, "run_series", lambda opt, *a, **k: seen.update(opt=opt) or 0), \
                mock.patch("bg3ai.skipper.start_background", lambda *a: None), \
                mock.patch.object(rpolicy.ramwatch, "start_background", lambda *a: None), \
                mock.patch.object(ragent.signal, "signal", lambda *a: None), \
                mock.patch("sys.stderr", SimpleNamespace(write=err.append, flush=lambda: None)):
            rc = P.main(["run", "--playthrough", "sbx", "--until", "nautiloid_escaped", "--runs", "3",
                         "--no-judge", *argv])
        return rc, "".join(err), logs, seen

    def test_series_mode_frozen(self):
        """series.json's sandbox mode rules a resumed series: another mode is refused before docker is touched
        (same as the version-drift refusal: logged, `runner stopped`), --allow-sandbox-change records it."""
        container = {"series": "sbx", "client": "claude", "sandbox": "container", "sandbox_unfirewalled": False}
        rc, err, logs, seen = self._series(container, [])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSING", err)
        self.assertIn("sandbox: container → none", err)
        self.assertIn("--sandbox container", err)
        self.assertNotIn("prepared", seen)
        self.assertTrue(any(m.startswith("runner stopped (sandbox mode") for m in logs))
        rc, err, _, seen = self._series(container, ["--sandbox", "container", "--sandbox-unfirewalled"])
        self.assertEqual(rc, 2)
        self.assertIn("container → container (unfirewalled)", err)
        rc, err, _, seen = self._series(container, ["--sandbox", "container"])               # same mode: plays
        self.assertEqual((rc, seen["prepared"], seen["opt"].sandbox_change), (0, False, None))
        rc, _, _, seen = self._series(container, ["--allow-sandbox-change"])
        self.assertEqual((rc, seen["opt"].sandbox_change), (0, "sandbox: container → none"))
        # a series from before the key (naut) played without a sandbox: a container run of it is a change
        old = {"series": "sbx", "client": "claude"}
        self.assertEqual(self._series(old, [])[0], 0)
        rc, err, _, seen = self._series(old, ["--sandbox", "container"])
        self.assertEqual(rc, 2)
        self.assertIn("sandbox: none → container", err)
        # a new series: nothing frozen yet, any mode
        self.assertEqual(self._series(None, ["--sandbox", "container", "--sandbox-unfirewalled"])[0], 0)

    def test_freeze_series_writes_the_mode_once(self):
        with mock.patch.object(renv, "RUNTIME", self.d / "runtime"):
            lser.freeze_series("sbx", 3, "m", {}, {"p": 1}, {}, None, None, "claude",
                            {"sandbox": "container", "sandbox_unfirewalled": True})
            lser.freeze_series("sbx", 3, "m", {}, {"p": 1}, {}, None, None, "claude",
                            {"sandbox": "none", "sandbox_unfirewalled": False})
            meta = lser.series_meta("sbx")
            self.assertEqual((meta["sandbox"], meta["sandbox_unfirewalled"]), ("container", True))
            # an existing series from before the key never gets one (it played without a sandbox)
            (self.d / "runtime" / "sessions" / "old").mkdir(parents=True)
            (self.d / "runtime" / "sessions" / "old" / "series.json").write_text(json.dumps({"series": "old"}))
            lser.freeze_series("old", 3, "m", {}, {"p": 1}, {}, None, None, "claude",
                            {"sandbox": "container", "sandbox_unfirewalled": False})
            self.assertNotIn("sandbox", lser.series_meta("old"))
            self.assertEqual(lser.series_meta("old")["pins"], {"p": 1})

    def test_series_sandbox_rules(self):
        from bg3bench.series import sandbox_mode as m, series_sandbox as f
        self.assertEqual(m(None, True), {"sandbox": "none", "sandbox_unfirewalled": False})
        self.assertEqual(f({}, m("container"), False), (None, None))
        self.assertEqual(f({"series": "x"}, m("none"), False), (None, None))
        err, change = f({"sandbox": "container"}, m("none"), False)
        self.assertEqual(change, "sandbox: container → none")
        self.assertIn("--allow-sandbox-change", err)
        self.assertEqual(f({"sandbox": "container"}, m("none"), True), (None, "sandbox: container → none"))

    def test_run_claude_in_the_container(self):
        """The whole play-process path with a fake docker: the http server gets the session env, the client runs
        through `docker run`, its stream is parsed as claude's, container and server are cleaned up."""
        docker = fake_docker(self.d, CLAUDE_STREAM)
        servers = []

        def start_server(server, env, cwd, log_path, pid_file=None, **kw):
            servers.append(SimpleNamespace(server=server, env=env, cwd=cwd, log=log_path, srv=FakeServer()))
            return servers[-1].srv
        with mock.patch.multiple(renv, RUNTIME=self.d / "rt", log=lambda m: None), \
                mock.patch.dict(os.environ, {"BG3AI_DOCKER": str(docker)}), \
                mock.patch.object(S, "start_server", start_server):
            (self.d / "rt").mkdir()
            box = S.Sandbox("claude", None)
            r = lpt.Runner(rstate.Options("sbx-pt", None, 60, "opus", keeper=False, sandbox="container", box=box))
            r.progress_loop = lambda *a, **k: None
            res = r.run_claude("S001", "go", 60, "11111111-1111-1111-1111-111111111111", needs_game=False)
        self.assertEqual(res.reason, "exited", res.detail)
        self.assertEqual(res.exit_code, 0)
        [srv] = servers
        self.assertEqual(srv.env["BG3AI_SERVE_RULES"], "1")        # the container has no play/CLAUDE.md
        self.assertEqual(srv.env["BG3AI_SESSION"], "S001")
        self.assertEqual(srv.env["BG3AI_FIX_NOTES"], "1")
        self.assertIn("BG3AI_SESSION_DEADLINE", srv.env)
        self.assertEqual(srv.cwd, renv.PLAY_DIR)
        self.assertTrue(srv.srv.stopped)
        argvs = calls(self.d)
        run = next(a for a in argvs if a[0] == "run")
        name = run[run.index("--name") + 1]
        self.assertTrue(name.startswith("bg3ai-play-S001-"))
        img = run.index(S.CLIENT_IMAGE)
        self.assertEqual(run[img + 1:img + 3], ["claude", "-p"])
        self.assertIn(S.URL, run[run.index("--mcp-config") + 1])
        self.assertIn(f"{r.state.dir / 'sandbox-home'}:/home/agent", run)
        self.assertEqual(argvs[-1], ["rm", "-f", name])               # removed after the docker CLI ended
        log = (r.state.dir / "S001.log").read_text()
        self.assertIn("[init] session=c-1 model=opus", log)
        self.assertIn("[tool] session_start", log)
        self.assertIn("ended: exited, exit 0", log)
        self.assertIsNone(r._sandbox_live)

    def test_pi_in_the_container_writes_its_session_files(self):
        """pi.md round 10: the container's root is read-only, so pi's agent dir and its session files (shared by the
        session's resumes) are mounted read-write at their own path; the configs it only reads stay read-only."""
        from bg3harness import profiles
        docker = fake_docker(self.d, [])
        system = self.d / "system.txt"
        system.write_text("rules\n")
        with mock.patch.multiple(renv, RUNTIME=self.d / "rt", log=lambda m: None), \
                mock.patch.dict(os.environ, {"BG3AI_DOCKER": str(docker), "DEEPSEEK_API_KEY": "sk-test"}), \
                mock.patch.object(S, "start_server", lambda *a, **k: FakeServer()):
            (self.d / "rt").mkdir()
            box = S.Sandbox("pi", None, auth_env=("DEEPSEEK_API_KEY",))
            prof = profiles.load(Path(__file__).resolve().parents[1] / "profiles" / "deepseek-flash.toml")
            r = lpt.Runner(rstate.Options("sbx-pi", None, 60, "deepseek-flash", keeper=False, sandbox="container",
                                          box=box,
                                   client="pi", profile=prof, system_file=str(system)))
            r.progress_loop = lambda *a, **k: None
            r.run_claude("S001", "go", 60, "11111111-1111-1111-1111-111111111111", needs_game=False)
        run = next(a for a in calls(self.d) if a[0] == "run")
        mounts = [run[i + 1] for i, a in enumerate(run) if a == "-v"]
        for d in (r.state.dir / "S001.pi", r.state.dir / "pi-sessions"):
            self.assertIn(f"{d.resolve()}:{d.resolve()}", mounts)
            self.assertTrue(d.is_dir())

    def test_server_failure_is_a_setup_failure(self):
        def start_server(*a, **k):
            raise S.SandboxError("172.30.99.1:8770 is already taken")
        events = []
        with mock.patch.multiple(renv, RUNTIME=self.d / "rt", log=lambda m: None), \
                mock.patch.object(S, "start_server", start_server), \
                mock.patch.object(rcode.subprocess, "Popen", side_effect=AssertionError("no client without a server")):
            (self.d / "rt").mkdir()
            r = lpt.Runner(rstate.Options("sbx-pt2", None, 60, "opus", keeper=False, sandbox="container",
                                   box=S.Sandbox("claude", None)))
            r.event = lambda kind, **kw: events.append((kind, kw))
            res = r.run_claude("S001", "go", 60, "11111111-1111-1111-1111-111111111111", needs_game=False)
        self.assertEqual(res.reason, "mcp_failed")
        self.assertIn("already taken", res.detail)
        self.assertEqual(events[0][0], "mcp_failed")

    def test_record_marks_the_sandbox(self):
        self.assertEqual(S.Sandbox("codex", None, True).record(),
                         {"sandbox": "container", "sandbox_unfirewalled": True})
        src = Path(lser.__file__).read_text()
        self.assertIn("rec.update(self.box.record())", src)


class Keeper(unittest.TestCase):
    def test_keeper_runs_in_the_container(self):
        import memory_keeper as K
        d = Path(tempfile.mkdtemp(prefix="bg3ai-sandbox-keeper-"))
        self.addCleanup(shutil.rmtree, d, True)
        mem, knowledge = d / "memory", d / "knowledge"
        mem.mkdir()
        knowledge.mkdir()
        box = S.Sandbox("claude", d / "home")
        started, ran = [], []

        def start_server(server, env, cwd, log_path, **kw):
            started.append((server, env, cwd))
            return FakeServer()
        box.start_server = start_server
        removed = []
        box.remove = removed.append

        def run_claude(cmd, cwd, log_path, timeout, extra_env=None, pending=None, watch=None):
            ran.append(cmd)
            return 0, False
        with mock.patch.object(K, "_run_claude", run_claude), \
                mock.patch.dict(os.environ, {"BG3AI_DOCKER": "docker", "BG3AI_SESSION_DEADLINE": "123"}):
            res = K.run_keeper(mem, 1, d / "S001-keeper", "opus", "low", 5, knowledge=knowledge, client="claude",
                               sandbox=box)
        self.assertEqual(len(ran), 1, res.detail)
        cmd = ran[0]
        self.assertEqual(cmd[:2], ["docker", "run"])
        self.assertEqual(cmd[cmd.index("--label") + 1], "bg3ai.sandbox=keeper")
        img = cmd.index(S.CLIENT_IMAGE)
        self.assertEqual(cmd[img + 1], "claude")
        self.assertIn(S.URL, cmd[cmd.index("--mcp-config") + 1])
        self.assertNotIn("--add-dir", cmd)                               # no file tools: the MCP pass
        [(server, env, cwd)] = started
        self.assertEqual(env["BG3AI_KEEPER_MODE"], "1")
        self.assertEqual(env["BG3AI_MEMORY_DIR"], str(mem.resolve()))
        self.assertNotIn("BG3AI_SESSION_DEADLINE", env)
        self.assertEqual(cwd, mem)
        self.assertEqual(len(removed), 1)


if __name__ == "__main__":
    unittest.main()
