"""What a run needs before its first play process for a profile-driven client (pi; research/ovh/pi.md "play.py
wiring patch"): the profile resolved against the CLI flags, the API key in the env, the system text and the bg3
server's full tool list dumped once per run, the tool count each play process must get (pi, claude) and the run
manifest's fields.

    python3 -m unittest runner/test_client_setup.py
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import profiles
from profiles import Profile

DEFAULT_MODEL = "opus"  # play.py's --model default (claude)


class SetupRefused(ValueError):
    pass


@dataclass(frozen=True)
class ClientChoice:
    client: str
    model: str
    effort: str | None
    profile: Profile | None = None


def resolve(
    client: str | None,
    profile_path: str | None,
    model: str | None,
    effort: str | None,
    fix_model: str | None = None,
    keeper: bool = False,
) -> ClientChoice:
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


def session_env(client: str, prof: Profile | None, scenario_run: bool, environ: dict) -> dict[str, str]:
    """{name: value} to add to the runner's env, which every play process's bg3 server inherits: the prompt
    condition when the operator set none (coaching, i.e. the PACE:/NOTES WINDOW nudges and the kickoff's pace
    pre-flight, in playthroughs and for claude, whose play/CLAUDE.md is rules+coaching; scenario runs of the other
    clients get rules), and the profile's tools_off (a model without image input gets no screenshot tools), so the
    tool count check and the run manifest see the set the server serves."""
    out: dict[str, str] = {}
    if not environ.get("BG3AI_PROMPT_CONDITION"):
        out["BG3AI_PROMPT_CONDITION"] = "rules+coaching" if client == "claude" or not scenario_run else "rules"
    if prof is not None:
        off = profiles.tool_env(prof, environ).get("BG3AI_TOOLS_OFF")
        if off:
            out["BG3AI_TOOLS_OFF"] = off
    return out


def kickoff_pace(client: str, environ: dict) -> str:
    """The kickoff's pace pre-flight for this client (prompts/assemble.py pace_preflight), or "" when the session's
    condition (BG3AI_PROMPT_CONDITION, default rules) has no coaching."""
    import bg3harness

    if "coaching" not in (environ.get("BG3AI_PROMPT_CONDITION") or "rules").split("+"):
        return ""
    return bg3harness.prompts().pace_preflight(client)


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


def _dump(args: list[str], repo: Path, env: dict[str, str], run) -> None:
    """tools/pi/dump_server.py with these args, offline in the bridge's env."""
    run(
        ["uv", "run", "--project", str(repo / "bridge"), "python", str(repo / "tools/pi/dump_server.py"), *args],
        check=True,
        capture_output=True,
        timeout=300,
        env=env,
    )


def dump_server(system: Path, tools: Path, instructions: Path, repo: Path, run=subprocess.run) -> None:
    """The bg3 server's full tool list, then the system text (D2) and the server's own instructions, offline in the
    bridge's env (tools/pi/dump_server.py). The list leaves the runner's BG3AI_TOOLS* out so it is the whole
    registry; the texts keep the session env (BG3AI_TOOLS*, BG3AI_PROMPT_CONDITION), as the play processes' server
    words them for the tools it serves."""
    env = {k: v for k, v in os.environ.items() if k not in ("BG3AI_TOOLS", "BG3AI_TOOLS_OFF")}
    _dump(["--tools", str(tools)], repo, env, run)
    _dump(["--system", str(system), "--instructions", str(instructions)], repo, dict(os.environ), run)


def run_files(
    d: Path,
    client: str,
    seed: int | None,
    timing: str | None,
    repo: Path,
    log: Callable[[str], None],
    dump: Callable[[Path, Path, Path, Path], None] = dump_server,
    version: Callable[[str], str | None] | None = None,
) -> dict:
    """play.py's Options fields every play process of a pi or claude run shares, from the system text, the bg3
    server's tool list and its instructions dumped into the playthrough's dir `d`: pi's system_file, the bg3 tools
    the init must count (claude: its mcp__bg3__* tools) and the run manifest's fields. The condition is the text the
    client is served: claude's play/CLAUDE.md is rules+coaching, pi's the server's BG3AI_PROMPT_CONDITION (default
    rules). A condition the tools can't serve (coaching with a group off) leaves the manifest's error instead of
    its prompt."""
    from bg3harness import run_manifest

    d.mkdir(parents=True, exist_ok=True)
    system, tools, instructions = d / "pi-system.txt", d / "bg3-tools.json", d / "bg3-instructions.txt"
    dump(system, tools, instructions, repo)
    served = served_tools(tool_names(tools), os.environ)
    condition = "rules+coaching" if client == "claude" else os.environ.get("BG3AI_PROMPT_CONDITION", "rules")
    try:
        manifest = run_manifest.manifest_fields(
            client,
            (version or run_manifest.client_version)(client),
            condition,
            served,
            {"game_settings": {"seed": seed}, "timing": timing},
            instructions.read_text(encoding="utf-8"),
        )
        prompt = f"prompt {condition} sha256 {manifest['prompt']['sha256'][:12]}"
    except ValueError as e:
        manifest, prompt = {"condition": condition, "error": str(e)}, f"NO manifest prompt hash: {e}"
    log(
        f"{client}: {len(served)} bg3 tools expected, {prompt}"
        + (f"; system text {system} (sha256 {sha256(system)[:12]})" if client == "pi" else "")
    )
    return {"tools_expected": len(served), "manifest": manifest} | (
        {"system_file": str(system)} if client == "pi" else {}
    )


def container_writable(client, req) -> list[Path]:
    """The dirs a client in the --sandbox container writes (its root is read-only): pi's agent dir and its session
    files (pi_client.sessions_dir, shared by the session's resumes). Other clients write only under HOME."""
    if client.name != "pi":
        return []
    return [client.agent_dir(req), client.sessions_dir(req)]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tool_names(path: Path) -> list[str]:
    """The names in a dumped tools/list."""
    return [t["name"] for t in json.loads(path.read_text())]


def served_tools(registry: list[str], env: dict) -> list[str]:
    """The bg3 tools a play process with this session env gets, sorted (the server removes the rest at startup:
    bridge/src/bg3ai/capabilities.py). Raises ValueError for an unknown group or name, as the server would."""
    from bg3ai import capabilities

    return sorted(set(registry) - capabilities.disabled(registry, env))


def expected_tools(registry: list[str], env: dict) -> int:
    """How many bg3 tools a play process with this session env must get (served_tools)."""
    return len(served_tools(registry, env))


def tool_count_problem(got: int | None, expected: int | None, env: dict) -> str | None:
    """The mcp_failed detail when the init event's tool count isn't the expected one, else None."""
    if expected is None or got is None or got == expected:
        return None
    sel = ", ".join(f"{k}={env[k]}" for k in ("BG3AI_TOOLS", "BG3AI_TOOLS_OFF") if env.get(k)) or "all tools"
    return f"the bg3 MCP server gave {got} tools, expected {expected} ({sel})"
