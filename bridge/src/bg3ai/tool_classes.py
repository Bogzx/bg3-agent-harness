"""The bridge's tools by class, for whoever needs to tell them apart without loading the tool layer (the evidence
video draws acting calls apart from reading ones).

READ_TOOLS only look (the game, the wiki, the memory, the clock): they stay open after the session's deadline, and a
run of wait_for with only these in between counts as idle. Every other tool acts.
"""

import re

READ_TOOLS = frozenset(
    {
        "look",
        "look_at",
        "get_status",
        "get_nearby",
        "get_frontier",
        "get_layout",
        "get_map",
        "session_clock",
        "wiki_page",
        "wiki_search",
        "get_party",
        "list_saves",
        "get_inventory",
        "screenshot",
        "zoom",
        "route",
        "get_journal",
        "get_combat_state",
        "get_dialogue",
        "ability_preview",
        "get_level_up_options",
        "recall",
        "memory_list",
        "memory_read",
        "memory_search",
        "session_start",
    }
)

# What a failed call says: the tool did nothing (refused it, failed at it, or the game wasn't ready), or (CHAIN STOPPED
# as its outcome) a combat_turn chain played none of its members' turns.
FAILED = ("FAILED", "NOT READY", "REFUSED", "CHAIN STOPPED")

# The first text result of a session carries the memory briefing first; the call's own result follows this line.
RESULT_MARK = "\n\n=== the result of your call ===\n"

_WORD = "|".join(FAILED)
# A result's first line is its outcome: "FAILED: ...", "<tool>: FAILED — ...", "travel_to (x,y,z): REFUSED: ...",
# "swap_party: FAILED at go to camp ...", or "1. move_to: FAILED ..." (a batch whose first step failed did nothing).
_HEAD = re.compile(rf"(?:1\. )?(?:[a-z_]+(?: [^:\n]*)?: )?(?:{_WORD})\b")


def failed(text: str, tool: str = "") -> bool:
    """Whether a tool's text result reports that the call failed (the server sends it with is_error set).

    The outcome is the first line, or a line naming the called tool after notes the tool put before it (short_rest's
    "(closed the trade window first)", swap_party's steps). Not failures: a call that did part of its work (a
    combat_turn that played a turn, then a member's line or a CHAIN STOPPED after it; "partly done"; a batch that
    stopped at a later step), a job the game or an interrupt cut short (INTERRUPTED, STOPPED, TIMEOUT, CANCELLED), a
    roll that failed (the tool worked), a SAVE FAILED note from a background save, and wait_for's report of a queue
    that STOPPED: wait_for waited and says how the queue ended; the step that failed was the queue's, and an interrupt
    stops a queue the same way.
    """
    text = text.rpartition(RESULT_MARK)[2]
    if _HEAD.match(text):
        return True
    if not tool:
        return False
    named = re.compile(rf"{re.escape(tool)}(?: [^:\n]*)?: (?:{_WORD})\b")
    return any(named.match(line) for line in text.splitlines())
