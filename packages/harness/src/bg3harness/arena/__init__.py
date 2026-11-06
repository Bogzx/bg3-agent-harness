"""Two models fight each other in one turn-based fight: the arena a match plays (spec), the match itself (match:
run_arena with the game, the agents and the clock passed in), the agents' live processes and model-time clock
(agents) and the live game (live). The harness never imports the bench: runner/arena_cli.py loads the arena file
and calls run_arena."""

from bg3harness.arena.match import AgentExit, ArenaOutcome, run_arena
from bg3harness.arena.spec import ArenaSpec, ArenaSpecError, Side

__all__ = ["AgentExit", "ArenaOutcome", "ArenaSpec", "ArenaSpecError", "Side", "run_arena"]
