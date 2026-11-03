"""Which tools the server offers (research/arch/harness.md §3.1): the harness sets BG3AI_TOOLS and BG3AI_TOOLS_OFF
in the session env and the server removes every other tool at startup, so no client can call a tool its condition
switches off (a client's own allowlist only covers its built-ins).

BG3AI_TOOLS: comma list of groups and single tool names to keep. Unset or empty = every tool (today's server).
The "game" group (every tool in no other group) is always on. BG3AI_TOOLS_OFF: groups or names removed on top, or
a named list (LISTS, e.g. "arena-v1") standing for its groups and names.
An unknown group or tool name raises ValueError: the server fails at startup instead of serving the wrong set.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

GROUPS: dict[str, tuple[str, ...]] = {
    "memory": (
        "memory_list",
        "memory_read",
        "memory_search",
        "memory_write",
        "memory_edit",
        "memory_append",
        "note",
        "session_start",
        "end_session",
        "recall",
    ),
    "wiki": ("wiki_search", "wiki_page"),
    "reload": ("load_game",),
    "saves": ("save_game", "list_saves"),
    "screenshots": ("screenshot", "zoom"),
    "raw_input": ("press_key", "click", "ui_dump"),
}
GAME = "game"

# Named lists for BG3AI_TOOLS_OFF (only there): a name stands for its groups and tools. "arena-v1": a two-team match,
# one fight from its first turn to its last, played by two models on one game client. Off: memory across sessions, the
# wiki, saves and reloads, screenshots and raw input (the one client's screen and input show both teams), ui_action (a
# named UI command on the one client's screen carries no side check), travel, camp and rest, conversations and rolls,
# trade and the out-of-fight world actions, looting, character creation and levelling, the journal and map. confirm and
# dismiss_popups stay: a popup can come up mid-fight; only the acting side answers one (uitools._side_gate).
LISTS: dict[str, tuple[str, ...]] = {
    "arena-v1": (
        "memory",
        "wiki",
        "reload",
        "saves",
        "screenshots",
        "raw_input",
        "ui_action",
        "fast_travel",
        "camp",
        "swap_party",
        "long_rest",
        "short_rest",
        "rest_if_needed",
        "travel_to",
        "talk_to",
        "choose_dialogue",
        "dialogue_advance",
        "get_dialogue",
        "roll_check",
        "trade",
        "pickpocket",
        "lockpick",
        "disarm",
        "loot",
        "loot_corpse",
        "loot_area",
        "pick_up",
        "feed_gale",
        "insert_item",
        "read_all",
        "place_marker",
        "delete_marker",
        "new_game",
        "character_creation_options",
        "create_character",
        "get_level_up_options",
        "level_up",
        "prepare_spells",
        "get_journal",
        "get_map",
    ),
}


def _names(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _expand(names: list[str], tools: set[str], var: str, lists: bool = False) -> set[str]:
    out: set[str] = set()
    for name in names:
        if lists and name in LISTS:
            out |= _expand(list(LISTS[name]), tools, f"{var} list {name}")
        elif name == GAME:
            out |= tools - {t for g in GROUPS.values() for t in g}
        elif name in GROUPS:
            out |= set(GROUPS[name])
        elif name in tools:
            out.add(name)
        else:
            raise ValueError(f"{var}: unknown tool group or tool name {name!r} (groups: {', '.join([GAME, *GROUPS])})")
    return out


def disabled(tools: Iterable[str], env: Mapping[str, str]) -> set[str]:
    """The tools to remove from a server that registered `tools`, for BG3AI_TOOLS / BG3AI_TOOLS_OFF in env."""
    tools = set(tools)
    grouped = {t for g in GROUPS.values() for t in g}
    if missing := sorted(grouped - tools):  # a renamed tool must not silently stay on or off
        raise ValueError(f"capabilities: grouped tools not registered: {', '.join(missing)}")
    on = _names(env.get("BG3AI_TOOLS"))
    keep = _expand([GAME, *on], tools, "BG3AI_TOOLS") if on else set(tools)
    keep -= _expand(_names(env.get("BG3AI_TOOLS_OFF")), tools, "BG3AI_TOOLS_OFF", lists=True)
    return tools - keep


NOTE_REFUSED = "REFUSED: note= is not available (memory tools are off in this session). Call again without note."
