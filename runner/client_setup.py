"""What a run needs before its first play process for a profile-driven client (pi; research/ovh/pi.md "play.py
wiring patch"): the profile resolved against the CLI flags, the API key in the env, the system text and the bg3
server's full tool list dumped once per run, and the tool count each play process must get.

    python3 -m unittest runner/test_client_setup.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import profiles
from profiles import Profile

DEFAULT_MODEL = "opus"   # play.py's --model default (claude)


class SetupRefused(ValueError):
    pass


@dataclass(frozen=True)
class ClientChoice:
    client: str
    model: str
    effort: str | None
    profile: Profile | None = None


def resolve(client: str | None, profile_path: str | None, model: str | None, effort: str | None,
            fix_model: str | None = None, keeper: bool = False) -> ClientChoice:
    """--client/--profile/--model/--effort -> the play client. The profile names the client and the model (a
    different --client or --model is refused); --effort wins over the profile's. pi needs a profile, and plays
    without the memory keeper and the fix-notes model (both would start pi with another model)."""
    if profile_path is None:
        if client == "pi":
            raise SetupRefused("--client pi needs --profile <file.toml> (profiles/*.toml)")
        return ClientChoice(client or "claude", model or DEFAULT_MODEL, effort)
    try:
        prof = profiles.load(Path(profile_path))
    except (OSError, profiles.ProfileError) as e:
        raise SetupRefused(f"--profile {profile_path}: {e}") from None
    if client and client != prof.client:
        raise SetupRefused(f"--client {client} but --profile {profile_path} is a {prof.client} profile")
    if model and model != prof.model:
        raise SetupRefused(f"--model {model} but --profile {profile_path} plays {prof.model}")
    if prof.client == "pi" and keeper:
        raise SetupRefused("--client pi: the memory keeper can't run through pi yet; pass --no-keeper")
    if prof.client == "pi" and fix_model and fix_model != prof.model:
        raise SetupRefused("--client pi plays one model: --fix-model must be the profile's")
    return ClientChoice(prof.client, prof.model, effort or prof.effort, prof)


def read_dotenv(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a .env file (`export ` and quotes allowed, # comments); {} when there is none."""
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.removeprefix("export ").partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def api_key_env(prof: Profile | None, environ: dict, dotenv: Path) -> dict[str, str]:
    """{name: value} of the profile's API key env var to add to the runner's env: {} when it is set already (or
    the client has no endpoint), else the value from the .env file. Missing everywhere: refused. The value is
    never logged or written: pi's models.json names the variable."""
    if prof is None or prof.endpoint is None:
        return {}
    name = prof.endpoint.api_key_env
    if environ.get(name):
        return {}
    value = read_dotenv(dotenv).get(name)
    if not value:
        raise SetupRefused(f"{name} (the profile's api_key_env) is set neither in the env nor in {dotenv}")
    return {name: value}


def dump_server(system: Path, tools: Path, repo: Path, run=subprocess.run) -> None:
    """The system text (D2) and the bg3 server's full tool list, offline in the bridge's env (tools/pi/dump_server.py).
    The runner's BG3AI_TOOLS* are left out so the list is the whole registry."""
    env = {k: v for k, v in os.environ.items() if k not in ("BG3AI_TOOLS", "BG3AI_TOOLS_OFF")}
    run(["uv", "run", "--project", str(repo / "bridge"), "python", str(repo / "tools/pi/dump_server.py"),
         "--system", str(system), "--tools", str(tools)], check=True, capture_output=True, timeout=300, env=env)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tool_names(path: Path) -> list[str]:
    """The names in a dumped tools/list."""
    return [t["name"] for t in json.loads(path.read_text())]


def expected_tools(registry: list[str], env: dict) -> int:
    """How many bg3 tools a play process with this session env must get (the server removes the rest at startup:
    bridge/src/bg3ai/capabilities.py). Raises ValueError for an unknown group or name, as the server would."""
    from bg3ai import capabilities
    return len(set(registry) - capabilities.disabled(registry, env))


def tool_count_problem(got: int | None, expected: int | None, env: dict) -> str | None:
    """The mcp_failed detail when the init event's tool count isn't the expected one, else None."""
    if expected is None or got is None or got == expected:
        return None
    sel = ", ".join(f"{k}={env[k]}" for k in ("BG3AI_TOOLS", "BG3AI_TOOLS_OFF") if env.get(k)) or "all tools"
    return f"the bg3 MCP server gave {got} tools, expected {expected} ({sel})"
