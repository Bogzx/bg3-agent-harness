"""The supervisor's Keeper: the memory keeper pass (memory/keeper.py run_keeper) between two sessions.

Which client keeps the memory comes from the session runner (`client_for`): with the legacy CLI, play.py runs the
keeper itself for every client but pi (play.py --profile pi needs --no-keeper), so only a pi playthrough's keeper
runs here, through pi with the playthrough's client profile. The pass's model, effort, minutes and cost cap
(max_cost_usd; a paid client also needs runtime/budget.json: memory/keeper_spend.py) are the config's [keeper]; its
log is <runtime>/sessions/<pt>/Sxxx-keeper.* and its result a keeper_history.jsonl row, as play.py writes them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config
from .memory import keeper
from .supervisor import SessionResult

# (client name, parsed client profile or None) for a config, or None when the session runner keeps the memory itself
ClientFor = Callable[[config.PlaythroughConfig], tuple[str, Any] | None]


class MemoryKeeper:
    def __init__(
        self,
        runtime: Path,
        playthroughs: Path,
        client_for: ClientFor,
        clock: Callable[[], float],
        run: Callable[..., keeper.KeeperResult] = keeper.run_keeper,
    ):
        self.runtime, self.playthroughs, self.client_for, self.clock, self.run = (
            runtime,
            playthroughs,
            client_for,
            clock,
            run,
        )

    def run_keeper(self, pt: str, result: SessionResult) -> None:
        cfg = config.load(self.playthroughs / pt)
        who = self.client_for(cfg)
        sid = result.session.split("-")[0]
        if who is None or not sid[1:].isdigit():  # "?": the runner reported no session
            return
        client, profile = who
        d = self.runtime / "sessions" / pt
        d.mkdir(parents=True, exist_ok=True)
        model = cfg.keeper.model or cfg.agent.model
        r = self.run(
            self.playthroughs / pt / "memory",
            int(sid[1:]),
            d / f"{sid}-keeper",
            model,
            cfg.keeper.effort,
            cfg.keeper.minutes,
            client=client,
            profile=profile,
            max_cost=cfg.keeper.max_cost_usd,
            budget=self.runtime / "budget.json",
        )
        row = {"t": round(self.clock(), 1), "client": client, "model": model, "effort": cfg.keeper.effort}
        with (d / "keeper_history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({**row, **r.__dict__}) + "\n")
