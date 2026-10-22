"""The bridge's settings, read from the environment in one place.

`from_env` is pure (it takes the mapping, the repo root and the home dir), so a test or a second server in one
process builds its own Settings instead of patching module globals. tools/core.py reads os.environ through it once,
at import, and passes the values on (Game gets port, pool size and job push from it).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # the checkout (bridge/src/bg3ai -> repo root)


@dataclass(frozen=True)
class SaveIdentity:
    """Which saves are this run's (runner I1): folders are "<character name>-<number>__<label>"."""

    owner: str = ""  # $BG3AI_SAVE_OWNER: the character's name
    prefix: str = ""  # $BG3AI_SAVE_PREFIX, else the owner's name and the dash the game adds
    playthrough: str = ""  # $BG3AI_PLAYTHROUGH
    # $BG3AI_SAVE_GATES "1": the runner opened the save/load gates for this game, "0": it closed them (not our
    # character); None: hand-run.
    gates: str | None = None
    # $BG3AI_SAVES_AFTER, epoch seconds: a `run --runs` playthrough's saves are only those written since its run
    # started (the runs of a series share one character name, so the prefix alone would let run 2 load run 1's).
    after: float = 0.0
    start_save: str = ""  # $BG3AI_START_SAVE: a scenario's start save, older than the run, still its own
    # $BG3AI_SAVES_EXCLUDE (newline-separated): this character's save folders that another playthrough (same name) or
    # a worker wrote (the runner's foreign_saves): never in list_saves / load_game.
    exclude: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Settings:
    repo: Path
    runtime: Path  # $BG3AI_RUNTIME: actions.jsonl, metrics, locks
    play_dir: Path  # $BG3AI_PLAY_DIR
    memory: Path  # $BG3AI_MEMORY_DIR, else play_dir/memory
    knowledge: Path  # $BG3_KNOWLEDGE_DIR, else play_dir/knowledge
    save_dir: Path  # $BG3AI_SAVE_DIR, else the profile's Savegames/Story (same lookup as steam_paths.saves_dir)
    deadline: float  # $BG3AI_SESSION_DEADLINE: epoch seconds, 0 = no deadline
    wrapup_at: float  # $BG3AI_SESSION_WRAPUP: epoch seconds the runner's wrap-up starts, 0 = not set
    progress_path: str  # $BG3AI_PROGRESS_PATH, else next to a $BG3AI_METRICS_PATH Sxxx.metrics.jsonl, else ""
    hold_path: str  # $BG3AI_FIGHT_HOLD_PATH: the fight hold the runner reads, "" = none
    port: int  # $BG3LE_DEBUG_PORT
    rpc_conns: int  # $BG3AI_RPC_CONNS: LuaDebug connections in the pool (1 = one serialised connection)
    job_push: bool  # $BG3AI_JOB_PUSH != "0": wait on the mod's job-finished prints (jobpush.py)
    saves: SaveIdentity


def _float(env: Mapping[str, str], key: str) -> float:
    try:
        return float(env.get(key) or 0)
    except ValueError:
        return 0.0


def from_env(env: Mapping[str, str], repo: Path = REPO, home: Path | None = None) -> Settings:
    play = Path(env.get("BG3AI_PLAY_DIR") or repo / "play")
    home = home or Path.home()
    profile = Path(env.get("BG3AI_PROFILE_DIR") or home / ".local/share/Larian Studios/Baldur's Gate 3")
    metrics = env.get("BG3AI_METRICS_PATH", "")
    return Settings(
        repo=repo,
        runtime=Path(env.get("BG3AI_RUNTIME") or repo / "runtime"),
        play_dir=play,
        memory=Path(env.get("BG3AI_MEMORY_DIR") or play / "memory"),
        knowledge=Path(env.get("BG3_KNOWLEDGE_DIR") or play / "knowledge"),
        save_dir=Path(env.get("BG3AI_SAVE_DIR") or profile / "PlayerProfiles/Public/Savegames/Story"),
        deadline=float(env.get("BG3AI_SESSION_DEADLINE") or 0),
        wrapup_at=_float(env, "BG3AI_SESSION_WRAPUP"),
        progress_path=env.get("BG3AI_PROGRESS_PATH")
        or (re.sub(r"\.metrics\.jsonl$", ".progress.jsonl", metrics) if metrics.endswith(".metrics.jsonl") else ""),
        hold_path=env.get("BG3AI_FIGHT_HOLD_PATH", ""),
        port=int(env.get("BG3LE_DEBUG_PORT") or 9998),
        rpc_conns=max(1, int(env.get("BG3AI_RPC_CONNS") or 4)),
        job_push=env.get("BG3AI_JOB_PUSH", "1") != "0",
        saves=save_identity(env),
    )


def save_identity(env: Mapping[str, str]) -> SaveIdentity:
    owner = env.get("BG3AI_SAVE_OWNER", "")
    return SaveIdentity(
        owner=owner,
        prefix=env.get("BG3AI_SAVE_PREFIX", "") or (f"{owner}-" if owner else ""),
        playthrough=env.get("BG3AI_PLAYTHROUGH", ""),
        gates=env.get("BG3AI_SAVE_GATES"),
        after=_float(env, "BG3AI_SAVES_AFTER"),
        start_save=env.get("BG3AI_START_SAVE") or "",
        exclude=frozenset(x for x in (env.get("BG3AI_SAVES_EXCLUDE") or "").split("\n") if x),
    )
