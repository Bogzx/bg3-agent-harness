"""The bridge's tools by class, for whoever needs to tell them apart without loading the tool layer (the evidence
video draws acting calls apart from reading ones).

READ_TOOLS only look (the game, the wiki, the memory, the clock): they stay open after the session's deadline, and a
run of wait_for with only these in between counts as idle. Every other tool acts.
"""

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

# A tool result that starts with one of these is a failure: the tool did nothing, or (CHAIN STOPPED first) a
# combat_turn chain played none of its members' turns.
FAILED = ("FAILED", "NOT READY", "REFUSED", "CHAIN STOPPED")


def failed(text: str) -> bool:
    """Whether a tool's text result reports a failure (the server sends it with is_error set)."""
    return text.startswith(FAILED)
