"""What a run's manifest needs from the harness (docs/BACKLOG.md "Real E2E round 1 result"): the dice seed and
timing of the scenario, the client's name and version, the sha256 and parts of what the client is served (the bg3
server's instructions and the rules from assemble()), and the bg3 tools the play processes get (their count and a
hash of their names). The runner collects them once per run
(manifest_fields) and hands them to bg3bench.run_artifacts.write_run_artifacts (artifact_kwargs).

    fields = manifest_fields("claude", client_version("claude"), "rules+coaching", tools, scenario, instructions)
    write_run_artifacts(run_dir, series, record, harness_version, **artifact_kwargs(fields), scenario_spec=scenario)
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection
from typing import Any

from . import assemble, prompts, sandbox

# the clients whose binary reports its version (`<name> --version`); pi is pinned in tools/pi/package.json
CLI_CLIENTS = ("claude", "codex", "gemini")

# the prompt part that is the bg3 server's instructions (the others are prompts/ files)
INSTRUCTIONS_PART = "server:instructions"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tools_digest(names: Collection[str]) -> dict[str, Any]:
    """The manifest's tools: the count and the sha256 of the sorted names as a JSON list (order-free, the same set
    gives the same hash)."""
    ordered = sorted(set(names))
    return {"allowlist_sha256": hashlib.sha256(json.dumps(ordered).encode("utf-8")).hexdigest(), "count": len(ordered)}


def client_version(
    client: str,
    cli: Callable[[str], str | None] = sandbox.cli_version_line,
    pinned: Callable[[], str | None] = sandbox.pi_version,
) -> str | None:
    """The client's version: the CLI's own `--version` line (`2.1.301 (Claude Code)`), pi's pinned one, None when
    unknown."""
    if client == "pi":
        return pinned()
    return cli(client) if client in CLI_CLIENTS else None


def served_text(instructions: str, rules: str) -> str:
    """The server instructions with the rules, as the bg3 server serves them (bridge rules.instructions) and pi's
    system text holds them (tools/pi/dump_server.py --system)."""
    return f"{instructions.strip()}\n\n=== RULES ===\n{rules.strip()}\n"


def manifest_fields(
    client: str,
    version: str | None,
    condition: str,
    tools: Collection[str],
    scenario: dict[str, Any] | None,
    instructions: str,
) -> dict[str, Any]:
    """The run's manifest fields. tools: the bg3 tools every play process gets (the server's registry minus the
    session's BG3AI_TOOLS / BG3AI_TOOLS_OFF: client_setup.expected_tools' set); the prompt is assemble(condition,
    client) with the capability groups those tools leave on. instructions: the bg3 server's own instructions for
    those tools (tools/pi/dump_server.py --instructions in the session env), which every client gets in the MCP
    initialize result; the prompt hash is of served_text(instructions, the rules), its parts the instructions' and
    assemble's. A coaching condition is served with every group's rules whatever tools are off, as the bg3 server
    serves it (bridge rules.load) and play/CLAUDE.md holds it (live9: a claude bench run with the memory tools off
    had an error instead of its hash). scenario: a v2 spec (packages/bench/scenarios); seed and timing are None
    without one. Raises ValueError for an unknown condition or client, as assemble does."""
    on = set(tools)
    p = prompts()
    if p.CONDITIONS.get(condition):
        caps = set(p.ALL_CAPS)
    else:
        caps = {c for c, names in p.CAPABILITIES.items() if set(names) <= on}
    asm = assemble(condition, "mcp" if client not in p.CLIENTS else client, caps=caps)
    text = served_text(instructions, asm.text)
    parts = [{"file": INSTRUCTIONS_PART, "sha256": _sha(instructions.strip())}, *asm.parts]
    sc: dict[str, Any] = scenario or {}
    settings: dict[str, Any] = sc.get("game_settings") or {}
    return {
        "seed": settings.get("seed"),
        "timing": sc.get("timing"),
        "client": {"name": client, "version": version},
        "condition": condition,
        "prompt": {"sha256": _sha(text), "parts": parts},
        "tools": tools_digest(on),
    }


def artifact_kwargs(fields: dict[str, Any]) -> dict[str, Any]:
    """manifest_fields as write_run_artifacts' keywords (seed and timing reach it through scenario_spec)."""
    return {
        "prompt_sha": fields["prompt"]["sha256"],
        "prompt_parts": fields["prompt"]["parts"],
        "tools": fields["tools"],
        "client_version": fields["client"]["version"],
        "condition": fields["condition"],
    }
