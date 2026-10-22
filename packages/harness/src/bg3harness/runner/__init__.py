"""The session runner (Phase D.3): runner/play.py's Runner and its parts, split by concern.

env (paths, timings, log), policy (pure session logic), codewatch, state (RunnerState, Options, the hooks),
game_side / agent_side / checks_side (SessionRunner's mixins), core (SessionRunner, PlaythroughParts), run_host (the
bench's live RunHost).
"""
