"""A stub bg3 MCP server for the baseline tests: the tool names the agents use plus forbidden ones, and a log of
every call (to the file in $STUB_LOG, one JSON line per call). `python stub_server.py` serves it over stdio."""

from __future__ import annotations

import json
import os

from mcp.server import MCPServer


def build(log: list | None = None) -> MCPServer:
    srv = MCPServer("bg3")
    state = {"n": 0}

    def record(tool: str, **args) -> None:
        entry = {"tool": tool, "args": {k: v for k, v in args.items() if v is not None}}
        if log is not None:
            log.append(entry)
        if os.environ.get("STUB_LOG"):
            with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    @srv.tool()
    def look(full: bool = False) -> str:
        """What is in view."""
        record("look")
        state["n"] += 1
        # what is in view changes from call to call, the same way every run
        return ["Nothing in view.", "e3 goblin (hostile), e12 chest", "e7 Shadowheart, e12 chest, e40 door"][
            state["n"] % 3
        ]

    def plain(name: str):
        def tool() -> str:
            record(name)
            return f"{name}: ok"

        return tool

    for name in ("session_start", "end_session", "loot", "dialogue_advance", "end_turn", "load_game", "save_game"):
        srv.tool(name=name, description=name)(plain(name))

    @srv.tool()
    def move_to(target: str) -> str:
        """Walk."""
        record("move_to", target=target)
        return f"walking to {target}"

    @srv.tool()
    def interact(target: str) -> str:
        """Use."""
        record("interact", target=target)
        return "FAILED: nothing to use" if target == "e3" else "used"

    @srv.tool()
    def talk_to(npc: str) -> str:
        """Talk."""
        record("talk_to", npc=npc)
        return "REFUSED: hostile"

    @srv.tool()
    def pick_up(item: str) -> str:
        """Take."""
        record("pick_up", item=item)
        return "taken"

    @srv.tool()
    def walk_toward(direction: str = "") -> str:
        """Walk a direction."""
        record("walk_toward", direction=direction)
        return f"walking {direction}"

    @srv.tool()
    def choose_dialogue(option: int) -> str:
        """Answer."""
        record("choose_dialogue", option=option)
        return "no dialogue open"

    @srv.tool()
    def combat_turn(plan: str) -> str:
        """A turn."""
        record("combat_turn", plan=plan)
        return "not in combat"

    return srv


if __name__ == "__main__":
    build().run("stdio")
