# pyright: basic
"""`play.py run --sandbox container`: the play client in a container that reaches only the bg3 MCP server
(benchmark step 3; tools/container/README.md, "The runner").

Per play process (and per memory-keeper call) the runner starts the bg3 MCP server on the host over streamable HTTP,
bound to the sandbox network's gateway (172.30.99.1:8770) with the session env a stdio server would get, and runs the
client CLI (runner/clients.py) inside `bg3ai/agent-client` with that server as its only MCP server:

    host                                         internal network bg3ai-sandbox (172.30.99.0/24)
      bg3ai-mcp --transport http  <── 8770 ───   client container (claude | codex | gemini | pi),
      game, repo, saves, memory                    --cap-drop ALL, read-only root,
                                                   HOME = runtime/sessions/<pt>/sandbox-home
                                                     │ HTTPS_PROXY
      internet  <── bridge ── bg3ai-egress (172.30.99.2:3128): CONNECT to the client's model API host only

The host firewall (tools/container/firewall.sh, nft table `inet bg3ai_sandbox`) closes every other host port to
that network; `prepare` refuses to start without it unless the run passes --sandbox-unfirewalled (a dev flag; the
run record says so). Everything else stays the runner's: the client's stdout is the same stream (docker run passes
it through), the stop check, keeper and records run unchanged.

Docker is driven through the `docker` CLI ($BG3AI_DOCKER, tests use a fake one); nft through `nft` or `sudo -n nft`
(read-only: `list table`), then a probe from a container (`firewall.sh check`).
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
CONTAINER_DIR = REPO / "tools" / "container"
PI_DIR = REPO / "tools" / "pi"              # Dockerfile.agent's `pi` build context: pi pinned by its package-lock
PI_HOME = "/opt/pi"                         # where the image has it (npm ci of tools/pi, with bg3.ts)
PI_BIN = f"{PI_HOME}/node_modules/.bin/pi"
PI_EXTENSION = f"{PI_HOME}/bg3.ts"
FIREWALL = CONTAINER_DIR / "firewall.sh"
NET = os.environ.get("BG3AI_SANDBOX_NET", "bg3ai-sandbox")
SUBNET = os.environ.get("BG3AI_SANDBOX_SUBNET", "172.30.99.0/24")
GW = os.environ.get("BG3AI_SANDBOX_GW", "172.30.99.1")
PORT = int(os.environ.get("BG3AI_MCP_PORT", "8770"))
PROXY_IP = os.environ.get("BG3AI_SANDBOX_PROXY_IP", "172.30.99.2")
PROXY_PORT = 3128
TABLE = "bg3ai_sandbox"
TOOLS_IMAGE = "bg3ai/mcp-client:latest"     # tools/container/Dockerfile: the smoke client, firewall probe, proxy
CLIENT_IMAGE = "bg3ai/agent-client:latest"  # tools/container/Dockerfile.agent: the play CLIs
PROXY_NAME = "bg3ai-egress"
LABEL = "bg3ai.sandbox"
MODES = ("none", "container")
SERVER_START_S = 90          # uv may sync the bridge env first
BUILD_TIMEOUT_S = 1800       # the first agent-client build downloads the CLIs
URL = f"http://{GW}:{PORT}/mcp"

# What each client needs outside: its model API (and its auth refresh). Override with BG3AI_SANDBOX_EGRESS
# ("host1,host2"). CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC keeps claude off its telemetry hosts.
EGRESS = {
    "claude": ("api.anthropic.com", "console.anthropic.com"),
    "codex": ("api.openai.com", "chatgpt.com", "auth.openai.com"),
    "gemini": ("generativelanguage.googleapis.com", "cloudcode-pa.googleapis.com", "oauth2.googleapis.com"),
    "pi": ("openrouter.ai", "api.deepseek.com"),   # the profiles' endpoints (profiles/*.toml)
}
# Credentials passed into the container by name (`docker run -e NAME`: the value never shows in argv or logs), when
# set in the runner's env. A login kept in the sandbox home (e.g. `claude setup-token` → CLAUDE_CODE_OAUTH_TOKEN, or
# the CLI's own login run once in the container) works too. pi's key is the one its profile names
# (`[endpoint] api_key_env`): prepare(..., auth_env=(profile.endpoint.api_key_env,)).
AUTH_ENV = {
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}
CLIENT_ENV = {"claude": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}}
# The Dockerfile.agent build args: the host's CLI versions, so the container plays with the same client.
VERSION_ARGS = {"claude": "CLAUDE_VERSION", "codex": "CODEX_VERSION", "gemini": "GEMINI_VERSION"}


class SandboxError(RuntimeError):
    """The sandbox can't run (docker, image, network, firewall, server): the message says what to do."""


