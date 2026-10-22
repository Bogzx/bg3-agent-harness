"""The playthrough's runner parts (moved from runner/legacy_playthrough.py and bg3harness.runner.policy, Phase D.2).

prompts        the kickoff and the playthrough-only notes (scenario start, fix-notes, the stuck directive): pure
memory_session the memory session's pure parts: its clock env, session.py start's and stop's answers, HANDOFF's sid
memory_side    the memory session (session.py start/stop/session-end/end) as the legacy Runner runs it
keeper_side    the plan-line check, the memory keeper between sessions, the session review
session_side   one playthrough session (play_session) and its dry run
loop           the continuous loop (run): sessions, fix-notes, API back-off, cost/heat pauses, restarts
playthrough    PlaythroughSide: all of them, what bg3harness.runner.core.SessionRunner calls as PlaythroughParts

`runner/play.py run` plays with runner/legacy_playthrough.py's Runner = PlaythroughSide + the legacy series' parts
+ SessionRunner. What the bench's kickoff also needs (the resume prompts, the pace pre-flight, the new-game note,
the roleplay texts) stays in bg3harness.runner.policy, the profile in bg3harness.runner.profile.
"""
