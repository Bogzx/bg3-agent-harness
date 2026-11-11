"""What the ChatGPT plan's Codex usage stands at: the 5-hour and weekly windows' used percent and the credits balance
(GET chatgpt.com/backend-api/wham/usage, the endpoint the Codex CLI's /status reads; no model call). A subscription
profile (profiles/gpt-6*-pi.toml) spends the windows first, then the credits. Run it before and after a run or a
matrix to see what it used; one line of JSON per call, so a log of calls diffs easily.

    python3 tools/dev/codex_usage.py [--auth ~/.codex/auth.json]

The token is the Codex CLI's login, the file a Codex profile reads. It is read only and never printed. An expired token
is not refreshed here: run the Codex CLI once, which refreshes its own file.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

URL = "https://chatgpt.com/backend-api/wham/usage"
AUTH = Path.home() / ".codex" / "auth.json"


def summary(usage: dict[str, Any]) -> dict[str, Any]:
    """The fields that say what is left (no account ids or e-mail)."""
    rl = usage.get("rate_limit") or {}
    cr = usage.get("credits") or {}

    def window(w: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if not w else {"used_percent": w.get("used_percent"), "reset_at": w.get("reset_at")}

    return {
        "plan": usage.get("plan_type"),
        "limit_reached": rl.get("limit_reached"),
        "five_hour": window(rl.get("primary_window")),
        "weekly": window(rl.get("secondary_window")),
        "credits": float(cr["balance"]) if cr.get("balance") is not None else None,
        "credits_unlimited": cr.get("unlimited"),
    }


def credentials(auth: Path) -> tuple[str, str]:
    tokens = json.loads(auth.read_text()).get("tokens") or {}
    access, account = tokens.get("access_token"), tokens.get("account_id")
    if not access or not account:
        raise SystemExit(f"{auth}: no Codex CLI login (log in with the Codex CLI)")
    claims = json.loads(base64.urlsafe_b64decode(access.split(".")[1] + "=="))
    if float(claims["exp"]) <= time.time():
        raise SystemExit(f"{auth}: the access token has expired; run the Codex CLI once, it refreshes its own file")
    return access, account


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The ChatGPT plan's Codex windows and credits, as one JSON line.")
    ap.add_argument("--auth", type=Path, default=AUTH)
    a = ap.parse_args(argv)
    token, account = credentials(a.auth)
    req = urllib.request.Request(
        URL, headers={"Authorization": f"Bearer {token}", "chatgpt-account-id": account, "originator": "pi"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print(json.dumps({"t": round(time.time()), **summary(json.loads(r.read()))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