def docker_bin() -> str:
    return os.environ.get("BG3AI_DOCKER", "docker")


def run(argv: list[str], timeout: float = 60, check: bool = False) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise SandboxError(f"{argv[0]} not found ({e})") from None
    except subprocess.TimeoutExpired:
        raise SandboxError(f"{' '.join(argv[:3])} timed out after {timeout:g} s") from None
    if check and r.returncode != 0:
        raise SandboxError(f"{' '.join(argv[:4])} failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def docker(*args: str, timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess:
    return run([docker_bin(), *args], timeout, check)


# ------------------------------------------------------------------------------------------------ firewall gate

def table_state(runner=run) -> str:
    """'loaded' (the table holds the bg3 server's accept rule for GW:PORT), 'stale' (loaded for another address or
    port), 'missing', or 'unknown' (nft can't be read without root and `sudo -n` isn't allowed). Read-only."""
    for argv in (["nft", "list", "table", "inet", TABLE], ["sudo", "-n", "nft", "list", "table", "inet", TABLE]):
        try:
            r = runner(argv, 15)
        except SandboxError:
            continue
        if r.returncode == 0:
            return "loaded" if f"ip daddr {GW} tcp dport {PORT}" in r.stdout else "stale"
        if re.search(r"no such file|does not exist", r.stderr or "", re.I):
            return "missing"
    return "unknown"


def probe(runner=run) -> tuple[bool, str]:
    """firewall.sh check: from a container on the network, which host ports answer. (only the bg3 server?, report)"""
    try:
        r = runner([str(FIREWALL), "check"], 300)
    except SandboxError as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


LOAD_HINT = (f"load it once per boot (root): sudo {FIREWALL} up   — then `{FIREWALL} check` should print OK "
             "(tools/container/README.md); --sandbox-unfirewalled runs without it (dev only, recorded)")


def firewall_gate(unfirewalled: bool, log=print, runner=run) -> dict:
    """Refuses (SandboxError) unless the nft table is loaded (or unreadable here) and the probe sees only the bg3
    server. With unfirewalled=True it only reports. Returns {"table", "probe_ok", "unfirewalled"} for the record."""
    state = table_state(runner)
    if unfirewalled:
        log(f"sandbox: WARNING --sandbox-unfirewalled: the nft table is {state}; the container may reach every host "
            "port bound to all interfaces (sshd, Steam, ...). Dev runs only.")
        return {"table": state, "probe_ok": None, "unfirewalled": True}
    if state in ("missing", "stale"):
        raise SandboxError(f"the sandbox firewall (nft table inet {TABLE}) is {state}: {LOAD_HINT}")
    ok, report = probe(runner)
    if not ok:
        raise SandboxError(f"the sandbox reaches more of the host than the bg3 server (nft table {state}):\n{report}\n"
                           f"{LOAD_HINT}")
    if state == "unknown":
        log("sandbox: the nft table can't be read without root here; the probe from a container passed")
    return {"table": state, "probe_ok": True, "unfirewalled": False}


# ------------------------------------------------------------------------------------------- docker resources

def ensure_network() -> None:
    if docker("network", "inspect", NET, check=False).returncode != 0:
        docker("network", "create", "--internal", "--subnet", SUBNET, "--gateway", GW, NET)


def cli_version(name: str) -> str | None:
    """The host CLI's version (`claude --version` → 2.1.301), for the image build args."""
    try:
        r = run([name, "--version"], 20)
    except SandboxError:
        return None
    m = re.search(r"\d+\.\d+\.\d+", r.stdout or "")
    return m.group(0) if r.returncode == 0 and m else None


def pi_version() -> str | None:
    """The pi version tools/pi/package.json pins (the image installs it from the lock file)."""
    try:
        deps = json.loads((PI_DIR / "package.json").read_text())["dependencies"]
    except (OSError, ValueError, KeyError):
        return None
    return deps.get("@earendil-works/pi-coding-agent")


def build_images(client: str) -> dict:
    """Builds the tools image and the agent image (cached when nothing changed). {client version} for the record."""
    docker("build", "-q", "-t", TOOLS_IMAGE, str(CONTAINER_DIR), timeout=BUILD_TIMEOUT_S)
    args, version = ["--build-context", f"pi={PI_DIR}"], None
    if client in VERSION_ARGS and (version := cli_version(client)):
        args += ["--build-arg", f"{VERSION_ARGS[client]}={version}"]
    elif client == "pi":
        version = pi_version()
    docker("build", "-q", "-t", CLIENT_IMAGE, "-f", str(CONTAINER_DIR / "Dockerfile.agent"), *args,
           str(CONTAINER_DIR), timeout=BUILD_TIMEOUT_S)
    return {"client_version": version}


def egress_hosts(client: str) -> list[str]:
    raw = os.environ.get("BG3AI_SANDBOX_EGRESS")
    return [h.strip() for h in raw.split(",") if h.strip()] if raw else list(EGRESS.get(client, ()))


def start_proxy(client: str) -> list[str]:
    """(Re)creates the egress proxy for this client's hosts: on the sandbox network at PROXY_IP, and on Docker's
    default bridge for its way out."""
    hosts = egress_hosts(client)
    docker("rm", "-f", PROXY_NAME, check=False)
    docker("run", "-d", "--name", PROXY_NAME, "--label", f"{LABEL}=egress", "--network", NET, "--ip", PROXY_IP,
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--entrypoint", "python3",
           TOOLS_IMAGE, "/app/egress_proxy.py", "--listen", PROXY_IP, "--port", str(PROXY_PORT),
           "--allow", ",".join(hosts))
    docker("network", "connect", "bridge", PROXY_NAME)
    return hosts


def remove_containers(role: str = "play") -> None:
    r = docker("ps", "-aq", "--filter", f"label={LABEL}={role}", check=False)
    ids = (r.stdout or "").split()
    if ids:
        docker("rm", "-f", *ids, check=False)


def listening(host: str = GW, port: int = PORT, timeout: float = 1.0) -> bool:
    try:
        socket.create_connection((host, port), timeout).close()
        return True
    except OSError:
        return False


# ------------------------------------------------------------------------------------------------ the server

@dataclass
class HttpServer:
    proc: subprocess.Popen
    log_path: Path
    pid_file: Path | None = None

    def stop(self, grace: float = 10.0) -> None:
        """The server and its uv parent (one process group): SIGTERM, then SIGKILL."""
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5)):
            if self.proc.poll() is not None:
                break
            try:
                os.killpg(self.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                self.proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                continue
        if self.pid_file is not None:
            self.pid_file.unlink(missing_ok=True)


def server_argv(server: dict, cwd: Path) -> list[str]:
    """A stdio server spec of play/.mcp.json (or the keeper's) as an http server on GW:PORT. A relative --project is
    made absolute against cwd."""
    args = list(server.get("args") or [])
    for i, a in enumerate(args[:-1]):
        if a == "--project":
            args[i + 1] = str((Path(cwd) / args[i + 1]).resolve())
    return [server["command"], *args, "--transport", "http", "--host", GW, "--port", str(PORT)]


def kill_stale_server(pid_file: Path) -> str | None:
    """A server a dead runner left (its pid file): killed when its cmdline is still bg3ai-mcp over http."""
    try:
        pid = int(pid_file.read_text().split()[0])
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError, IndexError):
        pid_file.unlink(missing_ok=True)
        return None
    if b"bg3ai-mcp" not in cmd or b"http" not in cmd:
        pid_file.unlink(missing_ok=True)
        return None
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    pid_file.unlink(missing_ok=True)
    return f"stopped a stale bg3 http server (pid {pid})"


