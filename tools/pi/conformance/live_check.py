#!/usr/bin/env python3
"""Real-call checks of pi + bg3.ts against one real model, no game (research/arch/pi-client.md §3.5, T8): the model
plays a short scripted task against the stub MCP server, through bg3harness/pi_client.py exactly as the runner would
run it, and the script reports tool calls, malformed-argument rate, per-response usage (the endpoint's own usage
fields, from bg3.ts's bg3.billing entries), cache hits on the later requests and the cost. COSTS MONEY: it needs
the profile's key in the env and stops after --max-requests responses.

    DEEPSEEK_API_KEY=... python3 tools/pi/conformance/live_check.py profiles/deepseek-flash.toml \
        --price 0.3,1.2,0.006 --out <dir>

--price is input (cache miss), output, cache hit in USD per million tokens, for endpoints that report tokens but
no cost (DeepSeek); OpenRouter's usage.cost is used when present. For DeepSeek the account balance before and
after is printed too (GET /user/balance, free), as the billed check. An anthropic-messages profile is priced from
Anthropic's usage fields at the profile's rates (and tiers), next to pi's own estimate (pi_cost_usd): the two agree
when pi prices what Anthropic bills.

The cheapest check: --tools 4 (the stub lists only get_status, look, stay, get_party), --prompt with a two-call
task, a one-line --system file and a profile copy with effort = "low" and a small max_tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "packages" / "harness" / "src"))
from bg3harness import clients as cl
from bg3harness import pi_client as pc
from bg3harness import profiles as pr

PROMPT = (
    "This is a harness test, not a game. Do exactly this, one tool call per step: 1) call get_status; "
    '2) call press_key with key "e"; 3) call look; 4) call get_party and wait with seconds 1 in the same '
    "message; 5) reply with the single word DONE."
)


def balance(base_url: str, key: str) -> str | None:
    if "deepseek.com" not in base_url:
        return None
    req = urllib.request.Request(base_url.rstrip("/") + "/user/balance", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        infos = json.load(r).get("balance_infos") or []
    return ", ".join(f"{b.get('total_balance')} {b.get('currency')}" for b in infos)


def response_cost(usage: dict, price: tuple[float, float, float] | None) -> float | None:
    if isinstance(usage.get("cost"), (int, float)):
        return float(usage["cost"])
    if price is None:
        return None
    hit = int(
        usage.get("prompt_cache_hit_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    )
    miss = int(usage.get("prompt_cache_miss_tokens") or (int(usage.get("prompt_tokens") or 0) - hit))
    return (miss * price[0] + int(usage.get("completion_tokens") or 0) * price[1] + hit * price[2]) / 1e6


def anthropic_cost(usage: dict, profile: pr.Profile) -> float | None:
    """USD for one Messages API response from its usage (input = uncached; cache writes at the 5-minute rate; the
    prompt-length tier over the whole request), at the profile's rates; None without Anthropic's fields."""
    mi = profile.model_info
    if mi is None or "cache_read_input_tokens" not in usage:
        return None
    n = {
        k: int(usage.get(f) or 0)
        for k, f in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("cache_read", "cache_read_input_tokens"),
            ("cache_write", "cache_creation_input_tokens"),
        )
    }
    rates: dict[str, float] = mi.cost
    for tier in mi.cost_tiers:
        if n["input"] + n["cache_read"] + n["cache_write"] > tier[pr.TIER_ABOVE]:
            rates = tier
    return sum(n[k] * rates.get(k, 0.0) for k in n) / 1e6


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("profile", type=Path)
    ap.add_argument("--price", help="miss,output,hit USD per M tokens")
    ap.add_argument("--max-requests", type=int, default=8)
    ap.add_argument("--tools", type=int, help="list only the first N of the stub's tools (fewer input tokens)")
    ap.add_argument("--prompt", default=PROMPT, help="the task (default: the five-step tool script)")
    ap.add_argument("--system", type=Path, help="system text (default: dump the real one with tools/pi/dump_server.py)")
    ap.add_argument("--out", type=Path, required=True, help="keeps the stream, requests' usage and the report")
    a = ap.parse_args(argv)
    profile = pr.load(a.profile)
    key = os.environ.get(profile.endpoint.api_key_env) if profile.endpoint else None
    if not key:
        print(
            f"set {profile.endpoint.api_key_env if profile.endpoint else 'the key'} for this command", file=sys.stderr
        )
        return 2
    price = tuple(float(x) for x in a.price.split(",")) if a.price else None
    a.out.mkdir(parents=True, exist_ok=True)
    system = a.system or pc.write_system_text(a.out / "system.txt")
    before = balance(profile.endpoint.base_url, key)
    with tempfile.TemporaryDirectory(prefix="bg3-pi-live-") as tmp:
        ws = Path(tmp)
        server = {
            "command": sys.executable,
            "args": [str(HERE / "stub_mcp.py")],
            "timeout": 110_000,
            "env": {"STUB_MCP_LOG": str(a.out / "mcp.jsonl"), **({"STUB_MCP_TOOLS": str(a.tools)} if a.tools else {})},
        }
        settings = {**pc.PI_SETTINGS, "retry": {"enabled": True, "maxRetries": 1}}
        client = pc.PiClient(profile, system, server=server, cwd=ws, settings=settings)
        req = cl.Request(prompt=a.prompt, model=profile.model, session_dir=ws, session="live-check-0001", tag="live")
        client.write_config(req)
        env = {**os.environ, **client.env(req)}
        p = subprocess.Popen(
            client.command(req),
            cwd=ws,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        events, billing, out = [], [], []
        for line in p.stdout:
            out.append(line)
            events += client.parse(line)
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry = e.get("entry") or {}
            if e.get("type") == "entry_appended" and entry.get("customType") == "bg3.billing":
                billing.append(entry["data"])
                if len(billing) >= a.max_requests:
                    p.terminate()
                    break
        p.wait(120)
        err = p.stderr.read()
    (a.out / "stream.jsonl").write_text("".join(out))
    after = balance(profile.endpoint.base_url, key)
    calls = [e for e in events if e.kind == "tool_call"]
    anthropic = profile.endpoint is not None and profile.endpoint.api == "anthropic-messages"
    costs = [
        anthropic_cost(b.get("usage") or {}, profile) if anthropic else response_cost(b.get("usage") or {}, price)
        for b in billing
    ]
    report = {
        "profile": str(a.profile),
        "model": profile.model,
        "rc": p.returncode,
        "init": next(({"tools": e.tools, "mcp": e.mcp} for e in events if e.kind == "init"), None),
        "tool_calls": [{"tool": e.tool, "args": e.args} for e in calls],
        "malformed": client.parser.malformed,
        "malformed_rate": client.parser.malformed / len(calls) if calls else None,
        "server_calls": [
            json.loads(x)["tool"]
            for x in (a.out / "mcp.jsonl").read_text().splitlines()
            if json.loads(x).get("ev") == "start"
        ]
        if (a.out / "mcp.jsonl").exists()
        else [],
        "texts": [e.text for e in events if e.kind == "text"],
        "responses": [
            {"id": b.get("id"), "provider": b.get("provider"), "usage": b.get("usage"), "cost_usd": c}
            for b, c in zip(billing, costs, strict=True)
        ],
        "cost_usd": round(sum(c for c in costs if c is not None), 6),
        "pi_cost_usd": client.parser._cost(),  # what the runner's cost cap counts
        "result": next(
            (
                {"subtype": e.subtype, "is_error": e.is_error, "text": e.text[:300]}
                for e in events
                if e.kind == "result"
            ),
            None,
        ),
        "balance_before": before,
        "balance_after": after,
        "stderr_tail": err[-500:],
    }
    (a.out / "report.json").write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
