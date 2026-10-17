#!/usr/bin/env python3
"""Host tuning before a game launch, opt-in per machine (runner/launch_game.sh calls `apply`).

the test machine cut its power twice under the naut series load (2026-10-16 ~20:07, 2026-10-17 ~16:01) with
platform_profile 'performance' and the NVIDIA GPU uncapped. Both settings reset on every reboot, so the launcher
sets them again from the machine's marker file $BG3AI_CONFIG_DIR (default $XDG_CONFIG_HOME/bg3ai, else
~/.config/bg3ai) / host-tuning, one `key=value` per line (# comments):

    platform_profile=balanced     # echo balanced | sudo -n tee /sys/firmware/acpi/platform_profile
    nvidia_max_clock=1500         # sudo -n nvidia-smi -lgc <nvidia_min_clock>,1500
    nvidia_min_clock=210          # optional (default 210)

No marker = nothing to do. Only `sudo -n`: when it needs a password the setting is skipped with a warning (never a
prompt, never a failed launch). Every line it prints says what it set, found already set, or skipped.

    python3 runner/host_tuning.py apply      # what launch_game.sh runs
    python3 runner/host_tuning.py plan       # what it would do (launch_game.sh --dry-run)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")
KEYS = ("platform_profile", "nvidia_max_clock", "nvidia_min_clock")
NVIDIA_MIN_CLOCK = 210


def marker(home: Path | None = None) -> Path:
    """The same config dir as tools/steam_paths.py's require-steam-offline marker."""
    base = os.environ.get("BG3AI_CONFIG_DIR")
    if base:
        return Path(base) / "host-tuning"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg and home is None else (home or Path.home()) / ".config"
    return root / "bg3ai" / "host-tuning"


def read_config(path: Path) -> tuple[dict[str, str], list[str]]:
    """(settings, warnings) of the marker; ({}, []) without one."""
    try:
        text = path.read_text()
    except OSError:
        return {}, []
    cfg, warn = {}, []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        k, sep, v = (x.strip() for x in line.partition("="))
        if not sep or k not in KEYS or not v:
            warn.append(f"{path}:{n}: ignored {raw.strip()!r} (keys: {', '.join(KEYS)})")
        elif k != "platform_profile" and not v.isdigit():
            warn.append(f"{path}:{n}: ignored {raw.strip()!r} ({k} is MHz)")
        else:
            cfg[k] = v
    return cfg, warn


def _sudo(args: list[str], run, stdin: str | None = None) -> str | None:
    """None when `sudo -n <args>` worked, else why not (one line)."""
    try:
        r = run(["sudo", "-n", *args], input=stdin if stdin is not None else "", capture_output=True, text=True,
                timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if r.returncode == 0:
        return None
    return ((r.stderr or r.stdout).strip().splitlines() or [f"exit {r.returncode}"])[-1][:160]


def apply(cfg: dict[str, str], run=subprocess.run, profile: Path = PLATFORM_PROFILE, dry: bool = False) -> list[str]:
    """Applies cfg; returns the log lines ('set …', 'already …', 'WARNING: skipped …')."""
    out = []
    want = cfg.get("platform_profile")
    if want:
        try:
            now = profile.read_text().strip()
        except OSError:
            now = None
        try:
            choices = (profile.parent / "platform_profile_choices").read_text().split()
        except OSError:
            choices = []
        if now is None:
            out.append(f"WARNING: skipped platform_profile={want}: no {profile}")
        elif now == want:
            out.append(f"platform_profile already {want}")
        elif choices and want not in choices:
            out.append(f"WARNING: skipped platform_profile={want}: not one of {' '.join(choices)} (now {now})")
        elif dry:
            out.append(f"would set platform_profile {now} -> {want}")
        else:
            err = _sudo(["tee", str(profile)], run, stdin=want + "\n")
            out.append(f"set platform_profile {now} -> {want}" if err is None else
                       f"WARNING: skipped platform_profile={want} (now {now}): sudo -n tee failed: {err}")
    top = cfg.get("nvidia_max_clock")
    if top:
        clocks = f"{cfg.get('nvidia_min_clock') or NVIDIA_MIN_CLOCK},{top}"
        if dry:
            out.append(f"would lock the NVIDIA GPU clock to {clocks} MHz (nvidia-smi -lgc)")
        else:
            err = _sudo(["nvidia-smi", "-lgc", clocks], run)
            out.append(f"locked the NVIDIA GPU clock to {clocks} MHz (nvidia-smi -lgc)" if err is None else
                       f"WARNING: skipped nvidia_max_clock={top}: sudo -n nvidia-smi -lgc {clocks} failed: {err}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("apply", "plan"):
        print(__doc__, file=sys.stderr)
        return 2
    path = marker()
    cfg, warn = read_config(path)
    lines = warn + apply(cfg, dry=argv[0] == "plan")
    for ln in lines:
        print(f"host tuning ({path}): {ln}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