def start_server(server: dict, env: dict, cwd: Path, log_path: Path, pid_file: Path | None = None,
                 timeout: float = SERVER_START_S, popen=subprocess.Popen, is_up=listening) -> HttpServer:
    """Starts the bg3 server over http with `env` (the session env a stdio server would get) and waits until it
    listens. SandboxError when GW:PORT is already taken or it doesn't come up."""
    if pid_file is not None:
        kill_stale_server(pid_file)
    if is_up():
        raise SandboxError(f"{GW}:{PORT} is already taken (a server from tools/container/run.sh?); stop it first")
    out = open(log_path, "a")
    try:
        proc = popen(server_argv(server, cwd), cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        out.close()
    srv = HttpServer(proc, Path(log_path), pid_file)
    if pid_file is not None:
        pid_file.write_text(f"{proc.pid}\n")
    end = time.time() + timeout
    while not is_up():
        if proc.poll() is not None or time.time() > end:
            srv.stop(grace=2)
            why = f"exited {proc.returncode}" if proc.returncode is not None else f"not listening after {timeout:g} s"
            raise SandboxError(f"the bg3 http server {why}: see {log_path}")
        time.sleep(0.25)
    return srv


# ----------------------------------------------------------------------------------------------- the sandbox

@dataclass
class Sandbox:
    """One runner's container sandbox (prepare() builds it once per runner start)."""
    client: str
    home: Path | None                            # the client's HOME inside (its sessions, settings, login): the
                                                 # Runner sets runtime/sessions/<pt>/sandbox-home
    unfirewalled: bool = False
    info: dict = field(default_factory=dict)     # firewall + build facts for the log and the record
    pid_file: Path | None = None
    auth_env: tuple[str, ...] | None = None      # the credential env names to pass (None: AUTH_ENV[client])

    @property
    def url(self) -> str:
        return URL

    def record(self) -> dict:
        """runs.jsonl fields: sandbox=container, and sandbox_unfirewalled when the firewall wasn't required."""
        return {"sandbox": "container", **({"sandbox_unfirewalled": True} if self.unfirewalled else {})}

    def container_name(self, tag: str) -> str:
        return f"bg3ai-play-{re.sub(r'[^A-Za-z0-9_.-]', '-', tag)}-{os.getpid()}-{int(time.time() * 1000) % 10**8}"

    def run_argv(self, name: str, client_cmd: list[str], client_env: dict | None = None,
                 files: list[Path] | tuple = (), role: str = "play", writable: list[Path] | tuple = ()) -> list[str]:
        """`docker run` of client_cmd in the agent image: the sandbox network only, no capabilities, read-only root,
        HOME from the host's sandbox home, config files (client.write_config's) mounted read-only at their path, and
        `writable` dirs (pi's agent dir: its session files) read-write at their path."""
        if self.home is None:
            raise SandboxError("no sandbox home set (the Runner sets runtime/sessions/<pt>/sandbox-home)")
        self.home.mkdir(parents=True, exist_ok=True)
        proxy = f"http://{PROXY_IP}:{PROXY_PORT}"
        argv = [docker_bin(), "run", "--rm", "--name", name, "--label", f"{LABEL}={role}", "--network", NET,
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp",
                "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{self.home}:/home/agent", "-w", "/home/agent",
                "-e", "HOME=/home/agent"]
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            argv += ["-e", f"{k}={proxy}"]
        argv += ["-e", f"NO_PROXY={GW}", "-e", f"no_proxy={GW}"]
        for k, v in {**CLIENT_ENV.get(self.client, {}), **(client_env or {})}.items():
            argv += ["-e", f"{k}={v}"]
        for k in AUTH_ENV.get(self.client, ()) if self.auth_env is None else self.auth_env:
            if os.environ.get(k):
                argv += ["-e", k]   # by name: docker copies the value from its own env
        dirs = [Path(d).resolve() for d in writable]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            argv += ["-v", f"{d}:{d}"]
        for f in files:
            f = Path(f).resolve()
            if not any(f.is_relative_to(d) for d in dirs):
                argv += ["-v", f"{f}:{f}:ro"]
        return argv + [CLIENT_IMAGE, *client_cmd]

    def start_server(self, server: dict, env: dict, cwd: Path, log_path: Path, **kw) -> HttpServer:
        return start_server(server, env, cwd, log_path, self.pid_file, **kw)

    @staticmethod
    def remove(name: str) -> None:
        """The container, in case the docker CLI died before it (a SIGKILL doesn't reach the container)."""
        try:
            docker("rm", "-f", name, check=False, timeout=30)
        except SandboxError:
            pass


def prepare(client: str, home: Path | None = None, unfirewalled: bool = False, log=print, pid_file: Path | None = None,
            gate=firewall_gate, auth_env: tuple[str, ...] | None = None) -> Sandbox:
    """Once per runner start: docker answers, the network and images exist, the firewall is up (or the dev flag),
    the egress proxy runs, no play container of a dead runner is left. SandboxError says what's missing. auth_env:
    the credential env names to pass in (pi: its profile's api_key_env; default AUTH_ENV[client])."""
    if docker("version", "--format", "{{.Server.Version}}", check=False, timeout=30).returncode != 0:
        raise SandboxError("docker doesn't answer (is the daemon running and the user in the docker group?)")
    ensure_network()
    log(f"sandbox: building {TOOLS_IMAGE} and {CLIENT_IMAGE} (cached after the first build)")
    info = build_images(client)
    info.update(gate(unfirewalled, log))
    remove_containers("play")
    remove_containers("keeper")
    info["egress"] = start_proxy(client)
    log(f"sandbox: {client} in {CLIENT_IMAGE} on {NET}; bg3 server {URL}; egress {', '.join(info['egress'])}; "
        f"firewall {'OFF (--sandbox-unfirewalled)' if unfirewalled else info.get('table')}")
    return Sandbox(client, home, unfirewalled, info, pid_file, auth_env)


def shutdown() -> None:
    """At the runner's end: the proxy and any play/keeper container left."""
    for role in ("play", "keeper"):
        try:
            remove_containers(role)
        except SandboxError:
            pass
    try:
        docker("rm", "-f", PROXY_NAME, check=False, timeout=30)
    except SandboxError:
        pass
