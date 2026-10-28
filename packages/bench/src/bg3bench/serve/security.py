"""The panel's request checks and secret handling: pure functions over headers and strings, plus the token file.

The panel holds model API keys and a token that submits in the user's name, so:

- it binds 127.0.0.1 only and answers only a Host of `127.0.0.1:<port>` or `localhost:<port>` (else 421): a page on
  another name that resolves to 127.0.0.1 (DNS rebinding) sends its own name as the Host;
- every non-GET request carries the write token in `X-Panel-Token` (never a cookie, so a cross-site form has none)
  and `Content-Type: application/json` (a cross-origin page can't send that without a CORS preflight, and the panel
  sends no CORS headers at all);
- a key is shown masked (`mask`), never returned whole.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Protocol

TOKEN_HEADER = "X-Panel-Token"
TOKEN_FILE = "panel-token"
JSON = "application/json"


class Headers(Protocol):
    def get(self, name: str, /) -> str | None: ...


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def host_ok(host: str | None, port: int) -> bool:
    """The Host header names this panel by its loopback name and port, exactly (case-insensitive name)."""
    return host is not None and host.strip().lower() in allowed_hosts(port)


def write_refusal(headers: Headers, token: str, port: int) -> str | None:
    """Why a write is refused (None: allowed). The Host was already checked; this adds the token, the JSON
    content type and, when the browser sends one, an Origin of this panel."""
    origin = headers.get("Origin")
    if origin is not None and origin.lower() not in {f"http://{h}" for h in allowed_hosts(port)}:
        return "cross-origin write"
    sent = headers.get(TOKEN_HEADER) or ""
    if not hmac.compare_digest(sent.encode(), token.encode()):
        return f"bad or missing {TOKEN_HEADER}"
    ctype = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ctype != JSON:
        return f"writes need Content-Type: {JSON}"
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path` with mode 600 from the first byte (the file is created 600, then renamed over)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)  # an older tmp file keeps its mode through O_TRUNC
    os.replace(tmp, path)


def issue_token(config_dir: Path) -> str:
    """A fresh write token, saved to <config_dir>/panel-token (mode 600) for tools that call the panel."""
    token = new_token()
    write_private(config_dir / TOKEN_FILE, token + "\n")
    return token


def mask(value: str | None) -> str | None:
    """What the panel shows of a secret: None when unset, the last 4 characters of a long one, else only that it is
    set. Never more than 4 characters of it."""
    if not value:
        return None
    return f"…{value[-4:]}" if len(value) >= 16 else "set"
