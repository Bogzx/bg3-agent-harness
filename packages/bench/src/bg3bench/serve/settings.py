"""The panel's Settings: API keys in the repo's .env, the site token in its own file, and the panel's preferences.

Keys are read by name (a profile's `api_key_env`) from the repo's .env, the file the runner reads them from
(bg3harness.client_setup.read_dotenv), and written back one line at a time. Nothing here returns a key: callers get
`security.mask` of it. The site token lives in <config>/site-token (mode 600), outside the repo, so nothing built from
the repo tree can pick it up. Preferences (the YouTube question, the recordings' disk cap) are <config>/panel.json.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

from bg3harness.client_setup import read_dotenv

from bg3bench.serve import security

KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_SECRET = 512
SITE_TOKEN = "site-token"
PREFS = "panel.json"
DEFAULT_PREFS: dict[str, Any] = {"youtube_verified": None, "record_cap_gb": 50}
PREF_CHECKS: dict[str, Callable[[Any], bool]] = {
    "youtube_verified": lambda v: v is None or isinstance(v, bool),  # None: not answered yet
    "record_cap_gb": lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 10_000,
}


class SettingsError(ValueError):
    pass


def git_ignored(path: Path) -> bool:
    """`git check-ignore` says the file is ignored in its checkout (False outside a checkout or without git)."""
    try:
        p = subprocess.run(
            ["git", "-C", str(path.parent), "check-ignore", "-q", path.name],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def check_secret(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_SECRET:
        raise SettingsError(f"the value must be a non-empty string of at most {MAX_SECRET} characters")
    if any(c.isspace() or not c.isprintable() for c in value) or value[0] in "\"'":
        raise SettingsError("the value has whitespace, quotes or control characters")
    return value


def set_dotenv(path: Path, name: str, value: str) -> None:
    """Set NAME=value in the .env file: replaces its line (`export ` kept), else appends; the file stays mode 600."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    pat = re.compile(rf"^\s*(export\s+)?{re.escape(name)}\s*=")
    out: list[str] = []
    done = False
    for ln in lines:
        m = pat.match(ln)
        if m and not done:
            out.append(f"{m.group(1) or ''}{name}={value}")
            done = True
        elif not m:
            out.append(ln)
    if not done:
        out.append(f"{name}={value}")
    security.write_private(path, "\n".join(out) + "\n")


class Settings:
    def __init__(
        self,
        dotenv: Path,
        config_dir: Path,
        key_names: Callable[[], Iterable[str]],
        *,
        ignored: Callable[[Path], bool] = git_ignored,
    ):
        self.dotenv, self.config_dir, self.key_names, self.ignored = dotenv, config_dir, key_names, ignored

    def prefs(self) -> dict[str, Any]:
        try:
            raw: Any = json.loads((self.config_dir / PREFS).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            return dict(DEFAULT_PREFS)
        saved = cast(dict[str, Any], raw)
        return {k: saved.get(k, v) for k, v in DEFAULT_PREFS.items()}

    def site_token(self) -> str | None:
        try:
            return (self.config_dir / SITE_TOKEN).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def view(self) -> dict[str, Any]:
        """Everything the Settings screen shows; keys and the site token masked."""
        env = read_dotenv(self.dotenv)
        return {
            "dotenv": str(self.dotenv),
            "dotenv_ignored": self.ignored(self.dotenv),
            "keys": [{"name": n, "masked": security.mask(env.get(n))} for n in sorted(set(self.key_names()))],
            "site_token": security.mask(self.site_token()),
            "prefs": self.prefs(),
        }

    def set_key(self, name: Any, value: Any) -> dict[str, Any]:
        if not isinstance(name, str) or not KEY_NAME.match(name) or name not in set(self.key_names()):
            raise SettingsError("name must be the api_key_env of a profile")
        value = check_secret(value)
        if not self.ignored(self.dotenv):
            raise SettingsError(f"{self.dotenv} is not ignored by git: add .env to .gitignore before saving a key")
        set_dotenv(self.dotenv, name, value)
        return {"name": name, "masked": security.mask(value)}

    def set_site_token(self, value: Any) -> dict[str, Any]:
        value = check_secret(value)
        security.write_private(self.config_dir / SITE_TOKEN, value + "\n")
        return {"site_token": security.mask(value)}

    def set_prefs(self, body: dict[str, Any]) -> dict[str, Any]:
        prefs = self.prefs()
        for k, v in body.items():
            ok = PREF_CHECKS.get(k)
            if ok is None or not ok(v):
                raise SettingsError(f"bad preference {k!r}")
            prefs[k] = v
        security.write_private(self.config_dir / PREFS, json.dumps(prefs, indent=1, sort_keys=True) + "\n")
        return {"prefs": prefs}
